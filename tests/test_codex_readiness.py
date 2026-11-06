"""Readiness polling for Codex's local app-server, with controlled clocks and processes."""

import subprocess
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from ucode.smart_routing import v2


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds):
        assert seconds > 0
        state.sleeps.append(seconds)
        state.now += seconds

    # Keep subprocess.wait's real clock/sleep intact when testing an actual exited child.
    monkeypatch.setattr(v2, "time", SimpleNamespace(monotonic=lambda: state.now, sleep=sleep))
    return state


def test_unhealthy_response_waits_before_retrying(monkeypatch, clock):
    statuses = iter([503, 204, 200])
    calls = []

    def get(url, *, timeout):
        calls.append((url, timeout))
        return nullcontext(SimpleNamespace(status=next(statuses)))

    monkeypatch.setattr(v2.urllib.request, "urlopen", get)

    assert v2._wait_for_app_server(1234, timeout=1)
    assert len(calls) == 3
    assert len(clock.sleeps) == 2
    assert all(url == "http://127.0.0.1:1234/healthz" for url, _ in calls)


def test_ready_server_returns_without_a_poll_delay(monkeypatch, clock):
    monkeypatch.setattr(
        v2.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: nullcontext(SimpleNamespace(status=200)),
    )

    assert v2._wait_for_app_server(1234, timeout=1)
    assert clock.sleeps == []


def test_request_and_retry_wait_share_the_remaining_deadline(monkeypatch, clock):
    timeouts = []

    def get(_url, *, timeout):
        timeouts.append(timeout)
        if len(timeouts) == 2:
            clock.now += timeout
        raise URLError("not ready")

    monkeypatch.setattr(v2.urllib.request, "urlopen", get)

    assert not v2._wait_for_app_server(1234, timeout=0.075)
    assert timeouts == pytest.approx([0.075, 0.025])
    assert clock.sleeps == pytest.approx([0.05])
    assert clock.now == pytest.approx(0.075)


def test_short_remaining_deadline_bounds_the_sleep(monkeypatch, clock):
    monkeypatch.setattr(
        v2.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: nullcontext(SimpleNamespace(status=204)),
    )

    assert not v2._wait_for_app_server(1234, timeout=0.01)
    assert clock.sleeps == pytest.approx([0.01])


@pytest.mark.parametrize("exit_code", [0, 7, -15])
def test_exited_child_never_waits_for_health(monkeypatch, clock, exit_code):
    monkeypatch.setattr(
        v2.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("exited child was probed"),
    )

    assert not v2._wait_for_app_server(
        1234, timeout=30, process=SimpleNamespace(poll=lambda: exit_code)
    )
    assert clock.sleeps == []


def test_child_exit_during_startup_stops_retrying(monkeypatch, clock):
    polls = iter([None, 7])
    requests = []

    def get(url, *, timeout):
        requests.append(url)
        raise URLError("connection refused")

    monkeypatch.setattr(v2.urllib.request, "urlopen", get)

    assert not v2._wait_for_app_server(
        1234, timeout=30, process=SimpleNamespace(poll=lambda: next(polls))
    )
    assert len(requests) == 1
    assert clock.sleeps == [0.05]


def test_keyboard_interrupt_propagates(monkeypatch, clock):
    def get(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(v2.urllib.request, "urlopen", get)
    with pytest.raises(KeyboardInterrupt):
        v2._wait_for_app_server(1234, timeout=30)
    assert clock.sleeps == []


def test_real_exited_child_is_observed_before_the_first_probe(monkeypatch, clock):
    child = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", "raise SystemExit(7)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert child.wait(timeout=10) == 7
        monkeypatch.setattr(
            v2.urllib.request,
            "urlopen",
            lambda *_args, **_kwargs: pytest.fail("exited child was probed"),
        )
        assert not v2._wait_for_app_server(1234, timeout=30, process=child)
        assert clock.sleeps == []
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.parametrize("exit_code", [7, None])
def test_launch_reports_failure_and_reaps_child_without_starting_tui(
    tmp_path, monkeypatch, clock, exit_code
):
    calls = []
    child = SimpleNamespace(
        poll=lambda: exit_code,
        terminate=lambda: calls.append("terminate"),
        wait=lambda timeout: calls.append(("wait", timeout)),
        kill=lambda: pytest.fail("clean shutdown should not need kill"),
    )

    def popen(argv, **kwargs):
        assert not calls, "a failed app-server must not start another process"
        calls.append(argv[:2])
        assert kwargs["stdin"] == subprocess.DEVNULL
        return child

    def unavailable(*_args, **_kwargs):
        raise URLError("connection refused")

    monkeypatch.setenv(v2.OAUTH_TOKEN_ENV_VAR, "test-token")
    monkeypatch.setenv(v2.SESSION_PYTHON_ENV_VAR, sys.executable)
    monkeypatch.setattr(v2, "launch_token", lambda *_args: "test-token")
    monkeypatch.setattr(v2, "custom_catalog_models", lambda: ["test-model"])
    monkeypatch.setattr(v2, "custom_catalog_path", lambda: None)
    monkeypatch.setattr(v2, "_v2_hooks", lambda *_args: {})
    monkeypatch.setattr(
        v2, "_prepare_smart_router_session", lambda _agent: tmp_path / "session.json"
    )
    monkeypatch.setattr(v2, "first_prompt_routing_enabled", lambda **_kwargs: True)
    monkeypatch.setattr(v2, "_free_port", lambda: 1234)
    monkeypatch.setattr(v2, "APP_SERVER_READY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(v2.subprocess_cross_os, "popen", popen)
    monkeypatch.setattr(v2.urllib.request, "urlopen", unavailable)

    expected = "exited with status 7" if exit_code is not None else "did not become ready"
    with pytest.raises(RuntimeError, match=expected):
        v2.launch_codex(
            {"workspace": "https://example.databricks.com"},
            [],
            binary="codex",
            start_model="test-model",
            render_overlay=lambda *_args, **_kwargs: {},
        )
    assert calls == [["codex", "app-server"], "terminate", ("wait", 5)]
