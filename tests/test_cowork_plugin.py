"""Claude Cowork plugin generation and scoped CLI installation."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucode.agents.desktop import cowork


def _cowork_root(desktop_root: Path, account: str, org: str) -> Path:
    root = desktop_root / "local-agent-mode-sessions" / account / org
    root.mkdir(parents=True)
    return root


def test_discover_cowork_root_requires_started_session(tmp_path):
    with pytest.raises(RuntimeError, match="start Claude Cowork once"):
        cowork.discover_cowork_root(desktop_root=tmp_path / "Claude-3p")


def test_discover_cowork_root_uses_only_candidate(tmp_path):
    desktop_root = tmp_path / "Claude-3p"
    expected = _cowork_root(desktop_root, "account", "org")

    assert cowork.discover_cowork_root(desktop_root=desktop_root) == expected


def test_discover_cowork_root_prefers_marked_candidate(tmp_path):
    desktop_root = tmp_path / "Claude-3p"
    _cowork_root(desktop_root, "account-a", "org-a")
    expected = _cowork_root(desktop_root, "account-b", "org-b")
    (expected / "cowork_plugins").mkdir()

    assert cowork.discover_cowork_root(desktop_root=desktop_root) == expected


def test_discover_cowork_root_prefers_new_active_candidate_over_old_marked_candidate(tmp_path):
    desktop_root = tmp_path / "Claude-3p"
    old = _cowork_root(desktop_root, "account-a", "org-a")
    old_marker = old / "cowork_plugins"
    old_marker.mkdir()
    new = _cowork_root(desktop_root, "account-b", "org-b")
    os.utime(old, ns=(100, 100))
    os.utime(old_marker, ns=(100, 100))
    os.utime(new, ns=(200, 200))

    assert cowork.discover_cowork_root(desktop_root=desktop_root) == new


def test_discover_cowork_root_uses_unique_newest_marker(tmp_path):
    desktop_root = tmp_path / "Claude-3p"
    old = _cowork_root(desktop_root, "account-a", "org-a")
    new = _cowork_root(desktop_root, "account-b", "org-b")
    for index, root in enumerate((old, new), start=1):
        marker = root / "cowork_settings.json"
        marker.write_text("{}")
        timestamp = 1_900_000_000_000_000_000 + index
        os.utime(marker, ns=(timestamp, timestamp))

    assert cowork.discover_cowork_root(desktop_root=desktop_root) == new


def test_discover_cowork_root_rejects_tied_candidates(tmp_path):
    desktop_root = tmp_path / "Claude-3p"
    candidates = [
        _cowork_root(desktop_root, "account-a", "org-a"),
        _cowork_root(desktop_root, "account-b", "org-b"),
    ]
    for root in candidates:
        marker = root / "cowork_settings.json"
        marker.write_text("{}")
        os.utime(marker, ns=(100, 100))
        os.utime(root, ns=(100, 100))

    with pytest.raises(RuntimeError, match="Could not determine the active Cowork profile"):
        cowork.discover_cowork_root(desktop_root=desktop_root)


def test_configure_generates_plugin_and_installs_in_cowork_scope(tmp_path, monkeypatch):
    app_dir = tmp_path / ".ucode"
    cowork_root = tmp_path / "Claude-3p" / "local-agent-mode-sessions" / "acct" / "org"
    cowork_root.mkdir(parents=True)
    claude_skill = tmp_path / "home" / ".claude" / "skills" / "review"
    agent_skill = tmp_path / "home" / ".agents" / "skills" / "review"
    claude_skill.mkdir(parents=True)
    agent_skill.mkdir(parents=True)
    (claude_skill / "SKILL.md").write_text("managed skill")
    (agent_skill / "SKILL.md").write_text("other copy")
    monkeypatch.setattr(cowork.config_io, "APP_DIR", app_dir)
    monkeypatch.setattr(cowork, "discover_cowork_root", lambda: cowork_root)
    monkeypatch.setattr(cowork.shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        cowork,
        "records_for_scope",
        lambda scope, base: [
            {
                "bundle_name": "review",
                "workspace": "https://workspace.example/",
                "dirs": [str(agent_skill), str(claude_skill)],
            },
            {
                "bundle_name": "foreign",
                "workspace": "https://other.example",
                "dirs": [str(claude_skill)],
            },
        ],
    )
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        stdout = "[]" if argv[1:4] == ["plugin", "list", "--cowork"] else '{"outcome":"ok"}'
        if argv[1:3] == ["plugin", "validate"]:
            stdout = "valid"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)

    assert cowork.configure_cowork(
        {"workspace": "https://workspace.example", "profile": "chosen"},
        [
            {
                "name": "github",
                "url": "https://workspace.example/api/2.0/mcp/github",
                "clients": ["claude", "codex"],
            },
            {
                "name": "codex-only",
                "url": "https://workspace.example/api/2.0/mcp/codex",
                "clients": ["codex"],
            },
        ],
    )

    plugin_root = app_dir / cowork.MARKETPLACE_DIRECTORY_NAME / "plugins" / cowork.PLUGIN_NAME
    plugin = json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["mcpServers"] == "./.mcp.json"
    mcp = json.loads((plugin_root / ".mcp.json").read_text())
    assert mcp == {
        "mcpServers": {
            "github": {
                "type": "stdio",
                "command": "/opt/bin/ug",
                "args": [
                    "mcp-proxy",
                    "--url",
                    "https://workspace.example/api/2.0/mcp/github",
                    "--host",
                    "https://workspace.example",
                    "--profile",
                    "chosen",
                ],
            }
        }
    }
    assert (plugin_root / "skills" / "review" / "SKILL.md").read_text() == "managed skill"
    assert not (plugin_root / "skills" / "foreign").exists()
    assert [call[0][1:3] for call in calls] == [
        ["plugin", "validate"],
        ["plugin", "marketplace"],
        ["plugin", "marketplace"],
        ["plugin", "list"],
        ["plugin", "install"],
    ]
    assert all(call[1]["env"]["CLAUDE_CONFIG_DIR"] == str(cowork_root) for call in calls)
    assert json.loads((app_dir / cowork.STATE_FILENAME).read_text())["cowork_root"] == str(
        cowork_root
    )


def test_configure_updates_installed_plugin(tmp_path, monkeypatch):
    cowork_root = tmp_path / "cowork"
    cowork_root.mkdir()
    monkeypatch.setattr(cowork.config_io, "APP_DIR", tmp_path / ".ucode")
    monkeypatch.setattr(cowork, "discover_cowork_root", lambda: cowork_root)
    monkeypatch.setattr(cowork.shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(cowork, "records_for_scope", lambda *args: [])
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        stdout = (
            json.dumps([{"pluginId": cowork.PLUGIN_ID}])
            if argv[1:4] == ["plugin", "list", "--cowork"]
            else '{"outcome":"ok"}'
        )
        if argv[1:3] == ["plugin", "validate"]:
            stdout = "valid"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)

    assert cowork.configure_cowork(
        {"workspace": "https://workspace.example", "profile": "chosen"},
        [{"name": "github", "url": "https://mcp.example", "clients": ["claude"]}],
    )

    assert calls[-1][1:3] == ["plugin", "update"]


def test_configure_records_root_before_plugin_command_failure(tmp_path, monkeypatch):
    app_dir = tmp_path / ".ucode"
    cowork_root = tmp_path / "cowork"
    cowork_root.mkdir()
    monkeypatch.setattr(cowork.config_io, "APP_DIR", app_dir)
    monkeypatch.setattr(cowork, "discover_cowork_root", lambda: cowork_root)
    monkeypatch.setattr(cowork, "records_for_scope", lambda *args: [])
    monkeypatch.setattr(
        cowork,
        "_cowork_mcp_config",
        lambda *args, **kwargs: {"mcpServers": {"github": {"type": "stdio"}}},
    )

    def fail_install(*args):
        raise RuntimeError("plugin command failed")

    monkeypatch.setattr(cowork, "_install_plugin", fail_install)

    with pytest.raises(RuntimeError, match="plugin command failed"):
        cowork.configure_cowork(
            {"workspace": "https://workspace.example", "profile": "chosen"},
            [{"name": "github"}],
        )

    assert json.loads((app_dir / cowork.STATE_FILENAME).read_text()) == {
        "version": 1,
        "cowork_root": str(cowork_root),
    }


def test_configure_requires_absolute_ug_binary(monkeypatch):
    monkeypatch.setattr(cowork, "build_mcp_proxy_argv", lambda *args, **kwargs: ["ug", "proxy"])

    with pytest.raises(RuntimeError, match="absolute `ug` executable"):
        cowork._cowork_mcp_config(
            [{"name": "github", "url": "https://mcp.example", "clients": ["claude"]}],
            "https://workspace.example",
            "chosen",
        )


def test_cowork_mcp_config_uses_saved_pat_mode(monkeypatch):
    monkeypatch.setattr(
        cowork,
        "build_mcp_proxy_argv",
        lambda *args, **kwargs: (
            ["/opt/bin/ug", "mcp-proxy", "--use-pat"]
            if kwargs["use_pat"]
            else ["/opt/bin/ug", "mcp-proxy"]
        ),
    )

    config = cowork._cowork_mcp_config(
        [{"name": "github", "url": "https://mcp.example", "clients": ["claude"]}],
        "https://workspace.example",
        "chosen",
        use_pat=True,
    )

    assert config["mcpServers"]["github"]["args"][-1] == "--use-pat"


def test_cowork_mcp_config_rejects_custom_oauth():
    with pytest.raises(RuntimeError, match="do not support custom OAuth"):
        cowork._cowork_mcp_config(
            [{"name": "github", "url": "https://mcp.example", "clients": ["claude"]}],
            "https://workspace.example",
            "chosen",
            custom_oauth=True,
        )


def test_empty_managed_content_removes_prior_plugin_without_discovery(monkeypatch):
    monkeypatch.setattr(cowork, "records_for_scope", lambda *args: [])
    monkeypatch.setattr(cowork, "revert_cowork", lambda: True)
    monkeypatch.setattr(
        cowork,
        "discover_cowork_root",
        lambda: pytest.fail("Cowork discovery should not run for empty content"),
    )

    assert not cowork.configure_cowork(
        {"workspace": "https://workspace.example", "profile": "chosen"}, []
    )


def test_revert_uses_saved_root_and_removes_owned_files(tmp_path, monkeypatch):
    app_dir = tmp_path / ".ucode"
    app_dir.mkdir(exist_ok=True)
    cowork_root = tmp_path / "cowork"
    marketplace = app_dir / cowork.MARKETPLACE_DIRECTORY_NAME
    marketplace.mkdir()
    (app_dir / cowork.STATE_FILENAME).write_text(
        json.dumps({"version": 1, "cowork_root": str(cowork_root)})
    )
    monkeypatch.setattr(cowork.config_io, "APP_DIR", app_dir)
    monkeypatch.setattr(cowork.shutil, "which", lambda name: f"/opt/bin/{name}")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"outcome":"ok"}', stderr="")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)

    assert cowork.revert_cowork()
    assert [call[0][1:3] for call in calls] == [
        ["plugin", "uninstall"],
        ["plugin", "marketplace"],
    ]
    assert all(call[1]["env"]["CLAUDE_CONFIG_DIR"] == str(cowork_root) for call in calls)
    assert not marketplace.exists()
    assert not (app_dir / cowork.STATE_FILENAME).exists()


def test_revert_accepts_idempotent_json_failure(tmp_path, monkeypatch):
    app_dir = tmp_path / ".ucode"
    app_dir.mkdir(exist_ok=True)
    (app_dir / cowork.STATE_FILENAME).write_text(
        json.dumps({"version": 1, "cowork_root": str(tmp_path / "cowork")})
    )
    monkeypatch.setattr(cowork.config_io, "APP_DIR", app_dir)
    monkeypatch.setattr(cowork.shutil, "which", lambda name: f"/opt/bin/{name}")

    def run(argv, **kwargs):
        message = "Plugin not installed" if argv[1:3] == ["plugin", "uninstall"] else "Not found"
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"outcome": "failed", "message": message}),
            stderr="",
        )

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)

    assert cowork.revert_cowork()
    assert not (app_dir / cowork.STATE_FILENAME).exists()
