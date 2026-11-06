"""Stdio MCP servers that coding agents start with every session.

Claude Code starts `ug mcp web-search` when ug replaces its web search. `ucode.entrypoint` runs
it through `run` without loading the full CLI; `ucode.cli` registers the same function, so parsing
and help are identical. Proxy relays keep their dedicated lightweight parser in mcp_proxy_runtime.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

# Keep in sync with `ucode.entrypoint._AGENT_HELPERS`, which can't import this module.
COMMANDS = ("mcp web-search",)


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
    """Bridge a coding agent's stdio MCP transport to a Databricks MCP endpoint."""
    from ucode.mcp_proxy_runtime import run_proxy

    run_proxy(url, host, profile, use_pat, workspace_id)


def run(argv: list[str]) -> None:
    """Run `ug mcp web-search ...` with only that command registered."""
    app = typer.Typer(add_completion=False)
    mcp_app = typer.Typer(add_completion=False, no_args_is_help=True)
    mcp_app.command("web-search")(mcp_web_search_cmd)
    app.add_typer(mcp_app, name="mcp")
    app(args=argv, prog_name="ug")
