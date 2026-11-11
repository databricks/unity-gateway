"""Startup observations terminate at the actual agent handoff."""

from unittest.mock import Mock

import pytest

from ucode import launcher, metrics


def test_startup_is_flushed_before_exec_without_waiting_for_agent(metrics_recorder, monkeypatch):
    metrics.start_agent("claude")
    order = []
    monkeypatch.setattr(metrics_recorder, "flush", lambda: order.append("flush"))
    monkeypatch.setattr(metrics_recorder, "start_periodic", lambda: None)
    monkeypatch.setattr(launcher.os, "name", "posix")
    monkeypatch.setattr(launcher.os, "execvp", lambda *_: order.append("exec"))
    launcher.exec_or_spawn(["claude"])
    assert order == ["flush", "exec"]
    assert [n for _, n, _, _ in metrics_recorder.pending] == ["ug_startup_duration_ms"]


def test_installer_failure_keeps_original_exception_and_dependency_label(
    metrics_recorder, monkeypatch
):
    import subprocess

    from ucode import agents

    monkeypatch.setattr(agents, "print_section", lambda *_: None)
    monkeypatch.setattr(agents, "print_warning", lambda *_: None)
    failure = subprocess.CalledProcessError(7, ["installer"])
    with pytest.raises(RuntimeError):
        agents._run_native_installer("claude", Mock(side_effect=failure), strict=True)
    rows = [(n, a) for _, n, _, a in metrics_recorder.pending]
    error = next(a for n, a in rows if n == "ug_cli_error")
    duration = next(a for n, a in rows if n == "dependency_install_duration_ms")
    assert duration["dependency"] == "claude"
    assert duration["action"] == "install"
    assert error["process_exit_code"] == 7
    assert error["continued_with_fallback"] is False
