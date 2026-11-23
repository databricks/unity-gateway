import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ucode.agents.desktop import copilot_app_plugin as plugin

NODE = shutil.which("node")
PLUGIN_SOURCE = Path(plugin.__file__).parent


def _config(**overrides) -> plugin.PluginConfig:
    values = {
        "workspace": "https://example.cloud.databricks.com",
        "ug_path": "/opt/ug/bin/ug",
        "profile": "dev",
        "headers": {
            "User-Agent": "ucode/1.0 copilot/1.4.2",
            "x-databricks-use-coding-agent-mode": "true",
        },
        "path_dirs": ("/opt/ug/bin",),
        "providers": (
            plugin.PluginProvider(
                id="11111111-1111-5111-8111-111111111111",
                wire="anthropic",
                base_path="/ai-gateway/anthropic",
                models=(
                    plugin.PluginModel(
                        "claude-sonnet-5", "system.ai.claude-sonnet-5", "Claude Sonnet 5"
                    ),
                ),
            ),
            plugin.PluginProvider(
                id="22222222-2222-5222-8222-222222222222",
                wire="responses",
                base_path="/ai-gateway/mlflow/v1",
                models=(plugin.PluginModel("gpt-6", "system.ai.gpt-6", "GPT-6"),),
            ),
            plugin.PluginProvider(
                id="33333333-3333-5333-8333-333333333333",
                wire="completions",
                base_path="/ai-gateway/mlflow/v1",
                models=(plugin.PluginModel("glm-5", "system.ai.glm-5", "GLM 5"),),
            ),
        ),
    }
    values.update(overrides)
    return plugin.PluginConfig(**values)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(plugin.time, "sleep", lambda seconds: None)
    return tmp_path


def _settings(home: Path) -> Path:
    return home / ".copilot" / "settings.json"


