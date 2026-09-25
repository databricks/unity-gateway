"""Tests for `ucode doctor` — check classification and the fix-apply flow."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import ucode.doctor as doctor_mod
from ucode.databricks import MIN_DATABRICKS_CLI_VERSION
from ucode.doctor import (
    Check,
    Suggestion,
    _check_agent_clis,
    _check_anthropic_env_collision,
    _check_databricks_auth,
    _check_databricks_cli,
    _check_npm,
    _check_uv,
    _check_workspace,
    doctor,
)

_WS = "https://ws.example.com"
_CLAUDE_BASE_URL = "https://ws.example.com/ai-gateway/anthropic"
_CODEX_BASE_URL = "https://ws.example.com/ai-gateway/codex/v1"


class TestUvCheck:
    def test_ok_when_present(self):
        with patch.object(doctor_mod.shutil, "which", return_value="/usr/bin/uv"):
            check = _check_uv()
        assert check.status == "ok"
        assert check.suggestion is None

    def test_error_when_missing(self):
        with patch.object(doctor_mod.shutil, "which", return_value=None):
            check = _check_uv()
        assert check.status == "error"


class TestNpmCheck:
    def test_ok_when_present(self):
        with patch.object(doctor_mod.shutil, "which", return_value="/usr/bin/npm"):
            assert _check_npm().status == "ok"

    def test_warn_when_missing(self):
        with patch.object(doctor_mod.shutil, "which", return_value=None):
            assert _check_npm().status == "warn"


class TestDatabricksCliCheck:
    def test_error_and_install_suggestion_when_missing(self):
        with patch.object(doctor_mod.shutil, "which", return_value=None):
            check = _check_databricks_cli()
        assert check.status == "error"
        assert check.suggestion is not None
        assert "Install" in check.suggestion.prompt

    def test_warn_and_upgrade_suggestion_when_below_floor(self):
        # A public-preview build below the aitools floor: recommend an upgrade,
        # not a hard failure.
        old = (MIN_DATABRICKS_CLI_VERSION[0], MIN_DATABRICKS_CLI_VERSION[1] - 1, 0)
        with (
            patch.object(doctor_mod.shutil, "which", return_value="/usr/bin/databricks"),
            patch.object(doctor_mod, "databricks_cli_version", return_value=old),
        ):
            check = _check_databricks_cli()
        assert check.status == "warn"
        assert check.suggestion is not None
        assert "Upgrade" in check.suggestion.prompt

    def test_ok_when_at_or_above_floor(self):
        with (
            patch.object(doctor_mod.shutil, "which", return_value="/usr/bin/databricks"),
            patch.object(
                doctor_mod, "databricks_cli_version", return_value=MIN_DATABRICKS_CLI_VERSION
            ),
        ):
            check = _check_databricks_cli()
        assert check.status == "ok"
        assert check.suggestion is None

    def test_warn_when_version_unreadable(self):
        with (
            patch.object(doctor_mod.shutil, "which", return_value="/usr/bin/databricks"),
            patch.object(doctor_mod, "databricks_cli_version", return_value=None),
        ):
            check = _check_databricks_cli()
        assert check.status == "warn"
        assert check.suggestion is None


class TestWorkspaceCheck:
    def test_ok_when_configured(self):
        with patch.object(doctor_mod, "load_state", return_value={"workspace": "https://ws"}):
            check = _check_workspace()
        assert check.status == "ok"
        assert "https://ws" in check.detail

    def test_warn_when_unconfigured(self):
        with patch.object(doctor_mod, "load_state", return_value={}):
            assert _check_workspace().status == "warn"


class TestAgentCliChecks:
    def test_missing_binary_offers_install(self):
        state = {"available_tools": ["claude"]}
        with (
            patch.object(doctor_mod, "load_state", return_value=state),
            patch.object(doctor_mod, "tool_binary_installed", return_value=False),
        ):
            checks = _check_agent_clis()
        assert len(checks) == 1
        assert checks[0].status == "warn"
        assert checks[0].suggestion is not None

    def test_compatible_agents_are_ok_without_registry_checks(self):
        state = {"available_tools": ["claude", "codex", "opencode", "gemini", "pi", "copilot"]}
        with (
            patch.object(doctor_mod, "load_state", return_value=state),
            patch.object(doctor_mod, "tool_binary_installed", return_value=True),
            patch.object(doctor_mod, "tool_version_error", return_value=None),
            patch("subprocess.run", side_effect=AssertionError("must not query npm")),
        ):
            checks = _check_agent_clis()
        assert len(checks) == 6
        assert all(check.status == "ok" and check.suggestion is None for check in checks)

    def test_only_below_minimum_agent_offers_upgrade(self):
        state = {"available_tools": ["claude", "opencode"]}
        with (
            patch.object(doctor_mod, "load_state", return_value=state),
            patch.object(doctor_mod, "tool_binary_installed", return_value=True),
            patch.object(doctor_mod, "tool_version_error", side_effect=[None, "version too old"]),
            patch.object(doctor_mod, "update_tool_binary", return_value=True) as update,
        ):
            checks = _check_agent_clis()
            assert checks[0].suggestion is None
            assert checks[1].status == "warn"
            assert checks[1].detail == "version too old"
            assert checks[1].suggestion.prompt == "Upgrade OpenCode to meet the required version?"
            assert checks[1].suggestion.apply() is True
        update.assert_called_once_with("opencode")

    def test_unknown_tool_is_skipped(self):
        state = {"available_tools": ["not-a-real-tool"]}
        with patch.object(doctor_mod, "load_state", return_value=state):
            assert _check_agent_clis() == []


class TestDatabricksAuthCheck:
    @pytest.fixture(autouse=True)
    def _clear_bearer_env(self, monkeypatch):
        # CI sets DATABRICKS_BEARER; clear both so each test selects its own auth branch.
        monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
        monkeypatch.delenv("DATABRICKS_BEARER_COMMAND", raising=False)

    def test_none_when_no_workspace(self):
        with patch.object(doctor_mod, "load_state", return_value={}):
            assert _check_databricks_auth() is None

    def test_ok_when_valid(self):
        with (
            patch.object(doctor_mod, "load_state", return_value={"workspace": "https://ws"}),
            patch.object(doctor_mod, "has_valid_databricks_auth", return_value=True),
        ):
            check = _check_databricks_auth()
        assert check.status == "ok"
        assert check.suggestion is None

    def test_warn_and_login_suggestion_when_invalid(self):
        with (
            patch.object(doctor_mod, "load_state", return_value={"workspace": "https://ws"}),
            patch.object(doctor_mod, "has_valid_databricks_auth", return_value=False),
        ):
            check = _check_databricks_auth()
        assert check.status == "warn"
        assert check.suggestion is not None
        assert "Log in" in check.suggestion.prompt

    def test_login_fix_reports_success(self):
        with (
            patch.object(doctor_mod, "load_state", return_value={"workspace": "https://ws"}),
            # invalid at first, then valid after login
            patch.object(doctor_mod, "has_valid_databricks_auth", side_effect=[False, True]),
            patch.object(doctor_mod, "run_databricks_login") as login,
        ):
            check = _check_databricks_auth()
            assert check.suggestion.apply() is True
        login.assert_called_once()

    def test_login_fix_reports_failure_when_login_raises(self):
        with (
            patch.object(doctor_mod, "load_state", return_value={"workspace": "https://ws"}),
            patch.object(doctor_mod, "has_valid_databricks_auth", return_value=False),
            patch.object(doctor_mod, "run_databricks_login", side_effect=RuntimeError("nope")),
        ):
            check = _check_databricks_auth()
            assert check.suggestion.apply() is False

    def test_static_bearer_warns_without_verification(self, monkeypatch):
        monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
        monkeypatch.delenv("DATABRICKS_BEARER_COMMAND", raising=False)
        monkeypatch.setenv("DATABRICKS_BEARER", "garbage")
        with patch.object(doctor_mod, "load_state", return_value={"workspace": "https://ws"}):
            check = _check_databricks_auth()
        assert check.status == "warn"
        assert check.suggestion is None
        assert "not verified" in check.detail.lower()
        assert check.status != "ok"

    def test_bearer_command_ok_when_obtainable(self, monkeypatch):
        monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
        monkeypatch.delenv("DATABRICKS_BEARER_COMMAND", raising=False)
        monkeypatch.setenv("DATABRICKS_BEARER_COMMAND", "echo tok")
        with (
            patch.object(doctor_mod, "load_state", return_value={"workspace": "https://ws"}),
            patch.object(doctor_mod, "get_databricks_token", return_value="tok"),
        ):
            check = _check_databricks_auth()
        assert check.status == "ok"
        assert check.suggestion is None

    def test_bearer_command_error_when_not_obtainable(self, monkeypatch):
        monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
        monkeypatch.delenv("DATABRICKS_BEARER_COMMAND", raising=False)
        monkeypatch.setenv("DATABRICKS_BEARER_COMMAND", "false")
        with (
            patch.object(doctor_mod, "load_state", return_value={"workspace": "https://ws"}),
            patch.object(doctor_mod, "get_databricks_token", side_effect=RuntimeError("nope")),
        ):
            check = _check_databricks_auth()
        assert check.status == "error"
        assert check.suggestion is None

    def test_custom_oauth_ok_when_profile_obtainable(self, monkeypatch):
        monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
        monkeypatch.delenv("DATABRICKS_BEARER_COMMAND", raising=False)
        state = {
            "workspace": "https://ws",
            "custom_oauth": {"profile": "oauth-profile"},
        }
        with (
            patch.object(doctor_mod, "load_state", return_value=state),
            patch.object(doctor_mod, "has_valid_databricks_auth", return_value=True),
        ):
            check = _check_databricks_auth()
        assert check.status == "ok"
        assert check.suggestion is None

    def test_custom_oauth_warn_when_profile_not_obtainable(self, monkeypatch):
        monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
        monkeypatch.delenv("DATABRICKS_BEARER_COMMAND", raising=False)
        state = {
            "workspace": "https://ws",
            "custom_oauth": {"profile": "oauth-profile"},
        }
        with (
            patch.object(doctor_mod, "load_state", return_value=state),
            patch.object(doctor_mod, "has_valid_databricks_auth", return_value=False),
        ):
            check = _check_databricks_auth()
        assert check.status == "warn"
        assert check.suggestion is None

    def test_custom_oauth_warn_without_profile(self, monkeypatch):
        monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
        monkeypatch.delenv("DATABRICKS_BEARER_COMMAND", raising=False)
        state = {
            "workspace": "https://ws",
            "custom_oauth": {},
        }
        with patch.object(doctor_mod, "load_state", return_value=state):
            check = _check_databricks_auth()
        assert check.status == "warn"
        assert check.suggestion is None


class TestAnthropicEnvCollision:
    def test_none_when_unset(self, monkeypatch):
        for var in doctor_mod._CLAUDE_TOKEN_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        assert _check_anthropic_env_collision() is None

    def test_warns_when_set(self, monkeypatch):
        for var in doctor_mod._CLAUDE_TOKEN_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "secret")
        check = _check_anthropic_env_collision()
        assert check.status == "warn"
        assert "ANTHROPIC_AUTH_TOKEN" in check.detail
        # Advisory only — no auto-fix for a parent shell's env.
        assert check.suggestion is None

    def test_blank_value_is_ignored(self, monkeypatch):
        for var in doctor_mod._CLAUDE_TOKEN_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
        assert _check_anthropic_env_collision() is None


class TestUgCheck:
    def test_reports_version_without_optional_reinstall(self):
        with patch.object(doctor_mod, "ug_version", return_value="1.2.3"):
            check = doctor_mod._check_ug()
        assert "1.2.3" in check.detail
        assert check.suggestion is None


class TestDoctorFlow:
    def _only(self, checks: list[Check]):
        """Run doctor() with a fixed set of checks and a stubbed prompter."""
        return patch.object(doctor_mod, "_gather_checks", return_value=checks)

    def test_applies_fix_when_user_accepts(self):
        applied = []
        suggestion = Suggestion("Fix it?", lambda: applied.append(True) or True)
        check = Check("thing", "warn", "broken", suggestion)
        with (
            self._only([check]),
            patch("sys.stdin.isatty", return_value=True),
            patch.object(doctor_mod, "prompt_yes_no_default", return_value=True),
        ):
            rc = doctor()
        assert rc == 0
        assert applied == [True]

    def test_skips_fix_when_user_declines(self):
        applied = []
        suggestion = Suggestion("Fix it?", lambda: applied.append(True) or True)
        check = Check("thing", "warn", "broken", suggestion)
        with (
            self._only([check]),
            patch("sys.stdin.isatty", return_value=True),
            patch.object(doctor_mod, "prompt_yes_no_default", return_value=False),
        ):
            doctor()
        assert applied == []

    def test_reports_fix_failure_without_raising(self):
        suggestion = Suggestion("Fix it?", lambda: False)
        check = Check("thing", "error", "broken", suggestion)
        with (
            self._only([check]),
            patch("sys.stdin.isatty", return_value=True),
            patch.object(doctor_mod, "prompt_yes_no_default", return_value=True),
            patch.object(doctor_mod, "print_warning") as warn,
        ):
            rc = doctor()
        assert rc == 1
        warn.assert_called()

    def test_ok_check_is_never_prompted(self):
        check = Check("thing", "ok", "healthy", None)
        with (
            self._only([check]),
            patch.object(doctor_mod, "prompt_yes_no_default") as prompt,
        ):
            doctor()
        prompt.assert_not_called()


class TestDoctorNonInteractive:
    def test_piped_run_never_prompts_or_applies(self):
        applied = []
        suggestion = Suggestion("Fix it?", lambda: applied.append(True) or True)
        check = Check("thing", "error", "broken", suggestion)
        with (
            patch.object(doctor_mod, "_gather_checks", return_value=[check]),
            patch("sys.stdin.isatty", return_value=False),
            patch.object(doctor_mod, "prompt_yes_no_default") as prompt,
        ):
            rc = doctor()
        prompt.assert_not_called()
        assert applied == []
        assert rc == 1

    def test_stdin_none_does_not_raise(self):
        check = Check("thing", "ok", "healthy")
        with (
            patch.object(doctor_mod, "_gather_checks", return_value=[check]),
            patch("sys.stdin", None),
        ):
            rc = doctor()
        assert rc == 0


class TestDoctorExitCode:
    def _run(self, checks: list[Check], interactive: bool = False, prompt_answer: bool = False):
        with (
            patch.object(doctor_mod, "_gather_checks", return_value=checks),
            patch("sys.stdin.isatty", return_value=interactive),
            patch.object(doctor_mod, "prompt_yes_no_default", return_value=prompt_answer),
        ):
            return doctor()

    def test_unresolved_error_returns_1(self):
        assert self._run([Check("thing", "error", "broken")]) == 1

    def test_warn_only_returns_0(self):
        assert self._run([Check("thing", "warn", "meh")]) == 0

    def test_ok_returns_0(self):
        assert self._run([Check("thing", "ok", "healthy")]) == 0

    def test_resolved_error_returns_0(self):
        suggestion = Suggestion("Fix it?", lambda: True)
        check = Check("thing", "error", "broken", suggestion)
        assert self._run([check], interactive=True, prompt_answer=True) == 0


class TestGatherChecksIsolation:
    def test_failing_inspector_does_not_suppress_others(self):
        ok = Check("npm", "ok", "fine")
        with (
            patch.object(doctor_mod, "_check_uv", side_effect=RuntimeError("boom")),
            patch.object(doctor_mod, "_check_npm", return_value=ok),
            patch.object(doctor_mod, "_check_databricks_cli", return_value=None),
            patch.object(doctor_mod, "_check_workspace", return_value=None),
            patch.object(doctor_mod, "_check_databricks_auth", return_value=None),
            patch.object(doctor_mod, "_check_anthropic_env_collision", return_value=None),
            patch.object(doctor_mod, "_check_agent_clis", return_value=[]),
            patch.object(doctor_mod, "_check_ug", return_value=None),
        ):
            checks = doctor_mod._gather_checks()
        assert ok in checks
        uv = next(c for c in checks if c.name == "uv")
        assert uv.status == "error"
        assert uv.detail.startswith("check could not run")


class TestClaudeGatewayConfig:
    def _run(self, tmp_path, monkeypatch, state, contents=None):
        path = tmp_path / "ucode-settings.json"
        if contents is not None:
            path.write_text(contents, encoding="utf-8")
        monkeypatch.setattr(doctor_mod, "CLAUDE_SETTINGS_PATH", path)
        with patch.object(doctor_mod, "load_state", return_value=state):
            return doctor_mod._check_claude_gateway_config()

    def test_none_when_claude_not_configured(self, tmp_path, monkeypatch):
        state = {"workspace": _WS, "available_tools": ["codex"]}
        assert self._run(tmp_path, monkeypatch, state) is None

    def test_none_when_no_workspace(self, tmp_path, monkeypatch):
        state = {"available_tools": ["claude"]}
        assert self._run(tmp_path, monkeypatch, state) is None

    def test_error_when_settings_missing(self, tmp_path, monkeypatch):
        state = {"workspace": _WS, "available_tools": ["claude"]}
        check = self._run(tmp_path, monkeypatch, state)
        assert check.status == "error"
        assert "missing" in check.detail

    def test_error_when_settings_malformed(self, tmp_path, monkeypatch):
        state = {"workspace": _WS, "available_tools": ["claude"]}
        check = self._run(tmp_path, monkeypatch, state, contents="{not json")
        assert check.status == "error"
        assert "not valid JSON" in check.detail

    def test_ok_for_valid_non_relay_config(self, tmp_path, monkeypatch):
        state = {"workspace": _WS, "managed_configs": {"claude": {"keys": []}}}
        contents = json.dumps(
            {"apiKeyHelper": "echo token", "env": {"ANTHROPIC_BASE_URL": _CLAUDE_BASE_URL}}
        )
        check = self._run(tmp_path, monkeypatch, state, contents=contents)
        assert check.status == "ok"
        assert check.suggestion is None

    def test_error_for_stale_base_url(self, tmp_path, monkeypatch):
        state = {"workspace": _WS, "available_tools": ["claude"]}
        contents = json.dumps(
            {
                "apiKeyHelper": "echo token",
                "env": {"ANTHROPIC_BASE_URL": "https://old.example.com/ai-gateway/anthropic"},
            }
        )
        check = self._run(tmp_path, monkeypatch, state, contents=contents)
        assert check.status == "error"
        assert "stale gateway URL" in check.detail
        assert _CLAUDE_BASE_URL in check.detail

    def test_error_when_api_key_helper_missing(self, tmp_path, monkeypatch):
        state = {"workspace": _WS, "available_tools": ["claude"]}
        contents = json.dumps({"env": {"ANTHROPIC_BASE_URL": _CLAUDE_BASE_URL}})
        check = self._run(tmp_path, monkeypatch, state, contents=contents)
        assert check.status == "error"
        assert "apiKeyHelper" in check.detail

    def test_ok_for_relay_config_without_api_key_helper(self, tmp_path, monkeypatch):
        state = {"workspace": _WS, "available_tools": ["claude"], "claude_relayed": True}
        contents = json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:4567"}})
        check = self._run(tmp_path, monkeypatch, state, contents=contents)
        assert check.status == "ok"


class TestCodexGatewayConfig:
    _VALID = f'model_provider = "Databricks"\n\n[model_providers.Databricks]\nbase_url = "{_CODEX_BASE_URL}"\n'

    def _run(self, tmp_path, monkeypatch, state, contents=None):
        path = tmp_path / "ucode.config.toml"
        if contents is not None:
            path.write_text(contents, encoding="utf-8")
        monkeypatch.setattr(doctor_mod, "CODEX_CONFIG_PATH", path)
        with patch.object(doctor_mod, "load_state", return_value=state):
            return doctor_mod._check_codex_gateway_config()

    def _state(self):
        return {"workspace": _WS, "available_tools": ["codex"]}

    def test_error_when_config_missing(self, tmp_path, monkeypatch):
        check = self._run(tmp_path, monkeypatch, self._state())
        assert check.status == "error"
        assert "missing" in check.detail

    def test_error_when_config_malformed(self, tmp_path, monkeypatch):
        check = self._run(tmp_path, monkeypatch, self._state(), contents="model_provider = ")
        assert check.status == "error"
        assert "not valid TOML" in check.detail

    def test_ok_for_valid_config(self, tmp_path, monkeypatch):
        check = self._run(tmp_path, monkeypatch, self._state(), contents=self._VALID)
        assert check.status == "ok"
        assert check.suggestion is None

    def test_error_for_wrong_model_provider(self, tmp_path, monkeypatch):
        contents = self._VALID.replace('model_provider = "Databricks"', 'model_provider = "openai"')
        check = self._run(tmp_path, monkeypatch, self._state(), contents=contents)
        assert check.status == "error"
        assert "model_provider" in check.detail

    def test_error_for_stale_base_url(self, tmp_path, monkeypatch):
        contents = self._VALID.replace(
            _CODEX_BASE_URL, "https://old.example.com/ai-gateway/codex/v1"
        )
        check = self._run(tmp_path, monkeypatch, self._state(), contents=contents)
        assert check.status == "error"
        assert "stale/absent gateway URL" in check.detail
        assert _CODEX_BASE_URL in check.detail

    def test_error_when_model_catalog_missing(self, tmp_path, monkeypatch):
        contents = f'model_catalog_json = "{tmp_path / "missing.json"}"\n' + self._VALID
        check = self._run(tmp_path, monkeypatch, self._state(), contents=contents)
        assert check.status == "error"
        assert "model catalog" in check.detail

    def test_ok_when_model_catalog_exists(self, tmp_path, monkeypatch):
        catalog = tmp_path / "catalog.json"
        catalog.write_text("{}", encoding="utf-8")
        contents = f'model_catalog_json = "{catalog}"\n' + self._VALID
        check = self._run(tmp_path, monkeypatch, self._state(), contents=contents)
        assert check.status == "ok"
