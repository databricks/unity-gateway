"""Claude Code agent: writes ~/.claude/settings.json env block."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, cast

from ucode import gateway_proxy
from ucode.config_io import (
    APP_DIR,
    ToolSpec,
    backup_existing_file,
    deep_merge_dict,
    is_dry_run,
    read_json_safe,
    write_json_file,
)
from ucode.constants import (
    LOOPBACK_HOST,
    MCP_CLEANUP_SCOPES,
    MCP_USER_SCOPE,
    MODEL_PROVIDER_SERVICE_HEADER,
    MODEL_SERVICE_PARENT_SCHEMA_HEADER,
)
from ucode.custom_oauth import CustomOAuthConfig, build_custom_auth_shell_command
from ucode.databricks import (
    build_auth_shell_command,
    build_otel_headers_shell_command,
    build_otel_traces_endpoint,
    build_tool_base_url,
    get_databricks_token,
    ug_binary,
)
from ucode.launcher import exec_or_spawn
from ucode.managed_files import (
    OS,
    ManagedFileWriteUnavailable,
    current_os,
    managed_file_conflicts,
    managed_file_is_verified,
    managed_file_status,
    managed_last_applied_paths,
    managed_writes_allowed,
    mark_managed_file_verified,
    read_managed_file,
    reconcile_managed_file,
    restore_unchanged_managed_paths,
    revert_managed_file,
    write_private_json_file,
)
from ucode.mcp_oauth import CLAUDE_CODE_OAUTH_CLIENT_ID, MCP_OAUTH_CALLBACK_PORT
from ucode.smart_routing import v2 as smart_routing_v2
from ucode.smart_routing.claude_hooks import (
    remove_smart_routing_hooks,
    sync_smart_routing_hooks,
)
from ucode.state import (
    MANAGED_OVERLAY_KEY,
    developer_state_from_resolved,
    is_tool_managed,
    load_full_state,
    load_state,
    mark_tool_managed,
    save_state,
)
from ucode.telemetry import agent_version, ug_version
from ucode.tracing import tracing_env
from ucode.ui import print_note, print_success, print_warning

from .args import LaunchOptions, has_explicit_model_arg


class _WindowsFileLockApi(Protocol):
    LK_LOCK: int
    LK_UNLCK: int

    def locking(self, fd: int, mode: int, nbytes: int) -> None: ...


GATEWAY_MODEL_DISCOVERY_ENV_VAR = "ENABLE_CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY"
# If set, Claude Code launches in headless mode instead of the interactive login flow.
CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_CONFIG_DIR = Path.home() / ".claude"
CLAUDE_SETTINGS_PATH = CLAUDE_CONFIG_DIR / "ucode-settings.json"
CLAUDE_MCP_CONFIG_PATH = Path.home() / ".claude.json"
# The default model is stored in Claude's default user settings, not the ucode settings.
CLAUDE_USER_SETTINGS_PATH = CLAUDE_CONFIG_DIR / "settings.json"
CLAUDE_BACKUP_PATH = APP_DIR / "claude-ucode-settings.backup.json"
CLAUDE_PICKER_MANAGEMENT_PATH = APP_DIR / "claude-picker-management.json"
CLAUDE_PICKER_MANAGEMENT_VERSION = 2
WEB_SEARCH_MCP_STATE_KEY = "claude_web_search_mcp"
WEB_SEARCH_MCP_GENERATION_KEY = "claude_web_search_generation"
MINIMUM_CLAUDE_VERSION = (2, 1, 248)
MINIMUM_CLAUDE_VERSION_TEXT = "2.1.248"

SPEC: ToolSpec = {
    "binary": "claude",
    "package": "@anthropic-ai/claude-code",
    "display": "Claude Code",
    "config_path": CLAUDE_SETTINGS_PATH,
    "backup_path": CLAUDE_BACKUP_PATH,
}

_MISSING_PICKER_VALUE = object()
_picker_thread_lock = threading.RLock()
_picker_lock_state = threading.local()
_web_search_registration_thread_lock = threading.Lock()

# Retained only to identify and remove state written by the legacy persisted opt-in.
SMART_ROUTING_STATE_KEY = smart_routing_v2.LEGACY_STATE_KEY


def _parse_version(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _minimum_version_requirement_message(version: str) -> str:
    feature = "Smart routing" if smart_routing_v2.smart_routing_enabled() else "Model discovery"
    return (
        f"{feature} requires Claude Code {MINIMUM_CLAUDE_VERSION_TEXT} or newer. "
        f"Your current version is Claude Code {version}."
    )


def minimum_version_error() -> str | None:
    if (
        os.environ.get(GATEWAY_MODEL_DISCOVERY_ENV_VAR) != "1"
        and not smart_routing_v2.smart_routing_enabled()
    ):
        return None
    version = agent_version(SPEC["binary"])
    parsed = _parse_version(version)
    if parsed is None or parsed >= MINIMUM_CLAUDE_VERSION:
        return None
    return _minimum_version_requirement_message(version)


def _resolve_web_search_model(state: dict) -> str | None:
    """Pick the model the web_search MCP server should call. Prefers an
    explicit override in state, otherwise the first endpoint discovered as
    Responses-API-capable. Returns None if no GPT endpoint is available —
    callers should skip the MCP wiring in that case."""
    override = state.get("web_search_model")
    if isinstance(override, str) and override.strip():
        return override.strip()
    codex_models = state.get("codex_models") or []
    if isinstance(codex_models, list) and codex_models:
        first = codex_models[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return None


WEB_SEARCH_MCP_NAME = "web_search"
# Matches both the AI Gateway form (`databricks-claude-opus-4-8`) and the UC
# model-services form (`system.ai.claude-opus-4-8`).
_CLAUDE_MODEL_RE = re.compile(
    r"^(?:system\.ai\.)?(?:databricks-)?claude-(opus|sonnet)-(\d+)(?:-(\d+))?(.*)$"
)

# Env keys the MLflow Stop hook reads to route traces. Written into the
# settings `env` block alongside the hook itself.
CLAUDE_TRACING_ENV_KEYS = (
    "MLFLOW_CLAUDE_TRACING_ENABLED",
    "MLFLOW_TRACKING_URI",
    "MLFLOW_EXPERIMENT_ID",
    "MLFLOW_TRACING_SQL_WAREHOUSE_ID",
)
# OTLP trace-export keys owned by the managed configuration path.
CLAUDE_OTEL_TRACE_ENV_KEYS = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
    "OTEL_TRACES_EXPORTER",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS",
    "CLAUDE_CODE_PROPAGATE_TRACEPARENT",
)


def _otel_trace_env(workspace: str) -> dict[str, str]:
    """Build Claude Code's client-side OTLP trace configuration."""
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": build_otel_traces_endpoint(workspace),
        "CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS": "900000",
        "CLAUDE_CODE_PROPAGATE_TRACEPARENT": "1",
    }


# Model-selection env keys ucode manages. Existing family defaults in the enterprise-managed file
# are preserved unless Coding Agent Config explicitly supplies that family.
CLAUDE_MANAGED_MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
)
CLAUDE_DEFAULT_MODEL_ENV_KEYS = {
    "fable": "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}
# Launch-scoped feature flags that ucode may write into Claude settings. These
# must be removed again when the corresponding launch flag is absent.
CLAUDE_CONDITIONAL_ENV_KEYS = ("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",)
# Env keys ucode used to write but no longer does; stripped from the managed
# settings file on every launch so stale values never linger.
CLAUDE_REMOVED_ENV_KEYS = ("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",)
CLAUDE_MANAGED_PICKER_KEYS = ("availableModels", "enforceAvailableModels", "modelPicker")
ANTHROPIC_CUSTOM_HEADERS_ENV_KEY = "ANTHROPIC_CUSTOM_HEADERS"
CLAUDE_MANAGED_CUSTOM_HEADER_NAMES = frozenset(
    {
        "x-databricks-use-coding-agent-mode",
        "user-agent",
        MODEL_PROVIDER_SERVICE_HEADER.casefold(),
        MODEL_SERVICE_PARENT_SCHEMA_HEADER.casefold(),
    }
)
CLAUDE_TRACING_STOP_HOOK_SUFFIX = " autolog claude stop-hook"
# Tracing is driven by an `mlflow autolog claude stop-hook` Stop hook, run by
# the `mlflow` CLI on each session end. Pin to 3.11.x: 3.12 dropped the Unity
# Catalog trace-write path, so traces silently land in the classic store
# instead of the experiment's UC table. ucode installs this via `uv tool` at
# `configure tracing` time (where UV_INDEX_URL is set), then writes the hook
# with the resolved absolute path — so the hook needs no uv or index at run
# time, and can't be shadowed by a project venv's mlflow.
MLFLOW_CLI_SPEC = "mlflow[databricks]>=3.11,<3.12"
MINIMUM_MLFLOW_VERSION = (3, 11)
# Upper bound (exclusive) — an installed mlflow at or above this is too new and
# must be replaced, not just left alone.
MAXIMUM_MLFLOW_VERSION = (3, 12)

# Relayed drops the user scope to deliberately omit the stale apiKeyHelper. Only applied to relayed
# launches — normal launches keep loading user settings (hooks/permissions) as before.
_RELAYED_SETTING_SOURCES = "project,local"


def configured_paths(state: dict) -> list[str]:
    """The Claude config file ug writes; the OS-managed file is added by the dispatcher."""
    return [str(CLAUDE_SETTINGS_PATH)]


def _managed_settings_path() -> Path | None:
    """OS-specific location of Claude Code's enterprise managed-settings.json.
    Returns None on unsupported platforms."""
    if current_os() is OS.LINUX:
        return Path("/etc/claude-code/managed-settings.json")
    if current_os() is OS.MACOS:
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return None


def _parse_managed_settings(text: str) -> dict:
    try:
        settings = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(settings, dict):
        raise RuntimeError("the top-level JSON value must be an object")
    return settings


def _dump_managed_settings(settings: dict) -> str:
    return json.dumps(settings, indent=2) + "\n"


def managed_settings_are_current(state: dict) -> bool:
    path = _managed_settings_path()
    if path is None:
        return True
    if state.get("claude_relayed"):
        required_scope = "relay-compatible"
    elif managed_writes_allowed():
        required_scope = "managed"
    else:
        required_scope = None
    return managed_file_is_verified(state, "claude", path, required_scope=required_scope)


def gateway_model_discovery_setting_is_absent() -> bool:
    """Return whether model discovery is absent from persistent Claude settings."""
    env = read_json_safe(CLAUDE_SETTINGS_PATH).get("env")
    actual = (
        env.get("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY") if isinstance(env, dict) else None
    )
    return actual is None


def managed_settings_status(state: dict) -> tuple[Path | None, str, str]:
    path = _managed_settings_path()
    status, backup = managed_file_status(state, "claude", path, parser=_parse_managed_settings)
    return path, status, backup


def revert_managed_settings() -> str:
    path = _managed_settings_path()
    if path is None:
        return revert_managed_file(
            "claude",
            display="Claude Code",
            parser=_parse_managed_settings,
            dumper=_dump_managed_settings,
        )
    with _picker_management_lock():
        current_text = read_managed_file(path)
        current = _parse_managed_settings(current_text) if current_text is not None else {}
        lease, recovery, phase = _recover_picker_transition("managed", path, current)
        if phase == "applying" and recovery in {"target", "drift"}:
            raise RuntimeError(
                "Cannot safely revert Claude Code managed settings while a picker update is "
                "incomplete. Re-run configuration to repair its metadata, then run `ucode "
                "revert` again."
            )
        if not lease and recovery is None:
            lease = _legacy_managed_picker_management(path, current)

        transitioned = copy.deepcopy(current)
        if lease:
            _transition_private_picker(transitioned, {}, lease)
        target = _picker_group(transitioned)
        journaled = bool(lease or recovery is not None)
        if journaled:
            _begin_picker_transition(
                "managed",
                path,
                lease,
                {},
                _picker_group(current),
                target,
                phase="reverting",
            )
        acquisition_lease = [
            {
                "path": [key],
                "baseline_exists": entry["original_exists"],
                **(
                    {"baseline": copy.deepcopy(entry["original"])}
                    if entry["original_exists"]
                    else {}
                ),
                "applied": copy.deepcopy(entry["last_applied"]),
            }
            for key, entry in lease.items()
        ]

        def mark_revert_written() -> None:
            written_text = read_managed_file(path)
            written = _parse_managed_settings(written_text) if written_text is not None else {}
            if not _picker_group_matches(written, target):
                raise RuntimeError(f"Could not verify restored Claude picker settings at {path}.")
            _mark_picker_revert_written("managed", path)

        result = revert_managed_file(
            "claude",
            display="Claude Code",
            parser=_parse_managed_settings,
            dumper=_dump_managed_settings,
            acquisition_lease=acquisition_lease,
            excluded_owned_paths=[[key] for key in CLAUDE_MANAGED_PICKER_KEYS],
            before_backup_delete=mark_revert_written if journaled else None,
        )
        written_text = read_managed_file(path)
        written = _parse_managed_settings(written_text) if written_text is not None else {}
        if journaled and not _picker_group_matches(written, target):
            raise RuntimeError(f"Could not verify restored Claude picker settings at {path}.")
        if journaled:
            _save_managed_picker_management(path, {})
        return result


def revert_settings(state: dict) -> tuple[str, bool]:
    """Revert both Claude settings scopes as one serialized transaction."""
    with _picker_management_lock():
        return revert_managed_settings(), revert_private_settings(state)


def _managed_relayed_conflicts(path: Path) -> list[str]:
    """Return managed settings that would override Claude subscription relay auth."""
    text = read_managed_file(path)
    if text is None:
        return []
    try:
        settings = _parse_managed_settings(text)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Cannot safely inspect Claude Code managed settings at {path}: {exc}. Repair the "
            "file or contact your administrator."
        ) from exc
    conflicts: list[str] = []
    if settings.get("apiKeyHelper"):
        conflicts.append("apiKeyHelper")
    env = settings.get("env")
    if isinstance(env, dict):
        if env.get("ANTHROPIC_BASE_URL"):
            conflicts.append("env.ANTHROPIC_BASE_URL")
        if env.get("ANTHROPIC_CUSTOM_HEADERS"):
            conflicts.append("env.ANTHROPIC_CUSTOM_HEADERS")
    return conflicts