class TestExtensionConfig:
    def test_providers_keep_ids_wires_and_headers(self):
        config = plugin.extension_config(_config())
        claude, gpt, open_models = config["providers"]
        assert claude == {
            "name": "11111111-1111-5111-8111-111111111111",
            "type": "anthropic",
            "baseUrl": "https://example.cloud.databricks.com/ai-gateway/anthropic",
            "headers": {
                "User-Agent": "ucode/1.0 copilot/1.4.2",
                "x-databricks-use-coding-agent-mode": "true",
            },
        }
        assert (gpt["type"], gpt["wireApi"], gpt["baseUrl"]) == (
            "openai",
            "responses",
            "https://example.cloud.databricks.com/ai-gateway/mlflow/v1",
        )
        assert (open_models["type"], open_models["wireApi"]) == ("openai", "completions")

    def test_models_match_the_picker_rows(self):
        models = plugin.extension_config(_config())["models"]
        assert models[0] == {
            "id": "claude-sonnet-5",
            "modelId": "claude-sonnet-5",
            "provider": "11111111-1111-5111-8111-111111111111",
            "wireModel": "system.ai.claude-sonnet-5",
            "name": "Claude Sonnet 5",
        }
        assert [m["provider"][0] for m in models] == ["1", "2", "3"]

    def test_models_repeat_the_rows_limits_and_vision(self):
        model = plugin.PluginModel(
            "claude-sonnet-5",
            "system.ai.claude-sonnet-5",
            "Claude Sonnet 5",
            (936_000, 64_000),
            True,
        )
        provider = plugin.PluginProvider("p", "anthropic", "/ai-gateway/anthropic", (model,))
        (entry,) = plugin.extension_config(_config(providers=(provider,)))["models"]
        assert entry["maxPromptTokens"] == 936_000
        assert entry["maxOutputTokens"] == 64_000
        assert entry["maxContextWindowTokens"] == 1_000_000
        assert entry["capabilities"] == {"supports": {"vision": True}}

    def test_a_small_output_limit_is_left_to_the_runtime(self):
        model = plugin.PluginModel("m", "system.ai.m", "M", (100_000, 16_000))
        provider = plugin.PluginProvider("p", "completions", "/ai-gateway/mlflow/v1", (model,))
        (entry,) = plugin.extension_config(_config(providers=(provider,)))["models"]
        assert entry["maxPromptTokens"] == 100_000
        assert "maxOutputTokens" not in entry
        assert "maxContextWindowTokens" not in entry

    def test_reasoning_effort_can_be_turned_off(self):
        model = plugin.PluginModel("glm-5", "system.ai.glm-5", "GLM 5", reasoning_effort=False)
        provider = plugin.PluginProvider("p", "completions", "/ai-gateway/mlflow/v1", (model,))
        (entry,) = plugin.extension_config(_config(providers=(provider,)))["models"]
        assert entry["capabilities"] == {"supports": {"reasoningEffort": False}}
        assert "capabilities" not in plugin.extension_config(_config())["models"][1]

    def test_app_rows_and_databricks_path(self):
        rows = plugin.AppProviderRows(
            provider={"id": "p", "name": "Databricks GPT", "kind": "custom", "settings": {}},
            secret=None,
            models=({"id": "m", "providerId": "p", "modelId": "gpt-6"},),
        )
        config = plugin.extension_config(
            _config(
                app_providers=(rows,),
                owned_provider_ids=("p", "q"),
                databricks_path="/usr/local/bin/databricks",
            )
        )
        assert config["app"] == {
            "providers": [
                {
                    "provider": {
                        "id": "p",
                        "name": "Databricks GPT",
                        "kind": "custom",
                        "settings": {},
                    },
                    "secret": None,
                    "models": [{"id": "m", "providerId": "p", "modelId": "gpt-6"}],
                }
            ],
            "ownedProviderIds": ["p", "q"],
        }
        assert config["databricksPath"] == "/usr/local/bin/databricks"
        assert plugin.extension_config(_config())["app"] == {
            "providers": [],
            "ownedProviderIds": [],
        }

    def test_the_seat_relay_is_off_unless_it_names_a_provider_service(self):
        assert plugin.extension_config(_config())["seatRelay"] is None
        config = plugin.extension_config(
            _config(
                headers={"X-Team": "data"},
                seat_relay="main.default.copilot_seats",
                user_agent="ucode/1.0 (linux ubuntu-24.04; x86_64)",
            )
        )
        assert config["seatRelay"] == {
            "gatewayBase": "https://example.cloud.databricks.com",
            "providerService": "main.default.copilot_seats",
            "headers": {"X-Team": "data"},
            "userAgent": "ucode/1.0 (linux ubuntu-24.04; x86_64)",
            "askGitHub": True,
        }

    def test_a_pat_token_command_and_hint(self):
        config = plugin.extension_config(_config(use_pat=True))
        assert config["tokenCommand"][-1] == "--use-pat"
        assert config["tokenCommand"][0] == "/opt/ug/bin/ug"
        assert config["ugPath"] == "/opt/ug/bin/ug"
        assert "`token = <PAT>` entry under [dev]" in config["loginHint"]

    def test_a_custom_oauth_token_command_and_hint(self):
        oauth = {
            "client_id": "my-app",
            "redirect_url": "http://localhost:8020",
            "scopes": ["all-apis", "offline_access"],
        }
        config = plugin.extension_config(_config(profile=None, custom_oauth=oauth))
        assert config["tokenCommand"] == [
            "/opt/ug/bin/ug",
            "auth-token",
            "--host",
            "https://example.cloud.databricks.com",
            "--client-id",
            "my-app",
            "--redirect-url",
            "http://localhost:8020",
            "--scopes",
            "all-apis,offline_access",
        ]
        assert "Rerun `ug configure`" in config["loginHint"]
        with_profile = plugin.extension_config(
            _config(profile=None, custom_oauth={**oauth, "profile": "ug-oauth"})
        )
        assert with_profile["tokenCommand"][-2:] == ["--profile", "ug-oauth"]

    def test_a_missing_ug_hint_names_its_path(self):
        assert "/opt/ug/bin/ug" in plugin.extension_config(_config())["missingUgHint"]

    def test_token_command_and_hint_name_the_workspace_and_profile(self):
        config = plugin.extension_config(_config())
        assert config["tokenCommand"] == [
            "/opt/ug/bin/ug",
            "auth-token",
            "--host",
            "https://example.cloud.databricks.com",
            "--profile",
            "dev",
        ]
        assert (
            "databricks auth login --host https://example.cloud.databricks.com --profile dev"
            in (config["loginHint"])
        )
        assert config["pathDirs"] == ["/opt/ug/bin"]

    def test_without_profile(self):
        config = plugin.extension_config(_config(profile=None))
        assert "--profile" not in config["tokenCommand"]
        assert "--profile" not in config["loginHint"]

    def test_trailing_slash_on_workspace(self):
        config = plugin.extension_config(_config(workspace="https://example.cloud.databricks.com/"))
        assert (
            config["providers"][0]["baseUrl"]
            == "https://example.cloud.databricks.com/ai-gateway/anthropic"
        )


