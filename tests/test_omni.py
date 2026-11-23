"""Omnigent configuration, launch, and revert component coverage."""

import os
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ucode.cli import app


@pytest.fixture(autouse=True)
def configured_launcher():
    with patch(
        "ucode.cli.load_state",
        return_value={"workspace": "https://example.databricks.com", "available_tools": ["omni"]},
    ):
        yield


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--help"],
        ["--version"],
        ["--debug", "run", "--harness", "codex", "-p", "hello world"],
        ["codex", "--", "--help"],
        ["--", "--help"],
        ["claude", "--", "-p", 'keep "quotes" & pipes | and $HOME literal\nnext line'],
        ["codex", "--model=example-model", "", "--", "--", "-p"],
    ],
)
def test_omni_forwards_arguments(args):
    with (
        patch("ucode.agents.omni.shutil.which", return_value="/path with spaces/omnigent"),
        patch("ucode.agents.omni.exec_or_spawn") as launch,
        patch("ucode.cli._launch_tool") as configure_agent,
    ):
        result = CliRunner().invoke(app, ["omni", *args])
    assert result.exit_code == 0, result.output
    launch.assert_called_once_with(["/path with spaces/omnigent", *args])
    configure_agent.assert_not_called()


def test_omni_missing_install():
    with (
        patch("ucode.agents.omni.shutil.which", return_value=None),
        patch("ucode.agents.omni.exec_or_spawn") as launch,
    ):
        result = CliRunner().invoke(app, ["omni"])
    assert result.exit_code == 1
    assert "configure --agents omni" in result.output
    launch.assert_not_called()


@pytest.mark.parametrize("error", [PermissionError("denied"), FileNotFoundError("removed")])
def test_omni_launch_failure(error):
    with (
        patch("ucode.agents.omni.shutil.which", return_value="/bin/omnigent"),
        patch("ucode.agents.omni.exec_or_spawn", side_effect=error),
    ):
        result = CliRunner().invoke(app, ["omni"])
    assert result.exit_code == 1
    assert "Could not launch Omnigent" in result.output


def test_omni_propagates_child_exit_status():
    with (
        patch("ucode.agents.omni.shutil.which", return_value="/bin/omnigent"),
        patch("ucode.agents.omni.exec_or_spawn", side_effect=SystemExit(42)),
    ):
        result = CliRunner().invoke(app, ["omni"])
    assert result.exit_code == 42
    assert "Could not launch" not in result.output


def test_omni_preserves_session_environment_and_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMNIGENT_CONFIG", "/example/config.yaml")
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "example-profile")

    def handoff(argv):
        assert os.getcwd() == str(tmp_path)
        assert os.environ["OMNIGENT_CONFIG"] == "/example/config.yaml"
        assert os.environ["DATABRICKS_CONFIG_PROFILE"] == "example-profile"

    with (
        patch("ucode.agents.omni.shutil.which", return_value="/bin/omnigent"),
        patch("ucode.agents.omni.exec_or_spawn", side_effect=handoff) as launch,
    ):
        result = CliRunner().invoke(app, ["omni"])
    assert result.exit_code == 0, result.output
    launch.assert_called_once()


def test_top_level_help_lists_omni_without_an_installation():
    with patch("ucode.agents.omni.shutil.which", return_value=None):
        result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "omni" in result.output
    assert "Launch Omnigent" in result.output


