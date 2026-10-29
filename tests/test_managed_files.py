"""Tests for managed settings without real ``sudo`` or ``/etc`` writes."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import ucode.agents.claude as claude_agent
import ucode.codex_config as codex_config
import ucode.config_io as config_io
from ucode import managed_files
from ucode.env import NONINTERACTIVE_ENV

_REAL_SUDO_REPLACE = managed_files._sudo_replace
# Captured before the autouse ``_supported`` fixture pins the module attribute to True.
_REAL_MANAGED_FILES_SUPPORTED = managed_files.managed_files_supported
_REAL_WINDOWS_REPLACE = managed_files._windows_replace
_REAL_SUBPROCESS_RUN = managed_files.subprocess_cross_os.run


@pytest.fixture(autouse=True)
def _reset_dry_run():
    config_io.set_dry_run(False)
    yield
    config_io.set_dry_run(False)


@pytest.fixture(autouse=True)
def _supported(monkeypatch):
    # Pin platform support on so tests are deterministic on any host.
    monkeypatch.setattr(managed_files, "managed_files_supported", lambda: True)
    monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: True)
    # These tests cover the sudo writer; on a Windows host the elevated-write path would run
    # instead. Not pinned elsewhere: the real worker shell picks cp flags from the host OS.
    if managed_files.current_os() is managed_files.OS.WINDOWS:
        monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS.LINUX)


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    path = tmp_path / "managed-backups"
    monkeypatch.setattr(managed_files, "MANAGED_BACKUP_DIR", path)
    monkeypatch.setattr(managed_files, "MANAGED_BACKUP_MANIFEST_PATH", path / "manifest.json")
    return path


class _FakeSudoWorkerProcess:
    def __init__(self, command: list[str], **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.requests: list[tuple[str, str, str]] = []
        self.responses: list[str] = []
        self.returncode: int | None = None
        self.quit_received = False
        self.exit_on_request = False
        self.stderr = io.StringIO()
        self.stdin = self._Stdin(self)
        self.stdout = self._Stdout(self)

    @staticmethod
    def _decode(value: str) -> str:
        return base64.b64decode(value).decode("utf-8")

    class _Stdin:
        def __init__(self, process):
            self.process = process
            self.closed = False

        def write(self, data: str) -> int:
            if data == "QUIT\n":
                self.process.quit_received = True
                return len(data)
            if self.process.exit_on_request:
                # Simulates sudo exiting after failed authentication: no response, nonzero exit.
                self.process.returncode = 1
                self.process.responses.append("")
                return len(data)
            operation, request_id, source, target = data.split()
            assert operation == "REPLACE"
            source_path = self.process._decode(source)
            target_path = self.process._decode(target)
            text = Path(source_path).read_text(encoding="utf-8")
            self.process.requests.append((source_path, target_path, text))
            self.process.responses.append(f"OK {request_id}\n")
            return len(data)

        def flush(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    class _Stdout:
        def __init__(self, process):
            self.process = process
            self.closed = False

        def readline(self) -> str:
            return self.process.responses.pop(0)

        def close(self) -> None:
            self.closed = True

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class TestClearImmutableStatDenied:
    def test_stat_denied_path_returns_no_flags_without_raising(self, monkeypatch):
        # Regression: `_clear_immutable` ran an unguarded path.exists() inside the sudo write; under a
        # root-locked /etc/codex that raised PermissionError and aborted the write ("without root").
        class _StatDenied:
            def exists(self):
                raise PermissionError(13, "Permission denied")

        # Ensure no sudo subprocess is attempted if the guard ever regresses.
        monkeypatch.setattr(
            managed_files.subprocess, "run", lambda *a, **k: pytest.fail("should not shell out")
        )
        assert managed_files._clear_immutable(_StatDenied()) == ()


class TestImmutableFlags:
    def test_macos_flags_are_cleared_and_restored(self, tmp_path, monkeypatch):
        path = tmp_path / "managed.json"
        path.write_text("{}", encoding="utf-8")
        calls: list[list[str]] = []

        def run(command, **kwargs):
            calls.append(command)
            stdout = "schg,uchg\n" if command[0] == "/usr/bin/stat" else ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS.MACOS)
        monkeypatch.setattr(managed_files.subprocess, "run", run)

        flags = managed_files._clear_immutable(path)
        managed_files._restore_immutable(path, flags)

        assert flags == ("schg", "uchg")
        assert ["/usr/bin/sudo", "chflags", "noschg,nouchg", str(path)] in calls
        assert ["/usr/bin/sudo", "chflags", "schg,uchg", str(path)] in calls

    def test_linux_flags_are_cleared_and_restored(self, tmp_path, monkeypatch):
        path = tmp_path / "managed.toml"
        path.write_text("", encoding="utf-8")
        calls: list[list[str]] = []

        def run(command, **kwargs):
            calls.append(command)
            stdout = "----ia------- managed.toml\n" if command[1] == "lsattr" else ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS.LINUX)
        monkeypatch.setattr(managed_files.subprocess, "run", run)

        flags = managed_files._clear_immutable(path)
        managed_files._restore_immutable(path, flags)

        assert flags == ("i", "a")
        assert ["/usr/bin/sudo", "chattr", "-ia", str(path)] in calls
        assert ["/usr/bin/sudo", "chattr", "+ia", str(path)] in calls


class TestSudoReplace:
    @pytest.mark.parametrize(
        "body_error", [RuntimeError("setup failed"), KeyboardInterrupt(), SystemExit(2)]
    )
    def test_session_cleanup_preserves_active_exception(self, monkeypatch, body_error):
        processes: list[_FakeSudoWorkerProcess] = []
        warnings: list[str] = []

        def popen(command, **kwargs):
            process = _FakeSudoWorkerProcess(command, **kwargs)
            processes.append(process)
            return process

        def failed_close(self):
            raise subprocess.CalledProcessError(1, ["sudo"])

        monkeypatch.setattr(managed_files.subprocess, "Popen", popen)
        monkeypatch.setattr(managed_files._SudoReplaceWorker, "close", failed_close)
        monkeypatch.setattr(managed_files, "print_warning", warnings.append)

        with pytest.raises(type(body_error)) as caught:
            with managed_files.managed_write_session():
                managed_files._session_worker()
                raise body_error

        assert caught.value is body_error
        assert managed_files._managed_write_worker is None
        assert managed_files._managed_write_session_depth == 0
        assert warnings == ["The privileged settings session did not close cleanly."]

    def test_session_reports_cleanup_failure_without_active_exception(self, monkeypatch):
        monkeypatch.setattr(managed_files.subprocess, "Popen", _FakeSudoWorkerProcess)

        def failed_close(self):
            raise subprocess.CalledProcessError(1, ["sudo"])

        monkeypatch.setattr(managed_files._SudoReplaceWorker, "close", failed_close)

        with pytest.raises(RuntimeError, match="Could not close the privileged settings session"):
            with managed_files.managed_write_session():
                managed_files._session_worker()

        assert managed_files._managed_write_worker is None
        assert managed_files._managed_write_session_depth == 0

    @pytest.mark.parametrize("timeouts", [1, 2, 3])
    def test_worker_shutdown_is_bounded_and_closes_streams(self, monkeypatch, timeouts):
        monkeypatch.setattr(managed_files.subprocess, "Popen", _FakeSudoWorkerProcess)
        worker = managed_files._SudoReplaceWorker()
        process = worker.process
        waits: list[float] = []
        signals: list[str] = []

        def wait(timeout):
            waits.append(timeout)
            if len(waits) <= timeouts:
                raise subprocess.TimeoutExpired("sudo", timeout)
            process.returncode = -15
            return process.returncode

        monkeypatch.setattr(process, "wait", wait)
        monkeypatch.setattr(
            process, "terminate", lambda: signals.append("terminate"), raising=False
        )
        monkeypatch.setattr(process, "kill", lambda: signals.append("kill"))

        expected_error = subprocess.TimeoutExpired if timeouts == 3 else RuntimeError
        with pytest.raises(expected_error):
            worker.close()

        assert waits == [5] * min(timeouts + 1, 3)
        assert signals == (["terminate"] if timeouts == 1 else ["terminate", "kill"])
        assert process.quit_received is True
        assert process.stdin.closed
        assert process.stdout.closed
        assert process.stderr.closed

    def test_worker_closes_other_streams_after_broken_stdin(self, monkeypatch):
        monkeypatch.setattr(managed_files.subprocess, "Popen", _FakeSudoWorkerProcess)
        worker = managed_files._SudoReplaceWorker()

        def broken_close():
            raise BrokenPipeError("worker exited")

        monkeypatch.setattr(worker.stdin, "close", broken_close)

        worker.close()

        assert worker.process.returncode == 0
        assert worker.stdout.closed
        assert worker.process.stderr.closed

    def test_session_requests_password_once_across_agent_phases(self, monkeypatch):
        processes: list[_FakeSudoWorkerProcess] = []
        notes: list[str] = []

        def popen(command, **kwargs):
            process = _FakeSudoWorkerProcess(command, **kwargs)
            processes.append(process)
            return process

        monkeypatch.setattr(managed_files.subprocess, "Popen", popen)
        monkeypatch.setattr(managed_files, "print_note", notes.append)

        with managed_files.managed_write_session():
            managed_files._print_managed_write_permission("Claude Code")
            managed_files._session_worker()
            managed_files._print_managed_write_permission("Codex")

        assert notes == ["Enter password once to configure machine-wide coding agent settings."]
        assert len(processes) == 1

    def test_session_restarts_worker_after_it_exits(self, tmp_path, monkeypatch):
        processes: list[_FakeSudoWorkerProcess] = []
        notes: list[str] = []

        def popen(command, **kwargs):
            process = _FakeSudoWorkerProcess(command, **kwargs)
            process.exit_on_request = not processes
            processes.append(process)
            return process

        monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS.LINUX)
        monkeypatch.setattr(managed_files.subprocess, "Popen", popen)
        monkeypatch.setattr(managed_files, "print_note", notes.append)
        first = tmp_path / "one.json"
        second = tmp_path / "two.json"
        monkeypatch.setitem(
            managed_files._SUDO_REPLACE_TARGETS,
            managed_files.OS.LINUX,
            frozenset({first, second}),
        )

        with managed_files.managed_write_session():
            managed_files._print_managed_write_permission("Claude Code")
            with pytest.raises(subprocess.CalledProcessError):
                _REAL_SUDO_REPLACE(first, "first\n")
            managed_files._print_managed_write_permission("Codex")
            _REAL_SUDO_REPLACE(second, "second\n")

        assert len(processes) == 2
        assert processes[0].requests == []
        assert [target for _source, target, _text in processes[1].requests] == [str(second)]
        assert len(notes) == 2

    def test_reused_worker_still_reports_batch_success(self, monkeypatch):
        successes: list[str] = []
        monkeypatch.setattr(managed_files.subprocess, "Popen", _FakeSudoWorkerProcess)
        monkeypatch.setattr(managed_files, "print_note", lambda _message: None)
        monkeypatch.setattr(managed_files, "print_success", successes.append)

        with managed_files.managed_write_session():
            with managed_files.managed_write_batch(["Claude Code"]):
                managed_files._print_managed_write_permission("Claude Code")
                managed_files._session_worker()
            with managed_files.managed_write_batch(["Codex"]):
                managed_files._print_managed_write_permission("Codex")

        assert successes == [
            "Settings configured for Claude Code",
            "Settings configured for Codex",
        ]

    def test_session_shares_one_lazy_sudo_process(self, tmp_path, monkeypatch):
        processes: list[_FakeSudoWorkerProcess] = []

        def popen(command, **kwargs):
            process = _FakeSudoWorkerProcess(command, **kwargs)
            processes.append(process)
            return process

        monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS.LINUX)
        monkeypatch.setattr(managed_files.subprocess_cross_os, "popen", popen)
        monkeypatch.setattr(
            managed_files.subprocess_cross_os,
            "run",
            lambda *args, **kwargs: pytest.fail("session must not start another sudo process"),
        )
        first = tmp_path / "managed settings; $(not-a-command)" / "one.json"
        second = tmp_path / "other settings" / "two 'quoted'.json"
        monkeypatch.setitem(
            managed_files._SUDO_REPLACE_TARGETS,
            managed_files.OS.LINUX,
            frozenset({first, second}),
        )

        with managed_files.managed_write_session():
            _REAL_SUDO_REPLACE(first, "first\n")
            with managed_files.managed_write_session():
                _REAL_SUDO_REPLACE(second, "second\n")

        assert len(processes) == 1
        process = processes[0]
        assert process.command[:3] == ["/usr/bin/sudo", "/bin/sh", "-c"]
        assert process.command[3] == managed_files._SUDO_REPLACE_SCRIPT
        assert process.command[4:] == ["ucode-managed-replace", "session", "linux"]
        assert process.kwargs == {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "bufsize": 1,
        }
        assert [(target, text) for _source, target, text in process.requests] == [
            (str(first), "first\n"),
            (str(second), "second\n"),
        ]
        assert all(not Path(source).exists() for source, _target, _text in process.requests)
        assert process.quit_received is True

    def test_unchanged_reconcile_starts_no_sudo_process(self, tmp_path, monkeypatch):
        path = tmp_path / "managed.json"
        path.write_text("same", encoding="utf-8")
        monkeypatch.setattr(
            managed_files.subprocess,
            "Popen",
            lambda *args, **kwargs: pytest.fail("unchanged file must not start a sudo session"),
        )
        monkeypatch.setattr(
            managed_files.subprocess,
            "run",
            lambda *args, **kwargs: pytest.fail("unchanged file must not invoke sudo"),
        )

        with managed_files.managed_write_session():
            result = managed_files.reconcile_managed_file(
                path,
                "same",
                tool="claude",
                display="Claude Code",
                owned_paths=[["env"]],
                parser=json.loads,
            )

        assert result == "unchanged"

    def test_outside_session_uses_one_sudo_process_and_path_arguments(self, tmp_path, monkeypatch):
        calls: list[tuple[list[str], dict]] = []
        source_path: Path | None = None

        def run(command, **kwargs):
            nonlocal source_path
            source_path = Path(command[7])
            assert source_path.read_text(encoding="utf-8") == "$(not-a-command)\n"
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS.LINUX)
        monkeypatch.setattr(managed_files.subprocess_cross_os, "run", run)
        monkeypatch.setattr(
            managed_files.subprocess_cross_os,
            "popen",
            lambda *args, **kwargs: pytest.fail("one-shot replacement must not start a session"),
        )
        parent = tmp_path / "managed settings; $(not-a-command)"
        path = parent / "policy 'quoted' $HOME.json"
        monkeypatch.setitem(
            managed_files._SUDO_REPLACE_TARGETS,
            managed_files.OS.LINUX,
            frozenset({path}),
        )

        _REAL_SUDO_REPLACE(path, "$(not-a-command)\n")

        assert len(calls) == 1
        command, kwargs = calls[0]
        assert command[:3] == ["/usr/bin/sudo", "/bin/sh", "-c"]
        assert command[3] == managed_files._SUDO_REPLACE_SCRIPT
        assert command[4:7] == ["ucode-managed-replace", "once", "linux"]
        assert command[8:] == [str(path)]
        assert str(path) not in command[3]
        assert str(parent) not in command[3]
        assert "$(not-a-command)\n" not in command[3]
        assert kwargs == {"capture_output": True, "text": True, "check": True, "stdin": None}
        assert source_path is not None and not source_path.exists()

    @pytest.mark.parametrize("denied", [False, True])
    def test_noninteractive_repair_uses_sudo_n_without_session_or_stdin(
        self, tmp_path, monkeypatch, denied
    ):
        calls: list = []
        source_paths: list[Path] = []
        path = tmp_path / "managed-settings.json"
        path.write_text('{"apiKeyHelper": "isaac"}')
        monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS.LINUX)
        monkeypatch.setitem(
            managed_files._SUDO_REPLACE_TARGETS, managed_files.OS.LINUX, frozenset({path})
        )

        def run(command, **kwargs):
            source = Path(command[-2])
            source_paths.append(source)
            assert source.read_text() == '{"apiKeyHelper": "ug auth token"}'
            calls.append((command, kwargs))
            if denied:
                raise subprocess.CalledProcessError(
                    1, command, stderr="sudo: a password is required"
                )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        monkeypatch.setattr(managed_files.subprocess_cross_os, "run", run)
        monkeypatch.setattr(
            managed_files.subprocess_cross_os,
            "popen",
            lambda *args, **kwargs: pytest.fail(
                "headless repair must not start an interactive worker"
            ),
        )

        with managed_files.managed_write_session():
            if denied:
                with pytest.raises(subprocess.CalledProcessError):
                    _REAL_SUDO_REPLACE(path, '{"apiKeyHelper": "ug auth token"}')
            else:
                _REAL_SUDO_REPLACE(path, '{"apiKeyHelper": "ug auth token"}')

        assert len(calls) == 1
        command, kwargs = calls[0]
        assert command[:4] == ["/usr/bin/sudo", "-n", "/bin/sh", "-c"]
        assert command[4] == managed_files._SUDO_REPLACE_SCRIPT
        assert command[5:8] == ["ucode-managed-replace", "once", "linux"]
        assert command[-1] == str(path)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["check"] is True
        assert all(not source.exists() for source in source_paths)

    def test_rejects_target_outside_fixed_allowlist_before_sudo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            managed_files.subprocess,
            "run",
            lambda *args, **kwargs: pytest.fail("rejected target must not invoke sudo"),
        )
        monkeypatch.setattr(
            managed_files.subprocess,
            "Popen",
            lambda *args, **kwargs: pytest.fail("rejected target must not start a sudo session"),
        )

        with pytest.raises(RuntimeError, match="Refusing unexpected managed-settings target"):
            _REAL_SUDO_REPLACE(tmp_path / "unexpected.json", "content")

    def test_rejects_allowlisted_symlink_before_sudo(self, tmp_path, monkeypatch):
        target = tmp_path / "target.json"
        target.write_text("original", encoding="utf-8")
        path = tmp_path / "managed.json"
        path.symlink_to(target)
        monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS.LINUX)
        monkeypatch.setitem(
            managed_files._SUDO_REPLACE_TARGETS,
            managed_files.OS.LINUX,
            frozenset({path}),
        )
        monkeypatch.setattr(
            managed_files.subprocess,
            "run",
            lambda *args, **kwargs: pytest.fail("symlink target must not invoke sudo"),
        )
        monkeypatch.setattr(
            managed_files.subprocess,
            "Popen",
            lambda *args, **kwargs: pytest.fail("symlink target must not start a sudo session"),
        )

        with pytest.raises(RuntimeError, match="Refusing to replace symlinked managed settings"):
            _REAL_SUDO_REPLACE(path, "content")

    def test_shell_allowlist_matches_python_targets(self):
        """The shell worker's quoted allowlist must mirror _SUDO_REPLACE_TARGETS exactly."""
        body = managed_files._SUDO_REPLACE_SCRIPT.split("target_is_allowed() {", 1)[1].split(
            "\n}", 1
        )[0]
        shell_entries = set(re.findall(r'"(linux|macos):([^"]+)"', body))
        python_entries = {
            # These are fixed Unix paths; compare via as_posix so Windows'
            # backslash separators in str(Path) don't break the match.
            (os_enum.value, path.as_posix())
            for os_enum, paths in managed_files._SUDO_REPLACE_TARGETS.items()
            for path in paths
        }
        assert shell_entries == python_entries

    @pytest.mark.parametrize("os_enum", [managed_files.OS.LINUX, managed_files.OS.MACOS])
    def test_real_managed_path_helpers_are_allowlisted(self, os_enum, monkeypatch):
        """The paths the agent helpers compute must be in the sudo-replace allowlist."""
        # Both helpers bind current_os into their own module namespaces.
        monkeypatch.setattr(managed_files, "current_os", lambda: os_enum)
        monkeypatch.setattr(claude_agent, "current_os", lambda: os_enum)
        monkeypatch.setattr(codex_config, "current_os", lambda: os_enum)
        allowed = managed_files._SUDO_REPLACE_TARGETS[os_enum]
        assert claude_agent._managed_settings_path() in allowed
        assert codex_config.codex_managed_config_path() in allowed


