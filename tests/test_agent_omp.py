"""Tests for agents/omp.py."""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest
import yaml

from ucode.agents import LaunchOptions, omp

WS = "https://example.databricks.com"


def _base_urls() -> dict[str, str]:
    # Native API per family — see agents/omp.py docstring for path conventions.
    return {
        "claude": f"{WS}/ai-gateway/anthropic",
        "openai": f"{WS}/ai-gateway/codex/v1",
        "gemini": f"{WS}/ai-gateway/gemini/v1beta",
    }


def _empty() -> dict:
    """No-models input bundle for render_overlay."""
    return {
        "claude_models": {},
        "codex_models": [],
        "gemini_models": [],
    }


def _overlay(model: str, api_key: str = "tok", **kwargs):
    """Wrapper to call render_overlay with sensible defaults so tests stay terse."""
    bundle = {**_empty(), **kwargs}
    return omp.render_overlay(
        model,
        api_key,
        _base_urls(),
        bundle["claude_models"],
        bundle["codex_models"],
        bundle["gemini_models"],
    )


class TestOmpSpec:
    def test_binary(self):
        assert omp.SPEC["binary"] == "omp"

    def test_package(self):
        assert omp.SPEC["package"] == "@oh-my-pi/pi-coding-agent"

    def test_display(self):
        assert omp.SPEC["display"] == "Oh My Pi"

    def test_config_path_is_models_yml_under_omp_agent_dir(self):
        assert omp.SPEC["config_path"].name == "models.yml"
        assert omp.SPEC["config_path"].parent.name == "agent"
        assert omp.OMP_UCODE_HOME in omp.SPEC["config_path"].parents
        # Isolated from pi's home so the two agents never share config.
        assert "omp-home" in omp.SPEC["config_path"].parts
        assert "pi-home" not in omp.SPEC["config_path"].parts


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

    def test_overlay_carries_no_model_key(self):
        # omp's models.yml schema rejects unknown root keys; the default model
        # is pinned in config.yml instead (see TestWriteDefaultModel).
        overlay, _ = _overlay(
            "claude-sonnet",
            claude_models={"sonnet": "claude-sonnet"},
        )
        assert "model" not in overlay


class TestRenderOverlayUserAgent:
    def test_user_agent_set_on_all_three_providers(self, monkeypatch):
        monkeypatch.setattr(omp, "ug_version", lambda: "0.1.0")
        monkeypatch.setattr(omp, "agent_version", lambda binary: "18.1.13")
        overlay, _ = _overlay(
            "claude-sonnet",
            claude_models={"sonnet": "claude-sonnet"},
            codex_models=["gpt-5"],
            gemini_models=["gemini-2"],
        )
        expected = "ucode/0.1.0 omp/18.1.13"
        for name in ("databricks-claude", "databricks-openai", "databricks-gemini"):
            assert overlay["providers"][name]["headers"]["User-Agent"] == expected


class TestRenderOverlayCompatFlags:
    def test_claude_disables_eager_tool_input_streaming(self):
        # Gateway's Anthropic translator rejects per-tool
        # `eager_input_streaming`; this flag makes omp send the legacy beta
        # header instead.
        overlay, _ = _overlay("claude-sonnet", claude_models={"sonnet": "claude-sonnet"})
        compat = overlay["providers"]["databricks-claude"]["compat"]
        assert compat["supportsEagerToolInputStreaming"] is False

    def test_openai_and_gemini_have_no_compat_flags(self):
        # Their gateway routes accept omp's request shape as-is.
        overlay, _ = _overlay(
            "gpt-5",
            codex_models=["gpt-5"],
            gemini_models=["gemini-2"],
        )
        assert "compat" not in overlay["providers"]["databricks-openai"]
        assert "compat" not in overlay["providers"]["databricks-gemini"]


class TestRenderOverlayAuthAndModels:
    def test_api_key_is_the_auth_token_command(self):
        # omp runs a leading-`!` value as a command and uses its stdout, so the
        # token is minted on demand instead of being baked into models.yml.
        overlay, _ = _overlay(
            "claude-sonnet",
            api_key="!ug auth-token --host https://example.databricks.com",
            claude_models={"sonnet": "claude-sonnet"},
        )
        api_key = overlay["providers"]["databricks-claude"]["apiKey"]
        assert api_key.startswith("!")
        assert "auth-token" in api_key
        assert "https://example.databricks.com" in api_key

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
    def test_managed_keys_exclude_model(self):
        _, keys = _overlay("foo")
        assert ["model"] not in keys

    def test_managed_keys_include_each_provider_present(self):
        _, keys = _overlay(
            "claude-sonnet",
            claude_models={"sonnet": "claude-sonnet"},
            codex_models=["gpt-5"],
            gemini_models=["gemini-2"],
        )
        for name in ("databricks-claude", "databricks-openai", "databricks-gemini"):
            assert ["providers", name] in keys


