"""Shared fixtures for E2E tests + global state-isolation guard."""

from __future__ import annotations

import functools
import json
import os
from types import SimpleNamespace

import pytest

from ucode.databricks import (
    build_shared_base_urls,
    fetch_ai_gateway_claude_models,
    fetch_codex_models,
    fetch_gemini_models,
    get_databricks_token,
)
from ucode.ui import normalize_workspace_url

# Live suites have their own configuration and must be selected explicitly.
collect_ignore = ["integration", "e2e_cuj", "native_claude", "connector_behavior"]


@pytest.fixture(autouse=True)
def _isolate_ucode_state(tmp_path, monkeypatch):
    """Redirect ucode's state file and APP_DIR to a per-test tmp dir.

    Defense in depth: even if an individual test forgets to patch save_state,
    it can never touch the developer's real ~/.ucode/state.json or invoke the
    privileged writer for an OS-managed agent config.
    """
    import ucode.config_io as config_io_mod
    import ucode.databricks as databricks_mod
    import ucode.managed_config as managed_config_mod
    import ucode.managed_files as managed_files_mod
    import ucode.os_compatibility.subprocess_cross_os as subprocess_cross_os_mod
    import ucode.state as state_mod
    from ucode import desktop_auth as desktop_auth_mod
    from ucode.agents import antigravity as antigravity_mod
    from ucode.agents import claude as claude_mod
    from ucode.agents import codex as codex_mod
    from ucode.agents import copilot as copilot_mod
    from ucode.agents import vscode as vscode_mod
    from ucode.agents.desktop import claude as desktop_mod

    state_dir = tmp_path / ".ucode"
    state_dir.mkdir()
    monkeypatch.setattr(state_mod, "STATE_PATH", state_dir / "state.json")
    monkeypatch.setattr(config_io_mod, "APP_DIR", state_dir)
    monkeypatch.setattr(codex_mod, "_otel_service_path", lambda: state_dir / "otel.service")
    monkeypatch.setattr(desktop_mod, "APP_DIR", state_dir)
    # No test may run the developer's `code` or look at their installed VS Code extensions.
    monkeypatch.setattr(vscode_mod, "find_code", lambda: None)
    monkeypatch.setattr(vscode_mod, "_find_command", lambda command: None)
    monkeypatch.setattr(
        vscode_mod, "extensions_dir", lambda folder: tmp_path / folder / "extensions"
    )
    monkeypatch.setattr(desktop_auth_mod, "_AUTH_LOCK_PATH", state_dir / "claude-desktop-auth.lock")
    # Antigravity's customization root is shared with the developer's Antigravity app and CLI.
    agy_dir = tmp_path / ".gemini" / "config"
    for name, path in (
        ("AGY_MCP_CONFIG_PATH", agy_dir / "mcp_config.json"),
        ("AGY_SKILLS_CONFIG_PATH", agy_dir / "skills.json"),
        ("AGY_SKILLS_CREATED_MARKER", state_dir / "antigravity-skills.created"),
        ("AGY_MCP_BACKUP_PATH", state_dir / "antigravity-mcp-config.backup.json"),
    ):
        monkeypatch.setattr(antigravity_mod, name, path)
    monkeypatch.setitem(antigravity_mod.SPEC, "config_path", agy_dir / "skills.json")
    monkeypatch.setitem(
        antigravity_mod.SPEC, "backup_path", state_dir / "antigravity-skills.created"
    )
    # The workspace policy lives outside APP_DIR; rebind it, and its user-owned refresh stamps,
    # so no test reads or installs the machine-wide copy.
    monkeypatch.setattr(
        managed_config_mod,
        "MANAGED_CONFIG_PATH",
        tmp_path / "etc" / "unitygateway" / "managed-settings.json",
    )
    monkeypatch.setattr(managed_config_mod, "REFRESH_TS_PATH", state_dir / "refresh_ts.json")
    monkeypatch.setattr(
        managed_config_mod, "USER_MANAGED_CONFIG_PATH", state_dir / "managed-config.json"
    )

    def install_test_policy(path, desired_text, *, interactive=False, allow_sudo=True):
        if path != managed_config_mod.MANAGED_CONFIG_PATH or not path.is_relative_to(tmp_path):
            pytest.fail(f"test attempted to install machine-wide policy at {path}")
        if config_io_mod.is_dry_run():
            return
        if desired_text is None:
            path.unlink(missing_ok=True)
        else:
            config_io_mod.atomic_write_json(path, json.loads(desired_text))
            path.chmod(0o644)

    monkeypatch.setattr(managed_config_mod, "update_workspace_policy", install_test_policy)
    # The installer is faked above, so let every host take the installable (Linux/macOS) path;
    # tests for Windows or UG_SKIP_OS_SETTINGS override this.
    monkeypatch.setattr(managed_config_mod, "current_os", lambda: managed_files_mod.OS.LINUX)
    backup_dir = state_dir / "managed-backups"
    monkeypatch.setattr(managed_files_mod, "MANAGED_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(
        managed_files_mod, "MANAGED_BACKUP_MANIFEST_PATH", backup_dir / "manifest.json"
    )
    monkeypatch.setattr(managed_files_mod, "_config_scope", "global")
    # `ug configure` and `ug revert` set this for the rest of the process; start each test as a
    # launch-like run.
    monkeypatch.setattr(managed_files_mod, "_explicit_managed_command", None)
    monkeypatch.setattr(codex_mod, "codex_managed_config_path", lambda: None)
    # Launch seeds Claude's feature cache in `.claude.json`; never touch the developer's copy.
    monkeypatch.setattr(claude_mod, "CLAUDE_CODE_CONFIG_PATH", tmp_path / ".claude.json")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    # The header mod installs beside the real managed file; tests opt in with a tmp root.
    monkeypatch.setattr(claude_mod, "claude_header_mod_root", lambda: None)
    monkeypatch.setattr(
        codex_mod, "CODEX_MODEL_CATALOG_PATH", state_dir / "codex-model-catalog.json"
    )
    monkeypatch.setattr(codex_mod, "CODEX_CONFIG_PATH", tmp_path / ".codex" / "ucode.config.toml")
    # MCP writes and revert edit Copilot's user config and delete the legacy side file.
    monkeypatch.setattr(
        copilot_mod, "COPILOT_MCP_CONFIG_PATH", tmp_path / ".copilot" / "mcp-config.json"
    )
    monkeypatch.setattr(
        copilot_mod, "LEGACY_MCP_CONFIG_PATH", tmp_path / ".copilot" / "ucode-mcp-config.json"
    )
    monkeypatch.setattr(
        copilot_mod, "LEGACY_MCP_BACKUP_PATH", state_dir / "copilot-ucode-mcp-config.backup.json"
    )
    monkeypatch.delenv("COPILOT_HOME", raising=False)

    def reject_privileged_write(path, _desired_text):
        pytest.fail(
            f"test attempted a privileged managed-config write to {path}; "
            "mock the agent's managed path and writer"
        )

    monkeypatch.setattr(managed_files_mod, "_sudo_replace", reject_privileged_write)
    # Windows CI runners are elevated; never let an unmocked test write C:\Program Files.
    monkeypatch.setattr(managed_files_mod.system_cross_os, "is_admin", lambda: False)
    monkeypatch.setattr(managed_files_mod, "_windows_replace", reject_privileged_write)
    monkeypatch.delenv("ENABLE_CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", raising=False)
    monkeypatch.delenv("UCODE_SESSION_ENV_FILE", raising=False)
    monkeypatch.delenv("UCODE_SMART_ROUTER_PYTHON", raising=False)
    # `ug --header` exports this to the agent, so an outer session's value would leak into launches.
    monkeypatch.delenv("UG_HEADERS", raising=False)
    # When the suite runs inside a ug smart-routing session, these ambient env vars
    # make smart_routing_enabled()/configured_router_name() read the host's live
    # routing state, leaking a Databricks-Smart-Router-Recipe header into
    # header-rendering tests. Clear them so routing stays off unless a test opts in.
    monkeypatch.delenv("ENABLE_SMART_ROUTING_V2", raising=False)
    monkeypatch.delenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", raising=False)
    monkeypatch.delenv("ENABLE_SMART_ROUTER_ORCHESTRATOR", raising=False)
    monkeypatch.delenv("SMART_ROUTER_CONFIG_VERSION", raising=False)
    monkeypatch.delenv("SMART_ROUTER_NAME", raising=False)
    # On Windows, resolve_command swaps a bare program name for whatever `shutil.which`
    # finds on the developer's PATH (e.g. a real `codex.CMD`). Rebind only the compatibility
    # helper's `shutil` so argv stays host-independent; helper tests patch `which` explicitly.
    monkeypatch.setattr(
        subprocess_cross_os_mod, "shutil", SimpleNamespace(which=lambda _name: None)
    )
    # A developer's ambient managed-config stub would otherwise short-circuit every fetch in the suite.
    monkeypatch.delenv("UCODE_MANAGED_CONFIG_STUB", raising=False)
    # The model-services listing is memoized for the life of the process, so without this a cached
    # result would leak into the next test and make a stubbed listing look like it was never called.
    databricks_mod.clear_model_services_cache()
    databricks_mod.clear_workspace_org_id_cache()
    # The resolved Databricks CLI path/discovery is cached for the life of the
    # process; a real resolution (or a prior test's patched one) must not leak
    # into a test that assumes a bare "databricks" or a specific fake CLI.
    databricks_mod.clear_databricks_cli_cache()
    # Tokens and token failures are memoized for the process; a fake CLI's answer
    # in one test must not satisfy (or fail) a fetch in the next.
    databricks_mod.clear_databricks_token_cache()


