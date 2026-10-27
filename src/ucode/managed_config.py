"""Managed coding-agent config: fetch, normalize, and local persistence (launch/refresh side).

An org admin defines a ``CodingAgentConfig`` on the Databricks AI Gateway; developers read it
(non-admin) and ``ucode`` applies it locally. This module owns the fetch/normalize side and the one
machine-wide file, ``/etc/unitygateway/managed-settings.json`` (root-owned, 0644):

- fetching the raw manifest (via :func:`ucode.databricks.fetch_managed_coding_agent_configs`),
- normalizing the proto-JSON into a stable internal dict keyed by ucode's own tool names,
- persisting it via :func:`save_managed_state` / :func:`load_managed_state` — ``ug configure`` pulls
  the published copy into this file only when its workspace or configuration changes, and
- replaying it on each launch without a network read, falling back to the persisted copy when a
  read fails. A launch never refreshes it: an admin's later edit reaches a machine when the
  developer re-runs ``ug configure``. Only when nothing is saved for the workspace yet does a launch
  read the control plane once.

The last read's outcome (published / none / feature_disabled) lives separately in
``~/.ucode/refresh_ts.json`` (0600), keyed by workspace, so a launch can tell "no managed config"
from "feature disabled" without a request. Unchanged remote reads never rewrite the protected
policy. Non-root launches never invoke sudo.

:func:`refresh_managed_config` is the launch path's entry point. It is called before model discovery,
because the manifest decides whether that discovery is needed at all; the launch path then hands the
manifest to :func:`ucode.managed_resolve.resolve_state` once the state it layers over is final.
Deciding *which* value wins for a given key is :mod:`ucode.managed_resolve`'s job, kept separate so
that logic stays pure and I/O-free.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import NamedTuple, cast

import ucode.config_io as config_io
from ucode.databricks import (
    fetch_managed_coding_agent_configs,
    fetch_model_recommendation,
    get_databricks_token,
)
from ucode.managed_files import (
    OS,
    SKIP_OS_SETTINGS_ENV,
    UNITY_GATEWAY_MANAGED_SETTINGS_PATH,
    current_os,
    managed_file_fingerprint,
    managed_settings_skipped,
    update_workspace_policy,
)
from ucode.os_compatibility.system_cross_os import is_admin
from ucode.time_utils import parse_update_time
from ucode.ui import console, normalize_workspace_url, print_warning

MANAGED_CONFIG_PATH = UNITY_GATEWAY_MANAGED_SETTINGS_PATH
# Per-user policy cache for platforms or modes without machine installation (Windows,
# UG_SKIP_OS_SETTINGS, `--user` scope).
USER_MANAGED_CONFIG_PATH = config_io.APP_DIR / "managed-config.json"
REFRESH_TS_PATH = config_io.APP_DIR / "refresh_ts.json"

# Shown to a developer when their workspace has no admin-defined managed config yet — the normal
# case, not an error. Kept here so the CLI (which surfaces it) uses one consistent message.
NO_MANAGED_CONFIG_MESSAGE = "No coding-agent config has been set up by your workspace admin yet."

# CodingAgent proto enum -> ucode tool name. Anything unrecognized (e.g. a newer agent this ucode
# build doesn't know) is dropped during normalization rather than guessed at. Public because the
# admin-write side (``managed_setup``) inverts these maps to serialize, so a new agent or MCP type
# only has to be declared once.
AGENT_ENUM_TO_TOOL: dict[str, str] = {
    "CODING_AGENT_CLAUDE_CODE": "claude",
    "CODING_AGENT_CODEX": "codex",
    "CODING_AGENT_GEMINI": "gemini",
    "CODING_AGENT_COPILOT": "copilot",
    "CODING_AGENT_PI": "pi",
    "CODING_AGENT_OPENCODE": "opencode",
}

_AGENT_ENUM_PREFIX = "CODING_AGENT_"
AGENT_NAME_TO_TOOL: dict[str, str] = {
    enum[len(_AGENT_ENUM_PREFIX) :].lower(): tool for enum, tool in AGENT_ENUM_TO_TOOL.items()
}

MAX_SPEC_VERSION = 1

# The last authoritative read's outcome, persisted alongside the config so a launch can replay it
# without a GET. "none" and "feature_disabled" both persist an empty config, so the outcome is what
# tells them apart.
_OUTCOME_PUBLISHED = "published"
_OUTCOME_NONE = "none"
_OUTCOME_FEATURE_DISABLED = "feature_disabled"

# A per-family default-model key in the `default_models` map, e.g. `default_opus_model`. Matches the
# server's `default_.+_model` validation so a new Claude family needs no ucode change. The bare
# `default_model` overall default does not match (no family segment) and is read on its own.
_FAMILY_SLOT_RE = re.compile(r"default_.+_model")


@dataclass(frozen=True)
class AgentModels:
    """Model configuration for an agent with mutually-exclusive source selection.

    Reads the default model and family slots from default_models map, and selects
    exactly one of: model_provider_service, unity_catalog_location, or model_services.
    """

    default_model: str | None = None
    family_slots: dict[str, str] | None = None  # default_opus_model, etc.
    model_provider_service: str | None = None
    unity_catalog_location: str | None = None
    model_services: list[str] | None = None

    @classmethod
    def from_wire(cls, default_models: object, models_obj: object) -> AgentModels | None:
        """Parse wire format (default_models map + models oneof) into AgentModels."""
        defaults = _as_dict(default_models)
        models_dict = _as_dict(models_obj)

        overall_default = _str(defaults.get("default_model"))
        # Per-family default keys are `default_<family>_model` (matching the server's
        # `default_.+_model` validation), so a new Claude family is picked up without a code change;
        # the bare `default_model` overall default is handled separately above.
        slots = {
            key: model
            for key, value in defaults.items()
            if isinstance(key, str) and _FAMILY_SLOT_RE.fullmatch(key) and (model := _str(value))
        }
        # The model source is a server-enforced oneof; keep the one present, precedence
        # model_provider_service > unity_catalog_location > model_services.
        provider = _str(models_dict.get("model_provider_service"))
        location = None if provider else _str(models_dict.get("unity_catalog_location"))
        model_services = (
            None if provider or location else (_str_list(models_dict.get("model_services")) or None)
        )

        if not (overall_default or slots or provider or location or model_services):
            return None
        return cls(
            default_model=overall_default,
            family_slots=slots or None,
            model_provider_service=provider,
            unity_catalog_location=location,
            model_services=model_services,
        )

    def to_internal(self) -> dict | None:
        """Convert to internal normalized shape (model_config key in agent config)."""
        result: dict = {}
        if self.default_model:
            result["default_model"] = self.default_model
        if self.family_slots:
            result["default_models_by_model_family"] = self.family_slots
        if self.model_provider_service:
            result["model_provider_service"] = self.model_provider_service
        elif self.unity_catalog_location:
            result["unity_catalog_location"] = self.unity_catalog_location
        elif self.model_services:
            result["model_services"] = self.model_services
        return result or None


@dataclass(frozen=True)
class AgentConfig:
    """Per-agent configuration from the wire format."""

    http_headers: dict[str, str] | None = None
    models: AgentModels | None = None
    smart_routing_enabled: bool = False
    otel_tracing_enabled: bool | None = None
    # Harness-native settings delivered verbatim into the agent's managed settings file.
    settings: dict | None = None

    @classmethod
    def from_wire(cls, config: object) -> AgentConfig:
        """Parse wire format AgentConfig into normalized AgentConfig."""
        config_dict = _as_dict(config)
        headers = _clean_str_dict(config_dict.get("http_headers"))
        smart_routing = _as_dict(config_dict.get("smart_routing"))
        agent_models = AgentModels.from_wire(
            config_dict.get("default_models"), config_dict.get("models")
        )
        return cls(
            http_headers=headers or None,
            models=agent_models,
            smart_routing_enabled=smart_routing.get("enabled") is True,
            otel_tracing_enabled=_tracing_enabled(config_dict.get("tracing")),
            settings=_as_dict(config_dict.get("agent_native_settings")) or None,
        )

    def to_internal(self) -> dict:
        """Convert to internal shape for enabled_agents dict."""
        result: dict = {}
        if self.http_headers:
            result["http_headers"] = self.http_headers
        if self.smart_routing_enabled:
            result["smart_routing_enabled"] = True
        model_config = self.models.to_internal() if self.models else None
        if model_config is not None:
            result["model_config"] = model_config
        if self.otel_tracing_enabled is not None:
            result["otel_tracing_enabled"] = self.otel_tracing_enabled
        if self.settings:
            result["agent_native_settings"] = self.settings
        return result


@dataclass(frozen=True)
class NamesOrLocation:
    """Selector for mcp_servers and skills: names list or UC location."""

    names: list[str] | None = None
    unity_catalog_location: str | None = None

    @classmethod
    def from_wire(cls, value: object) -> NamesOrLocation | None:
        """Parse the wire ``{names, unity_catalog_location}`` selector (exactly one per the API)."""
        value_dict = _as_dict(value)
        names = _str_list(value_dict.get("names"))
        location = _str(value_dict.get("unity_catalog_location"))
        if not names and not location:
            return None
        return cls(names=names or None, unity_catalog_location=location)

    def to_internal(self) -> dict:
        """Convert to internal shape."""
        result: dict = {}
        if self.names:
            result["names"] = self.names
        if self.unity_catalog_location:
            result["unity_catalog_location"] = self.unity_catalog_location
        return result


@dataclass(frozen=True)
class SmartDefaultTier:
    """One spend-based smart-default tier."""

    spending_percentage: float
    recommended_agent: str | None = None
    recommended_model: str | None = None

    @classmethod
    def from_wire(cls, tier: object) -> SmartDefaultTier | None:
        """Parse a wire-format smart-default tier."""
        tier_dict = _as_dict(tier)
        pct = tier_dict.get("spending_percentage")
        if not isinstance(pct, (int, float)) or isinstance(pct, bool):
            return None

        agent = _resolve_agent_tool(tier_dict.get("recommended_agent"))
        model = _str(tier_dict.get("recommended_model"))

        return cls(spending_percentage=float(pct), recommended_agent=agent, recommended_model=model)

    def to_internal(self) -> dict:
        """Convert to internal shape."""
        result: dict = {"spending_percentage": self.spending_percentage}
        if self.recommended_agent:
            result["recommended_agent"] = self.recommended_agent
        if self.recommended_model:
            result["recommended_model"] = self.recommended_model
        return result


@dataclass(frozen=True)
class SmartDefaults:
    """Spend-based smart defaults (the wire ``smart_defaults`` / proto ``SmartDefaults``)."""

    budget_id: str | None = None
    tiers: list[SmartDefaultTier] | None = None

    @classmethod
    def from_wire(cls, value: object) -> SmartDefaults | None:
        """Parse the wire ``smart_defaults`` (or a legacy value supplied by the caller)."""
        smart_defaults = _as_dict(value)
        if not smart_defaults:
            return None

        budget_id = _str(smart_defaults.get("budget_id"))

        raw_tiers = smart_defaults.get("tiers")
        tiers_list = [
            tier
            for raw in (raw_tiers if isinstance(raw_tiers, list) else [])
            if (tier := SmartDefaultTier.from_wire(raw)) is not None
        ]

        if not (budget_id or tiers_list):
            return None
        return cls(budget_id=budget_id, tiers=tiers_list or None)

    def to_internal(self) -> dict:
        """Convert to internal shape."""
        result: dict = {}
        if self.budget_id:
            result["budget_id"] = self.budget_id
        if self.tiers:
            result["tiers"] = [tier.to_internal() for tier in self.tiers]
        return result


@dataclass(frozen=True)
class CodingAgentConfig:
    """Top-level managed config from wire format."""

    name: str | None = None
    default_agent: str | None = None
    update_time: str | None = None
    enabled_agents: dict[str, AgentConfig] | None = None
    mcp_servers: NamesOrLocation | None = None
    skills: NamesOrLocation | None = None
    smart_defaults: SmartDefaults | None = None

    @classmethod
    def from_wire(cls, raw: object) -> CodingAgentConfig:
        """Parse wire format CodingAgentConfig."""
        raw_dict = _as_dict(raw)

        name = _str(raw_dict.get("name"))
        default_agent = _resolve_agent_tool(raw_dict.get("default_agent"))
        update_time = _str(raw_dict.get("update_time"))

        # enabled_agents is a repeated list of {agent, config} on the wire (proto EnabledAgent).
        enabled_agents_dict: dict[str, AgentConfig] = {}
        raw_agents = raw_dict.get("enabled_agents")
        if isinstance(raw_agents, list):
            for entry in raw_agents:
                entry_dict = _as_dict(entry)
                tool = _resolve_agent_tool(entry_dict.get("agent"))
                if tool is not None:
                    enabled_agents_dict[tool] = AgentConfig.from_wire(entry_dict.get("config"))

        mcp_servers = NamesOrLocation.from_wire(raw_dict.get("mcp_servers"))
        skills = NamesOrLocation.from_wire(raw_dict.get("skills"))

        # ``smart_defaults`` is the current wire key. A config read or cached by an older ug may
        # still carry ``spend_tiers``; use it only when the current key is absent, so an explicit
        # empty/null current value cannot resurrect the old policy.
        smart_defaults_value = raw_dict.get("smart_defaults", raw_dict.get("spend_tiers"))
        smart_defaults = SmartDefaults.from_wire(smart_defaults_value)

        return cls(
            name=name,
            default_agent=default_agent,
            update_time=update_time,
            enabled_agents=enabled_agents_dict or None,
            mcp_servers=mcp_servers,
            skills=skills,
            smart_defaults=smart_defaults,
        )

    def to_internal(self) -> dict:
        """Convert to internal normalized shape for launch path."""
        result: dict = {}
        if self.name:
            result["name"] = self.name
        if self.default_agent:
            result["default_agent"] = self.default_agent
        if self.update_time:
            result["update_time"] = self.update_time

        if self.enabled_agents:
            enabled_agents_internal: dict[str, dict] = {}
            for tool, agent_config in self.enabled_agents.items():
                enabled_agents_internal[tool] = agent_config.to_internal()
            result["enabled_agents"] = enabled_agents_internal

        if self.mcp_servers:
            mcp_internal = self.mcp_servers.to_internal()
            if mcp_internal:
                result["mcp_servers"] = mcp_internal

        if self.skills:
            skills_internal = self.skills.to_internal()
            if skills_internal:
                result["skills"] = skills_internal

        if self.smart_defaults:
            smart_defaults_internal = self.smart_defaults.to_internal()
            if smart_defaults_internal:
                result["smart_defaults"] = smart_defaults_internal

        return result


class FetchedManagedConfig(NamedTuple):
    """A managed-config read: the normalized ``manifest`` (None when the workspace has none) and,
    when the read did not settle the question, the ``reason`` it failed (None on a clean answer)."""

    manifest: dict | None
    reason: str | None


class ManagedPolicyInstallDenied(RuntimeError):
    """``ug configure`` could not install, replace, or remove the machine policy (e.g. no sudo)."""


class ManagedConfigResult(NamedTuple):
    """The launch-path refresh outcome: the ``manifest`` to apply (None when absent or dropped) and
    ``feature_disabled``, True when the coding-agent-configs feature is off server-side."""

    manifest: dict | None
    feature_disabled: bool


def _as_dict(value: object) -> dict[str, object]:
    """Return ``value`` as a ``dict[str, object]`` when it is a dict, else an empty dict.

    Centralizes the isinstance-narrowing so downstream ``.get`` calls type-check (a bare
    ``isinstance(x, dict)`` narrows to ``dict[Never, Never]``, which rejects string keys)."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _clean_str_dict(value: object) -> dict[str, str]:
    """Keep only the string->string entries of ``value`` (a headers map), or an empty dict."""
    return {k: v for k, v in _as_dict(value).items() if isinstance(k, str) and isinstance(v, str)}


