"""Tests for agents/pi.py."""

from __future__ import annotations

import json
import shlex
from unittest.mock import patch

import pytest

from ucode.agents import pi
from ucode.telemetry import client_platform

WS = "https://example.databricks.com"


def _base_urls() -> dict[str, str]:
    # Native API per family — see agents/pi.py docstring for path conventions.
    return {
        "claude": f"{WS}/ai-gateway/anthropic",
        "openai": f"{WS}/ai-gateway/codex/v1",
        "gemini": f"{WS}/ai-gateway/gemini/v1beta",
        "oss": f"{WS}/ai-gateway/mlflow/v1",
    }


def _empty() -> dict:
    """No-models input bundle for render_overlay."""
    return {
        "claude_models": {},
        "codex_models": [],
        "gemini_models": [],
        "oss_models": [],
    }


def _overlay(model: str, token: str = "tok", **kwargs):
    """Wrapper to call render_overlay with sensible defaults so tests stay terse."""
    bundle = {**_empty(), **kwargs}
    return pi.render_overlay(
        model,
        token,
        _base_urls(),
        bundle["claude_models"],
        bundle["codex_models"],
        bundle["gemini_models"],
        bundle["oss_models"],
    )


class TestPiSpec:
    def test_binary(self):
        assert pi.SPEC["binary"] == "pi"

    def test_package(self):
        assert pi.SPEC["package"] == "@earendil-works/pi-coding-agent"

    def test_display(self):
        assert pi.SPEC["display"] == "Pi"

    def test_config_path_under_pi_agent_dir(self):
        assert pi.SPEC["config_path"].name == "models.json"
        assert pi.SPEC["config_path"].parent.name == "agent"
        assert pi.PI_UCODE_HOME in pi.SPEC["config_path"].parents


class TestRenderOverlayProviders:
    def test_no_providers_when_no_models(self):
        overlay, _ = _overlay("foo")
        assert "providers" not in overlay

    def test_claude_provider_uses_anthropic_messages(self):
        overlay, _ = _overlay("claude-sonnet", claude_models={"sonnet": "claude-sonnet"})
        provider = overlay["providers"]["databricks-claude"]
        assert provider["api"] == "anthropic-messages"
        assert provider["baseUrl"] == f"{WS}/ai-gateway/anthropic"

    def test_openai_provider_uses_openai_responses(self):
        overlay, _ = _overlay("gpt-5", codex_models=["gpt-5"])
        provider = overlay["providers"]["databricks-openai"]
        assert provider["api"] == "openai-responses"
        assert provider["baseUrl"] == f"{WS}/ai-gateway/codex/v1"

    def test_gemini_provider_uses_google_generative_ai(self):
        overlay, _ = _overlay("gemini-2", gemini_models=["gemini-2"])
        provider = overlay["providers"]["databricks-gemini"]
        assert provider["api"] == "google-generative-ai"
        assert provider["baseUrl"] == f"{WS}/ai-gateway/gemini/v1beta"

    def test_all_three_providers_when_all_present(self):
        overlay, _ = _overlay(
            "claude-sonnet",
            claude_models={"sonnet": "claude-sonnet"},
            codex_models=["gpt-5"],
            gemini_models=["gemini-2"],
        )
        assert set(overlay["providers"].keys()) == {
            "databricks-claude",
            "databricks-openai",
            "databricks-gemini",
        }

    def test_mlflow_provider_uses_openai_completions(self):
        overlay, keys = _overlay("system.ai.glm-5-3", oss_models=["system.ai.glm-5-3"])
        provider = overlay["providers"]["databricks-mlflow"]
        assert provider["api"] == "openai-completions"
        assert provider["baseUrl"] == f"{WS}/ai-gateway/mlflow/v1"
        assert provider["authHeader"] is True
        assert ["providers", "databricks-mlflow"] in keys
        assert overlay["model"] == "databricks-mlflow/system.ai.glm-5-3"

    def test_no_mlflow_provider_without_oss_models(self):
        overlay, keys = _overlay("gpt-5", codex_models=["gpt-5"])
        assert "databricks-mlflow" not in overlay["providers"]
        assert ["providers", "databricks-mlflow"] not in keys

    def test_render_overlay_without_oss_argument_still_moves_gpt_oss(self):
        # Callers that predate the MLflow provider pass only three model lists.
        overlay, _ = pi.render_overlay(
            "system.ai.gpt-oss-120b",
            "tok",
            _base_urls(),
            {},
            ["system.ai.gpt-oss-120b"],
            [],
        )
        assert set(overlay["providers"]) == {"databricks-mlflow"}


class TestRenderOverlayUserAgent:
    def test_user_agent_set_on_all_three_providers(self, monkeypatch):
        monkeypatch.setattr(pi, "ug_version", lambda: "0.1.0")
        monkeypatch.setattr(pi, "agent_version", lambda binary: "0.74.0")
        overlay, _ = _overlay(
            "claude-sonnet",
            claude_models={"sonnet": "claude-sonnet"},
            codex_models=["gpt-5"],
            gemini_models=["gemini-2"],
        )
        expected = f"ucode/0.1.0 pi/0.74.0 {client_platform()}"
        for name in ("databricks-claude", "databricks-openai", "databricks-gemini"):
            assert overlay["providers"][name]["headers"]["User-Agent"] == expected

    def test_request_tags_header_on_all_providers_when_env_set(self, monkeypatch):
        tags = '{"team":"infra","env":"prod"}'
        monkeypatch.setenv("AI_GATEWAY_REQUEST_TAGS", tags)
        overlay, _ = _overlay(
            "claude-sonnet",
            claude_models={"sonnet": "claude-sonnet"},
            codex_models=["gpt-5"],
            gemini_models=["gemini-2"],
        )
        for name in ("databricks-claude", "databricks-openai", "databricks-gemini"):
            headers = overlay["providers"][name]["headers"]
            assert headers["Databricks-Ai-Gateway-Request-Tags"] == tags

    def test_request_tags_header_absent_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("AI_GATEWAY_REQUEST_TAGS", raising=False)
        overlay, _ = _overlay("claude-sonnet", claude_models={"sonnet": "claude-sonnet"})
        headers = overlay["providers"]["databricks-claude"]["headers"]
        assert "Databricks-Ai-Gateway-Request-Tags" not in headers


