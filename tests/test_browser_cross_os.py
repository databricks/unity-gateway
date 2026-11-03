"""Tests for opening a URL in the user's browser across operating systems."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import ucode.os_compatibility.browser_cross_os as browser_cross_os

URL = "https://example.cloud.databricks.com/ml/ai-gateway"


def _fake_run(returncodes: dict[str, int], calls: list[list[str]]):
    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["stdin"] is subprocess.DEVNULL
        return SimpleNamespace(returncode=returncodes[args[0]])

    return run


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_opens_with_webbrowser_in_a_child_process(monkeypatch, platform):
    calls: list[list[str]] = []
    monkeypatch.setattr(browser_cross_os.sys, "platform", platform)
    monkeypatch.setattr(browser_cross_os, "_is_wsl", lambda: False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(
        browser_cross_os.subprocess_cross_os,
        "run",
        _fake_run({browser_cross_os.sys.executable: 0}, calls),
    )

    assert browser_cross_os.open_in_browser(URL) is True
    assert calls[0][0] == browser_cross_os.sys.executable
    assert calls[0][-1] == URL


@pytest.mark.parametrize("error", [OSError("no python"), subprocess.TimeoutExpired("python", 10)])
def test_reports_failure_when_the_child_cannot_run(monkeypatch, error):
    def run(args, **kwargs):
        raise error

    monkeypatch.setattr(browser_cross_os.sys, "platform", "darwin")
    monkeypatch.setattr(browser_cross_os, "_is_wsl", lambda: False)
    monkeypatch.setattr(browser_cross_os.subprocess_cross_os, "run", run)

    assert browser_cross_os.open_in_browser(URL) is False


@pytest.mark.parametrize("browser", ["", "www-browser", "/usr/bin/lynx", "w3m %s"])
def test_headless_linux_skips_the_browser(monkeypatch, browser):
    calls: list[list[str]] = []
    monkeypatch.setattr(browser_cross_os.sys, "platform", "linux")
    monkeypatch.setattr(browser_cross_os, "_is_wsl", lambda: False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("BROWSER", browser)
    monkeypatch.setattr(browser_cross_os.subprocess_cross_os, "run", _fake_run({}, calls))

    assert browser_cross_os.open_in_browser(URL) is False
    assert calls == []


def test_headless_linux_uses_a_forwarding_browser(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(browser_cross_os.sys, "platform", "linux")
    monkeypatch.setattr(browser_cross_os, "_is_wsl", lambda: False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("BROWSER", "/home/u/.vscode-server/bin/helpers/browser.sh")
    monkeypatch.setattr(
        browser_cross_os.subprocess_cross_os,
        "run",
        _fake_run({browser_cross_os.sys.executable: 0}, calls),
    )

    assert browser_cross_os.open_in_browser(URL) is True


def test_wsl_falls_back_to_wslview(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(browser_cross_os, "_is_wsl", lambda: True)
    monkeypatch.setattr(browser_cross_os.shutil, "which", lambda name: "/usr/bin/wslview")
    monkeypatch.setattr(
        browser_cross_os.subprocess_cross_os,
        "run",
        _fake_run({browser_cross_os.sys.executable: 1, "/usr/bin/wslview": 0}, calls),
    )

    assert browser_cross_os.open_in_browser(URL) is True
    assert calls[1] == ["/usr/bin/wslview", URL]


def test_wsl_without_wslview_reports_failure(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(browser_cross_os, "_is_wsl", lambda: True)
    monkeypatch.setattr(browser_cross_os.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        browser_cross_os.subprocess_cross_os,
        "run",
        _fake_run({browser_cross_os.sys.executable: 1}, calls),
    )

    assert browser_cross_os.open_in_browser(URL) is False


@pytest.mark.parametrize(
    ("platform", "env", "proc_version", "expected"),
    [
        ("linux", {"WSL_DISTRO_NAME": "Ubuntu"}, "", True),
        ("linux", {}, "Linux version 5.15.0-microsoft-standard-WSL2", True),
        ("linux", {}, "Linux version 6.8.0-generic", False),
        ("win32", {"WSL_DISTRO_NAME": "Ubuntu"}, "", False),
        ("darwin", {}, "", False),
    ],
)
def test_is_wsl(monkeypatch, tmp_path, platform, env, proc_version, expected):
    monkeypatch.setattr(browser_cross_os.sys, "platform", platform)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    version_file = tmp_path / "version"
    version_file.write_text(proc_version, encoding="utf-8")
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda path, *a, **kw: (
            real_open(version_file, *a, **kw)
            if path == "/proc/version"
            else real_open(path, *a, **kw)
        ),
    )

    assert browser_cross_os._is_wsl() is expected
