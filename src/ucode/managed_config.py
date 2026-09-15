"""Managed coding-agent config: fetch, normalize, and local persistence (launch/refresh side).

An org admin defines a ``CodingAgentConfig`` on the Databricks AI Gateway; developers read it
(non-admin) and ``ucode`` applies it locally. This module owns the fetch/normalize side and the one
local file, ``~/.ucode/managed-config.json`` (0600), used on the launch path:

- fetching the raw manifest (via :func:`ucode.databricks.fetch_managed_coding_agent_configs`),
- normalizing the proto-JSON into a stable internal dict keyed by ucode's own tool names,
- persisting it via :func:`save_managed_state` / :func:`load_managed_state` — the launch path pulls
  the published copy into this file, and
- re-reading it on each launch, falling back to the persisted copy when the read fails.

There is deliberately one file: the workspace is the source of truth, so the pulled copy lives in
``managed-config.json`` and a launch re-reads it from there.

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
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, cast

import ucode.config_io as config_io
from ucode.databricks import (
    fetch_managed_coding_agent_configs,
    fetch_model_recommendation,
    get_databricks_token,
)
from ucode.ui import console, print_warning

MANAGED_CONFIG_PATH = config_io.APP_DIR / "managed-config.json"

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

    @classmethod
    def from_wire(cls, config: object) -> AgentConfig:
        """Parse wire format AgentConfig into normalized AgentConfig."""
        config_dict = _as_dict(config)
        headers = _clean_str_dict(config_dict.get("http_headers"))
        agent_models = AgentModels.from_wire(
            config_dict.get("default_models"), config_dict.get("models")
        )
        return cls(http_headers=headers or None, models=agent_models)

    def to_internal(self) -> dict:
        """Convert to internal shape for enabled_agents dict."""
        result: dict = {}
        if self.http_headers:
            result["http_headers"] = self.http_headers
        model_config = self.models.to_internal() if self.models else None
        if model_config is not None:
            result["model_config"] = model_config
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
class SpendTier:
    """One budget tier."""

    spending_percentage: float
    recommended_agent: str | None = None
    recommended_model: str | None = None

    @classmethod
    def from_wire(cls, tier: object) -> SpendTier | None:
        """Parse wire format spend tier."""
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
class SpendTiers:
    """Spend-based routing tiers (the wire ``spend_tiers`` / proto ``SpendTiers``)."""

    budget_id: str | None = None
    tiers: list[SpendTier] | None = None

    @classmethod
    def from_wire(cls, value: object) -> SpendTiers | None:
        """Parse the wire ``spend_tiers``."""
        spend_tiers = _as_dict(value)
        if not spend_tiers:
            return None

        budget_id = _str(spend_tiers.get("budget_id"))

        raw_tiers = spend_tiers.get("tiers")
        tiers_list = [
            tier
            for raw in (raw_tiers if isinstance(raw_tiers, list) else [])
            if (tier := SpendTier.from_wire(raw)) is not None
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
    spend_tiers: SpendTiers | None = None

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

        spend_tiers = SpendTiers.from_wire(raw_dict.get("spend_tiers"))

        return cls(
            name=name,
            default_agent=default_agent,
            update_time=update_time,
            enabled_agents=enabled_agents_dict or None,
            mcp_servers=mcp_servers,
            skills=skills,
            spend_tiers=spend_tiers,
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

        if self.spend_tiers:
            spend_tiers_internal = self.spend_tiers.to_internal()
            if spend_tiers_internal:
                result["spend_tiers"] = spend_tiers_internal

        return result


class FetchedManagedConfig(NamedTuple):
    """A managed-config read: the normalized ``manifest`` (None when the workspace has none) and,
    when the read did not settle the question, the ``reason`` it failed (None on a clean answer)."""

    manifest: dict | None
    reason: str | None


class ManagedConfigResult(NamedTuple):
    """The launch-path refresh outcome: the ``manifest`` to apply (None when absent or dropped),
    ``feature_disabled`` True when the coding-agent-configs feature is off server-side, and
    ``definitively_absent`` True when the absence is definitive (NOT_FOUND or feature disabled)
    rather than due to a transient fetch failure."""

    manifest: dict | None
    feature_disabled: bool
    definitively_absent: bool


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


def _parse_update_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # An offset-less timestamp (e.g. a stub value) parses tz-naive; pin it to UTC so it can be
    # compared against the tz-aware persisted watermark without raising.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def managed_config_is_newer(fetched: dict | None, applied_update_time: str | None) -> bool:
    """True when ``fetched`` is a newer version than the last one applied locally.

    A fetched config whose ``update_time`` is missing or unparseable is treated as newer, so a launch
    re-applies it rather than trusting possibly-stale local settings; no previously-applied watermark
    also counts as newer (the first apply).
    """
    fetched_ut = _parse_update_time(managed_update_time(fetched))
    applied_ut = _parse_update_time(applied_update_time)
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
    that file's single CodingAgentConfig is used verbatim. It exists so this client can be exercised
    against the managed-config shape before the server emits it (AIGTWY-4572); unset in normal use.
    """
    stub = _stub_config()
    if stub is not None:
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


