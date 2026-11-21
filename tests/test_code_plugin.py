"""Code connector guidance with isolated native plugin CLI boundaries."""

import json
import shutil
from types import SimpleNamespace

import pytest

from ucode.agents.desktop import cowork
from ucode.agents.desktop import setup as desktop_setup


@pytest.fixture
def code_plugins(tmp_path, monkeypatch):
    calls = []
    installed = {"personal@other", cowork.PLUGIN_ID}
    config_root = tmp_path / "code-config"
    monkeypatch.setattr(cowork, "_code_config_root", lambda: config_root)
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(tmp_path / "bin" / name))

    def run(argv, **kwargs):
        assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(config_root)
        assert "--cowork" not in argv
        calls.append(argv[1:])
        stdout = '{"outcome":"ok"}'
        if argv[1:3] == ["plugin", "validate"]:
            stdout = "valid"
        elif argv[1:3] == ["plugin", "list"]:
            stdout = json.dumps([{"id": plugin_id} for plugin_id in sorted(installed)])
        elif argv[1:3] in (["plugin", "install"], ["plugin", "update"]):
            installed.add(argv[3])
        elif argv[1:3] == ["plugin", "uninstall"]:
            installed.discard(argv[3])
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)
    return calls, installed


def _server(name, *, clients=None):
    return {
        "name": name,
        "url": f"https://workspace.example/ai-gateway/mcp-services/{name}",
        "clients": ["claude"] if clients is None else clients,
    }


def _plugin_root():
    return cowork.config_io.APP_DIR / cowork.CODE_MARKETPLACE_DIRECTORY_NAME / "plugins" / "ug"


def test_code_plugin_uses_native_names_without_duplicate_servers_or_hooks(code_plugins):
    calls, installed = code_plugins

    assert cowork.configure_code(
        [
            _server("system-ai-slack"),
            _server("system-ai-slack"),
            _server("codex", clients=["codex"]),
        ]
    )

    root = _plugin_root()
    plugin = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["name"] == "ug"
    assert "version" not in plugin
    assert "mcpServers" not in plugin
    assert not (root / ".mcp.json").exists()
    assert not (root / "hooks").exists()
    assert sorted(path.name for path in (root / "skills").iterdir()) == [
        cowork.CONNECTOR_SKILL_NAME
    ]
    skill = (root / "skills" / cowork.CONNECTOR_SKILL_NAME / "SKILL.md").read_text()
    assert "`system-ai-slack`" in skill
    assert "`slack`" not in skill
    assert "`codex`" not in skill
    assert "wait 5 seconds" in skill
    assert "mcp login <registered-name>" in skill
    assert "CLAUDE_CONFIG_DIR" in skill
    assert calls[-1] == ["plugin", "install", cowork.CODE_PLUGIN_ID, "--json"]
    assert installed == {"personal@other", cowork.PLUGIN_ID, cowork.CODE_PLUGIN_ID}


def test_native_auth_guidance_is_scoped_to_code(code_plugins):
    cowork.configure_code([_server("system-ai-gmail")])
    code_skill = (_plugin_root() / "skills" / cowork.CONNECTOR_SKILL_NAME / "SKILL.md").read_text()
    cowork_skill_root = cowork.config_io.APP_DIR / "cowork-skills"
    cowork._write_connector_skill(cowork_skill_root, ["gmail"])
    cowork_skill = (cowork_skill_root / cowork.CONNECTOR_SKILL_NAME / "SKILL.md").read_text()

    assert "`system-ai-gmail`" in code_skill
    assert "mcp login <registered-name>" in code_skill
    assert "mcp login" not in cowork_skill
    assert "`gmail`" in cowork_skill


