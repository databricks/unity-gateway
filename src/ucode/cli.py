#!/usr/bin/env python3
"""CLI entry point for ``ug``."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from importlib import metadata
from typing import Annotated, Any

import typer
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from typer import _click
from typer.core import HAS_RICH, TyperCommand, TyperGroup, TyperOption

from ucode import custom_oauth
from ucode.agents import (
    TOOL_SPECS,
    LaunchOptions,
    check_gateway_endpoint,
    configure_selected_tools,
    configure_single_tool,
    configure_tool,
    ensure_bootstrap_dependencies,
    ensure_provider_state,
    explicit_model_arg_value,
    install_databricks_ai_tools_for_agents,
    install_tool_binary,
    normalize_tool,
    resolve_gemini_provider_model,
    resolve_launch_model,
    resolve_provider_models,
)
from ucode.agents import claude as claude_agent
from ucode.agents import codex as codex_agent
from ucode.agents import (
    launch as launch_agent,
)
from ucode.agents.args import has_explicit_model_arg
from ucode.agents.codex import revert_legacy_shared_config
from ucode.agents.pi import PI_SETTINGS_BACKUP_PATH, PI_SETTINGS_PATH
from ucode.config_io import is_dry_run, restore_file, set_dry_run
from ucode.custom_oauth import (
    CUSTOM_OAUTH_CLI_ENV_VAR,
    custom_oauth_cli_enabled,
    ensure_custom_oauth_cli_token,
)
from ucode.databricks import (
    SKILLS_MCP_MIN_DATABRICKS_CLI_VERSION,
    apply_pat_environment,
    build_shared_base_urls,
    discover_claude_models,
    discover_codex_models,
    discover_gemini_models,
    discover_model_services,
    ensure_databricks_auth,
    ensure_pat_bearer,
    external_bearer_configured,
    find_profile_name_for_host,
    get_databricks_profiles,
    get_databricks_token,
    install_databricks_cli,
    is_model_provider_feature_unavailable,
    list_anthropic_model_catalog,
    list_profile_entries,
    list_tool_provider_services,
    normalize_workspace_url,
    probe_unity_gateway_capabilities,
    resolve_pat_token,
    resolve_provider_launch_model,
    run_databricks_login,
)
from ucode.managed_budget import (
    budget_usage_percent,
    recommendation_line,
    render_budget_panel,
)
from ucode.managed_config import (
    ManagedConfigResult,
    get_managed_config,
    get_model_recommendation,
    load_managed_state,
    normalize_managed_config,
    refresh_managed_config,
)
from ucode.managed_resolve import (
    managed_claude_family_models,
    managed_default_model,
    managed_enabled_tools,
    managed_launch_model,
    managed_provider_family_models,
    managed_provider_service,
    managed_supplies_models,
    managed_unity_catalog_location,
    managed_unservable_models,
    recommended_agent,
    resolve_state,
)
from ucode.mcp import (
    MCP_CLIENTS,
    SKILLS_MCP_KIND,
    McpServiceListingRateLimited,
    add_mcp_command,
    add_skills_command,
    available_mcp_clients,
    configure_bare_skills_mcp_command,
    configure_mcp_command,
    configure_skills_mcp_picker_command,
    configured_mcp_clients,
    list_mcp_command,
    purge_cross_workspace_mcp_residue,
    reconcile_managed_mcp_servers,
    remove_mcp_command,
    remove_skills_command,
    remove_skills_locations_command,
    revert_mcp_configs,
)
from ucode.skills_download import (
    configure_location_skills_download_command,
    configure_selected_skills_download_command,
    configure_skills_download_picker_command,
    reconcile_managed_skills,
    remove_downloaded_skills_command,
)
from ucode.skills_list import configured_skill_counts_by_agent, list_configured_skills_command
from ucode.skills_state import records_for_scope
from ucode.smart_routing import v2 as smart_routing_v2
from ucode.smart_routing.claude_hooks import FIRST_PROMPT_SOCKET_ENV, ROUTE_FIRST_PROMPT_EVENT
from ucode.state import (
    clear_state,
    get_provider_service,
    load_state,
    save_state,
    set_current_workspace,
    set_provider_service,
)
from ucode.string_utils import is_valid_catalog_schema
from ucode.ui import (
    console,
    heading,
    print_err,
    print_heading,
    print_kv,
    print_note,
    print_section,
    print_success,
    print_warning,
    prompt_for_selection,
    prompt_for_tools,
    prompt_for_workspace,
    prompt_yes_no,
    redirect_output_to_stderr,
    set_verbosity,
    spinner,
    status_badge,
)
from ucode.usage import usage as usage_report

CustomOAuthConfig = custom_oauth.CustomOAuthConfig

_DISCOVERY_CONSUMERS: dict[str, tuple[str, ...]] = {
    "claude": ("claude", "opencode", "copilot", "pi"),
    "codex": ("codex", "copilot", "pi"),
    "gemini": ("gemini", "opencode", "pi"),
    "oss": ("opencode",),
}


def _policy_summary_lines(managed: dict) -> list[str]:
    """Rich-markup lines describing the admin's tiered spend policy, or empty when it sets none."""
    policy = managed.get("budget_policy")
    if not isinstance(policy, dict):
        return []
    name = str(policy.get("display_name") or "coding-agents-default")
    lines = [f"[bold]Policy:[/bold] [cyan]{name}[/cyan]"]
    tiers = policy.get("tiers")
    for tier in tiers if isinstance(tiers, list) else []:
        if not isinstance(tier, dict):
            continue
        pct_raw = tier.get("spending_percentage")
        pct = (
            f"{float(pct_raw) * 100:g}%"
            if isinstance(pct_raw, int | float) and not isinstance(pct_raw, bool)
            else "?"
        )
        # A tier whose agent enum this build doesn't know is dropped during normalization, so it
        # arrives unset rather than as a tool name TOOL_SPECS could resolve.
        agent = tier.get("default_agent")
        agent_display = TOOL_SPECS[agent]["display"] if agent in TOOL_SPECS else "?"
        model = str(tier.get("default_model") or "?")
        lines.append(
            f"  [dim]·[/dim] [bold]at {pct}[/bold] → {agent_display} · [magenta]{model}[/magenta]"
        )
    return lines


def _configured_summary(names: list[str], *, limit: int = 5) -> str:
    """Format a resolved MCP/skill set for the Configuration panel as ``N (name, ..., name)``.

    Empty renders "none configured": the admin set none, or a fetch failed and warned above. At most
    ``limit`` names are shown, with the rest collapsed to an ellipsis so a large set stays scannable.
    """
    unique = sorted({name for name in names if name})
    if not unique:
        return "[dim]none configured[/dim]"
    shown = unique[:limit]
    if len(unique) > limit:
        shown.append("...")
    return f"{len(unique)} ({', '.join(shown)})"


def _print_managed_summary(
    managed: dict,
    state: dict,
    tool: str | None,
    *,
    abridged: bool = False,
    configured_tools: list[str] | None = None,
    registered_mcps: list[str] | None = None,
) -> None:
    """Show which of the admin's settings are in force.

    With ``tool`` set (launch path) the per-agent Agent/Provider/Model lines are included;
    with ``tool=None`` (e.g. ``ug configure`` under a managed config) they are skipped
    since no single agent has been chosen yet.

    ``abridged`` prints only what changes launch-to-launch — the agent and model this run will use,
    and the policy in force — with a pointer to ``ug status`` for the rest. Bare ``ucode`` runs
    every session, so re-enumerating the workspace's full MCP/skills/tier list each time is noise;
    the full box stays for ``status`` and ``configure``, where the reader asked to see it.
    """
    if abridged:
        _print_managed_summary_abridged(managed, state, tool)
        return
    lines = [f"[bold]Workspace:[/bold] [cyan]{state.get('workspace', '?')}[/cyan]"]
    if tool is not None:
        lines.append(f"[bold]Agent:[/bold] [green]{TOOL_SPECS[tool]['display']}[/green]")
    enabled = [t for t in (managed.get("enabled_agents") or {}) if t in TOOL_SPECS]
    failed: list[str] = []
    if configured_tools is not None:
        failed = [t for t in enabled if t not in configured_tools]
        enabled = [t for t in enabled if t in configured_tools]
    if enabled:
        lines.append(
            f"[bold]Coding Agents:[/bold] {', '.join(TOOL_SPECS[t]['display'] for t in enabled)}"
        )
    if failed:
        lines.append(
            f"[bold]Failed to configure:[/bold] "
            f"[yellow]{', '.join(TOOL_SPECS[t]['display'] for t in failed)}[/yellow]"
        )
    if tool is not None:
        provider = managed_provider_service(managed, tool)
        if provider:
            lines.append(f"[bold]Provider:[/bold] [magenta]{provider}[/magenta]")
        model = managed_default_model(managed, tool)
        if model:
            lines.append(f"[bold]Model:[/bold] [magenta]{model}[/magenta]")
    # Count what ug actually registered/downloaded, not the admin's raw selector (a UC location is
    # just a pointer with no count): the MCP servers reconcile registered this run, and the managed
    # skills on disk. State can't stand in for the MCPs — #717 writes them to OS-managed files.
    lines.append(f"[bold]MCPs:[/bold] {_configured_summary(registered_mcps or [])}")
    skill_names = [
        str(record.get("bundle_name"))
        for record in records_for_scope("managed")
        if record.get("bundle_name")
    ]
    lines.append(f"[bold]Skills:[/bold] {_configured_summary(skill_names)}")
    lines.extend(_policy_summary_lines(managed))
    console.print(Panel("\n".join(lines), title="Configuration", style="green", expand=False))


def _print_managed_summary_abridged(managed: dict, state: dict, tool: str | None) -> None:
    """One-line launch banner: which agent (and model) this managed run is launching.

    Bare ``ucode`` runs every session, so the full box's MCP/skills/policy enumeration is noise
    each time; ``ug status`` still shows all of it. See ``_print_managed_summary``'s ``abridged``
    note. ``tool`` is always set on the launch path, but is guarded for callers that pass None."""
    if tool is None:
        print_note("Using managed config.")
        return
    agent = TOOL_SPECS[tool]["display"]
    model = managed_default_model(managed, tool)
    model_suffix = f" with [magenta]{model}[/magenta]" if model else ""
    # "as the default agent" only when this really is the config's default: a budget tier can
    # override the default and launch a different agent, and the tier note in `_launch_tool` already
    # explains that case — so claiming "default" here would contradict it.
    role = " as the default agent" if tool == managed.get("default_agent") else ""
    console.print(
        f"[dim]•[/dim] Using managed config — launching [green]{agent}[/green]{role}{model_suffix}"
    )


def _summarize_managed_config(
    managed: dict, configured_tools: list[str], registered_mcps: list[str]
) -> None:
    """Show the resulting managed setup, listing only the agents that configured cleanly."""
    _print_managed_summary(
        managed,
        load_state(),
        tool=None,
        configured_tools=configured_tools,
        registered_mcps=registered_mcps,
    )
    print_success("Configuration complete — launch with [bold cyan]ug[/bold cyan].")


def _print_discovery_diagnostics(state: dict) -> None:
    """Surface per-source reasons after a failed discovery so the user knows
    which API call returned what — instead of the generic 'no agents' line."""
    reasons = state.get("_discovery_reasons") or {}
    if not reasons:
        return
    labels = {
        "claude": "Claude models",
        "codex": "Codex models",
        "gemini": "Gemini models",
        "oss": "OSS models",
    }
    for source, reason in reasons.items():
        consumers = ", ".join(_DISCOVERY_CONSUMERS.get(source, ()))
        label = labels.get(source, source)
        if reason:
            print_note(f"{label} (needed for: {consumers}): {reason}")
        else:
            print_note(f"{label} (needed for: {consumers}): no models returned")
    print_note("Re-run with `UCODE_DEBUG=1` to log raw discovery responses to ~/.ucode/debug.log.")


def _prompt_for_configuration(tool: str | None = None) -> tuple[str, str | None]:
    if tool is None:
        desc = "Configure your Databricks workspace"
    else:
        desc = f"Configure {TOOL_SPECS[tool]['display']} to use your Databricks endpoint."
    with spinner("Loading Databricks workspaces and profiles..."):
        profiles = get_databricks_profiles()
    return prompt_for_workspace(desc, profiles)


def _custom_oauth_config(
    client_id: str | None,
    redirect_url: str | None,
    scopes: str | None,
) -> CustomOAuthConfig | None:
    if client_id is None and redirect_url is None and scopes is None:
        return None
    if client_id is None:
        raise RuntimeError("--redirect-url and --scopes require --client-id.")
    if scopes is None:
        if not custom_oauth_cli_enabled(client_id):
            raise RuntimeError("--scopes is required with --client-id.")
        scopes = ",".join(custom_oauth.DEFAULT_CLI_SCOPES)

    return custom_oauth.create_custom_oauth_config(
        client_id,
        scopes.split(","),
        redirect_url or custom_oauth.DEFAULT_REDIRECT_URL,
    )


def _parse_agents_option(agents: str) -> list[str]:
    tools: list[str] = []
    for raw_tool in agents.split(","):
        raw_tool = raw_tool.strip()
        if not raw_tool:
            continue
        tool = normalize_tool(raw_tool)
        if tool not in tools:
            tools.append(tool)
    if not tools:
        raise RuntimeError(
            "No agents provided for --agents. Use a comma-separated list like `--agents claude,codex`."
        )
    return tools