def relayed_proxy_base_url(state: dict) -> str:
    """Loopback base URL for the relayed refresh proxy, allocating a free port
    on first call and caching it in state so config and launch agree."""
    port = state.get("relayed_proxy_port")
    if not isinstance(port, int):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((LOOPBACK_HOST, 0))
            port = sock.getsockname()[1]
        state["relayed_proxy_port"] = port
    return f"http://{LOOPBACK_HOST}:{port}"


def _web_search_mcp_entry(workspace: str, search_model: str, profile: str | None = None) -> dict:
    """Stdio MCP server entry pointing at `ug mcp web-search`. Resolves
    the absolute path to the `ug` binary so launchers without the right
    PATH (e.g. desktop GUI launchers) still find it."""
    env: dict[str, str] = {
        "DATABRICKS_HOST": workspace,
        "UCODE_WEB_SEARCH_MODEL": search_model,
    }
    if profile:
        env["DATABRICKS_CONFIG_PROFILE"] = profile
    return {
        "type": "stdio",
        "command": ug_binary(),
        "args": ["mcp", "web-search"],
        "env": env,
    }


def render_overlay(
    workspace: str,
    model: str | None,
    claude_models: dict[str, str] | None = None,
    disable_web_search: bool = False,
    profile: str | None = None,
    use_pat: bool = False,
    custom_oauth: CustomOAuthConfig | None = None,
    provider: str | None = None,
    provider_models: dict[str, str] | None = None,
    relayed: bool = False,
    relayed_base_url: str | None = None,
    route_root_model: str | None = None,
    custom_model: str | None = None,
    parent_schema: str | None = None,
    static_models: list[str] | None = None,
    otel_tracing: bool = False,
) -> tuple[dict, list[list[str]]]:
    """Return (overlay, managed_key_paths) for Claude settings.json.

    NOTE: MCP servers are NOT written here. Claude Code reads `mcpServers`
    from `~/.claude.json`, not `~/.claude/settings.json` — registration goes
    through `claude mcp add-json` (see `_register_web_search_mcp`).

    When `provider` is set (a `<catalog>.<schema>.<name>` Model Provider
    Service), the request is routed to that external provider via the
    `Databricks-Model-Provider-Service` header. An Anthropic-backed provider
    understands Claude Code's own canonical model names, so no model id is
    pinned. A Bedrock-backed provider exposes different model ids (e.g.
    `us.anthropic.claude-sonnet-4-6`), passed in `provider_models` by family —
    those get pinned via the `ANTHROPIC_DEFAULT_*_MODEL` env vars.

    When `relayed` is set (a credential-less Anthropic subscription-relay MPS,
    Claude Max/Team/Enterprise), Claude Code's own keychain OAuth must remain the
    `Authorization` credential, so no `apiKeyHelper` is written (it would outrank
    the subscription OAuth). The Databricks credential rides in the
    `X-Databricks-AI-Gateway-Token` swap header, injected per request by a local
    refresh proxy at `relayed_base_url` — not written here."""
    if relayed:
        if not relayed_base_url:
            raise RuntimeError("Relayed launch requires a proxy base URL.")
        base_url = relayed_base_url
    else:
        base_url = build_tool_base_url("claude", workspace)
    # ANTHROPIC_CUSTOM_HEADERS is parsed as `key: value` pairs separated by
    # newlines (Anthropic SDK convention). Setting User-Agent here overrides
    # the SDK's default UA on outbound requests so the gateway can attribute
    # traffic to ucode.
    header_lines = [
        "x-databricks-use-coding-agent-mode: true",
        f"User-Agent: ucode/{ug_version()} claude/{agent_version('claude')}",
    ]
    if provider:
        header_lines.append(f"{MODEL_PROVIDER_SERVICE_HEADER}: {provider}")
    elif parent_schema:
        header_lines.append(f"{MODEL_SERVICE_PARENT_SCHEMA_HEADER}: {parent_schema}")
    # Relayed: the X-Databricks-AI-Gateway-Token swap header is added per request
    # by the refresh proxy, not here — a static value would go stale mid-session.
    custom_headers = "\n".join(header_lines)
    env: dict[str, str] = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_CUSTOM_HEADERS": custom_headers,
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "900000",
        # 1h prompt caching needs the extended-cache-ttl beta header, which
        # Claude Code only sends when experimental betas are enabled — so we must
        # not set CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS (see CLAUDE_REMOVED_ENV_KEYS).
        "ENABLE_PROMPT_CACHING_1H": "1",
        "ENABLE_TOOL_SEARCH": "true",
        "CLAUDE_CODE_USE_GATEWAY": "1",
    }
    # Intentionally NOT setting ANTHROPIC_MODEL by default. Setting it produces a
    # duplicate catalog row in Claude Code's /model picker (e.g. "Opus 4.8 (1M
    # context) ✓") on top of the family-alias row from ANTHROPIC_DEFAULT_OPUS_MODEL.
    # Without it, Default resolves through the pinned family alias and the picker
    # shows only one row per model. `ucode claude -- --model X` still overrides for
    # a single session via Claude Code's own --model flag.
    #
    # The one exception is smart routing: `route_root_model` pins the
    # router's per-launch pick for the root session as ANTHROPIC_MODEL. The
    # duplicate-picker-row cost is acceptable because the whole point is to launch
    # on the routed model rather than the family default.
    _ = model  # API stability; no longer pinned via env.
    if route_root_model:
        env["ANTHROPIC_MODEL"] = route_root_model
    # A Bedrock-backed provider needs its provider-side ids pinned verbatim
    # (Claude Code's canonical names aren't routable there). These come from the
    # service's targets, already de-duped to one id per family upstream.
    elif provider and provider_models:
        if provider_models.get("opus"):
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = provider_models["opus"]
        if provider_models.get("sonnet"):
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = provider_models["sonnet"]
        if provider_models.get("haiku"):
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = provider_models["haiku"]
    # With an Anthropic Model Provider Service, the header routes to the external
    # provider and Claude Code's own canonical model names are sent verbatim —
    # pinning a Databricks model id here would mislabel the picker and isn't
    # routable.
    elif claude_models and not provider and not parent_schema:
        # Picker rows show the raw routable id (e.g. "system.ai.claude-opus-4-8[1m]")
        # so users can see which gateway-routable model is behind each shortcut.
        # We deliberately don't set the `_NAME` companion env vars — the raw id
        # is more useful than a friendly label for debugging gateway routing.
        for family, key in CLAUDE_DEFAULT_MODEL_ENV_KEYS.items():
            if family_model := claude_models.get(family):
                env[key] = (
                    _maybe_add_1m_suffix(family_model)
                    if family in ("opus", "sonnet")
                    else family_model
                )
    # Relayed omits apiKeyHelper so Claude Code's subscription OAuth stays the
    # Authorization credential; every other path uses it as the gateway auth.
    overlay: dict = {"env": env}
    if relayed:
        keys = [["env", k] for k in env]
    else:
        if custom_oauth:
            overlay["apiKeyHelper"] = build_custom_auth_shell_command(workspace, custom_oauth)
        else:
            overlay["apiKeyHelper"] = build_auth_shell_command(workspace, profile, use_pat=use_pat)
        keys = [["apiKeyHelper"]] + [["env", k] for k in env]

    # Disable Claude Code's built-in WebSearch: it declares Anthropic's hosted
    # `web_search_20250305` server tool, which the Databricks gateway rejects
    # (HTTP 400: "Input tag 'web_search_20250305' ... does not match"), so the
    # model wastes a turn on it before falling back. A *bare* `permissions.deny`
    # entry removes the tool from Claude's context entirely, so it is never
    # advertised to the model nor sent to the gateway. (Claude Code has no
    # `disabledTools` setting — the `permissions` block is the only settings.json
    # mechanism for built-in tools; a bare tool name in `deny` drops it, whereas
    # a scoped rule like `WebSearch(*)` would leave it advertised.) The
    # replacement `web_search` MCP server is registered separately via the
    # claude CLI.
    if disable_web_search:
        overlay["permissions"] = {"deny": ["WebSearch"]}
        keys.append(["permissions", "deny"])

    if static_models and not provider and not parent_schema and not relayed:
        overlay["availableModels"] = list(static_models)
        overlay["enforceAvailableModels"] = True
        overlay["modelPicker"] = {
            "replaceBuiltInOptions": True,
            "options": [{"model": m, "label": _picker_label(m)} for m in static_models],
        }
        keys += [[key] for key in CLAUDE_MANAGED_PICKER_KEYS]

    if otel_tracing:
        otel_env = _otel_trace_env(workspace)
        env.update(otel_env)
        overlay["otelHeadersHelper"] = build_otel_headers_shell_command(
            workspace, profile, use_pat=use_pat
        )
        keys += [["env", key] for key in otel_env] + [["otelHeadersHelper"]]

    return overlay, keys