def test_code_plugin_refresh_replaces_names_and_removes_stale_guidance(code_plugins):
    calls, _ = code_plugins
    assert cowork.configure_code([_server("old-workspace-slack")])
    assert cowork.configure_code([_server("new-workspace-gmail")])
    skill = (_plugin_root() / "skills" / cowork.CONNECTOR_SKILL_NAME / "SKILL.md").read_text()
    assert "old-workspace-slack" not in skill
    assert "new-workspace-gmail" in skill
    assert calls[-1] == ["plugin", "update", cowork.CODE_PLUGIN_ID, "--json"]

    assert cowork.configure_code([])
    assert not (_plugin_root() / "skills").exists()
    assert (_plugin_root() / ".claude-plugin" / "plugin.json").exists()


def test_code_plugin_no_servers_does_not_install_or_create_files(code_plugins):
    calls, _ = code_plugins
    assert not cowork.configure_code([_server("codex-only", clients=["codex"])])
    assert not _plugin_root().parent.parent.exists()
    assert calls == []


def test_code_revert_preserves_cowork_and_other_plugins(code_plugins):
    calls, installed = code_plugins
    cowork.configure_code([_server("system-ai-slack")])

    assert cowork.revert_code()
    assert installed == {"personal@other", cowork.PLUGIN_ID}
    assert not _plugin_root().parent.parent.exists()
    assert calls[-2:] == [
        ["plugin", "uninstall", cowork.CODE_PLUGIN_ID, "--json"],
        ["plugin", "marketplace", "remove", cowork.CODE_MARKETPLACE_NAME, "--json"],
    ]
    count = len(calls)
    assert not cowork.revert_code()
    assert len(calls) == count


def test_code_revert_failure_retains_source_for_retry(code_plugins, monkeypatch):
    cowork.configure_code([_server("system-ai-slack")])

    def fail(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="removal failed")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", fail)
    with pytest.raises(RuntimeError, match="removal failed"):
        cowork.revert_code()
    assert _plugin_root().exists()


def test_code_revert_uses_saved_root_even_after_source_is_deleted(code_plugins, monkeypatch):
    cowork.configure_code([_server("system-ai-slack")])
    shutil.rmtree(_plugin_root().parent.parent)

    def wrong_root():
        pytest.fail("Cleanup must use the root recorded at installation")

    monkeypatch.setattr(cowork, "_code_config_root", wrong_root)
    assert cowork.revert_code()
    assert not (cowork.config_io.APP_DIR / cowork.CODE_STATE_FILENAME).exists()


def test_code_root_switch_removes_only_previous_code_registration(
    code_plugins, monkeypatch, tmp_path
):
    cowork.configure_code([_server("system-ai-slack")])
    new_root = tmp_path / "another-code-config"
    calls = []

    def run(argv, **kwargs):
        calls.append((argv[1:], kwargs["env"]["CLAUDE_CONFIG_DIR"]))
        stdout = "[]" if argv[1:3] == ["plugin", "list"] else '{"outcome":"ok"}'
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)
    monkeypatch.setattr(cowork, "_code_config_root", lambda: new_root)
    cowork.configure_code([_server("new-workspace-gmail")])

    assert calls[-2:] == [
        (["plugin", "uninstall", cowork.CODE_PLUGIN_ID, "--json"], str(tmp_path / "code-config")),
        (
            ["plugin", "marketplace", "remove", cowork.CODE_MARKETPLACE_NAME, "--json"],
            str(tmp_path / "code-config"),
        ),
    ]
    assert calls[-3] == (["plugin", "install", cowork.CODE_PLUGIN_ID, "--json"], str(new_root))
    assert all(root == str(new_root) for _, root in calls[:-2])
    assert all("--cowork" not in argv for argv, _ in calls)
    state_path = cowork.config_io.APP_DIR / cowork.CODE_STATE_FILENAME
    assert json.loads(state_path.read_text())["config_root"] == str(new_root)


