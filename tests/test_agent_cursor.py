"""Tests for the Cursor MCP-only agent integration."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import pytest

from ucode.agents import cursor

WS = "https://example.databricks.com"
# The proxy argv ucode registers for every MCP server (see build_mcp_proxy_argv).
PROXY_ARGV = ["ucode", "mcp-proxy", "--url", f"{WS}/api/2.0/mcp/functions/system/ai"]


class TestMcpServerEntry:
    def test_builds_stdio_command_entry_from_proxy_argv(self):
        entry = cursor.build_mcp_server_entry(PROXY_ARGV)
        # Cursor's stdio schema splits argv into command + args; no token here —
        # the proxy authenticates itself.
        assert entry == {"command": "ucode", "args": PROXY_ARGV[1:]}
        assert "headers" not in entry


class TestWriteMcpServerConfig:
    def test_writes_without_clobbering_existing_entries(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp.json"
        monkeypatch.setattr(cursor, "CURSOR_MCP_CONFIG_PATH", config_file)
        # A pre-existing Cursor config (e.g. the user's own proxy entry).
        config_file.write_text(
            json.dumps({"mcpServers": {"proxy": {"command": "mcp", "args": ["start"]}}}),
            encoding="utf-8",
        )

        removed = cursor.write_mcp_server_config("databricks-system-ai", PROXY_ARGV)

        written = json.loads(config_file.read_text())
        assert removed is False
        assert written["mcpServers"]["proxy"] == {"command": "mcp", "args": ["start"]}
        assert written["mcpServers"]["databricks-system-ai"] == {
            "command": "ucode",
            "args": PROXY_ARGV[1:],
        }

    def test_creates_config_when_absent(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp.json"
        monkeypatch.setattr(cursor, "CURSOR_MCP_CONFIG_PATH", config_file)

        removed = cursor.write_mcp_server_config("databricks-sql", PROXY_ARGV)

        assert removed is False
        assert json.loads(config_file.read_text())["mcpServers"]["databricks-sql"]["command"] == (
            "ucode"
        )

    def test_reports_replaced_entry(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp.json"
        monkeypatch.setattr(cursor, "CURSOR_MCP_CONFIG_PATH", config_file)
        config_file.write_text(
            json.dumps({"mcpServers": {"databricks-sql": {"command": "old"}}}),
            encoding="utf-8",
        )

        removed = cursor.write_mcp_server_config("databricks-sql", PROXY_ARGV)

        assert removed is True
        assert (
            json.loads(config_file.read_text())["mcpServers"]["databricks-sql"]["args"]
            == (PROXY_ARGV[1:])
        )


AIGW_URL = f"{WS}/ai-gateway/mcp-services/system.ai.github"


class TestHttpMcpServerEntry:
    def test_builds_url_plus_auth_entry_with_pinned_client(self):
        entry = cursor.build_http_mcp_server_entry(AIGW_URL, "cursor-desktop")
        # A direct OAuth HTTP server: url + auth.CLIENT_ID, no stdio command and no
        # static bearer — Cursor drives the OAuth itself. Scopes come from the MCP
        # protected-resource metadata, so none are hard-coded here.
        assert entry == {"url": AIGW_URL, "auth": {"CLIENT_ID": "cursor-desktop"}}
        assert "command" not in entry
        assert "headers" not in entry

    def test_write_merges_and_reports_replacement(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp.json"
        monkeypatch.setattr(cursor, "CURSOR_MCP_CONFIG_PATH", config_file)
        config_file.write_text(
            json.dumps({"mcpServers": {"proxy": {"command": "keep"}}}),
            encoding="utf-8",
        )

        removed = cursor.write_http_mcp_server_config("github", AIGW_URL, "cursor-desktop")

        written = json.loads(config_file.read_text())
        assert removed is False
        assert written["mcpServers"]["proxy"] == {"command": "keep"}
        assert written["mcpServers"]["github"] == {
            "url": AIGW_URL,
            "auth": {"CLIENT_ID": "cursor-desktop"},
        }


class TestRemoveMcpServerConfig:
    def test_removes_without_clobbering_others(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp.json"
        monkeypatch.setattr(cursor, "CURSOR_MCP_CONFIG_PATH", config_file)
        config_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "databricks-sql": {"command": "ucode"},
                        "proxy": {"command": "keep"},
                    }
                }
            ),
            encoding="utf-8",
        )

        removed = cursor.remove_mcp_server_config("databricks-sql")

        written = json.loads(config_file.read_text())
        assert removed is True
        assert "databricks-sql" not in written["mcpServers"]
        assert written["mcpServers"]["proxy"] == {"command": "keep"}

    def test_returns_false_when_absent(self, tmp_path, monkeypatch):
        config_file = tmp_path / "mcp.json"
        monkeypatch.setattr(cursor, "CURSOR_MCP_CONFIG_PATH", config_file)
        config_file.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

        assert cursor.remove_mcp_server_config("databricks-sql") is False


class TestLaunch:
    def test_execs_cursor_agent_without_token_wiring(self, monkeypatch):
        # No OAUTH_TOKEN is set: the proxy handles auth, so launch is a thin exec.
        execs: list[list[str]] = []
        monkeypatch.setattr(cursor, "exec_or_spawn", lambda argv: execs.append(argv))

        cursor.launch({"workspace": WS}, ["--resume"])

        assert execs == [["cursor-agent", "--resume"]]


class TestLaunchApp:
    def test_uses_cursor_command_on_path(self, monkeypatch):
        monkeypatch.setattr(cursor.sys, "platform", "linux")
        monkeypatch.setattr(cursor.shutil, "which", lambda name: "/usr/bin/cursor")

        assert cursor.find_app_launch_argv(["."]) == ["/usr/bin/cursor", "."]

    def test_macos_prefers_open_app_bundle_over_cursor_command(self, tmp_path, monkeypatch):
        bundle = tmp_path / "Cursor.app"
        bundle.mkdir()
        monkeypatch.setattr(cursor.shutil, "which", lambda name: "/usr/bin/cursor")
        monkeypatch.setattr(cursor.sys, "platform", "darwin")
        monkeypatch.setattr(cursor, "CURSOR_MACOS_APP_PATHS", (tmp_path / "missing.app", bundle))
        monkeypatch.chdir(tmp_path)

        assert cursor.find_app_launch_argv(["."]) == ["open", "-a", str(bundle), str(tmp_path)]

    def test_flags_prefer_cursor_cli_even_on_macos(self, tmp_path, monkeypatch):
        bundle = tmp_path / "Cursor.app"
        bundle.mkdir()
        monkeypatch.setattr(cursor.shutil, "which", lambda name: "/usr/bin/cursor")
        monkeypatch.setattr(cursor.sys, "platform", "darwin")
        monkeypatch.setattr(cursor, "CURSOR_MACOS_APP_PATHS", (bundle,))

        assert cursor.find_app_launch_argv(["--new-window", "."]) == [
            "/usr/bin/cursor",
            "--new-window",
            ".",
        ]

    def test_macos_flags_without_cli_use_args_with_absolute_paths(self, tmp_path, monkeypatch):
        bundle = tmp_path / "Cursor.app"
        bundle.mkdir()
        (tmp_path / "a.py").write_text("")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(cursor.shutil, "which", lambda name: None)
        monkeypatch.setattr(cursor.sys, "platform", "darwin")
        monkeypatch.setattr(cursor, "CURSOR_MACOS_APP_PATHS", (bundle,))

        assert cursor.find_app_launch_argv(["--goto", "a.py:3", "."]) == [
            "open",
            "-a",
            str(bundle),
            "--args",
            "--goto",
            str(tmp_path / "a.py") + ":3",
            str(tmp_path),
        ]

    def test_macos_paths_only_are_made_absolute(self, tmp_path, monkeypatch):
        bundle = tmp_path / "Cursor.app"
        bundle.mkdir()
        (tmp_path / "a").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(cursor.sys, "platform", "darwin")
        monkeypatch.setattr(cursor, "CURSOR_MACOS_APP_PATHS", (bundle,))

        assert cursor.find_app_launch_argv(["a", "nonexistent"]) == [
            "open",
            "-a",
            str(bundle),
            str(tmp_path / "a"),
            "nonexistent",
        ]

    def test_macos_without_bundle_uses_cursor_command(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cursor.shutil, "which", lambda name: "/usr/bin/cursor")
        monkeypatch.setattr(cursor.sys, "platform", "darwin")
        monkeypatch.setattr(cursor, "CURSOR_MACOS_APP_PATHS", (tmp_path / "missing.app",))

        assert cursor.find_app_launch_argv(["."]) == ["/usr/bin/cursor", "."]

    def test_not_installed_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cursor.shutil, "which", lambda name: None)
        monkeypatch.setattr(cursor.sys, "platform", "linux")
        monkeypatch.setattr(cursor, "CURSOR_MACOS_APP_PATHS", (tmp_path / "missing.app",))

        assert cursor.find_app_launch_argv([]) is None

    def test_launch_app_starts_detached_without_waiting(self, monkeypatch):
        calls: list[tuple[list[str], dict]] = []
        proc = MagicMock()
        monkeypatch.setattr(
            cursor.subprocess_cross_os,
            "popen",
            lambda argv, **kw: (calls.append((argv, kw)), proc)[1],
        )
        cursor.launch_app(["cursor", "."])

        assert len(calls) == 1
        argv, kwargs = calls[0]
        assert argv == ["cursor", "."]
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        for key, value in cursor._DETACHED_POPEN_OPTIONS.items():
            assert kwargs[key] == value

    def test_launch_app_open_nonzero_exit_raises_with_stderr(self, monkeypatch):
        proc = MagicMock(returncode=1)
        proc.communicate.return_value = (None, b"Unable to find application")
        monkeypatch.setattr(cursor.subprocess_cross_os, "popen", lambda argv, **kw: proc)

        with pytest.raises(RuntimeError, match="exited 1.*Unable to find application"):
            cursor.launch_app(["open", "-a", "/x/Cursor.app"])

    def test_launch_app_open_success_and_timeout_do_not_raise(self, monkeypatch):
        ok = MagicMock(returncode=0)
        ok.communicate.return_value = (None, b"")
        slow = MagicMock(returncode=None)
        slow.communicate.side_effect = subprocess.TimeoutExpired("open", 10)
        procs = iter([ok, slow])
        monkeypatch.setattr(cursor.subprocess_cross_os, "popen", lambda argv, **kw: next(procs))

        cursor.launch_app(["open", "-a", "/x/Cursor.app"])
        cursor.launch_app(["open", "-a", "/x/Cursor.app"])

    def test_launch_app_cursor_cli_is_not_waited_on(self, monkeypatch):
        proc = MagicMock()
        monkeypatch.setattr(cursor.subprocess_cross_os, "popen", lambda argv, **kw: proc)

        cursor.launch_app(["/usr/bin/cursor", "."])

        proc.communicate.assert_not_called()
        proc.wait.assert_not_called()

    def test_launch_app_propagates_oserror(self, monkeypatch):
        def boom(argv, **kw):
            raise OSError("exec format error")

        monkeypatch.setattr(cursor.subprocess_cross_os, "popen", boom)

        with pytest.raises(OSError):
            cursor.launch_app(["/usr/bin/cursor", "."])


class TestWriteUserMcpServers:
    def test_batched_add_remove_preserves_user_entries(self, tmp_path, monkeypatch):
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"mine": {"command": "z"}, "gone": {}}}))
        monkeypatch.setattr(cursor, "CURSOR_MCP_CONFIG_PATH", path)

        cursor.write_user_mcp_servers(
            {"svc": cursor.build_mcp_server_entry(["ug", "mcp-proxy", "u"])}, {"gone"}
        )

        servers = json.loads(path.read_text())["mcpServers"]
        assert servers["mine"] == {"command": "z"}  # user's own kept
        assert "gone" not in servers
        assert servers["svc"] == {"command": "ug", "args": ["mcp-proxy", "u"]}