class TestResolveModelSelector:
    def test_prefixes_claude_model(self):
        selector = omp._resolve_model_selector("claude-sonnet", {"sonnet": "claude-sonnet"}, [], [])
        assert selector == "databricks-claude/claude-sonnet"

    def test_prefixes_openai_model(self):
        selector = omp._resolve_model_selector("gpt-5", {}, ["gpt-5"], [])
        assert selector == "databricks-openai/gpt-5"

    def test_prefixes_gemini_model(self):
        selector = omp._resolve_model_selector("gemini-2", {}, [], ["gemini-2"])
        assert selector == "databricks-gemini/gemini-2"

    def test_preserves_already_prefixed_model(self):
        selector = omp._resolve_model_selector(
            "databricks-claude/claude-sonnet", {"sonnet": "claude-sonnet"}, [], []
        )
        assert selector == "databricks-claude/claude-sonnet"

    def test_unknown_model_passes_through_unprefixed(self):
        # Lets a user override to whatever omp accepts even if we
        # didn't classify it.
        assert omp._resolve_model_selector("custom/whatever", {}, [], []) == "custom/whatever"


class TestOmpDefaultModel:
    def test_prefers_claude_opus(self):
        state = {"claude_models": {"opus": "o4", "sonnet": "s4", "haiku": "h4"}}
        assert omp.default_model(state) == "o4"

    def test_falls_back_to_sonnet(self):
        state = {"claude_models": {"sonnet": "s4"}}
        assert omp.default_model(state) == "s4"

    def test_falls_back_to_haiku(self):
        state = {"claude_models": {"haiku": "h4"}}
        assert omp.default_model(state) == "h4"

    def test_falls_back_to_codex(self):
        state = {"claude_models": {}, "codex_models": ["gpt-5"]}
        assert omp.default_model(state) == "gpt-5"

    def test_falls_back_to_gemini(self):
        state = {"claude_models": {}, "codex_models": [], "gemini_models": ["gemini-2"]}
        assert omp.default_model(state) == "gemini-2"

    def test_returns_none_when_empty(self):
        assert omp.default_model({}) is None
        assert (
            omp.default_model({"claude_models": {}, "codex_models": [], "gemini_models": []})
            is None
        )


class TestBuildRuntimeEnv:
    def test_sets_private_agent_dir_without_replacing_home(self, monkeypatch):
        monkeypatch.setenv("HOME", "/real-user-home")

        env = omp.build_runtime_env()

        assert env["PI_CODING_AGENT_DIR"] == str(omp.OMP_AGENT_DIR)
        assert env["HOME"] == "/real-user-home"

    def test_sets_no_token_env(self, monkeypatch):
        # omp never reads a token from the environment (no OAUTH_TOKEN-style
        # hook anywhere in its packages); auth is the baked models.yml apiKey.
        # ucode must not add one of its own — whatever the parent shell has
        # is passed through untouched and ignored by omp.
        monkeypatch.delenv("OAUTH_TOKEN", raising=False)
        env = omp.build_runtime_env()
        assert "OAUTH_TOKEN" not in env


class TestOmpValidateCmd:
    def test_starts_with_binary(self):
        cmd = omp.validate_cmd("omp")
        assert cmd[0] == "omp"

    def test_uses_print_flag(self):
        # `--print` puts omp in non-interactive mode; without it the TUI hangs on stdin.
        cmd = omp.validate_cmd("omp")
        assert "--print" in cmd

    def test_has_prompt(self):
        cmd = omp.validate_cmd("omp")
        assert len(cmd) > 2


