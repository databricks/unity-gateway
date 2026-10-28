"""Shared fixtures for E2E tests + global state-isolation guard."""

from __future__ import annotations

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

# The integration suite has its own configuration and subprocess-only fixtures.
# Run it through scripts/run_integration.py, outside this fixture hierarchy.
collect_ignore = ["integration", "e2e_cuj", "native_claude"]


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
    from ucode.agents import claude as claude_mod
    from ucode.agents import codex as codex_mod
    from ucode.agents.desktop import claude as desktop_mod

    state_dir = tmp_path / ".ucode"
    state_dir.mkdir()
    monkeypatch.setattr(state_mod, "STATE_PATH", state_dir / "state.json")
    monkeypatch.setattr(config_io_mod, "APP_DIR", state_dir)
    monkeypatch.setattr(codex_mod, "_otel_service_path", lambda: state_dir / "otel.service")
    monkeypatch.setattr(desktop_mod, "APP_DIR", state_dir)
    monkeypatch.setattr(desktop_auth_mod, "_AUTH_LOCK_PATH", state_dir / "claude-desktop-auth.lock")
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
    # Likewise let any run install the faked policy, as root may; non-root launch tests override it.
    monkeypatch.setattr(managed_config_mod, "is_admin", lambda: True)
    backup_dir = state_dir / "managed-backups"
    monkeypatch.setattr(managed_files_mod, "MANAGED_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(
        managed_files_mod, "MANAGED_BACKUP_MANIFEST_PATH", backup_dir / "manifest.json"
    )
    monkeypatch.setattr(managed_files_mod, "_config_scope", "global")
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


@pytest.fixture(scope="session")
def e2e_state(e2e_workspace, e2e_token):
    """Full state dict mirroring what configure_shared_state produces."""
    claude_models = fetch_ai_gateway_claude_models(e2e_workspace, e2e_token)
    gemini_models = fetch_gemini_models(e2e_workspace, e2e_token)
    codex_models = fetch_codex_models(e2e_workspace, e2e_token)

    opencode_models: dict = {}
    if claude_models:
        opencode_models["anthropic"] = list(claude_models.values())
    if gemini_models:
        opencode_models["gemini"] = gemini_models

    return {
        "workspace": e2e_workspace,
        "claude_models": claude_models,
        "gemini_models": gemini_models,
        "codex_models": codex_models,
        "opencode_models": opencode_models,
        "base_urls": build_shared_base_urls(e2e_workspace),
        "managed_configs": {},
    }


@pytest.fixture
def claude_managed_env(tmp_path, monkeypatch):
    """Factory for the real Claude reconcile flow, with privileged writes landing in ``tmp_path``.

    ``make(managed_doc=None)`` returns ``(managed_path, header_mod_root, writes)``."""
    import shutil

    from ucode import managed_files
    from ucode.agents import claude
    from ucode.managed_files import OS

    def make(managed_doc=None):
        managed_path = tmp_path / "managed-settings.json"
        if managed_doc is not None:
            managed_path.write_text(json.dumps(managed_doc), encoding="utf-8")
        root = tmp_path / "ucode-plugins"
        backup_dir = tmp_path / "managed-backups"
        writes: list[str] = []
        monkeypatch.setattr(managed_files, "managed_files_supported", lambda: True)
        monkeypatch.setattr(managed_files, "current_os", lambda: OS.LINUX)
        monkeypatch.setattr(managed_files, "MANAGED_BACKUP_DIR", backup_dir)
        monkeypatch.setattr(
            managed_files, "MANAGED_BACKUP_MANIFEST_PATH", backup_dir / "manifest.json"
        )

        def _write(target, text):
            writes.append(str(target))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

        monkeypatch.setattr(managed_files, "_sudo_replace", _write)
        monkeypatch.setattr(managed_files, "_sudo_remove", lambda path: path.unlink())
        monkeypatch.setattr(claude, "remove_privileged_tree", shutil.rmtree)
        monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed_path)
        monkeypatch.setattr(claude, "claude_header_mod_root", lambda: root)
        monkeypatch.setattr(claude, "managed_writes_allowed", lambda: True)
        monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(claude, "backup_existing_file", lambda *a, **kw: True)
        monkeypatch.setattr(claude, "save_state", lambda state: None)
        monkeypatch.setattr(claude, "ug_version", lambda: "1.0")
        monkeypatch.setattr(claude, "agent_version", lambda _binary: "2.0")
        monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", tmp_path / "ucode-settings.json")
        monkeypatch.setattr(
            claude,
            "refresh_managed_config",
            lambda *a, **kw: SimpleNamespace(manifest={"claude": {}}),
        )
        # As `ug configure` runs it.
        monkeypatch.setattr(claude, "_explicit_configure", True)
        monkeypatch.delenv("UG_HEADERS", raising=False)
        return managed_path, root, writes

    return make
