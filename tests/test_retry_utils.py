"""Offline unit tests for retry_utils — no network, no live workspace required."""

from __future__ import annotations

import subprocess
import time
import urllib.error
from unittest.mock import MagicMock

import pytest

from tests.retry_utils import is_transient_status, retry_transient

# ---------------------------------------------------------------------------
# is_transient_status
# ---------------------------------------------------------------------------


def test_transient_status_members():
    for code in (429, 502, 503, 504):
        assert is_transient_status(code)


def test_non_transient_status():
    for code in (200, 400, 401, 403, 404, 500):
        assert not is_transient_status(code)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sender(results):
    """Return a sender that pops values from *results* (exception → raise, else return)."""
    calls = []

    def send():
        calls.append(1)
        val = results.pop(0)
        if isinstance(val, BaseException):
            raise val
        return val

    return send, calls


# ---------------------------------------------------------------------------
# Basic success
# ---------------------------------------------------------------------------


def test_returns_immediately_on_success(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    send, calls = _make_sender(["ok"])
    result = retry_transient(send)

    assert result == "ok"
    assert len(calls) == 1
    assert sleep_calls == []  # no sleep on immediate success


# ---------------------------------------------------------------------------
# Retry on transient exception then succeed
# ---------------------------------------------------------------------------


def test_retries_on_transient_exception_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)

    import httpx

    err = httpx.TransportError("blip")
    send, calls = _make_sender([err, "ok"])
    result = retry_transient(send)

    assert result == "ok"
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Exhaust all attempts on persistent transient exception
# ---------------------------------------------------------------------------


def test_exhausts_attempts_and_reraises(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)

    import httpx

    err = httpx.TransportError("persistent")
    send, calls = _make_sender([err, err, err])
    with pytest.raises(httpx.TransportError):
        retry_transient(send, attempts=3)

    assert len(calls) == 3


# ---------------------------------------------------------------------------
# HTTPError: non-transient code → re-raise without retry
# ---------------------------------------------------------------------------


def test_http_error_non_transient_reraises_immediately(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    exc = urllib.error.HTTPError(url="http://x", code=400, msg="bad", hdrs=None, fp=None)
    send, calls = _make_sender([exc])
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        retry_transient(send)

    assert exc_info.value.code == 400
    assert len(calls) == 1  # only one attempt
    assert sleep_calls == []  # never slept


# ---------------------------------------------------------------------------
# HTTPError: transient code → retried
# ---------------------------------------------------------------------------


def test_http_error_transient_is_retried(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)

    err503 = urllib.error.HTTPError(url="http://x", code=503, msg="svc", hdrs=None, fp=None)
    send, calls = _make_sender([err503, "ok"])
    result = retry_transient(send)

    assert result == "ok"
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# retry_on_result: transient response returned after exhaustion
# ---------------------------------------------------------------------------


def test_retry_on_result_transient_exhausted_returns_last(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)

    resp = MagicMock()
    resp.status_code = 503
    send, calls = _make_sender([resp, resp, resp])
    result = retry_transient(send, attempts=3, retry_on_result=lambda r: r.status_code == 503)

    assert result is resp
    assert len(calls) == 3


def test_retry_on_result_ok_returns_immediately(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    resp = MagicMock()
    resp.status_code = 200
    send, calls = _make_sender([resp])
    result = retry_transient(send, retry_on_result=lambda r: r.status_code == 503)

    assert result is resp
    assert len(calls) == 1
    assert sleep_calls == []


# ---------------------------------------------------------------------------
# Caller-supplied exception type: subprocess.TimeoutExpired
# ---------------------------------------------------------------------------


def test_retries_on_caller_supplied_exception(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)

    timeout_err = subprocess.TimeoutExpired(cmd=["x"], timeout=1)
    send, calls = _make_sender([timeout_err, "done"])
    result = retry_transient(send, retry_exceptions=(subprocess.TimeoutExpired,))

    assert result == "done"
    assert len(calls) == 2