@pytest.fixture
def omni_config(monkeypatch, tmp_path):
    from ucode.agents import omni

    monkeypatch.setattr(omni, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(omni, "BACKUP_PATH", tmp_path / "backup.yaml")
    monkeypatch.setattr("ucode.workspace.workspace_headers", lambda _: {})
    with patch("ucode.agents.install_tool_binary", return_value=True) as install:
        yield (
            omni,
            install,
            {
                "workspace": "https://example.databricks.com",
                "profile": "first-workspace",
                "claude_models": {"sonnet": "system.ai.claude-sonnet"},
                "codex_models": ["system.ai.gpt-example"],
            },
        )


def test_configure_omni_updates_provider_preserves_settings_and_registers_native_clients(
    omni_config,
):
    import yaml

    omni, install, state = omni_config
    original = "host:\n  name: my-host\nproviders:\n  personal:\n    kind: subscription\n    cli: claude\n    default: true\n"
    omni.CONFIG_PATH.write_text(original)
    state["mcp_clients"] = ["cursor"]
    result = omni.write_tool_config(state)
    config = yaml.safe_load(omni.CONFIG_PATH.read_text())
    assert config["host"] == {"name": "my-host"}
    assert config["providers"]["personal"] == {
        "kind": "subscription",
        "cli": "claude",
        "default": False,
    }
    assert config["providers"]["unity-gateway"] == {
        "kind": "databricks",
        "profile": "first-workspace",
        "default": ["anthropic", "openai"],
    }
    assert result["mcp_clients"] == ["claude", "codex", "cursor"]
    assert [call.args[0] for call in install.call_args_list] == ["omni", "claude", "codex"]
    assert omni.BACKUP_PATH.read_text() == original
    state["profile"] = "second-workspace"
    omni.write_tool_config(state)
    assert (
        yaml.safe_load(omni.CONFIG_PATH.read_text())["providers"]["unity-gateway"]["profile"]
        == "second-workspace"
    )
    assert omni.BACKUP_PATH.read_text() == original


@pytest.mark.parametrize(
    ("existing_workspace", "args"),
    [
        (False, []),
        (True, ["start"]),
        (True, ["run", "--harness", "codex", "-p", "hello world"]),
        (True, ["claude", "--", "-p", "hello world"]),
        (True, ["codex", "--", "--model=example-model"]),
    ],
)
def test_omni_first_launch_configures_before_handoff(omni_config, existing_workspace, args):
    import yaml

    from ucode.state import load_state, save_state

    omni, _, state = omni_config
    if existing_workspace:
        state["available_tools"] = ["codex"]
        save_state(state)

    def handoff(argv):
        config = yaml.safe_load(omni.CONFIG_PATH.read_text())
        assert config["providers"]["unity-gateway"]["profile"] == state["profile"]
        persisted = load_state()
        assert "omni" in persisted["available_tools"]
        assert persisted["mcp_clients"] == ["claude", "codex"]
        if existing_workspace:
            assert "codex" in persisted["available_tools"]
        assert argv == ["/bin/omnigent", *args]

    with (
        patch("ucode.cli.load_state", side_effect=load_state),
        patch("ucode.cli.ensure_bootstrap_dependencies") as bootstrap,
        patch(
            "ucode.cli._prompt_for_configuration",
            return_value=(state["workspace"], state["profile"]),
        ) as prompt,
        patch("ucode.cli.configure_shared_state", return_value=state) as discover,
        patch.object(omni.shutil, "which", return_value="/bin/omnigent"),
        patch.object(omni, "exec_or_spawn", side_effect=handoff) as launch,
        patch("ucode.skills_download.refresh_downloaded_skills_on_launch"),
    ):
        result = CliRunner().invoke(app, ["omni", *args])

    assert result.exit_code == 0, result.output
    bootstrap.assert_called_once_with("omni")
    discover.assert_called_once_with(state["workspace"], profile=state["profile"], tools=["omni"])
    launch.assert_called_once()
    if existing_workspace:
        prompt.assert_not_called()
    else:
        prompt.assert_called_once_with("omni")


@pytest.mark.parametrize("failure", ["bootstrap", "configure"])
def test_omni_first_launch_setup_failure_does_not_start_agent(failure):
    with (
        patch("ucode.cli.load_state", return_value={}),
        patch("ucode.cli.ensure_bootstrap_dependencies") as bootstrap,
        patch("ucode.cli._auto_configure_tool") as configure,
        patch("ucode.agents.omni.exec_or_spawn") as launch,
    ):
        failing_step = bootstrap if failure == "bootstrap" else configure
        failing_step.side_effect = RuntimeError("Omni setup failed")
        result = CliRunner().invoke(app, ["omni"])

    assert result.exit_code == 1
    assert "Omni setup failed" in result.output
    launch.assert_not_called()
    if failure == "bootstrap":
        configure.assert_not_called()


@pytest.mark.parametrize("args", [["--help"], ["stop"]])
def test_omni_auxiliary_commands_do_not_bootstrap_an_unconfigured_workspace(args):
    with (
        patch("ucode.cli.load_state", return_value={}),
        patch("ucode.cli.ensure_bootstrap_dependencies") as bootstrap,
        patch("ucode.cli._auto_configure_tool") as configure,
        patch("ucode.agents.omni.shutil.which", return_value="/bin/omnigent"),
        patch("ucode.agents.omni.exec_or_spawn") as launch,
    ):
        result = CliRunner().invoke(app, ["omni", *args])

    assert result.exit_code == 0, result.output
    bootstrap.assert_not_called()
    configure.assert_not_called()
    launch.assert_called_once_with(["/bin/omnigent", *args])


@pytest.mark.parametrize("native", ["claude", "codex"])
def test_configure_omni_installs_only_available_family(omni_config, native):
    import yaml

    omni, install, state = omni_config
    other = "codex" if native == "claude" else "claude"
    state.pop(f"{other}_models")
    omni.write_tool_config(state)
    assert [call.args[0] for call in install.call_args_list] == ["omni", native]
    assert yaml.safe_load(omni.CONFIG_PATH.read_text())["providers"]["unity-gateway"][
        "default"
    ] == [omni._FAMILIES[native]]


@pytest.mark.parametrize(
    "content",
    [
        "[broken",
        "- list",
        "providers: []",
        "providers:\n  unity-gateway:\n    kind: subscription\n    cli: codex\n",
    ],
)
def test_configure_omni_rejects_invalid_config_without_writing(omni_config, content):
    omni, install, state = omni_config
    omni.CONFIG_PATH.write_text(content)
    with pytest.raises(RuntimeError):
        omni.write_tool_config(state)
    assert omni.CONFIG_PATH.read_text() == content
    assert not omni.BACKUP_PATH.exists()
    install.assert_not_called()


def test_configure_omni_install_failure_does_not_publish_config(omni_config):
    omni, install, state = omni_config
    install.side_effect = RuntimeError("installation failed")
    with pytest.raises(RuntimeError, match="installation failed"):
        omni.write_tool_config(state)
    assert not omni.CONFIG_PATH.exists()
    assert "omni" not in state.get("available_tools", [])


def test_configure_omni_write_failure_preserves_config_backup_and_state(omni_config):
    from ucode.state import load_state

    omni, _, state = omni_config
    original = "host:\n  name: keep-my-host\n"
    omni.CONFIG_PATH.write_text(original)
    previous_state = load_state()
    with patch.object(omni.os, "replace", side_effect=PermissionError("config is read-only")):
        with pytest.raises(PermissionError, match="config is read-only"):
            omni.write_tool_config(state)

    assert omni.CONFIG_PATH.read_text() == original
    assert omni.BACKUP_PATH.read_text() == original
    assert not list(omni.CONFIG_PATH.parent.glob(".ug-omni-*.yaml"))
    assert "omni" not in state.get("managed_configs", {})
    assert load_state() == previous_state


def test_configure_omni_dry_run_does_not_install_write_or_print_credentials(omni_config, capsys):
    omni, install, state = omni_config
    original = "host:\n  token: private-test-marker\n"
    omni.CONFIG_PATH.write_text(original)
    with patch.object(omni, "is_dry_run", return_value=True):
        omni.write_tool_config(state)
    install.assert_not_called()
    assert omni.CONFIG_PATH.read_text() == original
    assert not omni.BACKUP_PATH.exists()
    assert "private-test-marker" not in capsys.readouterr().out


def test_configure_omni_rejects_mult_workspace_host(omni_config):
    omni, install, state = omni_config
    with patch("ucode.workspace.workspace_headers", return_value={"X-Databricks-Org-Id": "123"}):
        with pytest.raises(RuntimeError, match="multiple workspaces"):
            omni.write_tool_config(state)
    install.assert_not_called()


@pytest.mark.parametrize("missing", ["profile", "models"])
def test_configure_omni_requires_profile_and_native_models(omni_config, missing):
    omni, install, state = omni_config
    if missing == "profile":
        state.pop("profile")
    else:
        state.pop("claude_models")
        state.pop("codex_models")
    with pytest.raises(RuntimeError):
        omni.write_tool_config(state)
    install.assert_not_called()


def test_omni_provider_defaults_preserve_other_families():
    from ucode.agents.omni import render_config

    existing = {
        "providers": {
            "old": {"kind": "databricks", "profile": "old", "default": True},
            "other": {"kind": "key", "gemini": {"api_key": "$MY_KEY"}, "default": ["gemini", "pi"]},
        }
    }
    config = render_config(existing, "new", ["openai"])
    assert config["providers"]["old"]["default"] == ["anthropic"]
    assert config["providers"]["other"] == existing["providers"]["other"]
    assert existing["providers"]["old"]["default"] is True
    assert render_config(config, "new", ["openai"]) == config


@pytest.mark.parametrize("selected", [None, ["omni"]])
def test_configure_picker_and_explicit_selection_configure_omni(omni_config, selected):
    import ucode.cli as cli
    from ucode.agents import configure_selected_tools

    omni, _, state = omni_config
    with (
        patch.object(cli, "_configure_shared_workspace_states", return_value=[state]),
        patch.object(cli, "refresh_managed_config", return_value=(None, False)),
        patch.object(cli, "install_tool_binary", return_value=True),
        patch.object(cli, "prompt_for_tools", return_value=["omni"]) as picker,
        patch.object(cli, "_maybe_select_provider_service", side_effect=lambda tool, state: state),
        patch.object(
            cli,
            "configure_selected_tools",
            side_effect=lambda state, tools, **kw: configure_selected_tools(
                state, tools, install_ai_tools=False
            ),
        ),
        patch.object(cli, "install_databricks_ai_tools_for_agents"),
        patch.object(cli, "_configure_managed_mcp_servers"),
        patch.object(cli, "_configure_managed_skills"),
        patch.object(cli, "_restore_mcp_after_workspace_switch"),
    ):
        assert (
            cli.configure_workspace_command(
                selected_tools=selected,
                workspaces=[(state["workspace"], state["profile"])],
            )
            == 0
        )
    assert omni.CONFIG_PATH.exists()
    assert "omni" in state["available_tools"]
    if selected is None:
        assert ("omni", "Omnigent (Claude and Codex)") in picker.call_args.args[0]
    else:
        picker.assert_not_called()


@pytest.mark.parametrize("flag", ["--agent", "--agents"])
def test_configure_accepts_omni_alias(flag):
    with patch("ucode.cli.configure_workspace_command") as configure:
        result = CliRunner().invoke(app, ["configure", flag, "omnigent"])
    assert result.exit_code == 0, result.output
    if flag == "--agents":
        assert configure.call_args.kwargs["selected_tools"] == ["omni"]
    else:
        assert configure.call_args.args == ("omni",)


@pytest.mark.parametrize("agents", ["omni", "omnigent", "omni,claude,codex"])
def test_mcp_omni_scope_targets_native_clients(agents):
    from ucode.cli import _parse_mcp_agents

    assert _parse_mcp_agents(agents) == {"claude", "codex"}


def test_omni_launch_refreshes_downloaded_skills_but_help_does_not():
    from ucode.agents import omni

    state = {"available_tools": ["omni"]}
    with (
        patch.object(omni.shutil, "which", return_value="/bin/omnigent"),
        patch.object(omni, "load_state", return_value=state),
        patch.object(omni, "exec_or_spawn"),
        patch("ucode.skills_download.refresh_downloaded_skills_on_launch") as refresh,
    ):
        omni.launch_cli([])
        refresh.assert_called_once_with(state)
        omni.launch_cli(["--help"])
        assert refresh.call_count == 1


@pytest.mark.parametrize("trust_store", ["os", "python"])
def test_omni_installer_uses_uv_databricks_extra_and_tls_settings(monkeypatch, trust_store):
    from ucode.agents import omni

    monkeypatch.setenv("UG_TLS_TRUST_STORE", trust_store)
    monkeypatch.setenv("UG_OMNI_INSTALL_TEST", "inherited")
    monkeypatch.delenv("UV_SYSTEM_CERTS", raising=False)
    monkeypatch.delenv("UV_NATIVE_TLS", raising=False)
    with (
        patch.object(omni.shutil, "which", return_value="/bin/uv"),
        patch.object(omni.subprocess_cross_os, "run") as run,
    ):
        omni.install_binary()
    run.assert_called_once()
    assert run.call_args.args == (["/bin/uv", "tool", "install", "omnigent[databricks]>=0.22.0"],)
    options = dict(run.call_args.kwargs)
    environment = options.pop("env")
    assert options == {"check": True, "timeout": 300}
    inherited = environment.get("UG_OMNI_INSTALL_TEST")
    assert inherited == "inherited"
    trust_flags = [environment.get(name) for name in ("UV_SYSTEM_CERTS", "UV_NATIVE_TLS")]
    assert trust_flags == (["1", "1"] if trust_store == "os" else [None, None])


def test_omni_installer_reports_missing_uv():
    from ucode.agents import omni

    with patch.object(omni.shutil, "which", return_value=None):
        with pytest.raises(RuntimeError, match="uv is required"):
            omni.install_binary()


def test_omni_managed_workspace_uses_self_managed_setup(omni_config):
    import ucode.cli as cli

    _, _, state = omni_config
    with patch.object(cli, "_setup_single_agent", return_value=state) as setup:
        assert (
            cli.configure_workspace_command(
                selected_tools=["omni"],
                workspaces=[(state["workspace"], state["profile"])],
            )
            == 0
        )
    assert setup.call_args.args[1] == "omni"
    assert setup.call_args.kwargs["apply_managed"] is True


def test_omni_explicit_configure_failure_is_not_reported_as_success(omni_config):
    import ucode.cli as cli

    _, _, state = omni_config
    state["last_configured_tools"] = []
    with (
        patch.object(cli, "_configure_shared_workspace_states", return_value=[state]),
        patch.object(cli, "refresh_managed_config", return_value=(None, False)),
        patch.object(cli, "install_tool_binary", return_value=False),
        patch.object(cli, "configure_selected_tools", return_value=state),
        patch.object(cli, "install_databricks_ai_tools_for_agents"),
        patch.object(cli, "_configure_managed_mcp_servers"),
        patch.object(cli, "_configure_managed_skills"),
        patch.object(cli, "_restore_mcp_after_workspace_switch"),
    ):
        with pytest.raises(RuntimeError, match="Omnigent configuration failed"):
            cli.configure_workspace_command(
                selected_tools=["omni", "pi"],
                workspaces=[(state["workspace"], state["profile"])],
            )


def test_omni_ai_tools_opt_in_targets_available_native_clients():
    from types import SimpleNamespace

    from ucode.agents import install_databricks_ai_tools_for_agents

    with (
        patch("ucode.agents.refresh_managed_config", return_value=SimpleNamespace(manifest=None)),
        patch("ucode.agents.install_ai_tools") as install,
    ):
        install_databricks_ai_tools_for_agents(
            ["omni", "codex"],
            {
                "profile": "example",
                "databricks_ai_tools_enabled": True,
                "claude_models": {"sonnet": "claude-sonnet"},
                "codex_models": ["gpt-example"],
            },
        )
    assert install.call_args.args == (["codex", "claude-code"], "example")


def test_omni_revert_restores_defaults_preserves_later_host_and_provider_edits(omni_config):
    import yaml

    omni, _, state = omni_config
    original = {
        "host": {"name": "before"},
        "providers": {
            "personal": {"kind": "subscription", "cli": "claude", "default": True},
        },
    }
    omni.CONFIG_PATH.write_text(yaml.safe_dump(original))
    omni.write_tool_config(state)
    if os.name == "posix":
        assert omni.BACKUP_PATH.stat().st_mode & 0o777 == 0o600
        assert omni.CONFIG_PATH.stat().st_mode & 0o777 == 0o600
    changed = yaml.safe_load(omni.CONFIG_PATH.read_text())
    changed["host"]["name"] = "edited-after-configure"
    changed["providers"]["new"] = {"kind": "subscription", "cli": "codex"}
    omni.CONFIG_PATH.write_text(yaml.safe_dump(changed))
    assert omni.revert_config()
    reverted = yaml.safe_load(omni.CONFIG_PATH.read_text())
    assert reverted["host"]["name"] == "edited-after-configure"
    assert reverted["providers"]["personal"]["default"] is True
    assert reverted["providers"]["new"] == changed["providers"]["new"]
    assert "unity-gateway" not in reverted["providers"]
    assert not omni.BACKUP_PATH.exists()


def test_omni_revert_keeps_config_created_after_initial_setup(omni_config):
    import yaml

    omni, _, state = omni_config
    omni.write_tool_config(state)
    changed = yaml.safe_load(omni.CONFIG_PATH.read_text())
    changed["host"] = {"name": "new-host"}
    omni.CONFIG_PATH.write_text(yaml.safe_dump(changed))
    assert omni.revert_config()
    assert yaml.safe_load(omni.CONFIG_PATH.read_text())["host"] == {"name": "new-host"}


def test_omni_revert_restores_existing_ug_provider_and_preserves_user_default_change(omni_config):
    import yaml

    omni, _, state = omni_config
    original_provider = {
        "kind": "databricks",
        "profile": "previous-profile",
        "default": "openai",
        "native_gemini": True,
    }
    original = {
        "providers": {
            "unity-gateway": original_provider,
            "personal": {"kind": "subscription", "cli": "claude", "default": True},
        }
    }
    omni.CONFIG_PATH.write_text(yaml.safe_dump(original))
    omni.write_tool_config(state)
    changed = yaml.safe_load(omni.CONFIG_PATH.read_text())
    changed["providers"]["personal"]["default"] = "anthropic"
    omni.CONFIG_PATH.write_text(yaml.safe_dump(changed))

    assert omni.revert_config()

    reverted = yaml.safe_load(omni.CONFIG_PATH.read_text())
    assert reverted["providers"]["unity-gateway"] == original_provider
    assert reverted["providers"]["personal"] == changed["providers"]["personal"]
    assert not omni.BACKUP_PATH.exists()


@pytest.mark.parametrize("invalid_file", ["CONFIG_PATH", "BACKUP_PATH"])
def test_omni_revert_invalid_yaml_keeps_config_and_backup_for_recovery(omni_config, invalid_file):
    from ucode.state import load_state

    omni, _, state = omni_config
    omni.CONFIG_PATH.write_text("host:\n  name: keep-my-host\n")
    omni.write_tool_config(state)
    getattr(omni, invalid_file).write_text("[broken")
    before_config = omni.CONFIG_PATH.read_bytes()
    before_backup = omni.BACKUP_PATH.read_bytes()
    before_state = load_state()

    with pytest.raises(RuntimeError, match="fix the YAML and retry"):
        omni.revert_config()

    assert omni.CONFIG_PATH.read_bytes() == before_config
    assert omni.BACKUP_PATH.read_bytes() == before_backup
    assert load_state() == before_state


def test_omni_does_not_enable_implicit_gateway_gemini_default():
    from ucode.agents.omni import render_config

    original = {
        "providers": {
            "gateway": {
                "kind": "gateway",
                "default": True,
                "anthropic": {"base_url": "https://example.com"},
                "gemini": {"base_url": "https://example.com"},
            }
        }
    }
    result = render_config(original, "ug", ["anthropic", "openai"])
    assert result["providers"]["gateway"]["default"] is False
