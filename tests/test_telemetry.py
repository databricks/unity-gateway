"""Tests for ucode.telemetry."""

from __future__ import annotations

import re
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

    @staticmethod
    def _host(monkeypatch, platform_name, machine="x86_64", **probes):
        """Fake host; unset probes fail like a missing syscall would."""

        def missing(*_):
            raise OSError("unavailable")

        monkeypatch.setattr(sys, "platform", platform_name)
        monkeypatch.setattr(telemetry.platform, "machine", lambda: machine)
        for name in (
            "mac_ver",
            "win32_ver",
            "freedesktop_os_release",
        ):
            monkeypatch.setattr(telemetry.platform, name, probes.get(name, missing), raising=False)
        for name in ("_macos_translated", "_windows_native_arch"):
            monkeypatch.setattr(telemetry, name, probes.get(name, missing))

    @pytest.mark.parametrize(
        ("mac_ver", "expected"),
        [
            ("15.1.2", "(macos 15.1; arm64)"),
            ("26.0", "(macos 26.0; arm64)"),
            ("15", "(macos 15; arm64)"),
            ("", "(macos unknown; arm64)"),
        ],
    )
    def test_macos_version_is_major_minor(self, monkeypatch, mac_ver, expected):
        self._host(
            monkeypatch,
            "darwin",
            "arm64",
            mac_ver=lambda: (mac_ver, ("", "", ""), ""),
            _macos_translated=lambda: False,
        )
        assert telemetry.client_platform() == expected

    def test_windows_version_is_build_number(self, monkeypatch):
        self._host(
            monkeypatch,
            "win32",
            "AMD64",
            win32_ver=lambda: ("10", "10.0.22631", "SP0", "Multiprocessor Free"),
            _windows_native_arch=lambda: "x86_64",
        )
        assert telemetry.client_platform() == "(windows 22631; x86_64)"

    @pytest.mark.parametrize(
        ("release", "expected"),
        [
            ({"ID": "ubuntu", "VERSION_ID": "24.04"}, "(linux ubuntu-24.04; x86_64)"),
            ({"ID": "arch"}, "(linux arch; x86_64)"),
            ({"VERSION_ID": "1"}, "(linux unknown; x86_64)"),
            ({"ID": "Wei rd;(x)", "VERSION_ID": "1,2"}, "(linux wei-rd-x--1-2; x86_64)"),
        ],
    )
    def test_linux_version_comes_from_os_release(self, monkeypatch, release, expected):
        self._host(monkeypatch, "linux", freedesktop_os_release=lambda: release)
        assert telemetry.client_platform() == expected

    def test_linux_without_os_release_is_unknown(self, monkeypatch):
        self._host(monkeypatch, "linux")
        assert telemetry.client_platform() == "(linux unknown; x86_64)"

    def test_other_os_version_is_unknown(self, monkeypatch):
        self._host(monkeypatch, "freebsd14", "riscv64")
        assert telemetry.client_platform() == "(freebsd14 unknown; riscv64)"

    def test_rosetta_translated_process_reports_arm64(self, monkeypatch):
        self._host(
            monkeypatch,
            "darwin",
            "x86_64",
            mac_ver=lambda: ("15.1", ("", "", ""), ""),
            _macos_translated=lambda: True,
        )
        assert telemetry.client_platform() == "(macos 15.1; arm64)"

    def test_macos_probe_failure_falls_back_to_interpreter_arch(self, monkeypatch):
        self._host(monkeypatch, "darwin", "x86_64")
        assert telemetry.client_platform() == "(macos unknown; x86_64)"

    def test_windows_native_arm64_under_emulation(self, monkeypatch):
        self._host(
            monkeypatch,
            "win32",
            "AMD64",
            win32_ver=lambda: ("11", "10.0.26100", "SP0", ""),
            _windows_native_arch=lambda: "arm64",
        )
        assert telemetry.client_platform() == "(windows 26100; arm64)"

    @pytest.mark.parametrize("probe", [lambda: None, "raises"])
    def test_windows_probe_failure_falls_back_to_interpreter_arch(self, monkeypatch, probe):
        self._host(
            monkeypatch,
            "win32",
            "AMD64",
            win32_ver=lambda: ("10", "10.0.19045", "SP0", ""),
            **({} if probe == "raises" else {"_windows_native_arch": probe}),
        )
        assert telemetry.client_platform() == "(windows 19045; x86_64)"

    @pytest.mark.parametrize(
        ("native", "expected"),
        [(0x8664, "x86_64"), (0xAA64, "arm64"), (0x014C, "x86"), (0x01C4, "arm"), (0x1234, None)],
    )
    def test_windows_machine_table(self, native, expected):
        assert telemetry._WINDOWS_MACHINES.get(native) == expected

    @pytest.mark.parametrize(
        ("machine", "expected"),
        [
            ("AMD64", "x86_64"),
            ("x86_64", "x86_64"),
            ("x64", "x86_64"),
            ("arm64", "arm64"),
            ("aarch64", "arm64"),
            ("i386", "x86"),
            ("i686", "x86"),
            ("x86", "x86"),
            ("armv6l", "arm"),
            ("armv7l", "arm"),
            ("riscv64", "riscv64"),
            ("", "unknown"),
            ("x86 (64), v2", "x86-64-v2"),
        ],
    )
    def test_arch_aliases(self, machine, expected):
        assert telemetry.normalize_arch(machine) == expected

    @pytest.mark.parametrize(
        ("platform_name", "expected"),
        [
            ("darwin", "macos"),
            ("win32", "windows"),
            ("cygwin", "windows"),
            ("linux", "linux"),
            ("linux2", "linux"),
            ("sun,os:5", "sun-os-5"),
        ],
    )
    def test_os_names(self, platform_name, expected):
        assert telemetry.normalize_os(platform_name) == expected

    def test_fields_never_contain_separators(self, monkeypatch):
        self._host(
            monkeypatch,
            "sun,os:5",
            "x86 (64), v2",
        )
        result = telemetry.client_platform()
        assert result == "(sun-os-5 unknown; x86-64-v2)"
        for forbidden in ",:":
            assert forbidden not in result
        assert result.count("(") == result.count(")") == 1
        assert result.count(";") == 1

    def test_never_raises(self, monkeypatch):
        def boom():
            raise OSError("no uname")

        self._host(monkeypatch, "linux")
        monkeypatch.setattr(telemetry.platform, "machine", boom)
        assert telemetry.client_platform() == "(unknown unknown; unknown)"

    def test_real_host_is_well_formed(self):
        assert re.fullmatch(
            r"\([a-z0-9_.-]+ [a-z0-9_.-]+; [a-z0-9_.-]+\)", telemetry.client_platform()
        )

    def test_user_agent_keeps_gateway_prefix_and_appends_platform(self, monkeypatch):
        self._host(
            monkeypatch,
            "darwin",
            "arm64",
            mac_ver=lambda: ("15.1.2", ("", "", ""), ""),
            _macos_translated=lambda: False,
        )
        ua = telemetry.ucode_user_agent("0.9.1", "claude/2.1.300")
        assert ua == "ucode/0.9.1 claude/2.1.300 (macos 15.1; arm64)"
        assert ua.startswith("ucode/0.9.1 claude/2.1.300")
