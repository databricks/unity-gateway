"""Offline checks for the live e2e suite's fast-fail retry around agents and gateway requests."""

from __future__ import annotations

import io
import subprocess
from urllib import error as urllib_error

import httpx
import pytest

from tests import test_e2e as e2e

TIMEOUT = e2e.E2E_ATTEMPT_TIMEOUT_SECONDS


@pytest.fixture
def sleeps(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(e2e.time, "sleep", waited.append)
    return waited


def _completed(returncode=0, stdout="ok", stderr=""):
    return subprocess.CompletedProcess(["agent"], returncode, stdout, stderr)


def _script(monkeypatch, target, attr, outcomes):
    """Replace `target.attr` with a stub that plays `outcomes` in order, recording calls."""
    calls = []

    def play(*args, **kwargs):
        calls.append(kwargs)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(target, attr, play)
    return calls


@pytest.mark.parametrize(("platform", "expected"), [("linux", 30), ("darwin", 30), ("win32", 60)])
def test_attempt_timeout_defaults_by_os(platform, expected):
    assert e2e._attempt_timeout_seconds({}, platform) == expected
    assert e2e.E2E_RETRIES == 3


def test_attempt_timeout_override_wins_on_every_os():
    for platform in ("linux", "win32"):
        assert e2e._attempt_timeout_seconds({"UCODE_E2E_AGENT_TIMEOUT": "90"}, platform) == 90
        assert e2e._attempt_timeout_seconds({"UCODE_E2E_AGENT_TIMEOUT": " "}, platform) == (
            60 if platform == "win32" else 30
        )


def test_agent_success_returns_without_retrying(monkeypatch, sleeps):
    calls = _script(monkeypatch, e2e.subprocess_cross_os, "run", [_completed()])

    assert e2e._run_agent(["agent"]).stdout == "ok"
    assert len(calls) == 1 and calls[0]["timeout"] == TIMEOUT
    assert sleeps == []


def test_agent_retries_timeouts_failures_and_empty_output_with_backoff(monkeypatch, sleeps):
    outcomes = [
        subprocess.TimeoutExpired(["agent"], TIMEOUT),
        _completed(returncode=1, stdout="", stderr="HTTP 429"),
        _completed(stdout="", stderr=""),
        _completed(),
    ]
    calls = _script(monkeypatch, e2e.subprocess_cross_os, "run", outcomes)

    assert e2e._run_agent(["agent"]).stdout == "ok"
    assert len(calls) == 4
    assert sleeps == [2, 4, 8]


def test_agent_returns_the_last_failure_after_three_retries(monkeypatch, sleeps):
    outcomes = [_completed(returncode=1, stdout="", stderr=f"fail {n}") for n in range(4)]
    _script(monkeypatch, e2e.subprocess_cross_os, "run", outcomes)

    result = e2e._run_agent(["agent"])

    assert (result.returncode, result.stderr) == (1, "fail 3")
    assert sleeps == [2, 4, 8]


def test_agent_raises_the_last_timeout_after_three_retries(monkeypatch, sleeps):
    outcomes = [subprocess.TimeoutExpired(["agent"], TIMEOUT) for _ in range(4)]
    _script(monkeypatch, e2e.subprocess_cross_os, "run", outcomes)

    with pytest.raises(subprocess.TimeoutExpired):
        e2e._run_agent(["agent"])
    assert sleeps == [2, 4, 8]


def _response(status):
    return httpx.Response(status, request=httpx.Request("POST", "https://gateway/v1/messages"))


def _messages(monkeypatch):
    monkeypatch.setattr(e2e, "build_tool_base_url", lambda *_args: "https://gateway")
    return lambda: e2e.TestAnthropicNonRelayMps._messages_request("ws", "token", "model")


def test_messages_request_retries_transient_failures(monkeypatch, sleeps):
    outcomes = [httpx.ReadTimeout("slow"), _response(503), _response(429), _response(200)]
    calls = _script(monkeypatch, e2e.httpx, "post", outcomes)

    assert _messages(monkeypatch)().status_code == 200
    assert len(calls) == 4 and all(call["timeout"] == TIMEOUT for call in calls)
    assert sleeps == [2, 4, 8]


def test_messages_request_returns_a_client_error_at_once(monkeypatch, sleeps):
    # test_rejects_model_outside_allowlist expects the gateway's 403 itself.
    calls = _script(monkeypatch, e2e.httpx, "post", [_response(403)])

    assert _messages(monkeypatch)().status_code == 403
    assert len(calls) == 1 and sleeps == []


def _http_error(code):
    return urllib_error.HTTPError("https://gateway", code, "error", {}, io.BytesIO(b"{}"))


class _Body(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_gemini_smoke_retries_transient_failures(monkeypatch, sleeps):
    monkeypatch.setattr(e2e, "build_tool_base_url", lambda *_args: "https://gateway")
    answer = b'{"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}'
    outcomes = [TimeoutError("slow"), _http_error(502), _Body(answer)]
    calls = _script(monkeypatch, e2e.urllib_request, "urlopen", outcomes)

    assert e2e._run_gemini_gateway_smoke("ws", "model", "token") == "hi"
    assert len(calls) == 3 and all(call["timeout"] == TIMEOUT for call in calls)
    assert sleeps == [2, 4]


def test_gemini_smoke_fails_a_client_error_at_once(monkeypatch, sleeps):
    monkeypatch.setattr(e2e, "build_tool_base_url", lambda *_args: "https://gateway")
    _script(monkeypatch, e2e.urllib_request, "urlopen", [_http_error(401)])

    with pytest.raises(AssertionError, match="HTTP 401"):
        e2e._run_gemini_gateway_smoke("ws", "model", "token")
    assert sleeps == []


def test_pi_skips_gpt_5_5_pro_only():
    assert e2e._model_is_skipped("system.ai.gpt-5-5-pro", "pi")
    assert not e2e._model_is_skipped("system.ai.gpt-5-5-pro", "codex")
    assert not e2e._model_is_skipped("system.ai.gpt-5-5", "pi")


def test_opencode_warms_each_config_dir_once(monkeypatch, sleeps):
    monkeypatch.setattr(e2e, "_warmed_opencode_configs", set())
    calls = _script(
        monkeypatch, e2e.subprocess_cross_os, "run", [_completed(), _completed(), _completed()]
    )

    for config in ("/a", "/a", "/b"):
        e2e._warm_opencode({"XDG_CONFIG_HOME": config})

    assert len(calls) == 2
    assert all(call["timeout"] == e2e.OPENCODE_WARM_UP_TIMEOUT_SECONDS for call in calls)


def test_opencode_warm_up_timeout_leaves_the_launch_to_fail(monkeypatch, sleeps):
    monkeypatch.setattr(e2e, "_warmed_opencode_configs", set())
    _script(
        monkeypatch,
        e2e.subprocess_cross_os,
        "run",
        [subprocess.TimeoutExpired(["opencode", "models"], 300)],
    )

    e2e._warm_opencode({"XDG_CONFIG_HOME": "/a"})

    assert e2e._warmed_opencode_configs == {"/a"}