class TestRenderOverlayCompatFlags:
    def test_claude_disables_eager_tool_input_streaming(self):
        # Gateway's Anthropic translator rejects per-tool
        # `eager_input_streaming`; this flag makes pi send the legacy beta
        # header instead.
        overlay, _ = _overlay("claude-sonnet", claude_models={"sonnet": "claude-sonnet"})
        compat = overlay["providers"]["databricks-claude"]["compat"]
        assert compat["supportsEagerToolInputStreaming"] is False

    def test_openai_and_gemini_have_no_compat_flags(self):
        # Their gateway routes accept pi's request shape as-is.
        overlay, _ = _overlay(
            "gpt-5",
            codex_models=["gpt-5"],
            gemini_models=["gemini-2"],
        )
        assert "compat" not in overlay["providers"]["databricks-openai"]
        assert "compat" not in overlay["providers"]["databricks-gemini"]

    def test_mlflow_omits_the_openai_fields_mlflow_models_reject(self):
        # gpt-oss, Qwen and Llama reject `store`, `max_completion_tokens` and
        # `prompt_cache_retention` ("json: unknown field"); Qwen rejects the developer role.
        overlay, _ = _overlay("system.ai.kimi-k3", oss_models=["system.ai.kimi-k3"])
        assert overlay["providers"]["databricks-mlflow"]["compat"] == {
            "supportsStore": False,
            "supportsStrictMode": False,
            "maxTokensField": "max_tokens",
            "supportsDeveloperRole": False,
            "supportsLongCacheRetention": False,
        }


class TestRenderOverlayAuthAndModels:
    def test_api_key_config_value_embedded_verbatim(self):
        # render_overlay takes a pi config value, so it must not reinterpret it.
        overlay, _ = _overlay(
            "claude-sonnet", token="!mint.sh", claude_models={"sonnet": "claude-sonnet"}
        )
        assert overlay["providers"]["databricks-claude"]["apiKey"] == "!mint.sh"

    def test_auth_header_flag_set_on_all_providers(self):
        overlay, _ = _overlay(
            "claude-sonnet",
            claude_models={"sonnet": "claude-sonnet"},
            codex_models=["gpt-5"],
            gemini_models=["gemini-2"],
        )
        for name in ("databricks-claude", "databricks-openai", "databricks-gemini"):
            assert overlay["providers"][name]["authHeader"] is True

    def test_claude_models_listed(self):
        claude_models = {"opus": "claude-opus", "sonnet": "claude-sonnet"}
        overlay, _ = _overlay("claude-sonnet", claude_models=claude_models)
        ids = {m["id"] for m in overlay["providers"]["databricks-claude"]["models"]}
        assert ids == {"claude-opus", "claude-sonnet"}

    def test_openai_models_listed(self):
        overlay, _ = _overlay("gpt-5", codex_models=["gpt-5", "gpt-5-mini"])
        ids = {m["id"] for m in overlay["providers"]["databricks-openai"]["models"]}
        assert ids == {"gpt-5", "gpt-5-mini"}

    def test_gemini_models_listed(self):
        overlay, _ = _overlay("gemini-2", gemini_models=["gemini-2", "gemini-2-pro"])
        ids = {m["id"] for m in overlay["providers"]["databricks-gemini"]["models"]}
        assert ids == {"gemini-2", "gemini-2-pro"}


class TestRenderOverlayManagedKeys:
    def test_managed_keys_include_model(self):
        _, keys = _overlay("foo")
        assert ["model"] in keys

    def test_managed_keys_include_each_provider_present(self):
        _, keys = _overlay(
            "claude-sonnet",
            claude_models={"sonnet": "claude-sonnet"},
            codex_models=["gpt-5"],
            gemini_models=["gemini-2"],
        )
        for name in ("databricks-claude", "databricks-openai", "databricks-gemini"):
            assert ["providers", name] in keys


class TestRenderOverlayModelSelector:
    def test_prefixes_claude_model(self):
        overlay, _ = _overlay("claude-sonnet", claude_models={"sonnet": "claude-sonnet"})
        assert overlay["model"] == "databricks-claude/claude-sonnet"

    def test_prefixes_openai_model(self):
        overlay, _ = _overlay("gpt-5", codex_models=["gpt-5"])
        assert overlay["model"] == "databricks-openai/gpt-5"

    def test_prefixes_gemini_model(self):
        overlay, _ = _overlay("gemini-2", gemini_models=["gemini-2"])
        assert overlay["model"] == "databricks-gemini/gemini-2"

    def test_preserves_already_prefixed_model(self):
        overlay, _ = _overlay(
            "databricks-claude/claude-sonnet",
            claude_models={"sonnet": "claude-sonnet"},
        )
        assert overlay["model"] == "databricks-claude/claude-sonnet"

    def test_unknown_model_passes_through_unprefixed(self):
        # Lets a user override `model` to whatever pi accepts even if we
        # didn't classify it.
        overlay, _ = _overlay("custom/whatever")
        assert overlay["model"] == "custom/whatever"


class TestPiDefaultModel:
    def test_prefers_claude_opus(self):
        state = {"claude_models": {"opus": "o4", "sonnet": "s4", "haiku": "h4"}}
        assert pi.default_model(state) == "o4"

    def test_falls_back_to_sonnet(self):
        state = {"claude_models": {"sonnet": "s4"}}
        assert pi.default_model(state) == "s4"

    def test_falls_back_to_haiku(self):
        state = {"claude_models": {"haiku": "h4"}}
        assert pi.default_model(state) == "h4"

    def test_falls_back_to_codex(self):
        state = {"claude_models": {}, "codex_models": ["gpt-5"]}
        assert pi.default_model(state) == "gpt-5"

    def test_falls_back_to_gemini(self):
        state = {"claude_models": {}, "codex_models": [], "gemini_models": ["gemini-2"]}
        assert pi.default_model(state) == "gemini-2"

    def test_falls_back_to_mlflow_models_last(self):
        state = {"codex_models": ["system.ai.gpt-oss-120b"], "oss_models": ["system.ai.glm-5-3"]}
        assert pi.default_model(state) == "system.ai.glm-5-3"
        assert pi.default_model({**state, "gemini_models": ["gemini-2"]}) == "gemini-2"
        assert pi.default_model({"codex_models": ["system.ai.gpt-oss-20b"]}) == (
            "system.ai.gpt-oss-20b"
        )

    def test_returns_none_when_empty(self):
        assert pi.default_model({}) is None
        assert (
            pi.default_model({"claude_models": {}, "codex_models": [], "gemini_models": []}) is None
        )


