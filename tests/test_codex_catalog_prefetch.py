"""Owned native extraction overlaps preflight without skipping catalog validation."""

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from ucode.agents import codex
from ucode.agents import codex_catalog as catalog

_BUNDLED = [{"slug": "gpt-5", "display_name": "GPT 5", "tool_mode": None}]


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists():
        if time.monotonic() >= deadline:
            pytest.fail(f"child did not create {path.name}")
        time.sleep(0.01)


@pytest.fixture
def native_child(monkeypatch):
    """Exercise real pipe/file/process lifetimes while replacing only the native program."""
    children = []
    real_popen = subprocess.Popen

    def install(script, pass_fds=()):
        def popen(argv, **kwargs):
            assert argv == ["selected-codex", "debug", "models", "--bundled"]
            home = Path(kwargs["cwd"])
            assert kwargs["env"]["CODEX_HOME"] == str(home)
            assert kwargs["stdin"] == subprocess.DEVNULL
            assert kwargs["start_new_session"] is True
            process = real_popen([sys.executable, "-c", script], pass_fds=pass_fds, **kwargs)
            children.append((process, home, kwargs["stdout"], kwargs["stderr"]))
            return process

        monkeypatch.setattr(catalog.subprocess_cross_os, "popen", popen)
        return children

    yield install
    for child, _, _, _ in children:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX extraction process groups")
def test_extraction_overlaps_work_and_is_validated_before_consumption(monkeypatch, native_child):
    # Output larger than a pipe's capacity must finish while the caller does other work.
    payload = {"models": [{**_BUNDLED[0], "metadata": "x" * 1_000_000}]}
    children = native_child(
        "from pathlib import Path; import json\n"
        "print(json.dumps({'models': [{'slug':'gpt-5','display_name':'GPT 5',"
        "'tool_mode':None,'metadata':'x'*1000000}]}))\n"
        "Path('ready').touch()\n"
    )
    validations = []

    def validate(binary, args, home):
        assert binary == "selected-codex"
        assert args[-2:] == ["debug", "models"]
        candidate = Path(json.loads(args[1].split("=", 1)[1]))
        result = json.loads(candidate.read_text())
        assert result["models"][0]["metadata"] == payload["models"][0]["metadata"]
        assert result["models"][0]["slug"] == "system.ai.gpt-5"
        validations.append(candidate)
        return subprocess.CompletedProcess(args, 0, "")

    monkeypatch.setattr(catalog, "_run_catalog_command", validate)
    with catalog.read_bundled_catalog_in_background("selected-codex") as pending:
        child, home, stdout, stderr = children[0]
        _wait_for(home / "ready")
        child.wait(timeout=5)
        assert validations == []
        result = catalog.prepare_codex_catalog(
            "selected-codex", ["system.ai.gpt-5"], bundled_read=pending
        )
        assert result["models"][0]["slug"] == "system.ai.gpt-5"
        assert len(validations) == 1
        assert not validations[0].exists()
    assert not home.exists()
    assert stdout.closed and stderr.closed
    assert not pending._timer.is_alive()


@pytest.mark.skipif(os.name != "posix", reason="POSIX extraction process groups")
@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_failed_or_interrupted_preflight_kills_extraction_and_cleans_up(native_child, failure):
    children = native_child(
        "from pathlib import Path; import time\nPath('ready').touch()\ntime.sleep(60)"
    )
    with pytest.raises(failure):
        with catalog.read_bundled_catalog_in_background("selected-codex") as pending:
            child, home, stdout, stderr = children[0]
            _wait_for(home / "ready")
            raise failure("preflight interrupted")
    assert child.poll() == -signal.SIGKILL
    assert not home.exists()
    assert stdout.closed and stderr.closed
    assert not pending._timer.is_alive()


@pytest.mark.skipif(os.name != "posix", reason="POSIX extraction process groups")
def test_watchdog_bounds_child_while_caller_is_busy(monkeypatch, native_child):
    children = native_child("import time; time.sleep(60)")
    monkeypatch.setattr(catalog, "_TIMEOUT_SECONDS", 0.1)
    with catalog.read_bundled_catalog_in_background("selected-codex") as pending:
        child, home, _, _ = children[0]
        child.wait(timeout=5)
        assert child.returncode == -signal.SIGKILL
        with pytest.raises(RuntimeError, match="Could not build the managed Codex"):
            catalog.prepare_codex_catalog("selected-codex", ["gpt-5"], bundled_read=pending)
    assert not home.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX extraction process groups")