class TestBuildOmpApiKey:
    def test_prefixes_command_marker(self):
        state = {"workspace": WS}
        api_key = omp.build_omp_api_key(state)
        assert api_key.startswith("!")
        assert "auth-token" in api_key

    def test_quotes_each_argv_token(self):
        # omp runs `!command` values through its embedded shell (bash on every
        # OS), so a workspace URL with shell metacharacters must arrive quoted.
        state = {"workspace": "https://host/with space", "profile": "corp profile"}
        api_key = omp.build_omp_api_key(state)
        assert "'https://host/with space'" in api_key
        assert "'corp profile'" in api_key

    def test_use_pat_is_honored(self):
        assert "--use-pat" not in omp.build_omp_api_key({"workspace": WS})
        assert "--use-pat" in omp.build_omp_api_key({"workspace": WS, "use_pat": True})


class TestWriteToolConfig:
    def _setup(self, tmp_path, monkeypatch):
        import ucode.agents.omp as omp_mod
        import ucode.config_io as config_io_mod

        monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)
        models_file = tmp_path / "models.yml"
        models_backup = tmp_path / "models.backup.yml"
        config_file = tmp_path / "config.yml"
        config_backup = tmp_path / "config.backup.yml"
        mcp_file = tmp_path / "mcp.json"
        mcp_backup = tmp_path / "mcp.backup.json"
        monkeypatch.setattr(omp_mod, "OMP_MODELS_PATH", models_file)
        monkeypatch.setattr(omp_mod, "OMP_MODELS_BACKUP_PATH", models_backup)
        monkeypatch.setattr(omp_mod, "OMP_CONFIG_PATH", config_file)
        monkeypatch.setattr(omp_mod, "OMP_CONFIG_BACKUP_PATH", config_backup)
        monkeypatch.setattr(omp_mod, "OMP_MCP_PATH", mcp_file)
        monkeypatch.setattr(omp_mod, "OMP_MCP_BACKUP_PATH", mcp_backup)
        return omp_mod, models_file, config_file, config_backup

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

    def _read_yaml(self, path):
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_stale_managed_providers_removed_before_merge(self, tmp_path, monkeypatch):
        omp_mod, models_file, _, _ = self._setup(tmp_path, monkeypatch)

        stale = {
            "providers": {
                "databricks-claude": {"old": True},
                "databricks-openai": {"old": True},
                "databricks-gemini": {"old": True},
                "user-provider": {"keep": True},
            }
        }
        models_file.write_text(yaml.safe_dump(stale), encoding="utf-8")

        with (
            patch("ucode.agents.omp.get_databricks_token", return_value="tok"),
            patch("ucode.agents.omp.save_state"),
        ):
            omp_mod.write_tool_config(self._state(), "claude-sonnet", token="tok")

        written = self._read_yaml(models_file)
        providers = written.get("providers", {})
        assert providers.get("databricks-claude") != {"old": True}
        assert "old" not in providers.get("databricks-claude", {})
        assert providers.get("user-provider") == {"keep": True}

    def test_legacy_providers_removed_on_upgrade(self, tmp_path, monkeypatch):
        """Earlier ucode versions wrote `databricks-anthropic`, `databricks-codex`,
        and `databricks-oss` providers. They must be stripped on the next write
        so users don't end up with stale entries pointing at routes that 400."""
        omp_mod, models_file, _, _ = self._setup(tmp_path, monkeypatch)

        models_file.write_text(
            yaml.safe_dump(
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
            patch("ucode.agents.omp.get_databricks_token", return_value="tok"),
            patch("ucode.agents.omp.save_state"),
        ):
            omp_mod.write_tool_config(self._state(), "claude-sonnet", token="tok")

        written_providers = self._read_yaml(models_file).get("providers", {})
        for legacy in ("databricks-anthropic", "databricks-codex", "databricks-oss"):
            assert legacy not in written_providers
        assert "databricks-claude" in written_providers

    def test_config_written_with_command_api_key_and_no_model_key(self, tmp_path, monkeypatch):
        omp_mod, models_file, _, _ = self._setup(tmp_path, monkeypatch)

        with (
            patch("ucode.agents.omp.get_databricks_token", return_value="tok"),
            patch("ucode.agents.omp.save_state"),
        ):
            omp_mod.write_tool_config(self._state(), "claude-sonnet", token="SECRET")

        written = self._read_yaml(models_file)
        api_key = written["providers"]["databricks-claude"]["apiKey"]
        assert api_key.startswith("!")
        assert "auth-token" in api_key
        # The literal token from the one-shot mint must never reach the file.
        assert "SECRET" not in models_file.read_text(encoding="utf-8")
        # omp's models.yml schema rejects unknown root keys.
        assert "model" not in written

    def test_written_config_has_no_yaml_anchors(self, tmp_path, monkeypatch):
        # Sharing one headers dict across providers would make the dumper emit
        # `&id001`/`*id001` into a file users are meant to read and edit.
        omp_mod, models_file, _, _ = self._setup(tmp_path, monkeypatch)

        with patch("ucode.agents.omp.save_state"):
            omp_mod.write_tool_config(
                self._state(codex_models=["gpt-5"], gemini_models=["gemini-2"]),
                "claude-sonnet",
                token="tok",
            )

        text = models_file.read_text(encoding="utf-8")
        assert "&id" not in text
        assert "\n" in text and not any(line.strip().startswith("*") for line in text.splitlines())
        written = self._read_yaml(models_file)
        for name in ("databricks-claude", "databricks-openai", "databricks-gemini"):
            assert written["providers"][name]["headers"]["User-Agent"].startswith("ucode/")

    def test_config_pins_default_role_in_config_yml(self, tmp_path, monkeypatch):
        # Without this, omp's startup resolution falls through to the first
        # available model when an unrelated env var makes a built-in provider
        # look auth-configured. Pinning the default role keeps omp on ours.
        omp_mod, _, config_file, _ = self._setup(tmp_path, monkeypatch)

        with (
            patch("ucode.agents.omp.get_databricks_token", return_value="tok"),
            patch("ucode.agents.omp.save_state"),
        ):
            omp_mod.write_tool_config(self._state(), "claude-sonnet", token="tok")

        config = self._read_yaml(config_file)
        assert config["modelRoles"]["default"] == "databricks-claude/claude-sonnet"

    def test_pre_existing_config_is_backed_up_before_first_write(self, tmp_path, monkeypatch):
        omp_mod, _, config_file, config_backup = self._setup(tmp_path, monkeypatch)

        original = "modelRoles:\n  default: some-other-provider/model\ntheme: titanium\n"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(original, encoding="utf-8")

        with (
            patch("ucode.agents.omp.get_databricks_token", return_value="tok"),
            patch("ucode.agents.omp.save_state"),
        ):
            omp_mod.write_tool_config(self._state(), "claude-sonnet", token="tok")

        assert config_backup.read_text(encoding="utf-8") == original
        # The on-disk config still gets the ucode pin applied via deep_merge.
        merged = self._read_yaml(config_file)
        assert merged["modelRoles"]["default"] == "databricks-claude/claude-sonnet"
        assert merged["theme"] == "titanium"


class TestOmpMcpServerConfig:
    def test_entry_is_exactly_command_and_args(self):
        assert omp.build_mcp_server_entry(["ucode", "mcp-proxy", "--url", "https://x"]) == {
            "command": "ucode",
            "args": ["mcp-proxy", "--url", "https://x"],
        }

    def test_ucode_server_names_match_omp_schema(self, tmp_path, monkeypatch):
        # omp's mcp.json restricts names to ^[a-zA-Z0-9_.-]{1,100}$.
        import ucode.agents.omp as omp_mod

        pattern = re.compile(r"^[a-zA-Z0-9_.-]{1,100}$")
        mcp_file = tmp_path / "mcp.json"
        monkeypatch.setattr(omp_mod, "OMP_MCP_PATH", mcp_file)
        monkeypatch.setattr(omp_mod, "OMP_MCP_BACKUP_PATH", tmp_path / "mcp.backup.json")

        for name in ("databricks-sql", "databricks-skill-registry", "github-mcp"):
            assert pattern.match(name), name
            assert omp_mod.write_mcp_server_config(name, ["ucode", "mcp-proxy"]) is False
            assert omp_mod.remove_mcp_server_config(name) is True

    def test_write_and_remove_round_trip(self, tmp_path, monkeypatch):
        import json

        import ucode.agents.omp as omp_mod

        mcp_file = tmp_path / "mcp.json"
        monkeypatch.setattr(omp_mod, "OMP_MCP_PATH", mcp_file)
        monkeypatch.setattr(omp_mod, "OMP_MCP_BACKUP_PATH", tmp_path / "mcp.backup.json")

        assert omp_mod.write_mcp_server_config("github-mcp", ["ucode", "mcp-proxy"]) is False
        written = json.loads(mcp_file.read_text(encoding="utf-8"))
        assert written["mcpServers"]["github-mcp"] == {
            "command": "ucode",
            "args": ["mcp-proxy"],
        }
        assert omp_mod.write_mcp_server_config("github-mcp", ["ucode", "mcp-proxy"]) is True
        assert omp_mod.remove_mcp_server_config("github-mcp") is True
        assert omp_mod.remove_mcp_server_config("github-mcp") is False


class TestWriteToolConfigBaseUrls:
    def _setup(self, tmp_path, monkeypatch):
        import ucode.agents.omp as omp_mod
        import ucode.config_io as config_io_mod

        monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)
        models_file = tmp_path / "models.yml"
        monkeypatch.setattr(omp_mod, "OMP_MODELS_PATH", models_file)
        monkeypatch.setattr(omp_mod, "OMP_MODELS_BACKUP_PATH", tmp_path / "m.bak")
        monkeypatch.setattr(omp_mod, "OMP_CONFIG_PATH", tmp_path / "config.yml")
        monkeypatch.setattr(omp_mod, "OMP_CONFIG_BACKUP_PATH", tmp_path / "c.bak")
        return omp_mod, models_file

    def _state(self, **overrides) -> dict:
        state = {
            "workspace": WS,
            "claude_models": {"sonnet": "claude-sonnet"},
            "codex_models": [],
            "gemini_models": [],
            "managed_configs": {},
        }
        state.update(overrides)
        return state

    def test_prefers_omp_base_urls_from_state(self, tmp_path, monkeypatch):
        import yaml as yaml_lib

        omp_mod, models_file = self._setup(tmp_path, monkeypatch)
        state_urls = {
            "claude": "https://state.example/anthropic",
            "openai": "https://state.example/codex",
            "gemini": "https://state.example/gemini",
        }

        def _fail(_workspace):
            raise AssertionError("should use state base_urls, not rebuild them")

        monkeypatch.setattr(omp_mod, "build_pi_base_urls", _fail)
        with patch("ucode.agents.omp.save_state"):
            omp_mod.write_tool_config(
                self._state(base_urls={"omp": state_urls}), "claude-sonnet", token="tok"
            )

        written = yaml_lib.safe_load(models_file.read_text(encoding="utf-8"))
        assert written["providers"]["databricks-claude"]["baseUrl"] == state_urls["claude"]

    def test_falls_back_to_building_urls_without_state_key(self, tmp_path, monkeypatch):
        import yaml as yaml_lib

        omp_mod, models_file = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(omp_mod, "build_pi_base_urls", lambda workspace: _base_urls())
        with patch("ucode.agents.omp.save_state"):
            omp_mod.write_tool_config(self._state(), "claude-sonnet", token="tok")

        written = yaml_lib.safe_load(models_file.read_text(encoding="utf-8"))
        assert written["providers"]["databricks-claude"]["baseUrl"] == f"{WS}/ai-gateway/anthropic"


