"""Tests for managed settings without real ``sudo`` or ``/etc`` writes."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import ucode.agents.claude as claude_agent
import ucode.codex_config as codex_config
import ucode.config_io as config_io
from ucode import managed_files

_REAL_SUDO_REPLACE = managed_files._sudo_replace


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
        monkeypatch.setattr(managed_files.subprocess, "Popen", popen)
        monkeypatch.setattr(
            managed_files.subprocess,
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
        monkeypatch.setattr(managed_files.subprocess, "run", run)
        monkeypatch.setattr(
            managed_files.subprocess,
            "Popen",
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
        assert kwargs == {"capture_output": True, "text": True, "check": True}
        assert source_path is not None and not source_path.exists()

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
            (os_enum.value, str(path))
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


@pytest.mark.skipif(sys.platform == "win32", reason="The managed writer is Unix-only")
class TestManagedWorkerShell:
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

    @pytest.mark.parametrize("parent_exists", [True, False])
    def test_real_shell_creates_missing_file(self, tmp_path, parent_exists):
        """The new-file branch installs content, mode, and (root) ownership from scratch."""
        source = tmp_path / "source.json"
        source.write_text("managed settings\n", encoding="utf-8")
        parent = tmp_path if parent_exists else tmp_path / "codex"
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
        expected_chown_calls = []
        if not parent_exists:
            assert parent.stat().st_mode & 0o777 == 0o755
            expected_chown_calls.append(f"0:0 {parent}")
        chown_calls = chown_log.read_text(encoding="utf-8").splitlines()
        assert chown_calls[: len(expected_chown_calls)] == expected_chown_calls
        assert len(chown_calls) == len(expected_chown_calls) + 1
        staging_owner, _, staging_arg = chown_calls[-1].partition(" ")
        assert staging_owner == "0:0"
        staging_path = Path(staging_arg)
        assert staging_path.parent == parent
        assert staging_path.name.startswith(".managed_config.toml.ucode.")
        assert not list(parent.glob(".managed_config.toml.ucode.*"))


class TestManagedFileLifecycle:
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


def test_managed_writes_disabled_without_tty(monkeypatch):
    monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: False)

    assert managed_files.managed_writes_allowed() is False


def test_sudo_command_refuses_noninteractive_execution(monkeypatch):
    monkeypatch.setattr(managed_files, "managed_writes_allowed", lambda: False)

    with pytest.raises(RuntimeError, match="Refusing to invoke sudo"):
        managed_files._sudo_command("cp", "a", "b")
