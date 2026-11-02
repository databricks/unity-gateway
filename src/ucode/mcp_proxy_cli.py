"""MCP proxy command, isolated from agent setup and launch imports."""

from __future__ import annotations

from typing import Annotated

import typer

from ucode.state import load_state
from ucode.ui import print_err
from ucode.workspace import pin, workspace_headers


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
    from ucode.mcp_proxy_runtime import serve

    state = load_state(hydrate=False)
    workspace = host or state.get("workspace")
    if not workspace:
        print_err("No workspace configured. Run `ug configure` first.")
        raise typer.Exit(1)
    if workspace_id:
        pin(workspace, workspace_id)
    profile = profile or state.get("profile")
    selected_id = workspace_headers(workspace).get("X-Databricks-Workspace-Id")
    serve(
        url,
        workspace,
        profile,
        use_pat=use_pat or bool(state.get("use_pat")),
        **({"workspace_id": selected_id} if selected_id else {}),
    )