class TestBuildRuntimeEnv:
    def test_sets_oauth_token(self):
        env = pi.build_runtime_env("tok")
        assert env["OAUTH_TOKEN"] == "tok"

    def test_sets_private_agent_dir_without_replacing_home(self, monkeypatch):
        monkeypatch.setenv("HOME", "/real-user-home")

        env = pi.build_runtime_env("tok")

        assert env["PI_CODING_AGENT_DIR"] == str(pi.PI_CONFIG_DIR)
        assert env["HOME"] == "/real-user-home"


class TestWriteToolConfig:
    def _setup(self, tmp_path, monkeypatch):
        import ucode.agents.pi as pi_mod
        import ucode.config_io as config_io_mod

        monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)
        config_file = tmp_path / "models.json"
        backup_file = tmp_path / "pi-backup.json"
        settings_file = tmp_path / "settings.json"
        settings_backup_file = tmp_path / "pi-settings-backup.json"
        monkeypatch.setattr(pi_mod, "PI_CONFIG_PATH", config_file)
        monkeypatch.setattr(pi_mod, "PI_SETTINGS_PATH", settings_file)
        monkeypatch.setattr(pi_mod, "PI_BACKUP_PATH", backup_file)
        monkeypatch.setattr(pi_mod, "PI_SETTINGS_BACKUP_PATH", settings_backup_file)
        return pi_mod, config_file, settings_file, settings_backup_file

    def _state(self, **overrides) -> dict:
        state = {
            "workspace": WS,
            "base_urls": {"pi": _base_urls()},
            "claude_models": {"sonnet": "claude-sonnet"},
            "codex_models": [],
            "gemini_models": [],
            "managed_configs": {},
        }
        state.update(overrides)
        return state

    def test_stale_managed_providers_removed_before_merge(self, tmp_path, monkeypatch):
        pi_mod, config_file, _, _ = self._setup(tmp_path, monkeypatch)

        stale = {
            "providers": {
                "databricks-claude": {"old": True},
                "databricks-openai": {"old": True},
                "databricks-gemini": {"old": True},
                "user-provider": {"keep": True},
            }
        }
        config_file.write_text(json.dumps(stale), encoding="utf-8")

        with (
            patch("ucode.agents.pi.get_databricks_token", return_value="tok"),
            patch("ucode.agents.pi.save_state"),
        ):
            pi_mod.write_tool_config(self._state(), "claude-sonnet", token="tok")

        written = json.loads(config_file.read_text())
        providers = written.get("providers", {})
        assert providers.get("databricks-claude") != {"old": True}
        assert "old" not in providers.get("databricks-claude", {})
        assert providers.get("user-provider") == {"keep": True}

    def test_legacy_providers_removed_on_upgrade(self, tmp_path, monkeypatch):
        """Earlier ucode versions wrote `databricks-anthropic`, `databricks-codex`,
        and `databricks-oss` providers. They must be stripped on the next write
        so users don't end up with stale entries pointing at routes that 400."""
        pi_mod, config_file, _, _ = self._setup(tmp_path, monkeypatch)

        config_file.write_text(
            json.dumps(
                {
                    "providers": {
                        "databricks-anthropic": {"api": "anthropic-messages"},
                        "databricks-codex": {"api": "openai-responses"},
                        "databricks-oss": {"api": "openai-completions"},
                    }
                }
            ),
            encoding="utf-8",
        )

        with (
            patch("ucode.agents.pi.get_databricks_token", return_value="tok"),
            patch("ucode.agents.pi.save_state"),
        ):
            pi_mod.write_tool_config(self._state(), "claude-sonnet", token="tok")

        written_providers = json.loads(config_file.read_text()).get("providers", {})
        for legacy in ("databricks-anthropic", "databricks-codex", "databricks-oss"):
            assert legacy not in written_providers
        assert "databricks-claude" in written_providers

    def test_config_written_with_correct_model_and_auth_command(self, tmp_path, monkeypatch):
        pi_mod, config_file, _, _ = self._setup(tmp_path, monkeypatch)

        with (
            patch("ucode.agents.pi.get_databricks_token", return_value="tok"),
            patch("ucode.agents.pi.save_state"),
        ):
            pi_mod.write_tool_config(self._state(), "claude-sonnet", token="tok")

        written = json.loads(config_file.read_text())
        assert written["model"] == "databricks-claude/claude-sonnet"
        api_key = written["providers"]["databricks-claude"]["apiKey"]
        assert api_key.startswith("!")
        assert "auth-token" in api_key

    def test_bearer_never_written_to_the_config(self, tmp_path, monkeypatch):
        # The whole point: a token on disk is a token that can go stale, and pi
        # resolves the command per request instead.
        pi_mod, config_file, _, _ = self._setup(tmp_path, monkeypatch)

        with (
            patch("ucode.agents.pi.get_databricks_token", return_value="dapi-secret"),
            patch("ucode.agents.pi.save_state"),
        ):
            pi_mod.write_tool_config(self._state(), "claude-sonnet")

        assert "dapi-secret" not in config_file.read_text()

    def test_every_provider_gets_the_auth_command(self, tmp_path, monkeypatch):
        pi_mod, config_file, _, _ = self._setup(tmp_path, monkeypatch)
        state = self._state(
            codex_models=["gpt-5"], gemini_models=["gemini-2"], oss_models=["system.ai.glm-5-2"]
        )

        with (
            patch("ucode.agents.pi.get_databricks_token", return_value="tok"),
            patch("ucode.agents.pi.save_state"),
        ):
            pi_mod.write_tool_config(state, "claude-sonnet", token="tok")

        providers = json.loads(config_file.read_text())["providers"]
        for name in pi_mod.PROVIDER_NAMES:
            assert providers[name]["apiKey"].startswith("!"), name

    def test_discovered_oss_and_gpt_oss_land_on_the_mlflow_provider(self, tmp_path, monkeypatch):
        pi_mod, config_file, _, _ = self._setup(tmp_path, monkeypatch)
        state = self._state(
            codex_models=["system.ai.gpt-5", "system.ai.gpt-oss-120b"],
            oss_models=["system.ai.kimi-k3"],
        )

        with patch("ucode.agents.pi.save_state"):
            pi_mod.write_tool_config(state, "claude-sonnet", token="tok")

        providers = json.loads(config_file.read_text())["providers"]
        assert [m["id"] for m in providers["databricks-mlflow"]["models"]] == [
            "system.ai.kimi-k3",
            "system.ai.gpt-oss-120b",
        ]
        assert [m["id"] for m in providers["databricks-openai"]["models"]] == ["system.ai.gpt-5"]

    def test_saved_base_urls_without_an_mlflow_entry_get_the_workspace_route(
        self, tmp_path, monkeypatch
    ):
        # State saved before the MLflow provider existed has only the three older routes.
        pi_mod, config_file, _, _ = self._setup(tmp_path, monkeypatch)
        saved = {k: v for k, v in _base_urls().items() if k != "oss"}
        state = self._state(base_urls={"pi": saved}, oss_models=["system.ai.glm-5-3"])

        with patch("ucode.agents.pi.save_state"):
            pi_mod.write_tool_config(state, "claude-sonnet", token="tok")

        providers = json.loads(config_file.read_text())["providers"]
        assert providers["databricks-mlflow"]["baseUrl"] == f"{WS}/ai-gateway/mlflow/v1"
        assert providers["databricks-claude"]["baseUrl"] == saved["claude"]

    def test_settings_pins_default_provider_and_model(self, tmp_path, monkeypatch):
        # Without this, Pi's `findInitialModel` can fall through to a built-in
        # provider when an unrelated env var (e.g. HF_TOKEN) makes one look
        # auth-configured. Pinning the default keeps Pi on our provider.
        pi_mod, _, settings_file, _ = self._setup(tmp_path, monkeypatch)

        with (
            patch("ucode.agents.pi.get_databricks_token", return_value="tok"),
            patch("ucode.agents.pi.save_state"),
        ):
            pi_mod.write_tool_config(self._state(), "claude-sonnet", token="tok")

        settings = json.loads(settings_file.read_text())
        assert settings["defaultProvider"] == "databricks-claude"
        assert settings["defaultModel"] == "claude-sonnet"

    def test_pre_existing_settings_are_backed_up_before_first_write(self, tmp_path, monkeypatch):
        pi_mod, _, settings_file, settings_backup_file = self._setup(tmp_path, monkeypatch)

        original = '{"theme": "Default Dark", "defaultProvider": "openai"}'
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(original, encoding="utf-8")

        with (
            patch("ucode.agents.pi.get_databricks_token", return_value="tok"),
            patch("ucode.agents.pi.save_state"),
        ):
            pi_mod.write_tool_config(self._state(), "claude-sonnet", token="tok")

        assert settings_backup_file.read_text(encoding="utf-8") == original
        # The on-disk settings still get the ucode pin applied via deep_merge.
        merged = json.loads(settings_file.read_text())
        assert merged["defaultProvider"] == "databricks-claude"
        assert merged["theme"] == "Default Dark"


