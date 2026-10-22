"""Tests for ucode.telemetry."""

from __future__ import annotations

import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from unittest.mock import patch

import pytest

from ucode import telemetry


class TestUcodeVersion:
    def test_returns_installed_distribution_version(self):
        assert telemetry.ug_version() == version("unity-gateway")

    def test_missing_metadata_does_not_block_launch(self):
        with patch.object(telemetry, "version", side_effect=PackageNotFoundError):
            assert telemetry.ug_version.__wrapped__() == "unknown"


class TestAgentVersion:
    def setup_method(self):
        # The helper is @cache'd; clear between tests so each gets a clean run.
        telemetry.agent_version.cache_clear()

    def test_falls_back_when_binary_missing(self):
        assert telemetry.agent_version("definitely-not-a-real-binary-9f8a") == "unknown"

    def test_falls_back_on_timeout(self):
        with patch.object(
            subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=2),
        ):
            assert telemetry.agent_version("anything") == "unknown"

    def test_parses_claude_format(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="2.1.136 (Claude Code)\n", stderr=""
        )
        with patch.object(subprocess, "run", return_value=result):
            assert telemetry.agent_version("claude") == "2.1.136"

    def test_parses_codex_format(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="codex-cli 0.123.0-alpha.8\n", stderr=""
        )
        with patch.object(subprocess, "run", return_value=result):
            assert telemetry.agent_version("codex") == "0.123.0-alpha.8"

    def test_parses_gemini_format(self):
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout="0.40.0\n", stderr="")
        with patch.object(subprocess, "run", return_value=result):
            assert telemetry.agent_version("gemini") == "0.40.0"

    def test_parses_opencode_format(self):
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout="0.74.0\n", stderr="")
        with patch.object(subprocess, "run", return_value=result):
            assert telemetry.agent_version("opencode") == "0.74.0"

    def test_parses_copilot_format(self):
        # Copilot prints a banner with the version embedded mid-line.
        stdout = "GitHub Copilot CLI 1.0.42-0.\nRun 'copilot update' to check for updates.\n"
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
        with patch.object(subprocess, "run", return_value=result):
            assert telemetry.agent_version("copilot") == "1.0.42-0."

    def test_parses_pi_dev_build_format(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="0.0.0-dev-202604280149\n", stderr=""
        )
        with patch.object(subprocess, "run", return_value=result):
            assert telemetry.agent_version("pi") == "0.0.0-dev-202604280149"

    def test_falls_back_to_stderr_when_stdout_empty(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="my-tool 9.9.9\n"
        )
        with patch.object(subprocess, "run", return_value=result):
            assert telemetry.agent_version("foo") == "9.9.9"

    def test_unknown_when_no_semver_in_output(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="hello world\n", stderr=""
        )
        with patch.object(subprocess, "run", return_value=result):
            assert telemetry.agent_version("foo") == "unknown"


class TestClientPlatform:
    def setup_method(self):
        telemetry.client_platform.cache_clear()

    def teardown_method(self):
        telemetry.client_platform.cache_clear()

    @pytest.mark.parametrize(
        ("platform_name", "machine", "expected"),
        [
            ("darwin", "arm64", "(macos; arm64)"),
            ("darwin", "x86_64", "(macos; x86_64)"),
            ("win32", "AMD64", "(windows; x86_64)"),
            ("cygwin", "x64", "(windows; x86_64)"),
            ("linux", "aarch64", "(linux; arm64)"),
            ("linux2", "ARM64", "(linux; arm64)"),
            ("freebsd14", "riscv64", "(freebsd14; riscv64)"),
            ("linux", "", "(linux; unknown)"),
            ("sun,os:5", "x86 (64), v2", "(sun-os-5; x86-64-v2)"),
        ],
    )
    def test_maps_os_and_arch(self, monkeypatch, platform_name, machine, expected):
        monkeypatch.setattr(sys, "platform", platform_name)
        monkeypatch.setattr(telemetry.platform, "machine", lambda: machine)
        assert telemetry.client_platform() == expected
        for forbidden in ",:":
            assert forbidden not in expected

    def test_never_raises(self, monkeypatch):
        def boom():
            raise OSError("no uname")

        monkeypatch.setattr(telemetry.platform, "machine", boom)
        assert telemetry.client_platform() == "(unknown; unknown)"

    def test_user_agent_keeps_gateway_prefix_and_appends_platform(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(telemetry.platform, "machine", lambda: "arm64")
        ua = telemetry.ucode_user_agent("0.9.1", "claude", "2.1.300")
        assert ua == "ucode/0.9.1 claude/2.1.300 (macos; arm64)"
        assert ua.startswith("ucode/0.9.1 claude/2.1.300")
