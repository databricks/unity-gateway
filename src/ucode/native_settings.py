"""Pure local-file native validation for Claude 2.1.268 and Codex 0.154.0.

This intentionally supports a bounded subset of the pinned native schemas.
Validation and composition never execute helpers or write destination files.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import PurePosixPath, PureWindowsPath
from typing import NoReturn, cast

Validator = Callable[[object, str], None]
PINNED_AGENT_VERSIONS = {"claude": "2.1.268", "codex": "0.154.0"}
CLAUDE_HOOK_EVENTS = (
    "WorktreeCreate",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "SessionStart",
    "SubagentStart",
    "StopFailure",
)


def _invalid(path: str, message: str) -> NoReturn:
    raise RuntimeError(f"Invalid --config-file at {path}: {message}.")


def _string(value: object, path: str) -> None:
    if not isinstance(value, str):
        _invalid(path, "expected a string")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        _invalid(path, "expected Unicode scalar characters")


def _boolean(value: object, path: str) -> None:
    if type(value) is not bool:
        _invalid(path, "expected a boolean")


def _integer(value: object, path: str) -> None:
    if type(value) is not int or not -(2**63) <= value < 2**63:
        _invalid(path, "expected a signed 64-bit integer")


def _number(value: object, path: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(cast(int | float, value)):
        _invalid(path, "expected a finite number")


def _positive_number(value: object, path: str) -> None:
    _number(value, path)
    if cast(int | float, value) <= 0:
        _invalid(path, "expected a positive number")


def _refresh_interval(value: object, path: str) -> None:
    _number(value, path)
    if cast(int | float, value) < 1:
        _invalid(path, "expected a number at least one")


def _enum(*choices: str) -> Validator:
    def validate(value: object, path: str) -> None:
        if not isinstance(value, str) or value not in choices:
            _invalid(path, "unsupported enum value")

    return validate


def _array(item_validator: Validator) -> Validator:
    def validate(value: object, path: str) -> None:
        if not isinstance(value, list):
            _invalid(path, "expected an array")
        for index, item in enumerate(value):
            item_validator(item, f"{path}[{index}]")

    return validate


def _object(value: object, path: str) -> dict:
    if not isinstance(value, dict):
        _invalid(path, "expected an object")
    if any(not isinstance(key, str) for key in value):
        _invalid(path, "expected string field names")
    for key in value:
        _string(key, path)
    return value


def _schema(
    fields: Mapping[str, Validator], *, required: tuple[str, ...] = (), allow_extra: bool = False
) -> Validator:
    def validate(value: object, path: str) -> None:
        document = _object(value, path)
        for key in document:
            if key not in fields and not allow_extra:
                _invalid(f"{path}.{key}", "unsupported native field")
        for key in required:
            if key not in document:
                _invalid(f"{path}.{key}", "required field is missing")
        for key, item in document.items():
            if key in fields:
                fields[key](item, f"{path}.{key}")

    return validate


def _toml_value(value: object, path: str) -> None:
    """Reject values no supported native document can represent before composing."""
    if isinstance(value, dict):
        for key, child in _object(value, path).items():
            _toml_value(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _toml_value(child, f"{path}[{index}]")
    elif type(value) is int:
        _integer(value, path)
    elif type(value) is float:
        _number(value, path)
    elif isinstance(value, str):
        _string(value, path)
    elif type(value) is not bool:
        _invalid(path, "expected a supported non-null native value")


def _absolute_path(value: object, path: str) -> None:
    _string(value, path)
    asset = cast(str, value)
    if not (PurePosixPath(asset).is_absolute() or PureWindowsPath(asset).is_absolute()):
        _invalid(path, "expected an absolute asset path")


def _org_pin(value: object, path: str) -> None:
    if isinstance(value, str):
        if not value:
            _invalid(path, "expected a nonempty organization identifier")
        return
    _array(_string)(value, path)
    items = cast(list[str], value)
    if not items or any(not item for item in items):
        _invalid(path, "expected nonempty organization identifiers")


def _headers(value: object, path: str) -> None:
    for key, item in _object(value, path).items():
        _string(item, f"{path}.{key}")


_STRINGS = _array(_string)
_COMMAND_HOOK_FIELDS = {
    "type": _enum("command"),
    "command": _string,
    "args": _STRINGS,
    "timeout": _positive_number,
    "statusMessage": _string,
    "once": _boolean,
    "async": _boolean,
    "asyncRewake": _boolean,
}
_COMMAND_HOOK = _schema(_COMMAND_HOOK_FIELDS, required=("type", "command"))
_HOOK_GROUP = _schema({"matcher": _string, "hooks": _array(_COMMAND_HOOK)}, required=("hooks",))
_TLS = _schema(
    {
        "ca-certificate": _absolute_path,
        "client-certificate": _absolute_path,
        "client-private-key": _absolute_path,
    }
)
_HTTP_EXPORTER = _schema(
    {"endpoint": _string, "protocol": _enum("binary", "json"), "headers": _headers, "tls": _TLS},
    required=("endpoint", "protocol"),
)
_GRPC_EXPORTER = _schema(
    {"endpoint": _string, "headers": _headers, "tls": _TLS}, required=("endpoint",)
)


def _exporter(value: object, path: str) -> None:
    if isinstance(value, str):
        _enum("none", "statsig")(value, path)
        return
    document = _object(value, path)
    if len(document) != 1:
        _invalid(path, "expected exactly one native exporter variant")
    _schema({"otlp-http": _HTTP_EXPORTER, "otlp-grpc": _GRPC_EXPORTER})(document, path)


_SPINNER_FIELDS = {"mode": _enum("append", "replace"), "verbs": _STRINGS}
_STATUS_LINE_FIELDS = {
    "type": _enum("command"),
    "command": _string,
    "padding": _number,
    "refreshInterval": _refresh_interval,
    "hideVimModeIndicator": _boolean,
}
_CLAUDE_FIELDS = {
    "permissions": _schema(
        {
            "allow": _STRINGS,
            "ask": _STRINGS,
            "deny": _STRINGS,
            "additionalDirectories": _STRINGS,
            "disableBypassPermissionsMode": _enum("disable"),
        }
    ),
    "hooks": _schema(dict.fromkeys(CLAUDE_HOOK_EVENTS, _array(_HOOK_GROUP))),
    "sandbox": _schema(
        dict.fromkeys(
            (
                "enabled",
                "failIfUnavailable",
                "autoAllowBashIfSandboxed",
                "allowUnsandboxedCommands",
            ),
            _boolean,
        )
    ),
    "allowManagedPermissionRulesOnly": _boolean,
    "forceLoginOrgUUID": _org_pin,
    "disableWorkflows": _boolean,
    "workflowKeywordTriggerEnabled": _boolean,
    "autoCompactEnabled": _boolean,
    "channelsEnabled": _boolean,
    "allowedChannelPlugins": _array(
        _schema({"marketplace": _string, "plugin": _string}, required=("marketplace", "plugin"))
    ),
    "spinnerVerbs": _schema(_SPINNER_FIELDS, required=("mode", "verbs")),
    "attribution": _schema({"commit": _string, "pr": _string, "sessionUrl": _boolean}),
    "companyAnnouncements": _STRINGS,
    "otelHeadersHelper": _string,
    "statusLine": _schema(
        _STATUS_LINE_FIELDS,
        required=("type", "command"),
    ),
}
_CODEX_FIELDS = {
    "otel": _schema(
        {
            "environment": _string,
            "log_user_prompt": _boolean,
            "exporter": _exporter,
            "metrics_exporter": _exporter,
            "trace_exporter": _exporter,
        }
    ),
    "features": _schema({"hooks": _boolean, "fast_mode": _boolean}),
    "tui": _schema({"status_line": _STRINGS}),
    "model_auto_compact_token_limit": _integer,
    "model_auto_compact_token_limit_scope": _enum("total", "body_after_prefix"),
}
_RESERVED_ROOTS = {
    "claude": {
        "env",
        "apiKeyHelper",
        "model",
        "availableModels",
        "enforceAvailableModels",
        "modelPicker",
        "managedMcpServers",
        "mcpServers",
        "skills",
        "enabledPlugins",
        "extraKnownMarketplaces",
    },
    "codex": {
        "env",
        "model",
        "model_provider",
        "model_providers",
        "model_catalog_json",
        "profile",
        "profiles",
        "auth",
        "mcp_servers",
        "skills",
        "shell_environment_policy",
        "codex_home",
    },
}
_CLAUDE_REQUIRED_POLICY = frozenset(
    {
        "permissions",
        "hooks",
        "sandbox",
        "allowManagedPermissionRulesOnly",
        "forceLoginOrgUUID",
        "channelsEnabled",
        "allowedChannelPlugins",
        "disableWorkflows",
        "workflowKeywordTriggerEnabled",
    }
)


def _supported_tool(tool: str, path: str) -> None:
    if tool not in PINNED_AGENT_VERSIONS:
        _invalid(path, "native settings support only claude and codex")


def tracing_conflict_paths(tool: str, settings: dict) -> tuple[str, ...]:
    """Native fields overridden by UG's explicitly enabled trace exporter."""
    _supported_tool(tool, "native_settings")
    if tool == "claude":
        return ("otelHeadersHelper",) if "otelHeadersHelper" in settings else ()
    return ("otel.trace_exporter",) if "trace_exporter" in settings.get("otel", {}) else ()