class TestManagedModels:
    """A managed config's models arrive as `pi_models` and must not come from the shared keys."""

    def test_managed_models_win_over_the_shared_discovery_lists(self):
        state = {
            "pi_models": ["system.ai.claude-opus-4-8"],
            "claude_models": {"opus": "shared-should-not-win"},
        }
        assert pi.default_model(state) == "system.ai.claude-opus-4-8"

    def test_falls_back_to_the_shared_lists_without_a_managed_config(self):
        assert pi.default_model({"claude_models": {"opus": "discovered"}}) == "discovered"

    def test_managed_models_split_into_pis_per_provider_inputs(self):
        # Pi builds one provider block per family, so a flat list has to be classified back out.
        state = {
            "pi_models": [
                "system.ai.claude-opus-4-8",
                "system.ai.gpt-5",
                "system.ai.gemini-3-flash",
            ]
        }
        assert pi._managed_model_families(state) == (
            {"opus": "system.ai.claude-opus-4-8"},
            ["system.ai.gpt-5"],
            ["system.ai.gemini-3-flash"],
            [],
        )

    def test_managed_oss_models_land_on_the_mlflow_input(self):
        state = {"pi_models": ["system.ai.kimi-k3", "system.ai.gpt-oss-20b", "system.ai.gpt-5"]}
        assert pi._managed_model_families(state) == (
            {},
            ["system.ai.gpt-5"],
            [],
            ["system.ai.kimi-k3", "system.ai.gpt-oss-20b"],
        )
        assert pi.default_model(state) == "system.ai.kimi-k3"

    def test_no_split_without_managed_models(self):
        assert pi._managed_model_families({"claude_models": {"opus": "x"}}) is None

    def test_none_when_no_managed_model_is_servable(self):
        # A family discovery doesn't bucket yields no provider. Returning an all-empty tuple
        # would be truthy and suppress the fallback, writing a config with zero providers.
        assert pi._managed_model_families({"pi_models": ["system.ai.qwen35-122b-a10b"]}) is None

    def test_partially_servable_list_still_splits(self):
        families = pi._managed_model_families(
            {"pi_models": ["system.ai.qwen35-122b-a10b", "system.ai.claude-opus-4-8"]}
        )
        assert families == ({"opus": "system.ai.claude-opus-4-8"}, [], [], [])


class TestManagedDefaultModel:
    """A managed config's `pi_default_model` takes priority over the allowlist."""

    def test_pi_default_model_wins_over_allowlist(self):
        state = {
            "pi_default_model": "admin-chosen-default",
            "pi_models": ["system.ai.claude-opus-4-8", "system.ai.gpt-5"],
        }
        assert pi.default_model(state) == "admin-chosen-default"

    def test_falls_back_to_pi_models_without_default(self):
        state = {"pi_models": ["system.ai.claude-opus-4-8"]}
        assert pi.default_model(state) == "system.ai.claude-opus-4-8"


