"""``ug configure``'s GitHub Copilot desktop app setup: ug's plugin, and ug's providers in a running
app, against a fake of the app's WebSocket API."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.test_copilot_app import CATALOG, FakeApp, seed
from ucode import agents as agents_mod
from ucode import databricks, state
from ucode.agents.desktop import copilot_app
from ucode.agents.desktop import copilot_app_plugin as plugin
from ucode.agents.desktop import setup as desktop_setup
from ucode.agents.vscode import copilot_models as real_copilot_models
from ucode.constants import MODEL_PROVIDER_SERVICE_HEADER
from ucode.workspace import SELECTOR_HEADER_NAMES, selector_headers

WS = "https://example.cloud.databricks.com"
STATE = {"workspace": WS, "profile": "dev"}


@pytest.fixture(autouse=True)
def _app_home(tmp_path, monkeypatch):
    """The app's settings and ug's plugin live under ``tmp_path``; the conftest already keeps
    every test away from a real app, and nothing here can start one."""
    monkeypatch.setenv("UG_EXPERIMENTAL_COPILOT_APP", "1")
    monkeypatch.setattr(desktop_setup, "copilot_app_installed", lambda: True)
    monkeypatch.setattr("ucode.agents.vscode.copilot_models", lambda *args, **kwargs: CATALOG)
    monkeypatch.setattr("ucode.agents.copilot.agent_version", lambda binary: "1.0.91")
    monkeypatch.setattr("ucode.databricks.ug_binary", lambda: "/opt/ug/bin/ug")
    monkeypatch.setattr(
        "ucode.databricks.databricks_cli_path", lambda: "/opt/databricks/bin/databricks"
    )
    monkeypatch.setattr(plugin, "UNLOAD_WAIT_S", 0)
    popen = Mock()
    monkeypatch.setattr("ucode.os_compatibility.subprocess_cross_os.popen", popen)
    yield
    popen.assert_not_called()


@pytest.fixture
def app(tmp_path):
    fake = FakeApp(tmp_path / ".copilot")
    yield fake
    fake.stop()


def _settings_path() -> Path:
    return plugin.copilot_settings_path()


def _settings() -> dict:
    return json.loads(_settings_path().read_text())


def _extension_config() -> dict:
    path = plugin.plugin_root() / "plugins" / plugin.PLUGIN / "extensions" / plugin.EXTENSION
    return json.loads((path / "config.json").read_text())


def _out(capsys) -> str:
    return " ".join(capsys.readouterr().out.split())


def _register(fake, app_state=STATE, **kwargs):
    seed(fake, copilot_app.state_providers(app_state, **kwargs).app)


class TestConfigure:
    def test_installs_the_plugin_with_the_app_rows(self, capsys):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)

        assert plugin.status() == "installed"
        assert _settings()["enabledPlugins"] == {plugin.PLUGIN_KEY: True}
        config = _extension_config()
        desired = copilot_app.state_providers(STATE)
        assert [p["name"] for p in config["providers"]] == [p.id for p in desired.app]
        assert [rows["provider"]["id"] for rows in config["app"]["providers"]] == [
            p.id for p in desired.app
        ]
        assert config["app"]["ownedProviderIds"] == list(copilot_app.OWNED_PROVIDER_IDS)
        assert config["databricksPath"] == "/opt/databricks/bin/databricks"
        assert config["tokenCommand"][:2] == ["/opt/ug/bin/ug", "auth-token"]
        assert "/opt/databricks/bin" in config["pathDirs"]
        out = _out(capsys)
        assert "Open (or restart) the GitHub Copilot app to load the Databricks models." in out
        assert "Settings -> Model providers" not in out

    def test_never_talks_to_the_running_app(self, app):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)

        assert app.received == []

    def test_the_plugin_sends_ugs_copilot_headers(self, monkeypatch):
        monkeypatch.setattr(
            desktop_setup,
            "copilot_app_state",
            lambda state: {**state, "copilot_http_headers": {"X-Team": "data"}},
        )
        monkeypatch.setattr(
            "ucode.workspace.workspace_headers", lambda url=None: {"X-Databricks-Org-Id": "42"}
        )

        desktop_setup.configure_copilot_app(STATE, provider="main.default.mps", location=None)

        for provider in _extension_config()["providers"]:
            headers = provider["headers"]
            assert not [name for name in headers if name.casefold() == "user-agent"]
            assert headers["x-databricks-use-coding-agent-mode"] == "true"
            assert headers["X-Team"] == "data"
            assert headers[MODEL_PROVIDER_SERVICE_HEADER] == "main.default.mps"
            assert headers["X-Databricks-Org-Id"] == "42"

    def test_the_plugin_selects_the_saved_workspace_on_a_shared_host(self):
        desktop_setup.configure_copilot_app(
            {**STATE, "workspace_id": "1234567890"}, provider=None, location=None
        )

        for provider in _extension_config()["providers"]:
            selected = {
                name: provider["headers"][name]
                for name in SELECTOR_HEADER_NAMES
                if name in provider["headers"]
            }
            assert selected == selector_headers("1234567890")

    def test_an_admin_header_cant_select_another_workspace(self, monkeypatch):
        monkeypatch.setattr(
            desktop_setup,
            "copilot_app_state",
            lambda state: {
                **state,
                "copilot_http_headers": {SELECTOR_HEADER_NAMES[1]: "999"},
            },
        )

        desktop_setup.configure_copilot_app(
            {**STATE, "workspace_id": "1234567890"}, provider=None, location=None
        )

        for provider in _extension_config()["providers"]:
            assert provider["headers"][SELECTOR_HEADER_NAMES[1]] == "1234567890"

    def test_records_the_databricks_cli_for_helpers_without_it_on_path(self):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)

        assert state.recorded_databricks_cli() == "/opt/databricks/bin/databricks"

    @pytest.mark.parametrize("content", ["{", "[]"])
    def test_unreadable_settings_are_left_alone(self, content, capsys):
        _settings_path().parent.mkdir(parents=True)
        _settings_path().write_text(content)

        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)

        assert _settings_path().read_text() == content
        assert not plugin.plugin_root().exists()
        assert "Skipped the GitHub Copilot app" in _out(capsys)

    def test_warns_about_telemetry_keys_that_disable_the_plugin(self, capsys):
        _settings_path().parent.mkdir(parents=True)
        _settings_path().write_text(json.dumps({"telemetry": {"filePath": "/tmp/spans"}}))

        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)

        assert plugin.status() == "installed"
        out = _out(capsys)
        assert "ignores its whole settings file" in out
        assert "telemetry.filePath" in out

    def test_no_models_skips_the_app(self, monkeypatch, capsys):
        def no_models(*args, **kwargs):
            raise RuntimeError("No models for the GitHub Copilot app.")

        monkeypatch.setattr("ucode.agents.vscode.copilot_models", no_models)

        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)

        assert not plugin.plugin_root().exists()
        assert "Skipped the GitHub Copilot app: No models" in _out(capsys)


class TestConfigureCopilotApp:
    def test_sets_up_the_installed_app(self):
        agents_mod.configure_copilot_app(STATE, provider=None, parent_schema=None)

        assert plugin.status() == "installed"

    def test_opt_out_and_a_missing_app_skip_it(self, monkeypatch):
        agents_mod.request_copilot_app_with_copilot(False)
        try:
            agents_mod.configure_copilot_app(STATE, provider=None, parent_schema=None)
        finally:
            agents_mod.request_copilot_app_with_copilot(True)
        monkeypatch.setattr(desktop_setup, "copilot_app_installed", lambda: False)
        agents_mod.configure_copilot_app(STATE, provider=None, parent_schema=None)

        assert not plugin.plugin_root().exists()
        assert not _settings_path().exists()

    def test_without_the_experiment_flag_nothing_changes(self, monkeypatch, app):
        monkeypatch.delenv("UG_EXPERIMENTAL_COPILOT_APP")

        agents_mod.configure_copilot_app(STATE, provider=None, parent_schema=None)
        agents_mod.configure_copilot_app(
            {**STATE, "copilot_app_integration": False}, provider=None, parent_schema=None
        )

        assert not plugin.plugin_root().exists()
        assert app.received == []
        assert desktop_setup.copilot_app_status(STATE, provider=None, location=None) is None

    def test_managed_off_switch_removes_the_plugin_and_providers(self, app, capsys):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)
        _register(app)

        agents_mod.configure_copilot_app(
            {**STATE, "copilot_app_integration": False}, provider=None, parent_schema=None
        )

        assert plugin.status() == "not_installed"
        assert app.providers == {}
        assert "turns off the GitHub Copilot app" in _out(capsys)

    def test_managed_off_switch_reads_the_managed_config(self, monkeypatch):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)
        monkeypatch.setattr(
            "ucode.managed_config.load_managed_state",
            lambda workspace: {"enabled_agents": {"copilot": {"app_integration_enabled": False}}},
        )

        agents_mod.configure_copilot_app(STATE, provider=None, parent_schema=None)

        assert plugin.status() == "removal_pending"


class TestRemove:
    def test_unregisters_the_running_app_and_removes_the_plugin(self, app):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)
        _register(app)

        assert desktop_setup.revert_copilot_app() == "removed"
        assert app.providers == {}
        assert plugin.status() == "not_installed"
        assert not _settings_path().exists()

    def test_a_closed_app_leaves_the_removal_to_the_plugins_next_start(self):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)

        result = desktop_setup.revert_copilot_app()

        assert result == desktop_setup.COPILOT_APP_REMOVAL_PENDING
        assert "the next time the GitHub Copilot app starts" in result
        assert plugin.status() == "removal_pending"
        assert _settings()["enabledPlugins"] == {plugin.PLUGIN_KEY: True}
        assert _extension_config()["remove"] is True

    def test_a_later_run_deletes_what_the_plugin_left_behind(self):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)
        desktop_setup.revert_copilot_app()
        _settings_path().write_text(json.dumps({"model": "kept"}))

        result = desktop_setup.copilot_app_status(STATE, provider=None, location=None)

        assert result is not None
        assert result.row == "plugin not installed, models unknown (app closed)"
        assert not plugin.plugin_root().exists()
        assert _settings() == {"model": "kept"}

    def test_a_pending_removal_is_kept_while_its_keys_remain(self):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)
        desktop_setup.revert_copilot_app()

        result = desktop_setup.copilot_app_status(STATE, provider=None, location=None)

        assert result is not None
        assert result.row == desktop_setup.COPILOT_APP_REMOVAL_PENDING
        assert plugin.plugin_root().exists()

    def test_status_after_revert_shows_the_pending_removal_then_cleans_up(self):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)
        desktop_setup.revert_copilot_app()

        assert desktop_setup.copilot_app_unconfigured_status() == (
            desktop_setup.COPILOT_APP_REMOVAL_PENDING
        )
        _settings_path().write_text("{}")

        assert desktop_setup.copilot_app_unconfigured_status() is None
        assert not plugin.plugin_root().exists()
        assert not _settings_path().exists()

    def test_reverting_with_the_app_open_after_a_pending_removal_finishes_it(self, app):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)
        desktop_setup.revert_copilot_app()
        _register(app)

        assert desktop_setup.revert_copilot_app() == "removed"
        assert app.providers == {}
        assert not plugin.plugin_root().exists()
        assert not _settings_path().exists()

    def test_configure_again_cancels_a_pending_removal(self):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)
        desktop_setup.revert_copilot_app()

        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)

        assert plugin.status() == "installed"
        assert "remove" not in _extension_config()

    def test_a_second_revert_after_opening_the_app_removes_the_rows(self, app):
        _register(app)

        assert desktop_setup.revert_copilot_app() == "removed"
        assert app.providers == {}

    def test_nothing_set_up(self):
        assert desktop_setup.revert_copilot_app() == "not set up"


class TestStatus:
    def _status(self, app_state=STATE):
        return desktop_setup.copilot_app_status(app_state, provider=None, location=None)

    def test_installed_and_registered(self, app):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)
        _register(app)

        result = self._status()

        assert result is not None
        assert result.row == "plugin installed, models registered"
        assert result.warnings == ()

    def test_registered_rows_without_the_plugin_fail(self, app):
        _register(app)

        result = self._status()

        assert result is not None
        assert result.row == "plugin not installed, models registered"
        assert "their turns fail without it" in result.warnings[0]

    def test_a_disabled_plugin(self, app):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)
        _register(app)
        settings = _settings()
        settings["enabledPlugins"][plugin.PLUGIN_KEY] = False
        _settings_path().write_text(json.dumps(settings))

        result = self._status()

        assert result is not None
        assert result.row == "plugin disabled, models registered"
        assert "is turned off" in result.warnings[0]

    def test_invalid_settings(self, app):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)
        _register(app)
        _settings_path().write_text("{")

        result = self._status()

        assert result is not None
        assert result.row == "plugin invalid settings, models registered"
        assert "isn't valid JSON" in result.warnings[0]

    def test_telemetry_keys_are_reported(self):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)
        settings = _settings()
        settings["telemetry"] = {"enabled": True, "endpoint": "http://localhost:4318"}
        _settings_path().write_text(json.dumps(settings))

        result = self._status()

        assert result is not None
        assert result.row == "plugin installed, ignored, models unknown (app closed)"
        assert "telemetry.enabled, telemetry.endpoint" in result.warnings[0]
        assert "managed settings" in result.warnings[0]

    def test_another_workspace_is_out_of_date(self, app):
        desktop_setup.configure_copilot_app(STATE, provider=None, location=None)
        _register(app, {**STATE, "workspace": "https://other.cloud.databricks.com"})

        result = self._status()

        assert result is not None
        assert result.row == "plugin installed, models out of date"
        assert "don't match" in result.warnings[0]

    def test_status_prints_no_version_warning(self, app, capsys):
        app.version = "9.9.9"
        _register(app)
        capsys.readouterr()

        self._status()

        assert "hasn't been verified" not in capsys.readouterr().out

    def test_managed_off(self):
        result = self._status({**STATE, "copilot_app_integration": False})

        assert result is not None
        assert result.row == "off (workspace-managed)"

    def test_not_installed_and_never_set_up(self, monkeypatch):
        monkeypatch.setattr(desktop_setup, "copilot_app_installed", lambda: False)

        assert self._status() is None


def test_ignored_settings_reason_names_only_rejected_telemetry_keys(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"telemetry": {"exporter": "x"}, "model": "y"}))
    assert desktop_setup.copilot_settings_ignored_reason(path) is None
    path.write_text(json.dumps({"telemetry": {"protocol": "http", "resourceAttributes": {}}}))
    reason = desktop_setup.copilot_settings_ignored_reason(path)
    assert reason is not None
    assert "telemetry.protocol, telemetry.resourceAttributes" in reason


def test_a_recorded_databricks_cli_is_found_off_path(tmp_path, monkeypatch):
    cli = tmp_path / "bin" / "databricks"
    cli.parent.mkdir()
    cli.write_text("#!/bin/sh\necho 'Databricks CLI v0.280.0'\n")
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr("sys.platform", "linux")
    databricks.clear_databricks_cli_cache()
    try:
        assert databricks._iter_databricks_executables() == []
        state.record_databricks_cli(str(cli))
        assert databricks._iter_databricks_executables() == [str(cli)]
    finally:
        databricks.clear_databricks_cli_cache()


def test_configure_no_copilot_app_opts_out_for_the_run(monkeypatch):
    from typer.testing import CliRunner

    from ucode.cli import app

    monkeypatch.setattr("ucode.cli.install_databricks_cli", lambda: None)
    monkeypatch.setattr("ucode.cli.configure_workspace_command", lambda **kwargs: 0)

    result = CliRunner().invoke(
        app, ["configure", "--agents", "copilot", "--workspace", WS, "--no-copilot-app"]
    )

    assert result.exit_code == 0, result.output
    assert agents_mod._copilot_app_with_copilot is False


def test_a_missing_catalog_names_the_app():
    with pytest.raises(RuntimeError, match="models for the GitHub Copilot app"):
        real_copilot_models({"workspace": WS}, agent="the GitHub Copilot app")


def test_a_dry_run_changes_nothing(monkeypatch, app, capsys):
    desktop_setup.configure_copilot_app(STATE, provider=None, location=None)
    _register(app)
    before = _settings_path().read_text()
    config = _extension_config()
    monkeypatch.setattr("ucode.config_io._dry_run", True)
    received = len(app.received)

    agents_mod.configure_copilot_app(
        {**STATE, "workspace": "https://other.cloud.databricks.com"},
        provider=None,
        parent_schema=None,
    )
    agents_mod.configure_copilot_app(
        {**STATE, "copilot_app_integration": False}, provider=None, parent_schema=None
    )
    assert desktop_setup.revert_copilot_app() == "unchanged (dry run)"

    assert _settings_path().read_text() == before
    assert _extension_config() == config
    assert len(app.received) == received
    assert "Dry run: the GitHub Copilot app isn't changed." in _out(capsys)