def _parse_skill_locations(location: str | None) -> list[str]:
    """Parse a comma-separated `--location` into `<catalog>.<schema>` refs,
    dropping duplicates while preserving order. `None`/empty yields `[]` (the
    schema-less, utility-tools-only connection)."""
    locations: list[str] = []
    for raw in (location or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(".")
        if len(parts) != 2 or not all(part.strip() for part in parts):
            raise RuntimeError(f"--location entries must be `<catalog>.<schema>`, got `{raw}`.")
        if raw not in locations:
            locations.append(raw)
    return locations


def _is_qualified_skill_name(name: str) -> bool:
    """True if `name` is a 3-part `<catalog>.<schema>.<name>` FQN with non-blank parts."""
    parts = name.split(".")
    return len(parts) == 3 and all(part and part == part.strip() for part in parts)


def _parse_workspace_option(workspace: str) -> list[tuple[str, str | None]]:
    """Parse `--workspace` into a single-element [(url, None)] entry.

    `--workspace` supplies one bare URL; the matching profile (if any) is
    resolved later via `find_profile_name_for_host`. The single entry is wrapped
    in a list so it flows through the same plumbing as `--profile`.
    """
    if "," in workspace:
        raise RuntimeError(
            "--workspace takes a single workspace URL, e.g. "
            "`--workspace https://workspace.databricks.com`."
        )
    try:
        url = normalize_workspace_url(workspace)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    return [(url, None)]


def _parse_profile_option(profile: str) -> list[tuple[str, str | None]]:
    """Parse `--profile` into a single-element [(url, profile_name)] entry.

    The name must be an existing Databricks CLI profile; its host supplies the
    workspace URL. Auth behaves the same as `--workspace`: OAuth login is forced
    unless `--use-pat` is also passed. The single entry is wrapped in a list so
    it flows through the same plumbing as `--workspace`.
    """
    name = profile.strip()
    if "," in name:
        raise RuntimeError(
            "--profile takes a single Databricks CLI profile, e.g. `--profile DEFAULT`."
        )
    available = {str(p.get("name")): p for p in list_profile_entries() if p.get("name")}
    entry = available.get(name)
    if entry is None:
        known = ", ".join(sorted(available)) or "none"
        raise RuntimeError(
            f"Databricks CLI profile '{name}' was not found (available: {known}). "
            "Check `databricks auth profiles` or add the profile to ~/.databrickscfg."
        )
    host = str(entry.get("host") or "").strip()
    if not host:
        raise RuntimeError(
            f"Databricks CLI profile '{name}' has no host configured in ~/.databrickscfg."
        )
    try:
        workspace = normalize_workspace_url(host)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    return [(workspace, name)]


def configure_shared_state(
    workspace: str,
    profile: str | None = None,
    tools: list[str] | None = None,
    force_login: bool = False,
    use_pat: bool | None = None,
    skip_model_discovery: bool = False,
    skip_preflight: bool = False,
    databricks_ai_tools_enabled: bool | None = None,
    custom_oauth: CustomOAuthConfig | None = None,
    clear_custom_oauth: bool = False,
) -> dict:
    """Log into Databricks, verify AI Gateway, fetch model lists, persist state.

    If tools is provided, only fetch models for those tools. Otherwise fetch all.
    If force_login is True, always run databricks auth login (used by explicit configure).
    If use_pat is True (explicit `configure --profile <name> --use-pat`), the
    profile's personal access token from ~/.databrickscfg is used instead of
    OAuth and no interactive login ever runs. ``None`` means "inherit": a
    launch re-run keeps the mode the workspace was configured with.
    ``profile`` is the Databricks CLI profile name to address — passed via
    ``--profile`` to every CLI invocation so ambiguous `~/.databrickscfg`
    entries (e.g. DEFAULT and a named profile both pointing at the same host)
    don't error out. If ``None``, we resolve it from the host after login.
    If skip_preflight is True, skip the entire preflight block below — auth
    validation, the AI Gateway probe, and model discovery — trusting a prior
    ``ug configure``. The PAT/bearer is already exported (``apply_pat_environment``
    in ``_launch_tool``) and the gateway was verified by that earlier configure.
    Only the local profile resolution and the shared state assembly still run;
    the saved model lists are preserved.
    """
    workspace = normalize_workspace_url(workspace)
    prior_state = load_state()
    previous_workspace = prior_state.get("workspace")
    if use_pat is None:
        use_pat = bool(prior_state.get("use_pat")) and previous_workspace == workspace
    if databricks_ai_tools_enabled is None:
        # Opt-in: a True from an opt-out-era configure is a stale default, not a
        # standing opt-in, so it is not carried forward.
        databricks_ai_tools_enabled = False
    fetch_all = tools is None

    # Assemble the shared workspace state that doesn't depend on model discovery:
    # workspace, profile, auth mode, base URLs. `profile` may still be None here;
    # each path below resolves it once, where a host->profile lookup is reliable
    # (the skip branch trusts the prior configure; the preflight resolves after
    # login). --skip-preflight persists exactly this and returns, trusting a prior
    # `ug configure` — it already validated auth + the AI Gateway and saved the
    # model lists (carried over by load_state, left untouched).
    state = load_state()
    state["workspace"] = workspace
    if profile:
        state["profile"] = profile
    else:
        state.pop("profile", None)
    # UC discovery is now always-on; drop any flag persisted by older versions.
    state.pop("uc_enabled", None)
    # Persist the auth mode so launches rebuild the same (PAT-based) agent
    # auth command; an explicit re-configure without --use-pat clears it.
    if use_pat:
        state["use_pat"] = True
    else:
        state.pop("use_pat", None)
    # Fable follows model discovery; discard the legacy opt-in.
    state.pop("fable_enabled", None)
    state["databricks_ai_tools_enabled"] = databricks_ai_tools_enabled
    if clear_custom_oauth:
        state.pop("custom_oauth", None)
    elif custom_oauth is not None:
        state["custom_oauth"] = dict(custom_oauth)
    elif previous_workspace != workspace:
        state.pop("custom_oauth", None)
    state["base_urls"] = build_shared_base_urls(workspace)

    cli_custom_oauth = state.get("custom_oauth") if custom_oauth_cli_enabled(custom_oauth) else None
    if cli_custom_oauth:
        token = ensure_custom_oauth_cli_token(workspace, cli_custom_oauth)

    if skip_preflight:
        # A prior `ug configure` created the profile; resolve it locally (no
        # login needed) and persist it so launches disambiguate.
        if profile is None:
            profile = find_profile_name_for_host(workspace)
            if profile:
                state["profile"] = profile
        save_state(state)
        # Scrub MCP entries ucode wrote for a previous workspace.
        if previous_workspace and previous_workspace != workspace:
            purge_cross_workspace_mcp_residue(state, workspace)
        # Diagnostic reasons are transient (attached after save_state so they
        # don't land on disk). No discovery ran, so there is nothing to report.
        state["_discovery_reasons"] = {"claude": None, "gemini": None, "codex": None, "oss": None}
        return state

    # ── Preflight (bypassed above under --skip-preflight): validate Databricks
    #    auth + the AI Gateway, then discover the available models. ──
    if cli_custom_oauth:
        pass  # The dedicated profile was authenticated above.
    elif use_pat:
        if not profile:
            raise RuntimeError(
                "--use-pat requires a Databricks CLI profile. Pass one via `--profile <name>`."
            )
        pat = resolve_pat_token(profile)
        if not pat:
            raise RuntimeError(
                f"--use-pat: profile '{profile}' has no personal access token in "
                "~/.databrickscfg (its auth_type must be `pat`). Add a `token = <PAT>` "
                f"entry under [{profile}], or re-run without --use-pat to use OAuth."
            )
        # Export the PAT for this process and launched agent subprocesses so
        # every token fetch takes the static-bearer path. ensure_pat_bearer
        # keeps a non-empty pre-set bearer (CI escape hatch) but treats an
        # empty one as absent, so it never shadows the PAT. Pass the validated
        # token to avoid re-reading ~/.databrickscfg.
        ensure_pat_bearer(profile, pat)
        ensure_databricks_auth(workspace, profile)
    elif force_login and not external_bearer_configured():
        run_databricks_login(workspace, profile)
    else:
        ensure_databricks_auth(workspace, profile)
    # After login the profile exists in ~/.databrickscfg, so a host->profile
    # lookup is reliable even when it returned nothing above.
    if profile is None:
        profile = find_profile_name_for_host(workspace)
        if profile:
            state["profile"] = profile
    with spinner("Verifying Unity AI Gateway..."):
        if not cli_custom_oauth:
            token = get_databricks_token(workspace, profile)
        model_service_probe = probe_unity_gateway_capabilities(workspace, token)
    if model_service_probe.resource_available:
        print_success("Unity Gateway connected")
    else:
        print_warning(f"Model service: {model_service_probe.detail}")

    want_claude = (
        fetch_all or "claude" in tools or "opencode" in tools or "copilot" in tools or "pi" in tools
    )
    want_gemini = fetch_all or "gemini" in tools or "opencode" in tools or "pi" in tools
    want_codex = fetch_all or "codex" in tools or "copilot" in tools or "pi" in tools
    # Codex smart routing can select OSS models such as GLM, so a Codex-only
    # configure must persist that discovered family too.
    want_oss = fetch_all or "opencode" in tools or "codex" in tools

    claude_reason: str | None = None
    gemini_reason: str | None = None
    codex_reason: str | None = None
    oss_reason: str | None = None
    claude_models = {}
    gemini_models = []
    codex_models = []
    oss_models = []
    opencode_models: dict[str, list[str]] = {}
    web_search_model: str | None = None
    if skip_model_discovery:
        # Provider mode: the agent routes through a Model Provider Service and
        # pins no Databricks model, so the full family discovery is unused. Web
        # search (claude only) still needs one Responses-capable model, so fetch
        # just that with a single call.
        if want_claude:
            with spinner("Fetching available models..."):
                ws_models, _ = discover_codex_models(workspace, token)
            if ws_models:
                web_search_model = ws_models[0]
    else:
        # UC-first, best-effort: one UC model-services call yields all families
        # as `system.ai.<model-name>` ids, bucketed by name. If a family comes
        # back empty (workspace without UC model-services, or the listing
        # failed), fall back to the per-family AI Gateway listing for that
        # family only.
        with spinner("Fetching available models..."):
            ms_claude, ms_codex, ms_gemini, ms_oss, ms_reason = discover_model_services(
                workspace, token
            )
            if want_claude:
                claude_models, claude_reason = ms_claude, ms_reason
                if not claude_models:
                    claude_models, claude_reason = discover_claude_models(workspace, token)
            if want_gemini:
                gemini_models, gemini_reason = ms_gemini, ms_reason
                if not gemini_models:
                    gemini_models, gemini_reason = discover_gemini_models(workspace, token)
            if want_codex:
                codex_models, codex_reason = ms_codex, ms_reason
                if not codex_models:
                    codex_models, codex_reason = discover_codex_models(workspace, token)
            if want_oss:
                oss_models, oss_reason = ms_oss, ms_reason
        if claude_models:
            opencode_models["anthropic"] = list(claude_models.values())
        if gemini_models:
            opencode_models["gemini"] = gemini_models
        if oss_models:
            opencode_models["oss"] = oss_models

    if skip_model_discovery:
        # Don't clobber any previously-discovered Databricks model lists; provider
        # mode just doesn't refresh or use them. Persist the web-search model so
        # claude's web_search MCP keeps working through the normal gateway.
        if web_search_model:
            state["web_search_model"] = web_search_model
    else:
        if want_claude:
            state["claude_models"] = claude_models
        if want_gemini:
            state["gemini_models"] = gemini_models
        if want_codex:
            state["codex_models"] = codex_models
        if want_oss:
            state["oss_models"] = oss_models
        if fetch_all or "opencode" in tools:
            state["opencode_models"] = opencode_models
    save_state(state)
    # Scrub MCP entries that ucode wrote for the previous workspace so the new
    # workspace's agent configs aren't stale.
    if previous_workspace and previous_workspace != workspace:
        purge_cross_workspace_mcp_residue(state, workspace)
    # Diagnostic reasons are transient — attach after save_state so they don't
    # land on disk but are available to the caller for this run.
    state["_discovery_reasons"] = {
        "claude": claude_reason,
        "gemini": gemini_reason,
        "codex": codex_reason,
        "oss": oss_reason,
    }
    return state


def _configure_shared_workspace_states(
    workspaces: list[tuple[str, str | None]],
    tools: list[str] | None,
    *,
    force_login: bool,
    use_pat: bool = False,
    databricks_ai_tools_enabled: bool | None = None,
    custom_oauth: CustomOAuthConfig | None = None,
    clear_custom_oauth: bool = False,
) -> list[dict]:
    if len(workspaces) != 1:
        raise RuntimeError(f"Expected exactly one workspace, got {len(workspaces)}.")
    states: list[dict] = []
    for workspace, profile in workspaces:
        custom_oauth_kwargs = {"custom_oauth": custom_oauth} if custom_oauth is not None else {}
        if clear_custom_oauth:
            custom_oauth_kwargs["clear_custom_oauth"] = True
        states.append(
            configure_shared_state(
                workspace,
                profile=profile,
                tools=tools,
                force_login=force_login,
                use_pat=use_pat,
                databricks_ai_tools_enabled=databricks_ai_tools_enabled,
                **custom_oauth_kwargs,
            )
        )
    return states


def _provider_summary(tool: str, state: dict) -> str:
    """Short label for the Configuration Complete box: 'Databricks' when no
    Model Provider Service is configured, otherwise the external provider type
    backing this tool (claude routes to Anthropic, codex to OpenAI)."""
    if not get_provider_service(state, tool):
        return "Databricks"
    return {"claude": "Anthropic", "codex": "OpenAI"}.get(tool, "Model Provider Service")


def _maybe_select_provider_service(tool: str, state: dict) -> dict:
    """Interactively let the user route claude/codex through a Model Provider
    Service instead of Databricks models, and persist (or clear) the choice.

    No-op for tools other than claude/codex/gemini. Falls back to Databricks when no
    matching provider services are found or the listing fails.
    """
    if tool not in ("claude", "codex", "gemini"):
        return state
    display = TOOL_SPECS[tool]["display"]

    def _use_databricks() -> dict:
        new_state = set_provider_service(state, tool, None)
        save_state(new_state)
        return new_state

    # Probe first so we only offer the picker when it's actually usable. The
    # interactive path always reaches here, so explain any fallback rather than
    # silently dropping back to Databricks.
    token = get_databricks_token(state["workspace"], state.get("profile"))
    with spinner("Checking for model provider services..."):
        names, reason = list_tool_provider_services(tool, state["workspace"], token)
    if reason is not None:
        # Most workspaces don't have the feature enabled — that's the common case,
        # so fall back to Databricks silently. Only surface unexpected failures.
        if not is_model_provider_feature_unavailable(reason):
            print_warning(f"Could not list model provider services: {reason}")
            print_note("Falling back to Databricks models.")
        return _use_databricks()
    if not names:
        # Feature is on but no service matches this tool's provider type.
        print_note(f"Using Databricks models for {display}.")
        return _use_databricks()

    choice = prompt_for_selection(
        f"How should {display} get its models?",
        [
            ("databricks", "Databricks Hosted"),
            ("mps", "External Models"),
        ],
    )
    if choice is None:
        raise KeyboardInterrupt
    if choice == "databricks":
        return _use_databricks()

    selected = prompt_for_selection(
        "Select a model provider service:", [(name, name) for name in names]
    )
    if selected is None:
        raise KeyboardInterrupt
    state = set_provider_service(state, tool, selected)
    save_state(state)
    print_success(f"{display} will route through {selected}")
    return state


def configure_workspace_command(
    tool: str | None = None,
    selected_tools: list[str] | None = None,
    workspaces: list[tuple[str, str | None]] | None = None,
    *,
    use_pat: bool = False,
    databricks_ai_tools_enabled: bool | None = None,
    custom_oauth: CustomOAuthConfig | None = None,
    offer_optional_setup: bool = False,
) -> int:
    if tool is not None and selected_tools is not None:
        raise RuntimeError("Use either --agent or --agents, not both.")

    # The Databricks-vs-Model-Provider-Service picker is shown only on the fully
    # interactive path (`ug configure` with no --agent/--agents). Naming agents
    # explicitly signals the non-interactive flow, which stays on Databricks.
    offer_provider = tool is None and selected_tools is None

    workspace_entries = workspaces or [_prompt_for_configuration(tool)]

    if tool is not None:
        states = _configure_shared_workspace_states(
            workspace_entries,
            [tool],
            force_login=True,
            use_pat=use_pat,
            databricks_ai_tools_enabled=databricks_ai_tools_enabled,
            custom_oauth=custom_oauth,
            clear_custom_oauth=custom_oauth is None,
        )
        state = states[0]
        parent_schema = None
        if tool in ("claude", "codex"):
            managed, _ = refresh_managed_config(state, force_refresh=True)
            _reject_disabled_agent(managed, tool)
            if managed is not None:
                state = resolve_state(managed, state, tool)
                if not managed_provider_service(managed, tool):
                    parent_schema = managed_unity_catalog_location(managed, tool)
        state = configure_single_tool(tool, state, parent_schema=parent_schema)
        install_databricks_ai_tools_for_agents(
            [tool], state, force_refresh=tool not in ("claude", "codex")
        )
        spec = TOOL_SPECS[tool]
        provider_summary = "Databricks" if parent_schema else _provider_summary(tool, state)
        console.print(
            Panel(
                f"[bold]Workspace:[/bold] [cyan]{state['workspace']}[/cyan]\n"
                f"[bold]{spec['display']}:[/bold] [green]configured[/green] "
                f"[dim](Provider: {provider_summary})[/dim]",
                title="Configuration Complete",
                style="green",
                expand=False,
            )
        )
        return 0

    states = _configure_shared_workspace_states(
        workspace_entries,
        selected_tools,
        force_login=True,
        use_pat=use_pat,
        databricks_ai_tools_enabled=databricks_ai_tools_enabled,
        custom_oauth=custom_oauth,
        clear_custom_oauth=custom_oauth is None,
    )
    state = states[0]
    save_state(state)

    # A published managed config means the admin dictates the setup: apply it to every enabled agent
    # now rather than prompting the developer to pick. Configure always reads fresh so it never
    # applies a config the admin has since changed.
    managed, _ = refresh_managed_config(state, force_refresh=True)
    managed_tools = managed_enabled_tools(managed) if managed is not None else []
    if managed is not None and managed_tools:
        configured_tools: list[str] = []
        for tool_name in managed_tools:
            resolved = resolve_state(managed, state, tool_name)
            parent_schema = (
                managed_unity_catalog_location(managed, tool_name)
                if tool_name in ("claude", "codex")
                and not managed_provider_service(managed, tool_name)
                else None
            )
            if (
                get_provider_service(resolved, tool_name)
                or parent_schema
                or check_gateway_endpoint(resolved, tool_name)
            ):
                if not install_tool_binary(tool_name, strict=False):
                    continue
                configured = configure_selected_tools(
                    resolved,
                    [tool_name],
                    install_ai_tools=not is_dry_run(),
                    parent_schemas={tool_name: parent_schema} if parent_schema else None,
                )
                # Each iteration resolves from `state` and persists a copy, so carry the
                # accumulated available_tools forward — otherwise the last agent's save drops
                # the earlier ones, and the MCP reconcile below only sees that final agent.
                state["available_tools"] = configured.get("available_tools") or state.get(
                    "available_tools"
                )
                # available_tools is cumulative across runs, so an agent that failed
                # this run may still be in it from a prior success. Trust the per-run
                # signal when present; fall back to "configured" only when it's absent.
                last = configured.get("last_configured_tools")
                if last is None or tool_name in last:
                    configured_tools.append(tool_name)
        if not configured_tools:
            raise RuntimeError(
                "None of the coding agents enabled by your workspace configuration "
                "are available on this workspace."
            )
        registered_mcps: list[str] = []
        if not is_dry_run():
            registered_mcps = _configure_managed_mcp_servers(managed)
            _configure_managed_skills(managed)
        _summarize_managed_config(managed, configured_tools, registered_mcps)
        return 0

    available_on_workspace: list[str] = []
    tools_to_check = selected_tools or list(TOOL_SPECS)
    for tool_name in tools_to_check:
        with spinner(f"Checking {TOOL_SPECS[tool_name]['display']} availability..."):
            if check_gateway_endpoint(state, tool_name):
                available_on_workspace.append(tool_name)

    if not available_on_workspace:
        _print_discovery_diagnostics(state)
        raise RuntimeError("No coding agents are available on this workspace.")

    if selected_tools is None:
        picked = prompt_for_tools([(t, TOOL_SPECS[t]["display"]) for t in available_on_workspace])
    else:
        unavailable_tools = [
            tool_name for tool_name in selected_tools if tool_name not in available_on_workspace
        ]
        if unavailable_tools:
            _print_discovery_diagnostics(state)
            displays = ", ".join(
                TOOL_SPECS[tool_name]["display"] for tool_name in unavailable_tools
            )
            print_warning(f"Skipping agent(s) not available on this workspace: {displays}.")
        picked = [tool_name for tool_name in selected_tools if tool_name in available_on_workspace]

    if not picked:
        print_note("No coding agents selected — nothing to configure.")
        return 0

    for tool_name in picked:
        install_tool_binary(
            tool_name,
            strict=False,
        )

    # Offer the provider picker for the chosen claude/codex tools only on the
    # interactive path (no --agents); otherwise stay on the Databricks path.
    if offer_provider:
        for tool_name in picked:
            state = _maybe_select_provider_service(tool_name, state)

    if offer_optional_setup:
        state = configure_selected_tools(state, picked, install_ai_tools=False)
    else:
        state = configure_selected_tools(state, picked)

    # No managed config here: undo what a prior managed workspace left behind (its MCP servers and
    # skills), so switching workspaces doesn't strand the old registry and skills.
    if not is_dry_run():
        _configure_managed_mcp_servers(None)
        _configure_managed_skills(None)

    # Prefer this run's outcome; available_tools is cumulative and can still list a
    # tool that failed this run from an earlier success. Fall back to it only when the
    # per-run signal is absent (e.g. a stubbed configure_selected_tools).
    last_configured = state.get("last_configured_tools")
    configured_set = set(
        last_configured if last_configured is not None else state.get("available_tools") or []
    )
    summary_lines = [f"[bold]Workspace:[/bold] [cyan]{state['workspace']}[/cyan]"]
    for tool_name in picked:
        spec = TOOL_SPECS[tool_name]
        if tool_name in configured_set:
            summary_lines.append(
                f"[bold]{spec['display']}:[/bold] [green]configured[/green] "
                f"[dim](Provider: {_provider_summary(tool_name, state)})[/dim]"
            )
        else:
            summary_lines.append(
                f"[bold]{spec['display']}:[/bold] [yellow]not configured "
                "(see warnings above)[/yellow]"
            )
    console.print(
        Panel(
            "\n".join(summary_lines),
            title="Configuration Complete",
            style="green",
            expand=False,
        )
    )
    if offer_optional_setup and not is_dry_run():
        _configure_optional_setup(state, picked)
    return 0


def _print_status_panel(title: str, rows: list[tuple[str, str]]) -> None:
    """Render a compact two-column status card with values that wrap safely."""
    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True)
    table.add_column()
    for key, value in rows:
        table.add_row(Text(f"{key}:", style="bold"), Text(value, style="cyan"))
    console.print(
        Panel(
            table,
            title=title,
            border_style="blue",
            width=min(console.width, 120),
        )
    )