class TestBuildPiApiKey:
    """Pi resolves a leading-`!` config value as a command before every provider
    request, so the apiKey is that command rather than a baked bearer."""

    def test_is_a_pi_command_value_running_auth_token(self, monkeypatch):
        monkeypatch.setattr("ucode.databricks.shutil.which", lambda command: f"/tools/{command}")
        api_key = pi.build_pi_api_key({"workspace": WS})

        assert api_key == f"!/tools/ug auth-token --host {WS}"

    @pytest.mark.parametrize(
        "ug_exe",
        [r"C:\Users\me\.local\bin\ug.EXE", r"C:\Program Files\ug.exe", r"C:\Users\O'Brien\ug.exe"],
    )
    def test_windows_executable_path_survives_bash(self, monkeypatch, ug_exe):
        # Pi runs `!command` through Git Bash on Windows, so the command must be
        # POSIX-quoted: an unquoted `C:\...\ug.EXE` would lose its backslashes.
        monkeypatch.setattr("ucode.databricks.platform.system", lambda: "Windows")
        monkeypatch.setattr("ucode.databricks.shutil.which", lambda command: ug_exe)
        api_key = pi.build_pi_api_key({"workspace": WS, "profile": "my profile", "use_pat": True})

        assert api_key.startswith("!")
        assert shlex.split(api_key[1:]) == [
            ug_exe,
            "auth-token",
            "--host",
            WS,
            "--profile",
            "my profile",
            "--use-pat",
        ]

    def test_omits_force_refresh(self):
        # Pi has no token cache on this path, so --force-refresh would round-trip
        # to the workspace on every turn.
        assert "--force-refresh" not in pi.build_pi_api_key({"workspace": WS})

    def test_passes_the_profile_through(self):
        api_key = pi.build_pi_api_key({"workspace": WS, "profile": "stablebox"})

        assert "--profile stablebox" in api_key

    def test_forwards_use_pat(self):
        assert "--use-pat" in pi.build_pi_api_key({"workspace": WS, "use_pat": True})
        assert "--use-pat" not in pi.build_pi_api_key({"workspace": WS})


# Pi thinking levels each gateway model accepts, from the Databricks reasoning-models doc and then
# Pi's catalog for undocumented models: {Pi level: value sent}. A level absent here is hidden.
_PI_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
_BUDGET = {"off": "off", "minimal": "minimal", "low": "low", "medium": "medium", "high": "high"}
_ADAPTIVE_MAX = {**_BUDGET, "max": "max"}
_ADAPTIVE_XHIGH_MAX = {**_ADAPTIVE_MAX, "xhigh": "xhigh"}
_NO_OFF_NO_MINIMAL = {"low": "low", "medium": "medium", "high": "high"}
_NONE_THROUGH_XHIGH = {"off": "none", **_NO_OFF_NO_MINIMAL, "xhigh": "xhigh"}
_DOCUMENTED_LEVELS = {
    "claude-sonnet-4-5": _BUDGET,
    "claude-opus-4-5": _BUDGET,
    "claude-haiku-4-5": _BUDGET,
    "claude-opus-4-6": _ADAPTIVE_MAX,
    "claude-opus-4-7": _ADAPTIVE_XHIGH_MAX,
    "claude-opus-4-8": _ADAPTIVE_XHIGH_MAX,
    "claude-opus-5": _ADAPTIVE_XHIGH_MAX,
    "claude-opus-5-5": {**_NO_OFF_NO_MINIMAL, "xhigh": "xhigh", "max": "max"},
    "claude-sonnet-4-6": _ADAPTIVE_MAX,
    "claude-sonnet-5": _ADAPTIVE_XHIGH_MAX,
    "claude-sonnet-5-5": {**_NO_OFF_NO_MINIMAL, "xhigh": "xhigh", "max": "max"},
    "claude-fable-5": {k: v for k, v in _ADAPTIVE_XHIGH_MAX.items() if k != "off"},
    "claude-fable-5-1": {k: v for k, v in _ADAPTIVE_XHIGH_MAX.items() if k != "off"},
    # The gateway rejects xhigh and max for Haiku 5.5.
    "claude-haiku-5-5": {"off": "off", **_NO_OFF_NO_MINIMAL},
    # `none` is rejected by the original GPT-5 models, so off stays hidden there.
    "gpt-5": {k: v for k, v in _BUDGET.items() if k != "off"},
    "gpt-5-mini": {k: v for k, v in _BUDGET.items() if k != "off"},
    "gpt-5-nano": {k: v for k, v in _BUDGET.items() if k != "off"},
    "gpt-5-pro": {"high": "high"},
    "gpt-5.1": {"off": "none", **_NO_OFF_NO_MINIMAL},
    "gpt-5.2": _NONE_THROUGH_XHIGH,
    "gpt-5.2-pro": {"medium": "medium", "high": "high", "xhigh": "xhigh"},
    "gpt-5.3-codex": _NONE_THROUGH_XHIGH,
    "gpt-5.3-codex-spark": {**_NO_OFF_NO_MINIMAL, "xhigh": "xhigh"},
    "gpt-5.4": _NONE_THROUGH_XHIGH,
    "gpt-5.4-mini": _NONE_THROUGH_XHIGH,
    "gpt-5.4-nano": _NONE_THROUGH_XHIGH,
    "gpt-5.4-pro": {"medium": "medium", "high": "high", "xhigh": "xhigh"},
    "gpt-5.5": _NONE_THROUGH_XHIGH,
    "gpt-5.5-pro": {"medium": "medium", "high": "high", "xhigh": "xhigh"},
    "gpt-5.6-luna": {**_NONE_THROUGH_XHIGH, "max": "max"},
    "gpt-5.6-sol": {**_NONE_THROUGH_XHIGH, "max": "max"},
    "gpt-5.6-terra": {**_NONE_THROUGH_XHIGH, "max": "max"},
    "gpt-6-astra": {**_NO_OFF_NO_MINIMAL, "xhigh": "xhigh", "max": "max"},
    "gpt-6-luna": {**_NONE_THROUGH_XHIGH, "max": "max"},
    "gpt-6-sol": {**_NONE_THROUGH_XHIGH, "max": "max"},
    "gpt-6.1-sol": {**_NONE_THROUGH_XHIGH, "max": "max"},
    "gpt-6.1-terra": {**_NONE_THROUGH_XHIGH, "max": "max"},
    "gemini-4-argon": _NO_OFF_NO_MINIMAL,
    "gemini-3-8-flash": {"off": "off", **_NO_OFF_NO_MINIMAL},
    "gemini-3-7-flash": {"off": "off", **_NO_OFF_NO_MINIMAL},
    "gemini-3-6-flash": {k: v for k, v in _BUDGET.items() if k != "off"},
    "gemini-3-5-flash": {k: v for k, v in _BUDGET.items() if k != "off"},
    "gemini-3-5-flash-lite": {k: v for k, v in _BUDGET.items() if k != "off"},
    "gemini-3-1-flash-lite": {k: v for k, v in _BUDGET.items() if k != "off"},
    "gemini-3-flash": {k: v for k, v in _BUDGET.items() if k != "off"},
    # The doc lists minimal for 3.1 Pro, but the gateway rejects it.
    "gemini-3-1-pro": _NO_OFF_NO_MINIMAL,
    "gemini-2-5-pro": _BUDGET,
    # MLflow models: hybrid ones turn reasoning off with `none`; levels the doc says fall back to
    # `max` or alias another tier are hidden.
    "glm-5-2": {"off": "none", "high": "high", "max": "max"},
    "glm-5-3": {"low": "low", "high": "high", "max": "max"},
    "glm-5-3-flash": {"low": "low", "high": "high", "max": "max"},
    "kimi-k3": {"off": "none", "low": "low", "high": "high", "max": "max"},
    "kimi-k3-1-pro": {"off": "none", "low": "low", "high": "high", "max": "max"},
    "deepseek-v4-1-flash": {
        "off": "none",
        "low": "low",
        "high": "high",
        "xhigh": "xhigh",
        "max": "max",
    },
    "deepseek-v4-1-flash-1017": {
        "off": "none",
        "low": "low",
        "high": "high",
        "xhigh": "xhigh",
        "max": "max",
    },
    "gpt-oss-120b": _NO_OFF_NO_MINIMAL,
    "gpt-oss-20b": _NO_OFF_NO_MINIMAL,
}