class TestWorkspacePolicyWriter:
    @pytest.fixture
    def policy_path(self, tmp_path, monkeypatch):
        path = tmp_path / "unitygateway" / "managed-settings.json"
        path.parent.mkdir()
        monkeypatch.setattr(managed_files, "UNITY_GATEWAY_MANAGED_SETTINGS_PATH", path)
        monkeypatch.setitem(
            managed_files._SUDO_REPLACE_TARGETS,
            managed_files.current_os(),
            frozenset({path}),
        )
        monkeypatch.setattr(managed_files.os, "geteuid", lambda: 1000, raising=False)
        return path

    def test_identical_policy_does_not_invoke_sudo(self, policy_path, monkeypatch):
        policy_path.write_text("installed")
        monkeypatch.setattr(
            managed_files.subprocess,
            "run",
            lambda *args, **kwargs: pytest.fail("unchanged policy must not invoke sudo"),
        )
        managed_files.update_workspace_policy(policy_path, "installed", interactive=True)

    @pytest.mark.parametrize("desired_text", ["new", None])
    @pytest.mark.parametrize("interactive", [False, True])
    def test_nonroot_launch_never_probes_or_invokes_sudo(
        self, policy_path, monkeypatch, desired_text, interactive
    ):
        policy_path.write_text("old")
        monkeypatch.setattr(managed_files, "managed_writes_allowed", lambda: True)
        monkeypatch.setattr(
            managed_files.subprocess,
            "run",
            lambda *args, **kwargs: pytest.fail("non-root launch must never invoke sudo"),
        )
        monkeypatch.setattr(
            managed_files,
            "_sudo_replace",
            lambda *args, **kwargs: pytest.fail("non-root launch must never start a sudo worker"),
        )
        with pytest.raises(managed_files.ManagedFileWriteUnavailable, match="administrator"):
            managed_files.update_workspace_policy(
                policy_path, desired_text, interactive=interactive, allow_sudo=False
            )
        assert policy_path.read_text() == "old"

    def test_nonroot_launch_cannot_install_initial_policy(self, policy_path, monkeypatch):
        monkeypatch.setattr(
            managed_files.subprocess,
            "run",
            lambda *args, **kwargs: pytest.fail("initial launch install must not invoke sudo"),
        )
        with pytest.raises(managed_files.ManagedFileWriteUnavailable, match="administrator"):
            managed_files.update_workspace_policy(policy_path, "new", allow_sudo=False)
        assert not policy_path.exists()

    def test_changed_interactive_policy_uses_existing_prompting_writer(
        self, policy_path, monkeypatch
    ):
        policy_path.write_text("old")
        calls = []

        def write(path, desired_text):
            calls.append((path, desired_text))
            path.write_text(desired_text)

        monkeypatch.setattr(managed_files, "_sudo_replace", write)
        managed_files.update_workspace_policy(policy_path, "new", interactive=True, allow_sudo=True)
        assert calls == [(policy_path, "new")]

    @pytest.mark.skipif(sys.platform == "win32", reason="The workspace-policy writer is Unix-only")
    def test_noninteractive_policy_update_uses_sudo_n(self, policy_path, monkeypatch):
        monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: False)
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            policy_path.write_text(Path(command[-2]).read_text())
            assert kwargs["timeout"] == 30
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(managed_files.subprocess_cross_os, "run", run)
        managed_files.update_workspace_policy(policy_path, "new", interactive=True, allow_sudo=True)
        assert calls[0][:2] == [managed_files._SUDO, "-n"]
        assert policy_path.read_text() == "new"
        assert not Path(calls[0][-2]).exists()

    def test_failed_privileged_update_leaves_original_file(self, policy_path, monkeypatch):
        policy_path.write_text("old")

        def denied(command, **kwargs):
            raise subprocess.CalledProcessError(1, command, stderr="a password is required")

        monkeypatch.setattr(managed_files.subprocess_cross_os, "run", denied)
        with pytest.raises(managed_files.ManagedFileWriteUnavailable, match="password"):
            managed_files.update_workspace_policy(policy_path, "new", allow_sudo=True)
        assert policy_path.read_text() == "old"

    @pytest.mark.skipif(sys.platform == "win32", reason="The workspace-policy writer is Unix-only")
    def test_interactive_policy_removal_may_prompt(self, policy_path, monkeypatch):
        policy_path.write_text("old")
        calls = []

        def remove(command, **kwargs):
            calls.append(command)
            assert Path(command[-2]).read_text() == "old"
            policy_path.unlink()
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(managed_files.subprocess_cross_os, "run", remove)
        managed_files.update_workspace_policy(policy_path, None, interactive=True, allow_sudo=True)
        assert calls[0][0:2] == [managed_files._SUDO, "/bin/sh"]
        assert "remove" in calls[0]

    @pytest.mark.skipif(sys.platform == "win32", reason="The workspace-policy writer is Unix-only")
    @pytest.mark.parametrize(("interactive", "timeout"), [(True, None), (False, 30)])
    def test_policy_removal_waits_for_a_password_prompt(
        self, policy_path, monkeypatch, interactive, timeout
    ):
        """Only an unattended removal times out; a person typing a sudo password is not rushed."""
        policy_path.write_text("old")
        seen = {}

        def remove(command, **kwargs):
            seen.update(kwargs)
            policy_path.unlink()
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(managed_files.subprocess_cross_os, "run", remove)
        managed_files.update_workspace_policy(
            policy_path, None, interactive=interactive, allow_sudo=True
        )
        assert seen["timeout"] == timeout

    @pytest.mark.skipif(sys.platform == "win32", reason="The workspace-policy writer is Unix-only")
    def test_failed_policy_update_reports_why_sudo_failed(self, policy_path, monkeypatch):
        policy_path.write_text("old")

        def denied(command, **kwargs):
            raise subprocess.CalledProcessError(1, command, stderr="sudo: a password is required\n")

        monkeypatch.setattr(managed_files.subprocess_cross_os, "run", denied)
        with pytest.raises(managed_files.ManagedFileWriteUnavailable) as error:
            managed_files.update_workspace_policy(
                policy_path, None, interactive=False, allow_sudo=True
            )
        assert "sudo: a password is required" in str(error.value)

    @pytest.mark.skipif(sys.platform == "win32", reason="The workspace-policy writer is Unix-only")
    def test_timed_out_policy_update_decodes_sudo_output(self, policy_path, monkeypatch):
        policy_path.write_text("old")

        def slow(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 30, stderr=b"sudo: still waiting\n")

        monkeypatch.setattr(managed_files.subprocess_cross_os, "run", slow)
        with pytest.raises(managed_files.ManagedFileWriteUnavailable) as error:
            managed_files.update_workspace_policy(policy_path, None, allow_sudo=True)
        assert "sudo: still waiting" in str(error.value)
        assert "b'" not in str(error.value)

    @pytest.mark.skipif(sys.platform == "win32", reason="The workspace-policy writer is Unix-only")
    def test_policy_removal_compares_the_raw_bytes(self, policy_path, monkeypatch):
        """A BOM or CRLF endings must survive into the copy the script compares before deleting."""
        raw = b'\xef\xbb\xbf{"workspace": "x",\r\n "config": {}}\r\n'
        policy_path.write_bytes(raw)
        seen = {}

        def remove(command, **kwargs):
            seen["source"] = Path(command[-2]).read_bytes()
            policy_path.unlink()
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(managed_files.subprocess_cross_os, "run", remove)
        managed_files.update_workspace_policy(policy_path, None, allow_sudo=True)
        assert seen["source"] == raw

    @pytest.mark.skipif(sys.platform == "win32", reason="The workspace-policy writer is Unix-only")
    def test_root_policy_update_does_not_need_sudo(self, policy_path, monkeypatch):
        monkeypatch.setattr(managed_files.os, "geteuid", lambda: 0)
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            policy_path.write_text(Path(command[-2]).read_text())
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(managed_files.subprocess_cross_os, "run", run)
        managed_files.update_workspace_policy(
            policy_path, "new", interactive=True, allow_sudo=False
        )
        assert calls[0][0:4] == ["/usr/bin/env", "SUDO_UID=0", "/bin/sh", "-c"]

    def test_policy_dry_run_never_invokes_sudo(self, policy_path, monkeypatch):
        config_io.set_dry_run(True)
        monkeypatch.setattr(
            managed_files.subprocess,
            "run",
            lambda *args, **kwargs: pytest.fail("dry run must not invoke sudo"),
        )
        managed_files.update_workspace_policy(policy_path, "new", interactive=True, allow_sudo=True)
        assert not policy_path.exists()

    def test_policy_symlink_is_rejected_before_sudo(self, policy_path, tmp_path, monkeypatch):
        target = tmp_path / "user-file.json"
        target.write_text("user settings")
        policy_path.symlink_to(target)
        monkeypatch.setattr(
            managed_files.subprocess,
            "run",
            lambda *args, **kwargs: pytest.fail("symlink must not invoke sudo"),
        )
        with pytest.raises(RuntimeError, match="symlink"):
            managed_files.update_workspace_policy(policy_path, "new")
        assert target.read_text() == "user settings"


