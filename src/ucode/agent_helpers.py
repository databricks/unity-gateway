"""Token helpers that coding agents run on their own schedule.

Claude Code runs `auth-token` as its `apiKeyHelper` and Codex as its provider auth command, at
session start and on every token refresh; Claude Code runs `otel-headers` to refresh trace export
headers. `ucode.entrypoint` runs them through `run` without importing `ucode.cli` or building its
full command tree. `ucode.cli` registers the same functions, so parsing and help are identical.
"""

from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout
from typing import Annotated

import typer

from ucode import custom_oauth
from ucode.databricks import ensure_pat_bearer, get_databricks_token
from ucode.managed_files import set_config_scope
from ucode.state import load_config_scope, load_saved_state
from ucode.ui import print_err
from ucode.workspace import selector_headers

# Keep in sync with `ucode.entrypoint._AGENT_HELPERS`, which can't import this module.
COMMANDS = ("auth-token", "otel-headers")


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
    browser_login: Annotated[bool, typer.Option("--browser-login", hidden=True)] = False,
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
    state = load_saved_state()
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
            token = get_databricks_token(
                workspace, profile, force_refresh=force_refresh, browser_login=browser_login
            )
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    # Write the bare token (with trailing newline) to stdout — nothing else may
    # land on stdout or the consuming agent will treat it as part of the token.
    sys.stdout.write(token + "\n")


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
    workspace_id: Annotated[str | None, typer.Option("--workspace-id", hidden=True)] = None,
) -> None:
    """Print fresh OTLP export headers as JSON to stdout, then exit."""
    state = load_saved_state()
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
        with redirect_stdout(sys.stderr):
            token = get_databricks_token(workspace, profile, force_refresh=force_refresh)
    except RuntimeError as exc:
        print_err(str(exc))
        raise typer.Exit(1) from None
    sys.stdout.write(
        json.dumps({"Authorization": f"Bearer {token}", **selector_headers(workspace_id)}) + "\n"
    )


def register(app: typer.Typer) -> None:
    """Add the helper commands to the full CLI or to `run`'s helper-only app."""
    app.command("auth-token", hidden=True)(auth_token_cmd)
    app.command("otel-headers", hidden=True)(otel_headers_cmd)


def run(argv: list[str]) -> None:
    """Run `ug <helper> ...` with only the helper commands registered."""
    app = typer.Typer(add_completion=False)

    @app.callback()
    def _apply_config_scope() -> None:
        # The CLI's root callback applies the saved config scope before every subcommand.
        set_config_scope(load_config_scope())

    register(app)
    app(args=argv, prog_name="ug")