def validate_native_settings(value: object, path: str, tool: str, raw_config: dict) -> dict:
    """Validate one file extension without normalizing or mutating caller values."""
    _supported_tool(tool, path)
    document = _object(value, path)
    _toml_value(document, path)
    for key in document:
        if key in _RESERVED_ROOTS[tool]:
            _invalid(f"{path}.{key}", "reserved field; use the dedicated configuration input")
    _schema(_CLAUDE_FIELDS if tool == "claude" else _CODEX_FIELDS)(document, path)
    if "tracing" in raw_config:
        tracing_path = f"{path.rsplit('.', 1)[0]}.tracing"
        tracing = raw_config["tracing"]
        _schema({"enabled": _boolean})(tracing, tracing_path)
        if tracing.get("enabled"):
            for conflict in tracing_conflict_paths(tool, document):
                _invalid(f"{path}.{conflict}", "conflicts with explicitly enabled UG tracing")
    if tool == "codex":
        _check_feature_pin(document, raw_config.get("native_requirements"), path)
    return deepcopy(document)


def _disabled_feature(value: object, path: str) -> None:
    if value is not False:
        _invalid(path, "only an explicit false feature requirement is supported")


def _check_feature_pin(settings: dict, requirements: object, path: str) -> None:
    features = settings.get("features")
    pins = requirements.get("features") if isinstance(requirements, dict) else None
    if (
        isinstance(features, dict)
        and features.get("fast_mode") is True
        and isinstance(pins, dict)
        and pins.get("fast_mode") is False
    ):
        _invalid(f"{path}.features.fast_mode", "conflicts with the native feature requirement")