def _model_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    if isinstance(value, dict):
        return [model for models in value.values() for model in _model_values(models)]
    return []


def _status_models(tool: str, state: dict) -> list[str]:
    """Return the effective model allow-list for one configured agent."""
    static_models = _model_values(state.get(f"{tool}_static_models"))
    if static_models:
        models = static_models
    elif tool in ("claude", "codex", "gemini", "opencode"):
        models = _model_values(state.get(f"{tool}_models"))
    elif tool == "copilot":
        models = _model_values(state.get("copilot_models")) or (
            _model_values(state.get("claude_models")) + _model_values(state.get("codex_models"))
        )
    elif tool == "pi":
        models = _model_values(state.get("pi_models")) or (
            _model_values(state.get("claude_models"))
            + _model_values(state.get("codex_models"))
            + _model_values(state.get("gemini_models"))
        )
    else:
        models = []
    return list(dict.fromkeys(models))


def _status_default_model(tool: str, state: dict, models: list[str]) -> str | None:
    explicit = state.get(f"{tool}_default_model")
    if isinstance(explicit, str) and explicit:
        return explicit
    # Claude and Codex deliberately leave the starting model to the agent unless a managed
    # config pins one. The other clients write the first resolved model into their ug config.
    return models[0] if models and tool in ("gemini", "opencode", "copilot", "pi") else None


def _live_status_model_state(state: dict, tools: set[str]) -> tuple[dict, str]:
    """Return a fresh, read-only model inventory and its freshness label."""
    workspace = state.get("workspace")
    if not workspace or not tools:
        return state, "cached"
    profile = state.get("profile")
    if not profile and not external_bearer_configured():
        print_warning("Live model discovery needs the CLI profile saved by ug configure.")
        return state, "cached"

    try:
        if state.get("use_pat"):
            apply_pat_environment(state)
        with spinner("Refreshing live workspace models..."):
            token = get_databricks_token(workspace, profile)
            claude_models, codex_models, gemini_models, oss_models, shared_reason = (
                discover_model_services(workspace, token)
            )
            reasons: dict[str, str | None] = {}
            if not claude_models:
                claude_models, reasons["claude"] = discover_claude_models(workspace, token)
            if not codex_models:
                codex_models, reasons["codex"] = discover_codex_models(workspace, token)
            if not gemini_models:
                gemini_models, reasons["gemini"] = discover_gemini_models(workspace, token)
    except RuntimeError as exc:
        print_warning(f"Live model discovery failed ({exc}); showing cached models.")
        return state, "cached"

    live = dict(state)
    live["claude_models"] = claude_models
    live["codex_models"] = codex_models
    live["gemini_models"] = gemini_models
    live["oss_models"] = oss_models
    opencode_models: dict[str, list[str]] = {}
    if claude_models:
        opencode_models["anthropic"] = list(claude_models.values())
    if gemini_models:
        opencode_models["gemini"] = gemini_models
    if oss_models:
        opencode_models["oss"] = oss_models
    live["opencode_models"] = opencode_models
    live["_status_model_reasons"] = {
        family: reason or shared_reason for family, reason in reasons.items()
    }
    return live, "live"


def _live_status_managed_state(state: dict, cached: dict | None) -> tuple[dict | None, str]:
    """Return the current managed policy without updating the on-disk cache."""
    workspace = state.get("workspace")
    if not workspace:
        return cached, "cached"
    profile = state.get("profile")
    if not profile and not external_bearer_configured():
        print_warning("Live managed configuration needs the CLI profile saved by ug configure.")
        return cached, "cached"

    try:
        if state.get("use_pat"):
            apply_pat_environment(state)
        with spinner("Refreshing live managed configuration..."):
            token = get_databricks_token(workspace, profile)
            raw, reason = get_managed_config(workspace, token)
    except RuntimeError as exc:
        print_warning(f"Live managed configuration failed ({exc}); showing cached configuration.")
        return cached, "cached"

    if reason is not None:
        if "feature_disabled" in reason.lower():
            return None, "live"
        print_warning(
            f"Live managed configuration failed ({reason}); showing cached configuration."
        )
        return cached, "cached"
    return (normalize_managed_config(raw) if raw is not None else None), "live"


def status() -> int:
    state = load_state()
    workspace = state.get("workspace")
    managed_configs = state.get("managed_configs") or {}
    # Both developer- and workspace-managed servers, so the count agrees with `ug mcp list`.
    mcp_servers = (state.get("mcp_servers") or []) + (state.get("managed_mcp_servers") or [])
    cached_managed = load_managed_state(workspace) if workspace else None
    managed, managed_freshness = _live_status_managed_state(state, cached_managed)
    configured_tools = (
        set(state.get("available_tools") or [])
        | set(managed_configs)
        | set((managed or {}).get("enabled_agents") or {})
    )

    console.print(heading("ug status"))
    console.print(
        f"  {status_badge('Configured', 'ok') if workspace else status_badge('Not Configured', 'warn')}"
    )

    provider_rows = [("Workspace URL", workspace or "not configured")]
    profile = state.get("profile")
    if profile:
        provider_rows.append(("CLI profile", profile))
    provider_rows.append(
        (
            "Configuration",
            f"Workspace-managed ({managed_freshness})" if managed else "Self-configured",
        )
    )
    policy = (managed or {}).get("budget_policy")
    if isinstance(policy, dict):
        provider_rows.append(("Policy", str(policy.get("display_name") or "coding-agents-default")))
    _print_status_panel("Provider", provider_rows)

    model_state, model_freshness = _live_status_model_state(state, configured_tools)
    print_heading("Coding Agents")
    skill_counts_by_agent = configured_skill_counts_by_agent(state, TOOL_SPECS)
    for tool, spec in TOOL_SPECS.items():
        if tool not in configured_tools:
            continue
        effective_state = resolve_state(managed, model_state, tool) if managed else model_state
        agent_managed = tool in ((managed or {}).get("enabled_agents") or {})
        provider_service = get_provider_service(effective_state, tool)
        rows = [
            (
                "Configuration",
                f"Workspace-managed ({managed_freshness})" if agent_managed else "Self-configured",
            ),
            (
                "Model provider",
                provider_service or "Databricks AI Gateway",
            ),
        ]
        models = (
            []
            if provider_service and not effective_state.get(f"{tool}_static_models")
            else _status_models(tool, effective_state)
        )
        model_source = "managed" if agent_managed and models else model_freshness
        if models:
            rows.append((f"Models ({len(models)}, {model_source})", ", ".join(models)))
        elif provider_service:
            rows.append(("Models", "Defined by provider service"))
        else:
            rows.append((f"Models ({model_source})", "none available"))
        default_model = _status_default_model(tool, effective_state, models)
        if default_model:
            rows.append(("Default model", default_model))
        if tool in ("claude", "codex"):
            rows.append(
                (
                    "Tracing",
                    "enabled" if effective_state.get(f"{tool}_otel_tracing") else "disabled",
                )
            )
        if tool in MCP_CLIENTS:
            # High-level overview: just a count per agent. `ug mcp list` (see the note below) shows
            # the per-server detail and live connection status, so status stays scannable. Dedupe by
            # name so a server present in both mcp_servers and managed_mcp_servers isn't double-counted.
            mcp_names = {
                server.get("name")
                for server in mcp_servers
                if tool in (server.get("clients") or [])
                and server.get("name")
                and server.get("kind") != SKILLS_MCP_KIND
            }
            # Managed servers ug delivers through an OS-managed file live in that file, not state.
            if tool == "claude":
                mcp_names |= claude_agent.read_managed_mcp_urls().keys()
            elif tool == "codex":
                mcp_names |= codex_agent.read_managed_mcp_urls().keys()
            rows.append(("MCP servers", str(len(mcp_names))))
            rows.append(("Skills", str(skill_counts_by_agent.get(tool, 0))))
        base_url = state.get("base_urls", {}).get(tool)
        if isinstance(base_url, dict):
            base_url = ", ".join(str(url) for url in base_url.values())
        rows.append(("Endpoint", str(base_url or "not configured")))
        _print_status_panel(str(spec["display"]), rows)

    if not configured_tools:
        print_note("No coding agents are configured.")
    return 0


def revert() -> int:
    state = load_state()
    managed_configs = state.get("managed_configs") or {}
    mcp_results = revert_mcp_configs(state)
    claude_managed_result = claude_agent.revert_managed_settings()
    claude_user_settings_restored = claude_agent.restore_user_settings_mirror(state)
    codex_managed_result = codex_agent.revert_managed_config()

    results: dict[str, bool] = {
        tool: restore_file(
            spec["config_path"], spec["backup_path"], bool(managed_configs.get(tool))
        )
        for tool, spec in TOOL_SPECS.items()
    }
    pi_settings_restored = restore_file(
        PI_SETTINGS_PATH, PI_SETTINGS_BACKUP_PATH, bool(managed_configs.get("pi"))
    )
    # Older Codex (< 0.134.0) had ucode edit the shared ~/.codex/config.toml in
    # place; restoring the per-profile file above does not undo that.
    legacy_codex_stripped = revert_legacy_shared_config()
    clear_state()

    print_heading("Revert")
    print_kv("Workspace", state.get("workspace") or "none")
    for tool, spec in TOOL_SPECS.items():
        print_kv(f"{spec['display']} config", "restored" if results[tool] else "unchanged")
    if legacy_codex_stripped:
        print_kv("Codex shared config", "ucode entries removed")
    print_kv("Claude Code OS-managed settings", claude_managed_result)
    print_kv(
        "Claude Code user settings",
        "ug entries removed" if claude_user_settings_restored else "unchanged",
    )
    print_kv("Codex OS-managed settings", codex_managed_result)
    print_kv("Pi settings", "restored" if pi_settings_restored else "unchanged")
    for client, spec in MCP_CLIENTS.items():
        print_kv(
            f"{spec['display']} MCP config",
            "restored" if mcp_results.get(client) else "unchanged",
        )
    print_success("ug state cleared")
    return 0


# ---------------------------------------------------------------------------
# typer app
# ---------------------------------------------------------------------------


_HELP_COMMAND_ORDER = (
    "claude",
    "codex",
    "copilot",
    "cursor",
    "gemini",
    "opencode",
    "pi",
    "configure",
    "mcp",
    "skills",
    "export",
    "revert",
    "status",
    "upgrade",
    "doctor",
    "usage",
)


class _HelpOrderedGroup(TyperGroup):
    """Keep top-level help organized across commands and nested Typer apps."""

    def list_commands(self, ctx: _click.Context) -> list[str]:
        commands = super().list_commands(ctx)
        order = {name: index for index, name in enumerate(_HELP_COMMAND_ORDER)}
        return sorted(commands, key=lambda name: order.get(name, len(order)))

    def format_options(self, ctx: _click.Context, formatter: _click.HelpFormatter) -> None:
        self.format_commands(ctx, formatter)
        options = []
        for param in self.get_params(ctx):
            record = param.get_help_record(ctx)
            if record is not None and param.param_type_name == "option":
                options.append(record)
        if options:
            with formatter.section("Global Options"):
                formatter.write_dl(options)

    def format_help(self, ctx: _click.Context, formatter: _click.HelpFormatter) -> None:
        if not HAS_RICH or self.rich_markup_mode is None:
            return super().format_help(ctx, formatter)

        from typer import rich_utils

        options = [
            param
            for param in self.get_params(ctx)
            if isinstance(param, TyperOption) and not param.hidden
        ]
        for option in options:
            option.hidden = True
        try:
            rich_utils.rich_format_help(obj=self, ctx=ctx, markup_mode=self.rich_markup_mode)
        finally:
            for option in options:
                option.hidden = False
        option_rows: list[_click.Command] = []
        for option in options:
            signature = ", ".join(option.opts)
            if option.secondary_opts:
                signature += f" / {', '.join(option.secondary_opts)}"
            metavar = option.make_metavar(ctx=ctx)
            if metavar and "boolean" not in metavar.lower():
                signature += f" {metavar}"
            help_record = option.get_help_record(ctx)
            option_rows.append(
                TyperCommand(
                    name=signature,
                    help=help_record[1] if help_record is not None else "",
                )
            )
        rich_utils._print_commands_panel(
            name="Global Options",
            commands=option_rows,
            markup_mode=self.rich_markup_mode,
            console=rich_utils._get_rich_console(),
            cmd_len=max(len(row.name or "") for row in option_rows),
        )