class TestWorkspacePolicyAsRoot:
    _real_replace = staticmethod(managed_files._sudo_replace)

    def test_direct_root_run_points_sudo_uid_at_root(self, tmp_path, monkeypatch):
        """Under `sudo ug configure` SUDO_UID names the caller, but root owns the temp source."""
        path = tmp_path / "unitygateway" / "managed-settings.json"
        monkeypatch.setattr(managed_files, "UNITY_GATEWAY_MANAGED_SETTINGS_PATH", path)
        monkeypatch.setitem(managed_files._SUDO_REPLACE_TARGETS, managed_files.OS.LINUX, {path})
        monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS.LINUX)
        monkeypatch.setattr(managed_files.os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setenv("SUDO_UID", "1000")
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"a": 1}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(managed_files.subprocess_cross_os, "run", run)

        managed_files.update_workspace_policy(path, '{"a": 1}\n', interactive=True)

        command, _kwargs = calls[0]
        assert command[0:3] == ["/usr/bin/env", "SUDO_UID=0", "/bin/sh"]

    @pytest.mark.parametrize("interactive", [False, True], ids=["headless", "interactive"])
    def test_agent_settings_writes_point_sudo_uid_at_root_too(
        self, tmp_path, monkeypatch, interactive
    ):
        """The same direct-root rule for Claude/Codex managed settings, one-shot or not."""
        path = tmp_path / "managed-settings.json"
        monkeypatch.setattr(managed_files, "_validate_sudo_replace_target", lambda _path: None)
        monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS.LINUX)
        monkeypatch.setattr(managed_files.os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: interactive)
        monkeypatch.setenv("SUDO_UID", "1000")
        managed_files.request_explicit_managed_writes()
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(managed_files.subprocess_cross_os, "run", run)
        # Undo conftest's guard to reach the real one-shot writer.
        monkeypatch.setattr(managed_files, "_sudo_replace", TestWorkspacePolicyAsRoot._real_replace)

        managed_files._sudo_replace(path, "{}")

        assert calls[0][0:3] == ["/usr/bin/env", "SUDO_UID=0", "/bin/sh"]


