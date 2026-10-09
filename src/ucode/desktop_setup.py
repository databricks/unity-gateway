"""Best-effort Desktop setup after successful Claude Code configuration."""

from __future__ import annotations

import os
import sys

from ucode.agents.claude_desktop import (
    claude_desktop_directory,
    configure_claude_desktop,
    revert_claude_desktop,
)
from ucode.config_io import is_dry_run
from ucode.constants import MODEL_PROVIDER_SERVICE_HEADER, MODEL_SERVICE_PARENT_SCHEMA_HEADER
from ucode.databricks import build_tool_base_url, get_databricks_token, list_anthropic_model_catalog
from ucode.state import get_provider_service
from ucode.ui import print_note, print_success, print_warning


def configure_desktop_after_claude(state: dict, *, parent_schema: str | None = None) -> None:
    if sys.platform not in ("darwin", "win32"):
        return
    if is_dry_run():
        print_note("Would configure Claude Desktop alongside Claude Code.")
        return
    try:
        _configure_desktop(state, parent_schema=parent_schema)
    except Exception as exc:  # noqa: BLE001 -- Desktop must not block Claude Code setup
        print_warning(f"Could not configure Claude Desktop: {exc}. Claude Code setup is unchanged.")


def _configure_desktop(state: dict, *, parent_schema: str | None) -> None:
    if claude_desktop_directory() is None:
        raise RuntimeError("The native Windows profile location has not been validated yet.")
    if state.get("claude_relayed"):
        raise RuntimeError("Anthropic subscription relay is not supported for Desktop yet.")
    if state.get("custom_oauth") or state.get("use_pat"):
        raise RuntimeError("Desktop currently requires a Databricks CLI OAuth profile.")
    workspace = state.get("workspace")
    profile = state.get("profile")
    if not isinstance(workspace, str) or not workspace:
        raise RuntimeError("No Databricks workspace is configured.")
    if not isinstance(profile, str) or not profile:
        raise RuntimeError("Select an explicit Databricks CLI profile and run ug configure again.")

    provider = None if parent_schema else get_provider_service(state, "claude")
    static_models = (
        state.get("claude_static_models") if not provider and not parent_schema else None
    )
    if static_models:
        model_ids = static_models
    else:
        token = get_databricks_token(workspace, profile)
        catalog = list_anthropic_model_catalog(
            workspace, token, provider=provider, parent_schema=parent_schema
        )
        if not catalog.model_ids:
            raise RuntimeError(catalog.error_msg or "Gateway model discovery returned no models.")
        model_ids = catalog.model_ids

    headers = {"x-databricks-use-coding-agent-mode": "true"}
    if provider:
        headers[MODEL_PROVIDER_SERVICE_HEADER] = provider
    elif parent_schema:
        headers[MODEL_SERVICE_PARENT_SCHEMA_HEADER] = parent_schema
    for name, value in (state.get("claude_http_headers") or {}).items():
        existing = next((key for key in headers if key.casefold() == name.casefold()), None)
        if existing:
            del headers[existing]
        headers[name] = value

    result = configure_claude_desktop(
        workspace,
        model_ids=model_ids,
        base_url=build_tool_base_url("claude", workspace.rstrip("/")),
        helper_command=os.path.abspath(sys.executable),
        helper_args=[
            "-m",
            "ucode.cli",
            "claude-desktop-auth",
            "--host",
            workspace,
            "--profile",
            profile,
        ],
        custom_headers=headers,
        profile_key=profile,
    )
    for warning in result.warnings:
        print_warning(warning)
    print_success("Claude Desktop gateway profile configured. Quit and reopen Claude Desktop.")


def revert_desktop(state: dict) -> str:
    if sys.platform not in ("darwin", "win32") or is_dry_run():
        return "unchanged"
    if claude_desktop_directory() is None:
        return "unchanged"
    workspace = state.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        return "unchanged"
    try:
        result = revert_claude_desktop(workspace, profile_key=state.get("profile"))
    except Exception as exc:  # noqa: BLE001 -- Preserve other revert operations and Desktop edits
        print_warning(f"Could not revert Claude Desktop: {exc}. Ownership records were retained.")
        return "failed"
    for warning in result.warnings:
        print_warning(warning)
    if result.drifted:
        print_warning("Claude Desktop user edits were preserved during revert.")
    return "restored" if result.changed else "unchanged"
