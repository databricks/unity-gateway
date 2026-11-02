"""`ucode doctor` — diagnose the local ucode setup and offer to fix what it can.

Mirrors the `brew doctor` / `flutter doctor` / `npm doctor` pattern: run a
series of independent checks, print a status line for each, and for any problem
ucode knows how to fix, prompt the user to apply the fix and report whether it
worked. The command is read-only until the user says yes to a specific
suggestion, and a declined or piped run (no tty) changes nothing.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ucode.agents import (
    TOOL_SPECS,
    copilot,
    tool_binary_installed,
    tool_version_error,
    update_tool_binary,
    vscode,
)
from ucode.databricks import (
    MIN_DATABRICKS_CLI_VERSION,
    check_databricks_auth,
    databricks_cli_installed,
    databricks_cli_version,
    has_valid_databricks_auth,
    install_databricks_cli,
    run_databricks_login,
    upgrade_databricks_cli,
)
from ucode.managed_config import load_managed_state
from ucode.managed_files import OS, current_os
from ucode.managed_resolve import (
    managed_provider_service,
    managed_unity_catalog_location,
    resolve_state,
)
from ucode.state import load_state
from ucode.telemetry import ug_version
from ucode.ui import (
    console,
    encodable_glyph,
    heading,
    label,
    print_note,
    print_success,
    print_warning,
    prompt_yes_no_default,
    spinner,
    status_badge,
)
from ucode.workspace import state_workspace_key

# Env vars that shadow the credential ucode configures for Claude Code. Claude
# warns when its own token and one of these are both set, so we surface them.
_CLAUDE_TOKEN_ENV_VARS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")


# Copilot device-policy settings that stop `ug copilot`, which runs Copilot offline on Databricks
# models without a GitHub sign-in.
_COPILOT_BLOCKING_POLICY = {
    "forceLoginOrgs": "requires a GitHub sign-in",
    "forceRemoteSettingsRefresh": "requires a live fetch of GitHub's policy",
}
# Settings that change what ug sets up for Copilot.
_COPILOT_LIMITING_POLICY = {
    "allowedMcpServers": "can block ug's MCP servers",
    "allowManagedMcpServersOnly": "can block ug's MCP servers",
    "deniedMcpServers": "can block ug's MCP servers",
    "strictPluginOnlyCustomization": "can ignore ug's MCP servers and skills",
    "telemetry": "sends Copilot's telemetry to your IT team's endpoint",
    "sandbox": "can block shell commands where the sandbox is unavailable",
}
# The first VS Code that reads ug's MCP servers from the Copilot CLI's config.
_VSCODE_MCP_VERSION = (1, 142, 0)

# status -> (glyph, status_badge kind). "info" is a healthy line that still
# carries an optional suggestion (e.g. installing a missing dependency).
_BADGES = {
    "ok": (("✓", "+"), "ok"),
    "warn": (("!", "!"), "warn"),
    "error": (("✗", "x"), "error"),
    "info": (("•", "*"), "info"),
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
    return databricks_cli_installed()


def _check_databricks_cli() -> Check:
    if not databricks_cli_installed():
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
        "not configured — run `ug configure` to set your Databricks workspace",
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


def _check_copilot_models() -> Check | None:
    """The models `ug copilot` registers for the current workspace, when Copilot is configured."""
    state = load_state()
    if "copilot" not in (state.get("available_tools") or []):
        return None
    managed = load_managed_state(state_workspace_key(state))
    if managed:
        source = managed_provider_service(managed, "copilot") or managed_unity_catalog_location(
            managed, "copilot"
        )
        if source:
            return Check(
                "Copilot models",
                "ok",
                f"from {source}, your workspace's managed config; listed at each launch",
            )
        state = resolve_state(managed, state, "copilot")
    catalog = copilot.copilot_catalog(state)
    if not catalog:
        return Check(
            "Copilot models",
            "warn",
            "none Copilot can use — run `ug configure --agents copilot` to rediscover them",
        )
    counts = ", ".join(
        f"{sum(m.provider == provider for m in catalog)} {provider}"
        for provider in copilot.PROVIDER_NAMES.values()
        if any(m.provider == provider for m in catalog)
    )
    return Check("Copilot models", "ok", f"{len(catalog)} models ({counts})")


def _ignored_by_copilot(path: Path) -> bool:
    """Copilot ignores a device policy that is a symlink, not owned by root, or writable by others."""
    if path.is_symlink():
        return True
    if current_os() is OS.WINDOWS:
        return False
    info = path.stat()
    return info.st_uid != 0 or bool(info.st_mode & 0o022)


def _check_copilot_device_policy() -> Check | None:
    """The Copilot CLI's device managed settings, when Copilot is configured and IT set them."""
    if "copilot" not in (load_state().get("available_tools") or []):
        return None
    path = copilot.device_policy_path()
    if path is None or not (path.exists() or path.is_symlink()):
        return None
    name = "Copilot device policy"
    if _ignored_by_copilot(path):
        return Check(
            name,
            "warn",
            f"{path} is a symlink, not owned by root, or writable by others, so Copilot ignores it",
        )
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Check(name, "error", f"{path} can't be read ({exc}); Copilot may refuse to start")
    if not isinstance(policy, dict):
        return Check(name, "error", f"{path} isn't a JSON object; Copilot may refuse to start")
    blocking = [f"{key} {why}" for key, why in _COPILOT_BLOCKING_POLICY.items() if policy.get(key)]
    if blocking:
        return Check(
            name,
            "error",
            f"{path}: {'; '.join(blocking)}, which `ug copilot` can't use. Ask your IT team to "
            "remove it for this machine, or use Copilot without ug.",
        )
    if not policy:
        return None
    # Any setting in this file turns them off, whatever it is.
    harnesses = "VS Code turns off its Claude and Codex agents while this file has settings"
    limiting = {key: why for key, why in _COPILOT_LIMITING_POLICY.items() if key in policy}
    if limiting:
        reasons = "; ".join(dict.fromkeys(limiting.values()))
        return Check(name, "warn", f"{path} sets {', '.join(limiting)}: {reasons}. {harnesses}.")
    return Check(name, "info", f"{path} is set. {harnesses}.")


