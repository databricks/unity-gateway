"""Best-effort Desktop configuration and cleanup for CLI commands."""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections import Counter

from ucode.agents.copilot import claude_model_name
from ucode.agents.desktop.claude import (
    claude_desktop_directory,
    revert_claude_desktop,
)
from ucode.agents.desktop.claude import (
    configure_claude_desktop as write_claude_desktop_profile,
)
from ucode.agents.desktop.cowork import configure_code, configure_cowork, revert_code, revert_cowork
from ucode.config_io import is_dry_run
from ucode.constants import MODEL_PROVIDER_SERVICE_HEADER, MODEL_SERVICE_PARENT_SCHEMA_HEADER
from ucode.databricks import build_tool_base_url, get_databricks_token, list_anthropic_model_catalog
from ucode.managed_files import ManagedFileWriteUnavailable, machine_wide_only
from ucode.state import get_provider_service
from ucode.string_utils import model_picker_label
from ucode.ui import print_note, print_success, print_warning
from ucode.workspace import workspace_headers, workspace_id_args

_CLAUDE_MODEL_PATTERN = re.compile(r"\bclaude-", re.IGNORECASE)


def configure_claude_desktop(state: dict, *, parent_schema: str | None = None) -> None:
    if sys.platform not in ("darwin", "win32", "linux"):
        return
    try:
        if machine_wide_only():
            if sys.platform != "darwin":
                print_note(
                    "Skipped Claude Desktop: --global MDM provisioning currently supports macOS only."
                )
                return
            from ucode.agents.desktop import mdm

            if state.get("use_pat") or state.get("custom_oauth"):
                raise RuntimeError(
                    "Desktop MDM provisioning does not support PAT/custom OAuth profiles."
                )
            mdm.configure(state["workspace"], state.get("profile"))
            if not is_dry_run():
                print_success(
                    "Claude Desktop MDM deployment configured. Users authenticate when they open Desktop."
                )
            return
        if is_dry_run():
            print_note("Would configure Claude Desktop alongside Claude Code.")
            return
        _configure_claude_desktop_user_level(state, parent_schema=parent_schema)
    except Exception as exc:  # noqa: BLE001 -- Desktop must not block Claude Code setup
        print_warning(f"Could not configure Claude Desktop: {exc}. Claude Code setup is unchanged.")


def configure_claude_code_plugin(mcp_servers: list[dict]) -> None:
    if sys.platform not in ("darwin", "win32", "linux"):
        return
    if machine_wide_only():
        return
    if is_dry_run():
        print_note("Would configure Claude Code connector guidance.")
        return
    try:
        configured = configure_code(mcp_servers)
    except Exception as exc:  # noqa: BLE001 -- Plugin setup must not block MCP configuration
        print_warning(f"Could not configure Claude Code connector plugin: {exc}.")
        return
    if configured:
        print_success("Claude Code connector plugin configured. Start a new Code session.")


def configure_claude_cowork(
    state: dict, mcp_servers: list[dict], *, sync_plugin: bool = True
) -> None:
    if sys.platform not in ("darwin", "win32", "linux"):
        return
    if machine_wide_only():
        # The Cowork plugin is installed for the user's own Claude account.
        return
    if is_dry_run():
        print_note("Would configure Claude Cowork workspace skills and MCP servers.")
        return
    try:
        configured = configure_cowork(state, mcp_servers, sync_plugin=sync_plugin)
    except Exception as exc:  # noqa: BLE001 -- Cowork must not block Claude Code setup
        print_warning(
            f"Could not configure Claude Desktop workspace plugin: {exc}. Claude Code setup is unchanged."
        )
        return
    if configured:
        print_success("Claude Desktop workspace plugin configured. Quit and reopen Claude Desktop.")


def revert_claude_cowork() -> str:
    if sys.platform not in ("darwin", "win32", "linux") or is_dry_run():
        return "unchanged"
    try:
        return "removed" if revert_cowork() else "unchanged"
    except Exception as exc:  # noqa: BLE001 -- Preserve other cleanup operations on failure
        print_warning(f"Could not revert Claude Cowork: {exc}. Cowork setup was retained.")
        return "failed"


def revert_claude_code_plugin() -> str:
    if sys.platform not in ("darwin", "win32", "linux") or is_dry_run():
        return "unchanged"
    try:
        return "removed" if revert_code() else "unchanged"
    except Exception as exc:  # noqa: BLE001 -- Preserve other cleanup operations on failure
        print_warning(f"Could not revert Claude Code connector plugin: {exc}.")
        return "failed"


