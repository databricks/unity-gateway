"""Tests for the Claude Desktop / Cowork launcher config + auth resolution."""

from __future__ import annotations

import json
import subprocess

import pytest

from ucode.agents import claude_desktop
from ucode.managed_files import OS


class TestRenderConfig:
    def test_points_at_proxy_and_holds_no_real_token(self):
        config = claude_desktop.render_config("http://127.0.0.1:51234", [])
        assert config["inferenceProvider"] == "gateway"
        assert config["inferenceGatewayBaseUrl"] == "http://127.0.0.1:51234"
        # Discovery off (relayed /v1/models isn't served); models listed explicitly.
        assert config["modelDiscoveryEnabled"] is False
        # The proxy injects credentials per request; the file must not carry a real one.
        assert config["inferenceGatewayApiKey"] == claude_desktop._CONFIG_KEY_PLACEHOLDER
        assert "sk-ant" not in config["inferenceGatewayApiKey"]
        # The proxy injects headers; the config must not carry its own.
        assert "inferenceCustomHeaders" not in config

    def test_uses_only_schema_keys_desktop_authors(self):
        # Guard against re-introducing invented keys Desktop ignores.
        config = claude_desktop.render_config("http://127.0.0.1:1", [])
        for invented in (
            "chatTabEnabled",
            "coworkTabEnabled",
            "modelPrefer1mContext",
            "disableDeploymentModeChooser",
        ):
            assert invented not in config

    def test_models_populate_inference_models_to_skip_discovery(self):
        config = claude_desktop.render_config(
            "http://127.0.0.1:1", ["claude-opus-4-8", "claude-sonnet-4-5"]
        )
        assert config["inferenceModels"] == [
            {"name": "claude-opus-4-8"},
            {"name": "claude-sonnet-4-5"},
        ]

    def test_no_models_omits_inference_models(self):
        assert "inferenceModels" not in claude_desktop.render_config("http://127.0.0.1:1", [])


class TestMetaRegistration:
    """`_meta.json` is what makes an entry visible (`entries`) and active (`appliedId`)."""

    def _library(self, monkeypatch, tmp_path):
        monkeypatch.setattr(claude_desktop, "current_os", lambda: OS.MACOS)
        monkeypatch.setenv("HOME", str(tmp_path))
        lib = claude_desktop._config_library_dir()
        lib.mkdir(parents=True, exist_ok=True)
        return lib

    def test_creates_meta_and_applies_when_absent(self, monkeypatch, tmp_path):
        lib = self._library(monkeypatch, tmp_path)
        prior = claude_desktop.register_active_config("ug-id", name="Unity Gateway")
        assert prior is None
        meta = json.loads((lib / "_meta.json").read_text())
        assert meta["appliedId"] == "ug-id"
        assert {"id": "ug-id", "name": "Unity Gateway"} in meta["entries"]

    def test_preserves_existing_entries_and_returns_prior_applied(self, monkeypatch, tmp_path):
        lib = self._library(monkeypatch, tmp_path)
        (lib / "_meta.json").write_text(
            json.dumps(
                {"appliedId": "default-id", "entries": [{"id": "default-id", "name": "Default"}]}
            )
        )
        prior = claude_desktop.register_active_config("ug-id")
        assert prior == "default-id"
        meta = json.loads((lib / "_meta.json").read_text())
        assert meta["appliedId"] == "ug-id"
        ids = {e["id"] for e in meta["entries"]}
        assert ids == {"default-id", "ug-id"}  # Default preserved

    def test_reregistering_does_not_duplicate(self, monkeypatch, tmp_path):
        lib = self._library(monkeypatch, tmp_path)
        claude_desktop.register_active_config("ug-id")
        claude_desktop.register_active_config("ug-id")
        meta = json.loads((lib / "_meta.json").read_text())
        assert [e for e in meta["entries"] if e["id"] == "ug-id"] == [
            {"id": "ug-id", "name": claude_desktop._ENTRY_NAME}
        ]

    def test_restore_hands_applied_id_back(self, monkeypatch, tmp_path):
        lib = self._library(monkeypatch, tmp_path)
        (lib / "_meta.json").write_text(
            json.dumps(
                {"appliedId": "default-id", "entries": [{"id": "default-id", "name": "Default"}]}
            )
        )
        prior = claude_desktop.register_active_config("ug-id")
        claude_desktop.restore_active_config(prior)
        meta = json.loads((lib / "_meta.json").read_text())
        assert meta["appliedId"] == "default-id"


class TestConfigPath:
    def test_macos_path_is_stable_per_workspace(self, monkeypatch, tmp_path):
        monkeypatch.setattr(claude_desktop, "current_os", lambda: OS.MACOS)
        monkeypatch.setenv("HOME", str(tmp_path))
        p1 = claude_desktop.config_path("https://ws.example.com")
        p2 = claude_desktop.config_path("https://ws.example.com")
        assert p1 == p2  # deterministic
        assert p1 != claude_desktop.config_path("https://other.example.com")
        assert p1.parent.name == "configLibrary"
        assert p1.suffix == ".json"
        assert "Claude-3p" in str(p1)

    def test_unsupported_os_raises(self, monkeypatch):
        monkeypatch.setattr(claude_desktop, "current_os", lambda: OS.LINUX)
        with pytest.raises(RuntimeError, match="macOS and Windows"):
            claude_desktop.config_path("https://ws.example.com")