def _effective_levels(level_map: dict | None) -> dict[str, object]:
    """Resolve a thinkingLevelMap the way Pi does: unmapped levels through `high` send their own
    name, unmapped `xhigh`/`max` are hidden, and a `None` value hides the level."""
    level_map = level_map or {}
    resolved: dict[str, object] = {}
    for level in _PI_LEVELS:
        if level in level_map:
            if level_map[level] is not None:
                resolved[level] = level_map[level]
        elif level not in ("xhigh", "max"):
            resolved[level] = level
    return resolved


def _entry(model_id: str) -> dict:
    """The models.json entry Pi gets for one gateway model."""
    if "claude-" in model_id:
        overlay, _ = _overlay(model_id, claude_models={"opus": model_id})
        provider = "databricks-claude"
    elif "gemini-" in model_id:
        overlay, _ = _overlay(model_id, gemini_models=[model_id])
        provider = "databricks-gemini"
    elif "gpt-oss" in model_id.replace(".", "-"):
        # Discovery buckets gpt-oss with GPT; Pi moves it to the MLflow provider.
        overlay, _ = _overlay(model_id, codex_models=[model_id])
        provider = "databricks-mlflow"
    elif any(family in model_id for family in ("kimi-", "glm-", "deepseek-")):
        overlay, _ = _overlay(model_id, oss_models=[model_id])
        provider = "databricks-mlflow"
    else:
        overlay, _ = _overlay(model_id, codex_models=[model_id])
        provider = "databricks-openai"
    (entry,) = overlay["providers"][provider]["models"]
    return entry


class TestModelEntries:
    """Gateway ids aren't in Pi's catalog, so each entry declares what Pi would otherwise default:
    no thinking, text-only input, a 128K context and 16K output."""

    @pytest.mark.parametrize("vendor_id", sorted(_DOCUMENTED_LEVELS))
    @pytest.mark.parametrize("prefix", ["system.ai.", "databricks-"])
    def test_thinking_levels_match_the_documented_levels(self, vendor_id, prefix):
        entry = _entry(prefix + vendor_id.replace(".", "-"))

        assert entry["reasoning"] is True
        assert _effective_levels(entry.get("thinkingLevelMap")) == _DOCUMENTED_LEVELS[vendor_id]

    @pytest.mark.parametrize(
        ("model_id", "context", "output"),
        [
            ("system.ai.claude-opus-5-5", 1_000_000, 128_000),
            ("system.ai.claude-sonnet-4-6", 1_000_000, 128_000),
            ("system.ai.claude-haiku-5-5", 1_000_000, 128_000),
            ("system.ai.claude-fable-5-1", 1_000_000, 128_000),
            ("system.ai.claude-sonnet-4-5", 200_000, 32_000),
            ("system.ai.gpt-6-sol", 1_050_000, 128_000),
            ("system.ai.gpt-6-1-sol", 1_050_000, 128_000),
            ("system.ai.gpt-6-luna", 1_050_000, 128_000),
            ("system.ai.gpt-5-6-terra", 1_050_000, 128_000),
            ("system.ai.gpt-5-5-pro", 1_050_000, 128_000),
            ("system.ai.gpt-5-5", 272_000, 128_000),
            ("system.ai.gpt-5-4-mini", 400_000, 128_000),
            ("system.ai.gpt-5", 400_000, 128_000),
            ("system.ai.gemini-3-1-pro", 1_048_576, 65_536),
            ("system.ai.gemini-3-pro-image", 65_536, 32_768),
            ("system.ai.glm-5-3-flash", 1_048_576, 131_072),
            ("system.ai.glm-5-2", 1_000_000, 131_072),
            ("system.ai.kimi-k3-1-pro", 1_000_000, 131_072),
            ("system.ai.gpt-oss-120b", 128_000, 25_000),
            # Unlisted GLM keeps the shared family limits; other unlisted models get the floor.
            ("system.ai.glm-4-6", 200_000, 25_000),
            ("system.ai.deepseek-v4-1-flash", 128_000, 8_192),
        ],
    )
    def test_limits_are_model_specific(self, model_id, context, output):
        entry = _entry(model_id)

        assert (entry["contextWindow"], entry["maxTokens"]) == (context, output)

    @pytest.mark.parametrize(
        "model_id",
        [
            "system.ai.claude-opus-5-5",
            "system.ai.gpt-6-sol",
            "system.ai.gpt-5",
            "system.ai.gemini-4-argon",
        ],
    )
    def test_image_input_carries_the_gateway_request_cap(self, model_id):
        entry = _entry(model_id)

        assert entry["input"] == ["text", "image"]
        assert entry["inputLimits"] == {
            "maxRequestBytes": 32 * 1024 * 1024,
            "images": {
                "resize": {
                    "maxWidth": 2000,
                    "maxHeight": 2000,
                    "maxBytes": 768 * 1024,
                    "jpegQuality": 80,
                }
            },
        }

    def test_adaptive_claude_models_force_adaptive_thinking(self):
        assert _entry("system.ai.claude-haiku-5-5")["compat"] == {"forceAdaptiveThinking": True}
        assert "compat" not in _entry("system.ai.claude-sonnet-4-5")

    @pytest.mark.parametrize(
        "model_id",
        [
            "codex-mini",
            "system.ai.gpt-6",
            # Undocumented GPT-5 ids must not inherit the original GPT-5 `minimal` level.
            "system.ai.gpt-5-6",
            "gpt-5.6",
            "system.ai.gpt-5-codex",
            "system.ai.gpt-5-7",
        ],
    )
    def test_gpt_models_without_documented_levels_get_limits_only(self, model_id):
        entry = _entry(model_id)

        assert set(entry) == {"id", "contextWindow", "maxTokens"}

    @pytest.mark.parametrize(
        "model_id",
        ["system.ai.gpt-4o", "system.ai.gpt-4o-mini", "system.ai.gpt-4-1", "gpt-4.1-nano"],
    )
    def test_vision_gpt_models_without_reasoning_take_images(self, model_id):
        entry = _entry(model_id)

        assert entry["input"] == ["text", "image"]
        assert entry["inputLimits"]["maxRequestBytes"] == 32 * 1024 * 1024
        assert "reasoning" not in entry
        assert "thinkingLevelMap" not in entry

    @pytest.mark.parametrize(
        "model_id",
        [
            "system.ai.gemini-3-pro-image",
            "system.ai.gemini-3-1-flash-image",
            "system.ai.gemini-5-future",
        ],
    )
    def test_undocumented_and_image_generation_gemini_models_get_limits_only(self, model_id):
        assert set(_entry(model_id)) == {"id", "contextWindow", "maxTokens"}

    @pytest.mark.parametrize("model_id", ["system.ai.glm-6", "system.ai.kimi-k4"])
    def test_undocumented_mlflow_models_get_limits_only(self, model_id):
        # Without a documented level map Pi sends no reasoning_effort, so the model uses its own
        # default instead of a value it might reject.
        assert set(_entry(model_id)) == {"id", "contextWindow", "maxTokens"}

    def test_mlflow_entries_stay_text_only(self):
        # Image input through the MLflow route is unverified, so Pi keeps its text-only default.
        assert "input" not in _entry("system.ai.kimi-k3")

    def test_dotted_ids_resolve_like_dashed_ones(self):
        assert _entry("gemini-3.8-flash")["thinkingLevelMap"] == {"minimal": None}
        assert _entry("gpt-6.1-sol")["contextWindow"] == 1_050_000
        assert (
            _entry("claude-opus-5.5")["thinkingLevelMap"]
            == _entry("claude-opus-5-5")["thinkingLevelMap"]
        )
        assert not pi._serves_claude_model("claude-opus-4.1")