@pytest.mark.skipif(sys.platform == "win32", reason="The managed writer is Unix-only")
class TestManagedWorkerShell:
    @pytest.mark.parametrize("existing", [False, True])
    def test_real_shell_enforces_workspace_policy_permissions(self, tmp_path, existing):
        source = tmp_path / "source.json"
        source.write_text('{"workspace":"https://ws","config":{"spec_version":1}}\n')
        parent = tmp_path / "unitygateway"
        parent.mkdir(mode=0o700)
        target = parent / "managed-settings.json"
        if existing:
            target.write_text("old policy")
            target.chmod(0o600)
        ownership_log = tmp_path / "ownership.log"
        script = managed_files._SUDO_REPLACE_SCRIPT.replace(
            "/etc/unitygateway/managed-settings.json", str(target)
        )
        shim = """
chown() {
    printf '%s\\n' "$*" >> "$UG_TEST_CHOWN_LOG"
}
"""
        script = script.replace('\ncase "$mode" in', shim + '\ncase "$mode" in')
        result = subprocess.run(
            [
                "/bin/sh",
                "-c",
                script,
                "policy-test",
                "once",
                managed_files.current_os().value,
                str(source),
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env={
                **os.environ,
                "SUDO_UID": str(os.getuid()),
                "UG_TEST_CHOWN_LOG": str(ownership_log),
            },
        )
        assert result.returncode == 0, result.stderr
        assert target.read_text() == source.read_text()
        assert target.stat().st_mode & 0o777 == 0o644
        assert parent.stat().st_mode & 0o777 == 0o755
        assert f"0:0 {parent}" in ownership_log.read_text()
        assert len(ownership_log.read_text().splitlines()) >= 2

    @pytest.mark.parametrize(
        ("changed", "neighbor"), [(False, False), (True, False), (False, True)]
    )
    def test_real_shell_removes_only_matching_workspace_policy(self, tmp_path, changed, neighbor):
        source = tmp_path / "expected.json"
        source.write_text("old policy")
        policy_dir = tmp_path / "unitygateway"
        policy_dir.mkdir()
        target = policy_dir / "managed-settings.json"
        target.write_text("new policy" if changed else "old policy")
        if neighbor:
            (policy_dir / "other").write_text("not ours")
        script = managed_files._SUDO_REPLACE_SCRIPT.replace(
            "/etc/unitygateway/managed-settings.json", str(target)
        )
        result = subprocess.run(
            [
                "/bin/sh",
                "-c",
                script,
                "policy-test",
                "remove",
                managed_files.current_os().value,
                str(source),
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "SUDO_UID": str(os.getuid())},
        )
        if changed:
            assert result.returncode != 0
            assert target.read_text() == "new policy"
        else:
            assert result.returncode == 0, result.stderr
            assert not target.exists()
            # The emptied folder goes too; one holding anything else stays.
            assert policy_dir.exists() == neighbor

    @pytest.mark.parametrize("failed_step", ["", "metadata", "content", "rename"])
    def test_real_shell_preserves_file_on_failed_step(self, tmp_path, failed_step):
        """Exercise the actual shell protocol without sudo, using only temporary files."""
        source = tmp_path / "source.json"
        source.write_text("final settings\n", encoding="utf-8")
        target = tmp_path / "managed_config.toml"
        target.write_text("original settings\n", encoding="utf-8")
        target.chmod(0o640)
        # Unit-only relocation: production still accepts only its fixed machine-wide targets.
        script = managed_files._SUDO_REPLACE_SCRIPT.replace(
            "/etc/codex/managed_config.toml", str(target)
        )
        inject_failure = r"""
cp() {
    case "$UG_TEST_FAILED_STEP:$1" in
        metadata:-p|metadata:--preserve=all) return 71 ;;
        content:/*) return 72 ;;
    esac
    command cp "$@"
}
mv() {
    [ "$UG_TEST_FAILED_STEP" != rename ] || return 73
    command mv "$@"
}
"""
        script = script.replace('\ncase "$mode" in', inject_failure + '\ncase "$mode" in')
        request = " ".join(
            [
                "REPLACE",
                "1",
                managed_files._encode_worker_arg(str(source)),
                managed_files._encode_worker_arg(str(target)),
            ]
        )
        result = subprocess.run(
            ["/bin/sh", "-c", script, "worker-test", "session", managed_files.current_os().value],
            input=request + "\nQUIT\n",
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "SUDO_UID": str(os.getuid()), "UG_TEST_FAILED_STEP": failed_step},
        )
        assert result.returncode == 0, result.stderr
        if failed_step:
            fields = result.stdout.rstrip().split(" ", 3)
            assert fields[:2] == ["ERROR", "1"], result.stdout
            assert int(fields[2]) == {"metadata": 71, "content": 72, "rename": 73}[failed_step]
            assert "update failed during" in base64.b64decode(fields[3]).decode()
            assert target.read_text(encoding="utf-8") == "original settings\n"
        else:
            assert result.stdout == "OK 1\n"
            assert target.read_text(encoding="utf-8") == "final settings\n"
        assert target.stat().st_mode & 0o777 == 0o640
        assert not list(tmp_path.glob(".managed_config.toml.ucode.*"))

    @pytest.mark.parametrize("missing_parents", [0, 1, 2])
    def test_real_shell_creates_missing_file(self, tmp_path, missing_parents):
        """The new-file branch installs content, mode, and (root) ownership from scratch."""
        source = tmp_path / "source.json"
        source.write_text("managed settings\n", encoding="utf-8")
        created = [tmp_path / "codex", tmp_path / "codex" / "nested"][:missing_parents]
        parent = created[-1] if created else tmp_path
        target = parent / "managed_config.toml"
        chown_log = tmp_path / "chown.log"
        # Unit-only relocation: production still accepts only its fixed machine-wide targets.
        script = managed_files._SUDO_REPLACE_SCRIPT.replace(
            "/etc/codex/managed_config.toml", str(target)
        )
        # chown 0:0 needs root; log the calls instead, leaving chmod/mkdir/cp/mv real.
        shim_chown = r"""
chown() {
    printf '%s\n' "$*" >> "$UG_TEST_CHOWN_LOG"
    return 0
}
"""
        script = script.replace('\ncase "$mode" in', shim_chown + '\ncase "$mode" in')
        # A restrictive umask must not leave any created folder unreadable to the agents.
        script = "umask 027\n" + script
        request = " ".join(
            [
                "REPLACE",
                "1",
                managed_files._encode_worker_arg(str(source)),
                managed_files._encode_worker_arg(str(target)),
            ]
        )
        result = subprocess.run(
            ["/bin/sh", "-c", script, "worker-test", "session", managed_files.current_os().value],
            input=request + "\nQUIT\n",
            capture_output=True,
            text=True,
            timeout=10,
            env={
                **os.environ,
                "SUDO_UID": str(os.getuid()),
                "UG_TEST_CHOWN_LOG": str(chown_log),
            },
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "OK 1\n"
        assert target.read_text(encoding="utf-8") == "managed settings\n"
        assert target.stat().st_mode & 0o777 == 0o644
        for folder in created:
            assert folder.stat().st_mode & 0o777 == 0o755
        expected_chown_calls = [f"0:0 {parent}"] if created else []
        chown_calls = chown_log.read_text(encoding="utf-8").splitlines()
        assert chown_calls[: len(expected_chown_calls)] == expected_chown_calls
        assert len(chown_calls) == len(expected_chown_calls) + 1
        staging_owner, _, staging_arg = chown_calls[-1].partition(" ")
        assert staging_owner == "0:0"
        staging_path = Path(staging_arg)
        assert staging_path.parent == parent
        assert staging_path.name.startswith(".managed_config.toml.ucode.")
        assert not list(parent.glob(".managed_config.toml.ucode.*"))


class TestManagedSettingsSkipped:
    """``UG_SKIP_OS_SETTINGS`` opts out of every OS-managed-file operation."""

    def test_absent_env_does_not_skip(self, monkeypatch):
        monkeypatch.delenv(managed_files.SKIP_OS_SETTINGS_ENV, raising=False)
        assert managed_files.managed_settings_skipped() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " on "])
    def test_truthy_values_skip(self, monkeypatch, value):
        monkeypatch.setenv(managed_files.SKIP_OS_SETTINGS_ENV, value)
        assert managed_files.managed_settings_skipped() is True
        # Skipping also reports the platform as unsupported so managed MCP falls back to user scope
        # (the real gate; the autouse fixture otherwise pins support on).
        assert _REAL_MANAGED_FILES_SUPPORTED() is False

    @pytest.mark.parametrize("value", ["0", "false", "off", "no", ""])
    def test_falsey_values_do_not_skip(self, monkeypatch, value):
        monkeypatch.setenv(managed_files.SKIP_OS_SETTINGS_ENV, value)
        assert managed_files.managed_settings_skipped() is False

    def test_skip_prevents_privileged_write(self, monkeypatch):
        monkeypatch.setenv(managed_files.SKIP_OS_SETTINGS_ENV, "1")
        # Use the real gate so skipping short-circuits before sudo (fixture otherwise pins it on).
        monkeypatch.setattr(managed_files, "managed_files_supported", _REAL_MANAGED_FILES_SUPPORTED)
        monkeypatch.setattr(
            managed_files, "_sudo_replace", lambda *args: pytest.fail("must not write")
        )
        result = managed_files.reconcile_managed_file(
            Path("/tmp/ucode-test/managed.json"),
            '{"ucode": true}\n',
            tool="claude",
            display="Claude Code",
            owned_paths=[["ucode"]],
            parser=json.loads,
        )
        assert result == "unsupported"


