"""Stdio MCP servers that coding agents start with every session.

Claude Code starts `ug mcp web-search` when ug replaces its web search, and every agent starts one
`ug mcp-proxy` per configured Databricks MCP server. `ucode.entrypoint` runs them through `run`
without loading the full CLI; `ucode.cli` registers the same functions, so parsing and help are
identical.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

# Keep in sync with `ucode.entrypoint._AGENT_HELPERS`, which can't import this module.
COMMANDS = ("mcp web-search", "mcp-proxy")


def mcp_web_search_cmd(
    managed_by_ucode: Annotated[
        bool, typer.Option("--managed-by-ucode", help="Identify a ug-generated registration.")
    ] = False,
    external_provider_override: Annotated[
        bool, typer.Option("--external-provider-override", hidden=True)
    ] = False,
    show_capabilities: Annotated[
        bool, typer.Option("--capabilities", help="Print the launcher contract as JSON and exit.")
    ] = False,
) -> None:
    """Run the web_search MCP server over stdio. Invoked as a subprocess by Claude Code."""
    from ucode.mcp_web_search import capabilities, serve

    if show_capabilities:
        print(json.dumps(capabilities()))
        return
    serve(
        managed_by_ucode=managed_by_ucode,
        external_provider_override=external_provider_override,
    )


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
    workspace_id: Annotated[str | None, typer.Option("--workspace-id", hidden=True)] = None,
) -> None:
    """Bridge a coding agent's stdio MCP transport to a Databricks MCP endpoint.

    Each configured client spawns this as a local stdio MCP server (see
    `ug mcp add`); it forwards messages to ``--url`` and injects a
    freshly-minted token on every upstream request, so it never expires
    mid-session. Not meant for interactive use — the agent manages this
    process's lifecycle."""
    from ucode.mcp_proxy import serve
    from ucode.state import load_saved_state
    from ucode.ui import print_err
    from ucode.workspace import pin

    state = load_saved_state()
    workspace = host or state.get("workspace")
    if not workspace:
        print_err("No workspace configured. Run `ug configure` first.")
        raise typer.Exit(1)
    if workspace_id:
        pin(workspace, workspace_id)
    profile = profile or state.get("profile")
    serve(url, workspace, profile, use_pat=use_pat or bool(state.get("use_pat")))


def run(argv: list[str]) -> None:
    """Run `ug mcp web-search ...` or `ug mcp-proxy ...` with only those commands registered."""
    app = typer.Typer(add_completion=False)
    mcp_app = typer.Typer(add_completion=False, no_args_is_help=True)
    mcp_app.command("web-search")(mcp_web_search_cmd)
    app.add_typer(mcp_app, name="mcp")
    app.command("mcp-proxy", hidden=True)(mcp_proxy_cmd)
    app(args=argv, prog_name="ug")