app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    cls=_HelpOrderedGroup,
)
configure_app = typer.Typer(add_completion=False, no_args_is_help=False)
app.add_typer(
    configure_app,
    name="configure",
    help="Configure workspace and tool settings.",
    rich_help_panel="Setup",
)
mcp_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(
    mcp_app,
    name="mcp",
    help="Inspect and manage the Databricks MCP servers ug configures for your coding agents.",
    rich_help_panel="Tools and Skills",
)
skill_app = typer.Typer(add_completion=False, no_args_is_help=False)
app.add_typer(
    skill_app,
    name="skills",
    help="Databricks Skills for your coding tools.",
    rich_help_panel="Tools and Skills",
)


class SkillsVia(StrEnum):
    """How `ug skills add`/`remove` act on skills: download them to disk (the
    default) or scope them on the skills MCP connection."""

    download = "download"
    mcp = "mcp"


def _version_callback(value: bool) -> None:
    if value:
        from ucode.telemetry import ug_version

        print(ug_version())
        raise typer.Exit()


def _configure_agents_for_mcp(requested: list[str]) -> set[str]:
    """Ensure the named coding agents are set up (workspace + models) so a
    subsequent `ug mcp add` / `ug skills add --via mcp` has them as targets, and
    return the full canonical name set. Agents already configured are left as-is;
    only the rest are bootstrapped. Model agents go through
    configure_workspace_command (which installs binaries and configures models);
    Cursor is MCP-only, so it just needs workspace state established and rides
    along via MCP_ONLY_CLIENTS. Interactive — prompts for the workspace URL on
    first run."""
    scope = {a if a == "cursor" else normalize_tool(a) for a in requested}
    ready = set(configured_mcp_clients(load_state(), available_mcp_clients()))
    to_bootstrap = scope - ready
    model_agents = sorted(a for a in to_bootstrap if a != "cursor")
    if model_agents:
        configure_workspace_command(selected_tools=model_agents)
    if "cursor" in to_bootstrap and not model_agents:
        _configure_shared_workspace_states(
            [_prompt_for_configuration(None)], tools=[], force_login=True
        )
    return scope


def _configure_optional_setup(state: dict, tools: list[str]) -> None:
    enabled = prompt_yes_no("Configure MCP servers, skills, and plugins?")
    state["databricks_ai_tools_enabled"] = enabled
    save_state(state)
    if not enabled:
        return

    install_databricks_ai_tools_for_agents(tools, state)
    configure_mcp_command()


@mcp_app.command("add")
def mcp_add(
    location: Annotated[
        str | None,
        typer.Option(
            "--location",
            help="Non-interactive: register the MCP services in the given Unity Catalog "
            "`<catalog>.<schema>` (e.g. `system.ai`) and exit without showing the picker. "
            "Servers already configured outside this location are kept.",
        ),
    ] = None,
    names: Annotated[
        str | None,
        typer.Option(
            "--names",
            help="Register this comma-separated subset of MCP services (additively). Full names "
            "like `system.ai.github` work on their own; bare short names like `github` need "
            "--location to locate them. Omit --names to register the whole --location schema; "
            'an empty `--names ""` adds nothing (no-op). V2 AI Gateway servers (not in the '
            "interactive picker) are added by naming them here: `vector-search:<catalog>.<schema>`, "
            "`uc-functions:<catalog>.<schema>`, `external:<connection>`, `genie-space:<id>`, or "
            "`app:<name>` (workspace access required).",
        ),
    ] = None,
    agents: Annotated[
        str | None,
        typer.Option(
            "--agents",
            help="Comma-separated coding agents to register the server(s) for (e.g. "
            "claude,codex,cursor). Any that aren't configured yet are set up first "
            "(workspace + models), so this works as a one-command setup. Without --agents, "
            "the server is registered for every already-configured agent.",
        ),
    ] = None,
) -> None:
    """Add Databricks MCP servers to installed coding tools.

    Purely additive: it never removes MCP servers that are already configured, only registers new
    ones (use `ug mcp remove` to remove). Pass --agents to target (and, if needed, set up) specific
    agents.
    """
    selected = None if names is None else {s.strip() for s in names.split(",") if s.strip()}
    requested_agents = (
        None
        if agents is None
        else ({a.strip().lower() for a in agents.split(",") if a.strip()} or None)
    )
    try:
        scope = _configure_agents_for_mcp(sorted(requested_agents)) if requested_agents else None
        add_mcp_command(location=location, services=selected, agents=scope)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


@mcp_app.command("remove")
def mcp_remove(
    agents: Annotated[
        str | None,
        typer.Option(
            "--agents",
            help="Comma-separated coding agents to remove the server(s) from (e.g. "
            "claude,codex). A server registered on several agents is unregistered only "
            "from the named ones and kept on the rest. Without --agents, a selected server "
            "is removed from every agent it's on.",
        ),
    ] = None,
) -> None:
    """Remove configured Databricks MCP servers from your coding tools.

    Interactive: shows the servers you currently have configured and unregisters the
    ones you select. Needs no Databricks login.
    """
    requested_agents = (
        None
        if agents is None
        else ({a.strip().lower() for a in agents.split(",") if a.strip()} or None)
    )
    try:
        remove_mcp_command(agents=requested_agents)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


@mcp_app.command("list")
def mcp_list(
    agents: Annotated[
        str | None,
        typer.Option(
            "--agents",
            help="Comma-separated coding agents to report on (e.g. claude,codex). Without "
            "--agents, every installed MCP-capable agent is included.",
        ),
    ] = None,
) -> None:
    """List the Databricks MCP servers ug has configured and their live connection status.

    Reads ug's saved state and each installed agent's own `mcp list` to show, per agent, whether
    each server is connected. Read-only; needs no Databricks login. Use the `add`/`remove`
    subcommands to change what's configured.
    """
    requested_agents = (
        None
        if agents is None
        else ({a.strip().lower() for a in agents.split(",") if a.strip()} or None)
    )
    try:
        list_mcp_command(agents=requested_agents)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


@mcp_app.command("web-search")
def mcp_web_search_cmd() -> None:
    """Run the web_search MCP server over stdio. Invoked as a subprocess by Claude Code."""
    from ucode.mcp_web_search import serve

    serve()


def _stdin_is_interactive() -> bool:
    import sys

    return sys.stdin.isatty()


@skill_app.callback(invoke_without_command=True)
def skills(ctx: typer.Context) -> None:
    """Databricks Skills for your coding tools.

    With no subcommand, prints this help, then registers the skills MCP connection
    (utility tools only) for your configured agents, keeping any existing scope.
    """
    if ctx.invoked_subcommand is not None:
        return
    console.print(ctx.get_help())
    try:
        install_databricks_cli(minimum=SKILLS_MCP_MIN_DATABRICKS_CLI_VERSION)
        first_time = configure_bare_skills_mcp_command()
    except (RuntimeError, ValueError) as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None
    if first_time:
        print_note(
            "To create a skill, ask your agent to create one with the Databricks skills "
            "registry MCP, which registers it in Unity Catalog."
        )


@skill_app.command("list")
def skills_list() -> None:
    """List the skills configured for your coding tools and how each was configured."""
    try:
        install_databricks_cli(minimum=SKILLS_MCP_MIN_DATABRICKS_CLI_VERSION)
        list_configured_skills_command()
    except (RuntimeError, ValueError) as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


@skill_app.command("add")
def skills_add(
    location: Annotated[
        str | None,
        typer.Option(
            "--location", help="Comma-separated `<catalog>.<schema>` skill scopes to add."
        ),
    ] = None,
    via: Annotated[
        SkillsVia,
        typer.Option(
            "--via",
            help="`download` skills to disk (default) or add the schemas to the skills MCP "
            "connection's scope (`mcp`).",
        ),
    ] = SkillsVia.download,
    path: Annotated[
        str | None,
        typer.Option(
            "--path",
            help="(download) Existing absolute project directory to download into; defaults "
            "to user-level skill directories.",
        ),
    ] = None,
    names: Annotated[
        str | None,
        typer.Option(
            "--names",
            help="(download) Download exactly these comma-separated fully-qualified "
            "`<catalog>.<schema>.<name>` skills, spanning any number of schemas. Not valid "
            "with --via mcp or --location.",
        ),
    ] = None,
    agents: Annotated[
        str | None,
        typer.Option(
            "--agents",
            help="(--via mcp only) Comma-separated coding agents whose skills MCP scope should "
            "be updated. Any that aren't configured yet are set up first. Without --agents, "
            "every configured agent is updated.",
        ),
    ] = None,
) -> None:
    """Add Databricks Skills to your coding tools, keeping any already configured.

    With ``--via mcp``, adds the given schemas to the skills MCP connection's scope.
    By default (``--via download``) downloads skills to project-level skill directories
    under ``--path``, or to user-level skill directories when omitted, keeping
    already-downloaded skills. ``--location`` downloads whole ``<catalog>.<schema>``
    schemas; ``--names`` downloads a named set of fully-qualified skills that may span
    schemas (and takes no ``--location``). With no ``--location``/``--names`` on an
    interactive terminal, opens a picker of the workspace's schemas to scope
    (``--via mcp``) or skills to download.
    """
    try:
        install_databricks_cli(minimum=SKILLS_MCP_MIN_DATABRICKS_CLI_VERSION)
        mcp = via is SkillsVia.mcp
        requested_skills = (
            None if names is None else {s.strip() for s in names.split(",") if s.strip()}
        )
        requested_agents = (
            None
            if agents is None
            else ({agent.strip().lower() for agent in agents.split(",") if agent.strip()} or None)
        )
        if mcp and path is not None:
            raise RuntimeError("--path is not supported with --via mcp")
        if mcp and requested_skills is not None:
            raise RuntimeError("--names is not supported with --via mcp")
        if requested_skills is not None and location is not None:
            raise RuntimeError("--names takes fully-qualified names; drop --location.")
        # Downloaded skills use shared directory families, so only MCP scopes can be agent-scoped.
        if not mcp and agents is not None:
            raise RuntimeError("--agents is only supported with --via mcp")
        if requested_skills is not None:
            invalid = sorted(s for s in requested_skills if not _is_qualified_skill_name(s))
            if invalid:
                raise RuntimeError(
                    "--names entries must be fully-qualified `<catalog>.<schema>.<name>` names "
                    f"(invalid: {', '.join(invalid)})."
                )
            configure_selected_skills_download_command(sorted(requested_skills), path)
            return
        locations = _parse_skill_locations(location)
        if not locations:
            if _stdin_is_interactive():
                if mcp:
                    configured_agents = (
                        _configure_agents_for_mcp(sorted(requested_agents))
                        if requested_agents
                        else None
                    )
                    configure_skills_mcp_picker_command(agents=configured_agents)
                else:
                    configure_skills_download_picker_command(path=path)
                return
            raise RuntimeError("--location is required for `ucode skills add`.")
        if mcp:
            configured_agents = (
                _configure_agents_for_mcp(sorted(requested_agents)) if requested_agents else None
            )
            add_skills_command(locations, agents=configured_agents)
        else:
            configure_location_skills_download_command(locations, path=path)
    except (RuntimeError, ValueError) as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


@skill_app.command("remove")
def skills_remove(
    location: Annotated[
        str | None,
        typer.Option(
            "--location",
            help="Comma-separated `<catalog>.<schema>` schemas to remove (from the skills MCP "
            "scope with --via mcp, else their downloaded skills).",
        ),
    ] = None,
    via: Annotated[
        SkillsVia,
        typer.Option(
            "--via",
            help="Remove `download`ed skill files (default) or drop the schemas from the skills "
            "MCP connection (`mcp`).",
        ),
    ] = SkillsVia.download,
    path: Annotated[
        str | None,
        typer.Option(
            "--path",
            help="(download) Limit removal to skills downloaded under this base directory; "
            "without it, every base is in scope.",
        ),
    ] = None,
    names: Annotated[
        str | None,
        typer.Option(
            "--names",
            help="(download) Remove exactly these comma-separated fully-qualified "
            "`<catalog>.<schema>.<name>` skills, spanning any number of schemas. Not valid "
            "with --via mcp or --location.",
        ),
    ] = None,
    agents: Annotated[
        str | None,
        typer.Option(
            "--agents",
            help="(--via mcp only) Comma-separated coding agents to remove the schemas from "
            "(e.g. claude,codex). A schema scoped to several agents is removed only from the "
            "named ones and kept on the rest. Without --agents, it is removed from every agent.",
        ),
    ] = None,
) -> None:
    """Remove Skills previously added to your coding tools.

    With ``--via mcp``, drops skill schemas from the skills MCP connection: ``--location`` removes
    the named ``<catalog>.<schema>`` schemas, and with none on an interactive terminal a picker
    lists the scoped schemas. By default (``--via download``) removes downloaded skill directories:
    ``--location`` removes every skill downloaded from a ``<catalog>.<schema>``, ``--names`` removes
    named fully-qualified skills that may span schemas, and with none of them a picker lists every
    downloaded skill. ``--path`` limits either to one download base. Only skills ucode downloaded
    are removed; a same-named skill you authored is left alone.
    """
    try:
        install_databricks_cli(minimum=SKILLS_MCP_MIN_DATABRICKS_CLI_VERSION)
        mcp = via is SkillsVia.mcp
        requested_skills = (
            None if names is None else {s.strip() for s in names.split(",") if s.strip()}
        )
        if mcp:
            if path is not None or requested_skills is not None:
                raise RuntimeError("--path and --names are not supported with --via mcp.")
            requested_agents = (
                None
                if agents is None
                else ({a.strip().lower() for a in agents.split(",") if a.strip()} or None)
            )
            locations = _parse_skill_locations(location)
            if locations:
                remove_skills_locations_command(locations, agents=requested_agents)
            elif _stdin_is_interactive():
                remove_skills_command(agents=requested_agents)
            else:
                raise RuntimeError("--location is required for `ug skills remove --via mcp`.")
            return
        if agents is not None:
            raise RuntimeError("--agents is only supported with --via mcp.")
        if requested_skills is not None and location is not None:
            raise RuntimeError("--names takes fully-qualified names; drop --location.")
        if requested_skills is not None:
            invalid = sorted(s for s in requested_skills if not _is_qualified_skill_name(s))
            if invalid:
                raise RuntimeError(
                    "--names entries must be fully-qualified `<catalog>.<schema>.<name>` names "
                    f"(invalid: {', '.join(invalid)})."
                )
            remove_downloaded_skills_command([], sorted(requested_skills), path=path)
            return
        locations = _parse_skill_locations(location)
        if path is not None and not locations:
            raise RuntimeError("--path is only supported with --location or --names.")
        if not locations and not _stdin_is_interactive():
            raise RuntimeError("--location or --names is required for `ug skills remove`.")
        remove_downloaded_skills_command(locations, path=path)
    except (RuntimeError, ValueError) as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


@app.command("mcp-proxy", hidden=True)
def mcp_proxy_cmd(
    url: Annotated[
        str,
        typer.Option("--url", help="Databricks streamable-HTTP MCP endpoint to forward to."),
    ],
    host: Annotated[
        str | None,
        typer.Option(
            "--host", help="Workspace URL for token minting. Defaults to the saved workspace."
        ),
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="Databricks CLI profile.")
    ] = None,
    use_pat: Annotated[
        bool,
        typer.Option(
            "--use-pat",
            help="Authenticate with the profile's static personal access token (from "
            "~/.databrickscfg) instead of OAuth. Set automatically for workspaces configured "
            "with `ug configure --profile <name> --use-pat`.",
        ),
    ] = False,
) -> None:
    """Bridge a coding agent's stdio MCP transport to a Databricks MCP endpoint.

    Each configured client spawns this as a local stdio MCP server (see
    `ug mcp add`); it forwards messages to ``--url`` and injects a
    freshly-minted token on every upstream request, so it never expires
    mid-session. Not meant for interactive use — the agent manages this
    process's lifecycle."""
    from ucode.mcp_proxy import serve

    state = load_state()
    workspace = host or state.get("workspace")
    if not workspace:
        print_err("No workspace configured. Run `ug configure` first.")
        raise typer.Exit(1)
    profile = profile or state.get("profile")
    serve(url, workspace, profile, use_pat=use_pat or bool(state.get("use_pat")))