class TestWindowsManagedWrites:
    """Windows has no sudo: an elevated process writes Program Files directly, others can't."""

    @pytest.fixture
    def windows(self, tmp_path, backup_dir, monkeypatch):
        monkeypatch.setenv("ProgramW6432", str(tmp_path / "Program Files"))
        monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files (x86)"))
        monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
        monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS.WINDOWS)
        monkeypatch.setattr(managed_files, "_windows_replace", _REAL_WINDOWS_REPLACE)
        monkeypatch.setattr(
            managed_files.subprocess_cross_os,
            "run",
            lambda *a, **k: pytest.fail("Windows writes must not shell out to sudo"),
        )
        return managed_files.windows_claude_managed_settings_path()

    def _reconcile(self, path, text):
        return managed_files.reconcile_managed_file(
            path,
            text,
            tool="claude",
            display="Claude Code",
            owned_paths=[["apiKeyHelper"]],
            parser=json.loads,
        )

    def _reconcile_codex(self, path, text):
        return managed_files.reconcile_managed_file(
            path,
            text,
            tool="codex",
            display="Codex",
            owned_paths=[["model_provider"]],
            parser=tomllib.loads,
        )

    def test_path_is_under_64_bit_program_files(self, windows, tmp_path):
        assert windows == tmp_path / "Program Files" / "ClaudeCode" / "managed-settings.json"

    def test_reads_file_written_with_a_bom(self, windows):
        # Windows PowerShell 5.1's `Set-Content -Encoding UTF8` prefixes a BOM.
        windows.parent.mkdir(parents=True)
        windows.write_bytes(b'\xef\xbb\xbf{"apiKeyHelper": "it"}\n')

        assert json.loads(managed_files.read_managed_file(windows)) == {"apiKeyHelper": "it"}

    @pytest.mark.parametrize("admin", [True, False])
    def test_writes_follow_elevation_not_the_terminal(self, windows, monkeypatch, admin):
        monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: admin)
        monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: not admin)

        assert managed_files.managed_writes_allowed() is admin
        assert managed_files.managed_writes_allowed(repair_existing=True) is admin
        assert managed_files._sudo_may_prompt() is False

    def test_elevated_headless_run_creates_file(self, windows, monkeypatch):
        # Device provisioning runs `ug configure` elevated and without a terminal.
        monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: True)
        monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: False)

        assert self._reconcile(windows, '{"apiKeyHelper": "ug"}\n') == "created"

        assert windows.read_text(encoding="utf-8") == '{"apiKeyHelper": "ug"}\n'
        assert [entry.name for entry in windows.parent.iterdir()] == [windows.name]

    def test_non_admin_update_quits_with_admin_hint(self, windows, monkeypatch):
        windows.parent.mkdir(parents=True)
        windows.write_text('{"apiKeyHelper": "old"}\n', encoding="utf-8")
        monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: False)

        with pytest.raises(RuntimeError, match="requires administrator rights.*ug configure"):
            self._reconcile(windows, '{"apiKeyHelper": "ug"}\n')

        assert windows.read_text(encoding="utf-8") == '{"apiKeyHelper": "old"}\n'

    def test_elevated_revert_removes_created_file(self, windows, monkeypatch):
        monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: True)
        self._reconcile(windows, '{"apiKeyHelper": "ug"}\n')

        result = managed_files.revert_managed_file(
            "claude", display="Claude Code", parser=json.loads, dumper=json.dumps
        )

        assert result == "removed"
        assert not windows.exists()

    def test_non_admin_revert_quits_with_admin_hint(self, windows, monkeypatch):
        monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: True)
        self._reconcile(windows, '{"apiKeyHelper": "ug"}\n')
        monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: False)

        with pytest.raises(RuntimeError, match="requires administrator rights.*ug revert"):
            managed_files.revert_managed_file(
                "claude", display="Claude Code", parser=json.loads, dumper=json.dumps
            )

        assert windows.exists()

    def test_refuses_unexpected_target(self, windows, tmp_path, monkeypatch):
        monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: True)

        with pytest.raises(RuntimeError, match="unexpected managed-settings target"):
            managed_files._windows_replace(tmp_path / "elsewhere.json", "{}")

    def test_agent_paths(self, windows, monkeypatch):
        monkeypatch.setattr(claude_agent, "current_os", lambda: managed_files.OS.WINDOWS)
        monkeypatch.setattr(codex_config, "current_os", lambda: managed_files.OS.WINDOWS)

        assert claude_agent._managed_settings_path() == windows
        assert codex_config.codex_managed_config_path() == (
            managed_files.windows_codex_managed_settings_path()
        )

    def _record_icacls(self, monkeypatch, returncode=0):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, returncode, "Access is denied.", "")

        monkeypatch.setattr(managed_files.subprocess_cross_os, "run", run)
        return calls

    def test_elevated_run_writes_codex_requirements(self, windows, tmp_path, monkeypatch):
        monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: True)
        monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
        calls = self._record_icacls(monkeypatch)
        path = managed_files.windows_codex_managed_settings_path()

        assert path == tmp_path / "ProgramData" / "OpenAI" / "Codex" / "requirements.toml"
        assert self._reconcile_codex(path, 'model_provider = "Databricks"\n') == "created"
        assert path.read_text(encoding="utf-8") == 'model_provider = "Databricks"\n'
        # A standard user may have created these folders; both end up owned by Administrators.
        icacls = str(tmp_path / "Windows" / "System32" / "icacls.exe")
        codex, openai = str(path.parent), str(path.parent.parent)
        assert calls == [
            [icacls, openai, "/setowner", "*S-1-5-32-544", "/L"],
            [icacls, openai, "/reset", "/L"],
            [icacls, codex, "/setowner", "*S-1-5-32-544", "/L"],
            [
                icacls,
                codex,
                "/inheritance:r",
                "/grant:r",
                "*S-1-5-18:(OI)(CI)F",
                "*S-1-5-32-544:(OI)(CI)F",
                "*S-1-5-32-545:(OI)(CI)RX",
                "/L",
            ],
        ]

    def test_unsecured_codex_folder_blocks_the_write(self, windows, monkeypatch):
        monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: True)
        self._record_icacls(monkeypatch, returncode=5)
        path = managed_files.windows_codex_managed_settings_path()

        with pytest.raises(RuntimeError, match="could not secure .*Access is denied"):
            self._reconcile_codex(path, 'model_provider = "Databricks"\n')

        assert not path.exists()

    @pytest.mark.skipif(sys.platform != "win32", reason="needs the real icacls")
    def test_standard_users_can_only_read_secured_codex_folder(self, windows, monkeypatch):
        # The Windows CI lane is elevated, so this runs the real icacls on a temporary ProgramData.
        monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: True)
        monkeypatch.setattr(managed_files.subprocess_cross_os, "run", _REAL_SUBPROCESS_RUN)
        path = managed_files.windows_codex_managed_settings_path()

        assert self._reconcile_codex(path, 'model_provider = "Databricks"\n') == "created"

        for folder in (path.parent.parent, path.parent):
            owner = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-Acl -LiteralPath '{folder}').Owner",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            assert owner == "BUILTIN\\Administrators"
        acl = subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True, check=True
        ).stdout
        assert "BUILTIN\\Users:(I)(RX)" in acl
        assert not re.search(r"\\Users:.*\((F|M|W)\)", acl)

    def test_refuses_linked_codex_folder(self, windows, tmp_path, monkeypatch):
        # Standard users can create folders under ProgramData, including links elsewhere.
        monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: True)
        path = managed_files.windows_codex_managed_settings_path()
        elsewhere = tmp_path / "user-folder"
        elsewhere.mkdir()
        path.parent.parent.parent.mkdir()
        try:
            path.parent.parent.symlink_to(elsewhere, target_is_directory=True)
        except OSError:
            pytest.skip("creating a directory link needs extra privileges here")

        with pytest.raises(RuntimeError, match="through link"):
            self._reconcile_codex(path, 'model_provider = "Databricks"\n')

        assert not any(elsewhere.iterdir())