class TestWriteToolConfigEdgeCases:
    def _setup(self, tmp_path, monkeypatch):
        import ucode.agents.omp as omp_mod
        import ucode.config_io as config_io_mod

        monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)
        models_file = tmp_path / "models.yml"
        config_file = tmp_path / "config.yml"
        monkeypatch.setattr(omp_mod, "OMP_MODELS_PATH", models_file)
        monkeypatch.setattr(omp_mod, "OMP_MODELS_BACKUP_PATH", tmp_path / "m.bak")
        monkeypatch.setattr(omp_mod, "OMP_CONFIG_PATH", config_file)
        monkeypatch.setattr(omp_mod, "OMP_CONFIG_BACKUP_PATH", tmp_path / "c.bak")
        return omp_mod, models_file, config_file

    def _state(self, **overrides) -> dict:
        state = {
            "workspace": WS,
            "base_urls": {"omp": _base_urls()},
            "claude_models": {"sonnet": "claude-sonnet"},
            "codex_models": [],
            "gemini_models": [],
            "managed_configs": {},
        }
        state.update(overrides)
        return state

    def test_non_dict_providers_section_is_replaced(self, tmp_path, monkeypatch):
        import yaml as yaml_lib

        omp_mod, models_file, _ = self._setup(tmp_path, monkeypatch)
        models_file.write_text("providers:\n- not-a-mapping\n", encoding="utf-8")

        with patch("ucode.agents.omp.save_state"):
            omp_mod.write_tool_config(self._state(), "claude-sonnet", token="tok")

        written = yaml_lib.safe_load(models_file.read_text(encoding="utf-8"))
        assert isinstance(written["providers"], dict)
        assert "databricks-claude" in written["providers"]

    def test_passthrough_model_pins_verbatim_selector(self, tmp_path, monkeypatch):
        import yaml as yaml_lib

        omp_mod, _, config_file = self._setup(tmp_path, monkeypatch)

        with patch("ucode.agents.omp.save_state"):
            omp_mod.write_tool_config(self._state(), "custom/whatever", token="tok")

        # Unclassified selectors pass through like pi's `model` override and
        # pin verbatim, so a user override still wins the startup default.
        config = yaml_lib.safe_load(config_file.read_text(encoding="utf-8"))
        assert config["modelRoles"]["default"] == "custom/whatever"

    def test_bare_model_name_writes_no_default_role(self, tmp_path, monkeypatch):
        omp_mod, _, config_file = self._setup(tmp_path, monkeypatch)

        with patch("ucode.agents.omp.save_state"):
            omp_mod.write_tool_config(self._state(), "somename", token="tok")

        assert not config_file.exists()

    def test_ignores_pi_managed_keys(self):
        # omp has no managed-config support: pi's admin keys must not leak
        # into omp's model choice even when both tools share a workspace.
        state = {
            "pi_default_model": "admin-chosen",
            "pi_models": ["admin-allowlisted"],
            "claude_models": {"sonnet": "discovered"},
        }
        assert omp.default_model(state) == "discovered"