@app.command("auth-token", hidden=True)
def auth_token_cmd(
    host: Annotated[
        str | None, typer.Option("--host", help="Workspace URL. Defaults to the saved workspace.")
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="Databricks CLI profile.")
    ] = None,
    use_pat: Annotated[
        bool, typer.Option("--use-pat", help="Read the profile's static PAT instead of OAuth.")
    ] = False,
    force_refresh: Annotated[
        bool,
        typer.Option("--force-refresh", help="Force the Databricks CLI to mint a new token."),
    ] = False,
    client_id: Annotated[
        str | None,
        typer.Option(
            "--client-id", hidden=True, help="Experimental: custom public OAuth client ID."
        ),
    ] = None,
    redirect_url: Annotated[
        str | None,
        typer.Option(
            "--redirect-url",
            hidden=True,
            help="Registered OAuth callback URL. Defaults to http://localhost:8020.",
        ),
    ] = None,
    scopes: Annotated[
        str | None,
        typer.Option(
            "--scopes",
            hidden=True,
            help="Comma-separated OAuth scopes for the custom client.",
        ),
    ] = None,
) -> None:
    """Print a Databricks bearer token to stdout, then exit.

    This is the cross-platform helper invoked by Claude Code's `apiKeyHelper`
    and Codex's auth command on every token refresh. It is not meant for
    interactive use. All token logic (DATABRICKS_BEARER short-circuit, PAT
    profiles, OAuth refresh) lives in `get_databricks_token`, so the same
    binary works on macOS, Linux, and Windows without any POSIX shell."""
    import sys

    if client_id is not None and use_pat:
        print_err("--client-id cannot be combined with --use-pat.")
        raise typer.Exit(1)
    if redirect_url is not None and client_id is None:
        print_err("--redirect-url requires --client-id.")
        raise typer.Exit(1)
    if scopes is not None and client_id is None:
        print_err("--scopes requires --client-id.")
        raise typer.Exit(1)
    if client_id is not None and scopes is None:
        print_err("--scopes is required with --client-id.")
        raise typer.Exit(1)
    state = load_state()
    explicit_host = bool(host and host.strip())
    workspace = host or state.get("workspace")
    if not workspace:
        print_err("No workspace configured. Run `ug configure` first.")
        raise typer.Exit(1)
    if profile is None and not explicit_host:
        profile = state.get("profile")
    if client_id is None and (use_pat or state.get("use_pat")):
        # --use-pat explicitly means "serve the profile's static PAT". Fail
        # closed if it can't be read rather than falling through to OAuth —
        # `auth token` cannot serve a PAT-only profile, so that path would
        # surface a misleading stale-login error instead of the real cause.
        if not ensure_pat_bearer(profile):
            print_err(
                f"--use-pat: no personal access token available for profile "
                f"'{profile or '<none>'}'. Add a `token = <PAT>` entry under "
                f"[{profile or 'your-profile'}] in ~/.databrickscfg, or re-run "
                "`ug configure` without --use-pat to use OAuth."
            )
            raise typer.Exit(1)
    try:
        if client_id is not None:
            assert scopes is not None
            token = custom_oauth.get_custom_client_token(
                workspace,
                client_id=client_id,
                redirect_url=(
                    redirect_url if redirect_url is not None else custom_oauth.DEFAULT_REDIRECT_URL
                ),
                scopes=scopes.split(","),
                profile=profile,
                force_refresh=force_refresh,
            )
        else:
            token = get_databricks_token(workspace, profile, force_refresh=force_refresh)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    # Write the bare token (with trailing newline) to stdout — nothing else may
    # land on stdout or the consuming agent will treat it as part of the token.
    sys.stdout.write(token + "\n")


@app.command("otel-headers", hidden=True)
def otel_headers_cmd(
    host: Annotated[
        str | None, typer.Option("--host", help="Workspace URL. Defaults to the saved workspace.")
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="Databricks CLI profile.")
    ] = None,
    use_pat: Annotated[
        bool, typer.Option("--use-pat", help="Read the profile's static PAT instead of OAuth.")
    ] = False,
    force_refresh: Annotated[
        bool,
        typer.Option("--force-refresh", help="Force the Databricks CLI to mint a new token."),
    ] = False,
) -> None:
    """Print fresh OTLP export headers as JSON to stdout, then exit."""
    import json
    import sys

    state = load_state()
    workspace = host or state.get("workspace")
    if not workspace:
        print_err("No workspace configured. Run `ug configure` first.")
        raise typer.Exit(1)
    profile = profile or state.get("profile")
    if use_pat or state.get("use_pat"):
        if not ensure_pat_bearer(profile):
            print_err(
                f"--use-pat: no personal access token available for profile "
                f"'{profile or '<none>'}'. Add a `token = <PAT>` entry under "
                f"[{profile or 'your-profile'}] in ~/.databrickscfg, or re-run "
                "`ug configure` without --use-pat to use OAuth."
            )
            raise typer.Exit(1)
    try:
        token = get_databricks_token(workspace, profile, force_refresh=force_refresh)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    sys.stdout.write(json.dumps({"Authorization": f"Bearer {token}"}) + "\n")


def _oauth_token_is_fresh(token: str, buffer_seconds: float = 120) -> bool:
    import base64
    import binascii
    import json
    import time

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        expires_at = float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, KeyError, TypeError, ValueError, binascii.Error, json.JSONDecodeError):
        return False
    return time.time() < expires_at - buffer_seconds


@app.command("codex-router-hook", hidden=True)
def codex_router_hook_cmd(
    event: str,
    host: Annotated[str | None, typer.Option("--host")] = None,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    use_pat: Annotated[bool, typer.Option("--use-pat")] = False,
    model: Annotated[list[str] | None, typer.Option("--model")] = None,
) -> None:
    """Run a Codex smart-routing lifecycle hook."""
    import json
    import sys

    if not smart_routing_v2.smart_routing_enabled():
        return

    from ucode.smart_routing.codex_routing import (
        record_session_start,
        record_subagent_start,
        route_pre_tool_use,
    )

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    if event == "session-start":
        record_session_start(payload)
        return
    if event == "record-subagent":
        record = record_subagent_start(payload)
        matched = record.get("matches_router_decision")
        if matched is True:
            sys.stdout.write(
                json.dumps(
                    {
                        "systemMessage": "Smart Routing verified. "
                        f"Subagent is using {record.get('model')}."
                    }
                )
            )
        elif matched is False:
            sys.stdout.write(
                json.dumps(
                    {
                        "systemMessage": "Smart Routing mismatch: router requested "
                        f"{record.get('requested_model')}, but Codex started "
                        f"{record.get('model')}."
                    }
                )
            )
        # When matched is None the harness didn't report the subagent model —
        # the PreToolUse hook already injected the routed model, so emit nothing.
        return
    if event != "route-subagent" or not host:
        return
    if use_pat and not ensure_pat_bearer(profile):
        return
    token = os.environ.get("DATABRICKS_BEARER", "").strip()
    if not token:
        token = os.environ.get("OAUTH_TOKEN", "").strip()
        if not _oauth_token_is_fresh(token):
            try:
                token = get_databricks_token(host, profile, force_refresh=True)
            except RuntimeError:
                return
    output = route_pre_tool_use(
        payload,
        workspace=host,
        token=token,
        available_models=model or [],
        audit_decision=True,
    )
    if output is not None:
        sys.stdout.write(json.dumps(output))


@app.command("claude-router-hook", hidden=True)
def claude_router_hook_cmd(
    event: str,
    host: Annotated[str | None, typer.Option("--host")] = None,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    use_pat: Annotated[bool, typer.Option("--use-pat")] = False,
    model: Annotated[list[str] | None, typer.Option("--model")] = None,
    socket_path: Annotated[str | None, typer.Option("--socket")] = None,
) -> None:
    """Run a Claude Code smart-routing lifecycle hook."""
    import json
    import sys

    if not smart_routing_v2.smart_routing_enabled():
        return

    from ucode.smart_routing.claude_routing import (
        record_session_start,
        record_subagent_start,
    )

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    if event == ROUTE_FIRST_PROMPT_EVENT:
        if not socket_path:
            socket_path = os.environ.get(FIRST_PROMPT_SOCKET_ENV)
        if not socket_path:
            return
        from pathlib import Path

        from ucode.smart_routing.claude_pty import (
            first_prompt_hook_output,
            request_first_prompt_route,
        )

        response = request_first_prompt_route(
            Path(socket_path),
            payload,
            timeout=smart_routing_v2.CLAUDE_ROUTE_SELECTION_TIMEOUT_S + 5.0,
        )
        output = first_prompt_hook_output(response)
        if output is not None:
            sys.stdout.write(json.dumps(output))
        return
    if event == "session-start":
        record_session_start(payload)
        return
    if event == "record-subagent":
        record = record_subagent_start(payload)
        matched = record.get("matches_router_decision")
        if matched is True:
            sys.stdout.write(
                json.dumps(
                    {
                        "systemMessage": "Smart Routing verified. "
                        f"Subagent is using {record.get('model')}."
                    }
                )
            )
        elif matched is False:
            sys.stdout.write(
                json.dumps(
                    {
                        "systemMessage": "Smart Routing mismatch: router requested "
                        f"{record.get('requested_model')}, but Claude Code started "
                        f"{record.get('model')}."
                    }
                )
            )
        # When matched is None the harness didn't report the subagent model —
        # the PreToolUse hook already injected the routed model, so emit nothing.
        return
    if event != "route-subagent" or not host:
        return
    token = os.environ.get("OAUTH_TOKEN") or os.environ.get("DATABRICKS_BEARER")
    if not token:
        if use_pat and not ensure_pat_bearer(profile):
            return
        try:
            token = get_databricks_token(host, profile)
        except RuntimeError:
            return
    output = smart_routing_v2.route_claude_pre_tool_use(
        payload,
        workspace=host,
        token=token,
        available_models=model or [],
        audit_decision=True,
    )
    if output is not None:
        sys.stdout.write(json.dumps(output))


def _auto_configure_tool(tool: str, custom_oauth: CustomOAuthConfig | None = None) -> None:
    """Configure a tool for launch without sending a separate validation prompt.

    The real agent session follows immediately; explicit configure retains the
    test-prompt validation.
    """
    existing = load_state()
    workspace = existing.get("workspace")
    profile = existing.get("profile")
    if not workspace:
        workspace, profile = _prompt_for_configuration(tool)
    configure_kwargs = {"custom_oauth": custom_oauth} if custom_oauth is not None else {}
    state = configure_shared_state(workspace, profile=profile, tools=[tool], **configure_kwargs)

    state = configure_single_tool(tool, state)

    spec = TOOL_SPECS[tool]
    console.print(
        Panel(
            f"[bold]Workspace:[/bold] [cyan]{state['workspace']}[/cyan]\n"
            f"[bold]{spec['display']}:[/bold] [green]configured[/green] "
            f"[dim](Provider: {_provider_summary(tool, state)})[/dim]",
            title="Configuration Complete",
            style="green",
            expand=False,
        )
    )


CAN_USE_CACHED_CONFIG_AGENTS = frozenset({"claude", "codex"})


@contextmanager
def _smart_routing_v2_flag(enabled: bool) -> Iterator[None]:
    """Enable V2 for this launch without leaking into an embedding process."""
    if not enabled:
        yield
        return
    previous = smart_routing_v2.enable_smart_routing()
    try:
        yield
    finally:
        smart_routing_v2.restore_smart_routing_env(previous)


@contextmanager
def _disable_smart_routing_for_subcommand(tool: str, ctx: Any) -> Iterator[None]:
    """Keep native agent subcommands out of every smart-routing path.

    The environment flag is also consulted during bootstrap/version checks,
    before the final launch options are built. Native positional subcommands
    must therefore suppress the flag for the whole ucode launch flow. An
    explicit prompt after `--` remains eligible for routing.
    """
    if _smart_routing_launch_shape(tool, ctx.args, _has_explicit_prompt(ctx)):
        yield
        return
    previous = smart_routing_v2.disable_smart_routing()
    try:
        yield
    finally:
        smart_routing_v2.restore_smart_routing_env(previous)


def _migrate_legacy_smart_routing(state: dict) -> dict:
    """Remove the former persisted opt-in and its permanent routing hooks."""
    if smart_routing_v2.LEGACY_STATE_KEY not in state:
        return state
    # The legacy key was shared by both agents. Clean both sets of files and
    # artifacts before the first helper removes the marker from state.
    codex_agent.disable_smart_routing(state)
    claude_agent.disable_smart_routing(state)
    return state


def _reject_disabled_agent(managed: dict | None, tool: str) -> None:
    """Refuse to launch ``tool`` when the managed config enables other agents but not this one.

    ``enabled_agents`` is an allowlist: launching an agent the admin didn't enable would run
    unmanaged, with none of their models or provider applied. A config that names no agents at all
    expresses no opinion, so it blocks nothing.
    """
    enabled = managed_enabled_tools(managed or {})
    if enabled and tool not in enabled:
        names = ", ".join(TOOL_SPECS[name]["display"] for name in enabled)
        raise RuntimeError(
            f"Your workspace's managed config doesn't enable {TOOL_SPECS[tool]['display']}. "
            f"Enabled: {names}."
        )


def _reject_managed_launch_source_options(
    managed: dict | None,
    *,
    provider: str | None,
    parent_schema: str | None,
) -> None:
    if managed is not None and (provider is not None or parent_schema is not None):
        raise RuntimeError(
            "`--provider` or `--model-location` is not allowed when a managed config exists "
            "for the workspace; the managed config controls the model source."
        )


def _fetch_managed_config(state: dict) -> ManagedConfigResult:
    """The workspace's managed config for this launch, plus whether the feature is disabled.

    ``ManagedConfigResult(None, True)`` when the workspace has the feature disabled server-side;
    ``ManagedConfigResult(None, False)`` when the feature is on but no config is published.
    """
    with spinner("Loading..."):
        return refresh_managed_config(state)


def _note_recommended_agent(recommendation: dict | None, tool: str) -> None:
    """Say when the budget tier points at a different agent than the one being launched.

    Launching any enabled agent is allowed, so this informs rather than blocks — and explains why
    the session is not on the tier's model.
    """
    # The tier's own agent, not `recommended_agent`'s default_agent fallback: there is nothing to
    # say when the config's baseline simply differs from what the developer asked for.
    agent = (recommendation or {}).get("agent")
    if agent == tool or agent not in TOOL_SPECS:
        return
    model = (recommendation or {}).get("model")
    suffix = f" with {model}" if isinstance(model, str) and model else ""
    print_note(
        f"Your budget tier recommends {TOOL_SPECS[agent]['display']}{suffix}; "
        f"launching {TOOL_SPECS[tool]['display']} as requested."
    )


def _fetch_budget_recommendation(state: dict, managed: dict | None) -> dict | None:
    """The agent and model the caller's budget tier allows, or None when there is no budget to read.

    Enforcement is server-side, so a failed read only costs the recommendation: the config's own
    ``default_model`` still applies and the launch proceeds.
    """
    if managed is None or is_dry_run():
        return None
    reason: str | None = None
    recommendation = None
    with spinner("Checking your budget..."):
        try:
            recommendation, reason = get_model_recommendation(
                state["workspace"],
                get_databricks_token(state["workspace"], state.get("profile")),
            )
        except (RuntimeError, OSError) as exc:
            # A token that lapsed since the config refresh — or a Databricks CLI that isn't
            # installed or reachable — must not block the launch; the config's default_model stands.
            reason = str(exc)
    if reason is not None and not reason.startswith("HTTP 404"):
        print_warning(
            f"Could not check your budget ({reason}); "
            "using the default model from your workspace's config."
        )
    return recommendation