def _str(value: object) -> str | None:
    """Return a non-empty stripped string, or None."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        s = _str(item)
        if s:
            out.append(s)
    return out


def _tracing_enabled(tracing: object) -> bool | None:
    """Return ``TracingConfig.enabled`` as a tri-state value."""
    value = _as_dict(tracing).get("enabled")
    return value if isinstance(value, bool) else None


def _resolve_agent_tool(key: object) -> str | None:
    """Map an agent reference to a ucode tool name, accepting either spelling.

    The server may send agent references as either proto enum (``CODING_AGENT_CLAUDE_CODE``) or
    by name (``claude_code``). Both resolve to the same tool, or None when this build doesn't
    know the agent.
    """
    name = _str(key)
    if name is None:
        return None
    return AGENT_ENUM_TO_TOOL.get(name) or AGENT_NAME_TO_TOOL.get(name)


def normalize_managed_config(raw: dict) -> dict:
    """Normalize a raw ``CodingAgentConfig`` proto-JSON dict into ucode's internal shape.

    The internal shape uses ucode's own tool names so downstream reconcile and apply code never
    touches proto enum spellings. Unknown agents are dropped.
    """
    cfg = CodingAgentConfig.from_wire(raw)
    return cfg.to_internal()


def managed_update_time(managed: dict | None) -> str | None:
    """The config's server-side ``update_time`` (RFC-3339), or None when absent.

    This is the version watermark: it advances only when an admin edits the workspace config, so a
    launch compares it against the last-applied value to decide whether to re-apply. The GET also
    returns ``retrieved_time``, which changes on every read and must never be used for this.
    """
    return _str(_as_dict(managed).get("update_time"))


def managed_config_is_newer(fetched: dict | None, applied_update_time: str | None) -> bool:
    """True when ``fetched`` is a newer version than the last one applied locally.

    A fetched config whose ``update_time`` is missing or unparseable is treated as newer, so a launch
    re-applies it rather than trusting possibly-stale local settings; no previously-applied watermark
    also counts as newer (the first apply).
    """
    fetched_ut = parse_update_time(managed_update_time(fetched))
    applied_ut = parse_update_time(applied_update_time)
    if fetched_ut is None or applied_ut is None:
        return True
    return fetched_ut > applied_ut


def _decimal(value: object) -> float | None:
    """Parse one of the API's decimal-string money fields, or None when absent/unparseable."""
    text = _str(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def get_model_recommendation(workspace: str, token: str) -> tuple[dict | None, str | None]:
    """Fetch the agent and model the caller's budget tier allows, normalized for the launch path.

    Returns ``(recommendation, reason)`` where the recommendation is ``{"agent", "model",
    "current_spend", "effective_threshold"}``. Every field is optional server-side, so each is
    normalized independently: an agent this build doesn't recognize is dropped rather than failing
    the read, and a model can arrive without an agent.
    """
    payload, reason = fetch_model_recommendation(workspace, token)
    if reason is not None:
        return None, reason
    agent = AGENT_ENUM_TO_TOOL.get(_str(payload.get("recommended_agent")) or "")
    model = _str(payload.get("recommended_model"))
    spend = _decimal(payload.get("current_spend"))
    threshold = _decimal(payload.get("effective_threshold"))
    if agent is None and model is None and spend is None and threshold is None:
        return None, None
    return {
        "agent": agent,
        "model": model,
        "current_spend": spend,
        "effective_threshold": threshold,
    }, None


class _StubUnset:
    """Sentinel type: the stub env var is unset or unreadable, so fall through to the real read."""


_STUB_UNSET = _StubUnset()


def get_managed_config(workspace: str, token: str) -> FetchedManagedConfig:
    """Fetch and normalize the workspace's managed config.

    Returns a :class:`FetchedManagedConfig`:
    - ``manifest`` set, ``reason`` None — the normalized manifest for the workspace's single config;
    - both None — the workspace definitively has no managed config (not an error);
    - ``manifest`` None, ``reason`` set — the read didn't settle the question; ``reason`` says why.

    The distinction matters to callers that cache: only "both None" is authoritative enough to clear
    a previously stored config. "No config defined" arrives two ways depending on the backend — an
    empty listing (HTTP 200 with no configs) or a NOT_FOUND — and both collapse to "both None".
    Anything else, including a PERMISSION_DENIED, leaves the question unanswered and is surfaced as a
    failure: an admin may have published a config the developer can't read, which they need to know
    about rather than silently launch without.

    v0 stores at most one config per workspace, so the first entry is the workspace's config.

    ``UCODE_MANAGED_CONFIG_STUB`` short-circuits the HTTP read: when it names a readable JSON file,
    that file's single CodingAgentConfig is used verbatim, or an explicit ``null`` stands in for a
    workspace with no managed config. It exists so this client can be exercised against the
    managed-config shape before the server emits it (AIGTWY-4572); unset in normal use.
    """
    stub = _stub_config()
    if not isinstance(stub, _StubUnset):
        if stub is None:
            return FetchedManagedConfig(None, None)
        return _gate_config(stub)
    configs, reason = fetch_managed_coding_agent_configs(workspace, token)
    if reason is not None:
        if _is_feature_disabled(reason):
            return FetchedManagedConfig(None, reason)
        # A NOT_FOUND means the admin hasn't defined a config for this workspace — not a failure.
        if _is_not_found(reason):
            return FetchedManagedConfig(None, None)
        return FetchedManagedConfig(None, reason)
    if not configs:
        return FetchedManagedConfig(None, None)
    return _gate_config(configs[0])


def _stub_config() -> dict | None | _StubUnset:
    """The stub named by ``UCODE_MANAGED_CONFIG_STUB``: the CodingAgentConfig dict it holds, ``None``
    when it is an explicit JSON ``null`` (a workspace with no managed config), or ``_STUB_UNSET``
    when the var is unset, the file cannot be read, or its content is not a config dict (fall
    through to the real read)."""
    path = os.environ.get("UCODE_MANAGED_CONFIG_STUB")
    if not path:
        return _STUB_UNSET
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print_warning(f"UCODE_MANAGED_CONFIG_STUB could not be read ({exc}); ignoring it.")
        return _STUB_UNSET
    if raw is None:
        return None
    return raw if isinstance(raw, dict) else _STUB_UNSET


def _gate_config(raw: dict) -> FetchedManagedConfig:
    """Apply the ``spec_version`` forward-compat gate and return the raw config unchanged.

    A config declaring a ``spec_version`` newer than this build understands is refused as an
    unresolved read (``reason`` set), so the launch path falls back to the last-known-good cache and
    never blocks — the same treatment as any read this build can't act on. On a clean read the raw
    config is returned verbatim; normalization happens later, at the read/return boundaries, so the
    persisted file stays byte-identical to what the gateway returned.
    """
    spec = raw.get("spec_version")
    if spec is not None:
        if isinstance(spec, bool) or not isinstance(spec, int):
            return FetchedManagedConfig(
                None,
                f"Your managed configuration has an unrecognized spec_version ({spec!r}); "
                "update Unity Gateway with `ug upgrade`.",
            )
        if spec > MAX_SPEC_VERSION:
            return FetchedManagedConfig(
                None,
                f"Your managed configuration needs a newer Unity Gateway (spec_version {spec}; "
                f"this build supports up to {MAX_SPEC_VERSION}). Run `ug upgrade`.",
            )
    return FetchedManagedConfig(raw, None)


def _is_not_found(reason: str) -> bool:
    """True when a read failure reason means the workspace definitively has no managed config.

    ``_http_get_json`` formats failures as ``HTTP <code> <text>[: <body>]``; a NOT_FOUND surfaces
    as an ``HTTP 404`` there (and the API's error body carries ``NOT_FOUND``)."""
    lowered = reason.lower()
    return "http 404" in lowered or "not_found" in lowered


def _is_permission_denied(reason: str) -> bool:
    """True when the read was refused rather than answering whether a config exists.

    The read is meant to be available to any workspace user, so a refusal means the workspace's
    managed config isn't readable by this developer — worth telling them about, since an admin may
    have published a config that silently isn't reaching them. It settles nothing about whether one
    exists, so a cached config is left in place rather than cleared."""
    lowered = reason.lower()
    return "http 403" in lowered or "permission_denied" in lowered


def _is_unsupported_spec(reason: str) -> bool:
    """True when the read failed because the config's ``spec_version`` is newer than this build.

    Unlike a transient read failure, this is proof a policy exists, so it is surfaced even with no
    cached config to fall back on.
    """
    return "spec_version" in reason.lower()


def _policy_content(config: dict) -> dict:
    return {key: value for key, value in config.items() if key != "retrieved_time"}


def _policy_hash(workspace: str, config: dict) -> str:
    payload = {"workspace": normalize_workspace_url(workspace), "config": _policy_content(config)}
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def save_managed_state(
    workspace: str,
    config: dict,
    *,
    interactive: bool = False,
    allow_sudo: bool = False,
    sets_machine_workspace: bool = False,
) -> None:
    """Install raw policy only when changed; remove a matching policy when it no longer exists.

    ``sets_machine_workspace`` marks an explicit ``ug configure``, which decides the machine's
    workspace: a workspace that publishes no policy then also removes another workspace's
    installed policy. A launch never does.
    """
    workspace = normalize_workspace_url(workspace)
    existing = config_io.read_json_safe(MANAGED_CONFIG_PATH)
    existing_workspace = existing.get("workspace")
    matches = (
        isinstance(existing_workspace, str)
        and bool(existing_workspace.strip())
        and normalize_workspace_url(existing_workspace) == workspace
    )
    if not config:
        if matches or (sets_machine_workspace and existing_workspace):
            update_workspace_policy(
                MANAGED_CONFIG_PATH, None, interactive=interactive, allow_sudo=allow_sudo
            )
        return
    existing_config = existing.get("config")
    if (
        matches
        and isinstance(existing_config, dict)
        and _policy_content(existing_config) == _policy_content(config)
    ):
        return
    payload = {"workspace": workspace, "config": config}
    update_workspace_policy(
        MANAGED_CONFIG_PATH,
        json.dumps(payload, indent=2) + "\n",
        interactive=interactive,
        allow_sudo=allow_sudo,
    )


def revert_machine_policy() -> str:
    """Remove the installed workspace policy for ``ug revert``: "removed", "unchanged", or "failed".

    Nothing else writes this file, so revert deletes it outright. Without sudo it warns and keeps
    the file, like the other machine-wide settings revert restores.
    """
    if not _machine_policy_installable() or not MANAGED_CONFIG_PATH.exists():
        return "unchanged"
    try:
        update_workspace_policy(MANAGED_CONFIG_PATH, None, interactive=True, allow_sudo=True)
    except RuntimeError as exc:
        print_warning(
            f"Could not remove the workspace policy at {MANAGED_CONFIG_PATH}: {exc} Rerun "
            "`ug revert` from a terminal where you can use sudo."
        )
        return "failed"
    return "removed"


def _restrict_permissions(path: Path) -> None:
    """Best-effort chmod 0600 for user-owned refresh metadata."""
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def load_managed_state(workspace: str | None) -> dict | None:
    """Load the persisted managed config for ``workspace`` normalized into ucode's internal shape.

    The file stores the raw gateway config; this reads it and returns
    :func:`normalize_managed_config` of it, so every consumer keeps working against the normalized
    shape. Returns None when there is no file for this workspace (a stale file from another workspace
    is ignored rather than misapplied). A stored empty config normalizes to an empty dict, which
    callers already treat as "no config".
    """
    raw = load_managed_configuration(workspace)
    if raw is None:
        return None
    return normalize_managed_config(raw)


def load_managed_configuration(workspace: str | None) -> dict | None:
    """Return the raw managed config persisted for ``workspace`` (verbatim as the gateway returned
    it), or None if absent or stored for a different workspace.

    Unlike :func:`load_managed_state` this does not normalize: it is the exact CodingAgentConfig, for
    ``ug export`` and for inspecting the on-disk file.
    """
    if not workspace:
        return None
    installed = _installed_config(workspace)
    if installed is not None or _machine_policy_installable():
        return installed
    return _stored_config(USER_MANAGED_CONFIG_PATH, workspace)


def _installed_config(workspace: str) -> dict | None:
    """The installed machine policy for ``workspace``, read only where ug installs one.

    On Windows the POSIX path resolves under the current drive's root, which a standard user may be
    able to create, so it is never trusted there.
    """
    if not _machine_policy_supported():
        return None
    return _stored_config(MANAGED_CONFIG_PATH, workspace)


def _stored_config(path: Path, workspace: str) -> dict | None:
    """The raw config ``path`` stores for ``workspace``, or None if absent or for another one."""
    data = config_io.read_json_safe(path)
    stored_workspace = data.get("workspace")
    if (
        not isinstance(stored_workspace, str)
        or not stored_workspace.strip()
        or normalize_workspace_url(stored_workspace) != normalize_workspace_url(workspace)
    ):
        return None
    config = data.get("config")
    return config if isinstance(config, dict) else None


def managed_state_workspace(*, include_user_cache: bool = False) -> str | None:
    """The workspace the installed machine policy belongs to, or None when there is none.

    Launches enforce this workspace. ``include_user_cache`` also accepts the per-user cache's
    workspace where no machine policy is possible, for a caller with no workspace in local state
    (e.g. ``ug export`` before ``ug configure``) that only needs to find a saved policy.
    """
    paths = [MANAGED_CONFIG_PATH] if _machine_policy_supported() else []
    if include_user_cache and not _machine_policy_installable():
        paths.append(USER_MANAGED_CONFIG_PATH)
    for path in paths:
        workspace = config_io.read_json_safe(path).get("workspace")
        if isinstance(workspace, str) and workspace:
            return workspace
    return None


def managed_launch_workspace(workspace: str | None) -> str | None:
    """Resolve launch policy from the installed workspace, refusing a different selection."""
    installed = managed_state_workspace()
    installed = normalize_workspace_url(installed) if installed and installed.strip() else None
    selected = normalize_workspace_url(workspace) if workspace else None
    if installed and selected and selected != installed:
        # UG_SKIP_OS_SETTINGS never writes the policy, so the switch must run without it.
        without_skip = f" without {SKIP_OS_SETTINGS_ENV}" if managed_settings_skipped() else ""
        raise RuntimeError(
            f"This machine is managed for {installed}, but your selected workspace is {selected}. "
            f"Launch refused. Use `--workspace {installed}`, or ask your IT team or an administrator "
            f"to switch the machine with `ug configure --workspace {selected}`{without_skip}."
        )
    return installed or selected


def _saved_result(workspace: str) -> ManagedConfigResult | None:
    """Replay the saved read for ``workspace`` while the installed policy is unchanged.

    None when nothing is saved: no outcome record for this workspace, or the installed policy
    changed since it was recorded.
    """
    entry = config_io.read_json_safe(REFRESH_TS_PATH).get(normalize_workspace_url(workspace))
    if not isinstance(entry, dict):
        return None
    if entry.get("policy_fingerprint") != managed_file_fingerprint(MANAGED_CONFIG_PATH):
        return None
    outcome = entry.get("outcome")
    raw = load_managed_configuration(workspace)
    if raw:
        # A disabled feature whose installed policy couldn't be removed still reports disabled.
        return ManagedConfigResult(
            normalize_managed_config(raw), outcome == _OUTCOME_FEATURE_DISABLED
        )
    if outcome == _OUTCOME_FEATURE_DISABLED:
        return ManagedConfigResult(None, True)
    if outcome == _OUTCOME_NONE:
        return ManagedConfigResult(None, False)
    return None


def _record_refresh(
    workspace: str, result: ManagedConfigResult, *, pending_config_hash: str | None = None
) -> None:
    if config_io.is_dry_run():
        return
    data = config_io.read_json_safe(REFRESH_TS_PATH)
    data[normalize_workspace_url(workspace)] = {
        # Disabled first: an installed policy whose removal failed still replays as disabled.
        "outcome": (
            _OUTCOME_FEATURE_DISABLED
            if result.feature_disabled
            else _OUTCOME_PUBLISHED
            if result.manifest is not None
            else _OUTCOME_NONE
        ),
        "policy_fingerprint": managed_file_fingerprint(MANAGED_CONFIG_PATH),
    }
    if pending_config_hash is not None:
        data[normalize_workspace_url(workspace)]["pending_config_hash"] = pending_config_hash
    try:
        config_io.atomic_write_json(REFRESH_TS_PATH, data)
        _restrict_permissions(REFRESH_TS_PATH)
    except RuntimeError as exc:
        print_warning(f"Could not save the workspace policy refresh record: {exc}")


def refresh_managed_config(state: dict, *, force_refresh: bool = False) -> ManagedConfigResult:
    """Read the workspace policy; a launch replays what ``ug configure`` saved.

    A launch reads the control plane only when nothing is saved for the workspace yet; it never
    refreshes on a timer, so an admin's edit reaches a machine with the next ``ug configure``.
    ``force_refresh`` (explicit configuration) always reads fresh. The installed policy changes
    only when the workspace or configuration changes; unchanged reads update only refresh metadata.
    Only explicit configuration may invoke sudo. Non-root launches are read-only regardless of
    terminal attachment or cached sudo access; root launches can install without sudo.

    The installed workspace is authoritative for launches; mismatched selections fail before
    saved reads or remote requests. Explicit configure can change the machine workspace.
    Failed launch writes warn once per distinct pending policy and return the installed policy,
    recording the outcome. Explicit configuration write failures raise instead.
    Fetch failures retain the existing installed-policy fallback and record nothing.
    No-config and ``FEATURE_DISABLED`` remove a matching installed policy; if removal fails, launches
    keep using it with a warning rather than pretending it was removed.
    """
    workspace = (
        state.get("workspace")
        if force_refresh
        else managed_launch_workspace(state.get("workspace"))
    )
    if not workspace:
        return ManagedConfigResult(None, False)
    if not _machine_policy_installable() and _installed_config(workspace) is None:
        return _refresh_user_cache(state, workspace, force_refresh=force_refresh)
    if config_io.is_dry_run() and not force_refresh:
        return ManagedConfigResult(load_managed_state(workspace), False)
    if not force_refresh:
        cached = _saved_result(workspace)
        if cached is not None:
            return cached
    try:
        token = get_databricks_token(workspace, state.get("profile"))
    except RuntimeError as exc:
        return ManagedConfigResult(_persisted_fallback(workspace, str(exc)), False)
    raw, reason = get_managed_config(workspace, token)
    if reason is not None:
        if not _is_feature_disabled(reason):
            fallback = _persisted_fallback(workspace, reason, refused=_is_permission_denied(reason))
            return ManagedConfigResult(fallback, False)
    if not _can_install_policy(force_refresh) and load_managed_configuration(workspace) is None:
        # Nothing is installed for this workspace and this non-root launch can't install it: apply
        # the fetched policy to this run only, without installing it. An installed policy still
        # wins, and only `ug configure` or root can install one.
        result = ManagedConfigResult(
            normalize_managed_config(raw) if raw is not None else None,
            reason is not None and _is_feature_disabled(reason),
        )
        _record_refresh(workspace, result)
        return result
    pending_config_hash = None
    try:
        save_managed_state(
            workspace,
            raw or {},
            interactive=force_refresh,
            allow_sudo=force_refresh,
            sets_machine_workspace=force_refresh,
        )
    except RuntimeError as exc:
        if force_refresh and _machine_policy_installable():
            message = (
                f"Could not install the workspace policy at {MANAGED_CONFIG_PATH}: {exc} "
                "This machine-wide file needs sudo. Rerun `ug configure` from a terminal where you "
                "can use sudo, or ask your IT team or an administrator to run it."
            )
            raise ManagedPolicyInstallDenied(message) from exc
        installed = load_managed_state(workspace)
        if installed is not None:
            result = ManagedConfigResult(
                installed, raw is None and reason is not None and _is_feature_disabled(reason)
            )
            fallback_message = "Continuing with the installed policy."
        else:
            # Nothing is installed for this workspace: the fetched policy still governs this run.
            result = ManagedConfigResult(
                normalize_managed_config(raw) if raw is not None else None,
                reason is not None and _is_feature_disabled(reason),
            )
            fallback_message = "Continuing with the fetched policy for this run."
        pending_config_hash = _policy_hash(workspace, raw or {})
        entry = config_io.read_json_safe(REFRESH_TS_PATH).get(normalize_workspace_url(workspace))
        if not isinstance(entry, dict) or entry.get("pending_config_hash") != pending_config_hash:
            if managed_settings_skipped():
                print_warning(
                    f"Your workspace policy changed, but {SKIP_OS_SETTINGS_ENV} is set, so the "
                    f"installed policy at {MANAGED_CONFIG_PATH} was not updated. Run `ug configure` "
                    f"without {SKIP_OS_SETTINGS_ENV} as an administrator. {fallback_message}"
                )
            else:
                print_warning(
                    "Your workspace policy changed. Ask your IT team or an administrator to apply "
                    f"the update on this machine. {fallback_message}"
                )
    else:
        result = ManagedConfigResult(
            normalize_managed_config(raw) if raw is not None else None,
            reason is not None and _is_feature_disabled(reason),
        )
    _record_refresh(workspace, result, pending_config_hash=pending_config_hash)
    return result


def _machine_policy_supported() -> bool:
    """Whether ug installs a machine policy on this OS at all (Linux and macOS)."""
    return current_os() in (OS.LINUX, OS.MACOS)


def _machine_policy_installable() -> bool:
    """Whether this machine can hold the installed workspace policy (Linux or macOS, not opted out
    with ``UG_SKIP_OS_SETTINGS``)."""
    return not managed_settings_skipped() and _machine_policy_supported()


def _can_install_policy(explicit_configure: bool) -> bool:
    """Whether this run may install the machine policy: root, or an explicit ``ug configure``
    (which may use sudo). A non-root launch never does."""
    return _machine_policy_installable() and (explicit_configure or is_admin())


def _refresh_user_cache(state: dict, workspace: str, *, force_refresh: bool) -> ManagedConfigResult:
    """Cache policy per user when machine installation is unavailable.

    A launch replays the saved read and only reads the control plane when none is saved,
    ``ug configure`` always reads fresh, a failed read falls back to the last saved config for this
    workspace, and no-config or ``FEATURE_DISABLED`` replaces it.
    """
    if not force_refresh:
        cached = _saved_user_result(workspace)
        if cached is not None:
            return cached
    try:
        token = get_databricks_token(workspace, state.get("profile"))
    except RuntimeError as exc:
        return ManagedConfigResult(_persisted_fallback(workspace, str(exc)), False)
    raw, reason = get_managed_config(workspace, token)
    if reason is not None:
        if _is_feature_disabled(reason):
            _save_user_cache(workspace, {}, outcome=_OUTCOME_FEATURE_DISABLED)
            return ManagedConfigResult(None, True)
        fallback = _persisted_fallback(workspace, reason, refused=_is_permission_denied(reason))
        return ManagedConfigResult(fallback, False)
    if raw is None:
        # Record that this workspace has no config, so a removed policy can't return as the
        # fallback after a later outage.
        _save_user_cache(workspace, {}, outcome=_OUTCOME_NONE)
        return ManagedConfigResult(None, False)
    _save_user_cache(workspace, raw, outcome=_OUTCOME_PUBLISHED)
    return ManagedConfigResult(normalize_managed_config(raw), False)


def _save_user_cache(workspace: str, config: dict, *, outcome: str) -> None:
    """Persist the raw config and its read outcome to the per-user cache at mode 0600."""
    payload = {
        "workspace": workspace,
        "config": config,
        "outcome": outcome,
    }
    if config_io.is_dry_run():
        console.print(
            f"\n[bold]\\[dry run] {USER_MANAGED_CONFIG_PATH}[/bold]\n"
            f"{json.dumps(payload, indent=2)}\n"
        )
        return
    config_io.ensure_parent_dir(USER_MANAGED_CONFIG_PATH)
    try:
        USER_MANAGED_CONFIG_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"Failed to write managed state file: {USER_MANAGED_CONFIG_PATH}"
        ) from exc
    _restrict_permissions(USER_MANAGED_CONFIG_PATH)