class TestWritePlugin:
    def test_layout(self, home):
        root = plugin.write_plugin(_config())
        assert root == home / ".ucode" / "copilot-app-plugin"
        marketplace = json.loads((root / ".github" / "plugin" / "marketplace.json").read_text())
        assert marketplace["name"] == "databricks-unity-gateway"
        assert marketplace["plugins"][0]["source"] == "./plugins/unity-gateway"
        manifest = json.loads(
            (root / "plugins" / "unity-gateway" / ".plugin" / "plugin.json").read_text()
        )
        assert manifest["name"] == "unity-gateway"
        extension = root / "plugins" / "unity-gateway" / "extensions" / "databricks"
        assert {p.name for p in extension.iterdir()} == {
            "extension.mjs",
            "ug-plugin.mjs",
            "app-rows.mjs",
            "app-remove.mjs",
            "app-restart.mjs",
            "app-state.mjs",
            "app-host.mjs",
            "app-proxy.mjs",
            "app-windows.mjs",
            "app-debug.mjs",
            "app-system-proxy.mjs",
            "app-seat-relay.mjs",
            "app-seat-redirect.mjs",
            "config.json",
        }
        assert (extension / "extension.mjs").read_text() == (
            PLUGIN_SOURCE / "extension.mjs"
        ).read_text()
        assert json.loads((extension / "config.json").read_text()) == plugin.extension_config(
            _config()
        )

    def test_rewrite_replaces_config(self, home):
        plugin.write_plugin(_config())
        root = plugin.write_plugin(_config(profile="other"))
        config = json.loads(
            (
                root / "plugins" / "unity-gateway" / "extensions" / "databricks" / "config.json"
            ).read_text()
        )
        assert config["tokenCommand"][-1] == "other"