def _picker_label(model: str) -> str:
    """A short picker label for a model id — the raw id minus the ``system.ai.`` prefix."""
    return model.removeprefix("system.ai.")


@contextmanager
def _picker_management_lock() -> Iterator[None]:
    """Serialize sidecar and settings transitions that share picker ownership."""
    if is_dry_run():
        yield
        return

    with _picker_thread_lock:
        depth = getattr(_picker_lock_state, "depth", 0)
        if depth:
            _picker_lock_state.depth = depth + 1
            try:
                yield
            finally:
                _picker_lock_state.depth = depth
            return
        _picker_lock_state.depth = 1
        try:
            with _picker_process_lock():
                yield
        finally:
            _picker_lock_state.depth = 0


@contextmanager
def _picker_process_lock() -> Iterator[None]:
    lock_path = CLAUDE_PICKER_MANAGEMENT_PATH.with_name(
        f"{CLAUDE_PICKER_MANAGEMENT_PATH.name}.lock"
    )
    with _process_file_lock(lock_path):
        yield


@contextmanager
def _process_file_lock(lock_path: Path) -> Iterator[None]:
    """Hold one cross-platform advisory byte lock at an explicit private path."""
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+b")
    except OSError as exc:
        raise RuntimeError(f"Cannot lock Claude picker metadata at {lock_path}: {exc}") from exc
    with lock_file:
        try:
            os.chmod(lock_path, 0o600)
            _lock_picker_file(lock_file)
        except OSError as exc:
            raise RuntimeError(f"Cannot lock Claude picker metadata at {lock_path}: {exc}") from exc
        try:
            yield
        finally:
            _unlock_picker_file(lock_file)


@contextmanager
def _web_search_registration_lock() -> Iterator[None]:
    lock_path = CLAUDE_PICKER_MANAGEMENT_PATH.with_name("claude-web-search-registration.lock")
    with _web_search_registration_thread_lock:
        with _process_file_lock(lock_path):
            yield


def _lock_picker_file(lock_file) -> None:
    if current_os() is OS.WINDOWS:
        import msvcrt

        windows_lock = cast("_WindowsFileLockApi", msvcrt)
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        windows_lock.locking(lock_file.fileno(), windows_lock.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(lock_file, fcntl.LOCK_EX)


def _unlock_picker_file(lock_file) -> None:
    if current_os() is OS.WINDOWS:
        import msvcrt

        windows_lock = cast("_WindowsFileLockApi", msvcrt)
        lock_file.seek(0)
        windows_lock.locking(lock_file.fileno(), windows_lock.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(lock_file, fcntl.LOCK_UN)


def _valid_picker_entries(entries: object) -> bool:
    if not isinstance(entries, dict) or set(entries) != set(CLAUDE_MANAGED_PICKER_KEYS):
        return False
    for entry in entries.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("original_exists"), bool):
            return False
        expected = {"original_exists", "last_applied"}
        if entry["original_exists"]:
            expected.add("original")
        if set(entry) != expected:
            return False
    return True


def _picker_group(settings: dict) -> dict[str, dict]:
    group: dict[str, dict] = {}
    for key in CLAUDE_MANAGED_PICKER_KEYS:
        exists = key in settings
        entry: dict = {"exists": exists}
        if exists:
            entry["value"] = copy.deepcopy(settings[key])
        group[key] = entry
    return group


def _valid_picker_group(group: object) -> bool:
    if not isinstance(group, dict) or set(group) != set(CLAUDE_MANAGED_PICKER_KEYS):
        return False
    for entry in group.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("exists"), bool):
            return False
        if set(entry) != ({"exists", "value"} if entry["exists"] else {"exists"}):
            return False
    return True


def _picker_group_matches(settings: dict, group: dict[str, dict]) -> bool:
    return _picker_group(settings) == group


