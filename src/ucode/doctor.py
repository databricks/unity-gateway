"""`ucode doctor` — diagnose the local ucode setup and offer to fix what it can.

Mirrors the `brew doctor` / `flutter doctor` / `npm doctor` pattern: run a
series of independent checks, print a status line for each, and for any problem
ucode knows how to fix, prompt the user to apply the fix and report whether it
worked. The command is read-only until the user says yes to a specific
suggestion, and a declined or piped run (no tty) changes nothing.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass

from ucode.agents import (
    TOOL_SPECS,
    tool_binary_installed,
    tool_version_error,
    update_tool_binary,
)
from ucode.databricks import (
    MIN_DATABRICKS_CLI_VERSION,
    databricks_cli_version,
    has_valid_databricks_auth,
    install_databricks_cli,
    run_databricks_login,
    upgrade_databricks_cli,
)
from ucode.state import load_state
from ucode.telemetry import ug_version
from ucode.ui import (
    console,
    heading,
    label,
    print_note,
    print_success,
    print_warning,
    prompt_yes_no_default,
    spinner,
    status_badge,
)

# Env vars that shadow the credential ucode configures for Claude Code. Claude
# warns when its own token and one of these are both set, so we surface them.
_CLAUDE_TOKEN_ENV_VARS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")


# status -> (glyph, status_badge kind). "info" is a healthy line that still
# carries an optional suggestion (e.g. installing a missing dependency).
_BADGES = {
    "ok": ("✓", "ok"),
    "warn": ("!", "warn"),
    "error": ("✗", "error"),
    "info": ("•", "info"),
}


@dataclass
class Suggestion:
    """A fix ucode offers to apply. ``apply`` returns True on success."""

    prompt: str
    apply: Callable[[], bool]


@dataclass
class Check:
    name: str
    status: str  # one of _BADGES
    detail: str
    suggestion: Suggestion | None = None


def _fmt_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(n) for n in version)


# ── individual checks ──────────────────────────────────────────────────────


def _check_uv() -> Check:
    if shutil.which("uv"):
        return Check("uv", "ok", "found on PATH")
    return Check(
        "uv",
        "error",
        "not found — needed to install and upgrade ucode. Install it from "
        "https://docs.astral.sh/uv/getting-started/installation/.",
    )


def _check_npm() -> Check:
    if shutil.which("npm"):
        return Check("npm", "ok", "found on PATH")
    return Check(
        "npm",
        "warn",
        "not found — needed to install coding-agent CLIs automatically. "
        "Install Node.js/npm from https://nodejs.org/.",
    )


def _install_databricks() -> bool:
    try:
        install_databricks_cli()
    except RuntimeError:
        return False
    return shutil.which("databricks") is not None


def _check_databricks_cli() -> Check:
    if not shutil.which("databricks"):
        return Check(
            "Databricks CLI",
            "error",
            "not installed",
            Suggestion("Install the Databricks CLI?", _install_databricks),
        )
    version = databricks_cli_version()
    if version is None:
        return Check("Databricks CLI", "warn", "installed, but its version could not be read")
    current = _fmt_version(version)
    if version < MIN_DATABRICKS_CLI_VERSION:
        floor = _fmt_version(MIN_DATABRICKS_CLI_VERSION)
        return Check(
            "Databricks CLI",
            "warn",
            f"v{current} is below v{floor}, the release that ships `databricks aitools`",
            Suggestion("Upgrade the Databricks CLI to the latest release?", upgrade_databricks_cli),
        )
    return Check("Databricks CLI", "ok", f"v{current}")


def _check_workspace() -> Check:
    workspace = load_state().get("workspace")
    if workspace:
        return Check("Workspace", "ok", str(workspace))
    return Check(
        "Workspace",
        "warn",
        "not configured — run `ucode configure` to set your Databricks workspace",
    )


def _check_agent_clis() -> list[Check]:
    """One check per configured coding agent: installed and compatible?"""
    tools = load_state().get("available_tools") or []
    checks: list[Check] = []
    for tool in tools:
        if tool not in TOOL_SPECS:
            continue
        spec = TOOL_SPECS[tool]
        display = spec["display"]
        if not tool_binary_installed(tool):
            checks.append(
                Check(
                    display,
                    "warn",
                    f"`{spec['binary']}` not found on PATH",
                    Suggestion(f"Install {display}?", lambda t=tool: update_tool_binary(t)),
                )
            )
            continue
        blocker = tool_version_error(tool)
        if blocker:
            checks.append(
                Check(
                    display,
                    "warn",
                    blocker,
                    Suggestion(
                        f"Upgrade {display} to meet the required version?",
                        lambda t=tool: update_tool_binary(t),
                    ),
                )
            )
        else:
            checks.append(Check(display, "ok", "installed; no required update"))
    return checks


def _check_databricks_auth() -> Check | None:
    """Validate the configured workspace's Databricks credentials.

    The most common ucode failure at launch is an expired/invalid token
    surfacing as a `403 Invalid Token` from the agent. Checking auth up front
    (and offering to re-run `databricks auth login`) catches it before launch.
    Returns None when there's no workspace yet — `_check_workspace` covers that.
    """
    state = load_state()
    workspace = state.get("workspace")
    if not workspace:
        return None
    profile = state.get("profile")
    with spinner("Verifying Databricks credentials..."):
        ok = has_valid_databricks_auth(workspace, profile)
    if ok:
        return Check("Databricks auth", "ok", "credentials are valid")

    def _login() -> bool:
        try:
            run_databricks_login(workspace, profile)
        except RuntimeError:
            return False
        return has_valid_databricks_auth(workspace, profile)

    return Check(
        "Databricks auth",
        "warn",
        "no valid credentials for this workspace (launches will fail to authenticate)",
        Suggestion("Log in to Databricks now?", _login),
    )


def _check_anthropic_env_collision() -> Check | None:
    """Warn when a Claude token env var is set that collides with ucode's own.

    ucode authenticates Claude Code through the gateway; a stray
    `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` in the environment shadows that
    and makes Claude complain. We can't unset a parent shell's env, so this is
    advisory (no fix). Returns None when nothing collides.
    """
    set_vars = [name for name in _CLAUDE_TOKEN_ENV_VARS if os.environ.get(name, "").strip()]
    if not set_vars:
        return None
    joined = ", ".join(set_vars)
    return Check(
        "Claude auth env",
        "warn",
        f"{joined} is set and can collide with ucode's Claude auth; unset it in your shell",
    )


def _check_ug() -> Check:
    """Report the installed build. Explicit updates are available via `ug upgrade`."""
    version = ug_version()
    return Check("ug", "info", f"v{version} (installed from GitHub)")


# ── orchestration ──────────────────────────────────────────────────────────


def _run_inspector(name: str, fn: Callable[[], Check | list[Check] | None]) -> list[Check]:
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - one inspector must not suppress the rest
        return [Check(name, "error", f"check could not run: {type(exc).__name__}: {exc}")]
    if result is None:
        return []
    if isinstance(result, Check):
        return [result]
    return result


def _gather_checks() -> list[Check]:
    inspectors: list[tuple[str, Callable[[], Check | list[Check] | None]]] = [
        ("uv", _check_uv),
        ("npm", _check_npm),
        ("Databricks CLI", _check_databricks_cli),
        ("Workspace", _check_workspace),
        ("Databricks auth", _check_databricks_auth),
        ("Claude auth env", _check_anthropic_env_collision),
        ("Coding agents", _check_agent_clis),
        ("ug", _check_ug),
    ]
    checks: list[Check] = []
    for name, fn in inspectors:
        checks.extend(_run_inspector(name, fn))
    return checks


def doctor() -> int:
    """Run every check, print its status, and prompt to apply any offered fix."""
    console.print(heading("ug doctor"))
    console.print()

    # Piped input must never apply fixes; prompt only on a real tty.
    interactive = sys.stdin is not None and sys.stdin.isatty()
    checks = _gather_checks()
    problems = 0
    applied = 0
    unresolved_errors = 0
    for check in checks:
        glyph, kind = _BADGES[check.status]
        console.print(f"  {status_badge(glyph, kind)} {label(check.name)}: {check.detail}")
        if check.status in ("warn", "error"):
            problems += 1
        if check.status == "error":
            unresolved_errors += 1
        if check.suggestion is None or not interactive:
            continue
        if prompt_yes_no_default(f"    {check.suggestion.prompt}", default=False):
            if check.suggestion.apply():
                print_success(f"{check.name}: fixed")
                applied += 1
                if check.status == "error":
                    unresolved_errors -= 1
            else:
                print_warning(f"{check.name}: fix did not complete")

    console.print()
    if problems == 0:
        print_success("No problems detected.")
    else:
        noun = "issue" if problems == 1 else "issues"
        print_note(f"{problems} {noun} found; {applied} fix(es) applied.")
    return 1 if unresolved_errors else 0