class TestManagedWriteSkippedWarning:
    @pytest.fixture
    def warnings(self, monkeypatch):
        captured: list[str] = []
        monkeypatch.setattr(managed_files, "print_warning", captured.append)
        return captured

    def test_batched_setup_names_the_file_it_could_not_write(self, warnings, tmp_path):
        path = tmp_path / "requirements.toml"

        with managed_files.managed_write_batch(["Codex"]):
            managed_files.warn_managed_write_skipped(path, {}, {"model_provider": "Databricks"})

        assert warnings == [
            f"{path} wasn't updated: this run has no terminal to approve sudo. Run `ug configure` to update it."
        ]

    def test_dry_run_stays_quiet(self, warnings, monkeypatch, tmp_path):
        monkeypatch.setattr(managed_files, "is_dry_run", lambda: True)

        with managed_files.managed_write_batch(["Codex"]):
            managed_files.warn_managed_write_skipped(tmp_path, {}, {"model_provider": "Databricks"})

        assert warnings == []

    def test_launches_stay_quiet(self, warnings, tmp_path):
        # Launches reconfigure on every run, outside configure's batch.
        managed_files.warn_managed_write_skipped(tmp_path, {}, {"model_provider": "Databricks"})

        assert warnings == []

    def test_up_to_date_file_needs_no_warning(self, warnings, tmp_path):
        # Only ug's own User-Agent versions differ, which doesn't change the file's meaning.
        current = {"http_headers": {"User-Agent": "ucode/1.0 codex/0.154.0"}}
        desired = {"http_headers": {"User-Agent": "ucode/1.1 codex/0.160.0"}}

        with managed_files.managed_write_batch(["Codex"]):
            managed_files.warn_managed_write_skipped(tmp_path, current, desired)

        assert warnings == []