class TestLaunch:
    def _state(self, **overrides) -> dict:
        state = {
            "workspace": WS,
            "base_urls": {"omp": _base_urls()},
            "claude_models": {"sonnet": "claude-sonnet"},
            "codex_models": [],
            "gemini_models": [],
            "managed_configs": {},
        }
        state.update(overrides)
        return state

    def test_launch_raises_before_spawning_without_models(self, monkeypatch):
        import ucode.agents.omp as omp_mod

        def _no_spawn(*args, **kwargs):
            raise AssertionError("must not spawn a process without a model")

        monkeypatch.setattr(omp_mod.subprocess_cross_os, "popen", _no_spawn)
        try:
            omp_mod.launch({"workspace": WS}, [], options=LaunchOptions())
        except RuntimeError as exc:
            assert "Oh My Pi" in str(exc)
        else:  # pragma: no cover - launch must raise
            raise AssertionError("launch did not raise without models")

    def test_launch_spawns_with_private_agent_dir_and_no_refresher(self, tmp_path, monkeypatch):
        import ucode.agents.omp as omp_mod

        monkeypatch.setattr(omp_mod, "OMP_MODELS_PATH", tmp_path / "models.yml")
        monkeypatch.setattr(omp_mod, "OMP_MODELS_BACKUP_PATH", tmp_path / "models.backup.yml")
        monkeypatch.setattr(omp_mod, "OMP_CONFIG_PATH", tmp_path / "config.yml")
        monkeypatch.setattr(omp_mod, "OMP_CONFIG_BACKUP_PATH", tmp_path / "config.backup.yml")
        monkeypatch.setattr(omp_mod, "save_state", lambda state: state)
        monkeypatch.setattr(
            omp_mod, "get_databricks_token", lambda workspace, profile: "MINTED-SECRET"
        )

        spawned: list[tuple[list[str], dict]] = []

        class _Proc:
            def wait(self):
                return 7

        def _popen(argv, env=None, **kwargs):
            spawned.append((argv, env or {}))
            return _Proc()

        monkeypatch.setattr(omp_mod.subprocess_cross_os, "popen", _popen)

        with pytest.raises(SystemExit) as exit_info:
            omp_mod.launch(self._state(), ["--print", "hi"], options=LaunchOptions())

        assert exit_info.value.code == 7
        argv, env = spawned[0]
        assert argv == ["omp", "--print", "hi"]
        assert env["PI_CODING_AGENT_DIR"] == str(omp_mod.OMP_AGENT_DIR)
        # The token is minted per request by the `!command` apiKey, never exported.
        assert all("MINTED-SECRET" not in value for value in env.values())
        # Launch writes the config exactly once (no refresh thread rewriting it).
        assert (tmp_path / "models.yml").exists()