def test_cancellation_also_kills_the_wrappers_native_child(native_child):
    read_fd, write_fd = os.pipe()
    descendant = "from pathlib import Path; import time; Path('ready').touch(); time.sleep(60)"
    children = native_child(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}], pass_fds=({write_fd},))\n"
        "time.sleep(60)\n",
        pass_fds=(write_fd,),
    )
    try:
        with catalog.read_bundled_catalog_in_background("selected-codex"):
            os.close(write_fd)
            write_fd = None
            _wait_for(children[0][1] / "ready")
        # EOF means both the wrapper and its descendant closed their inherited write ends.
        assert select.select([read_fd], [], [], 5)[0] == [read_fd]
        assert os.read(read_fd, 1) == b""
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="POSIX extraction process groups")
def test_watchdog_start_failure_cleans_up(monkeypatch, native_child):
    children = native_child("import time; time.sleep(60)")
    monkeypatch.setattr(threading.Timer, "start", Mock(side_effect=RuntimeError("no threads")))
    with pytest.raises(RuntimeError, match="no threads"):
        with catalog.read_bundled_catalog_in_background("selected-codex"):
            pytest.fail("must not enter after watchdog failure")
    child, home, stdout, stderr = children[0]
    assert child.poll() is not None
    assert not home.exists()
    assert stdout.closed and stderr.closed


@pytest.mark.skipif(os.name != "posix", reason="POSIX extraction process groups")
def test_native_failure_keeps_actionable_error_and_still_cleans_up(monkeypatch, native_child):
    children = native_child("import sys; print('private-native-diagnostic'); sys.exit(7)")
    validate = Mock(side_effect=AssertionError("must not validate failed extraction"))
    monkeypatch.setattr(catalog, "_run_catalog_command", validate)
    with catalog.read_bundled_catalog_in_background("selected-codex") as pending:
        with pytest.raises(RuntimeError, match="Gateway discovery was not used") as failure:
            catalog.prepare_codex_catalog("selected-codex", ["gpt-5"], bundled_read=pending)
        assert "private-native-diagnostic" not in str(failure.value)
    assert not children[0][1].exists()


def test_changed_binary_uses_a_fresh_extraction(monkeypatch):
    pending = Mock(binary="old-codex")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"models": _BUNDLED})))
    monkeypatch.setattr(catalog, "_run_catalog_command", run)

    result = catalog.prepare_codex_catalog("new-codex", ["gpt-5"], bundled_read=pending)

    assert result["models"][0]["slug"] == "gpt-5"
    pending.result.assert_not_called()
    assert [call.args[0] for call in run.call_args_list] == ["new-codex", "new-codex"]


def test_config_dispatch_still_requires_validation_before_any_write(monkeypatch):
    from ucode import agents

    pending = Mock(binary=codex.SPEC["binary"])
    pending.result.return_value = subprocess.CompletedProcess(
        [], 0, json.dumps({"models": _BUNDLED})
    )
    monkeypatch.setattr(codex, "_use_legacy_layout", lambda: False)
    detach = Mock()
    monkeypatch.setattr(codex, "_detach_app_catalog_after_failure", detach)
    writes = Mock(side_effect=AssertionError("configuration must wait for validation"))
    monkeypatch.setattr(codex, "write_toml_file", writes)
    monkeypatch.setattr(
        catalog,
        "_run_catalog_command",
        Mock(side_effect=subprocess.CalledProcessError(1, ["codex", "debug", "models"])),
    )
    with pytest.raises(RuntimeError, match="Could not build the managed Codex"):
        agents.configure_tool(
            "codex",
            {"workspace": "https://example.invalid", "codex_static_models": ["gpt-5"]},
            bundled_codex_catalog=pending,
        )
    pending.result.assert_called_once_with()
    writes.assert_not_called()
    detach.assert_called_once_with()


def test_windows_keeps_synchronous_extraction(monkeypatch):
    monkeypatch.setattr(catalog.os, "name", "nt")
    start = Mock(side_effect=AssertionError("no process group support"))
    monkeypatch.setattr(catalog.subprocess_cross_os, "popen", start)
    with catalog.read_bundled_catalog_in_background("codex") as pending:
        assert pending is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX extraction process groups")
def test_failed_speculative_start_leaves_normal_error_handling_in_place(monkeypatch):
    monkeypatch.setattr(
        catalog.subprocess_cross_os, "popen", Mock(side_effect=FileNotFoundError("missing"))
    )
    with catalog.read_bundled_catalog_in_background("codex") as pending:
        assert pending is None


@pytest.mark.parametrize("machine_wide,legacy", [(True, False), (False, True)])
def test_unsupported_static_catalog_does_not_start_extraction(monkeypatch, machine_wide, legacy):
    monkeypatch.setattr(codex, "machine_wide_only", lambda: machine_wide)
    monkeypatch.setattr(codex, "_use_legacy_layout", lambda: legacy)
    start = Mock(side_effect=AssertionError("catalog will not be used"))
    monkeypatch.setattr(codex, "read_bundled_catalog_in_background", start)
    with codex.prefetch_static_catalog() as pending:
        assert pending is None