class TestSettings:
    def test_install_merges_only_ug_keys(self, home):
        path = _settings(home)
        path.parent.mkdir()
        existing = {
            "model": "gpt-5",
            "extraKnownMarketplaces": {"other": {"source": {"source": "github", "repo": "o/r"}}},
            "enabledPlugins": {"x@other": False},
        }
        path.write_text(json.dumps(existing))
        root = plugin.install(_config())
        settings = json.loads(path.read_text())
        assert settings["model"] == "gpt-5"
        assert settings["extraKnownMarketplaces"] == {
            "other": {"source": {"source": "github", "repo": "o/r"}},
            "databricks-unity-gateway": {"source": {"source": "directory", "path": str(root)}},
        }
        assert settings["enabledPlugins"] == {
            "x@other": False,
            "unity-gateway@databricks-unity-gateway": True,
        }
        assert "telemetry" not in settings

    def test_install_creates_settings(self, home):
        plugin.install(_config())
        settings = json.loads(_settings(home).read_text())
        assert set(settings) == {"extraKnownMarketplaces", "enabledPlugins"}

    def test_install_is_idempotent(self, home):
        plugin.install(_config())
        first = _settings(home).read_text()
        plugin.install(_config())
        assert _settings(home).read_text() == first

    @pytest.mark.parametrize("content", ["{not json", "// comment\n{}", "[]"])
    def test_invalid_settings_are_left_alone(self, home, content):
        path = _settings(home)
        path.parent.mkdir()
        path.write_text(content)
        with pytest.raises(plugin.SettingsError):
            plugin.install(_config())
        assert path.read_text() == content
        assert not (home / ".ucode" / "copilot-app-plugin").exists()
        assert plugin.settings_valid() is False
        assert plugin.status() == "invalid_settings"

    def test_non_object_plugin_keys_are_left_alone(self, home):
        path = _settings(home)
        path.parent.mkdir()
        path.write_text(json.dumps({"enabledPlugins": ["x"]}))
        with pytest.raises(plugin.SettingsError):
            plugin.enable()
        assert json.loads(path.read_text()) == {"enabledPlugins": ["x"]}

    def test_copilot_home(self, home, monkeypatch):
        custom = home / "custom-copilot"
        monkeypatch.setenv("COPILOT_HOME", str(custom))
        plugin.install(_config())
        assert (custom / "settings.json").exists()
        assert not _settings(home).exists()

    def test_remove_leaves_no_ug_state(self, home):
        path = _settings(home)
        path.parent.mkdir()
        path.write_text(json.dumps({"model": "gpt-5", "enabledPlugins": {"x@other": True}}))
        root = plugin.install(_config())
        assert plugin.remove() is True
        assert json.loads(path.read_text()) == {
            "model": "gpt-5",
            "enabledPlugins": {"x@other": True},
        }
        assert not root.exists()
        assert plugin.remove() is False

    def test_remove_deletes_a_settings_file_ug_created(self, home):
        plugin.install(_config())
        assert plugin.remove() is True
        assert not _settings(home).exists()

    def test_remove_keeps_a_created_settings_file_the_user_added_to(self, home):
        plugin.install(_config())
        settings = json.loads(_settings(home).read_text())
        settings["model"] = "gpt-5"
        _settings(home).write_text(json.dumps(settings))
        assert plugin.remove() is True
        assert json.loads(_settings(home).read_text()) == {"model": "gpt-5"}

    def test_remove_keeps_an_empty_settings_file_ug_didnt_create(self, home):
        _settings(home).parent.mkdir()
        _settings(home).write_text("{}")
        plugin.install(_config())
        assert plugin.remove() is True
        assert json.loads(_settings(home).read_text()) == {}

    def test_a_finished_deferred_remove_deletes_a_settings_file_ug_created(self, home):
        plugin.install(_config())
        plugin.defer_remove()
        _settings(home).write_text("{}")
        assert plugin.cleanup_leftover() is True
        assert not _settings(home).exists()

    def test_remove_drops_the_keys_before_the_directory(self, home):
        _settings(home).parent.mkdir()
        _settings(home).write_text(json.dumps({"model": "x"}))
        root = plugin.install(_config())
        seen = []

        def wait(seconds):
            settings = json.loads(_settings(home).read_text())
            seen.append((seconds, settings, root.exists()))

        assert plugin.remove(wait=wait) is True
        assert seen == [(plugin.UNLOAD_WAIT_S, {"model": "x"}, True)]
        assert not root.exists()

    def test_remove_without_keys_deletes_the_directory_at_once(self, home):
        root = plugin.write_plugin(_config())
        waits = []
        assert plugin.remove(wait=waits.append) is True
        assert waits == []
        assert not root.exists()

    def test_remove_keeps_a_foreign_unity_gateway_marketplace(self, home):
        path = _settings(home)
        path.parent.mkdir()
        foreign = {
            "extraKnownMarketplaces": {
                "databricks-unity-gateway": {
                    "source": {"source": "directory", "path": "/elsewhere"}
                }
            },
            "enabledPlugins": {"unity-gateway@databricks-unity-gateway": True},
        }
        path.write_text(json.dumps(foreign))
        assert plugin.remove(wait=lambda seconds: None) is False
        assert json.loads(path.read_text()) == foreign

    def test_a_symlinked_settings_file_is_refused(self, home, tmp_path):
        target = tmp_path / "real-settings.json"
        target.write_text("{}")
        _settings(home).parent.mkdir()
        _settings(home).symlink_to(target)
        with pytest.raises(plugin.SettingsError, match="symlink"):
            plugin.install(_config())
        assert not plugin.plugin_root().exists()
        assert target.read_text() == "{}"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_rewriting_settings_keeps_their_mode(self, home):
        path = _settings(home)
        path.parent.mkdir()
        path.write_text("{}")
        path.chmod(0o644)
        plugin.install(_config())
        assert path.stat().st_mode & 0o777 == 0o644
        plugin.remove()
        assert path.stat().st_mode & 0o777 == 0o644

    def test_remove_drops_empty_keys(self, home):
        _settings(home).parent.mkdir()
        _settings(home).write_text(json.dumps({"model": "x"}))
        plugin.install(_config())
        plugin.remove()
        assert json.loads(_settings(home).read_text()) == {"model": "x"}

    def test_remove_with_invalid_settings_changes_nothing(self, home):
        root = plugin.install(_config())
        _settings(home).write_text("{oops")
        with pytest.raises(plugin.SettingsError):
            plugin.remove()
        assert root.exists()