class TestManagedFileLifecycle:
    def test_noninteractive_creation_is_blocked(self, tmp_path, backup_dir, monkeypatch):
        path = tmp_path / "managed.json"
        monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(
            managed_files,
            "_sudo_replace",
            lambda *args: pytest.fail("headless creation must not write"),
        )

        with pytest.raises(RuntimeError, match="Refusing to update .* non-interactively"):
            managed_files.reconcile_managed_file(
                path,
                '{"apiKeyHelper": "ug"}',
                tool="claude",
                display="Claude Code",
                owned_paths=[["apiKeyHelper"]],
                parser=json.loads,
            )

        assert not path.exists()
        assert not backup_dir.exists()

    def test_noninteractive_revert_is_blocked(self, tmp_path, backup_dir, monkeypatch):
        path = tmp_path / "managed.json"
        path.write_text('{"enterprise": true}\n', encoding="utf-8")
        monkeypatch.setattr(
            managed_files,
            "_sudo_replace",
            lambda target, text: target.write_text(text, encoding="utf-8"),
        )
        managed_files.reconcile_managed_file(
            path,
            '{"enterprise": true, "ucode": true}\n',
            tool="claude",
            display="Claude Code",
            owned_paths=[["ucode"]],
            parser=json.loads,
        )
        monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: False)

        with pytest.raises(RuntimeError, match="Cannot restore .* non-interactively"):
            managed_files.revert_managed_file(
                "claude", display="Claude Code", parser=json.loads, dumper=json.dumps
            )

        assert path.read_text(encoding="utf-8") == '{"enterprise": true, "ucode": true}\n'

    def test_noninteractive_repair_preserves_policy_and_backup(
        self, tmp_path, backup_dir, monkeypatch
    ):
        path = tmp_path / "managed.json"
        original = '{"apiKeyHelper": "isaac", "permissions": {"deny": ["Read(secret.txt)"]}}\n'
        desired = '{"apiKeyHelper": "ug", "permissions": {"deny": ["Read(secret.txt)"]}}\n'
        path.write_text(original)
        monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: False)

        def replace(target, text):
            target.write_text(text)

        monkeypatch.setattr(managed_files, "_sudo_replace", replace)
        monkeypatch.setattr(
            managed_files,
            "_print_managed_write_permission",
            lambda *args: pytest.fail("headless repair must not ask for a password"),
        )

        assert (
            managed_files.reconcile_managed_file(
                path,
                desired,
                tool="claude",
                display="Claude Code",
                owned_paths=[["apiKeyHelper"]],
                parser=json.loads,
            )
            == "written"
        )
        assert path.read_text() == desired
        assert (backup_dir / "manifest.json").exists()
        snapshots = managed_files.managed_file_snapshots("claude", json.loads)
        assert snapshots.original_before_ug == json.loads(original)
        assert snapshots.last_applied_by_ug == json.loads(desired)

    def test_noninteractive_permission_failure_preserves_file_and_does_not_retry(
        self, tmp_path, backup_dir, monkeypatch
    ):
        path = tmp_path / "managed.json"
        original = '{"apiKeyHelper": "isaac"}\n'
        path.write_text(original)
        monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: False)
        calls: list = []

        def deny_write(target, text):
            calls.append(target)
            raise subprocess.CalledProcessError(
                1, ["sudo", "-n"], stderr="sudo: a password is required"
            )

        monkeypatch.setattr(managed_files, "_sudo_replace", deny_write)
        monkeypatch.setattr(
            managed_files,
            "_print_managed_write_permission",
            lambda *args: pytest.fail("headless repair must not ask for a password"),
        )

        with pytest.raises(managed_files.ManagedFileWriteUnavailable, match="password is required"):
            managed_files.reconcile_managed_file(
                path,
                '{"apiKeyHelper": "ug"}\n',
                tool="claude",
                display="Claude Code",
                owned_paths=[["apiKeyHelper"]],
                parser=json.loads,
            )

        assert path.read_text() == original
        assert calls == [path]
        assert managed_files.managed_file_snapshots(
            "claude", json.loads
        ).original_before_ug == json.loads(original)

    def test_dry_run_does_not_write_or_backup(self, tmp_path, backup_dir, monkeypatch):
        path = tmp_path / "managed.json"
        config_io.set_dry_run(True)
        monkeypatch.setattr(
            managed_files, "_sudo_replace", lambda *args: pytest.fail("must not write")
        )

        result = managed_files.reconcile_managed_file(
            path,
            '{"ucode": true}\n',
            tool="claude",
            display="Claude Code",
            owned_paths=[["ucode"]],
            parser=json.loads,
        )

        assert result == "written"
        assert not backup_dir.exists()

    def test_unsupported_platform_skips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(managed_files, "managed_files_supported", lambda: False)
        monkeypatch.setattr(
            managed_files, "_sudo_replace", lambda *args: pytest.fail("must not write")
        )

        result = managed_files.reconcile_managed_file(
            tmp_path / "managed.json",
            '{"ucode": true}\n',
            tool="claude",
            display="Claude Code",
            owned_paths=[["ucode"]],
            parser=json.loads,
        )

        assert result == "unsupported"

    def test_permission_failure_is_actionable(self, tmp_path, backup_dir, monkeypatch):
        path = tmp_path / "managed.json"

        def deny_write(path, text):
            raise PermissionError("no root")

        monkeypatch.setattr(managed_files, "_sudo_replace", deny_write)

        with pytest.raises(managed_files.ManagedFileWriteUnavailable, match="could not update"):
            managed_files.reconcile_managed_file(
                path,
                '{"ucode": true}\n',
                tool="claude",
                display="Claude Code",
                owned_paths=[["ucode"]],
                parser=json.loads,
            )
        assert (backup_dir / "manifest.json").exists()

    def test_reconcile_refuses_symlink_target(self, tmp_path, backup_dir, monkeypatch):
        target = tmp_path / "real.json"
        target.write_text("{}", encoding="utf-8")
        path = tmp_path / "managed.json"
        path.symlink_to(target)
        monkeypatch.setattr(
            managed_files, "_sudo_replace", lambda *args: pytest.fail("must not write")
        )

        with pytest.raises(RuntimeError, match="Refusing to update"):
            managed_files.reconcile_managed_file(
                path,
                '{"ucode": true}\n',
                tool="claude",
                display="Claude Code",
                owned_paths=[["ucode"]],
                parser=json.loads,
            )

    def test_reconcile_backs_up_before_write(self, tmp_path, backup_dir, monkeypatch):
        path = tmp_path / "managed.json"
        path.write_text('{"enterprise": true}\n', encoding="utf-8")

        def replace(target, text):
            assert (backup_dir / "claude-managed-settings.backup.json").exists()
            target.write_text(text, encoding="utf-8")

        monkeypatch.setattr(managed_files, "_sudo_replace", replace)
        result = managed_files.reconcile_managed_file(
            path,
            '{"enterprise": true, "ucode": true}\n',
            tool="claude",
            display="Claude Code",
            owned_paths=[["ucode"]],
            parser=json.loads,
        )

        assert result == "written"
        assert (backup_dir / "claude-managed-settings.backup.json").read_text() == (
            '{"enterprise": true}\n'
        )
        manifest = json.loads((backup_dir / "manifest.json").read_text())
        assert manifest["files"]["claude"]["original_existed"] is True

    def test_snapshots_report_paths_ucode_wrote_to_the_file(
        self, tmp_path, backup_dir, monkeypatch
    ):
        path = tmp_path / "managed.json"
        path.write_text('{"enterprise": true}\n', encoding="utf-8")
        monkeypatch.setattr(
            managed_files,
            "_sudo_replace",
            lambda target, text: target.write_text(text, encoding="utf-8"),
        )

        managed_files.reconcile_managed_file(
            path,
            '{"enterprise": true, "ucode": true}\n',
            tool="claude",
            display="Claude Code",
            owned_paths=[["ucode"]],
            parser=json.loads,
        )

        snapshots = managed_files.managed_file_snapshots("claude", json.loads)
        assert snapshots.original_before_ug == {"enterprise": True}
        assert snapshots.last_applied_by_ug == {"enterprise": True, "ucode": True}

    def test_batch_messages_name_all_agents_once(self, tmp_path, backup_dir, monkeypatch):
        notes: list[str] = []
        successes: list[str] = []

        monkeypatch.setattr(managed_files, "print_note", notes.append)
        monkeypatch.setattr(managed_files, "print_success", successes.append)
        monkeypatch.setattr(
            managed_files,
            "_sudo_replace",
            lambda target, text: target.write_text(text, encoding="utf-8"),
        )

        with managed_files.managed_write_batch(["Codex", "Claude Code"]):
            for tool in ("codex", "claude"):
                managed_files.reconcile_managed_file(
                    tmp_path / f"{tool}.json",
                    '{"ucode": true}\n',
                    tool=tool,
                    display=tool.title(),
                    owned_paths=[["ucode"]],
                    parser=json.loads,
                )

        assert notes == ["Enter password to configure settings for Codex and Claude Code."]
        assert successes == ["Settings configured for Codex and Claude Code"]

    def test_unchanged_file_never_creates_backup(self, tmp_path, backup_dir, monkeypatch):
        path = tmp_path / "managed.json"
        path.write_text("same", encoding="utf-8")
        monkeypatch.setattr(
            managed_files, "_sudo_replace", lambda *args: pytest.fail("must not write")
        )

        result = managed_files.reconcile_managed_file(
            path,
            "same",
            tool="claude",
            display="Claude Code",
            owned_paths=[["env"]],
            parser=json.loads,
        )

        assert result == "unchanged"
        assert not backup_dir.exists()

    def test_semantic_noop_with_parser_retains_bytes_without_write(
        self, tmp_path, backup_dir, monkeypatch
    ):
        path = tmp_path / "managed.json"
        path.write_text('{"b": 2, "a": 1}\n', encoding="utf-8")  # admin key order
        monkeypatch.setattr(
            managed_files, "_sudo_replace", lambda *args: pytest.fail("must not write")
        )

        result = managed_files.reconcile_managed_file(
            path,
            '{"a": 1, "b": 2}\n',  # same content, different serialization
            tool="claude",
            display="Claude Code",
            owned_paths=[["a"]],
            parser=json.loads,
        )

        assert result == "unchanged"
        assert path.read_text() == '{"b": 2, "a": 1}\n'  # exact bytes retained
        assert not backup_dir.exists()

    def test_semantically_different_with_parser_still_writes(
        self, tmp_path, backup_dir, monkeypatch
    ):
        # The semantic check uses this function's own fresh read, so a genuinely different live
        # value (e.g. a concurrent device-management replacement) is never mistaken for a no-op.
        path = tmp_path / "managed.json"
        path.write_text('{"a": 1}\n', encoding="utf-8")
        monkeypatch.setattr(
            managed_files, "_sudo_replace", lambda t, text: t.write_text(text, encoding="utf-8")
        )

        result = managed_files.reconcile_managed_file(
            path,
            '{"a": 2}\n',
            tool="claude",
            display="Claude Code",
            owned_paths=[["a"]],
            parser=json.loads,
        )

        assert result == "written"
        assert json.loads(path.read_text()) == {"a": 2}

    def test_user_agent_version_only_difference_is_unchanged_without_write(
        self, tmp_path, backup_dir, monkeypatch
    ):
        path = tmp_path / "managed.json"
        path.write_text('{"User-Agent": "ucode/1.0 claude/2.1.288", "a": 1}\n', encoding="utf-8")
        monkeypatch.setattr(
            managed_files, "_sudo_replace", lambda *args: pytest.fail("must not write")
        )

        result = managed_files.reconcile_managed_file(
            path,
            '{"User-Agent": "ucode/1.1 claude/unknown", "a": 1}\n',
            tool="claude",
            display="Claude Code",
            owned_paths=[["User-Agent"], ["a"]],
            parser=json.loads,
        )

        assert result == "unchanged"
        assert path.read_text() == '{"User-Agent": "ucode/1.0 claude/2.1.288", "a": 1}\n'
        assert not backup_dir.exists()

    def test_user_agent_change_with_another_change_still_writes(
        self, tmp_path, backup_dir, monkeypatch
    ):
        path = tmp_path / "managed.json"
        path.write_text('{"User-Agent": "ucode/1.0 claude/2.1.288", "a": 1}\n', encoding="utf-8")
        monkeypatch.setattr(
            managed_files, "_sudo_replace", lambda t, text: t.write_text(text, encoding="utf-8")
        )

        result = managed_files.reconcile_managed_file(
            path,
            '{"User-Agent": "ucode/1.1 claude/2.1.289", "a": 2}\n',
            tool="claude",
            display="Claude Code",
            owned_paths=[["User-Agent"], ["a"]],
            parser=json.loads,
        )

        assert result == "written"
        assert json.loads(path.read_text()) == {"User-Agent": "ucode/1.1 claude/2.1.289", "a": 2}

    def test_version_only_user_agent_difference_is_not_a_conflict(self):
        existing = {"h": {"User-Agent": "ucode/1.0 codex/0.1"}, "a": 1}
        desired = {"h": {"User-Agent": "ucode/1.1 codex/unknown"}, "a": 1}

        assert managed_files.managed_file_conflicts(existing, desired, [["h"], ["a"]]) == []
        assert managed_files.managed_file_conflicts(existing, {**desired, "a": 2}, [["a"]]) == ["a"]

    @pytest.mark.parametrize(
        ("value", "masked"),
        [
            ({"User-Agent": "ucode/0.1.0 claude/2.1.288"}, {"User-Agent": "ucode/* claude/*"}),
            ({"user-agent": "ucode/0.1.0+abc codex/unknown"}, {"user-agent": "ucode/* codex/*"}),
            (
                {"h": "a: 1\nUser-Agent: ucode/1 claude/2\nb: 2"},
                {"h": "a: 1\nUser-Agent: ucode/* claude/*\nb: 2"},
            ),
            ({"User-Agent": "admin-agent/9"}, {"User-Agent": "admin-agent/9"}),
            # Outside a User-Agent header the same text is a real value and compares exactly.
            ({"mcp": {"args": ["ucode/1 claude/2"]}}, {"mcp": {"args": ["ucode/1 claude/2"]}}),
            ({"h": "X-Client: ucode/1 claude/2"}, {"h": "X-Client: ucode/1 claude/2"}),
            ({"x": [True, 1]}, {"x": [True, 1]}),
        ],
    )
    def test_mask_user_agent_versions_masks_only_ug_version_tokens(self, value, masked):
        assert managed_files.mask_user_agent_versions(value) == masked

    @pytest.mark.parametrize(
        ("user_agent", "token"),
        [
            ("ucode/1.0 claude/2.1", "claude/2.1"),
            ("ucode/1.0 claude/2.1 (macos; arm64)", "claude/2.1"),
            ("ucode/1.0 claude/2.1 (windows; x86_64)", "claude/2.1"),
            ("ucode/1.0 claude/2.1 (macos 15.1; arm64)", "claude/2.1"),
            ("ucode/1.0 claude/2.1 (linux ubuntu-24.04; x86_64)", "claude/2.1"),
            ("ucode/1.0 claude/2.1 (anything, new: fields; v2)", "claude/2.1"),
            ("ucode/1.0 claude/2.1 (a (nested))", None),
            ("ucode/1.0 claude/2.1 (macos; arm64) extra", None),
            ("ucode/1.0 codex/2.1 (macos; arm64)", None),
        ],
    )
    def test_ug_agent_token_accepts_optional_platform_suffix(self, user_agent, token):
        assert managed_files.ug_agent_token(user_agent, "claude") == token

    @pytest.mark.parametrize(
        "user_agent",
        [
            "ucode/0.9 claude/2.1",
            "ucode/1.0 claude/2.2 (macos; arm64)",
            "ucode/1.0 claude/2.2 (linux; x86_64)",
            "ucode/1.0 claude/2.2 (macos 15.1; arm64)",
        ],
    )
    def test_platform_suffix_is_masked_with_the_versions(self, user_agent):
        masked = managed_files.mask_user_agent_versions({"User-Agent": user_agent})
        assert masked == {"User-Agent": "ucode/* claude/*"}
        header = managed_files.mask_user_agent_versions({"h": f"a: 1\nUser-Agent: {user_agent}"})
        assert header == {"h": "a: 1\nUser-Agent: ucode/* claude/*"}

    def test_ug_upgrade_adding_platform_suffix_is_not_a_conflict(self):
        # A file written by an older ug (no suffix) must not force a sudo rewrite on upgrade.
        existing = {"h": {"User-Agent": "ucode/1.0 codex/0.1"}, "a": 1}
        desired = {"h": {"User-Agent": "ucode/1.1 codex/0.1 (macos; arm64)"}, "a": 1}

        assert managed_files.managed_file_conflicts(existing, desired, [["h"], ["a"]]) == []
        assert managed_files.managed_file_conflicts(desired, existing, [["h"], ["a"]]) == []
        assert managed_files.managed_file_conflicts(existing, {**desired, "a": 2}, [["a"]]) == ["a"]

    def test_verified_check_uses_fingerprint(self, tmp_path):
        path = tmp_path / "managed.json"
        path.write_text("current", encoding="utf-8")
        state: dict = {}
        managed_files.mark_managed_file_verified(state, "claude", path)

        assert managed_files.managed_file_is_verified(state, "claude", path) is True
        path.write_text("changed-content", encoding="utf-8")
        assert managed_files.managed_file_is_verified(state, "claude", path) is False

    def test_revert_restores_exact_original(self, tmp_path, backup_dir, monkeypatch):
        path = tmp_path / "managed.json"
        path.write_text('{"enterprise": true}\n', encoding="utf-8")
        monkeypatch.setattr(
            managed_files,
            "_sudo_replace",
            lambda target, text: target.write_text(text, encoding="utf-8"),
        )
        managed_files.reconcile_managed_file(
            path,
            '{"enterprise": true, "ucode": true}\n',
            tool="claude",
            display="Claude Code",
            owned_paths=[["ucode"]],
            parser=json.loads,
        )

        result = managed_files.revert_managed_file(
            "claude",
            display="Claude Code",
            parser=json.loads,
            dumper=lambda doc: json.dumps(doc) + "\n",
        )

        assert result == "restored"
        assert path.read_text() == '{"enterprise": true}\n'
        assert json.loads((backup_dir / "manifest.json").read_text())["files"] == {}

    def test_revert_removes_file_created_by_ucode(self, tmp_path, backup_dir, monkeypatch):
        path = tmp_path / "managed.json"
        monkeypatch.setattr(
            managed_files,
            "_sudo_replace",
            lambda target, text: target.write_text(text, encoding="utf-8"),
        )
        monkeypatch.setattr(managed_files, "_sudo_remove", lambda target: target.unlink())
        managed_files.reconcile_managed_file(
            path,
            '{"ucode": true}\n',
            tool="claude",
            display="Claude Code",
            owned_paths=[["ucode"]],
            parser=json.loads,
        )

        result = managed_files.revert_managed_file(
            "claude",
            display="Claude Code",
            parser=json.loads,
            dumper=lambda doc: json.dumps(doc) + "\n",
        )

        assert result == "removed"
        assert not path.exists()

    def test_revert_preserves_external_changes(self, tmp_path, backup_dir, monkeypatch):
        path = tmp_path / "managed.json"
        path.write_text('{"enterprise": "original"}\n', encoding="utf-8")
        monkeypatch.setattr(
            managed_files,
            "_sudo_replace",
            lambda target, text: target.write_text(text, encoding="utf-8"),
        )
        managed_files.reconcile_managed_file(
            path,
            '{"enterprise": "original", "ucode": "gateway"}\n',
            tool="claude",
            display="Claude Code",
            owned_paths=[["ucode"]],
            parser=json.loads,
        )
        path.write_text(
            '{"enterprise": "new-policy", "ucode": "gateway", "new": true}\n',
            encoding="utf-8",
        )

        result = managed_files.revert_managed_file(
            "claude",
            display="Claude Code",
            parser=json.loads,
            dumper=lambda doc: json.dumps(doc, sort_keys=True) + "\n",
        )

        assert result == "ucode entries removed; external changes preserved"
        assert json.loads(path.read_text()) == {"enterprise": "new-policy", "new": True}

    def test_reconcile_retries_exact_mdm_restore_once(self, tmp_path, backup_dir, monkeypatch):
        path = tmp_path / "managed.json"
        path.write_text('{"enterprise": true}\n', encoding="utf-8")
        calls = 0

        def restore_original(target, text):
            nonlocal calls
            calls += 1
            target.write_text('{"enterprise": true}\n', encoding="utf-8")

        monkeypatch.setattr(managed_files, "_sudo_replace", restore_original)

        with pytest.raises(RuntimeError, match="immediately restored by device management"):
            managed_files.reconcile_managed_file(
                path,
                '{"enterprise": true, "ucode": true}\n',
                tool="claude",
                display="Claude Code",
                owned_paths=[["ucode"]],
                parser=json.loads,
            )

        assert calls == 2

    def test_reconcile_preserves_concurrent_policy_change(self, tmp_path, backup_dir, monkeypatch):
        path = tmp_path / "managed.json"
        path.write_text('{"enterprise": "old"}\n', encoding="utf-8")

        def external_update(target, text):
            target.write_text('{"enterprise": "new"}\n', encoding="utf-8")

        monkeypatch.setattr(managed_files, "_sudo_replace", external_update)

        with pytest.raises(RuntimeError, match="changed concurrently"):
            managed_files.reconcile_managed_file(
                path,
                '{"enterprise": "old", "ucode": true}\n',
                tool="claude",
                display="Claude Code",
                owned_paths=[["ucode"]],
                parser=json.loads,
            )

        assert json.loads(path.read_text()) == {"enterprise": "new"}