def _launch_title(tool: str) -> str:
    return f"Launching {TOOL_SPECS[tool]['display']} with Unity Gateway"


def _print_budget_panel(recommendation: dict, tool: str, managed: dict | None = None) -> None:
    """Show the workspace budget this launch spends against, when one is configured."""
    agent = recommendation.get("agent")
    display_agent = TOOL_SPECS[agent]["display"] if agent in TOOL_SPECS else None
    percent = budget_usage_percent(
        float(recommendation.get("current_spend") or 0.0),
        float(recommendation.get("effective_threshold") or 0.0),
    )
    line = recommendation_line(display_agent, recommendation.get("model"), percent)
    panel = render_budget_panel(
        recommendation,
        title=_launch_title(tool),
        extra_lines=[line] if line else None,
        managed=managed,
    )
    if panel is not None:
        console.print(panel)


def _configure_managed_mcp_servers(managed: dict | None) -> list[str]:
    """Register the managed config's MCP servers for every enabled MCP-client agent.

    Runs during ``ug configure`` after the enabled agents are configured, so a workspace-published
    server reaches each agent's `/mcp` list without the developer re-adding it. ``managed`` is None
    when the (now-current) workspace has no managed config: the reconcile then unregisters any
    servers a prior managed workspace registered, so switching workspaces resets the MCP registry.
    Best-effort: a failure warns and leaves the rest of configure intact. Returns the names of the
    servers registered this run (the completion summary counts them; state alone can't, since #717
    writes them to agents' OS-managed files rather than ``managed_mcp_servers``).
    """
    managed = managed or {}
    agents = {tool for tool in managed_enabled_tools(managed) if tool in MCP_CLIENTS}
    try:
        registered = reconcile_managed_mcp_servers(managed, agents)
    except McpServiceListingRateLimited:
        # A transient 429 while discovering the workspace's MCP services: skip MCP setup for this
        # run (existing servers are left untouched) with an info note instead of a hard failure, so
        # `ug configure` still completes. The next configure retries.
        print_note(
            "Skipped workspace MCP setup this run — MCP service discovery was rate-limited "
            "(HTTP 429). Existing MCP servers are unchanged; run `ug configure` again to retry."
        )
        return []
    except RuntimeError as exc:
        print_warning(f"Could not register your workspace's MCP servers: {exc}")
        return []
    names = [str(server["name"]) for server in registered if server.get("name")]
    if names:
        print_note(f"Registered workspace MCP server(s): {', '.join(names)}")
    return names


def _configure_managed_skills(managed: dict | None) -> None:
    """Download and reconcile the managed config's skills for every agent's ``/skills`` picker.

    Mirrors :func:`_configure_managed_mcp_servers`: runs during ``ug configure`` after the enabled
    agents are configured, so a workspace-published skill reaches ``.claude/skills`` and
    ``.agents/skills`` (both agents) without the developer downloading it. ``managed`` is None when
    the current workspace has no config: the reconcile then removes any managed skills a prior
    workspace left behind. Best-effort: a failure warns and leaves the rest of configure intact.
    """
    try:
        written, removed = reconcile_managed_skills(managed or {})
    except (RuntimeError, OSError) as exc:
        print_warning(f"Could not sync your workspace's skills: {exc}")
        return
    if written:
        print_note(f"Downloaded workspace skill(s): {', '.join(written)}")
    if removed:
        print_note(f"Removed workspace skill(s) no longer configured: {', '.join(removed)}")


def _child_owns_stdout(tool: str, tool_args: list[str]) -> bool:
    """True when the forwarded agent command speaks a stdio protocol on stdout.

    ``codex app-server`` puts its JSON-RPC stream on stdout, so ug's status
    output must move to stderr for that launch; the file descriptor stays
    untouched for the agent process.
    """
    return tool == "codex" and tool_args[:1] == ["app-server"]


def _should_launch_smart_routing(
    tool: str,
    tool_args: list[str],
    *,
    explicit_prompt: bool,
    model: str | None,
) -> bool:
    if model is not None or has_explicit_model_arg(tool_args):
        return False
    return _smart_routing_launch_shape(tool, tool_args, explicit_prompt)


def _smart_routing_launch_shape(tool: str, tool_args: list[str], explicit_prompt: bool) -> bool:
    """Whether the forwarded arguments represent an interactive launch."""
    if not tool_args or explicit_prompt:
        return True
    return tool == "claude" and tool_args[0].startswith("-")


def _launch_options(
    tool: str,
    tool_args: list[str],
    *,
    smart_routing_enabled: bool,
    explicit_prompt: bool,
    user_pinned_model: str | None,
    provider: str | None,
) -> LaunchOptions:
    return LaunchOptions(
        # Pinned models for providers are resolved above through the provider-specific launch path.
        user_pinned_model=user_pinned_model if provider is None else None,
        launch_smart_routing=(
            # Smart routing is enabled globally.
            smart_routing_enabled
            # Only Claude Code and Codex currently support smart routing.
            and tool in CAN_USE_CACHED_CONFIG_AGENTS
            # Smart routing does not currently support Model Provider Services.
            and provider is None
            # Route a supported interactive launch shape.
            and _should_launch_smart_routing(
                tool,
                tool_args,
                explicit_prompt=explicit_prompt,
                model=user_pinned_model,
            )
        ),
    )


@contextmanager
def _managed_smart_routing_environment(managed: dict | None, tool: str) -> Iterator[None]:
    """Expose an agent's managed smart-routing switch only to its launched session."""
    if not _managed_smart_routing_enabled(managed, tool):
        yield
        return

    previous = smart_routing_v2.enable_smart_routing()
    try:
        yield
    finally:
        smart_routing_v2.restore_smart_routing_env(previous)


def _managed_smart_routing_enabled(managed: dict | None, tool: str) -> bool:
    """Whether the workspace enabled smart routing for this specific agent."""
    agent_config = ((managed or {}).get("enabled_agents") or {}).get(tool) or {}
    return agent_config.get("smart_routing_enabled") is True


def _launch_tool(
    tool_name: str,
    ctx: typer.Context,
    provider: str | None = None,
    refresh: bool = False,
    skip_preflight: bool = False,
    workspace_url: str | None = None,
    managed: dict | None = None,
    recommendation: dict | None = None,
    model: str | None = None,
    parent_schema: str | None = None,
    custom_oauth: CustomOAuthConfig | None = None,
) -> None:
    try:
        tool = normalize_tool(tool_name)
        if not custom_oauth_cli_enabled(custom_oauth):
            os.environ.pop(CUSTOM_OAUTH_CLI_ENV_VAR, None)
        # Before any status print: a stdio-protocol subcommand owns stdout, so
        # every ug line from here on must go to stderr instead.
        if _child_owns_stdout(tool, ctx.args):
            redirect_output_to_stderr()
        explicit_prompt = _has_explicit_prompt(ctx)
        smart_routing_enabled = smart_routing_v2.smart_routing_enabled()
        # Launchers such as isaac put their harness arguments after `--`, so the harness's own
        # `--model` lands in ctx.args instead of a ucode option. It still determines the effective
        # launch model and should therefore win in the launch summary.
        forwarded_model = (
            explicit_model_arg_value(ctx.args) if tool in {"claude", "codex"} else None
        )
        # `--model` is exposed by the claude and gemini launch commands. Under a provider it selects
        # which of the service's targets/tiers to launch on, rather than being rejected — see the
        # provider branch below.
        # An explicit --workspace targets that workspace for this launch (and
        # auto-configures it if unseen), so `ug claude --provider ... --workspace ...`
        # works without a prior `ug configure`.
        if workspace_url:
            set_current_workspace(normalize_workspace_url(workspace_url))
        existing = load_state()
        # Workspaces configured with --use-pat export the profile's PAT as
        # DATABRICKS_BEARER up front so every auth check below (and the
        # launched agent itself) uses the static token instead of OAuth.
        if not custom_oauth_cli_enabled(custom_oauth):
            apply_pat_environment(existing)
        needs_auto_configure = not existing.get("workspace") or tool not in (
            existing.get("available_tools") or []
        )
        ensure_bootstrap_dependencies(
            tool,
            skip_cli_version_check=skip_preflight,
        )
        if needs_auto_configure:
            if custom_oauth is None:
                _auto_configure_tool(tool)
            else:
                _auto_configure_tool(tool, custom_oauth=custom_oauth)
        state = ensure_provider_state(tool)
        # Remembered before the fallback below collapses the two cases: a managed config may not
        # silently override a provider the user typed on the command line (it errors instead).
        explicit_provider = provider
        # An explicit --provider overrides the persisted choice; otherwise fall
        # back to whatever `ug configure` saved for this tool.
        provider = provider or get_provider_service(state, tool)
        state = _migrate_legacy_smart_routing(state)
        # Fetched before `configure_shared_state` because it decides whether this agent may launch
        # at all and whether the model discovery below can be skipped.
        # Bare `ucode` already fetched one to choose the agent; refetching would double the
        # control-plane round trip and any fallback warning it printed.
        coding_agent_config_feature_disabled = False
        if managed is None:
            managed, coding_agent_config_feature_disabled = _fetch_managed_config(state)
        _reject_managed_launch_source_options(
            managed,
            provider=explicit_provider,
            parent_schema=parent_schema,
        )
        if explicit_provider is not None and parent_schema is not None:
            raise RuntimeError("--provider and --model-location cannot be used together.")
        if parent_schema is not None and not is_valid_catalog_schema(parent_schema):
            raise RuntimeError("--model-location must be `<catalog>.<schema>`.")
        # Checked before discovery, which can take tens of seconds, so a blocked launch fails fast.
        _reject_disabled_agent(managed, tool)
        managed_provider = managed_provider_service(managed or {}, tool)
        managed_parent_schema = (
            managed_unity_catalog_location(managed or {}, tool)
            if tool in {"claude", "codex"} and not managed_provider
            else None
        )
        if managed_provider:
            provider = managed_provider
            parent_schema = None
        elif managed_parent_schema:
            # Managed UC discovery supersedes a developer's persisted provider without
            # rewriting it; the admin's location exists only for this launch.
            provider = None
            parent_schema = managed_parent_schema
        # Unmanaged Claude launches discover gateway models automatically; with no
        # provider or parent header the gateway defaults to system.ai. Managed
        # configs opt into discovery by selecting an MPS or Unity Catalog location.
        if tool == "claude" and (managed is None or managed_provider or managed_parent_schema):
            os.environ[claude_agent.GATEWAY_MODEL_DISCOVERY_ENV_VAR] = "1"
        # The environment switch remains a developer override; managed config is the workspace
        # policy equivalent and must take effect before launch options are computed.
        managed_smart_routing_enabled = _managed_smart_routing_enabled(managed, tool)
        smart_routing_enabled = smart_routing_enabled or managed_smart_routing_enabled
        # Discovery exists to find models and isn't needed for managed config that already names them.
        managed_models_known = managed_supplies_models(managed, tool)
        # Re-fetch model lists on every launch so newly-added Databricks
        # endpoints show up without a manual `ug configure` (and so that
        # tools like pi which read multiple model bundles never run on
        # stale state from before a tool added a new bundle). Under a provider
        # this heavy discovery is skipped (only a web-search model is fetched).
        configure_kwargs = {"custom_oauth": custom_oauth} if custom_oauth is not None else {}
        state = configure_shared_state(
            state["workspace"],
            profile=state.get("profile"),
            tools=[tool],
            skip_model_discovery=(
                bool(provider) or bool(managed_parent_schema) or managed_models_known
            ),
            skip_preflight=skip_preflight,
            **configure_kwargs,
        )
        # An admin-published managed config wins over the developer's own settings. Layered on after
        # `configure_shared_state`, whose returned state it overrides, and before the provider and
        # model are settled below — the two state files are never merged on disk.
        # Bare `ucode` already read one to choose the agent; refetching would double the round trip.
        if recommendation is None:
            recommendation = _fetch_budget_recommendation(state, managed)
        _note_recommended_agent(recommendation, tool)
        if managed is not None:
            state = resolve_state(managed, state, tool)
            unservable = managed_unservable_models(managed, tool)
            if unservable:
                print_warning(
                    f"Your workspace's managed config lists no {TOOL_SPECS[tool]['display']}-servable "
                    f"models ({', '.join(unservable)}); using your discovered models instead."
                )
        elif not coding_agent_config_feature_disabled:
            print_note("No managed coding agent config found; using your own settings")
        if provider and parent_schema is not None:
            raise RuntimeError("--provider and --model-location cannot be used together.")
        # Checked after the managed config settles `provider`: an admin-set provider must trip this
        # guard too, or routing would be persisted as on while a provider is active.
        if tool in CAN_USE_CACHED_CONFIG_AGENTS and smart_routing_enabled and provider:
            raise RuntimeError(
                f"{TOOL_SPECS[tool]['display']} smart routing cannot be enabled with "
                "--provider. Launch without a Model Provider Service and try again."
            )
        # Validate the provider service before launching — it must exist, be a
        # provider type this tool can route to (e.g. claude can't use an OpenAI
        # or Foundry service), and, for Bedrock, expose Claude models to pin.
        # Gemini is exempt: it validates the service and resolves its target in a single
        # lookup via resolve_gemini_provider_model (below), and uses no family model map.
        provider_models = None
        picker_catalog = None
        relayed = False
        coding_agent_config_defaults = (
            managed_claude_family_models(managed) or {}
            if tool == "claude" and managed is not None
            else {}
        )
        managed_claude_uc_without_defaults = (
            tool == "claude"
            and managed is not None
            and bool(managed_parent_schema)
            and managed_default_model(managed, tool) is None
            and not coding_agent_config_defaults
        )
        if provider and tool != "gemini":
            provider_models, error, relayed = resolve_provider_models(tool, state, provider)
            if error:
                if managed is not None and provider == managed_provider_service(managed, tool):
                    # Clear error if the admin has Unity Catalog grants the developer doesn't.
                    raise RuntimeError(
                        f"Your admin's managed config specifies provider {provider} for "
                        f"{TOOL_SPECS[tool]['display']}, which can't be used: {error}"
                    )
                raise RuntimeError(error)
            # A managed config launch uses exactly what the admin authored: pin Claude's family
            # models from the manifest's slots rather than the versions resolve_provider_models
            # re-derived from the service's live targets. The developer-configured path keeps that
            # re-derivation (see resolve_provider_models). Only when the manifest actually selected
            # this provider for claude, and authored something to pin.
            if (
                tool == "claude"
                and managed is not None
                and provider == managed_provider_service(managed, tool)
            ):
                authored = managed_provider_family_models(managed)
                if authored:
                    provider_models = authored
                    coding_agent_config_defaults = authored
        elif managed_claude_uc_without_defaults:
            token = get_databricks_token(state["workspace"], state.get("profile"))
            picker_catalog = list_anthropic_model_catalog(
                state["workspace"], token, parent_schema=managed_parent_schema
            )
            error = picker_catalog.error_msg
            if error:
                raise RuntimeError(
                    f"Could not discover Claude models for managed Unity Catalog location "
                    f"{managed_parent_schema}: {error}"
                )
        # The router's per-launch pick for the root session. Codex pins it as the
        # resolved model; claude pins it via ANTHROPIC_MODEL (route_root_model).
        route_root_model = None
        managed_model = None
        relayed_forward_model = None  # forwarded to Claude Code's --model for a relayed provider
        if provider or managed_parent_schema:
            # Routing through a Model Provider Service pins no Databricks model;
            # managed UC discovery likewise lets the agent select from the parent schema. Skip model
            # resolution, which would otherwise fail when global discovery found no models.
            resolved_model = None
            managed_source_model = (
                managed_launch_model(managed or {}, recommendation, tool)
                if tool == "claude" and (managed_provider or managed_parent_schema)
                else None
            )
            if tool == "claude" and managed_parent_schema:
                # Native discovery supplies the catalog, but the managed policy still controls
                # which model Claude starts on.
                route_root_model = managed_source_model
            provider_launch_model = model
            if tool == "claude" and managed_provider:
                # A CLI model still wins, followed by the budget recommendation and the managed
                # default. Unmanaged providers retain their existing target-selection behavior.
                provider_launch_model = provider_launch_model or managed_source_model
            if provider and tool == "claude" and (provider_launch_model or provider_models):
                if relayed:
                    # Resolve against a curated allowlist so the forwarded id is one the gateway
                    # allows; an allow_all relay declares none, so forward as-is.
                    relayed_forward_model = (
                        resolve_provider_launch_model(provider_launch_model, provider_models)
                        if provider_models
                        else provider_launch_model
                    )
                else:
                    route_root_model = resolve_provider_launch_model(
                        provider_launch_model, provider_models or {}
                    )
            if provider and tool == "gemini":
                # Gemini is the exception: the request still names a concrete model
                # in the URL, so pin one of the service's targets (--model or default).
                resolved_model, gemini_error = resolve_gemini_provider_model(state, provider, model)
                if gemini_error:
                    raise RuntimeError(gemini_error)
        else:
            # A managed default_model is the model the admin wants sessions to start on, so it goes
            # in as the explicit model rather than being applied afterwards: for codex the proto has
            # no model list at all, so passing it here is the only way a launch succeeds when the
            # workspace's own discovery turned up nothing.
            managed_model = (
                managed_launch_model(managed, recommendation, tool) if managed is not None else None
            )
            state, resolved_model = resolve_launch_model(tool, state, managed_model)
            # The admin's model outranks a smart-routing pick too. Claude only launches on it when
            # pinned as ANTHROPIC_MODEL (route_root_model); other agents take `resolved_model`,
            # which already holds it from resolve_launch_model above.
            if managed_model:
                if tool == "claude":
                    route_root_model = managed_model
                else:
                    resolved_model = managed_model
            # An explicit `--model` is the user's own choice and outranks everything above (managed
            # default, smart-routing pick). Non-claude agents take it as the resolved model, which
            # Codex keeps an explicit --model in ctx.args and passes it to its CLI verbatim.
            if model and tool != "claude":
                resolved_model = model
        state = configure_tool(
            tool,
            state,
            resolved_model,
            provider=provider,
            provider_models=provider_models,
            picker_catalog=picker_catalog,
            relayed=relayed,
            route_root_model=route_root_model,
            # Claude's explicit model is launch-scoped and is passed through LaunchOptions below.
            custom_model=None,
            coding_agent_config_defaults=coding_agent_config_defaults,
            parent_schema=parent_schema,
        )
        if picker_catalog and picker_catalog.model_ids:
            # Claude re-adds an out-of-catalog saved model to /model even when built-ins are
            # replaced. Keep the managed catalog launch-scoped and leave the user's settings alone.
            state["_claude_launch_picker_models"] = picker_catalog.model_ids
        # Relayed = a Claude subscription: forward the model to Claude Code's own flag, like `-- --model X`.
        should_forward_relayed_model = (
            tool == "claude"
            and provider
            and relayed
            and relayed_forward_model
            and not forwarded_model
        )
        if should_forward_relayed_model:
            ctx.args = ["--model", relayed_forward_model, *ctx.args]
            forwarded_model = relayed_forward_model
        print_section(_launch_title(tool))
        if provider:
            print_kv("Provider", provider)
        if tool in CAN_USE_CACHED_CONFIG_AGENTS and smart_routing_enabled and not provider:
            print_note(
                f"{TOOL_SPECS[tool]['display']} may require one-time hook review. Open "
                "`/hooks` and trust the ug routing hooks if prompted."
            )
        if tool in ("gemini", "opencode", "copilot", "pi"):
            print_note(
                f"{TOOL_SPECS[tool]['display']} token refresh is managed automatically "
                f"every 30 minutes while the session is running."
            )
        if recommendation is not None:
            _print_budget_panel(recommendation, tool, managed)
        # The managed config's MCP servers and skills are both applied at `ug configure`, not here,
        # so the launch hot path makes no per-launch discovery calls for them.
        if tool == "claude":
            if provider:
                state["_claude_launch_provider"] = provider
        elif tool == "codex":
            if provider:
                state["_codex_launch_provider"] = provider
            elif parent_schema:
                state["_codex_launch_parent_schema"] = parent_schema
        launch_options = _launch_options(
            tool,
            ctx.args,
            smart_routing_enabled=smart_routing_enabled,
            explicit_prompt=explicit_prompt,
            # Only a developer's explicit model disables routing. A managed default is the
            # initial/fallback model and still participates in a routed session.
            user_pinned_model=model or forwarded_model,
            provider=provider,
        )
        print_success(f"Starting {TOOL_SPECS[tool]['display']}")
        with _managed_smart_routing_environment(managed, tool):
            launch_agent(tool, state, ctx.args, options=launch_options)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