def _check_vscode() -> Check | None:
    """Copilot Chat in VS Code, which `ug configure` sets up with Copilot."""
    if "copilot" not in (load_state().get("available_tools") or []):
        return None
    code = vscode.find_code()
    if code is None:
        return None
    try:
        installed = vscode.installed_version(code)
        version = vscode.code_version(code)
    except RuntimeError as exc:
        return Check("VS Code", "warn", str(exc))
    if installed is None:
        return Check(
            "VS Code",
            "info",
            "ug's extension isn't installed; run `ug configure --agents copilot` to set it up",
        )
    if version is not None and version < _VSCODE_MCP_VERSION:
        return Check(
            "VS Code",
            "warn",
            f"VS Code {_fmt_version(version)} has ug's models, but ug's MCP servers need VS Code "
            f"{_fmt_version(_VSCODE_MCP_VERSION)} or later",
        )
    detail = f"extension {installed}" + (f" (VS Code {_fmt_version(version)})" if version else "")
    return Check("VS Code", "ok", detail)


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
        ok = check_databricks_auth(workspace, profile)
    if ok:
        return Check("Databricks auth", "ok", "credentials are valid")
    if ok is None:
        # Logging in would hit the same unrunnable CLI, so offer no fix.
        return Check(
            "Databricks auth",
            "warn",
            "could not run the Databricks CLI to check credentials",
        )

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
    """Note a Claude token env var that would outrank ucode's own credential.

    Claude Code ranks `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` above ucode's
    `apiKeyHelper`. `ug configure` and `ug claude` blank both in ucode's Claude
    settings, so Claude launched via ug ignores the shell's value; a bare `claude`
    sees the blanks only once `ug configure` has written them to the managed file.
    Says so, and names the supported ways to use your own token. Advisory (no fix).
    Returns None when nothing collides.
    """
    set_vars = [name for name in _CLAUDE_TOKEN_ENV_VARS if os.environ.get(name, "").strip()]
    if not set_vars:
        return None
    joined = ", ".join(set_vars)
    return Check(
        "Claude auth env",
        "info",
        f"{joined} is set; Claude Code launched via ug ignores it. To use your own Databricks "
        "token, set DATABRICKS_BEARER or run `ug configure --profile <name> --use-pat` "
        "(re-run `ug configure` if bare `claude` still fails with a 401)",
    )


def _check_ug() -> Check:
    """Report the installed build. Explicit updates are available via `ug upgrade`."""
    version = ug_version()
    return Check("ug", "info", f"v{version} (installed from GitHub)")


# ── orchestration ──────────────────────────────────────────────────────────


def _gather_checks() -> list[Check]:
    checks: list[Check] = [_check_uv(), _check_npm(), _check_databricks_cli(), _check_workspace()]
    # These return None when they don't apply (no workspace, no env collision),
    # so drop the Nones before display.
    optional = [
        _check_databricks_auth(),
        _check_anthropic_env_collision(),
        _check_copilot_models(),
        _check_copilot_device_policy(),
        _check_vscode(),
    ]
    checks.extend(c for c in optional if c is not None)
    checks.extend(_check_agent_clis())
    checks.append(_check_ug())
    return checks


def doctor() -> int:
    """Run every check, print its status, and prompt to apply any offered fix."""
    console.print(heading("ug doctor"))
    console.print()

    checks = _gather_checks()
    problems = 0
    applied = 0
    for check in checks:
        (glyph, fallback), kind = _BADGES[check.status]
        badge = status_badge(encodable_glyph(glyph, fallback), kind)
        console.print(f"  {badge} {label(check.name)}: {check.detail}")
        if check.status in ("warn", "error"):
            problems += 1
        if check.suggestion is None:
            continue
        if prompt_yes_no_default(f"    {check.suggestion.prompt}", default=False):
            if check.suggestion.apply():
                print_success(f"{check.name}: fixed")
                applied += 1
            else:
                print_warning(f"{check.name}: fix did not complete")

    console.print()
    if problems == 0:
        print_success("No problems detected.")
    else:
        noun = "issue" if problems == 1 else "issues"
        print_note(f"{problems} {noun} found; {applied} fix(es) applied.")
    return 0