class TestSemanticEqual:
    @pytest.mark.parametrize(
        "left,right,expected",
        [
            ({"a": 1, "b": 2}, {"b": 2, "a": 1}, True),  # mapping order ignored
            ({"a": 1}, {"a": 1, "b": 2}, False),  # extra key
            ({"a": 1}, {"a": 2}, False),
            ([1, 2, 3], [1, 2, 3], True),
            ([1, 2, 3], [3, 2, 1], False),  # array order significant
            ([1, 2], [1, 2, 3], False),
            (True, 1, False),  # bool never equals int
            (1, True, False),
            (False, 0, False),
            (True, True, True),
            (1, 1.0, False),  # int never equals float
            (1, 1, True),
            ("1", 1, False),
            ("x", "x", True),
            (None, None, True),
            (None, 0, False),
            ({"x": [{"k": True}]}, {"x": [{"k": True}]}, True),  # nested
            ({"x": [{"k": True}]}, {"x": [{"k": 1}]}, False),  # nested bool vs int
        ],
    )
    def test_scalars_and_containers(self, left, right, expected):
        assert managed_files.is_semantically_equal(left, right) is expected

    def test_nan_floats_compare_equal(self):
        # A config that already holds a NaN must not be rewritten on every launch.
        assert managed_files.is_semantically_equal(float("nan"), float("nan")) is True
        assert managed_files.is_semantically_equal({"x": float("nan")}, {"x": float("nan")}) is True
        assert managed_files.is_semantically_equal(float("nan"), 1.0) is False

    def test_unwraps_tomlkit_values(self):
        import tomlkit

        doc = tomlkit.parse('b = true\ni = 1\n[t]\nk = "v"\n')
        assert (
            managed_files.is_semantically_equal(doc, {"b": True, "i": 1, "t": {"k": "v"}}) is True
        )
        # A parsed-TOML bool still never equals an int.
        assert managed_files.is_semantically_equal(doc, {"b": 1, "i": 1, "t": {"k": "v"}}) is False


def test_revert_semantic_noop_preserves_bytes_without_sudo(tmp_path, backup_dir, monkeypatch):
    # An admin who edited ug's owned value to their own leaves nothing for revert to remove; the
    # three-way merge is a semantic no-op and must not shell out to sudo just to reserialize.
    path = tmp_path / "managed.json"
    monkeypatch.setattr(
        managed_files, "_sudo_replace", lambda t, text: t.write_text(text, encoding="utf-8")
    )
    path.write_text('{"enterprise": true}\n', encoding="utf-8")
    managed_files.reconcile_managed_file(
        path,
        '{"enterprise": true, "ucode": "v1"}\n',
        tool="claude",
        display="Claude Code",
        owned_paths=[["ucode"]],
        parser=json.loads,
    )
    admin_text = '{"ucode":"admin","enterprise":true,"note":"x"}\n'  # reordered + extra key
    path.write_text(admin_text, encoding="utf-8")

    def no_sudo(*args, **kwargs):
        pytest.fail("semantic no-op must not invoke sudo")

    monkeypatch.setattr(managed_files, "_sudo_replace", no_sudo)
    monkeypatch.setattr(managed_files, "_sudo_remove", no_sudo)

    managed_files.revert_managed_file(
        "claude",
        display="Claude Code",
        parser=json.loads,
        dumper=lambda doc: json.dumps(doc, sort_keys=True) + "\n",
    )
    assert path.read_text() == admin_text  # exact bytes preserved


@pytest.mark.parametrize("command", [None, "ug configure", "ug revert"])
@pytest.mark.parametrize("os_name", ["LINUX", "WINDOWS"])
@pytest.mark.parametrize(
    ("interactive", "repair_existing", "expected"),
    [(False, False, False), (False, True, True), (True, False, True)],
)
def test_managed_writes_policy(
    monkeypatch, command, os_name, interactive, repair_existing, expected
):
    # `ug configure` and `ug revert` always attempt the write; the write settles privilege.
    monkeypatch.setattr(managed_files, "current_os", lambda: getattr(managed_files.OS, os_name))
    monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: interactive)
    managed_files.request_explicit_managed_writes(command)
    # Windows has no sudo to prompt with: only an elevated process (not this one) may write.
    other_run = interactive and os_name == "LINUX"
    repair = expected and os_name == "LINUX"

    assert managed_files.managed_writes_allowed() is (command is not None or other_run)
    assert managed_files.managed_writes_allowed(repair_existing=repair_existing) is (
        command is not None or repair
    )


@pytest.mark.parametrize("interactive", [False, True])
def test_sudo_command_derives_prompt_policy(monkeypatch, interactive):
    monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: interactive)

    expected = ["/usr/bin/sudo", *([] if interactive else ["-n"]), "cp", "a", "b"]
    assert managed_files._sudo_command("cp", "a", "b") == expected


@pytest.mark.parametrize("value", ["1", "true"])
def test_noninteractive_env_never_lets_sudo_prompt(monkeypatch, value):
    """A wrapper's unattended terminal gets sudo's non-prompting mode, as if no terminal were attached."""
    monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv(NONINTERACTIVE_ENV, value)

    assert managed_files._sudo_may_prompt() is False
    assert managed_files.managed_writes_allowed() is False
    assert managed_files.managed_writes_allowed(repair_existing=True) is True
    assert managed_files._sudo_command("cp", "a", "b") == ["/usr/bin/sudo", "-n", "cp", "a", "b"]


def test_noninteractive_env_unset_keeps_the_interactive_prompt(monkeypatch):
    monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv(NONINTERACTIVE_ENV, raising=False)

    assert managed_files._sudo_may_prompt() is True