def _private_document_sha256(settings: dict) -> str:
    encoded = json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_private_json_object(path: Path) -> dict:
    """Read private Claude transaction input strictly; only a missing file means empty."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RuntimeError(f"Cannot read Claude settings at {path}: {exc}") from exc
    try:
        settings = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Cannot parse Claude settings at {path}: {exc}") from exc
    if not isinstance(settings, dict):
        raise RuntimeError(f"Claude settings at {path} must contain a JSON object.")
    return settings


def _load_picker_management() -> dict[str, dict]:
    """Load per-scope acquisition leases for ucode's Claude picker fields."""
    path = CLAUDE_PICKER_MANAGEMENT_PATH
    if path.is_symlink():
        raise RuntimeError(f"Refusing to read symlinked Claude picker metadata at {path}.")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RuntimeError(f"Cannot read Claude picker metadata at {path}: {exc}") from exc
    try:
        metadata = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Cannot parse Claude picker metadata at {path}: {exc}") from exc
    if (
        not isinstance(metadata, dict)
        or metadata.get("version") != CLAUDE_PICKER_MANAGEMENT_VERSION
        or not isinstance(metadata.get("leases"), dict)
    ):
        raise RuntimeError(f"Invalid Claude picker metadata at {path}.")

    leases = metadata["leases"]
    if not leases or not set(leases).issubset({"private", "managed"}):
        raise RuntimeError(f"Invalid Claude picker metadata at {path}.")
    pending_keys = {"phase", "prior", "intended", "before", "target"}
    document_proof_keys = {
        "before_document_exists",
        "before_document_sha256",
        "target_document_exists",
        "target_document_sha256",
    }
    for scope, lease in leases.items():
        if not isinstance(lease, dict) or not isinstance(lease.get("path"), str):
            raise RuntimeError(f"Invalid Claude picker metadata at {path}.")
        pending = lease.get("pending")
        if pending is None:
            if set(lease) != {"path", "keys"} or not _valid_picker_entries(lease.get("keys")):
                raise RuntimeError(f"Invalid Claude picker metadata at {path}.")
            continue
        if (
            set(lease) != {"path", "pending"}
            or not isinstance(pending, dict)
            or frozenset(pending)
            not in {frozenset(pending_keys), frozenset(pending_keys | document_proof_keys)}
            or pending.get("phase") not in {"applying", "reverting", "revert_written"}
            or (pending.get("prior") is not None and not _valid_picker_entries(pending["prior"]))
            or (
                pending.get("intended") is not None
                and not _valid_picker_entries(pending["intended"])
            )
            or not _valid_picker_group(pending.get("before"))
            or not _valid_picker_group(pending.get("target"))
            or (
                document_proof_keys.issubset(pending)
                and (
                    scope != "private"
                    or not isinstance(pending["before_document_exists"], bool)
                    or not isinstance(pending["target_document_exists"], bool)
                    or not isinstance(pending["before_document_sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", pending["before_document_sha256"]) is None
                    or not isinstance(pending["target_document_sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", pending["target_document_sha256"]) is None
                )
            )
        ):
            raise RuntimeError(f"Invalid Claude picker metadata at {path}.")
    return copy.deepcopy(leases)


def _save_picker_management(leases: dict[str, dict]) -> None:
    path = CLAUDE_PICKER_MANAGEMENT_PATH
    if leases:
        write_private_json_file(
            path,
            {
                "version": CLAUDE_PICKER_MANAGEMENT_VERSION,
                "leases": leases,
            },
        )
        return
    if is_dry_run():
        return
    if path.is_symlink():
        raise RuntimeError(f"Refusing to remove symlinked Claude picker metadata at {path}.")
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Cannot remove Claude picker metadata at {path}: {exc}") from exc


def _save_picker_lease(scope: str, path: Path, entries: dict[str, dict]) -> None:
    if entries and not _valid_picker_entries(entries):
        raise RuntimeError("Invalid Claude picker lease.")
    leases = _load_picker_management()
    if entries:
        leases[scope] = {"path": str(path), "keys": copy.deepcopy(entries)}
    else:
        leases.pop(scope, None)
    _save_picker_management(leases)


def _begin_picker_transition(
    scope: str,
    path: Path,
    previous: dict[str, dict],
    intended: dict[str, dict],
    before: dict[str, dict],
    target: dict[str, dict],
    *,
    phase: str = "applying",
    document_transition: tuple[bool, dict, bool, dict] | None = None,
) -> None:
    if (
        (previous and not _valid_picker_entries(previous))
        or (intended and not _valid_picker_entries(intended))
        or not _valid_picker_group(before)
        or not _valid_picker_group(target)
        or phase not in {"applying", "reverting"}
    ):
        raise RuntimeError("Invalid Claude picker transition.")
    pending = {
        "phase": phase,
        "prior": copy.deepcopy(previous) or None,
        "intended": copy.deepcopy(intended) or None,
        "before": copy.deepcopy(before),
        "target": copy.deepcopy(target),
    }
    if document_transition is not None:
        before_exists, before_document, target_exists, target_document = document_transition
        pending.update(
            {
                "before_document_exists": before_exists,
                "before_document_sha256": _private_document_sha256(before_document),
                "target_document_exists": target_exists,
                "target_document_sha256": _private_document_sha256(target_document),
            }
        )
    leases = _load_picker_management()
    leases[scope] = {
        "path": str(path),
        "pending": pending,
    }
    _save_picker_management(leases)


def _recover_picker_transition(
    scope: str, path: Path, settings: dict
) -> tuple[dict[str, dict], str | None, str | None]:
    """Resolve an interrupted transition without discarding its retry proof."""
    leases = _load_picker_management()
    lease = leases.get(scope)
    if lease is None:
        return {}, None, None
    if lease["path"] != str(path):
        raise RuntimeError(
            f"Claude picker metadata for {scope} targets {lease['path']}, not {path}."
        )
    pending = lease.get("pending")
    if pending is None:
        return copy.deepcopy(lease["keys"]), None, None

    current_exists = path.exists()
    document_sha256 = _private_document_sha256(settings)
    has_document_proof = "target_document_sha256" in pending
    target_matches = (
        current_exists == pending["target_document_exists"]
        and document_sha256 == pending["target_document_sha256"]
        if has_document_proof
        else _picker_group_matches(settings, pending["target"])
    )
    before_matches = (
        current_exists == pending["before_document_exists"]
        and document_sha256 == pending["before_document_sha256"]
        if has_document_proof
        else _picker_group_matches(settings, pending["before"])
    )
    if target_matches:
        recovered = pending["intended"]
        recovery = "target"
    elif pending["phase"] == "revert_written":
        # Once the revert target was durably verified, every later non-target value is external
        # post-revert drift, even if it exactly recreates the old pre-revert settings.
        recovered = pending["intended"]
        recovery = "drift"
    elif before_matches:
        recovered = pending["prior"]
        recovery = "before"
    else:
        # The complete group is neither the pre-write nor intended state. Treat it as external
        # drift after the attempted transition; the next transition will preserve or rebase it.
        recovered = pending["intended"]
        recovery = "drift"
    return copy.deepcopy(recovered or {}), recovery, pending["phase"]


def _save_private_picker_management(entries: dict[str, dict]) -> None:
    _save_picker_lease("private", CLAUDE_SETTINGS_PATH, entries)


def _save_managed_picker_management(path: Path, entries: dict[str, dict]) -> None:
    _save_picker_lease("managed", path, entries)


def _mark_picker_revert_written(scope: str, path: Path) -> None:
    """Durably record that one revert target was written and fully verified."""
    leases = _load_picker_management()
    lease = leases.get(scope)
    if not isinstance(lease, dict):
        raise RuntimeError(f"Claude {scope} revert journal is not ready to commit.")
    pending = lease.get("pending")
    if (
        not isinstance(pending, dict)
        or lease.get("path") != str(path)
        or pending.get("phase") != "reverting"
    ):
        raise RuntimeError(f"Claude {scope} revert journal is not ready to commit.")
    pending["phase"] = "revert_written"
    _save_picker_management(leases)


def _apply_picker_group(settings: dict, group: dict[str, dict]) -> None:
    for key, entry in group.items():
        if entry["exists"]:
            settings[key] = copy.deepcopy(entry["value"])
        else:
            settings.pop(key, None)


def _state_records_legacy_picker_ownership(state: dict) -> bool:
    states = [state]
    workspaces = load_full_state().get("workspaces")
    if isinstance(workspaces, dict):
        states.extend(entry for entry in workspaces.values() if isinstance(entry, dict))
    for candidate in states:
        managed_configs = candidate.get("managed_configs")
        claude_management = (
            managed_configs.get("claude") if isinstance(managed_configs, dict) else None
        )
        paths = claude_management.get("keys") if isinstance(claude_management, dict) else None
        if isinstance(paths, list) and any([key] in paths for key in CLAUDE_MANAGED_PICKER_KEYS):
            return True
    return False


def _legacy_private_picker_management(settings: dict) -> dict[str, dict]:
    """Bootstrap pre-sidecar ownership only from an all-three managed snapshot proof."""
    managed_path = _managed_settings_path()
    picker_paths = [[key] for key in CLAUDE_MANAGED_PICKER_KEYS]
    if managed_path is None:
        return {}
    last_managed, recorded_paths = managed_last_applied_paths(
        "claude", managed_path, picker_paths, parser=_parse_managed_settings
    )
    if {tuple(path) for path in recorded_paths} != {tuple(path) for path in picker_paths}:
        return {}
    private_matches_last = all(
        settings.get(key, _MISSING_PICKER_VALUE) == last_managed.get(key, _MISSING_PICKER_VALUE)
        for key in CLAUDE_MANAGED_PICKER_KEYS
    )
    if not private_matches_last:
        return {}
    original = _read_private_json_object(CLAUDE_BACKUP_PATH)
    entries: dict[str, dict] = {}
    for key in CLAUDE_MANAGED_PICKER_KEYS:
        if key not in settings:
            continue
        entry = {
            "original_exists": key in original,
            "last_applied": copy.deepcopy(settings[key]),
        }
        if key in original:
            entry["original"] = copy.deepcopy(original[key])
        entries[key] = entry
    return entries if _valid_picker_entries(entries) else {}


def _legacy_managed_picker_management(path: Path, settings: dict) -> dict[str, dict]:
    """Bootstrap one all-three lease from integrity-checked legacy manifest snapshots."""
    picker_paths = [[key] for key in CLAUDE_MANAGED_PICKER_KEYS]
    applied = copy.deepcopy(settings)
    restored, restored_paths = restore_unchanged_managed_paths(
        "claude",
        path,
        copy.deepcopy(settings),
        picker_paths,
        parser=_parse_managed_settings,
    )
    if {tuple(candidate) for candidate in restored_paths} != {
        tuple(candidate) for candidate in picker_paths
    }:
        return {}
    if not all(key in applied for key in CLAUDE_MANAGED_PICKER_KEYS):
        return {}
    entries: dict[str, dict] = {}
    for key in CLAUDE_MANAGED_PICKER_KEYS:
        entry = {
            "original_exists": key in restored,
            "last_applied": copy.deepcopy(applied[key]),
        }
        if key in restored:
            entry["original"] = copy.deepcopy(restored[key])
        entries[key] = entry
    return entries


def _transition_private_picker(
    settings: dict, overlay: dict, previous: dict[str, dict]
) -> dict[str, dict]:
    """Restore or rebase private picker fields before applying the current overlay."""
    static_picker_active = all(key in overlay for key in CLAUDE_MANAGED_PICKER_KEYS)
    if not static_picker_active:
        unchanged = bool(previous) and all(
            settings.get(key, _MISSING_PICKER_VALUE) == entry["last_applied"]
            for key, entry in previous.items()
        )
        if unchanged:
            for key, entry in previous.items():
                if entry["original_exists"]:
                    settings[key] = copy.deepcopy(entry["original"])
                else:
                    settings.pop(key, None)
        return {}

    current_management: dict[str, dict] = {}
    previous_unchanged = bool(previous) and all(
        settings.get(key, _MISSING_PICKER_VALUE) == entry["last_applied"]
        for key, entry in previous.items()
    )
    for key in CLAUDE_MANAGED_PICKER_KEYS:
        current = settings.get(key, _MISSING_PICKER_VALUE)
        previous_entry = previous.get(key)
        if previous_unchanged and previous_entry is not None:
            original_exists = previous_entry["original_exists"]
            original = previous_entry.get("original")
        else:
            original_exists = current is not _MISSING_PICKER_VALUE
            original = current
        entry = {
            "original_exists": original_exists,
            "last_applied": copy.deepcopy(overlay[key]),
        }
        if original_exists:
            entry["original"] = copy.deepcopy(original)
        current_management[key] = entry
    return current_management


def _reconcile_private_settings(
    state: dict, overlay: dict, compose: Callable[[dict], dict]
) -> None:
    """Apply private settings with a crash-safe picker lease; caller holds the lock."""
    settings = _read_private_json_object(CLAUDE_SETTINGS_PATH)
    settings_existed = CLAUDE_SETTINGS_PATH.exists()
    settings_before = copy.deepcopy(settings)
    before = _picker_group(settings)
    previous, recovery, phase = _recover_picker_transition(
        "private", CLAUDE_SETTINGS_PATH, settings
    )
    if phase == "reverting" and recovery == "drift":
        raise RuntimeError(
            "Cannot safely configure Claude settings while an interrupted private revert has "
            "unverified external changes. Restore the file to its pre-revert state or remove "
            f"{CLAUDE_SETTINGS_PATH}, then run `ug revert` again."
        )
    legacy_management = (
        not previous and recovery is None and _state_records_legacy_picker_ownership(state)
    )

    # Back up only a file that predates ucode's management of the tool. A re-configure would
    # otherwise snapshot ucode's generated file, and revert would restore that snapshot.
    if not is_tool_managed(state, "claude") and not previous and not legacy_management:
        backup_existing_file(CLAUDE_SETTINGS_PATH, CLAUDE_BACKUP_PATH)
    elif phase == "revert_written" and recovery == "drift":
        # The old backup may still exist if the verified revert crashed before cleanup. Replace
        # it atomically so this complete post-revert document becomes the next lease baseline.
        if settings_existed:
            write_json_file(CLAUDE_BACKUP_PATH, settings_before)
        else:
            _remove_private_backup()
    elif (
        recovery == "target"
        and phase in {"reverting", "revert_written"}
        and not CLAUDE_BACKUP_PATH.exists()
    ):
        # The prior revert restored and verified this complete document before its backup was
        # removed, but failed to clear the journal. Treat it as the baseline of this new lease.
        backup_existing_file(CLAUDE_SETTINGS_PATH, CLAUDE_BACKUP_PATH)
    if legacy_management:
        previous = _legacy_private_picker_management(settings)

    intended = _transition_private_picker(settings, overlay, previous)
    desired = compose(settings)
    target = _picker_group(desired)
    if not previous and intended and before == target:
        # Matching IT policy was not changed by ucode, so it must not become leased merely
        # because the requested static policy happens to have the same values.
        intended = {}
    transition = bool(recovery is not None or previous or intended)
    if transition:
        _begin_picker_transition(
            "private",
            CLAUDE_SETTINGS_PATH,
            previous,
            intended,
            before,
            target,
            document_transition=(settings_existed, settings_before, True, desired),
        )
    write_json_file(CLAUDE_SETTINGS_PATH, desired)
    if is_dry_run():
        return
    written = _read_private_json_object(CLAUDE_SETTINGS_PATH)
    if written != desired:
        raise RuntimeError(f"Could not verify Claude settings at {CLAUDE_SETTINGS_PATH}.")
    if transition:
        _save_private_picker_management(intended)


def _remove_private_backup() -> None:
    try:
        CLAUDE_BACKUP_PATH.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Failed to remove Claude settings backup at {CLAUDE_BACKUP_PATH}"
        ) from exc


def revert_private_settings(state: dict) -> bool:
    """Restore private settings atomically while honoring the picker acquisition lease."""
    with _picker_management_lock():
        current = _read_private_json_object(CLAUDE_SETTINGS_PATH)
        lease, recovery, phase = _recover_picker_transition(
            "private", CLAUDE_SETTINGS_PATH, current
        )
        legacy_claim = _state_records_legacy_picker_ownership(state)

        if phase in {"reverting", "revert_written"} and recovery == "target":
            _remove_private_backup()
            _save_private_picker_management({})
            return True
        if phase == "reverting" and recovery == "drift":
            raise RuntimeError(
                "Cannot safely resume Claude settings revert because the file changed after the "
                "revert was journaled but before its target was verified. Restore the file to "
                f"its pre-revert state or remove {CLAUDE_SETTINGS_PATH}, then retry."
            )
        if not lease and recovery is None and legacy_claim:
            lease = _legacy_private_picker_management(current)

        backup_exists = CLAUDE_BACKUP_PATH.exists()
        managed_configs = state.get("managed_configs")
        state_managed = isinstance(managed_configs, dict) and bool(managed_configs.get("claude"))
        if not (backup_exists or state_managed or lease or recovery is not None or legacy_claim):
            return False

        retry_preserves_current = phase == "revert_written" and recovery == "drift"
        if retry_preserves_current:
            desired = copy.deepcopy(current)
        elif backup_exists:
            desired = _read_private_json_object(CLAUDE_BACKUP_PATH)
        else:
            desired = {}
        preserve_current_group = phase == "reverting" and recovery == "drift"
        if lease:
            transitioned = copy.deepcopy(current)
            _transition_private_picker(transitioned, {}, lease)
            _apply_picker_group(desired, _picker_group(transitioned))
        elif legacy_claim or preserve_current_group:
            _apply_picker_group(desired, _picker_group(current))

        desired_exists = (
            CLAUDE_SETTINGS_PATH.exists()
            if retry_preserves_current
            else backup_exists or bool(desired)
        )
        before = _picker_group(current)
        target = _picker_group(desired if desired_exists else {})
        _begin_picker_transition(
            "private",
            CLAUDE_SETTINGS_PATH,
            lease,
            {},
            before,
            target,
            phase="reverting",
            document_transition=(
                CLAUDE_SETTINGS_PATH.exists(),
                current,
                desired_exists,
                desired if desired_exists else {},
            ),
        )

        if desired_exists:
            write_json_file(CLAUDE_SETTINGS_PATH, desired)
            if _read_private_json_object(CLAUDE_SETTINGS_PATH) != desired:
                raise RuntimeError(
                    f"Could not verify restored Claude settings at {CLAUDE_SETTINGS_PATH}."
                )
        else:
            try:
                CLAUDE_SETTINGS_PATH.unlink(missing_ok=True)
            except OSError as exc:
                raise RuntimeError(
                    f"Failed to remove Claude settings at {CLAUDE_SETTINGS_PATH}"
                ) from exc
            if CLAUDE_SETTINGS_PATH.exists():
                raise RuntimeError(
                    f"Could not verify removal of Claude settings at {CLAUDE_SETTINGS_PATH}."
                )

        _mark_picker_revert_written("private", CLAUDE_SETTINGS_PATH)
        _remove_private_backup()
        _save_private_picker_management({})
        return True


def private_settings_are_globally_managed(state: dict | None = None) -> bool:
    """Whether global metadata records picker fields written into ucode's Claude settings."""
    with _picker_management_lock():
        settings = _read_private_json_object(CLAUDE_SETTINGS_PATH)
        lease, _recovery, _phase = _recover_picker_transition(
            "private", CLAUDE_SETTINGS_PATH, settings
        )
        return bool(lease) or _state_records_legacy_picker_ownership(state or {})


def clear_private_picker_management() -> None:
    """Forget private picker ownership after the corresponding settings file was reverted."""
    with _picker_management_lock():
        _save_private_picker_management({})


def _maybe_add_1m_suffix(model: str) -> str:
    if model.endswith("[1m]"):
        return model
    match = _CLAUDE_MODEL_RE.match(model)
    if not match:
        return model

    family, major_raw, minor_raw, _ = match.groups()
    major = int(major_raw)
    minor = int(minor_raw or 0)
    should_suffix = (family == "opus" and (major, minor) >= (4, 6)) or (
        family == "sonnet" and (major, minor) >= (4, 6)
    )
    return f"{model}[1m]" if should_suffix else model


def _enforce_model_default_hierarchy(
    family: str,
    *,
    coding_agent_config_defaults: dict[str, str],
    settings_file_existing_defaults: dict[str, str],
    ucode_defaults: dict[str, str],
) -> str | None:
    """Apply managed-file model precedence for one Claude family."""
    coding_agent_config_default_model = coding_agent_config_defaults.get(family)
    settings_file_existing_default_model = settings_file_existing_defaults.get(family)
    ucode_default_model = ucode_defaults.get(family)

    if coding_agent_config_default_model is not None:
        selected_default_model = coding_agent_config_default_model
    elif settings_file_existing_default_model is not None:
        return settings_file_existing_default_model
    else:
        selected_default_model = ucode_default_model

    if selected_default_model is None:
        return None
    if family in ("opus", "sonnet"):
        return _maybe_add_1m_suffix(selected_default_model)
    return selected_default_model


def add_claude_mcp_server(
    name: str,
    server: list[str] | dict,
    scope: str = MCP_USER_SCOPE,
    *,
    always_load: bool = False,
) -> None:
    # Three registration shapes share this helper. The plain proxy path passes an
    # argv list (`ug mcp-proxy ...`), registered via `claude mcp add ... -- <argv>`
    # where `--` fences the proxy's own flags off from claude's parser. The
    # web_search server passes a full stdio entry dict with its own env, which only
    # `add-json` can express — so a dict routes there. Finally, `always_load` (the
    # skills registry) needs `alwaysLoad: true`, which plain `mcp add` can't set, so
    # build a stdio entry dict and route it to add-json too.
    if isinstance(server, dict):
        cmd = ["claude", "mcp", "add-json", name, json.dumps(server), "-s", scope]
    elif always_load:
        entry = {
            "type": "stdio",
            "command": server[0],
            "args": list(server[1:]),
            "alwaysLoad": True,
        }
        cmd = ["claude", "mcp", "add-json", name, json.dumps(entry), "-s", scope]
    else:
        cmd = ["claude", "mcp", "add", name, "-s", scope, "--", *server]
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Failed to add MCP server '{name}' via claude CLI.") from exc


def add_claude_http_mcp_server(
    name: str,
    url: str,
    scope: str = MCP_USER_SCOPE,
    *,
    client_id: str = CLAUDE_CODE_OAUTH_CLIENT_ID,
    callback_port: int = MCP_OAUTH_CALLBACK_PORT,
) -> None:
    """Register a Databricks MCP endpoint as a **direct HTTP** server so Claude
    Code is the OAuth client and drives the RFC 8707 connection login itself.

    Unlike the stdio proxy (which injects a plain workspace token and hides the
    per-user connection state), a direct HTTP server lets Claude Code do MCP OAuth
    against ``/oidc`` with the ``resource`` indicator: on a missing/expired
    connection credential, ``/mcp`` shows "needs authentication" and Authenticate
    runs the login (``/oidc`` -> ``/mcp-service-login``). ``client_id`` is the
    published ``claude-code`` app (it has the loopback ``/callback`` redirect
    registered); the callback port is arbitrary because ``/oidc`` ignores the port
    for loopback redirects."""
    cmd = [
        "claude",
        "mcp",
        "add",
        "--transport",
        "http",
        "-s",
        scope,
        "--client-id",
        client_id,
        "--callback-port",
        str(callback_port),
        name,
        url,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Failed to add HTTP MCP server '{name}' via claude CLI.") from exc


def remove_claude_mcp_server(name: str, scope: str) -> bool:
    # Imported lazily: `_is_missing_mcp_server_output` is a shared CLI-output matcher
    # in ucode.mcp (used by the codex/gemini removers too), and ucode.mcp imports
    # this module at load time — a function-level import avoids that cycle.
    from ucode.mcp import _is_missing_mcp_server_output

    try:
        subprocess.run(
            ["claude", "mcp", "remove", name, "-s", scope],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return True
    except subprocess.CalledProcessError as exc:
        output = f"{exc.stderr or ''}\n{exc.stdout or ''}"
        if _is_missing_mcp_server_output(output):
            return False
        raise RuntimeError(f"Failed to remove MCP server '{name}' via claude CLI.") from exc


def _register_web_search_mcp(workspace: str, search_model: str, profile: str | None = None) -> bool:
    """Register (or replace) the web_search MCP server in Claude Code's user
    scope via `claude mcp add-json`. Removes any prior entry first so re-runs
    pick up changes to the workspace, model, or ucode binary path.

    Returns True if registration succeeded. Failures are non-blocking: we warn
    and return False so the rest of `ucode claude` setup can complete.
    """
    for scope in MCP_CLEANUP_SCOPES:
        try:
            remove_claude_mcp_server(WEB_SEARCH_MCP_NAME, scope)
        except RuntimeError:
            # Best-effort cleanup of stale entries — keep going.
            pass
    entry = _web_search_mcp_entry(workspace, search_model, profile)
    try:
        add_claude_mcp_server(WEB_SEARCH_MCP_NAME, entry)
    except RuntimeError as exc:
        print_warning(f"{exc} Web search will be unavailable; re-run `ucode claude` to retry.")
        return False
    return True


def _web_search_mcp_is_current(state: dict, entry: dict) -> bool:
    """Return whether the desired web-search entry is already registered.

    The persisted entry acts as a cheap fingerprint, while reading Claude's config repairs a
    registration removed or edited outside ucode. Avoiding the Claude CLI here matters: each
    ``claude mcp`` subprocess takes roughly 0.8 seconds during a launch.
    """
    if state.get(WEB_SEARCH_MCP_STATE_KEY) != entry:
        return False
    config = read_json_safe(CLAUDE_MCP_CONFIG_PATH)
    servers = config.get("mcpServers")
    return isinstance(servers, dict) and servers.get(WEB_SEARCH_MCP_NAME) == entry


def _claude_state_generation(state: dict) -> tuple:
    """Return persisted developer fields that identify one Claude settings generation."""
    managed_configs = state.get("managed_configs")
    claude_management = managed_configs.get("claude") if isinstance(managed_configs, dict) else None
    fingerprints = state.get("managed_file_fingerprints")
    claude_fingerprint = fingerprints.get("claude") if isinstance(fingerprints, dict) else None
    return (
        state.get("workspace"),
        state.get("profile"),
        state.get("web_search_model"),
        state.get("codex_models"),
        state.get("claude_static_models"),
        state.get("provider_services"),
        claude_management,
        claude_fingerprint,
    )


def _register_web_search_for_current_generation(state: dict, entry: dict) -> dict:
    """Register only if this generation is still current, then CAS its cache."""
    generation = state.get(WEB_SEARCH_MCP_GENERATION_KEY)
    if not isinstance(generation, str) or not generation:
        return load_state()
    persisted_generation = _claude_state_generation(developer_state_from_resolved(state))

    with _web_search_registration_lock():
        with _picker_management_lock():
            latest = load_state()
            if (
                latest.get(WEB_SEARCH_MCP_GENERATION_KEY) != generation
                or _claude_state_generation(latest) != persisted_generation
            ):
                return latest
            already_current = _web_search_mcp_is_current(latest, entry)

        model = _resolve_web_search_model(state)
        registration_success = bool(model) and (
            already_current
            or _register_web_search_mcp(state["workspace"], model, state.get("profile"))
        )
        with _picker_management_lock():
            latest = load_state()
            if (
                registration_success
                and latest.get(WEB_SEARCH_MCP_GENERATION_KEY) == generation
                and _claude_state_generation(latest) == persisted_generation
            ):
                latest[WEB_SEARCH_MCP_STATE_KEY] = entry
                save_state(latest)
            return latest


def _unregister_web_search_mcp() -> None:
    """Remove the web_search MCP server from all scopes. Used by revert."""
    for scope in MCP_CLEANUP_SCOPES:
        try:
            remove_claude_mcp_server(WEB_SEARCH_MCP_NAME, scope)
        except RuntimeError:
            pass


def disable_smart_routing(state: dict) -> bool:
    """Disable routing and remove only ucode's Claude Code routing hooks."""
    state.pop(SMART_ROUTING_STATE_KEY, None)
    if state.get("workspace"):
        save_state(state)
    changed = False
    if CLAUDE_SETTINGS_PATH.exists():
        doc = read_json_safe(CLAUDE_SETTINGS_PATH)
        if remove_smart_routing_hooks(doc):
            write_json_file(CLAUDE_SETTINGS_PATH, doc)
            changed = True
    from ucode.smart_routing.claude_routing import clear_routing_artifacts

    clear_routing_artifacts()
    return changed


def write_tool_config(
    state: dict,
    model: str | None,
    provider: str | None = None,
    provider_models: dict[str, str] | None = None,
    relayed: bool = False,
    route_root_model: str | None = None,
    custom_model: str | None = None,
    coding_agent_config_defaults: dict[str, str] | None = None,
    parent_schema: str | None = None,
) -> dict:
    web_search_model = _resolve_web_search_model(state)
    web_search_entry = (
        _web_search_mcp_entry(state["workspace"], web_search_model, state.get("profile"))
        if web_search_model
        else None
    )
    # Relayed inference points at a local refresh proxy; its loopback base URL is
    # recorded in state so launch starts the proxy on the matching port.
    relayed_base_url = relayed_proxy_base_url(state) if relayed else None
    overlay, managed_keys = render_overlay(
        state["workspace"],
        model,
        state.get("claude_models") or {},
        disable_web_search=web_search_model is not None,
        profile=state.get("profile"),
        use_pat=bool(state.get("use_pat")),
        custom_oauth=state.get("custom_oauth"),
        provider=provider,
        provider_models=provider_models,
        relayed=relayed,
        relayed_base_url=relayed_base_url,
        route_root_model=route_root_model,
        custom_model=custom_model,
        parent_schema=parent_schema,
        static_models=state.get("claude_static_models"),
        otel_tracing=bool(state.get("claude_otel_tracing")),
    )
    tracing_env_vars = tracing_env(state, "claude")
    stop_hook_command = claude_tracing_stop_hook_command() if tracing_env_vars else None
    if tracing_env_vars:
        overlay["env"]["MLFLOW_CLAUDE_TRACING_ENABLED"] = "true"
        overlay["env"].update(tracing_env_vars)
        managed_keys = managed_keys + [["env", key] for key in CLAUDE_TRACING_ENV_KEYS]
        if stop_hook_command:
            managed_keys = managed_keys + [["hooks", "Stop"]]
        else:
            print_warning(
                "MLflow tracing env was written, but the `mlflow` CLI could not be located "
                "to install the Claude Stop hook — traces won't be emitted. Re-run "
                "`ucode configure tracing`."
            )
    picker_paths = [[key] for key in CLAUDE_MANAGED_PICKER_KEYS]
    managed_file_keys = [path for path in managed_keys if path not in picker_paths]
    for path in (
        [["env", key] for key in CLAUDE_MANAGED_MODEL_ENV_KEYS]
        + [["env", key] for key in CLAUDE_CONDITIONAL_ENV_KEYS]
        + [["env", key] for key in CLAUDE_REMOVED_ENV_KEYS]
        + [["env", key] for key in CLAUDE_TRACING_ENV_KEYS]
        + [["env", key] for key in CLAUDE_OTEL_TRACE_ENV_KEYS]
        + [["otelHeadersHelper"]]
        + [["hooks", "Stop"]]
        + [["hooks", event] for event in ("PreToolUse", "SessionStart", "SubagentStart")]
    ):
        if path not in managed_file_keys:
            managed_file_keys.append(path)

    # V2 installs routing hooks in a transient per-launch settings file. Persistent settings must
    # contain no ucode routing hooks; surgically strip legacy ones while preserving user hooks.
    def _compose(base: dict, *, enforce_model_default_hierarchy: bool) -> dict:
        base_env = base.get("env")
        existing_custom_headers = (
            base_env.get(ANTHROPIC_CUSTOM_HEADERS_ENV_KEY) if isinstance(base_env, dict) else None
        )
        # Copy the overlay per file so merging into one base cannot affect the other.
        overlay_for_merge = copy.deepcopy(overlay)
        if enforce_model_default_hierarchy:
            settings_file_env = base_env if isinstance(base_env, dict) else {}
            target_env = overlay_for_merge["env"]
            configured_defaults = coding_agent_config_defaults or {}
            settings_file_existing_defaults = {
                family: model
                for family, key in CLAUDE_DEFAULT_MODEL_ENV_KEYS.items()
                if isinstance((model := settings_file_env.get(key)), str)
            }
            managed_overlay = state.get(MANAGED_OVERLAY_KEY, {})
            ucode_defaults = (
                managed_overlay.get("claude_models") or state.get("claude_models") or {}
            )

            for family, key in CLAUDE_DEFAULT_MODEL_ENV_KEYS.items():
                selected_default_model = _enforce_model_default_hierarchy(
                    family,
                    coding_agent_config_defaults=configured_defaults,
                    settings_file_existing_defaults=settings_file_existing_defaults,
                    ucode_defaults=ucode_defaults,
                )
                if selected_default_model is None:
                    target_env.pop(key, None)
                else:
                    target_env[key] = selected_default_model
        merged = deep_merge_dict(base, overlay_for_merge)
        overlay_custom_headers = overlay_for_merge["env"][ANTHROPIC_CUSTOM_HEADERS_ENV_KEY]
        merged["env"][ANTHROPIC_CUSTOM_HEADERS_ENV_KEY] = _merge_anthropic_custom_headers(
            existing_custom_headers, overlay_custom_headers
        )
        # Drop any apiKeyHelper a prior non-relayed launch left in the file; relayed
        # must not carry one (it would outrank the subscription OAuth).
        if relayed:
            merged.pop("apiKeyHelper", None)
        if tracing_env_vars and stop_hook_command:
            _upsert_tracing_stop_hook(merged, stop_hook_command)
        if not tracing_env_vars:
            env_block = merged.get("env")
            if isinstance(env_block, dict):
                for key in CLAUDE_TRACING_ENV_KEYS:
                    env_block.pop(key, None)
            # Strip only ucode's tracing Stop hook so user hooks stay intact.
            _remove_tracing_stop_hook(merged)
        # Prune ucode-managed model env keys we deliberately don't write this run
        # (e.g. ANTHROPIC_MODEL — see render_overlay).
        overlay_env = overlay_for_merge.get("env", {})
        merged_env = merged.get("env")
        if isinstance(merged_env, dict):
            for key in CLAUDE_MANAGED_MODEL_ENV_KEYS:
                if key not in overlay_env:
                    merged_env.pop(key, None)
            for key in CLAUDE_CONDITIONAL_ENV_KEYS:
                if key not in overlay_env:
                    merged_env.pop(key, None)
            for key in CLAUDE_OTEL_TRACE_ENV_KEYS:
                if key not in overlay_env:
                    merged_env.pop(key, None)
            # deep_merge_dict keeps keys already in the file, so drop the ones ucode no
            # longer writes.
            for key in CLAUDE_REMOVED_ENV_KEYS:
                merged_env.pop(key, None)
        if "otelHeadersHelper" not in overlay_for_merge:
            merged.pop("otelHeadersHelper", None)
        sync_smart_routing_hooks(merged, state, enabled=False)
        return merged

    with _picker_management_lock():
        _reconcile_private_settings(
            state,
            overlay,
            lambda base: _compose(base, enforce_model_default_hierarchy=False),
        )
        _reconcile_managed_settings(
            state,
            lambda base: _compose(
                base,
                enforce_model_default_hierarchy=provider is None and parent_schema is None,
            ),
            managed_file_keys,
            relayed,
            static_picker_active=all(key in overlay for key in CLAUDE_MANAGED_PICKER_KEYS),
        )
        if web_search_entry is None:
            state.pop(WEB_SEARCH_MCP_STATE_KEY, None)
            state.pop(WEB_SEARCH_MCP_GENERATION_KEY, None)
        else:
            # This opaque token is saved atomically with the settings files. Unlike resolved
            # managed values, it survives save_state's managed-overlay stripping and therefore
            # remains a stable CAS identity for the deferred MCP registration.
            state[WEB_SEARCH_MCP_GENERATION_KEY] = secrets.token_hex(16)
        # Persist the state generation under the same lock as both settings scopes. Registration
        # runs after releasing the lock and uses a generation check before recording its cache.
        if relayed:
            state["claude_relayed"] = True
        else:
            state.pop("claude_relayed", None)
            state.pop("relayed_proxy_port", None)
        state = mark_tool_managed(state, "claude", managed_keys)
        save_state(state)

    if web_search_entry is not None:
        state = _register_web_search_for_current_generation(state, web_search_entry)
    return state


def _merge_anthropic_custom_headers(existing: object, ucode_headers: str) -> str:
    """Preserve user headers while replacing the header names managed by ucode.

    Claude's ``ANTHROPIC_CUSTOM_HEADERS`` value is a newline-delimited string. To merge it, we:

    1. Split the existing custom headers by newline into individual header items.
    2. Split each item on ``:`` to identify its header name.
    3. Replace headers in ``CLAUDE_MANAGED_CUSTOM_HEADER_NAMES`` with ucode's values in their
       existing positions, while preserving all other existing headers.
    4. Append any ucode-managed headers that were not already present.

    Header names are compared case-insensitively. Non-header lines are also preserved to avoid
    silently discarding user configuration we do not understand.
    """

    if not isinstance(existing, str) or not existing:
        return ucode_headers

    ucode_lines_by_name: dict[str, str] = {}
    ucode_header_names: list[str] = []
    for line in ucode_headers.splitlines():
        name, separator, _value = line.partition(":")
        normalized_name = name.strip().casefold()
        if separator and normalized_name not in ucode_lines_by_name:
            ucode_header_names.append(normalized_name)
        if separator:
            ucode_lines_by_name[normalized_name] = line

    merged: list[str] = []
    replaced_names: set[str] = set()
    for line in existing.splitlines():
        name, separator, _value = line.partition(":")
        normalized_name = name.strip().casefold()
        if separator and normalized_name in CLAUDE_MANAGED_CUSTOM_HEADER_NAMES:
            replacement = ucode_lines_by_name.get(normalized_name)
            if replacement is not None and normalized_name not in replaced_names:
                merged.append(replacement)
                replaced_names.add(normalized_name)
            continue
        if line:
            merged.append(line)

    for name in ucode_header_names:
        if name not in replaced_names:
            merged.append(ucode_lines_by_name[name])
    return "\n".join(merged)


def _reconcile_managed_settings(
    state: dict,
    compose: Callable[[dict], dict],
    owned_paths: list[list[str]],
    relayed: bool,
    *,
    static_picker_active: bool,
) -> None:
    """Reconcile Claude Code's OS-managed settings so a bare ``claude`` uses the gateway.

    The managed file is root-owned and the highest-precedence scope, so every normal Claude
    configuration mirrors ucode's settings there. The same compose operation that produced the
    private file is applied to the existing managed file, preserving unrelated IT-authored keys.

    An existing IT-authored picker is retained by the merge. Picker fields that ucode recorded as
    managed are removed when the active model source switches away from a static model list.

    Relayed launches are skipped: they depend on a per-session loopback refresh proxy that only runs
    during `ucode claude`, so a bare `claude` could not reach the gateway anyway.
    """
    path = _managed_settings_path()
    if path is None:
        print_warning(
            "Machine-wide Claude settings aren't supported on this platform; skipped the managed "
            "settings."
        )
        return
    if path.is_symlink():
        raise RuntimeError(
            f"Refusing to use Claude Code managed settings through symlink {path}. Replace it "
            "with a regular file or contact your administrator."
        )
    if relayed:
        conflicts = _managed_relayed_conflicts(path)
        if conflicts:
            raise RuntimeError(
                "Claude subscription relay cannot start because enterprise managed settings "
                f"define {', '.join(conflicts)} at {path}. Ask your administrator to remove "
                "those entries or use standard Databricks authentication. If ucode previously "
                "created them, run `ucode revert` from an interactive terminal first."
            )
        mark_managed_file_verified(state, "claude", path, scope="relay-compatible")
        return

    with _picker_management_lock():
        current_text = read_managed_file(path)
        try:
            existing = _parse_managed_settings(current_text) if current_text is not None else {}
            managed_before = copy.deepcopy(existing)
            before = _picker_group(existing)
            previous, recovery, _phase = _recover_picker_transition("managed", path, existing)
            if not previous and recovery is None:
                previous = _legacy_managed_picker_management(path, existing)
            picker_overlay: dict = {}
            if static_picker_active:
                composed = compose(copy.deepcopy(existing))
                picker_overlay = {
                    key: copy.deepcopy(composed[key]) for key in CLAUDE_MANAGED_PICKER_KEYS
                }
            intended = _transition_private_picker(existing, picker_overlay, previous)
            desired_settings = compose(existing)
            target = _picker_group(desired_settings)
            if not previous and intended and before == target:
                intended = {}
        except (KeyError, RuntimeError) as exc:
            raise RuntimeError(
                f"Cannot safely update Claude Code managed settings at {path}: {exc}. "
                "ucode did not modify the file. Repair it or contact your administrator."
            ) from exc

        transition = bool(recovery is not None or previous or intended)
        conflict_paths = list(owned_paths)
        if transition or before != target:
            for picker_path in [[key] for key in CLAUDE_MANAGED_PICKER_KEYS]:
                if picker_path not in conflict_paths:
                    conflict_paths.append(picker_path)
        _preserve_permission_denies(managed_before, desired_settings)
        if not managed_writes_allowed():
            conflicts = managed_file_conflicts(managed_before, desired_settings, conflict_paths)
            if conflicts:
                raise RuntimeError(
                    "Claude Code configuration cannot be applied non-interactively because "
                    f"OS-managed settings at {path} override ucode values: {', '.join(conflicts)}. "
                    "Run `ucode configure --agent claude` from an interactive terminal or contact "
                    "your administrator."
                )
            mark_managed_file_verified(state, "claude", path, scope="local-compatible")
            return

        picker_paths = [[key] for key in CLAUDE_MANAGED_PICKER_KEYS]
        if transition:
            _begin_picker_transition("managed", path, previous, intended, before, target)
        try:
            reconcile_managed_file(
                path,
                _dump_managed_settings(desired_settings),
                tool="claude",
                display="Claude Code",
                owned_paths=owned_paths,
                conditional_owned_paths=picker_paths,
                repair_last_applied=bool(previous or recovery is not None),
            )
        except ManagedFileWriteUnavailable:
            conflicts = managed_file_conflicts(managed_before, desired_settings, conflict_paths)
            if conflicts:
                raise
            print_warning(
                f"Claude Code OS-managed settings could not be updated at {path}; continuing with "
                f"local settings at {CLAUDE_SETTINGS_PATH}."
            )
            mark_managed_file_verified(state, "claude", path, scope="local-compatible")
            return

        if is_dry_run():
            return
        written_text = read_managed_file(path)
        try:
            written = _parse_managed_settings(written_text) if written_text is not None else {}
        except RuntimeError as exc:
            raise RuntimeError(f"Could not verify Claude picker settings at {path}: {exc}") from exc
        if not _picker_group_matches(written, target):
            raise RuntimeError(f"Could not verify Claude picker settings at {path}.")
        if transition:
            _save_managed_picker_management(path, intended)
        mark_managed_file_verified(state, "claude", path)


def _preserve_permission_denies(existing: dict, desired: dict) -> None:
    existing_permissions = existing.get("permissions")
    desired_permissions = desired.get("permissions")
    if not isinstance(existing_permissions, dict) or not isinstance(desired_permissions, dict):
        return
    existing_denies = existing_permissions.get("deny")
    desired_denies = desired_permissions.get("deny")
    if not isinstance(existing_denies, list) or not isinstance(desired_denies, list):
        return
    desired_permissions["deny"] = [
        *existing_denies,
        *(rule for rule in desired_denies if rule not in existing_denies),
    ]


def _is_tracing_stop_hook(hook: object) -> bool:
    if not isinstance(hook, dict):
        return False
    hook = cast(dict, hook)
    if hook.get("type") != "command":
        return False
    command = hook.get("command")
    return isinstance(command, str) and command.endswith(CLAUDE_TRACING_STOP_HOOK_SUFFIX)


def _remove_tracing_stop_hook(settings: dict) -> None:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    stop_entries = hooks.get("Stop")
    if not isinstance(stop_entries, list):
        return

    cleaned_entries = []
    for entry in stop_entries:
        if not isinstance(entry, dict):
            cleaned_entries.append(entry)
            continue
        hook_list = entry.get("hooks")
        if not isinstance(hook_list, list):
            cleaned_entries.append(entry)
            continue
        cleaned_hooks = [hook for hook in hook_list if not _is_tracing_stop_hook(hook)]
        if cleaned_hooks:
            cleaned_entry = dict(entry)
            cleaned_entry["hooks"] = cleaned_hooks
            cleaned_entries.append(cleaned_entry)

    if cleaned_entries:
        hooks["Stop"] = cleaned_entries
    else:
        hooks.pop("Stop", None)
    if not hooks:
        settings.pop("hooks", None)


def _upsert_tracing_stop_hook(settings: dict, command: str) -> None:
    _remove_tracing_stop_hook(settings)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks
    stop_entries = hooks.get("Stop")
    if not isinstance(stop_entries, list):
        stop_entries = []
        hooks["Stop"] = stop_entries
    stop_entries.append({"hooks": [{"type": "command", "command": command}]})


def ensure_tracing_runtime() -> bool:
    """Ensure the MLflow tracing runtime is ready: a pinned `mlflow` CLI (3.11.x)
    installed via `uv tool`, whose absolute path the Stop hook will call.

    Best-effort — warns and returns False if it can't be set up, so
    `ucode configure tracing` can still finish for other agents."""
    return _ensure_mlflow_cli()


def _parse_mlflow_version(text: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\.(\d+)", text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _uv_tool_mlflow_path() -> str | None:
    """Absolute path to the `mlflow` installed by `uv tool`, or None.

    Resolved from `uv tool dir --bin` rather than ``shutil.which`` so a project
    venv's (possibly wrong-versioned) mlflow can't shadow the one ucode pins —
    the Stop hook must always run the uv-tool copy."""
    if not shutil.which("uv"):
        return None
    try:
        result = subprocess.run(
            ["uv", "tool", "dir", "--bin"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    bin_dir = (result.stdout or "").strip()
    if result.returncode != 0 or not bin_dir:
        return None
    candidate = Path(bin_dir) / "mlflow"
    return str(candidate) if candidate.exists() else None


def _installed_mlflow_version() -> tuple[int, int] | None:
    """The (major, minor) of the uv-tool `mlflow`, or None if absent."""
    path = _uv_tool_mlflow_path()
    if not path:
        return None
    try:
        result = subprocess.run(
            [path, "--version"], check=False, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_mlflow_version(result.stdout or result.stderr or "")


def claude_tracing_stop_hook_command() -> str | None:
    """The Stop hook command string: the absolute uv-tool `mlflow` invoking its
    `autolog claude stop-hook` handler. None when mlflow isn't installed.

    Using the absolute path means the hook needs neither `uv` nor a package
    index at run time (the minimal env Claude runs hooks in lacks UV_INDEX_URL),
    and can't be shadowed by another mlflow on PATH."""
    path = _uv_tool_mlflow_path()
    if not path:
        return None
    return f"{path} autolog claude stop-hook"


def _ensure_mlflow_cli() -> bool:
    """Ensure the pinned `mlflow` CLI (3.11.x) is installed via `uv tool`,
    installing or replacing an out-of-range version when needed."""
    current = _installed_mlflow_version()
    if current and MINIMUM_MLFLOW_VERSION <= current < MAXIMUM_MLFLOW_VERSION:
        return True

    if not shutil.which("uv"):
        verb = "replace" if current else "install"
        print_warning(
            f"Claude tracing needs the `mlflow` CLI ({MLFLOW_CLI_SPEC}), but `uv` is not "
            f'available to {verb} it. Run `uv tool install "{MLFLOW_CLI_SPEC}"`, then '
            "re-run `ucode configure tracing`."
        )
        return False

    print_note(f"{'Replacing' if current else 'Installing'} the mlflow CLI ({MLFLOW_CLI_SPEC})...")
    # Always --force: it installs fresh when absent and replaces in place when
    # present. Keying it on `current` broke when an mlflow existed but its
    # version couldn't be parsed — uv still errors "Executable already exists".
    cmd = ["uv", "tool", "install", "--force", MLFLOW_CLI_SPEC]
    try:
        subprocess.run(cmd, check=True, timeout=600)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print_warning(f"Could not install the mlflow CLI automatically: {exc}")
        return False

    if not _uv_tool_mlflow_path():
        print_warning(
            "Installed mlflow via `uv tool`, but its binary could not be located. "
            "Re-run `ucode configure tracing`."
        )
        return False
    print_success("mlflow CLI ready")
    return True


def default_model(state: dict) -> str | None:
    claude_models = state.get("claude_models") or {}
    return (
        claude_models.get("opus")
        or claude_models.get("sonnet")
        or claude_models.get("haiku")
        or next(iter(claude_models.values()), None)
    )


def _extract_caller_settings(tool_args: list[str]) -> tuple[list[str], list[str]]:
    """Split caller-supplied ``--settings`` values out of *tool_args*.

    Returns ``(values, remaining_args)``, handling both ``--settings <value>``
    and ``--settings=<value>`` spellings. Each value is either a JSON string or
    a path to a settings file — Claude Code accepts either.
    """
    values: list[str] = []
    remaining: list[str] = []
    i = 0
    while i < len(tool_args):
        arg = tool_args[i]
        if arg == "--settings" and i + 1 < len(tool_args):
            values.append(tool_args[i + 1])
            i += 2
            continue
        if arg.startswith("--settings="):
            values.append(arg[len("--settings=") :])
            i += 1
            continue
        remaining.append(arg)
        i += 1
    return values, remaining


def _load_caller_settings(value: str) -> dict:
    """Resolve a ``--settings`` value (inline JSON or file path) to a dict.

    Claude Code accepts either inline JSON or a path to a JSON file. Raises
    ``RuntimeError`` (surfaced by the CLI as an actionable error) when the value
    is neither, rather than silently dropping it: a dropped value would also be
    passed through as a second ``--settings`` flag, and Claude Code honors only
    one — so either the caller's settings or ucode's gateway config would be
    silently ignored. Failing loudly lets the caller fix their input.
    """
    text = value.strip()
    if text.startswith("{"):
        source, malformed = text, "value is not valid JSON"
    else:
        path = Path(text)
        if not path.exists():
            raise RuntimeError(
                f"--settings file not found: {value!r}. "
                "Pass inline JSON or a path to an existing JSON file."
            )
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"--settings file could not be read: {value!r} ({exc}).") from exc
        malformed = "file is not valid JSON"
    try:
        parsed = json.loads(source)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"--settings {malformed} ({exc}): {value!r}. Pass inline JSON or a path to a JSON file."
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"--settings must be a JSON object, got {type(parsed).__name__}: {value!r}."
        )
    return parsed


def _union_claude_hooks(base: dict, overlay: dict) -> dict:
    """Union two Claude Code ``hooks`` maps.

    ``hooks`` is ``{event: [entry, ...]}``. Per event we concatenate the entry
    lists so hooks from BOTH settings sources fire, rather than one replacing
    the other (which is what a plain deep-merge does to lists). This is what
    lets ucode's tracing Stop hook and a caller's own hooks coexist.
    """
    result: dict = {}
    for event in [*base, *(e for e in overlay if e not in base)]:
        entries: list = []
        for src in (base, overlay):
            val = src.get(event)
            if isinstance(val, list):
                entries.extend(val)
        result[event] = entries
    return result


def _merge_claude_settings(base: dict, overlay: dict) -> dict:
    """Deep-merge *overlay* onto *base* (overlay wins on conflicting leaves),
    but UNION the ``hooks`` so neither side's hooks are dropped. Inputs are not
    mutated.
    """
    merged = deep_merge_dict(copy.deepcopy(base), overlay)
    base_hooks = base.get("hooks")
    overlay_hooks = overlay.get("hooks")
    if isinstance(base_hooks, dict) or isinstance(overlay_hooks, dict):
        merged["hooks"] = _union_claude_hooks(
            base_hooks if isinstance(base_hooks, dict) else {},
            overlay_hooks if isinstance(overlay_hooks, dict) else {},
        )
    return merged


def _compose_v2_settings(tool_args: list[str]) -> tuple[dict, list[str]]:
    """Compose caller settings with ucode's Claude settings for a v2 launch."""
    caller_values, remaining = _extract_caller_settings(tool_args)
    settings: dict = {}
    for value in caller_values:
        settings = _merge_claude_settings(settings, _load_caller_settings(value))
    return _merge_claude_settings(settings, read_json_safe(CLAUDE_SETTINGS_PATH)), remaining


def _launch_model_args(tool_args: list[str], launch_model: str | None) -> list[str]:
    if not launch_model or has_explicit_model_arg(tool_args):
        return []
    return ["--model", launch_model]


def _build_claude_argv(
    binary: str,
    tool_args: list[str],
    relayed: bool = False,
    settings_override: dict | None = None,
) -> list[str]:
    """Build the ``claude`` argv, composing any caller ``--settings`` with
    ucode's managed settings.

    ucode needs its own settings (gateway ``apiKeyHelper`` + env) to reach
    Claude, and normally passes ``--settings <ucode-file>``. But Claude Code
    honors only ONE ``--settings`` flag, so a caller that ALSO passes
    ``--settings`` (e.g. an integration injecting hooks) would have exactly one
    of the two silently dropped. To let ucode compose with any prior command,
    we merge a caller-supplied ``--settings`` with ucode's — ucode's gateway
    keys win, hooks from both are unioned — and hand Claude a single merged
    ``--settings`` (inline JSON). The merge is per-launch and is never written
    back to the shared ucode settings file, so concurrent launches cannot
    accumulate one another's hooks. A caller ``--settings`` value ucode cannot
    resolve raises (see :func:`_load_caller_settings`) rather than being passed
    through as a second, colliding flag.

    ``relayed`` adds ``--setting-sources`` to exclude the user scope (see
    :data:`_RELAYED_SETTING_SOURCES`), so a stale user-scope apiKeyHelper cannot
    filter through and shadow the subscription OAuth.
    """
    source_args = ["--setting-sources", _RELAYED_SETTING_SOURCES] if relayed else []
    caller_values, remaining = _extract_caller_settings(tool_args)
    if not caller_values and settings_override is None:
        # No caller --settings: hand Claude ucode's settings file directly (the
        # common path; behavior unchanged).
        return [binary, *source_args, "--settings", str(CLAUDE_SETTINGS_PATH), *tool_args]
    caller_settings: dict = {}
    for value in caller_values:
        caller_settings = _merge_claude_settings(caller_settings, _load_caller_settings(value))
    # ucode wins over the caller for conflicting keys (protects gateway auth);
    # hooks from both sides survive.
    merged = _merge_claude_settings(caller_settings, read_json_safe(CLAUDE_SETTINGS_PATH))
    if settings_override is not None:
        merged = _merge_claude_settings(merged, settings_override)
    merged_env = merged.get("env")
    if isinstance(merged_env, dict):
        merged_env.pop("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", None)
    return [
        binary,
        *source_args,
        "--settings",
        json.dumps(merged, separators=(",", ":")),
        *remaining,
    ]


def _has_subscription_login() -> bool:
    """True when Claude Code already holds a subscription login (`claude auth
    status` exits 0). Never inspects or captures the credential itself."""
    try:
        result = subprocess.run(
            [SPEC["binary"], "auth", "status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _ensure_subscription_login() -> None:
    """Ensure Claude Code has a persisted subscription login, running the browser
    flow via `claude auth login` if not. ucode never sees or stores the token —
    Claude Code persists it to its own secure store and refreshes it natively."""
    # The OAuth token is the Authorization credential directly, so no interactive login
    # applies — return early so unattended runs can't hang on the browser fallback.
    is_headless_mode = os.environ.get(CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR)
    if is_headless_mode:
        return
    if _has_subscription_login():
        return
    print_note("Opening browser to sign in with your Claude subscription...")
    try:
        subprocess.run([SPEC["binary"], "auth", "login"], check=True, timeout=300)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("`claude auth login` failed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("`claude auth login` timed out.") from exc
    print_success("Claude subscription authenticated")


def _rewrite_relayed_port(state: dict, port: int) -> None:
    """Point the persisted config + state at ``port`` after the proxy had to bind
    a different port than the cached one. Keeps ANTHROPIC_BASE_URL (which Claude
    Code reads) in sync with the live proxy so requests reach it."""
    state["relayed_proxy_port"] = port
    save_state(state)
    settings = read_json_safe(CLAUDE_SETTINGS_PATH)
    env = settings.get("env")
    if isinstance(env, dict):
        env["ANTHROPIC_BASE_URL"] = f"http://{LOOPBACK_HOST}:{port}"
        write_json_file(CLAUDE_SETTINGS_PATH, settings)


def _launch_relayed(state: dict, binary: str, tool_args: list[str]) -> None:
    """Relayed launch: sign into the Claude subscription, start the loopback
    refresh proxy, then run Claude Code alongside it (the proxy must outlive the
    exec, so we spawn-and-wait rather than replacing the process)."""
    _ensure_subscription_login()
    workspace = state["workspace"]
    port = state.get("relayed_proxy_port")
    if not isinstance(port, int):
        raise RuntimeError("Relayed proxy port was not configured; re-run `ucode claude`.")

    server, cache, client = gateway_proxy.start_proxy(
        workspace,
        state.get("profile"),
        port,
        token_header=gateway_proxy.AI_GATEWAY_TOKEN_HEADER,
        force_refresh_near_expiry=False,
    )
    # start_proxy falls back to an OS-assigned port when the cached one is taken
    # (stale proxy from a killed session). Reconcile settings + state to whatever
    # it actually bound, so Claude Code connects to the live port.
    bound_port = server.server_address[1]
    if bound_port != port:
        _rewrite_relayed_port(state, bound_port)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    proc = subprocess.Popen(_build_claude_argv(binary, tool_args, relayed=True))
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


def launch(
    state: dict,
    tool_args: list[str],
    *,
    options: LaunchOptions,
) -> None:
    binary = SPEC["binary"]
    workspace = state.get("workspace")
    if workspace and os.environ.get(GATEWAY_MODEL_DISCOVERY_ENV_VAR) == "1":
        # Discovery is launch-scoped. Pass it in the process environment rather
        # than persisting it in Claude's private or OS-managed settings.
        os.environ["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
    if state.get("claude_relayed"):
        _launch_relayed(state, binary, tool_args)
        return
    # Smart routing needs Unix PTY support, which Windows does not provide.
    if options.launch_smart_routing and os.name == "nt":
        raise RuntimeError(
            "Smart routing in Claude Code is currently not supported on Windows. "
            "Please use Codex or disable smart routing."
        )
    if options.launch_smart_routing:
        smart_routing_v2.launch_claude(
            state,
            tool_args,
            binary=binary,
            user_settings_path=CLAUDE_USER_SETTINGS_PATH,
            # With no user pin, let Claude resolve its starting model from its own settings.
            launch_model=options.user_pinned_model,
            compose_settings=_compose_v2_settings,
            launch_model_args=_launch_model_args,
            model_name=_maybe_add_1m_suffix,
        )
        return
    if workspace:
        os.environ["OAUTH_TOKEN"] = get_databricks_token(workspace, state.get("profile"))
    settings_override = None
    launch_args = list(tool_args)
    if options.user_pinned_model:
        os.environ["ANTHROPIC_MODEL"] = options.user_pinned_model
        settings_override = {"env": {"ANTHROPIC_MODEL": options.user_pinned_model}}
        launch_args = [
            *_launch_model_args(tool_args, options.user_pinned_model),
            *tool_args,
        ]
    exec_or_spawn(_build_claude_argv(binary, launch_args, settings_override=settings_override))


def validate_cmd(binary: str) -> list[str]:
    return [
        binary,
        "--settings",
        str(CLAUDE_SETTINGS_PATH),
        "-p",
        "say hi in 5 words or less",
        "--max-turns",
        "1",
    ]