def validate_native_requirements(value: object, path: str, tool: str, raw_config: dict) -> dict:
    """The initial requirements surface supports only Codex's fast_mode pin."""
    if tool != "codex":
        _invalid(path, "native requirements support only codex")
    document = _object(value, path)
    _toml_value(document, path)
    _schema({"features": _schema({"fast_mode": _disabled_feature})})(document, path)
    settings = raw_config.get("native_settings")
    if isinstance(settings, dict):
        _check_feature_pin(settings, document, path)
    return deepcopy(document)


def required_os_scopes(
    tool: str, settings: dict, requirements: dict | None = None
) -> frozenset[str]:
    """Classify validated declarations that cannot use private-file fallback.

    Managed Codex features are authoritative startup settings; only the separate
    requirements scope establishes a runtime feature pin.
    """
    _supported_tool(tool, "native_settings")
    scopes = set()
    if tool == "claude" and any(
        key in settings and settings[key] != {} for key in _CLAUDE_REQUIRED_POLICY
    ):
        scopes.add("managed_settings")
    if tool == "codex":
        if settings.get("features"):
            scopes.add("managed_settings")
        if (requirements or {}).get("features"):
            scopes.add("requirements")
    return frozenset(scopes)


def preflight_native_launch(
    tool: str,
    settings: dict,
    requirements: dict | None = None,
    *,
    os_managed_supported: bool,
    relayed: bool = False,
    relayed_managed_supported: bool = False,
    first_party_oauth: bool = False,
    resolved_tracing_enabled: bool = False,
    legacy_codex: bool = False,
) -> frozenset[str]:
    """Check resolved launch choices before planning or writing destinations."""
    scopes = required_os_scopes(tool, settings, requirements)
    if tool == "codex" and legacy_codex and (settings or requirements):
        _invalid(
            "native_settings",
            "native extensions require the modern Codex configuration layout; upgrade Codex",
        )
    if scopes and not os_managed_supported:
        field = "native_requirements" if "requirements" in scopes else "native_settings"
        _invalid(field, "required OS-managed policy is unavailable on this platform")
    if tool == "claude":
        if "forceLoginOrgUUID" in settings and not first_party_oauth:
            _invalid(
                "native_settings.forceLoginOrgUUID", "requires first-party OAuth authentication"
            )
        if relayed and scopes and not relayed_managed_supported:
            _invalid(
                "native_settings", "required managed policy is unsupported for this relayed launch"
            )
    if resolved_tracing_enabled:
        for conflict in tracing_conflict_paths(tool, settings):
            _invalid(f"native_settings.{conflict}", "conflicts with enabled UG tracing")
    if tool == "codex":
        _check_feature_pin(settings, requirements, "native_settings")
    return scopes


def agent_native_settings(state: dict, tool: str) -> dict:
    return validate_native_settings(
        state.get(f"{tool}_native_settings", {}), f"{tool}.native_settings", tool, {}
    )


def validate_codex_routing_hooks(native: dict) -> None:
    """A selected V2 routing launch requires Codex's native hook engine."""
    if native.get("features", {}).get("hooks") is False:
        _invalid(
            "native_settings.features.hooks",
            "conflicts with selected Codex smart routing; enable hooks or disable routing",
        )