def test_code_root_switch_install_failure_preserves_old_registration_and_retries(
    code_plugins, monkeypatch, tmp_path
):
    _, old_plugins = code_plugins
    cowork.configure_code([_server("system-ai-slack")])
    old_root = tmp_path / "code-config"
    new_root = tmp_path / "another-code-config"
    state_path = cowork.config_io.APP_DIR / cowork.CODE_STATE_FILENAME
    old_state = state_path.read_bytes()
    plugins = {str(old_root): old_plugins, str(new_root): {"personal@another"}}
    calls = []
    fail_install = True

    def run(argv, **kwargs):
        root = kwargs["env"]["CLAUDE_CONFIG_DIR"]
        calls.append((argv[1:], root))
        installed = plugins[root]
        stdout = '{"outcome":"ok"}'
        if argv[1:3] == ["plugin", "validate"]:
            stdout = "valid"
        elif argv[1:3] == ["plugin", "list"]:
            stdout = json.dumps([{"id": plugin_id} for plugin_id in sorted(installed)])
        elif argv[1:3] == ["plugin", "install"]:
            if fail_install:
                return SimpleNamespace(returncode=1, stdout="", stderr="installation failed")
            installed.add(argv[3])
        elif argv[1:3] == ["plugin", "uninstall"]:
            installed.discard(argv[3])
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)
    monkeypatch.setattr(cowork, "_code_config_root", lambda: new_root)

    with pytest.raises(RuntimeError, match="installation failed"):
        cowork.configure_code([_server("system-ai-slack")])

    assert plugins[str(old_root)] == {"personal@other", cowork.PLUGIN_ID, cowork.CODE_PLUGIN_ID}
    assert plugins[str(new_root)] == {"personal@another"}
    assert state_path.read_bytes() == old_state
    assert all(root == str(new_root) for _, root in calls)

    fail_install = False
    assert cowork.configure_code([_server("system-ai-slack")])
    assert plugins[str(old_root)] == {"personal@other", cowork.PLUGIN_ID}
    assert plugins[str(new_root)] == {"personal@another", cowork.CODE_PLUGIN_ID}
    assert json.loads(state_path.read_text())["config_root"] == str(new_root)

    assert cowork.revert_code()
    assert plugins[str(new_root)] == {"personal@another"}
    assert plugins[str(old_root)] == {"personal@other", cowork.PLUGIN_ID}
    assert not state_path.exists()


def test_code_setup_is_best_effort_and_dry_run_does_not_install(code_plugins, monkeypatch, capsys):
    calls, _ = code_plugins
    monkeypatch.setattr(desktop_setup, "is_dry_run", lambda: True)
    desktop_setup.configure_claude_code_plugin([_server("system-ai-slack")])
    assert calls == []
    assert "Would configure Claude Code" in capsys.readouterr().out

    monkeypatch.setattr(desktop_setup, "is_dry_run", lambda: False)
    monkeypatch.setattr(cowork.shutil, "which", lambda name: None)
    desktop_setup.configure_claude_code_plugin([_server("system-ai-slack")])
    assert "Could not configure Claude Code connector plugin" in capsys.readouterr().out


def test_code_plugin_revert_runs_without_desktop_or_cowork(code_plugins, monkeypatch):
    cowork.configure_code([_server("system-ai-slack")])
    monkeypatch.setattr(desktop_setup, "claude_desktop_directory", lambda: None)
    monkeypatch.setattr(desktop_setup, "revert_claude_cowork", lambda: "unchanged")

    assert desktop_setup.revert_desktop() == "removed"
    assert not _plugin_root().exists()


def test_cowork_failure_does_not_prevent_code_plugin_setup(code_plugins, monkeypatch, capsys):
    from ucode import cli

    def fail(*args, **kwargs):
        raise RuntimeError("Cowork has not started")

    monkeypatch.setattr(desktop_setup, "configure_cowork", fail)
    cli._sync_claude_cowork_plugin(
        {
            "available_tools": ["claude"],
            "mcp_servers": [_server("system-ai-slack")],
        },
        managed_mcp_servers=[],
    )

    assert _plugin_root().exists()
    assert "Cowork has not started" in capsys.readouterr().out
