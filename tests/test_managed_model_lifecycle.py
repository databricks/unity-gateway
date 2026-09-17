"""Model-config lifecycle: managed defaults and the available-model list stay in sync as a
developer moves across workspaces.

Regression coverage for AIGTWY-4710. A developer configures, in one home, a sequence of
workspaces: none managed -> managed config A -> managed config B (different list + default) ->
none managed. Switching between managed configs reconciles the generated agent settings to the
*current* config, pruning models the previous config listed. Configuring a workspace with no managed
config clears ug's managed model settings (Claude's picker, Codex's catalog) so an unmanaged
workspace never enforces a stale list inherited from a prior managed config. User-owned keys survive
throughout.

These drive the real resolve + ``write_tool_config`` path against tmp files. The only stubbed
boundaries are the sudo-backed OS-managed write (Claude) and Codex's catalog-build subprocess;
the reconcile logic under test runs for real.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ucode import managed_files
from ucode.agents import claude, codex
from ucode.config_io import read_toml_safe
from ucode.managed_config import normalize_managed_config
from ucode.managed_resolve import (
    managed_claude_family_models,
    managed_provider_family_models,
    managed_provider_service,
    resolve_state,
)

MPS = "main.default.anthropic_mps"

WS = "https://example.databricks.com"

# A workspace's own gateway discovery, present regardless of admin curation.
DISCOVERED_CLAUDE = {
    "opus": "system.ai.claude-opus-4-8",
    "sonnet": "system.ai.claude-sonnet-4-6",
    "haiku": "system.ai.claude-haiku-4-5",
}

# Config A / config B model lists. Each agent's config enables only that agent (separation of
# concerns): a Claude config never lists Codex in enabled_agents and vice versa.
CLAUDE_A = [
    "system.ai.claude-opus-4-8",
    "system.ai.claude-sonnet-4-6",
    "system.ai.claude-haiku-4-5",
]
# Config B drops opus from Claude and swaps the Codex model.
CLAUDE_B = ["system.ai.claude-sonnet-5", "system.ai.claude-haiku-4-5"]
CODEX_A = ["system.ai.gpt-5-6-sol", "system.ai.gpt-5-5"]
CODEX_B = ["system.ai.gpt-6-thunder"]

PICKER_KEYS = ("availableModels", "enforceAvailableModels", "modelPicker")


def _claude_agent(models: list[str], defaults: dict[str, str]) -> dict:
    default_models = {"default_model": models[0]}
    default_models.update({f"default_{family}_model": m for family, m in defaults.items()})
    return {
        "agent": "CODING_AGENT_CLAUDE_CODE",
        "config": {"models": {"model_services": models}, "default_models": default_models},
    }


def _codex_agent(models: list[str]) -> dict:
    return {
        "agent": "CODING_AGENT_CODEX",
        "config": {
            "models": {"model_services": models},
            "default_models": {"default_model": models[0]},
        },
    }


def _mps_agent(agent: str, provider: str) -> dict:
    return {"agent": agent, "config": {"models": {"model_provider_service": provider}}}


def _config(*agents: dict) -> dict:
    return {
        "spec_version": 1,
        "default_agent": "CODING_AGENT_CLAUDE_CODE",
        "enabled_agents": list(agents),
    }


# Claude-only configs (no codex in enabled_agents).
CLAUDE_CONFIG_A = _config(
    _claude_agent(CLAUDE_A, {"opus": CLAUDE_A[0], "sonnet": CLAUDE_A[1], "haiku": CLAUDE_A[2]})
)
CLAUDE_CONFIG_B = _config(_claude_agent(CLAUDE_B, {"sonnet": CLAUDE_B[0], "haiku": CLAUDE_B[1]}))
CLAUDE_CONFIG_MPS = _config(_mps_agent("CODING_AGENT_CLAUDE_CODE", MPS))
# Codex-only configs (no claude in enabled_agents).
CODEX_CONFIG_A = _config(_codex_agent(CODEX_A))
CODEX_CONFIG_B = _config(_codex_agent(CODEX_B))
CODEX_CONFIG_MPS = _config(_mps_agent("CODING_AGENT_CODEX", MPS))


class _Harness:
    """Applies a managed config (or None) for one agent, persisting to tmp files across calls."""

    def __init__(self, tmp_path: Path):
        self.claude_settings = tmp_path / "ucode-settings.json"
        self.claude_managed = tmp_path / "claude-managed-settings.json"
        self.codex_config = tmp_path / "ucode.config.toml"
        self.codex_catalog = tmp_path / "codex-model-catalog.json"

    def apply_claude(self, config: dict | None) -> None:
        base = {"workspace": WS, "codex_models": [], "claude_models": dict(DISCOVERED_CLAUDE)}
        if config is None:
            claude.write_tool_config(base, None, coding_agent_config_defaults={})
            return
        managed = normalize_managed_config(config)
        state = resolve_state(managed, base, "claude")
        provider = managed_provider_service(managed, "claude")
        provider_models = managed_provider_family_models(managed) if provider else None
        defaults = managed_claude_family_models(managed) or {}
        claude.write_tool_config(
            state,
            None,
            provider=provider,
            provider_models=provider_models,
            coding_agent_config_defaults=defaults,
        )

    def apply_codex(self, config: dict | None) -> None:
        base = {"workspace": WS, "codex_models": []}
        if config is None:
            codex.write_tool_config(base)
            return
        managed = normalize_managed_config(config)
        state = resolve_state(managed, base, "codex")
        codex.write_tool_config(state, provider=managed_provider_service(managed, "codex"))

    def claude_files(self) -> tuple[dict, dict]:
        private = (
            json.loads(self.claude_settings.read_text()) if self.claude_settings.exists() else {}
        )
        managed = (
            json.loads(self.claude_managed.read_text()) if self.claude_managed.exists() else {}
        )
        return private, managed

    def codex_doc(self) -> dict:
        return read_toml_safe(self.codex_config) if self.codex_config.exists() else {}

    def codex_listed(self) -> list[str]:
        if not self.codex_catalog.exists():
            return []
        catalog = json.loads(self.codex_catalog.read_text())
        return [m["slug"] for m in catalog.get("models", []) if m.get("visibility") == "list"]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    h = _Harness(tmp_path)

    # --- Claude: real tmp private file; simulate the sudo-backed OS-managed write on tmp. ---
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", h.claude_settings)
    monkeypatch.setattr(claude, "CLAUDE_BACKUP_PATH", tmp_path / "claude.backup.json")
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: h.claude_managed)
    monkeypatch.setattr(claude, "managed_writes_allowed", lambda: True)
    monkeypatch.setattr(claude, "mark_managed_file_verified", lambda *a, **kw: None)
    monkeypatch.setattr(claude, "backup_existing_file", lambda *a, **kw: True)
    monkeypatch.setattr(claude, "save_state", lambda state: None)
    monkeypatch.setattr(claude, "_register_web_search_mcp", lambda *a, **kw: True)
    monkeypatch.setattr(claude, "ug_version", lambda: "1.0")
    monkeypatch.setattr(claude, "agent_version", lambda _binary: "2.0")
    # No admin ever touches this managed file, so the pre-ucode baseline has no picker (None) and
    # ucode's last write is the current file. The static picker therefore reverts to that empty
    # baseline (i.e. clears) on a later unmanaged run.
    monkeypatch.setattr(
        claude,
        "managed_file_snapshots",
        lambda tool, parser: managed_files.ManagedFileSnapshots(
            None,
            json.loads(h.claude_managed.read_text()) if h.claude_managed.exists() else None,
        ),
    )
    monkeypatch.setattr(
        claude,
        "read_managed_file",
        lambda path: Path(path).read_text() if Path(path).exists() else None,
    )

    def _write_managed(path, text, **kwargs):
        Path(path).write_text(text)
        return "written"

    monkeypatch.setattr(claude, "reconcile_managed_file", _write_managed)

    # --- Codex: real tmp files; stub the catalog-build subprocess and the OS-managed reconcile. ---
    monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", h.codex_config)
    monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "codex.backup.toml")
    monkeypatch.setattr(codex, "CODEX_MODEL_CATALOG_PATH", h.codex_catalog)
    monkeypatch.setattr(codex, "agent_version", lambda _binary: "0.134.0")
    monkeypatch.setattr(codex, "ug_version", lambda: "1.0")
    monkeypatch.setattr(codex, "save_state", lambda state: None)
    monkeypatch.setattr(codex, "_reconcile_managed_config", lambda *a, **kw: None)
    monkeypatch.setattr(
        codex,
        "prepare_codex_catalog",
        lambda _binary, names: {"models": [{"slug": n, "visibility": "list"} for n in names]},
    )
    return h


def test_claude_model_lifecycle(harness):
    # Seed a user-owned key in the OS-managed file; it must survive every transition.
    harness.claude_managed.write_text(json.dumps({"env": {"MY_OWN": "keep"}}))

    # State 0: no managed config -> no picker, no static allow-list.
    harness.apply_claude(None)
    private, managed = harness.claude_files()
    for key in PICKER_KEYS:
        assert key not in private, private
        assert key not in managed, managed
    assert managed["env"]["MY_OWN"] == "keep"

    # State 1: managed config A -> the picker lists exactly A's models.
    harness.apply_claude(CLAUDE_CONFIG_A)
    private, managed = harness.claude_files()
    assert managed["availableModels"] == CLAUDE_A, managed
    assert private["availableModels"] == CLAUDE_A, private
    assert [o["model"] for o in managed["modelPicker"]["options"]] == CLAUDE_A, managed
    assert managed["enforceAvailableModels"] is True
    assert managed["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == CLAUDE_A[0] + "[1m]", managed
    assert managed["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == CLAUDE_A[1] + "[1m]", managed
    assert managed["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == CLAUDE_A[2], managed
    assert managed["env"]["MY_OWN"] == "keep"

    # State 2: managed config B -> the picker reconciles to B; opus is pruned from the list.
    harness.apply_claude(CLAUDE_CONFIG_B)
    private, managed = harness.claude_files()
    assert managed["availableModels"] == CLAUDE_B, managed
    assert private["availableModels"] == CLAUDE_B, private
    assert "system.ai.claude-opus-4-8" not in managed["availableModels"], managed
    assert [o["model"] for o in managed["modelPicker"]["options"]] == CLAUDE_B, managed
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in managed["env"], managed
    assert managed["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == CLAUDE_B[0] + "[1m]", managed
    assert managed["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == CLAUDE_B[1], managed
    assert managed["env"]["MY_OWN"] == "keep"

    # State 3: managed config gone -> the picker is cleared, so an unmanaged workspace never enforces
    # a stale list. The user-owned key still survives.
    harness.apply_claude(None)
    private, managed = harness.claude_files()
    for key in PICKER_KEYS:
        assert key not in private, private
        assert key not in managed, managed
    assert managed["env"]["MY_OWN"] == "keep"


def test_codex_model_lifecycle(harness):
    # State 0: no managed config -> no catalog, no pinned model.
    harness.apply_codex(None)
    assert harness.codex_listed() == []
    assert "model" not in harness.codex_doc()

    # State 1: managed config A -> catalog lists A, model pinned to A's default.
    harness.apply_codex(CODEX_CONFIG_A)
    assert harness.codex_listed() == CODEX_A
    assert harness.codex_doc()["model"] == CODEX_A[0]

    # State 2: managed config B -> catalog reconciles to B (A pruned), model pinned to B's default.
    harness.apply_codex(CODEX_CONFIG_B)
    assert harness.codex_listed() == CODEX_B
    assert "system.ai.gpt-5-6-sol" not in harness.codex_listed()
    assert harness.codex_doc()["model"] == CODEX_B[0]

    # State 3: managed config gone -> the catalog and pinned model are cleared, so an unmanaged
    # workspace uses its own discovery rather than a stale managed list.
    harness.apply_codex(None)
    assert harness.codex_listed() == []
    assert "model" not in harness.codex_doc()
    assert "model_catalog_json" not in harness.codex_doc()


def test_claude_model_source_mode_lifecycle(harness):
    # no config -> static -> model discovery via MPS -> no config. MPS routes by header and enforces
    # no list, so the static picker is pruned. (unity_catalog_location is not consumed on the apply
    # path, so it has no mode to exercise yet.)
    harness.apply_claude(None)
    _, managed = harness.claude_files()
    assert "availableModels" not in managed

    harness.apply_claude(CLAUDE_CONFIG_A)
    _, managed = harness.claude_files()
    assert managed["availableModels"] == CLAUDE_A

    harness.apply_claude(CLAUDE_CONFIG_MPS)
    _, managed = harness.claude_files()
    for key in PICKER_KEYS:
        assert key not in managed, managed

    harness.apply_claude(None)
    _, managed = harness.claude_files()
    for key in PICKER_KEYS:
        assert key not in managed


def test_codex_model_source_mode_lifecycle(harness):
    # no config -> static -> model discovery via MPS -> no config. MPS discovers models at launch, so
    # the static catalog is pruned.
    harness.apply_codex(None)
    assert harness.codex_listed() == []

    harness.apply_codex(CODEX_CONFIG_A)
    assert harness.codex_listed() == CODEX_A

    harness.apply_codex(CODEX_CONFIG_MPS)
    assert harness.codex_listed() == []
    assert "model_catalog_json" not in harness.codex_doc()

    harness.apply_codex(None)
    assert harness.codex_listed() == []