def _workspace() -> str:
    ws = os.environ.get("UCODE_TEST_WORKSPACE", "").strip().rstrip("/")
    return normalize_workspace_url(ws) if ws else ""


@pytest.fixture(scope="session")
def e2e_workspace():
    ws = _workspace()
    if not ws:
        pytest.skip("Set UCODE_TEST_WORKSPACE=https://... to run E2E tests")
    return ws


@pytest.fixture(scope="session")
def e2e_token(e2e_workspace):
    return get_databricks_token(e2e_workspace)


@functools.cache
def e2e_state_for(workspace: str) -> dict:
    """Full state dict mirroring what configure_shared_state produces.

    Cached per process so collection-time parametrization (tests/test_e2e.py) and the
    ``e2e_state`` fixture query the live catalogs once. Keyed on the workspace only: a bearer
    command mints a fresh token per call, so a token key would miss and fetch a second snapshot.
    A failed fetch raises and is not cached."""
    token = get_databricks_token(workspace)
    claude_models = fetch_ai_gateway_claude_models(workspace, token)
    gemini_models = fetch_gemini_models(workspace, token)
    codex_models = fetch_codex_models(workspace, token)

    opencode_models: dict = {}
    if claude_models:
        opencode_models["anthropic"] = list(claude_models.values())
    if gemini_models:
        opencode_models["gemini"] = gemini_models

    return {
        "workspace": workspace,
        "claude_models": claude_models,
        "gemini_models": gemini_models,
        "codex_models": codex_models,
        "opencode_models": opencode_models,
        "base_urls": build_shared_base_urls(workspace),
        "managed_configs": {},
    }


@pytest.fixture(scope="session")
def e2e_state(e2e_workspace):
    """Full state dict mirroring what configure_shared_state produces."""
    # Copy so a test mutating its state cannot leak into the cached catalog.
    return dict(e2e_state_for(e2e_workspace))