class TestUnservableModels:
    def test_claude_older_than_4_5_is_left_out(self):
        overlay, _ = _overlay(
            "system.ai.claude-opus-4-1",
            claude_models={
                "opus": "system.ai.claude-opus-4-1",
                "sonnet": "system.ai.claude-sonnet-4-5",
            },
        )

        ids = [m["id"] for m in overlay["providers"]["databricks-claude"]["models"]]
        assert ids == ["system.ai.claude-sonnet-4-5"]
        assert overlay["model"] == "system.ai.claude-opus-4-1"

    def test_no_claude_provider_when_only_old_claude(self):
        overlay, keys = _overlay("x", claude_models={"opus": "databricks-claude-opus-4-1"})

        assert "providers" not in overlay
        assert ["providers", "databricks-claude"] not in keys

    def test_gpt_oss_moves_from_the_responses_provider_to_mlflow(self):
        # Its model service declares only the MLflow route.
        overlay, _ = _overlay(
            "system.ai.gpt-oss-120b",
            codex_models=["system.ai.gpt-5", "system.ai.gpt-oss-120b", "system.ai.gpt-oss-20b"],
        )

        ids = [m["id"] for m in overlay["providers"]["databricks-openai"]["models"]]
        assert ids == ["system.ai.gpt-5"]
        mlflow_ids = [m["id"] for m in overlay["providers"]["databricks-mlflow"]["models"]]
        assert mlflow_ids == ["system.ai.gpt-oss-120b", "system.ai.gpt-oss-20b"]
        assert overlay["model"] == "databricks-mlflow/system.ai.gpt-oss-120b"

    def test_gpt_oss_listed_in_both_inputs_appears_once(self):
        overlay, _ = _overlay(
            "system.ai.gpt-oss-20b",
            codex_models=["system.ai.gpt-oss-20b"],
            oss_models=["system.ai.gpt-oss-20b", "system.ai.glm-5-3"],
        )

        ids = [m["id"] for m in overlay["providers"]["databricks-mlflow"]["models"]]
        assert ids == ["system.ai.gpt-oss-20b", "system.ai.glm-5-3"]
        assert "databricks-openai" not in overlay["providers"]

    def test_default_model_skips_unservable_models(self):
        state = {
            "claude_models": {"opus": "system.ai.claude-opus-4-1"},
            "codex_models": ["system.ai.gpt-oss-120b", "system.ai.gpt-5"],
        }
        # gpt-oss is on the MLflow provider, which comes after the GPT models.
        assert pi.default_model(state) == "system.ai.gpt-5"
        assert pi.default_model({"claude_models": {"opus": "system.ai.claude-opus-4-1"}}) is None

    def test_managed_list_skips_unservable_models(self):
        state = {
            "pi_models": [
                "system.ai.claude-opus-4-1",
                "system.ai.gpt-oss-120b",
                "system.ai.claude-opus-5-5",
                "system.ai.gpt-6-sol",
            ]
        }

        assert pi._managed_model_families(state) == (
            {"opus": "system.ai.claude-opus-5-5"},
            ["system.ai.gpt-6-sol"],
            [],
            ["system.ai.gpt-oss-120b"],
        )
        assert pi.default_model(state) == "system.ai.gpt-oss-120b"

    def test_managed_list_of_only_gpt_oss_uses_the_mlflow_provider(self):
        assert pi._managed_model_families({"pi_models": ["system.ai.gpt-oss-120b"]}) == (
            {},
            [],
            [],
            ["system.ai.gpt-oss-120b"],
        )

    def test_managed_gpt_oss_default_is_the_default(self):
        state = {
            "pi_default_model": "system.ai.gpt-oss-120b",
            "pi_models": ["system.ai.gpt-oss-120b", "system.ai.gpt-5"],
        }

        assert pi.default_model(state) == "system.ai.gpt-oss-120b"

    def test_excluded_managed_default_is_not_the_default(self):
        state = {
            "pi_default_model": "system.ai.claude-opus-4-1",
            "pi_models": ["system.ai.claude-opus-4-1", "system.ai.gpt-5"],
        }

        assert pi.default_model(state) == "system.ai.gpt-5"

    @pytest.mark.parametrize(
        "managed",
        [["system.ai.claude-opus-4-1"], ["system.ai.qwen35-122b-a10b"]],
    )
    def test_unservable_managed_list_defaults_to_discovery(self, managed):
        # `_managed_model_families` returns None for these, so the config uses discovery; the
        # default must come from the same lists.
        state = {"pi_models": managed, "claude_models": {"opus": "system.ai.claude-opus-5-5"}}

        assert pi.default_model(state) == "system.ai.claude-opus-5-5"
        assert pi.default_model({"pi_models": managed}) is None


