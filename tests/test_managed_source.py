"""Tests for managed_source.py — load_file_managed_config."""

from __future__ import annotations

import json

import pytest

from ucode.managed_resolve import managed_enabled_tools
from ucode.managed_source import load_file_managed_config

# A minimal valid CodingAgentConfig with one Claude agent (static model_services + default_model).
_CLAUDE_CONFIG = {
    "spec_version": 1,
    "enabled_agents": [
        {
            "agent": "CODING_AGENT_CLAUDE_CODE",
            "config": {
                "models": {"model_services": ["system.ai.claude-sonnet-4-6"]},
                "default_models": {"default_model": "system.ai.claude-sonnet-4-6"},
            },
        }
    ],
}


class TestLoadFileManagedConfig:
    def test_valid_config_returns_normalized_manifest(self, tmp_path):
        # Case 1: valid full config -> returns a dict manifest with "claude" in managed_enabled_tools
        path = tmp_path / "config.json"
        path.write_text(json.dumps(_CLAUDE_CONFIG), encoding="utf-8")
        result = load_file_managed_config(str(path))
        assert isinstance(result, dict)
        assert "claude" in managed_enabled_tools(result)

    def test_config_with_mcp_servers_and_skills_is_accepted(self, tmp_path):
        # Case 2: mcp_servers and skills are accepted (not rejected) and appear in the manifest.
        config = {
            **_CLAUDE_CONFIG,
            "mcp_servers": {"names": ["system.ai.github", "main.default.jira"]},
            "skills": {"names": ["system.ai.pdf-extraction"]},
        }
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        result = load_file_managed_config(str(path))
        assert result["mcp_servers"] == {"names": ["system.ai.github", "main.default.jira"]}
        assert result["skills"] == {"names": ["system.ai.pdf-extraction"]}

    def test_metadata_keys_are_accepted_and_ignored(self, tmp_path):
        # Case 3: wire-shape metadata keys (name, workspace_id, create_time, update_time,
        # retrieved_time, spec_version) present -> no exception; unknown keys are silently dropped.
        config = {
            **_CLAUDE_CONFIG,
            "name": "main/coding-agent-config/default",
            "workspace_id": "12345",
            "create_time": "2026-01-01T00:00:00Z",
            "update_time": "2026-09-01T00:00:00Z",
            "retrieved_time": "2026-09-28T12:00:00Z",
        }
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        result = load_file_managed_config(str(path))
        assert isinstance(result, dict)
        assert "claude" in managed_enabled_tools(result)

    def test_missing_file_raises_runtime_error(self, tmp_path):
        # Case 4: non-existent file -> RuntimeError mentioning config-file.
        path = tmp_path / "does_not_exist.json"
        with pytest.raises(RuntimeError, match="config-file"):
            load_file_managed_config(str(path))

    def test_malformed_json_raises_runtime_error(self, tmp_path):
        # Case 5: malformed JSON -> RuntimeError.
        path = tmp_path / "bad.json"
        path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(RuntimeError, match="config-file"):
            load_file_managed_config(str(path))

    @pytest.mark.parametrize("content", ['"a string"', "[1, 2, 3]"])
    def test_non_object_top_level_raises_runtime_error(self, tmp_path, content):
        # Case 6: non-object top level (string or array) -> RuntimeError.
        path = tmp_path / "config.json"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(RuntimeError, match="config-file"):
            load_file_managed_config(str(path))

    def test_duplicate_json_key_raises_runtime_error(self, tmp_path):
        # Case 7: duplicate JSON object key -> RuntimeError.
        path = tmp_path / "config.json"
        path.write_text('{"spec_version": 1, "spec_version": 2}', encoding="utf-8")
        with pytest.raises(RuntimeError, match="config-file"):
            load_file_managed_config(str(path))

    def test_non_finite_number_raises_runtime_error(self, tmp_path):
        # Case 8: NaN in a JSON value -> RuntimeError (parse_constant hook fires).
        path = tmp_path / "config.json"
        path.write_text('{"value": NaN}', encoding="utf-8")
        with pytest.raises(RuntimeError, match="config-file"):
            load_file_managed_config(str(path))

    def test_overflow_to_infinity_raises_runtime_error(self, tmp_path):
        # Case 8b: a literal that overflows to infinity (1e999) is non-finite too, but parses past
        # parse_constant, so parse_float must reject it.
        path = tmp_path / "config.json"
        path.write_text('{"value": 1e999}', encoding="utf-8")
        with pytest.raises(RuntimeError, match="config-file"):
            load_file_managed_config(str(path))

    def test_oversized_integer_literal_raises_runtime_error(self, tmp_path):
        # Case 8c: an integer literal beyond CPython's int-string limit raises ValueError inside
        # json.loads (not JSONDecodeError); it must surface wrapped, not as a raw traceback.
        path = tmp_path / "config.json"
        path.write_text('{"value": ' + "9" * 5000 + "}", encoding="utf-8")
        with pytest.raises(RuntimeError, match="config-file"):
            load_file_managed_config(str(path))

    def test_spec_version_newer_than_supported_raises(self, tmp_path):
        # Case 8d: a spec_version this build can't act on is refused by the same gate the live fetch
        # applies, rather than silently applied.
        config = {**_CLAUDE_CONFIG, "spec_version": 2}
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        with pytest.raises(RuntimeError, match="spec_version"):
            load_file_managed_config(str(path))

    def test_both_claude_and_codex_enabled(self, tmp_path):
        # Case 9: both claude and codex enabled -> both appear in managed_enabled_tools.
        config = {
            "spec_version": 1,
            "enabled_agents": [
                {
                    "agent": "CODING_AGENT_CLAUDE_CODE",
                    "config": {
                        "models": {"model_services": ["system.ai.claude-sonnet-4-6"]},
                        "default_models": {"default_model": "system.ai.claude-sonnet-4-6"},
                    },
                },
                {
                    "agent": "CODING_AGENT_CODEX",
                    "config": {
                        "models": {"model_services": ["system.ai.gpt-5"]},
                        "default_models": {"default_model": "system.ai.gpt-5"},
                    },
                },
            ],
        }
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        result = load_file_managed_config(str(path))
        tools = managed_enabled_tools(result)
        assert "claude" in tools
        assert "codex" in tools
