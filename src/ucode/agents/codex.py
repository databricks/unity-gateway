"""Codex agent: writes ~/.codex/ucode.config.toml for Databricks-backed Codex."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import signal
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import tomlkit
from tomlkit.exceptions import ParseError

from ucode import gateway_proxy
from ucode.codex_config import (
    catalog_slugs,
    codex_config_args,
    codex_config_precedence_paths,
    codex_managed_config_path,
    custom_catalog_models,
)
from ucode.config_io import (
    APP_DIR,
    ToolSpec,
    backup_existing_file,
    deep_merge_dict,
    is_dry_run,
    prune_key_paths,
    read_json_safe,
    read_toml_safe,
    write_json_file,
    write_toml_file,
)
from ucode.constants import (
    LOOPBACK_HOST,
    MODEL_PROVIDER_SERVICE_HEADER,
    MODEL_SERVICE_PARENT_SCHEMA_HEADER,
    SMART_ROUTER_RECIPE_HEADER,
)
from ucode.custom_oauth import (
    CUSTOM_OAUTH_TIMEOUT_MS,
    CustomOAuthConfig,
    build_custom_auth_token_argv,
    get_custom_client_token,
)
from ucode.databricks import (
    CodexCatalogSource,
    CodexMpsModelCatalogUnavailable,
    _fetch_codex_model_catalog,
    build_auth_token_argv,
    build_tool_base_url,
    get_databricks_token,
)
from ucode.launcher import exec_or_spawn
from ucode.managed_files import (
    ManagedFileWriteUnavailable,
    managed_file_conflicts,
    managed_file_is_verified,
    managed_file_scope,
    managed_file_status,
    managed_files_supported,
    managed_writes_allowed,
    mark_managed_file_verified,
    read_managed_file,
    reconcile_managed_file,
    revert_managed_file,
)
from ucode.smart_routing import v2 as smart_routing_v2
from ucode.smart_routing.codex_hooks import (
    remove_smart_routing_hooks,
    routing_models,
    sync_smart_routing_hooks,
)
from ucode.smart_routing.codex_routing import codex_model_id
from ucode.smart_routing.routing import configured_router_name
from ucode.state import get_provider_service, is_tool_managed, mark_tool_managed, save_state
from ucode.telemetry import agent_version, ug_version
from ucode.ui import print_warning_err

from .args import LaunchOptions
from .codex_catalog import prepare_codex_catalog, validate_codex_catalog

CODEX_CONFIG_DIR = Path.home() / ".codex"
CODEX_PROFILE_NAME = "ucode"
CODEX_CONFIG_PATH = CODEX_CONFIG_DIR / f"{CODEX_PROFILE_NAME}.config.toml"
CODEX_BACKUP_PATH = APP_DIR / "codex-ucode-config.backup.toml"
CODEX_MODEL_CATALOG_PATH = APP_DIR / "codex-model-catalog.json"
LEGACY_CODEX_CONFIG_PATH = CODEX_CONFIG_DIR / "config.toml"
LEGACY_CODEX_BACKUP_PATH = APP_DIR / "codex-config.backup.toml"
CODEX_MODEL_PROVIDER_NAME = "Databricks"
LEGACY_CODEX_MODEL_PROVIDER_NAME = "ucode-databricks"
# ug owns the whole provider http_headers table, so it is pruned before each merge and rewritten
# from render_overlay — dropping stale routing and admin headers that deep_merge cannot delete.
_PROVIDER_HTTP_HEADERS_KEY_PATHS = [
    ["model_providers", CODEX_MODEL_PROVIDER_NAME, "http_headers"],
]
MINIMUM_CODEX_VERSION = (0, 145, 0)
MINIMUM_CODEX_VERSION_TEXT = "0.145.0"
# Codex 0.134.0 introduced per-profile config files; older releases use the legacy layout.
LEGACY_LAYOUT_CODEX_VERSION = (0, 134, 0)
LEGACY_LAYOUT_CODEX_VERSION_TEXT = "0.134.0"
# Retained only to identify and remove state written by the legacy persisted opt-in.
SMART_ROUTING_STATE_KEY = smart_routing_v2.LEGACY_STATE_KEY
APP_SERVER_SMART_ROUTING_STARTING_MODEL = "gpt-5.6-luna"

SPEC: ToolSpec = {
    "binary": "codex",
    "package": "@openai/codex",
    "display": "Codex",
    "config_path": CODEX_CONFIG_PATH,
    "backup_path": CODEX_BACKUP_PATH,
}

MANAGED_KEYS: list[list[str]] = [
    ["model_provider"],
    ["model"],
    ["model_providers", CODEX_MODEL_PROVIDER_NAME],
    ["model_providers", CODEX_MODEL_PROVIDER_NAME, "http_headers"],
]

LEGACY_MANAGED_KEYS: list[list[str]] = [
    ["profile"],
    ["profiles", CODEX_PROFILE_NAME],
    ["model_providers", CODEX_MODEL_PROVIDER_NAME],
    ["model_providers", CODEX_MODEL_PROVIDER_NAME, "http_headers"],
]


def _parse_version(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def minimum_version_error() -> str | None:
    version = agent_version(SPEC["binary"])
    parsed = _parse_version(version)
    if parsed is None or parsed >= MINIMUM_CODEX_VERSION:
        return None
    return f"ug requires Codex {MINIMUM_CODEX_VERSION_TEXT} or newer; found {version}."


def _use_legacy_layout() -> bool:
    """Return True when the installed Codex CLI predates per-profile config files.

    Codex 0.134.0 introduced support for `--profile <name>` resolving to
    `~/.codex/<name>.config.toml`. Older releases only honor a single
    `~/.codex/config.toml` with `[profiles.<name>]` sections. When the version
    is unknown we keep the new layout (matches the prior "unknown does not
    block" semantic).
    """
    parsed = _parse_version(agent_version(SPEC["binary"]))
    if parsed is None:
        return False
    return parsed < LEGACY_LAYOUT_CODEX_VERSION


def has_ucode_config() -> bool:
    """Return whether ucode has already written a Codex configuration."""
    if CODEX_CONFIG_PATH.exists():
        return True
    if not LEGACY_CODEX_CONFIG_PATH.exists():
        return False
    doc = read_toml_safe(LEGACY_CODEX_CONFIG_PATH)
    profiles = doc.get("profiles")
    return (
        doc.get("profile") == CODEX_PROFILE_NAME
        and isinstance(profiles, dict)
        and isinstance(profiles.get(CODEX_PROFILE_NAME), dict)
    )


def _apply_managed_headers(http_headers: dict[str, str], managed: dict[str, str] | None) -> None:
    """Merge admin ``managed`` headers into ``http_headers`` in place; admin wins case-insensitively."""
    for name, value in (managed or {}).items():
        for existing in [key for key in http_headers if key.casefold() == name.casefold()]:
            del http_headers[existing]
        http_headers[name] = value


def _provider_block(
    workspace: str,
    databricks_profile: str | None,
    use_pat: bool = False,
    provider: str | None = None,
    parent_schema: str | None = None,
    custom_oauth: CustomOAuthConfig | None = None,
    managed_http_headers: dict[str, str] | None = None,
) -> dict:
    if custom_oauth:
        auth_argv = build_custom_auth_token_argv(workspace, custom_oauth)
    else:
        auth_argv = build_auth_token_argv(workspace, databricks_profile, use_pat=use_pat)
    base_url = build_tool_base_url("codex", workspace)
    http_headers = {
        "User-Agent": f"ucode/{ug_version()} codex/{agent_version('codex')}",
    }
    if provider:
        http_headers[MODEL_PROVIDER_SERVICE_HEADER] = provider
    elif parent_schema:
        http_headers[MODEL_SERVICE_PARENT_SCHEMA_HEADER] = parent_schema
    if smart_routing_v2.smart_routing_enabled():
        http_headers[SMART_ROUTER_RECIPE_HEADER] = configured_router_name()
    _apply_managed_headers(http_headers, managed_http_headers)
    return {
        "name": "Databricks AI Gateway",
        "base_url": base_url,
        "wire_api": "responses",
        "http_headers": http_headers,
        # Run the `ug auth-token` executable directly (not via `sh -c`) so the
        # helper works on Windows, where there is no POSIX shell (issue #116).
        "auth": {
            "command": auth_argv[0],
            "args": auth_argv[1:],
            "timeout_ms": CUSTOM_OAUTH_TIMEOUT_MS if custom_oauth else 5000,
            "refresh_interval_ms": 900000,
        },
    }


def render_overlay(
    workspace: str,
    model: str | None = None,
    databricks_profile: str | None = None,
    use_pat: bool = False,
    provider: str | None = None,
    parent_schema: str | None = None,
    custom_oauth: CustomOAuthConfig | None = None,
    managed_http_headers: dict[str, str] | None = None,
) -> dict:
    overlay: dict = {"model_provider": CODEX_MODEL_PROVIDER_NAME}
    if model:
        overlay["model"] = model
    overlay["model_providers"] = {
        CODEX_MODEL_PROVIDER_NAME: _provider_block(
            workspace,
            databricks_profile,
            use_pat=use_pat,
            provider=provider,
            parent_schema=parent_schema,
            custom_oauth=custom_oauth,
            managed_http_headers=managed_http_headers,
        ),
    }
    return overlay


def render_legacy_overlay(
    workspace: str,
    model: str | None = None,
    databricks_profile: str | None = None,
    use_pat: bool = False,
    provider: str | None = None,
    parent_schema: str | None = None,
    custom_oauth: CustomOAuthConfig | None = None,
    managed_http_headers: dict[str, str] | None = None,
) -> dict:
    """Overlay for Codex CLI < 0.134.0, which only reads `~/.codex/config.toml`.

    The shared file uses `profile = "ucode"` to select `[profiles.ucode]`, which
    points at the shared `[model_providers.Databricks]` block.
    """
    profile_block: dict = {"model_provider": CODEX_MODEL_PROVIDER_NAME}
    if model:
        profile_block["model"] = model
    return {
        "profile": CODEX_PROFILE_NAME,
        "profiles": {CODEX_PROFILE_NAME: profile_block},
        "model_providers": {
            CODEX_MODEL_PROVIDER_NAME: _provider_block(
                workspace,
                databricks_profile,
                use_pat=use_pat,
                provider=provider,
                parent_schema=parent_schema,
                custom_oauth=custom_oauth,
                managed_http_headers=managed_http_headers,
            ),
        },
    }


def _legacy_config_path() -> Path:
    return CODEX_CONFIG_PATH.parent / "config.toml"


def _legacy_backup_path() -> Path:
    return CODEX_BACKUP_PATH.with_name("codex-legacy-config.backup.toml")


def _has_legacy_ucode_entries(doc: dict) -> bool:
    profiles = doc.get("profiles")
    providers = doc.get("model_providers")
    return (
        doc.get("profile") == CODEX_PROFILE_NAME
        or (isinstance(profiles, dict) and CODEX_PROFILE_NAME in profiles)
        or (isinstance(providers, dict) and LEGACY_CODEX_MODEL_PROVIDER_NAME in providers)
    )


def _strip_legacy_ucode_entries(path: Path) -> bool:
    """Surgically remove ucode's keys from a shared Codex config.

    Drops the top-level ``profile = "ucode"`` selector, ``[profiles.ucode]``,
    and ``[model_providers.ucode-databricks]`` while leaving everything else the
    user has in the file untouched. Returns True if anything was removed.

    Surgical removal beats restoring the backup: ``backup_existing_file`` only
    keeps the first-ever snapshot, so a whole-file restore would clobber edits
    made since ucode first ran.
    """
    if not path.exists():
        return False

    doc = read_toml_safe(path)
    changed = False

    if doc.get("profile") == CODEX_PROFILE_NAME:
        doc.pop("profile", None)
        changed = True

    profiles = doc.get("profiles")
    if isinstance(profiles, dict) and CODEX_PROFILE_NAME in profiles:
        profiles.pop(CODEX_PROFILE_NAME, None)
        if not profiles:
            doc.pop("profiles", None)
        changed = True

    providers = doc.get("model_providers")
    if isinstance(providers, dict) and LEGACY_CODEX_MODEL_PROVIDER_NAME in providers:
        providers.pop(LEGACY_CODEX_MODEL_PROVIDER_NAME, None)
        if not providers:
            doc.pop("model_providers", None)
        changed = True

    if changed:
        write_toml_file(path, doc)
    return changed


def _remove_legacy_ucode_profile() -> None:
    """Remove ucode's old shared-config entries when configuring modern Codex.

    Strips the legacy ``profile``/``[profiles.ucode]`` selector and the
    ``[model_providers.ucode-databricks]`` provider block that older ucode
    versions deep-merged into ``~/.codex/config.toml``.
    """
    path = _legacy_config_path()
    if path == CODEX_CONFIG_PATH or not path.exists():
        return

    if _has_legacy_ucode_entries(read_toml_safe(path)):
        backup_existing_file(path, _legacy_backup_path())
        _strip_legacy_ucode_entries(path)


def revert_legacy_shared_config() -> bool:
    """Undo legacy in-place edits to ``~/.codex/config.toml`` on revert.

    Codex CLI < 0.134.0 had ucode deep-merge ``profile = "ucode"``,
    ``[profiles.ucode]``, and ``[model_providers.ucode-databricks]`` into the
    user's real shared config, which routes every bare ``codex`` invocation
    through the workspace gateway. ``ucode revert`` only restored the
    per-profile file, leaving those edits in place. Surgically strip them here.

    Also remove the shared app catalog reference installed by modern ucode.
    Returns True if anything was removed.
    """
    legacy_changed = _strip_legacy_ucode_entries(_legacy_config_path())
    app_catalog_changed = detach_app_model_catalog()
    if app_catalog_changed and CODEX_MODEL_CATALOG_PATH.exists():
        CODEX_MODEL_CATALOG_PATH.unlink()
    return legacy_changed or app_catalog_changed


def configured_paths(state: dict) -> list[str]:
    """The Codex config files ug writes; the OS-managed file is added by the dispatcher.

    Includes the model catalog only when a managed static list drives it (same condition as
    :func:`write_tool_config`), so the summary names it exactly when it was written."""
    paths = [str(CODEX_CONFIG_PATH)]
    static_models = state.get("codex_static_models")
    if (
        isinstance(static_models, list)
        and static_models
        and not get_provider_service(state, "codex")
    ):
        paths.append(str(CODEX_MODEL_CATALOG_PATH))
    return paths


def write_tool_config(
    state: dict,
    model: str | None = None,
    provider: str | None = None,
    parent_schema: str | None = None,
) -> dict:
    workspace = state["workspace"]
    # Leave model selection to Codex. The gateway still receives the configured
    # provider and authentication settings, while Codex uses its own default.
    # A managed default is the sole exception.
    managed_model = state.get("codex_default_model")
    chosen_model = managed_model if isinstance(managed_model, str) else None
    databricks_profile = state.get("profile")
    static_models = state.get("codex_static_models")
    static_models = static_models if isinstance(static_models, list) and static_models else None

    if _use_legacy_layout():
        if static_models and not provider:
            raise RuntimeError(
                "This Codex version cannot use the managed static model catalog. "
                "Upgrade Codex and verify `codex debug models --bundled` works, then retry."
            )
        # Codex < 0.134.0 only reads ~/.codex/config.toml. Write the shared
        # config with [profiles.ucode] + shared [model_providers.Databricks]
        # and skip the per-profile-file cleanup that would normally strip
        # ucode's entry from the shared file.
        backup_existing_file(LEGACY_CODEX_CONFIG_PATH, LEGACY_CODEX_BACKUP_PATH)
        overlay = render_legacy_overlay(
            workspace,
            chosen_model,
            databricks_profile,
            use_pat=bool(state.get("use_pat")),
            provider=provider,
            parent_schema=parent_schema,
            custom_oauth=state.get("custom_oauth"),
            managed_http_headers=state.get("codex_http_headers"),
        )
        doc = read_toml_safe(LEGACY_CODEX_CONFIG_PATH)
        prune_key_paths(doc, _PROVIDER_HTTP_HEADERS_KEY_PATHS)
        deep_merge_dict(doc, overlay)
        # deep_merge can't drop keys, so clear model preferences from an earlier run.
        profiles = doc.get("profiles")
        if (
            chosen_model is None
            and isinstance(profiles, dict)
            and isinstance(profiles.get(CODEX_PROFILE_NAME), dict)
        ):
            for key in ("model", "model_reasoning_effort"):
                profiles[CODEX_PROFILE_NAME].pop(key, None)
        _set_provider_header(doc, None)
        write_toml_file(LEGACY_CODEX_CONFIG_PATH, doc)
        state = mark_tool_managed(state, "codex", LEGACY_MANAGED_KEYS)
        save_state(state)
        return state

    catalog_path = str(CODEX_MODEL_CATALOG_PATH) if static_models and not provider else None
    # Build and validate before modifying config so failure cannot leave a stale
    # catalog enabled or partially rewrite the user's configuration.
    try:
        catalog = (
            prepare_codex_catalog(SPEC["binary"], static_models)
            if static_models and not provider
            else None
        )
    except RuntimeError:
        _detach_app_catalog_after_failure()
        raise

    _remove_legacy_ucode_profile()
    # Back up only a file that predates ucode's management of the tool. A
    # re-configure would otherwise snapshot ucode's own generated file, and
    # revert would restore that snapshot instead of deleting the file.
    if not is_tool_managed(state, "codex"):
        backup_existing_file(CODEX_CONFIG_PATH, CODEX_BACKUP_PATH)
    overlay = render_overlay(
        workspace,
        chosen_model,
        databricks_profile,
        use_pat=bool(state.get("use_pat")),
        provider=provider,
        parent_schema=parent_schema,
        custom_oauth=state.get("custom_oauth"),
        managed_http_headers=state.get("codex_http_headers"),
    )

    def compose(base: dict, *, include_catalog: bool = True) -> dict:
        prune_key_paths(base, _PROVIDER_HTTP_HEADERS_KEY_PATHS)
        deep_merge_dict(base, copy.deepcopy(overlay))
        # deep_merge can't drop keys, so clear model preferences from an earlier run.
        if chosen_model is None and not smart_routing_v2.smart_routing_enabled():
            for key in ("model", "model_reasoning_effort"):
                base.pop(key, None)
        if include_catalog:
            if catalog_path:
                base["model_catalog_json"] = catalog_path
            else:
                base.pop("model_catalog_json", None)
        _set_provider_header(base, None)
        return base

    if catalog is not None:
        _sync_app_model_catalog(catalog)
    elif not is_dry_run():
        detach_app_model_catalog()
        if CODEX_MODEL_CATALOG_PATH.exists():
            CODEX_MODEL_CATALOG_PATH.unlink()

    doc = read_toml_safe(CODEX_CONFIG_PATH)
    compose(doc)
    sync_smart_routing_hooks(
        doc,
        state,
        enabled=False,
    )
    write_toml_file(CODEX_CONFIG_PATH, doc)
    _reconcile_managed_config(state, lambda base: compose(base, include_catalog=False))
    state = mark_tool_managed(state, "codex", MANAGED_KEYS)
    save_state(state)
    return state


def _is_gpt_family(model: str) -> bool:
    """Return True if this id is in the GPT family (versioned or OSS variants)."""
    tail = model.split("/")[-1]
    if tail.startswith("system.ai."):
        tail = tail[len("system.ai.") :]
    return tail.startswith("gpt-")


def _parse_managed_config(text: str) -> dict:
    try:
        return tomlkit.parse(text)
    except ParseError as exc:
        raise RuntimeError(f"invalid TOML: {exc}") from exc


def managed_config_is_current(state: dict) -> bool:
    path = codex_managed_config_path()
    if path is None:
        return True
    required_scope = "managed" if managed_writes_allowed() else None
    return managed_file_is_verified(state, "codex", path, required_scope=required_scope)


def managed_config_status(state: dict) -> tuple[Path | None, str, str]:
    path = codex_managed_config_path()
    status, backup = managed_file_status(state, "codex", path, parser=_parse_managed_config)
    return path, status, backup


def revert_managed_config() -> str:
    return revert_managed_file(
        "codex",
        display="Codex",
        parser=_parse_managed_config,
        dumper=tomlkit.dumps,
    )


def _reconcile_managed_config(state: dict, compose: Callable[[dict], dict]) -> None:
    """Reconcile Codex's highest-precedence config while preserving unrelated policy."""
    path = codex_managed_config_path()
    if path is None:
        print_warning_err(
            "Machine-wide Codex settings aren't supported on this platform; skipped the managed "
            "config."
        )
        return
    if path.is_symlink():
        raise RuntimeError(
            f"Refusing to use Codex managed settings through symlink {path}. Replace it with a "
            "regular file or contact your administrator."
        )
    current_text = read_managed_file(path)
    try:
        existing = _parse_managed_config(current_text) if current_text is not None else {}
    except RuntimeError as exc:
        raise RuntimeError(
            f"Cannot safely update Codex managed settings at {path}: {exc}. ucode did not modify "
            "the file. Repair it or contact your administrator."
        ) from exc
    managed_before = copy.deepcopy(existing)
    desired_doc = compose(existing)
    if not managed_writes_allowed():
        conflicts = managed_file_conflicts(managed_before, desired_doc, MANAGED_KEYS)
        if conflicts:
            raise RuntimeError(
                "Codex configuration cannot be applied non-interactively because OS-managed "
                f"settings at {path} override ucode values: {', '.join(conflicts)}. Run `ucode "
                "configure --agent codex` from an interactive terminal or contact your "
                "administrator."
            )
        mark_managed_file_verified(state, "codex", path, scope="local-compatible")
        return
    try:
        reconcile_managed_file(
            path,
            tomlkit.dumps(desired_doc),
            tool="codex",
            display="Codex",
            owned_paths=MANAGED_KEYS,
        )
    except ManagedFileWriteUnavailable:
        conflicts = managed_file_conflicts(managed_before, desired_doc, MANAGED_KEYS)
        if conflicts:
            raise
        print_warning_err(
            f"Codex OS-managed settings could not be updated at {path}; continuing with local "
            f"settings at {CODEX_CONFIG_PATH}."
        )
        mark_managed_file_verified(state, "codex", path, scope="local-compatible")
        return
    mark_managed_file_verified(state, "codex", path)


MANAGED_MCP_CONFIG_KEY = "mcp_servers"


def managed_mcp_uses_managed_file() -> bool:
    """Whether Codex's managed MCP servers belong in the OS-managed file rather than user scope.

    Codex reads ``/etc/codex/managed_config.toml`` natively and merges its ``[mcp_servers]`` table
    with the developer's own ``~/.codex/config.toml`` servers, so a managed entry never hides a
    personal one. It fits whenever the platform supports the sudo reconcile and the run is
    interactive; otherwise the caller falls back to the user-scope registration."""
    return managed_files_supported() and managed_writes_allowed()


def managed_mcp_entry(argv: list[str]) -> dict:
    """A ``[mcp_servers.<name>]`` stdio entry from the ``ug mcp-proxy`` argv (same as user scope)."""
    return {"command": argv[0], "args": list(argv[1:])}


def reconcile_managed_mcp(state: dict, servers: dict[str, dict]) -> bool:
    """Overwrite ug's ``[mcp_servers]`` table in Codex's OS-managed file with ``servers``.

    ``servers`` is the freshly resolved managed set keyed by name; an empty map clears the table.
    The managed file is the source of truth, so this is a wipe-and-rewrite, not a diff. Every other
    managed key is preserved, including the model configuration ug wrote earlier this run. Returns
    True when the managed file is the delivery mechanism, False when it cannot be used (unsupported
    platform or a non-interactive run), so the caller routes those servers to the user-scope
    registration instead."""
    path = codex_managed_config_path()
    if path is None or not managed_writes_allowed():
        return False
    if path.is_symlink():
        raise RuntimeError(
            f"Refusing to use Codex managed settings through symlink {path}. Replace it with a "
            "regular file or contact your administrator."
        )
    current_text = read_managed_file(path)
    try:
        existing = (
            _parse_managed_config(current_text) if current_text is not None else tomlkit.document()
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"Cannot safely update Codex managed settings at {path}: {exc}. ucode did not modify "
            "the file. Repair it or contact your administrator."
        ) from exc
    # Nothing managed to clear: never create or rewrite the file just to remove an absent key.
    if not servers and MANAGED_MCP_CONFIG_KEY not in existing:
        return True
    if servers:
        table = tomlkit.table()
        for name, entry in servers.items():
            server = tomlkit.table()
            server.update(entry)
            table[name] = server
        existing[MANAGED_MCP_CONFIG_KEY] = table
    else:
        del existing[MANAGED_MCP_CONFIG_KEY]
    try:
        reconcile_managed_file(
            path,
            tomlkit.dumps(existing),
            tool="codex",
            display="Codex",
            owned_paths=[[MANAGED_MCP_CONFIG_KEY]],
        )
    except ManagedFileWriteUnavailable:
        return False
    # Preserve the scope the model reconcile recorded (e.g. relay-compatible); an MCP-only write only
    # refreshes the fingerprint, it does not change how the file relates to the model settings.
    mark_managed_file_verified(state, "codex", path, scope=managed_file_scope(state, "codex"))
    return True


def read_managed_mcp_urls() -> dict[str, str]:
    """``{name: url}`` for ug's managed MCP servers in Codex's OS-managed file (empty if none).

    Read-only, for ``ug mcp list`` to tag managed servers now that the managed file is their source
    of truth rather than ug state. The gateway URL is the ``--url`` argument of the proxy command."""
    path = codex_managed_config_path()
    if path is None:
        return {}
    try:
        text = read_managed_file(path)
        doc = _parse_managed_config(text) if text else {}
    except RuntimeError:
        return {}
    servers = doc.get(MANAGED_MCP_CONFIG_KEY)
    if not isinstance(servers, dict):
        return {}
    urls: dict[str, str] = {}
    for name, entry in servers.items():
        args = entry.get("args") if isinstance(entry, dict) else None
        if isinstance(args, list) and "--url" in args:
            index = args.index("--url")
            if index + 1 < len(args):
                urls[name] = str(args[index + 1])
    return urls


def default_model(state: dict) -> str | None:
    """Return a managed Codex model, or leave selection to Codex."""
    if isinstance(state.get("codex_default_model"), str):
        return state["codex_default_model"]
    if smart_routing_v2.smart_routing_enabled():
        return _smart_routing_config_model(state)
    clear_model_preferences(state)
    return None


def _smart_routing_config_model(state: dict) -> str | None:
    """Read the startup model in managed, profile, then user config precedence."""
    model = state.get("codex_default_model")
    if isinstance(model, str) and model.strip():
        return model

    for path in config_precedence_paths():
        model = read_toml_safe(path).get("model")
        if isinstance(model, str) and model.strip():
            return model
    return None


def config_precedence_paths() -> tuple[Path, ...]:
    """Return Codex config paths in managed, profile, then user precedence."""
    return codex_config_precedence_paths(
        codex_managed_config_path(),
        CODEX_CONFIG_PATH,
    )


def clear_model_preferences(state: dict) -> bool:
    """Remove ucode profile model preferences so Codex selects its default."""
    if smart_routing_v2.smart_routing_enabled():
        return False
    if isinstance(state.get("codex_default_model"), str):
        return False
    doc = read_toml_safe(CODEX_CONFIG_PATH)
    changed = False
    for key in ("model", "model_reasoning_effort"):
        if key in doc:
            doc.pop(key)
            changed = True
    if changed:
        # Never snapshot ucode's own generated file here; revert would restore
        # the snapshot instead of deleting the file.
        if not is_tool_managed(state, "codex"):
            backup_existing_file(CODEX_CONFIG_PATH, CODEX_BACKUP_PATH)
        write_toml_file(CODEX_CONFIG_PATH, doc)
    return changed


def _set_provider_header(config: dict, provider: str | None) -> None:
    _set_routing_header(config, MODEL_PROVIDER_SERVICE_HEADER, provider)


def _set_parent_schema_header(config: dict, parent_schema: str | None) -> None:
    _set_routing_header(config, MODEL_SERVICE_PARENT_SCHEMA_HEADER, parent_schema)


def _set_routing_header(config: dict, header: str, value: str | None) -> None:
    model_providers = config.get("model_providers")
    if not isinstance(model_providers, dict):
        return
    provider_block = model_providers.get(CODEX_MODEL_PROVIDER_NAME)
    if not isinstance(provider_block, dict):
        return
    headers = provider_block.get("http_headers")
    if not isinstance(headers, dict):
        provider_block["http_headers"] = {}
        headers = provider_block["http_headers"]
    if value:
        headers[header] = value
    else:
        headers.pop(header, None)


def _model_catalog_path(workspace: str, scope: str) -> Path:
    key = f"{workspace.rstrip('/')}\0{scope}".encode()
    digest = hashlib.sha256(key).hexdigest()[:16]
    base = CODEX_MODEL_CATALOG_PATH
    return base.with_name(f"{base.stem}-{digest}{base.suffix}")


def _write_model_catalog(path: Path, catalog: dict) -> None:
    if is_dry_run():
        write_json_file(path, catalog)
        return
    temp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_temp_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        os.close(fd)
        temp_path = Path(raw_temp_path)
        write_json_file(temp_path, catalog)
        os.replace(temp_path, path)
    except OSError as exc:
        raise RuntimeError(f"Could not write Codex model catalog at {path}.") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _is_ucode_catalog_reference(value: object) -> bool:
    return isinstance(value, str) and Path(value).expanduser() == CODEX_MODEL_CATALOG_PATH


def _read_app_config() -> tomlkit.TOMLDocument:
    path = _legacy_config_path()
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return tomlkit.document()
    except (OSError, UnicodeError, ParseError) as exc:
        raise RuntimeError(f"Cannot update Codex App settings at {path}: {exc}") from exc


def _install_app_catalog_reference() -> bool:
    """Point Codex App at ucode's stable catalog without replacing a user catalog."""
    if is_dry_run():
        return False
    path = _legacy_config_path()
    doc = _read_app_config()
    existing = doc.get("model_catalog_json")
    if existing is not None and not _is_ucode_catalog_reference(existing):
        print_warning_err(
            f"Codex App already uses the custom model catalog {existing}; leaving it unchanged."
        )
        return False
    provider = doc.get("model_provider")
    if provider not in (None, CODEX_MODEL_PROVIDER_NAME, LEGACY_CODEX_MODEL_PROVIDER_NAME):
        if _is_ucode_catalog_reference(existing):
            doc.pop("model_catalog_json", None)
            write_toml_file(path, doc)
            changed = True
        else:
            changed = False
        print_warning_err(
            f"Codex App uses the custom provider {provider}; leaving its model catalog unmanaged."
        )
        return changed
    catalog_path = str(CODEX_MODEL_CATALOG_PATH)
    if existing == catalog_path:
        return False
    doc["model_catalog_json"] = catalog_path
    write_toml_file(path, doc)
    return True


def detach_app_model_catalog() -> bool:
    """Remove only a shared catalog reference owned by ucode."""
    if is_dry_run():
        return False
    path = _legacy_config_path()
    if not path.exists():
        return False
    doc = _read_app_config()
    if not _is_ucode_catalog_reference(doc.get("model_catalog_json")):
        return False
    doc.pop("model_catalog_json", None)
    write_toml_file(path, doc)
    _print_app_catalog_restart_notice()
    return True


def _detach_app_catalog_after_failure() -> None:
    """Keep the original catalog error when shared settings cannot be edited."""
    try:
        detach_app_model_catalog()
    except RuntimeError as exc:
        print_warning_err(str(exc))


def _print_app_catalog_restart_notice() -> None:
    # A desktop reconnect can reuse a daemon whose model manager still holds
    # the startup catalog. Never restart it here: it may be running tasks.
    print_warning_err(
        "Codex App model catalog changed. Existing app servers keep their startup "
        "model list. After active tasks finish, restart the app server on the connected "
        "host, then reconnect. For a Codex standalone daemon, run "
        "`codex app-server daemon restart`; otherwise restart the process or application "
        "that owns the app server. Reconnecting alone does not reload the catalog."
    )


def _sync_app_model_catalog(catalog: dict) -> None:
    """Publish a validated catalog without overwriting unreadable app settings."""
    if is_dry_run():
        return
    doc = _read_app_config()
    catalog_changed = read_json_safe(CODEX_MODEL_CATALOG_PATH) != catalog
    _write_model_catalog(CODEX_MODEL_CATALOG_PATH, catalog)
    reference_changed = _install_app_catalog_reference()
    app_uses_catalog = reference_changed or (
        _is_ucode_catalog_reference(doc.get("model_catalog_json"))
        and doc.get("model_provider")
        in (None, CODEX_MODEL_PROVIDER_NAME, LEGACY_CODEX_MODEL_PROVIDER_NAME)
    )
    if reference_changed or (app_uses_catalog and catalog_changed):
        _print_app_catalog_restart_notice()


def _launch_token(state: dict, workspace: str, *, force_refresh: bool = False) -> str:
    """The token Codex authenticates with: a custom-OAuth client token when configured,
    else the CLI profile. The single auth-selection point — the OTLP proxy's token
    provider (_otel_token_provider) delegates here so both mint the same principal."""
    custom_oauth = state.get("custom_oauth")
    if isinstance(custom_oauth, dict):
        return get_custom_client_token(
            workspace,
            custom_oauth["client_id"],
            custom_oauth["redirect_url"],
            scopes=custom_oauth["scopes"],
            profile=custom_oauth.get("profile"),
            force_refresh=force_refresh,
        )
    return get_databricks_token(workspace, state.get("profile"), force_refresh=force_refresh)


def _tool_args_select_model(tool_args: list[str]) -> bool:
    """Whether the passthrough args already pin a model via ``-m``/``--model``."""
    return any(arg in ("-m", "--model") or arg.startswith(("--model=", "-m=")) for arg in tool_args)


def _reject_managed_model_catalog() -> None:
    path = codex_managed_config_path()
    if path is None:
        return
    text = read_managed_file(path)
    if text is None:
        return
    try:
        managed = _parse_managed_config(text)
    except RuntimeError as exc:
        raise RuntimeError(f"Cannot read Codex managed settings at {path}: {exc}") from exc
    if "model_catalog_json" in managed:
        raise RuntimeError(
            f"Codex managed settings at {path} define model_catalog_json, which overrides "
            "model discovery. Remove it or contact your administrator."
        )


def _otel_proxy_overlay(endpoint: str) -> dict:
    """Codex OTLP trace exporter pointed at the loopback proxy — no auth header, since
    the proxy injects a freshly-minted Databricks token that codex could not refresh."""
    return {
        "otel": {
            "trace_exporter": {
                "otlp-http": {
                    "endpoint": endpoint,
                    "protocol": "binary",
                }
            }
        }
    }


def _otel_token_provider(state: dict, workspace: str) -> Callable[[bool], str]:
    """The proxy's token_provider: mints from the same source Codex uses for inference
    (via _launch_token) so exported traces are attributed to the same principal."""
    return lambda force: _launch_token(state, workspace, force_refresh=force)


def _launch_codex_with_otel_proxy(
    state: dict,
    base_argv: list[str],
    tool_args: list[str],
    workspace: str,
) -> None:
    """Run the loopback OTLP refresh proxy for the session, with Codex as a child.

    The proxy must outlive the launch, so Codex runs as a child rather than
    exec-replacing this process; mirrors Claude's relayed launch. The proxy binds an
    OS-assigned port and tears everything down when Codex exits (or fails to spawn).
    """
    server, cache, client = gateway_proxy.start_otel_proxy(
        workspace, _otel_token_provider(state, workspace)
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    endpoint = f"http://{LOOPBACK_HOST}:{server.server_address[1]}/v1/traces"
    otel_args = codex_config_args(_otel_proxy_overlay(endpoint))
    proc = subprocess.Popen([*base_argv, *otel_args, *tool_args])
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        returncode = proc.wait()
    finally:
        cache.stop()
        server.shutdown()
        client.close()
    raise SystemExit(returncode)


def _run_codex(
    state: dict,
    base_argv: list[str],
    tool_args: list[str],
    *,
    otel_tracing: bool,
    workspace: str | None,
) -> None:
    """Launch Codex — via the loopback proxy when OTLP tracing is on, else exec-replace."""
    if tool_args[:1] == ["update"]:
        # exec replaces ug, so reattach only on a later validated refresh.
        detach_app_model_catalog()
    if otel_tracing and workspace:
        _launch_codex_with_otel_proxy(state, base_argv, tool_args, workspace)
    else:
        exec_or_spawn([*base_argv, *tool_args])


def launch(
    state: dict,
    tool_args: list[str],
    *,
    options: LaunchOptions,
) -> None:
    if options.launch_smart_routing:
        _launch_smart_routing(state, tool_args)
        return
    clear_model_preferences(state)
    binary = SPEC["binary"]
    workspace = state.get("workspace")
    launch_provider = state.get("_codex_launch_provider")
    transient_provider = (
        launch_provider.strip()
        if isinstance(launch_provider, str) and launch_provider.strip()
        else None
    )
    launch_parent_schema = state.get("_codex_launch_parent_schema")
    parent_schema = (
        launch_parent_schema.strip()
        if isinstance(launch_parent_schema, str) and launch_parent_schema.strip()
        else None
    )
    # Launch-scoped admin routing wins over persisted developer configuration. A transient provider
    # is most specific; otherwise a transient UC parent must suppress a saved provider.
    provider = transient_provider or (
        None if parent_schema else get_provider_service(state, "codex")
    )
    if workspace and (provider or parent_schema):
        _reject_managed_model_catalog()
    token = None
    otel_tracing = bool(workspace and state.get("codex_otel_tracing"))
    if workspace:
        token = _launch_token(state, workspace)
        os.environ["OAUTH_TOKEN"] = token
    if _use_legacy_layout():
        print_warning_err(
            f"Codex {agent_version(binary)} is outdated. Upgrade Codex to "
            f"{LEGACY_LAYOUT_CODEX_VERSION_TEXT} or newer, then run `codex --version` to verify "
            "the active installation."
        )
        _run_codex(
            state,
            [binary, "--profile", CODEX_PROFILE_NAME],
            tool_args,
            otel_tracing=otel_tracing,
            workspace=workspace,
        )
        return
    # Layer ucode's named profile as ordinary config overrides. Unlike
    # `--profile`, `--config` is accepted by runtime, utility, and server
    # commands, so every invocation keeps the same Databricks settings without
    # classifying Codex subcommands or probing and retrying the real command.
    profile_doc = read_toml_safe(CODEX_CONFIG_PATH)
    if not profile_doc:
        raise RuntimeError(
            f"Cannot launch Codex with the ucode profile because {CODEX_CONFIG_PATH} "
            "is missing or empty. Run `ucode configure --agents codex` first."
        )
    _set_provider_header(profile_doc, provider)
    _set_parent_schema_header(profile_doc, parent_schema if not provider else None)
    updating = tool_args[:1] == ["update"]
    if updating and _is_ucode_catalog_reference(profile_doc.get("model_catalog_json")):
        profile_doc.pop("model_catalog_json")
    if workspace and token and (provider or parent_schema) and not updating:
        try:
            if provider is not None:
                catalog_source = CodexCatalogSource.PROVIDER
                catalog_identifier = provider
                catalog_scope = f"provider:{provider}"
            elif parent_schema is not None:
                catalog_source = CodexCatalogSource.PARENT_SCHEMA
                catalog_identifier = parent_schema
                catalog_scope = f"parent:{parent_schema}"
            else:
                raise RuntimeError("Codex model discovery requires a provider or parent schema.")
            catalog = _fetch_codex_model_catalog(
                workspace,
                token,
                source=catalog_source,
                identifier=catalog_identifier,
            )
            validate_codex_catalog(binary, catalog)
        except CodexMpsModelCatalogUnavailable:
            detach_app_model_catalog()
        except RuntimeError:
            # A failed discovery/validation must not leave a previous workspace's
            # catalog active in independently launched app servers.
            _detach_app_catalog_after_failure()
            raise
        else:
            catalog_path = _model_catalog_path(workspace, catalog_scope)
            _write_model_catalog(catalog_path, catalog)
            _sync_app_model_catalog(catalog)
            profile_doc["model_catalog_json"] = str(catalog_path)
            # Codex otherwise boots on its bundled default model (e.g. gpt-5.6-sol),
            # which an MPS's allowlist doesn't route, so the first request 403s. Pin
            # the MPS's primary (first) target unless the user chose a model or a
            # managed default already applies.
            if not profile_doc.get("model") and not _tool_args_select_model(tool_args):
                slugs = catalog_slugs(catalog)
                if slugs:
                    profile_doc["model"] = slugs[0]
    _run_codex(
        state,
        [binary, *codex_config_args(profile_doc)],
        tool_args,
        otel_tracing=otel_tracing,
        workspace=workspace,
    )


def _launch_smart_routing(state: dict, tool_args: list[str]) -> None:
    """Launch the Codex TUI through the smart-routing interposer."""
    binary = SPEC["binary"]

    configured_model = _smart_routing_config_model(state)
    # Prefer the custom catalog if it exists.
    models = custom_catalog_models() or routing_models(state)
    start_model = (
        configured_model
        or (codex_model_id(models[0]) if models else None)
        or APP_SERVER_SMART_ROUTING_STARTING_MODEL
    )
    smart_routing_v2.launch_codex(
        state,
        tool_args,
        binary=binary,
        start_model=start_model,
        render_overlay=render_overlay,
    )


def disable_smart_routing(state: dict) -> bool:
    """Disable routing and remove only ucode's Codex routing hooks."""
    state.pop(SMART_ROUTING_STATE_KEY, None)
    if state.get("workspace"):
        save_state(state)
    changed = False
    for path in (CODEX_CONFIG_PATH, LEGACY_CODEX_CONFIG_PATH):
        if not path.exists():
            continue
        doc = read_toml_safe(path)
        if remove_smart_routing_hooks(doc):
            write_toml_file(path, doc)
            changed = True
    from ucode.smart_routing.codex_routing import clear_routing_artifacts

    clear_routing_artifacts()
    return changed


def validate_cmd(binary: str) -> list[str]:
    return [
        binary,
        "--profile",
        CODEX_PROFILE_NAME,
        "exec",
        "--skip-git-repo-check",
        "say hi in 5 words or less",
    ]