def _extension_config(root: Path) -> dict:
    return json.loads(
        (
            root / "plugins" / "unity-gateway" / "extensions" / "databricks" / "config.json"
        ).read_text()
    )


def _remove_keys_as_the_extension_does(home: Path) -> None:
    settings = json.loads(_settings(home).read_text())
    del settings["extraKnownMarketplaces"]["databricks-unity-gateway"]
    del settings["enabledPlugins"]["unity-gateway@databricks-unity-gateway"]
    _settings(home).write_text(json.dumps({k: v for k, v in settings.items() if v}))


class TestDeferredRemove:
    def test_defer_remove_marks_the_config_and_keeps_the_plugin_enabled(self, home):
        root = plugin.install(_config(owned_provider_ids=("p",)))
        settings = _settings(home).read_text()
        assert plugin.defer_remove() is True
        config = _extension_config(root)
        assert config["remove"] is True
        assert config["pluginRoot"] == str(root)
        assert config["app"]["ownedProviderIds"] == ["p"]
        assert _settings(home).read_text() == settings
        assert plugin.status() == "removal_pending"

    def test_defer_remove_without_a_plugin(self, home):
        assert plugin.defer_remove() is False
        assert not plugin.plugin_root().exists()
        plugin.write_plugin(_config())
        assert plugin.defer_remove() is False
        assert "remove" not in _extension_config(plugin.plugin_root())

    @pytest.mark.parametrize("content", ["{not json", "[]"])
    def test_defer_remove_refuses_invalid_settings(self, home, content):
        root = plugin.install(_config())
        _settings(home).write_text(content)
        with pytest.raises(plugin.SettingsError):
            plugin.defer_remove()
        assert "remove" not in _extension_config(root)

    def test_defer_remove_refuses_a_symlinked_settings_file(self, home, tmp_path):
        root = plugin.install(_config())
        target = tmp_path / "real-settings.json"
        target.write_text(_settings(home).read_text())
        _settings(home).unlink()
        _settings(home).symlink_to(target)
        with pytest.raises(plugin.SettingsError, match="symlink"):
            plugin.defer_remove()
        assert "remove" not in _extension_config(root)

    def test_install_cancels_a_deferred_remove(self, home):
        plugin.install(_config())
        plugin.defer_remove()
        plugin.install(_config())
        assert plugin.status() == "installed"

    def test_a_disabled_plugin_is_not_pending(self, home):
        plugin.install(_config())
        plugin.defer_remove()
        settings = json.loads(_settings(home).read_text())
        settings["enabledPlugins"]["unity-gateway@databricks-unity-gateway"] = False
        _settings(home).write_text(json.dumps(settings))
        assert plugin.status() == "disabled"

    def test_cleanup_leftover_waits_for_the_extension(self, home):
        root = plugin.install(_config())
        plugin.defer_remove()
        assert plugin.cleanup_leftover() is False
        assert root.exists()
        _remove_keys_as_the_extension_does(home)
        assert plugin.status() == "not_installed"
        assert plugin.cleanup_leftover() is True
        assert not root.exists()
        assert plugin.cleanup_leftover() is False

    def test_cleanup_leftover_keeps_a_plugin_without_a_deferred_remove(self, home):
        root = plugin.install(_config())
        _remove_keys_as_the_extension_does(home)
        assert plugin.cleanup_leftover() is False
        assert root.exists()

    def test_cleanup_leftover_keeps_the_directory_while_a_key_is_left(self, home):
        root = plugin.install(_config())
        plugin.defer_remove()
        settings = json.loads(_settings(home).read_text())
        del settings["extraKnownMarketplaces"]
        _settings(home).write_text(json.dumps(settings))
        assert plugin.cleanup_leftover() is False
        assert root.exists()

    def test_cleanup_leftover_with_invalid_settings_changes_nothing(self, home):
        root = plugin.install(_config())
        plugin.defer_remove()
        _settings(home).write_text("{oops")
        assert plugin.cleanup_leftover() is False
        assert root.exists()

    def test_remove_after_the_extension_deletes_the_directory_at_once(self, home):
        root = plugin.install(_config())
        plugin.defer_remove()
        _remove_keys_as_the_extension_does(home)
        waits = []
        assert plugin.remove(wait=waits.append) is True
        assert waits == []
        assert not root.exists()

    def test_remove_while_pending_still_drops_the_keys_first(self, home):
        root = plugin.install(_config())
        plugin.defer_remove()
        waits = []
        assert plugin.remove(wait=waits.append) is True
        assert waits == [plugin.UNLOAD_WAIT_S]
        assert not _settings(home).exists()
        assert not root.exists()


