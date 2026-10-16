"""Offline checks for the live e2e suite's one-time agent warm-up."""

from __future__ import annotations

import subprocess

from tests import test_e2e as e2e


def _record_runs(monkeypatch, outcome):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(e2e, "_warmed_agent_configs", set())
    monkeypatch.setattr(e2e.subprocess_cross_os, "run", run)
    return calls


def test_opencode_warms_each_config_dir_once(monkeypatch):
    calls = _record_runs(monkeypatch, subprocess.CompletedProcess(["opencode"], 0, "", ""))

    for config in ("/a", "/a", "/b"):
        e2e._warm_agent("opencode", {"XDG_CONFIG_HOME": config})

    assert [cmd for cmd, _ in calls] == [["opencode", "models"]] * 2
    assert all(kw["timeout"] == e2e.AGENT_WARM_UP_TIMEOUT_SECONDS for _, kw in calls)


def test_warm_up_timeout_leaves_the_launch_to_fail(monkeypatch):
    _record_runs(monkeypatch, subprocess.TimeoutExpired(["opencode", "models"], 300))

    e2e._warm_agent("opencode", {"XDG_CONFIG_HOME": "/a"})

    assert e2e._warmed_agent_configs == {("opencode", "/a")}