def _stub_config() -> dict | None:
    """The stub CodingAgentConfig named by ``UCODE_MANAGED_CONFIG_STUB``, or None when unset/bad."""
    path = os.environ.get("UCODE_MANAGED_CONFIG_STUB")
    if not path:
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print_warning(f"UCODE_MANAGED_CONFIG_STUB could not be read ({exc}); ignoring it.")
        return None
    return raw if isinstance(raw, dict) else None


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


def save_managed_state(workspace: str, config: dict) -> None:
    """Persist the raw managed config to ``~/.ucode/managed-config.json`` at mode 0600.

    ``config`` is stored verbatim as the gateway returned it (byte-identical to the GET), so the file
    is inspectable and ``ug export`` can dump it unchanged; normalization into ucode's internal shape
    happens on read (:func:`load_managed_state`), not here. The file is org-authored, not
    developer-editable — 0600 keeps it readable/writable only by the user. No-op in dry-run.

    An empty ``config`` records "this workspace has no managed config", which matters because the
    file doubles as the fallback when a later read fails: without it, removing a config server-side
    would leave the old one on disk to be reapplied after a transient outage.
    """
    payload: dict = {"workspace": workspace, "config": config}
    if config_io.is_dry_run():
        # Print rather than write, matching how the agent config writers behave under --dry-run.
        console.print(
            f"\n[bold]\\[dry run] {MANAGED_CONFIG_PATH}[/bold]\n{json.dumps(payload, indent=2)}\n"
        )
        return
    config_io.ensure_parent_dir(MANAGED_CONFIG_PATH)
    try:
        MANAGED_CONFIG_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to write managed state file: {MANAGED_CONFIG_PATH}") from exc
    _restrict_permissions(MANAGED_CONFIG_PATH)


def _restrict_permissions(path: Path) -> None:
    """Best-effort chmod 0600. No-op where unsupported (e.g. Windows), where the effective
    read-only guarantee is left to a later change."""
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
    data = config_io.read_json_safe(MANAGED_CONFIG_PATH)
    if data.get("workspace") != workspace:
        return None
    config = data.get("config")
    return config if isinstance(config, dict) else None


def managed_state_workspace() -> str | None:
    """The workspace the on-disk managed config was authored/pulled for, or None when there is none.

    Lets a caller that has no workspace in local ucode state (e.g. ``ucode setup --show`` before
    ``ucode configure``) still find the manifest on disk and report which workspace it belongs to.
    """
    workspace = config_io.read_json_safe(MANAGED_CONFIG_PATH).get("workspace")
    return workspace if isinstance(workspace, str) and workspace else None


def refresh_managed_config(state: dict) -> ManagedConfigResult:
    """Fetch the workspace's managed config fresh and persist it as a :class:`ManagedConfigResult`.

    Runs on every launch so a developer picks up an admin's edits without re-running
    ``ucode configure``. It always hits the control plane; whether the fetched config is *newer* than
    what was last applied — and so whether the launch re-applies the settings — is the caller's
    decision, via :func:`managed_config_is_newer` against the persisted applied watermark. The
    manifest is None when the workspace has no managed config — the normal case for a workspace whose
    admin hasn't published one.

    A failed fetch never blocks the launch: an unreachable control plane shouldn't stop someone from
    coding. Instead it falls back to the last config persisted for this workspace, so the admin's
    most recent known policy still applies; only when there is no persisted config either does the
    launch fall through to the developer's own settings. ``FEATURE_DISABLED`` is the exception — it
    is an authoritative "off", not a transient failure, so it drops the cache rather than falling
    back (see below).

    ``coding_agent_config_feature_disabled`` is True whenever the gateway returned ``FEATURE_DISABLED`` —
    the coding-agent-configs feature isn't enabled server-side, so callers suppress the ``ucode
    setup`` recommendation. A config cached from when the feature was enabled is discarded in that
    case (returned manifest is None), so a launch doesn't re-apply a policy the workspace has turned
    off and ``ug configure`` doesn't route into a managed-setup flow that would dead-end.
    """
    workspace = state.get("workspace")
    if not workspace:
        return ManagedConfigResult(None, False, False)
    try:
        token = get_databricks_token(workspace, state.get("profile"))
    except RuntimeError as exc:
        return ManagedConfigResult(_persisted_fallback(workspace, str(exc)), False, False)
    raw, reason = get_managed_config(workspace, token)
    if reason is not None:
        if _is_feature_disabled(reason):
            save_managed_state(workspace, {})
            return ManagedConfigResult(None, True, True)
        fallback = _persisted_fallback(workspace, reason, refused=_is_permission_denied(reason))
        return ManagedConfigResult(fallback, False, False)
    if raw is None:
        # Record that this workspace has no config, rather than leaving an earlier one on disk:
        # the file doubles as the fallback above, so a removed policy would otherwise come back
        # into force after the next transient outage.
        save_managed_state(workspace, {})
        return ManagedConfigResult(None, False, True)
    # Persist the raw config verbatim; hand callers the normalized manifest they expect.
    save_managed_state(workspace, raw)
    return ManagedConfigResult(normalize_managed_config(raw), False, False)


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
