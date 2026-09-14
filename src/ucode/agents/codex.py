"""Codex agent: writes ~/.codex/ucode.config.toml for Databricks-backed Codex."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

import tomlkit
from tomlkit.exceptions import ParseError

from ucode.codex_config import (
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
    prune_key_paths,
    read_toml_safe,
    write_json_file,
    write_toml_file,
)
from ucode.constants import (
    MODEL_PROVIDER_SERVICE_HEADER,
    MODEL_SERVICE_PARENT_SCHEMA_HEADER,
)
from ucode.custom_oauth import (
    CustomOAuthConfig,
    build_custom_auth_token_argv,
    get_custom_client_token,
)
from ucode.databricks import (
    build_auth_token_argv,
    build_tool_base_url,
    fetch_codex_mps_model_catalog,
    get_databricks_token,
)
from ucode.launcher import exec_or_spawn
from ucode.managed_files import (
    ManagedFileWriteUnavailable,
    managed_file_conflicts,
    managed_file_is_verified,
    managed_file_status,
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
from ucode.state import get_provider_service, mark_tool_managed, save_state
from ucode.telemetry import agent_version, ucode_version
from ucode.ui import print_warning_err

from .args import LaunchOptions

CODEX_CONFIG_DIR = Path.home() / ".codex"
CODEX_PROFILE_NAME = "ucode"
CODEX_CONFIG_PATH = CODEX_CONFIG_DIR / f"{CODEX_PROFILE_NAME}.config.toml"
CODEX_BACKUP_PATH = APP_DIR / "codex-ucode-config.backup.toml"
CODEX_MPS_MODEL_CATALOG_PATH = APP_DIR / "codex-mps-model-catalog.json"
LEGACY_CODEX_CONFIG_PATH = CODEX_CONFIG_DIR / "config.toml"
LEGACY_CODEX_BACKUP_PATH = APP_DIR / "codex-config.backup.toml"
CODEX_MODEL_PROVIDER_NAME = "ucode-databricks"
CUSTOM_HEADER_ENV_PREFIX = "UCODE_CUSTOM_HEADER"
_MODEL_SERVICE_ROUTING_KEY_PATHS = [
    ["model_providers", CODEX_MODEL_PROVIDER_NAME, "http_headers", MODEL_PROVIDER_SERVICE_HEADER],
    [
        "model_providers",
        CODEX_MODEL_PROVIDER_NAME,
        "http_headers",
        MODEL_SERVICE_PARENT_SCHEMA_HEADER,
    ],
]
MINIMUM_CODEX_VERSION = (0, 134, 0)
MINIMUM_CODEX_VERSION_TEXT = "0.134.0"
MINIMUM_ROUTING_CODEX_VERSION = (0, 145, 0)
MINIMUM_ROUTING_CODEX_VERSION_TEXT = "0.145.0"
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
    """Return the active smart-routing version blocker, if any."""
    if not smart_routing_v2.enabled():
        return None
    version = agent_version(SPEC["binary"])
    parsed = _parse_version(version)
    if parsed is None or parsed >= MINIMUM_ROUTING_CODEX_VERSION:
        return None
    return (
        "Codex smart routing requires Codex "
        f"{MINIMUM_ROUTING_CODEX_VERSION_TEXT} or newer; found {version}."
    )


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
    return parsed < MINIMUM_CODEX_VERSION


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


def _provider_block(
    workspace: str,
    databricks_profile: str | None,
    use_pat: bool = False,
    provider: str | None = None,
    parent_schema: str | None = None,
    custom_oauth: CustomOAuthConfig | None = None,
) -> dict:
    if custom_oauth:
        auth_argv = build_custom_auth_token_argv(workspace, custom_oauth)
    else:
        auth_argv = build_auth_token_argv(workspace, databricks_profile, use_pat=use_pat)
    base_url = build_tool_base_url("codex", workspace)
    http_headers = {
        "User-Agent": f"ucode/{ucode_version()} codex/{agent_version('codex')}",
    }
    if provider:
        http_headers[MODEL_PROVIDER_SERVICE_HEADER] = provider
    elif parent_schema:
        http_headers[MODEL_SERVICE_PARENT_SCHEMA_HEADER] = parent_schema
    return {
        "name": "Databricks AI Gateway",
        "base_url": base_url,
        "wire_api": "responses",
        "http_headers": http_headers,
        # Run the `ucode auth-token` executable directly (not via `sh -c`) so the
        # helper works on Windows, where there is no POSIX shell (issue #116).
        "auth": {
            "command": auth_argv[0],
            "args": auth_argv[1:],
            "timeout_ms": 5000,
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
) -> dict:
    """Overlay for Codex CLI < 0.134.0, which only reads `~/.codex/config.toml`.

    The shared file uses `profile = "ucode"` to select `[profiles.ucode]`, which
    points at the shared `[model_providers.ucode-databricks]` block.
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
        or (isinstance(providers, dict) and CODEX_MODEL_PROVIDER_NAME in providers)
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
    if isinstance(providers, dict) and CODEX_MODEL_PROVIDER_NAME in providers:
        providers.pop(CODEX_MODEL_PROVIDER_NAME, None)
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

    Returns True if anything was removed.
    """
    return _strip_legacy_ucode_entries(_legacy_config_path())


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

    if _use_legacy_layout():
        # Codex < 0.134.0 only reads ~/.codex/config.toml. Write the shared
        # config with [profiles.ucode] + shared [model_providers.ucode-databricks]
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
        )
        doc = read_toml_safe(LEGACY_CODEX_CONFIG_PATH)
        prune_key_paths(doc, _MODEL_SERVICE_ROUTING_KEY_PATHS)
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

    _remove_legacy_ucode_profile()
    backup_existing_file(CODEX_CONFIG_PATH, CODEX_BACKUP_PATH)
    overlay = render_overlay(
        workspace,
        chosen_model,
        databricks_profile,
        use_pat=bool(state.get("use_pat")),
        provider=provider,
        parent_schema=parent_schema,
        custom_oauth=state.get("custom_oauth"),
    )

    def compose(base: dict) -> dict:
        prune_key_paths(base, _MODEL_SERVICE_ROUTING_KEY_PATHS)
        deep_merge_dict(base, copy.deepcopy(overlay))
        # deep_merge can't drop keys, so clear model preferences from an earlier run.
        if chosen_model is None and not smart_routing_v2.enabled():
            for key in ("model", "model_reasoning_effort"):
                base.pop(key, None)
        _set_provider_header(base, None)
        return base

    doc = read_toml_safe(CODEX_CONFIG_PATH)
    compose(doc)
    sync_smart_routing_hooks(
        doc,
        state,
        enabled=False,
    )
    write_toml_file(CODEX_CONFIG_PATH, doc)
    _reconcile_managed_config(state, compose)
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


def _managed_header_names() -> set[str]:
    """Return header names fixed by OS-managed config."""
    path = codex_managed_config_path()
    if path is None:
        return set()
    text = read_managed_file(path)
    if text is None:
        return set()
    try:
        doc = _parse_managed_config(text)
    except RuntimeError:
        # Configuration already validates the managed file before launch and reports
        # the full parse error there.
        return set()
    providers = doc.get("model_providers")
    provider = providers.get(CODEX_MODEL_PROVIDER_NAME) if isinstance(providers, dict) else None
    if not isinstance(provider, dict):
        return set()
    names: set[str] = set()
    for table_name in ("http_headers", "env_http_headers"):
        headers = provider.get(table_name)
        if isinstance(headers, dict):
            names.update(str(name).casefold() for name in headers)
    return names


def _with_custom_headers(doc: dict, custom_headers: dict[str, str]) -> dict:
    """Return a launch-only Codex config that reads custom header values from the environment."""
    if not custom_headers:
        return doc
    blocked_names = _managed_header_names() & {name.casefold() for name in custom_headers}
    if blocked_names:
        names = ", ".join(sorted(blocked_names))
        raise RuntimeError(f"--header cannot override OS-managed Codex header(s): {names}.")

    launch_doc = copy.deepcopy(doc)
    providers = launch_doc.get("model_providers")
    provider = providers.get(CODEX_MODEL_PROVIDER_NAME) if isinstance(providers, dict) else None
    if not isinstance(provider, dict):
        raise RuntimeError("Codex's ucode model provider configuration is missing or invalid.")
    env_headers = provider.setdefault("env_http_headers", {})
    if not isinstance(env_headers, dict):
        raise RuntimeError("Codex's ucode environment-header configuration is invalid.")

    requested_names = {name.casefold() for name in custom_headers}
    for existing_name in list(env_headers):
        if str(existing_name).casefold() in requested_names:
            env_headers.pop(existing_name, None)
    for index, (name, value) in enumerate(custom_headers.items()):
        env_name = f"{CUSTOM_HEADER_ENV_PREFIX}_{os.getpid()}_{index}"
        os.environ[env_name] = value
        env_headers[name] = env_name
    return launch_doc


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


def default_model(state: dict) -> str | None:
    """Return a managed Codex model, or leave selection to Codex."""
    if isinstance(state.get("codex_default_model"), str):
        return state["codex_default_model"]
    if smart_routing_v2.enabled():
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
    if smart_routing_v2.enabled():
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
        backup_existing_file(CODEX_CONFIG_PATH, CODEX_BACKUP_PATH)
        write_toml_file(CODEX_CONFIG_PATH, doc)
    return changed


def _set_provider_header(config: dict, provider: str | None) -> None:
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
    if provider:
        headers[MODEL_PROVIDER_SERVICE_HEADER] = provider
    else:
        headers.pop(MODEL_PROVIDER_SERVICE_HEADER, None)


def _model_catalog_path(workspace: str, provider: str) -> Path:
    key = f"{workspace.rstrip('/')}\0{provider}".encode()
    digest = hashlib.sha256(key).hexdigest()[:16]
    base = CODEX_MPS_MODEL_CATALOG_PATH
    return base.with_name(f"{base.stem}-{digest}{base.suffix}")


def _write_model_catalog(path: Path, catalog: dict) -> None:
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


def _launch_token(state: dict, workspace: str) -> str:
    custom_oauth = state.get("custom_oauth")
    if isinstance(custom_oauth, dict):
        return get_custom_client_token(
            workspace,
            custom_oauth["client_id"],
            custom_oauth["redirect_url"],
            scopes=custom_oauth["scopes"],
        )
    return get_databricks_token(workspace, state.get("profile"))


def _reject_managed_mps_catalog() -> None:
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
            f"Codex managed settings at {path} define model_catalog_json, which overrides MPS "
            "discovery. Remove it or contact your administrator."
        )


def launch(
    state: dict,
    tool_args: list[str],
    *,
    options: LaunchOptions,
) -> None:
    custom_headers = dict(options.custom_headers)
    if options.launch_smart_routing:
        _launch_smart_routing(state, tool_args, custom_headers=custom_headers)
        return
    clear_model_preferences(state)
    binary = SPEC["binary"]
    workspace = state.get("workspace")
    launch_provider = state.get("_codex_launch_provider")
    provider = (
        launch_provider.strip()
        if isinstance(launch_provider, str) and launch_provider.strip()
        else get_provider_service(state, "codex")
    )
    if workspace and provider:
        _reject_managed_mps_catalog()
    token = None
    if workspace:
        token = _launch_token(state, workspace)
        os.environ["OAUTH_TOKEN"] = token
    if _use_legacy_layout():
        if custom_headers:
            raise RuntimeError(f"--header requires Codex {MINIMUM_CODEX_VERSION_TEXT} or newer.")
        print_warning_err(
            f"Codex {agent_version(binary)} is outdated. Upgrade Codex to "
            f"{MINIMUM_CODEX_VERSION_TEXT} or newer, then run `codex --version` to verify "
            "the active installation."
        )
        exec_or_spawn([binary, "--profile", CODEX_PROFILE_NAME, *tool_args])
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
    if workspace and token and provider:
        catalog = fetch_codex_mps_model_catalog(workspace, token, provider)
        catalog_path = _model_catalog_path(workspace, provider)
        _write_model_catalog(catalog_path, catalog)
        profile_doc["model_catalog_json"] = str(catalog_path)
    profile_doc = _with_custom_headers(profile_doc, custom_headers)
    exec_or_spawn([binary, *codex_config_args(profile_doc), *tool_args])


def _launch_smart_routing(
    state: dict, tool_args: list[str], *, custom_headers: dict[str, str] | None = None
) -> None:
    """Launch the Codex TUI through the smart-routing interposer."""
    binary = SPEC["binary"]
    version_text = agent_version(binary)
    parsed_version = _parse_version(version_text)
    if parsed_version is not None and parsed_version < MINIMUM_ROUTING_CODEX_VERSION:
        raise RuntimeError(
            "Codex smart routing requires Codex "
            f"{MINIMUM_ROUTING_CODEX_VERSION_TEXT} or newer; found {version_text}."
        )

    configured_model = _smart_routing_config_model(state)
    # Prefer the custom catalog if it exists.
    models = custom_catalog_models() or routing_models(state)
    start_model = (
        configured_model
        or (codex_model_id(models[0]) if models else None)
        or APP_SERVER_SMART_ROUTING_STARTING_MODEL
    )
    if custom_headers:

        def routed_render_overlay(*args, **kwargs) -> dict:
            return _with_custom_headers(render_overlay(*args, **kwargs), custom_headers)

    else:
        routed_render_overlay = render_overlay

    smart_routing_v2.launch_codex(
        state,
        tool_args,
        binary=binary,
        start_model=start_model,
        render_overlay=routed_render_overlay,
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