class TestResolveAnthropicOauth:
    def test_prefers_preset_env_and_skips_browser(self, monkeypatch):
        monkeypatch.setenv(claude_desktop.CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR, "sk-ant-oat-preset")

        def _fail(*_a, **_k):  # pragma: no cover - must not run
            raise AssertionError("setup-token should not be invoked when the env var is set")

        monkeypatch.setattr(subprocess, "run", _fail)
        assert claude_desktop._resolve_anthropic_oauth() == "sk-ant-oat-preset"

    def test_parses_token_from_setup_token_output(self, monkeypatch):
        monkeypatch.delenv(claude_desktop.CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR, raising=False)

        def _fake_run(*_a, **_k):
            return subprocess.CompletedProcess(
                args=["claude", "setup-token"],
                returncode=0,
                stdout="Your token:\n  sk-ant-oat01-abcDEF_-123  \nKeep it secret.\n",
                stderr="",
            )

        monkeypatch.setattr(subprocess, "run", _fake_run)
        assert claude_desktop._resolve_anthropic_oauth() == "sk-ant-oat01-abcDEF_-123"

    def test_raises_when_no_token_in_output(self, monkeypatch):
        monkeypatch.delenv(claude_desktop.CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR, raising=False)

        def _fake_run(*_a, **_k):
            return subprocess.CompletedProcess(
                args=["claude", "setup-token"], returncode=0, stdout="no token here", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", _fake_run)
        with pytest.raises(RuntimeError, match="Could not read a subscription token"):
            claude_desktop._resolve_anthropic_oauth()

    def test_raises_when_claude_missing(self, monkeypatch):
        monkeypatch.delenv(claude_desktop.CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR, raising=False)

        def _fake_run(*_a, **_k):
            raise FileNotFoundError("claude")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        with pytest.raises(RuntimeError, match="was not found on PATH"):
            claude_desktop._resolve_anthropic_oauth()


class TestOpenDesktop:
    @staticmethod
    def _fake_run_factory(calls: list, running: bool):
        # Command-aware so a `quit` flips the running state — the post-quit wait loop
        # then breaks immediately instead of sleeping through all 40 iterations.
        state = {"running": running}

        def _fake_run(cmd, *_a, **_k):
            calls.append(cmd)
            joined = " ".join(cmd)
            if "quit app id" in joined:
                state["running"] = False
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if "is running" in joined:
                probe = "true" if state["running"] else "false"
                return subprocess.CompletedProcess(cmd, 0, stdout=probe, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        return _fake_run

    def test_macos_launches_closed_app_by_bundle_id(self, monkeypatch):
        monkeypatch.setattr(claude_desktop, "current_os", lambda: OS.MACOS)
        calls: list = []
        monkeypatch.setattr(subprocess, "run", self._fake_run_factory(calls, running=False))
        claude_desktop.open_desktop_app(restart_if_running=True)
        assert ["open", "-b", claude_desktop._APP_BUNDLE_ID] in calls

    def test_macos_opens_closed_app_even_without_restart(self, monkeypatch):
        # A closed app is harmless to launch, so --no-restart still opens it.
        monkeypatch.setattr(claude_desktop, "current_os", lambda: OS.MACOS)
        calls: list = []
        monkeypatch.setattr(subprocess, "run", self._fake_run_factory(calls, running=False))
        claude_desktop.open_desktop_app(restart_if_running=False)
        assert ["open", "-b", claude_desktop._APP_BUNDLE_ID] in calls

    def test_macos_running_app_not_restarted_without_flag(self, monkeypatch):
        # A running app is left alone under --no-restart: no quit, no relaunch.
        monkeypatch.setattr(claude_desktop, "current_os", lambda: OS.MACOS)
        calls: list = []
        monkeypatch.setattr(subprocess, "run", self._fake_run_factory(calls, running=True))
        claude_desktop.open_desktop_app(restart_if_running=False)
        assert ["open", "-b", claude_desktop._APP_BUNDLE_ID] not in calls
        assert not any("quit app id" in " ".join(c) for c in calls)

    def test_macos_running_app_restarted_with_flag(self, monkeypatch):
        monkeypatch.setattr(claude_desktop, "current_os", lambda: OS.MACOS)
        calls: list = []
        monkeypatch.setattr(subprocess, "run", self._fake_run_factory(calls, running=True))
        claude_desktop.open_desktop_app(restart_if_running=True)
        assert any("quit app id" in " ".join(c) for c in calls)  # force-quit
        assert ["open", "-b", claude_desktop._APP_BUNDLE_ID] in calls  # then reopen

    def test_non_macos_warns_and_launches_nothing(self, monkeypatch):
        monkeypatch.setattr(claude_desktop, "current_os", lambda: OS.LINUX)
        calls: list = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        claude_desktop.open_desktop_app(restart_if_running=True)  # must not raise
        assert calls == []  # non-macOS → nothing launched, manual guidance only


class TestEnsureDatabricksSession:
    def test_missing_databricks_cli_raises_actionable_error(self, monkeypatch):
        # A missing `databricks` binary must surface a clear install hint, not a
        # raw FileNotFoundError traceback out of get_databricks_token.
        monkeypatch.setattr(claude_desktop.shutil, "which", lambda _name: None)
        with pytest.raises(RuntimeError, match="`databricks` CLI was not found"):
            claude_desktop._ensure_databricks_session("https://ws.example.com", None)