def _configure_claude_desktop_user_level(state: dict, *, parent_schema: str | None) -> None:
    directory = claude_desktop_directory()
    if directory is None:
        raise RuntimeError("Claude Desktop profile configuration is unavailable on this platform.")
    if sys.platform != "darwin" and not directory.parent.is_dir():
        raise RuntimeError("Open Claude Desktop once, then run ug configure again.")
    relayed = bool(state.get("claude_relayed"))
    claude_binary = None
    if relayed:
        claude_binary = shutil.which("claude")
        if not claude_binary or not os.path.isabs(claude_binary):
            raise RuntimeError("Install Claude Code before configuring subscription relay.")
    if state.get("custom_oauth") or state.get("use_pat"):
        raise RuntimeError("Desktop currently requires a Databricks CLI OAuth profile.")
    workspace = state.get("workspace")
    profile = state.get("profile")
    if not isinstance(workspace, str) or not workspace:
        raise RuntimeError("No Databricks workspace is configured.")
    if not isinstance(profile, str) or not profile:
        raise RuntimeError("Select an explicit Databricks CLI profile and run ug configure again.")

    settings = resolve_gateway_profile(state, parent_schema=parent_schema)

    write_claude_desktop_profile(
        **settings,
        helper_command=os.path.abspath(sys.executable),
        helper_args=[
            "-m",
            "ucode.cli",
            "claude-desktop-auth",
            "--host",
            workspace,
            "--profile",
            profile,
            *(["--relay", "--claude-binary", claude_binary] if claude_binary else []),
        ],
        otel_headers_command=[
            os.path.abspath(sys.executable),
            "-m",
            "ucode.cli",
            "otel-headers",
            "--host",
            workspace,
            *workspace_id_args(workspace),
            "--profile",
            profile,
        ],
        profile_key=profile,
    )
    print_success("Claude Desktop gateway profile configured. Quit and reopen Claude Desktop.")


def resolve_gateway_profile(
    state: dict, *, parent_schema: str | None = None, token: str | None = None
) -> dict:
    workspace = state["workspace"]
    profile = state.get("profile")
    provider = None if parent_schema else get_provider_service(state, "claude")
    static_models = (
        state.get("claude_static_models") if not provider and not parent_schema else None
    )
    token = token if token is not None else get_databricks_token(workspace, profile)
    catalog = list_anthropic_model_catalog(
        workspace, token, provider=provider, parent_schema=parent_schema
    )
    if static_models:
        model_ids = static_models
    else:
        if not catalog.model_ids:
            raise RuntimeError(catalog.error_msg or "Gateway model discovery returned no models.")
        model_ids = catalog.model_ids

    model_ids = [model_id for model_id in model_ids if _CLAUDE_MODEL_PATTERN.search(model_id)]
    if not model_ids:
        raise RuntimeError(
            "No Claude-family models found for Desktop. "
            "Add a Claude model to your model source and run ug configure again."
        )

    display_names = {}
    for model_id in model_ids:
        label = catalog.model_id_to_display_name.get(model_id)
        if not label or label == model_id:
            label = model_picker_label(claude_model_name(model_id))
        display_names[model_id] = label
    label_counts = Counter(display_names.values())
    display_names = {
        model_id: model_id if label_counts[label] > 1 else label
        for model_id, label in display_names.items()
    }

    headers = {"x-databricks-use-coding-agent-mode": "true"}
    if provider:
        headers[MODEL_PROVIDER_SERVICE_HEADER] = provider
    elif parent_schema:
        headers[MODEL_SERVICE_PARENT_SCHEMA_HEADER] = parent_schema
    for name, value in {
        **(state.get("claude_http_headers") or {}),
        **workspace_headers(workspace),
    }.items():
        existing = next((key for key in headers if key.casefold() == name.casefold()), None)
        if existing:
            del headers[existing]
        headers[name] = value

    return {
        "model_ids": model_ids,
        "model_id_to_display_name": display_names,
        "base_url": build_tool_base_url("claude", workspace.rstrip("/")),
        "custom_headers": headers,
        "otel_endpoint": (
            f"{workspace.rstrip('/')}/ai-gateway/otel" if state.get("claude_otel_tracing") else None
        ),
    }


def revert_claude_desktop_mdm() -> str:
    if sys.platform != "darwin":
        return "unchanged"
    from ucode.agents.desktop import mdm

    try:
        return "removed" if mdm.remove() else "unchanged"
    except ManagedFileWriteUnavailable as exc:
        print_warning(
            f"Claude Desktop MDM is installed machine-wide; an administrator must run "
            f"`ug revert` to remove it ({exc})."
        )
        return "skipped"
    except Exception as exc:  # noqa: BLE001 -- Preserve other revert operations on failure
        print_warning(
            f"Could not revert Claude Desktop MDM: {exc}. Fix it, then run ug revert again."
        )
        return "failed"


def revert_desktop() -> str:
    if sys.platform not in ("darwin", "win32", "linux") or is_dry_run():
        return "unchanged"
    results = [revert_claude_code_plugin(), revert_claude_cowork(), revert_claude_desktop_mdm()]
    if claude_desktop_directory() is None:
        if "failed" in results:
            return "failed"
        if "removed" in results:
            return "removed"
        return "skipped" if "skipped" in results else "unchanged"
    try:
        result = revert_claude_desktop()
    except Exception as exc:  # noqa: BLE001 -- Preserve other revert operations on failure
        print_warning(f"Could not revert Claude Desktop: {exc}. Desktop setup was retained.")
        return "failed"
    native_result = "removed" if result.changed else "unchanged"
    if "failed" in results:
        return "failed"
    if "removed" in (native_result, *results):
        return "removed"
    return "skipped" if "skipped" in results else "unchanged"