class TestOmpMcpEdgeCases:
    def _setup(self, tmp_path, monkeypatch):
        import ucode.agents.omp as omp_mod

        mcp_file = tmp_path / "mcp.json"
        monkeypatch.setattr(omp_mod, "OMP_MCP_PATH", mcp_file)
        monkeypatch.setattr(omp_mod, "OMP_MCP_BACKUP_PATH", tmp_path / "mcp.backup.json")
        return omp_mod, mcp_file

    def test_write_resets_non_dict_server_map(self, tmp_path, monkeypatch):
        import json

        omp_mod, mcp_file = self._setup(tmp_path, monkeypatch)
        mcp_file.write_text('{"mcpServers": ["not-a-mapping"]}', encoding="utf-8")

        assert omp_mod.write_mcp_server_config("github-mcp", ["ucode"]) is False

        written = json.loads(mcp_file.read_text(encoding="utf-8"))
        assert written["mcpServers"] == {"github-mcp": {"command": "ucode", "args": []}}

    def test_remove_missing_file_returns_false(self, tmp_path, monkeypatch):
        omp_mod, _ = self._setup(tmp_path, monkeypatch)
        assert omp_mod.remove_mcp_server_config("github-mcp") is False

    def test_remove_non_dict_server_map_returns_false(self, tmp_path, monkeypatch):
        omp_mod, mcp_file = self._setup(tmp_path, monkeypatch)
        mcp_file.write_text('{"mcpServers": ["not-a-mapping"]}', encoding="utf-8")
        assert omp_mod.remove_mcp_server_config("github-mcp") is False