class TestGeminiEnvironment:
    def test_pins_the_gemini_sdk_off_vertex(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

        env = pi.build_runtime_env("tok")

        assert env["GOOGLE_GENAI_USE_VERTEXAI"] == "false"
        # Pi's shell and subprocesses (gcloud, the user's own Vertex code) keep the project.
        assert env["GOOGLE_CLOUD_PROJECT"] == "my-project"
        assert env["GOOGLE_CLOUD_LOCATION"] == "us-central1"

    def test_adds_nothing_when_the_switches_are_unset(self, monkeypatch):
        # A stock environment reaches Pi's shell unchanged.
        monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
        monkeypatch.delenv("GOOGLE_GENAI_USE_ENTERPRISE", raising=False)

        env = pi.build_runtime_env("tok")

        assert "GOOGLE_GENAI_USE_VERTEXAI" not in env
        assert "GOOGLE_GENAI_USE_ENTERPRISE" not in env

    def test_leaves_switches_that_are_off(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "0")
        monkeypatch.setenv("GOOGLE_GENAI_USE_ENTERPRISE", "false")

        env = pi.build_runtime_env("tok")

        assert env["GOOGLE_GENAI_USE_VERTEXAI"] == "0"
        assert env["GOOGLE_GENAI_USE_ENTERPRISE"] == "false"

    def test_turns_off_the_enterprise_switch_when_on(self, monkeypatch):
        # The SDK lets GOOGLE_GENAI_USE_ENTERPRISE outrank GOOGLE_GENAI_USE_VERTEXAI.
        monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
        monkeypatch.setenv("GOOGLE_GENAI_USE_ENTERPRISE", "TRUE")

        env = pi.build_runtime_env("tok")

        assert env["GOOGLE_GENAI_USE_ENTERPRISE"] == "false"
        assert "GOOGLE_GENAI_USE_VERTEXAI" not in env


class TestManagedModelEntries:
    def test_managed_models_get_per_model_entries(self, tmp_path, monkeypatch):
        TestWriteToolConfig()._setup(tmp_path, monkeypatch)
        state = {
            "workspace": WS,
            "base_urls": {"pi": _base_urls()},
            "pi_models": [
                "system.ai.claude-opus-5-5",
                "system.ai.gpt-5",
                "system.ai.gpt-oss-120b",
                "system.ai.gemini-3-1-pro",
            ],
            "claude_models": {"opus": "shared-should-not-win"},
        }

        with patch("ucode.agents.pi.save_state"):
            pi.write_tool_config(state, pi.default_model(state), token="tok")

        providers = json.loads((tmp_path / "models.json").read_text())["providers"]
        by_id = {m["id"]: m for p in providers.values() for m in p["models"]}
        assert set(by_id) == {
            "system.ai.claude-opus-5-5",
            "system.ai.gpt-5",
            "system.ai.gpt-oss-120b",
            "system.ai.gemini-3-1-pro",
        }
        assert [m["id"] for m in providers["databricks-mlflow"]["models"]] == [
            "system.ai.gpt-oss-120b"
        ]
        assert by_id["system.ai.gpt-oss-120b"]["maxTokens"] == 25_000
        assert by_id["system.ai.claude-opus-5-5"]["contextWindow"] == 1_000_000
        assert by_id["system.ai.gpt-5"]["thinkingLevelMap"] == {"off": None}
        assert by_id["system.ai.gemini-3-1-pro"]["reasoning"] is True


class TestExcludedSelectedModel:
    """A managed default or budget recommendation can name a model Pi leaves out."""

    def _write(self, tmp_path, monkeypatch, state: dict, model: str):
        TestWriteToolConfig()._setup(tmp_path, monkeypatch)
        with patch("ucode.agents.pi.save_state"), patch("ucode.agents.pi.print_warning") as warn:
            pi.write_tool_config(
                {"workspace": WS, "base_urls": {"pi": _base_urls()}, **state}, model, token="tok"
            )
        return warn

    def test_old_claude_falls_back_to_the_default_with_a_warning(self, tmp_path, monkeypatch):
        state = {"claude_models": {"opus": "system.ai.claude-opus-5-5"}}

        warn = self._write(tmp_path, monkeypatch, state, "system.ai.claude-opus-4-1")

        written = json.loads((tmp_path / "models.json").read_text())
        settings = json.loads((tmp_path / "settings.json").read_text())
        assert written["model"] == "databricks-claude/system.ai.claude-opus-5-5"
        assert settings["defaultModel"] == "system.ai.claude-opus-5-5"
        warn.assert_called_once()
        assert "system.ai.claude-opus-4-1" in warn.call_args.args[0]
        assert "Starting on system.ai.claude-opus-5-5" in warn.call_args.args[0]

    def test_old_claude_without_another_model_is_an_actionable_error(self, tmp_path, monkeypatch):
        state = {"claude_models": {"opus": "system.ai.claude-opus-4-1"}}

        with pytest.raises(RuntimeError, match="Choose a Claude 4.5 or newer, GPT or Gemini model"):
            self._write(tmp_path, monkeypatch, state, "databricks-claude/system.ai.claude-opus-4-1")

        assert not (tmp_path / "models.json").exists()

    def test_a_served_model_is_kept_without_a_warning(self, tmp_path, monkeypatch):
        state = {"claude_models": {"opus": "system.ai.claude-opus-5-5"}, "codex_models": ["gpt-5"]}

        warn = self._write(tmp_path, monkeypatch, state, "gpt-5")

        assert json.loads((tmp_path / "models.json").read_text())["model"] == (
            "databricks-openai/gpt-5"
        )
        warn.assert_not_called()