class TestStatus:
    def test_not_installed(self, home):
        assert plugin.status() == "not_installed"

    def test_installed(self, home):
        plugin.install(_config())
        assert plugin.status() == "installed"

    def test_disabled(self, home):
        plugin.install(_config())
        path = _settings(home)
        settings = json.loads(path.read_text())
        settings["enabledPlugins"]["unity-gateway@databricks-unity-gateway"] = False
        path.write_text(json.dumps(settings))
        assert plugin.status() == "disabled"

    def test_missing_directory_is_not_installed(self, home):
        root = plugin.install(_config())
        shutil.rmtree(root)
        assert plugin.status() == "not_installed"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
class TestExtensionSource:
    @pytest.mark.parametrize(
        "name",
        [
            "extension.mjs",
            "ug-plugin.mjs",
            "app-rows.mjs",
            "app-remove.mjs",
            "app-restart.mjs",
            "app-state.mjs",
            "app-host.mjs",
            "app-proxy.mjs",
            "app-windows.mjs",
            "app-debug.mjs",
            "app-system-proxy.mjs",
            "app-seat-relay.mjs",
            "app-seat-redirect.mjs",
        ],
    )
    def test_syntax(self, name):
        subprocess.run([NODE, "--check", str(PLUGIN_SOURCE / name)], check=True)

    def test_the_extension_finishes_a_deferred_remove_for_ug(self, home, tmp_path):
        root = plugin.install(_config())
        plugin.defer_remove()
        extension = root / "plugins" / "unity-gateway" / "extensions" / "databricks"
        script = (
            "import { readFileSync } from 'node:fs';"
            f"import {{ removeOnce }} from {json.dumps((extension / 'app-remove.mjs').as_uri())};"
            "const [config, home, stateDir] = process.argv.slice(1);"
            "console.log(await removeOnce(JSON.parse(readFileSync(config, 'utf8')), { home, stateDir }));"
        )
        result = subprocess.run(
            [
                NODE,
                "--input-type=module",
                "-e",
                script,
                str(extension / "config.json"),
                str(_settings(home).parent),
                str(extension),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "removed"
        assert json.loads(_settings(home).read_text()) == {}
        assert plugin.cleanup_leftover() is True
        assert not root.exists()


class TestEarlierIds:
    LEGACY_MARKETPLACE = "unity-gateway"
    LEGACY_KEY = "databricks@unity-gateway"

    def _legacy_install(self, root: Path, path: str | None = None) -> None:
        extension = root / "plugins" / "databricks" / "extensions" / "databricks"
        extension.mkdir(parents=True)
        (extension / "extension.mjs").write_text("")
        settings = {
            "model": "m",
            "extraKnownMarketplaces": {
                self.LEGACY_MARKETPLACE: {
                    "source": {"source": "directory", "path": path or str(root)}
                }
            },
            "enabledPlugins": {self.LEGACY_KEY: True},
        }
        plugin.copilot_settings_path().parent.mkdir(parents=True, exist_ok=True)
        plugin.copilot_settings_path().write_text(json.dumps(settings))

    def test_an_earlier_install_counts_as_installed(self, home):
        self._legacy_install(plugin.plugin_root())

        assert plugin.status() == "installed"

    def test_install_moves_an_earlier_install_to_the_current_id(self, home):
        root = plugin.plugin_root()
        self._legacy_install(root)

        plugin.install(_config())

        settings = json.loads(plugin.copilot_settings_path().read_text())
        assert settings["model"] == "m"
        assert list(settings["extraKnownMarketplaces"]) == [plugin.MARKETPLACE]
        assert settings["enabledPlugins"] == {plugin.PLUGIN_KEY: True}
        assert not (root / "plugins" / "databricks").exists()
        assert plugin.status() == "installed"

    def test_remove_takes_out_an_earlier_install(self, home):
        self._legacy_install(plugin.plugin_root())

        assert plugin.remove(wait=lambda _: None)

        assert json.loads(plugin.copilot_settings_path().read_text()) == {"model": "m"}
        assert not plugin.plugin_root().exists()

    def test_a_deferred_remove_marks_an_earlier_installs_extension(self, home):
        root = plugin.plugin_root()
        self._legacy_install(root)
        config = root / "plugins" / "databricks" / "extensions" / "databricks" / "config.json"
        config.write_text("{}")

        assert plugin.defer_remove()

        assert json.loads(config.read_text())["remove"] is True
        assert plugin.status() == "removal_pending"

    def test_an_earlier_id_at_another_path_isnt_ugs(self, home):
        self._legacy_install(plugin.plugin_root(), path="/elsewhere")

        plugin.install(_config())
        plugin.remove(wait=lambda _: None)

        settings = json.loads(plugin.copilot_settings_path().read_text())
        assert settings["enabledPlugins"] == {self.LEGACY_KEY: True}
        assert settings["extraKnownMarketplaces"][self.LEGACY_MARKETPLACE]["source"]["path"] == (
            "/elsewhere"
        )


@pytest.mark.parametrize("key", ["extraKnownMarketplaces", "enabledPlugins"])
def test_install_refuses_a_non_object_key_before_writing_the_plugin(home, key):
    path = plugin.copilot_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({key: []}))

    with pytest.raises(plugin.SettingsError, match=key):
        plugin.install(_config())

    assert not plugin.plugin_root().exists()
    assert json.loads(path.read_text()) == {key: []}


def test_install_leaves_another_marketplace_of_its_name_alone(home):
    path = plugin.copilot_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    foreign = {"source": {"source": "directory", "path": "/elsewhere"}}
    path.write_text(json.dumps({"extraKnownMarketplaces": {plugin.MARKETPLACE: foreign}}))

    with pytest.raises(plugin.SettingsError, match="another plugin marketplace"):
        plugin.install(_config())

    assert not plugin.plugin_root().exists()
    assert json.loads(path.read_text())["extraKnownMarketplaces"] == {plugin.MARKETPLACE: foreign}


class TestStagedWrite:
    def _extension(self, root: Path) -> Path:
        return root / "plugins" / plugin.PLUGIN / "extensions" / plugin.EXTENSION

    def _siblings(self, root: Path) -> list[str]:
        return sorted(p.name for p in root.parent.iterdir() if p.name.startswith(f".{root.name}"))

    def test_a_reinstall_swaps_in_a_new_tree_and_keeps_the_extensions_state(self, home):
        root = plugin.install(_config())
        (self._extension(root) / "app-restart.json").write_text('{"digest": "d"}')
        (self._extension(root) / "app-sync.lock").write_text("owner")

        plugin.install(_config(profile="other"))

        config = json.loads((self._extension(root) / "config.json").read_text())
        assert config["tokenCommand"][-1] == "other"
        assert (self._extension(root) / "app-restart.json").read_text() == '{"digest": "d"}'
        assert not (self._extension(root) / "app-sync.lock").exists()
        assert (root / "created-settings").is_file()
        assert self._siblings(root) == []

    def test_a_failed_write_leaves_the_installed_plugin_and_settings_alone(self, home, monkeypatch):
        root = plugin.install(_config())
        before = (self._extension(root) / "config.json").read_text()
        settings = plugin.copilot_settings_path().read_text()
        copies = []

        real = shutil.copyfile

        def failing_copy(src, dst):
            copies.append(dst)
            if len(copies) == 2:
                raise OSError("disk full")
            return real(src, dst)

        monkeypatch.setattr(plugin.shutil, "copyfile", failing_copy)

        with pytest.raises(OSError, match="disk full"):
            plugin.install(_config(profile="other"))

        assert (self._extension(root) / "config.json").read_text() == before
        assert plugin.copilot_settings_path().read_text() == settings
        assert self._siblings(root) == []

    def test_a_first_install_that_fails_writes_no_settings(self, home, monkeypatch):
        def fail(staged, root):
            raise OSError("busy")

        monkeypatch.setattr(plugin, "_publish", fail)

        with pytest.raises(OSError, match="busy"):
            plugin.install(_config())

        assert not plugin.copilot_settings_path().exists()
        assert not plugin.plugin_root().exists()

    def test_a_failed_swap_puts_the_old_plugin_back(self, home, monkeypatch):
        root = plugin.install(_config())
        before = (self._extension(root) / "config.json").read_text()
        real = os.replace

        def replace(src, dst):
            if ".staging-" in Path(src).name:
                raise PermissionError("in use")
            return real(src, dst)

        monkeypatch.setattr(plugin.os, "replace", replace)

        with pytest.raises(PermissionError):
            plugin.write_plugin(_config(profile="other"))

        assert (self._extension(root) / "config.json").read_text() == before
        assert self._siblings(root) == []

    def test_a_crashed_runs_leftovers_are_removed_once_old(self, home):
        root = plugin.plugin_root()
        root.parent.mkdir(parents=True, exist_ok=True)
        old = root.with_name(f".{root.name}.staging-old")
        fresh = root.with_name(f".{root.name}.replaced-fresh")
        for leftover in (old, fresh):
            (leftover / "x").mkdir(parents=True)
        stale = time.time() - 3600
        os.utime(old, (stale, stale))

        plugin.write_plugin(_config())

        assert not old.exists()
        assert fresh.exists()


class TestSyncLock:
    def _lock(self, root: Path) -> Path:
        return root / "plugins" / plugin.PLUGIN / "extensions" / plugin.EXTENSION / "app-sync.lock"

    def test_holds_the_extensions_lock_and_releases_only_its_own(self, home):
        root = plugin.install(_config())

        with plugin.sync_lock() as held:
            assert held
            owner = self._lock(root).read_text()
            assert owner.startswith("ug-")

        assert not self._lock(root).exists()

    def test_waits_for_an_extension_then_gives_up(self, home):
        root = plugin.install(_config())
        self._lock(root).write_text("1234-extension")
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        with plugin.sync_lock(timeout=1.0, sleep=sleep, clock=lambda: clock[0]) as held:
            assert not held

        assert self._lock(root).read_text() == "1234-extension"
        assert clock[0] >= 1.0

    def test_takes_over_a_stale_lock(self, home):
        root = plugin.install(_config())
        self._lock(root).write_text("1234-dead")
        stale = time.time() - 120
        os.utime(self._lock(root), (stale, stale))

        with plugin.sync_lock(timeout=0) as held:
            assert held

    @pytest.mark.skipif(NODE is None, reason="node is not installed")
    def test_the_extension_sees_ugs_lock(self, home):
        root = plugin.install(_config())
        rows = (
            root / "plugins" / plugin.PLUGIN / "extensions" / plugin.EXTENSION / "app-rows.mjs"
        ).as_uri()
        script = (
            f"import {{ tryLock }} from {json.dumps(rows)};"
            "console.log(tryLock(process.argv[1], Date.now) === null);"
        )
        with plugin.sync_lock() as held:
            assert held
            result = subprocess.run(
                [NODE, "--input-type=module", "-e", script, str(self._lock(root))],
                capture_output=True,
                text=True,
                check=True,
            )
        assert result.stdout.strip() == "true"

    def test_without_a_plugin_it_holds_nothing(self, home):
        with plugin.sync_lock() as held:
            assert not held