# Launch-only escape hatch for managed/headless launchers (e.g. omnigent) that
# have already run `ug configure`: skip the ~5-10s per-launch auth + AI
# Gateway re-validation, plus the Databricks CLI minimum-version check (whose
# `databricks aitools` floor otherwise false-positives on a usable public-preview
# build). Distinct from the configure-only `--skip-validate`, which skips the
# model smoke test.
SkipPreflightOption = Annotated[
    bool,
    typer.Option(
        "--skip-preflight",
        help="Skip the per-launch Databricks auth + AI Gateway re-validation (and the "
        "Databricks CLI minimum-version check), trusting a prior `ug configure`.",
    ),
]

REFRESH_HELP = (
    "Refresh Databricks auth, gateway, models, managed config, and agent configuration before "
    "launching."
)

# Target this launch at a specific workspace, auto-configuring (and logging in)
# if it hasn't been set up yet — so a launch needs no prior `ug configure`.
WorkspaceOption = Annotated[
    str | None,
    typer.Option(
        "--workspace",
        help="Databricks workspace URL to launch against; sets up and authenticates it "
        "if not already configured.",
    ),
]


_PROMPT_SUFFIX_KEY = "ucode_explicit_prompt_suffix"


class _PromptAwareCommand(TyperCommand):
    """Record an agent's ``--`` prompt separator before Click removes it."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        try:
            separator = args.index("--")
        except ValueError:
            pass
        else:
            ctx.meta[_PROMPT_SUFFIX_KEY] = tuple(args[separator + 1 :])
        return super().parse_args(ctx, args)


def _has_explicit_prompt(ctx: typer.Context) -> bool:
    suffix = ctx.meta.get(_PROMPT_SUFFIX_KEY)
    if not isinstance(suffix, tuple):
        return False
    suffix_args = list(suffix)
    return ctx.args == suffix_args and len(suffix_args) <= 1


@app.callback(invoke_without_command=True)
def default(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the ug version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print config files without writing them. Uses the last saved managed "
            "config instead of fetching a fresh one.",
        ),
    ] = False,
    skip_preflight: SkipPreflightOption = False,
    workspace: WorkspaceOption = None,
) -> None:
    """Configure and launch coding agents through Databricks AI Gateway."""
    if ctx.invoked_subcommand is not None:
        return
    set_dry_run(dry_run)
    try:
        _launch_managed_default(
            ctx, dry_run=dry_run, skip_preflight=skip_preflight, workspace=workspace
        )
    except typer.Exit:
        # `typer.Exit` subclasses RuntimeError, so it has to be re-raised ahead of the handler
        # below. Otherwise a launch that already reported its own error is followed by
        # `print_err(str(exc))` printing the exit code — a bare, meaningless "ERROR 1".
        raise
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None


def _launch_managed_default(
    ctx: typer.Context,
    *,
    dry_run: bool,
    skip_preflight: bool,
    workspace: str | None,
) -> None:
    """Route bare ``ucode`` by whether the workspace publishes a managed config."""
    if workspace:
        set_current_workspace(normalize_workspace_url(workspace))
    state = load_state()
    current = state.get("workspace")
    if not current:
        console.print(ctx.get_help())
        return
    install_databricks_cli(skip_version_check=skip_preflight)
    apply_pat_environment(state)
    coding_agent_config_feature_disabled = False
    if dry_run:
        managed = load_managed_state(current)
    else:
        with spinner("Loading..."):
            managed, coding_agent_config_feature_disabled = refresh_managed_config(state)
    if coding_agent_config_feature_disabled:
        print_note(
            "Run `ug configure` to set up your coding agents, then launch one with "
            "`ug <agent>` (for example `ug claude`)."
        )
        return
    if not managed:
        _print_no_managed_config_guidance()
        return
    # The budget tier can move the org to a cheaper agent, so it outranks the config's
    # default_agent. Fetched here and handed to _launch_tool so it is read once per launch.
    recommendation = _fetch_budget_recommendation(state, managed)
    tool = recommended_agent(recommendation, managed) or next(
        iter(managed.get("enabled_agents") or {}), None
    )
    if not isinstance(tool, str) or not tool:
        raise RuntimeError(
            "Your workspace's managed config names no agent to launch. Ask an admin to set a "
            "default agent, or run `ug <agent>` directly."
        )
    _print_managed_summary(managed, state, tool, abridged=True)
    _launch_tool(
        tool,
        ctx,
        skip_preflight=skip_preflight,
        workspace_url=workspace,
        managed=managed,
        recommendation=recommendation,
    )


def _print_no_managed_config_guidance() -> None:
    """Point the developer at per-user configure when no managed config is published."""
    print_note(
        "No managed coding agent config is published for this workspace. Run `ug configure` to "
        "set up your coding agents, then launch one with `ug <agent>` (for example `ug claude`)."
    )


@app.command(
    "codex",
    cls=_PromptAwareCommand,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    rich_help_panel="Launch",
)
def codex_cmd(
    ctx: typer.Context,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="Route through a Unity Catalog Model Provider Service "
            "(<catalog>.<schema>.<name>). Skips Databricks model pinning; pass "
            "before any `--` separator.",
        ),
    ] = None,
    model_location: Annotated[
        str | None,
        typer.Option(
            "--model-location",
            help="Discover model services in `<catalog>.<schema>`. Example: main.default",
        ),
    ] = None,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help=REFRESH_HELP,
        ),
    ] = False,
    skip_preflight: SkipPreflightOption = False,
    workspace: WorkspaceOption = None,
    client_id: Annotated[
        str | None,
        typer.Option("--client-id", hidden=True, help="Custom OAuth client ID for Codex auth."),
    ] = None,
    redirect_url: Annotated[
        str | None,
        typer.Option(
            "--redirect-url", hidden=True, help="Custom OAuth callback URL for Codex auth."
        ),
    ] = None,
    scopes: Annotated[
        str | None,
        typer.Option("--scopes", hidden=True, help="Comma-separated custom OAuth scopes."),
    ] = None,
    enable_smart_routing_flag: Annotated[
        bool,
        typer.Option(
            "--enable-smart-routing",
            help="Enable AI Gateway model routing for Codex sessions and subagents.",
        ),
    ] = False,
) -> None:
    """Launch Codex via Databricks."""
    try:
        custom_oauth = _custom_oauth_config(client_id, redirect_url, scopes)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from exc
    with _smart_routing_v2_flag(enable_smart_routing_flag):
        with _disable_smart_routing_for_subcommand("codex", ctx):
            _launch_tool(
                "codex",
                ctx,
                provider=provider,
                refresh=refresh,
                skip_preflight=skip_preflight,
                workspace_url=workspace,
                parent_schema=model_location,
                custom_oauth=custom_oauth,
            )


@app.command(
    "claude",
    cls=_PromptAwareCommand,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    rich_help_panel="Launch",
)
def claude_cmd(
    ctx: typer.Context,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="Route through a Unity Catalog Model Provider Service "
            "(<catalog>.<schema>.<name>). Skips Databricks model pinning; pass "
            "before any `--` separator.",
        ),
    ] = None,
    model_location: Annotated[
        str | None,
        typer.Option(
            "--model-location",
            help="Discover model services in `<catalog>.<schema>`. Example: main.default",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Launch on a specific Databricks model id (e.g. a UC "
            "`<catalog>.<schema>.<name>`). Pinned via ANTHROPIC_MODEL so the gateway "
            "resolves it — unlike Claude Code's own --model, which rejects non-catalog ids. "
            "With --provider, pass a family (opus/sonnet/haiku) or a target the service allows to "
            "start on that tier instead of Claude Code's opus default. Pass before any `--` separator.",
        ),
    ] = None,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help=REFRESH_HELP,
        ),
    ] = False,
    skip_preflight: SkipPreflightOption = False,
    workspace: WorkspaceOption = None,
    client_id: Annotated[
        str | None,
        typer.Option("--client-id", hidden=True, help="Custom OAuth client ID for apiKeyHelper."),
    ] = None,
    redirect_url: Annotated[
        str | None,
        typer.Option(
            "--redirect-url", hidden=True, help="Custom OAuth callback URL for apiKeyHelper."
        ),
    ] = None,
    scopes: Annotated[
        str | None,
        typer.Option("--scopes", hidden=True, help="Comma-separated custom OAuth scopes."),
    ] = None,
    enable_smart_routing_flag: Annotated[
        bool,
        typer.Option(
            "--enable-smart-routing",
            help="Enable AI Gateway model routing for Claude Code sessions and subagents.",
        ),
    ] = False,
) -> None:
    """Launch Claude Code via Databricks."""
    try:
        custom_oauth = _custom_oauth_config(client_id, redirect_url, scopes)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from exc
    with _smart_routing_v2_flag(enable_smart_routing_flag):
        with _disable_smart_routing_for_subcommand("claude", ctx):
            _launch_tool(
                "claude",
                ctx,
                provider=provider,
                model=model,
                refresh=refresh,
                skip_preflight=skip_preflight,
                workspace_url=workspace,
                parent_schema=model_location,
                custom_oauth=custom_oauth,
            )


@app.command(
    "gemini",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    rich_help_panel="Launch",
)
def gemini_cmd(
    ctx: typer.Context,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="Route through a Unity Catalog Model Provider Service "
            "(<catalog>.<schema>.<name>) that serves a Gemini model. Pass before any "
            "`--` separator.",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Model to launch on. Under --provider, selects which of the service's "
            "target models to use. Pass before any `--` separator.",
        ),
    ] = None,
    skip_preflight: SkipPreflightOption = False,
) -> None:
    """Launch Gemini CLI via Databricks."""
    _launch_tool("gemini", ctx, provider=provider, model=model, skip_preflight=skip_preflight)


@app.command(
    "opencode",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    rich_help_panel="Launch",
)
def opencode_cmd(
    ctx: typer.Context,
    skip_preflight: SkipPreflightOption = False,
) -> None:
    """Launch OpenCode via Databricks."""
    _launch_tool("opencode", ctx, skip_preflight=skip_preflight)


@app.command(
    "copilot",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    rich_help_panel="Launch",
)
def copilot_cmd(
    ctx: typer.Context,
    skip_preflight: SkipPreflightOption = False,
) -> None:
    """Launch GitHub Copilot CLI via Databricks."""
    _launch_tool("copilot", ctx, skip_preflight=skip_preflight)


@app.command(
    "pi",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    rich_help_panel="Launch",
)
def pi_cmd(
    ctx: typer.Context,
    skip_preflight: SkipPreflightOption = False,
) -> None:
    """Launch Pi coding agent via Databricks."""
    _launch_tool("pi", ctx, skip_preflight=skip_preflight)


@app.command(
    "cursor",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    rich_help_panel="Launch",
)
def cursor_cmd(ctx: typer.Context) -> None:
    """Launch Cursor Agent.

    Cursor is MCP-only: `cursor-agent` runs models on your own Cursor account, so
    ug configures no models for it. Its Databricks MCP servers (added via
    `ug mcp add`) run `ug mcp-proxy`, which authenticates itself — so
    this command is a thin convenience wrapper over `cursor-agent`, kept for
    symmetry with the other `ug <agent>` launchers.
    """
    from ucode.agents import cursor

    try:
        if not shutil.which(cursor.CURSOR_BINARY):
            raise RuntimeError(
                f"`{cursor.CURSOR_BINARY}` was not found on PATH. Install Cursor Agent "
                "(https://cursor.com/cli), then re-run `ug cursor`."
            )
        print_section("Unity Gateway with Cursor")
        print_note(
            "Cursor runs models on your Cursor account; its Databricks MCP servers "
            "authenticate through `ug mcp-proxy`."
        )
        print_success("Starting Cursor Agent")
        cursor.launch(load_state(), ctx.args)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


@configure_app.callback(invoke_without_command=True)
def configure(
    ctx: typer.Context,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print config files without writing them.")
    ] = False,
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            help="Configure only the named agent (e.g. claude, codex, gemini, opencode, copilot, pi).",
        ),
    ] = None,
    agents: Annotated[
        str | None,
        typer.Option(
            "--agents",
            help="Configure a comma-separated list of agents without prompting (e.g. claude,codex).",
        ),
    ] = None,
    workspace: Annotated[
        str | None,
        typer.Option(
            "--workspace",
            help="Configure a single workspace without prompting. "
            "Defaults to the UG_WORKSPACE environment variable when set.",
        ),
    ] = None,
    workspaces: Annotated[
        str | None,
        typer.Option(
            "--workspaces",
            hidden=True,
            help="Deprecated alias of --workspace, kept for backward compatibility. "
            "Takes a single workspace URL.",
        ),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="Configure a single existing Databricks CLI profile without the "
            "workspace prompt. The profile's host from ~/.databrickscfg supplies the "
            "workspace URL. Auth behaves like --workspace: OAuth login is forced "
            "unless --use-pat is also passed.",
        ),
    ] = None,
    profiles: Annotated[
        str | None,
        typer.Option(
            "--profiles",
            hidden=True,
            help="Deprecated alias of --profile, kept for backward compatibility. "
            "Takes a single Databricks CLI profile.",
        ),
    ] = None,
    use_pat: Annotated[
        bool,
        typer.Option(
            "--use-pat",
            help="Authenticate with the personal access token stored in "
            "~/.databrickscfg for the selected profile instead of OAuth. "
            "Requires --profile; no interactive login is run. Intended for "
            "CI / headless environments.",
        ),
    ] = False,
    client_id: Annotated[
        str | None,
        typer.Option("--client-id", hidden=True, help="Custom OAuth client ID for apiKeyHelper."),
    ] = None,
    redirect_url: Annotated[
        str | None,
        typer.Option(
            "--redirect-url",
            hidden=True,
            help="Custom OAuth callback URL for apiKeyHelper.",
        ),
    ] = None,
    scopes: Annotated[
        str | None,
        typer.Option(
            "--scopes",
            hidden=True,
            help="Comma-separated custom OAuth scopes for apiKeyHelper.",
        ),
    ] = None,
    skip_validate: Annotated[
        bool,
        typer.Option(
            "--skip-validate",
            hidden=True,
            help="Deprecated and ignored: agent validation has been removed. "
            "Accepted for backward compatibility so existing scripts keep working.",
        ),
    ] = False,
    skip_unavailable: Annotated[
        bool,
        typer.Option(
            "--skip-unavailable",
            hidden=True,
            help="Deprecated and ignored: configure already skips unavailable agents. "
            "Accepted for backward compatibility.",
        ),
    ] = False,
    enable_databricks_ai_tools: Annotated[
        bool | None,
        typer.Option(
            "--enable-databricks-ai-tools/--disable-databricks-ai-tools",
            help="Install Databricks AI Tools (skills + plugins that teach agents to use "
            "Databricks) for the configured agents. Installation is configure-only and off "
            "by default; pass --enable-databricks-ai-tools to opt in. Skipped when your "
            "workspace has an admin-managed config.",
        ),
    ] = None,
    mcp: Annotated[
        str | None,
        typer.Option(
            "--mcp",
            help="Also register the given Databricks MCP service(s) for the configured "
            "coding agents, in one command. Pass a comma-separated list of fully-qualified "
            "names like `system.ai.slack`. Combine with --agents to set up an agent and its "
            "MCP servers together (e.g. `--agents claude --mcp system.ai.slack`); use without "
            "--agents for MCP-only clients such as Cursor.",
        ),
    ] = None,
    skip_upgrade: Annotated[
        bool,
        typer.Option(
            "--skip-upgrade",
            hidden=True,
            help="Deprecated and ignored: agents are updated only when required for "
            "compatibility. Accepted for backward compatibility.",
        ),
    ] = False,
    verbose: Annotated[
        str,
        typer.Option(
            "--verbose",
            help="Output verbosity: 'normal' (default) renders decorative panels; "
            "'low' prints terse single-line status instead.",
        ),
    ] = "normal",
) -> None:
    """Configure workspace URL and AI Gateway."""
    if ctx.invoked_subcommand is not None:
        return
    if verbose not in ("normal", "low"):
        print_err("--verbose must be one of: normal, low.")
        raise typer.Exit(2)
    set_dry_run(dry_run)
    set_verbosity(verbose)
    if not dry_run:
        # An explicit configure retries the managed-settings write that launches stopped prompting for.
        prior_state = load_state()
        if claude_agent.MANAGED_WRITE_UNAVAILABLE_STATE_KEY in prior_state:
            claude_agent.forget_managed_write_failure(prior_state)
            save_state(prior_state)
    try:
        custom_oauth = _custom_oauth_config(client_id, redirect_url, scopes)
        if custom_oauth is not None and use_pat:
            raise RuntimeError("--client-id cannot be combined with --use-pat.")
        install_databricks_cli()
        if agent is not None and agents is not None:
            raise RuntimeError("Use either --agent or --agents, not both.")
        # --workspaces / --profiles are deprecated aliases of the singular flags.
        if workspace is not None and workspaces is not None:
            raise RuntimeError("Use either --workspace or --workspaces, not both.")
        if profile is not None and profiles is not None:
            raise RuntimeError("Use either --profile or --profiles, not both.")
        workspace = workspace if workspace is not None else workspaces
        profile = profile if profile is not None else profiles
        if workspace is None and profile is None:
            workspace = os.environ.get("UG_WORKSPACE") or None
        if workspace is not None and profile is not None:
            raise RuntimeError("Use either --workspace or --profile, not both.")
        if use_pat and profile is None:
            raise RuntimeError(
                "--use-pat requires --profile. Pass the PAT-backed Databricks CLI "
                "profile explicitly, e.g. `ug configure --profile DEFAULT --use-pat`."
            )
        workspace_entries = _parse_workspace_option(workspace) if workspace is not None else None
        if profile is not None:
            workspace_entries = _parse_profile_option(profile)
        flag_driven_workspace = workspace_entries is not None
        # Only forward the opt-in flags when set so existing call expectations
        # (and defaults) stay unchanged for the common interactive path.
        skip_kwargs: dict = {}
        if use_pat:
            skip_kwargs["use_pat"] = True
        if enable_databricks_ai_tools is not None:
            skip_kwargs["databricks_ai_tools_enabled"] = enable_databricks_ai_tools
        if custom_oauth is not None:
            skip_kwargs["custom_oauth"] = custom_oauth
        # Set True only in the fully-interactive branch below; gates the optional
        # MCP setup prompt so flag-driven / scripted runs are never interrupted.
        fully_interactive = False
        combined_optional_setup = False
        if agent is not None:
            tool = normalize_tool(agent)
            install_tool_binary(
                tool,
                strict=True,
            )
            if workspace_entries is None:
                configure_workspace_command(tool, **skip_kwargs)
            else:
                configure_workspace_command(
                    tool,
                    workspaces=workspace_entries,
                    **skip_kwargs,
                )
        elif agents is not None:
            # Cursor is MCP-only (no model routing), so it can't go through the
            # model-agent configure path. Split it out: model agents configure
            # normally; cursor only needs workspace state established here, and
            # its MCP servers are added separately via `ug mcp add`
            # (which picks cursor up through MCP_ONLY_CLIENTS). If cursor is the
            # only agent, do a workspace-only configure so that a later `ug mcp
            # add` run has a current workspace to target.
            requested = [a.strip().lower() for a in agents.split(",") if a.strip()]
            wants_cursor = "cursor" in requested
            model_agent_names = ",".join(a for a in requested if a != "cursor")
            if model_agent_names:
                selected_tools = _parse_agents_option(model_agent_names)
                if workspace_entries is None:
                    configure_workspace_command(
                        selected_tools=selected_tools,
                        **skip_kwargs,
                    )
                else:
                    configure_workspace_command(
                        selected_tools=selected_tools,
                        workspaces=workspace_entries,
                        **skip_kwargs,
                    )
            elif wants_cursor:
                # Cursor-only: establish workspace state without the model picker.
                _configure_shared_workspace_states(
                    workspace_entries or [_prompt_for_configuration(None)],
                    tools=[],
                    force_login=not use_pat,
                    use_pat=use_pat,
                    custom_oauth=custom_oauth,
                )
            else:
                # Neither model agents nor cursor -> empty/invalid --agents list.
                _parse_agents_option(agents)
        elif mcp is not None:
            # MCP-only: `--mcp` without --agent(s) (e.g. Cursor, which isn't a
            # model agent, or adding MCP servers to an already-configured setup).
            # Configure just the workspace — no interactive agent picker — so the
            # `--mcp` registration below has a current workspace to target.
            if workspace_entries is None:
                workspace_entries = [_prompt_for_configuration(None)]
            _configure_shared_workspace_states(
                workspace_entries,
                tools=[],
                force_login=not use_pat,
                use_pat=use_pat,
                custom_oauth=custom_oauth,
            )
        else:
            # Tool binaries are installed after the user picks which agents
            # they want, in configure_workspace_command.
            combined_optional_setup = (
                not flag_driven_workspace and enable_databricks_ai_tools is None
            )
            if combined_optional_setup:
                skip_kwargs["offer_optional_setup"] = True
            if workspace_entries is None:
                configure_workspace_command(**skip_kwargs)
            else:
                configure_workspace_command(
                    workspaces=workspace_entries,
                    **skip_kwargs,
                )
            # Only the no-agent, no-workspace path is truly interactive (the user
            # picked agents/workspace via prompts); that's where we offer the MCP
            # step below. Flag-driven runs stay scriptable.
            fully_interactive = not flag_driven_workspace
        if mcp is not None:
            # The workspace + agents were just configured above, so the current
            # workspace state now lists the agents whose MCP configs we should
            # write. `--mcp` takes fully-qualified service names, which
            # `configure_mcp_command` locates and registers without a picker
            # (bare short names would need --location, which we don't accept here).
            services = {name.strip() for name in mcp.split(",") if name.strip()}
            if not services:
                raise RuntimeError(
                    "--mcp needs at least one fully-qualified MCP service name, e.g. "
                    "`--mcp system.ai.slack`."
                )
            bare = sorted(name for name in services if name.count(".") < 2)
            if bare:
                raise RuntimeError(
                    "--mcp names must be fully qualified `<catalog>.<schema>.<name>` "
                    f"(got: {', '.join(bare)}). Use `ug mcp add` for the "
                    "interactive picker."
                )
            configure_mcp_command(services=services)
        if (
            fully_interactive
            and not combined_optional_setup
            and not dry_run
            and prompt_yes_no("Configure MCP servers now?")
        ):
            configure_mcp_command()
    except typer.Exit:
        # `typer.Exit` subclasses RuntimeError, so it has to be re-raised ahead of the handler
        # below. Otherwise a clean exit (e.g. `_reject_configure_under_managed_config` under a
        # managed config) is followed by `print_err(str(exc))` printing the exit code — a bare,
        # meaningless "ERROR 0".
        raise
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        print_err("Interrupted.")
        raise typer.Exit(130) from None


@app.command("export", rich_help_panel="Manage")
def export_cmd(
    file_path: Annotated[
        str | None,
        typer.Option(
            "--file",
            "-f",
            help="Write the exported config JSON to this file (atomically) instead of stdout. "
            "The parent directory must already exist.",
        ),
    ] = None,
) -> None:
    """Export this workspace's managed coding-agent config as portable JSON.

    Serializes the local managed config to the external `CodingAgentConfig` proto-JSON format,
    with credentials and server-owned fields (resource name, workspace id, timestamps, user ids)
    excluded. Any user can run it; it makes no network calls and mutates no workspace or local
    state. Without --file the JSON is printed to stdout; diagnostics and errors go to stderr.
    """
    from ucode.managed_export import export_command

    try:
        export_command(file_path=file_path)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None


@app.command("status", rich_help_panel="Manage")
def status_cmd() -> None:
    """Show current workspace, tool configs, and live model availability."""
    try:
        status()
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None


@app.command("revert", rich_help_panel="Manage")
def revert_cmd() -> None:
    """Clear ug state and restore backed-up agent config files."""
    try:
        revert()
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None


@app.command("doctor", rich_help_panel="Manage")
def doctor_cmd() -> None:
    """Diagnose the local ug setup and offer to fix any problems found."""
    from ucode.doctor import doctor

    try:
        doctor()
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None


@app.command("usage", rich_help_panel="Usage")
def usage_cmd() -> None:
    """Show AI Gateway dollars spent and total budget."""
    try:
        install_databricks_cli()
        usage_report()
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None


@app.command("upgrade", rich_help_panel="Manage")
def upgrade_cmd() -> None:
    """Upgrade ug to the latest version from GitHub."""
    legacy_distribution = "ucode"
    current_distribution = "unity-gateway"
    git_url = "git+https://github.com/databricks/unity-gateway"
    installed_distribution = _installed_cli_distribution()
    upgrade_requirement = f"{installed_distribution} @ {git_url}"
    migrated = False
    legacy_removed = False

    print_section("Upgrade")
    print_kv("Source", git_url)
    print_kv("Installed distribution", installed_distribution)
    try:
        result = subprocess.run(
            ["uv", "tool", "install", "--reinstall", upgrade_requirement],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            if installed_distribution == legacy_distribution and _is_distribution_cutover(result):
                print_note(
                    "The package is now distributed as `unity-gateway`; migrating this installation."
                )
                subprocess.run(
                    ["uv", "tool", "uninstall", legacy_distribution],
                    check=True,
                )
                legacy_removed = True
                subprocess.run(
                    ["uv", "tool", "install", "--force", git_url],
                    check=True,
                )
                migrated = True
                installed_distribution = current_distribution
            else:
                detail = _upgrade_failure_detail(result)
                print_err(
                    f"Upgrade failed (exit code {result.returncode})"
                    f"{f': {detail}' if detail else '.'}"
                )
                print_note("The existing installation was left unchanged.")
                raise typer.Exit(1)

        if migrated:
            _verify_upgraded_commands()
    except typer.Exit:
        raise
    except FileNotFoundError:
        print_err("`uv` was not found on PATH. Install uv to upgrade ug.")
        raise typer.Exit(1) from None
    except subprocess.CalledProcessError as exc:
        if legacy_removed:
            print_err(
                "The legacy `ucode` tool was removed, but installing `unity-gateway` failed "
                f"(exit code {exc.returncode})."
            )
            print_note(f"Recover by running `uv tool install --force {git_url}`.")
        else:
            print_err(f"Upgrade failed (exit code {exc.returncode}); `ucode` was not removed.")
        raise typer.Exit(1) from None
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None

    if migrated:
        print_success("Migrated to `unity-gateway`; both `ug` and `ucode` are working")
    else:
        print_success(f"{installed_distribution} upgraded; both `ug` and `ucode` are working")


def _installed_cli_distribution() -> str:
    """Return the uv tool identity, preferring the post-cutover distribution."""
    for distribution_name in ("unity-gateway", "ucode"):
        try:
            metadata.version(distribution_name)
        except metadata.PackageNotFoundError:
            continue
        return distribution_name
    # Source checkouts and unusual installers may expose neither distribution.
    # Use the current distribution name and let uv report an actionable error.
    return "unity-gateway"


def _is_distribution_cutover(result: subprocess.CompletedProcess[str]) -> bool:
    """Recognize uv's specific error when source metadata changes distribution name."""
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return all(
        marker in output
        for marker in (
            "metadata name",
            "unity-gateway",
            "does not match given name",
            "ucode",
        )
    )


def _upgrade_failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "").strip()


def _verify_upgraded_commands() -> None:
    """Ensure both compatibility entry points were installed and can start."""
    for command in ("ug", "ucode"):
        executable = shutil.which(command)
        if executable is None:
            raise RuntimeError(
                f"Upgrade completed, but `{command}` is not available on PATH. "
                "Reinstall Unity Gateway and ensure the uv tool bin directory is on PATH."
            )
        result = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = _upgrade_failure_detail(result)
            raise RuntimeError(
                f"Upgrade completed, but `{command} --version` failed"
                f"{f': {detail}' if detail else '.'}"
            )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