def _saved_user_result(workspace: str) -> ManagedConfigResult | None:
    """The per-user cache's read for ``workspace`` replayed as a result, or None when none is saved.

    A file written before ug recorded the outcome counts as nothing saved, so the next read fetches
    once and stamps it: an older empty file could be either "no config" or a disabled feature.
    """
    data = config_io.read_json_safe(USER_MANAGED_CONFIG_PATH)
    stored = data.get("workspace")
    if not isinstance(stored, str) or normalize_workspace_url(stored) != normalize_workspace_url(
        workspace
    ):
        return None
    config = data.get("config")
    if not isinstance(config, dict):
        return None
    outcome = data.get("outcome")
    if outcome == _OUTCOME_FEATURE_DISABLED:
        return ManagedConfigResult(None, True)
    if outcome == _OUTCOME_NONE:
        return ManagedConfigResult(None, False)
    if outcome == _OUTCOME_PUBLISHED and config:
        return ManagedConfigResult(normalize_managed_config(config), False)
    return None


def _is_feature_disabled(reason: str) -> bool:
    return "feature_disabled" in reason.lower()


def _persisted_fallback(workspace: str, reason: str, *, refused: bool = False) -> dict | None:
    """Return the last persisted config for ``workspace`` after a failed fetch.

    Warns only when there is a config to fall back on, because then the launch proceeds on an admin
    policy that may be out of date. With nothing persisted there is no managed config in play at
    all, so staying quiet keeps someone with (say) an expired session from being told about a
    feature they don't use — including when the read was ``refused``, since a refusal is no evidence
    that a config exists.
    """
    # An empty persisted config means the last successful read found none, so there is no admin
    # policy to fall back to — treat it the same as having no file at all.
    persisted = load_managed_state(workspace)
    if not persisted:
        if _is_unsupported_spec(reason):
            print_warning(reason)
        return None
    summary = _summarize_read_failure(reason)
    if refused:
        print_warning(
            f"Your managed configuration is not readable by you ({summary}); using the last "
            "one saved for this workspace. Ask an admin to grant access."
        )
    else:
        print_warning(
            f"Could not read your managed configuration ({summary}); "
            "using the last one saved for this workspace."
        )
    return persisted


def _summarize_read_failure(reason: str) -> str:
    """Condense a read failure into one short line fit for a terminal warning.

    ``_http_get_json`` appends the raw response body, which for a gateway error is a multi-line JSON
    blob (error_code, message, request_id, trace ids). Surface just the status and the API's own
    message; the full text is still available under ``UCODE_DEBUG=1``.
    """
    status, _, body = reason.partition(": ")
    body = body.strip()
    if body.startswith("{"):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            message = _str(parsed.get("message")) or _str(parsed.get("error_code"))
            if message:
                return f"{status.strip()}: {message}"
        return status.strip()
    condensed = " ".join(reason.split())
    return condensed if len(condensed) <= 160 else condensed[:157] + "..."