def agent_native_requirements(state: dict, tool: str) -> dict:
    if tool != "codex":
        return {}
    return validate_native_requirements(
        state.get("codex_native_requirements", {}), "codex.native_requirements", tool, {}
    )


def native_ownership(
    tool: str, native: dict
) -> tuple[list[list[str]], dict[tuple[str, ...], list], frozenset[tuple[str, ...]]]:
    """Ownership comes from declarations, never from composed live values."""
    from ucode.managed_files import _path_value
    from ucode.managed_ownership import leaf_paths

    paths = leaf_paths(native)
    contributions = {
        tuple(path): deepcopy(value)
        for path in paths
        if isinstance((value := _path_value(native, path)), list)
    }
    exact = {
        path
        for path in contributions
        if not (tool == "claude" and path[0] in {"permissions", "hooks"})
    }
    if tool == "claude" and "forceLoginOrgUUID" in native:
        exact.add(("forceLoginOrgUUID",))
    return paths, contributions, frozenset(exact)


def _plain(value: object) -> object:
    unwrap = getattr(value, "unwrap", None)
    return unwrap() if callable(unwrap) else value


def _composition_error(target: str, path: tuple[str, ...]) -> NoReturn:
    raise RuntimeError(
        f"Incompatible unowned native settings at {target}.{'.'.join(path)}; "
        "reconcile the existing fields or explicitly retire them before retrying."
    )


def validate_native_base(tool: str, base: dict, native: dict, *, target: str) -> None:
    """Reject ancestor/variant takeovers before generated composition can hide them."""

    def check(existing: dict, supplied: dict, prefix: tuple[str, ...]) -> None:
        for key, value in supplied.items():
            if value == {} or key not in existing:
                continue
            path = (*prefix, key)
            before = _plain(existing[key])
            if isinstance(value, dict):
                if not isinstance(before, dict):
                    _composition_error(target, path)
                check(before, value, path)
            elif isinstance(before, dict):
                _composition_error(target, path)
            elif isinstance(value, list) != isinstance(before, list):
                # Exact-array ownership checks also protect list-to-string org pins.
                if not (
                    tool == "claude" and path == ("forceLoginOrgUUID",) and isinstance(before, list)
                ):
                    _composition_error(target, path)

    check(base, native, ())
    if tool == "claude" and "statusLine" in native:
        status = base.get("statusLine")
        if isinstance(status, dict) and status.get("type", "command") != "command":
            _composition_error(target, ("statusLine", "type"))


def _validate_effective_native(tool: str, desired: dict, native: dict, target: str) -> None:
    if tool == "codex":
        for key in ("exporter", "metrics_exporter", "trace_exporter"):
            if key in native.get("otel", {}):
                _exporter(_plain(desired["otel"][key]), f"{target}.otel.{key}")
        return
    for key, fields, required in (
        ("spinnerVerbs", _SPINNER_FIELDS, ("mode", "verbs")),
        ("statusLine", _STATUS_LINE_FIELDS, ("type", "command")),
    ):
        if key in native:
            _schema(fields, required=required, allow_extra=True)(
                _plain(desired[key]), f"{target}.{key}"
            )
    for event in native.get("hooks", {}):
        groups = desired["hooks"][event]
        for group in groups:
            location = f"{target}.hooks.{event}"
            group = _object(_plain(group), location)
            if "matcher" in group:
                _string(group["matcher"], f"{location}.matcher")
            if not isinstance(group.get("hooks"), list):
                _invalid(location, "existing hook group requires a hooks array")
            for handler in group["hooks"]:
                handler = _object(_plain(handler), location)
                _string(handler.get("type"), f"{location}.type")
                if handler["type"] == "command":
                    _schema(_COMMAND_HOOK_FIELDS, required=("type", "command"), allow_extra=True)(
                        handler, location
                    )


def compose_native_settings(tool: str, desired: dict, native: dict, *, target: str) -> dict:
    """Compose only declared leaves, preserving sibling policy and additive arrays."""
    from ucode.managed_ownership import _add_elements, _remove_elements

    validate_native_base(tool, desired, native, target=target)
    result = deepcopy(desired)
    _, _, exact = native_ownership(tool, native)

    def merge(document: dict, supplied: dict, prefix: tuple[str, ...]) -> None:
        for key, value in supplied.items():
            path = (*prefix, key)
            if isinstance(value, dict):
                if value:
                    merge(document.setdefault(key, {}), value, path)
            elif isinstance(value, list) and path not in exact:
                existing = document.get(key, [])
                document[key] = _add_elements(existing, _remove_elements(value, existing))
            else:
                document[key] = deepcopy(value)

    merge(result, native, ())
    _validate_effective_native(tool, result, native, target)
    return result