class TestConfigureToolDispatch:
    def test_omp_writes_its_own_config_not_the_opencode_fallback(self, monkeypatch):
        import ucode.agents as agents_mod

        called: list[tuple[dict, str | None]] = []
        state = {"workspace": WS, "managed_configs": {}}
        monkeypatch.setattr(
            agents_mod.omp,
            "write_tool_config",
            lambda s, model, **kw: (called.append((s, model)), (s, "tok"))[1],
        )
        monkeypatch.setattr(
            agents_mod.opencode,
            "write_tool_config",
            lambda *a, **kw: pytest.fail("omp must not fall through to the OpenCode writer"),
        )

        result = agents_mod.configure_tool("omp", state, "claude-sonnet")

        assert result is state
        assert called == [(state, "claude-sonnet")]


class TestWriteUserMcpServers:
    """The batched writer `mcp.py` drives for the workspace-managed path."""

    def _setup(self, tmp_path, monkeypatch):
        import ucode.agents.omp as omp_mod

        mcp_file = tmp_path / "mcp.json"
        monkeypatch.setattr(omp_mod, "OMP_MCP_PATH", mcp_file)
        monkeypatch.setattr(omp_mod, "OMP_MCP_BACKUP_PATH", tmp_path / "mcp.backup.json")
        return omp_mod, mcp_file

    def test_adds_and_reports_only_removed_names(self, tmp_path, monkeypatch):
        import json

        omp_mod, mcp_file = self._setup(tmp_path, monkeypatch)
        mcp_file.write_text(
            json.dumps({"mcpServers": {"keep-me": {"command": "x", "args": []}}}), encoding="utf-8"
        )

        removed = omp_mod.write_user_mcp_servers(
            {"a": {"command": "ug", "args": ["mcp-proxy"]}},
            {"keep-me", "never-there"},
        )

        assert removed == {"keep-me"}
        written = json.loads(mcp_file.read_text(encoding="utf-8"))
        assert written["mcpServers"] == {"a": {"command": "ug", "args": ["mcp-proxy"]}}

    def test_preserves_unrelated_keys_and_developer_entries(self, tmp_path, monkeypatch):
        import json

        omp_mod, mcp_file = self._setup(tmp_path, monkeypatch)
        mcp_file.write_text(
            json.dumps(
                {
                    "$schema": "https://omp.sh/schema/mcp.json",
                    "mcpServers": {"my-own": {"command": "x", "args": ["y"]}},
                }
            ),
            encoding="utf-8",
        )

        omp_mod.write_user_mcp_servers({"added": {"command": "ug", "args": []}}, set())

        written = json.loads(mcp_file.read_text(encoding="utf-8"))
        assert written["$schema"] == "https://omp.sh/schema/mcp.json"
        assert written["mcpServers"]["my-own"] == {"command": "x", "args": ["y"]}
        assert written["mcpServers"]["added"] == {"command": "ug", "args": []}

    def test_missing_file_yields_no_removals(self, tmp_path, monkeypatch):
        omp_mod, _ = self._setup(tmp_path, monkeypatch)
        assert omp_mod.write_user_mcp_servers({}, {"a", "b"}) == set()
