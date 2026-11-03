"""Tests for opening a URL in the user's browser."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import ucode.os_compatibility.browser_cross_os as browser_cross_os

URL = "https://example.cloud.databricks.com/ml/ai-gateway"


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
@pytest.mark.parametrize(("returncode", "expected"), [(0, True), (1, False)])
def test_opens_with_webbrowser_in_a_child_process(monkeypatch, platform, returncode, expected):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(browser_cross_os.sys, "platform", platform)
    monkeypatch.setattr(browser_cross_os.subprocess_cross_os, "run", run)

    assert browser_cross_os.open_in_browser(URL) is expected
    args, kwargs = calls[0]
    assert args[0] == browser_cross_os.sys.executable
    assert args[-1] == URL
    assert kwargs["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize("error", [OSError("no python"), subprocess.TimeoutExpired("python", 10)])
def test_reports_failure_when_the_child_cannot_run(monkeypatch, error):
    def run(args, **kwargs):
        raise error

    monkeypatch.setattr(browser_cross_os.subprocess_cross_os, "run", run)

    assert browser_cross_os.open_in_browser(URL) is False
