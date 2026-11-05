"""Claude Cowork plugin generation and scoped CLI installation."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucode.agents.desktop import claude as desktop
from ucode.agents.desktop import cowork


@pytest.fixture(autouse=True)
def desktop_profile(tmp_path, monkeypatch):
    directory = tmp_path / "desktop" / "configLibrary"
    directory.mkdir(parents=True)
    monkeypatch.setattr(desktop, "claude_desktop_directory", lambda: directory)
    result = desktop.configure_claude_desktop(
        model_ids=["system.ai.claude-sonnet-5"],
        base_url="https://workspace.example/ai-gateway/anthropic",
        helper_command=tmp_path / "ug",
        helper_args=["claude-desktop-auth", "--profile", "chosen"],
    )
    return result.profile_path


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
        timestamp = 1_900_000_000_000_000_000 + index * 1_000_000_000
        os.utime(marker, ns=(timestamp, timestamp))
        os.utime(root, ns=(timestamp, timestamp))

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


def test_configure_generates_plugin_and_installs_in_cowork_scope(
    tmp_path, monkeypatch, desktop_profile
):
    app_dir = tmp_path / ".ucode"
    cowork_root = tmp_path / "Claude-3p" / "local-agent-mode-sessions" / "acct" / "org"
    cowork_root.mkdir(parents=True)
    claude_skill = tmp_path / "home" / ".claude" / "skills" / "review"
    agent_skill = tmp_path / "home" / ".agents" / "skills" / "review"
    claude_skill.mkdir(parents=True)
    agent_skill.mkdir(parents=True)
    (claude_skill / "SKILL.md").write_text("managed skill")
    (agent_skill / "SKILL.md").write_text("other copy")
    ug_binary = tmp_path / "bin" / "ug"
    monkeypatch.setattr(cowork.config_io, "APP_DIR", app_dir)
    monkeypatch.setattr(cowork, "discover_cowork_root", lambda: cowork_root)
    monkeypatch.setattr(cowork, "_cowork_sessions_root_exists", lambda: True)
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(ug_binary.parent / name))
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
    assert (plugin["name"], plugin["displayName"]) == ("ug", "ug")
    marketplace = json.loads(
        (
            app_dir / cowork.MARKETPLACE_DIRECTORY_NAME / ".claude-plugin" / "marketplace.json"
        ).read_text()
    )
    entry = marketplace["plugins"][0]
    assert (entry["name"], entry["displayName"]) == ("ug", "ug")
    assert "mcpServers" not in plugin
    assert not (plugin_root / ".mcp.json").exists()
    servers = json.loads(desktop_profile.read_text())["managedMcpServers"]
    assert servers == [
        {
            "name": "github",
            "transport": "stdio",
            "command": str(ug_binary),
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
    ]
    assert (plugin_root / "skills" / "review" / "SKILL.md").read_text() == "managed skill"
    assert not (plugin_root / "skills" / "foreign").exists()
    connector_skill = (
        plugin_root / "skills" / cowork.CONNECTOR_SKILL_NAME / "SKILL.md"
    ).read_text()
    assert connector_skill.startswith(f'---\nname: {cowork.CONNECTOR_SKILL_NAME}\ndescription: "')
    assert all(f"`{server['name']}`" in connector_skill for server in servers)
    assert "codex-only" not in connector_skill
    description_line = connector_skill.splitlines()[2]
    description = json.loads(description_line.removeprefix("description: "))
    assert len(description) <= 1024
    assert "sign-in recovery" in description
    assert "`authenticate` is the recovery action" in connector_skill
    assert "task in the same turn" in connector_skill
    assert "Do not sleep or poll for startup" in connector_skill
    assert "Seeing only `authenticate` does not establish that sign-in is needed" in connector_skill
    assert "does not guarantee that every connector is ready" in connector_skill
    assert "ask the user to try the request again shortly" in connector_skill
    hooks = json.loads((plugin_root / "hooks" / "hooks.json").read_text())
    assert set(hooks["hooks"]) == {"SessionStart", "UserPromptSubmit"}
    expected_command = cowork.subprocess_cross_os.shell_command(
        [
            str(ug_binary),
            "cowork-hook",
            "--host",
            "https://workspace.example",
            "--profile",
            "chosen",
            "--server",
            "github=https://workspace.example/api/2.0/mcp/github",
        ]
    )
    for entries in hooks["hooks"].values():
        hook = entries[0]["hooks"][0]
        assert (hook["type"], hook["command"]) == ("command", expected_command)
        assert hook["timeout"] <= 30
    assert [call[0][1:3] for call in calls] == [
        ["plugin", "validate"],
        ["plugin", "marketplace"],
        ["plugin", "marketplace"],
        ["plugin", "list"],
        ["plugin", "install"],
    ]
    assert all(call[1]["env"]["CLAUDE_CONFIG_DIR"] == str(cowork_root) for call in calls)
    assert calls[-1][0][1:] == [
        "plugin",
        "install",
        "ug@unity-gateway-cowork",
        "--cowork",
        "--json",
    ]
    assert json.loads((app_dir / cowork.STATE_FILENAME).read_text())["cowork_root"] == str(
        cowork_root
    )


def test_configure_updates_installed_plugin(tmp_path, monkeypatch, desktop_profile):
    cowork_root = tmp_path / "cowork"
    cowork_root.mkdir()
    monkeypatch.setattr(cowork.config_io, "APP_DIR", tmp_path / ".ucode")
    monkeypatch.setattr(cowork, "discover_cowork_root", lambda: cowork_root)
    monkeypatch.setattr(cowork, "_cowork_sessions_root_exists", lambda: True)
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(bin_dir / name))
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

    plugin_root = (
        tmp_path / ".ucode" / cowork.MARKETPLACE_DIRECTORY_NAME / "plugins" / cowork.PLUGIN_NAME
    )
    plugin_root.mkdir(parents=True)
    mcp_path = plugin_root / ".mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {"system-ai-github": {"type": "stdio"}}}))
    for _ in range(2):
        assert cowork.configure_cowork(
            {"workspace": "https://workspace.example", "profile": "chosen"},
            [
                {
                    "name": "system-ai-github",
                    "url": "https://workspace.example/ai-gateway/mcp-services/system.ai.github",
                    "clients": ["claude"],
                }
            ],
        )
        assert not mcp_path.exists()
        assert [
            entry["name"] for entry in json.loads(desktop_profile.read_text())["managedMcpServers"]
        ] == ["github"]

    assert calls[-1][1:3] == ["plugin", "update"]
    assert all(argv[1:3] != ["plugin", "uninstall"] for argv in calls)


@pytest.mark.parametrize(
    "legacy_entry",
    [
        cowork.LEGACY_PLUGIN_ID,
        {"pluginId": cowork.LEGACY_PLUGIN_ID},
        {"id": cowork.LEGACY_PLUGIN_ID},
        {"name": "unity-gateway", "marketplace": cowork.MARKETPLACE_NAME},
    ],
)
@pytest.mark.parametrize("already_installed", [False, True])
def test_install_replaces_legacy_registration_after_success(
    tmp_path, monkeypatch, legacy_entry, already_installed
):
    cowork_root = tmp_path / "cowork"
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(tmp_path / "bin" / name))
    installed = {cowork.LEGACY_PLUGIN_ID, "other@unrelated"}
    if already_installed:
        installed.add(cowork.PLUGIN_ID)
    calls = []

    # Keep the external Claude CLI isolated from the developer's plugin registrations.
    def run(argv, **kwargs):
        assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(cowork_root)
        calls.append(argv[1:])
        stdout = '{"outcome":"ok"}'
        if argv[1:3] == ["plugin", "validate"]:
            stdout = "valid"
        elif argv[1:3] == ["plugin", "list"]:
            entries = [legacy_entry, {"id": "other@unrelated"}]
            if already_installed:
                entries.append({"pluginId": cowork.PLUGIN_ID})
            stdout = json.dumps({"plugins": entries})
        elif argv[1:3] in (["plugin", "install"], ["plugin", "update"]):
            installed.add(argv[3])
        elif argv[1:3] == ["plugin", "uninstall"]:
            assert cowork.PLUGIN_ID in installed
            installed.remove(argv[3])
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)

    cowork._install_plugin(cowork_root, tmp_path / "marketplace")

    assert installed == {"ug@unity-gateway-cowork", "other@unrelated"}
    assert calls[-2:] == [
        [
            "plugin",
            "update" if already_installed else "install",
            cowork.PLUGIN_ID,
            "--cowork",
            "--json",
        ],
        ["plugin", "uninstall", cowork.LEGACY_PLUGIN_ID, "--cowork", "--json"],
    ]


def test_install_failure_preserves_legacy_registration(tmp_path, monkeypatch):
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(tmp_path / "bin" / name))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[1:])
        stdout = '{"outcome":"ok"}'
        if argv[1:3] == ["plugin", "validate"]:
            stdout = "valid"
        elif argv[1:3] == ["plugin", "list"]:
            stdout = json.dumps([{"pluginId": cowork.LEGACY_PLUGIN_ID}])
        elif argv[1:3] == ["plugin", "install"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="installation failed")
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)

    with pytest.raises(RuntimeError, match="installation failed"):
        cowork._install_plugin(tmp_path / "cowork", tmp_path / "marketplace")

    assert calls[-1] == ["plugin", "install", cowork.PLUGIN_ID, "--cowork", "--json"]
    assert all(argv[:2] != ["plugin", "uninstall"] for argv in calls)


def test_cowork_mcp_config_shortens_uc_names_and_preserves_routing(tmp_path, monkeypatch):
    ug_binary = tmp_path / "bin" / "ug"
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(ug_binary.parent / name))
    services = ["slack", "gmail", "github", "atlassian", "sales-tools"]
    servers = [
        {
            "name": f"system-ai-{service}",
            "url": f"https://workspace.example/ai-gateway/mcp-services/system.ai.{service}",
            "clients": ["claude"],
        }
        for service in services
    ]
    servers.append(
        {
            "name": "main-team-slack",
            "url": "https://workspace.example/ai-gateway/mcp-services/main.team.slack",
            "clients": ["codex"],
        }
    )

    config = cowork._cowork_mcp_config(servers, "https://workspace.example", "chosen")

    assert list(config["mcpServers"]) == services
    for service, server in zip(services, servers, strict=False):
        assert config["mcpServers"][service] == {
            "type": "stdio",
            "command": str(ug_binary),
            "args": [
                "mcp-proxy",
                "--url",
                server["url"],
                "--host",
                "https://workspace.example",
                "--profile",
                "chosen",
            ],
        }


@pytest.mark.parametrize(
    ("name", "url"),
    [
        ("work-slack", "https://workspace.example/ai-gateway/mcp-services/system.ai.slack"),
        ("system-ai-slack", "https://external.example/mcp"),
        ("system-slack", "https://workspace.example/ai-gateway/mcp-services/system.slack"),
    ],
)
def test_cowork_mcp_config_preserves_custom_names(tmp_path, monkeypatch, name, url):
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(tmp_path / "bin" / name))

    config = cowork._cowork_mcp_config(
        [{"name": name, "url": url, "clients": ["claude"]}],
        "https://workspace.example",
        "chosen",
    )

    assert list(config["mcpServers"]) == [name]


def test_cowork_mcp_config_disambiguates_names_independently_of_order(tmp_path, monkeypatch):
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(tmp_path / "bin" / name))
    servers = [
        {
            "name": "system-ai-slack",
            "url": "https://workspace.example/ai-gateway/mcp-services/system.ai.slack",
            "clients": ["claude"],
        },
        {
            "name": "main-team-slack",
            "url": "https://workspace.example/ai-gateway/mcp-services/main.team.slack",
            "clients": ["claude"],
        },
        {"name": "slack", "url": "https://custom.example/mcp", "clients": ["claude"]},
        {
            "name": "slack-system-ai",
            "url": "https://other.example/mcp",
            "clients": ["claude"],
        },
    ]
    expected_urls = {
        "slack-system-ai-2": servers[0]["url"],
        "slack-main-team": servers[1]["url"],
        "slack": servers[2]["url"],
        "slack-system-ai": servers[3]["url"],
    }

    for ordered in [servers, list(reversed(servers))]:
        config = cowork._cowork_mcp_config(ordered, "https://workspace.example", "chosen")
        assert {
            name: entry["args"][entry["args"].index("--url") + 1]
            for name, entry in config["mcpServers"].items()
        } == expected_urls


def test_configure_records_root_before_plugin_command_failure(
    tmp_path, monkeypatch, desktop_profile
):
    app_dir = tmp_path / ".ucode"
    cowork_root = tmp_path / "cowork"
    cowork_root.mkdir()
    monkeypatch.setattr(cowork.config_io, "APP_DIR", app_dir)
    monkeypatch.setattr(cowork, "discover_cowork_root", lambda: cowork_root)
    monkeypatch.setattr(cowork, "_cowork_sessions_root_exists", lambda: True)
    monkeypatch.setattr(cowork, "records_for_scope", lambda *args: [])
    monkeypatch.setattr(
        cowork,
        "_cowork_mcp_config",
        lambda *args, **kwargs: {"mcpServers": {"github": {"type": "stdio"}}},
    )

    def fail_install(*args):
        raise RuntimeError("plugin command failed")

    monkeypatch.setattr(cowork, "_install_plugin", fail_install)
    profile_before = desktop_profile.read_bytes()

    with pytest.raises(RuntimeError, match="plugin command failed"):
        cowork.configure_cowork(
            {"workspace": "https://workspace.example", "profile": "chosen"},
            [{"name": "github"}],
        )

    assert json.loads((app_dir / cowork.STATE_FILENAME).read_text()) == {
        "version": 1,
        "cowork_root": str(cowork_root),
    }
    assert desktop_profile.read_bytes() == profile_before


def test_native_connectors_do_not_require_a_cowork_session(monkeypatch, desktop_profile, tmp_path):
    monkeypatch.setattr(cowork, "records_for_scope", lambda *args: [])
    monkeypatch.setattr(cowork, "_cowork_sessions_root_exists", lambda: False)
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(tmp_path / "bin" / name))
    monkeypatch.setattr(cowork, "discover_cowork_root", lambda: pytest.fail("No Cowork session"))
    state = {"workspace": "https://workspace.example", "profile": "chosen"}
    servers = [{"name": "github", "url": "https://mcp.example", "clients": ["claude"]}]

    assert not cowork.configure_cowork(state, servers)
    profile = json.loads(desktop_profile.read_text())
    assert [server["name"] for server in profile["managedMcpServers"]] == ["github"]
    assert profile["toolSearchEnabled"] is True

    assert not cowork.configure_cowork(state, [])
    assert json.loads(desktop_profile.read_text())["managedMcpServers"] == []


@pytest.mark.parametrize("invalid_profile", [None, "malformed", "other-workspace"])
def test_native_connector_preflight_preserves_existing_plugin(
    invalid_profile, monkeypatch, desktop_profile, tmp_path
):
    if invalid_profile is None:
        desktop_profile.unlink()
    elif invalid_profile == "malformed":
        desktop_profile.write_text("{")
    else:
        profile = json.loads(desktop_profile.read_text())
        profile["inferenceGatewayBaseUrl"] = "https://other.example/ai-gateway/anthropic"
        desktop_profile.write_text(json.dumps(profile))
    before = desktop_profile.read_bytes() if desktop_profile.exists() else None
    monkeypatch.setattr(cowork, "records_for_scope", lambda *args: [])
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(tmp_path / "bin" / name))
    monkeypatch.setattr(
        cowork, "_write_marketplace", lambda *args, **kwargs: pytest.fail("Must preserve plugin")
    )

    with pytest.raises(desktop.ClaudeDesktopError):
        cowork.configure_cowork(
            {"workspace": "https://workspace.example", "profile": "chosen"},
            [{"name": "github", "url": "https://mcp.example", "clients": ["claude"]}],
        )
    assert (desktop_profile.read_bytes() if desktop_profile.exists() else None) == before


def test_native_connectors_preserve_workspace_selection(monkeypatch, desktop_profile, tmp_path):
    from ucode.workspace import pin, selector_headers

    workspace = "https://workspace.example"
    pin(workspace, "123")
    profile = json.loads(desktop_profile.read_text())
    profile["inferenceCustomHeaders"] = selector_headers("123")
    desktop_profile.write_text(json.dumps(profile))
    monkeypatch.setattr(cowork, "records_for_scope", lambda *args: [])
    monkeypatch.setattr(cowork, "_cowork_sessions_root_exists", lambda: False)
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(tmp_path / "bin" / name))
    state = {"workspace": workspace, "profile": "chosen", "workspace_id": "123"}
    servers = [{"name": "github", "url": f"{workspace}/mcp/github", "clients": ["claude"]}]

    cowork.configure_cowork(state, servers)

    profile = json.loads(desktop_profile.read_text())
    args = profile["managedMcpServers"][0]["args"]
    assert args[args.index("--workspace-id") + 1] == "123"
    before = desktop_profile.read_bytes()
    pin(workspace, "456")
    with pytest.raises(desktop.ClaudeDesktopConflictError, match="another workspace"):
        cowork.configure_cowork({**state, "workspace_id": "456"}, servers)
    assert desktop_profile.read_bytes() == before


def test_configure_removes_registration_from_previous_cowork_root(tmp_path, monkeypatch):
    app_dir = tmp_path / ".ucode"
    app_dir.mkdir(exist_ok=True)
    old_root = tmp_path / "old-cowork"
    new_root = tmp_path / "new-cowork"
    (app_dir / cowork.STATE_FILENAME).write_text(
        json.dumps({"version": 1, "cowork_root": str(old_root)})
    )
    removed_roots = []
    monkeypatch.setattr(cowork.config_io, "APP_DIR", app_dir)
    monkeypatch.setattr(cowork, "discover_cowork_root", lambda: new_root)
    monkeypatch.setattr(cowork, "records_for_scope", lambda *args: [])
    monkeypatch.setattr(
        cowork,
        "_cowork_mcp_config",
        lambda *args, **kwargs: {"mcpServers": {"github": {"type": "stdio"}}},
    )
    monkeypatch.setattr(cowork, "_remove_plugin_registration", removed_roots.append)
    monkeypatch.setattr(cowork, "_write_marketplace", lambda *args, **kwargs: None)
    monkeypatch.setattr(cowork, "_install_plugin", lambda *args: None)

    assert cowork.configure_cowork(
        {"workspace": "https://workspace.example", "profile": "chosen"},
        [{"name": "github"}],
    )

    assert removed_roots == [old_root]
    assert json.loads((app_dir / cowork.STATE_FILENAME).read_text())["cowork_root"] == str(new_root)


def test_configure_requires_absolute_ug_binary(monkeypatch):
    monkeypatch.setattr(cowork, "build_mcp_proxy_argv", lambda *args, **kwargs: ["ug", "proxy"])

    with pytest.raises(RuntimeError, match="absolute `ug` executable"):
        cowork._cowork_mcp_config(
            [{"name": "github", "url": "https://mcp.example", "clients": ["claude"]}],
            "https://workspace.example",
            "chosen",
        )


def test_cowork_mcp_config_uses_saved_pat_mode(tmp_path, monkeypatch):
    ug_binary = tmp_path / "bin" / "ug"
    monkeypatch.setattr(
        cowork,
        "build_mcp_proxy_argv",
        lambda *args, **kwargs: (
            [str(ug_binary), "mcp-proxy", "--use-pat"]
            if kwargs["use_pat"]
            else [str(ug_binary), "mcp-proxy"]
        ),
    )

    config = cowork._cowork_mcp_config(
        [{"name": "github", "url": "https://mcp.example", "clients": ["claude"]}],
        "https://workspace.example",
        "chosen",
        use_pat=True,
    )

    assert config["mcpServers"]["github"]["args"][-1] == "--use-pat"


def test_cowork_mcp_config_skips_custom_oauth(capsys):
    config = cowork._cowork_mcp_config(
        [{"name": "github", "url": "https://mcp.example", "clients": ["claude"]}],
        "https://workspace.example",
        "chosen",
        custom_oauth=True,
    )

    assert config == {"mcpServers": {}}
    assert "Skipping Cowork MCP server `github`" in capsys.readouterr().out


def test_empty_content_without_prior_plugin_or_cowork_sessions_skips_silently(
    tmp_path, monkeypatch
):
    app_dir = tmp_path / ".ucode"
    monkeypatch.setattr(cowork.config_io, "APP_DIR", app_dir)
    monkeypatch.setattr(cowork, "records_for_scope", lambda *args: [])
    monkeypatch.setattr(cowork, "claude_desktop_root_directory", lambda: tmp_path / "Claude-3p")
    monkeypatch.setattr(
        cowork,
        "discover_cowork_root",
        lambda: pytest.fail("Cowork discovery should not run for empty content"),
    )

    assert not cowork.configure_cowork({"workspace": "https://workspace.example"}, [])


def test_empty_content_replaces_prior_plugin(tmp_path, monkeypatch):
    app_dir = tmp_path / ".ucode"
    app_dir.mkdir(exist_ok=True)
    cowork_root = tmp_path / "cowork"
    state_path = app_dir / cowork.STATE_FILENAME
    state_path.write_text(json.dumps({"version": 1, "cowork_root": str(cowork_root)}))
    monkeypatch.setattr(cowork.config_io, "APP_DIR", app_dir)
    monkeypatch.setattr(cowork, "records_for_scope", lambda *args: [])
    monkeypatch.setattr(cowork, "discover_cowork_root", lambda: cowork_root)
    writes = []
    installs = []
    monkeypatch.setattr(cowork, "_write_marketplace", lambda *args, **kwargs: writes.append(args))
    monkeypatch.setattr(cowork, "_install_plugin", lambda *args: installs.append(args))

    assert cowork.configure_cowork(
        {"workspace": "https://workspace.example", "profile": "chosen"}, []
    )

    assert writes[0][3] == []
    assert installs[0][0] == cowork_root


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
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(bin_dir / name))
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"outcome":"ok"}', stderr="")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)

    assert cowork.revert_cowork()
    assert [call[0][1:3] for call in calls] == [
        ["plugin", "uninstall"],
        ["plugin", "uninstall"],
        ["plugin", "marketplace"],
    ]
    assert [call[0][3] for call in calls[:2]] == [
        "ug@unity-gateway-cowork",
        "unity-gateway@unity-gateway-cowork",
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
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(bin_dir / name))

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


def test_revert_removes_orphaned_registration_without_saved_state(tmp_path, monkeypatch):
    app_dir = tmp_path / ".ucode"
    desktop_root = tmp_path / "Claude-3p"
    unrelated_root = _cowork_root(desktop_root, "account-a", "org-a")
    orphaned_root = _cowork_root(desktop_root, "account-b", "org-b")
    plugins_dir = orphaned_root / "cowork_plugins"
    plugins_dir.mkdir()
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps({cowork.PLUGIN_ID: {"installPath": "/missing/source"}})
    )
    monkeypatch.setattr(cowork.config_io, "APP_DIR", app_dir)
    monkeypatch.setattr(cowork, "claude_desktop_root_directory", lambda: desktop_root)
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(bin_dir / name))
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"outcome":"ok"}', stderr="")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)

    assert cowork.revert_cowork()
    assert len(calls) == 3
    assert all(call[1]["env"]["CLAUDE_CONFIG_DIR"] == str(orphaned_root) for call in calls)
    assert all(call[1]["env"]["CLAUDE_CONFIG_DIR"] != str(unrelated_root) for call in calls)


def test_revert_retains_owned_files_when_claude_cli_is_missing(tmp_path, monkeypatch):
    app_dir = tmp_path / ".ucode"
    app_dir.mkdir(exist_ok=True)
    marketplace = app_dir / cowork.MARKETPLACE_DIRECTORY_NAME
    marketplace.mkdir()
    state_path = app_dir / cowork.STATE_FILENAME
    state_path.write_text(json.dumps({"version": 1, "cowork_root": str(tmp_path / "cowork")}))
    monkeypatch.setattr(cowork.config_io, "APP_DIR", app_dir)
    monkeypatch.setattr(cowork.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="Claude CLI is required"):
        cowork.revert_cowork()

    assert marketplace.exists()
    assert state_path.exists()


@pytest.mark.parametrize("options", [{}, {"code": False}], ids=["default", "explicit-cowork"])
def test_cowork_connector_skill_matches_guidance_fixture(tmp_path, options):
    """Both Cowork scopes retain discovery, sign-in recovery, and the retry-later fallback."""
    cowork._write_connector_skill(tmp_path, ["slack", "gmail"], **options)
    actual = (tmp_path / cowork.CONNECTOR_SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")
    expected = (Path(__file__).parent / "fixtures" / "cowork_connector_skill.md").read_text(
        encoding="utf-8"
    )
    assert actual == expected


@pytest.mark.parametrize("code", [False, True])
def test_connector_skill_description_is_bounded_with_many_connectors(tmp_path, code):
    names = [f"connector_{i:03}_with_a_long_service_name" for i in range(100)]
    cowork._write_connector_skill(tmp_path, names, code=code)
    text = (tmp_path / cowork.CONNECTOR_SKILL_NAME / "SKILL.md").read_text()
    description = json.loads(text.splitlines()[2].removeprefix("description: "))
    assert len(description) <= 1024
    assert all(f"`{name}`" in text.split("---\n", 2)[2] for name in names)
