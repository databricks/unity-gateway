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
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit.exceptions import ParseError

from ucode.agents import (
    TOOL_SPECS,
    tool_binary_installed,
    tool_version_error,
    update_tool_binary,
)
from ucode.agents.claude import (
    CLAUDE_SETTINGS_PATH,
    effective_managed_policy,
    managed_settings_status,
)
from ucode.agents.codex import (
    CODEX_CONFIG_PATH,
    CODEX_MODEL_PROVIDER_NAME,
    managed_config_status,
)
from ucode.databricks import (
    MIN_DATABRICKS_CLI_VERSION,
    build_tool_base_url,
    databricks_cli_version,
    get_databricks_token,
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


def _gateway_configured(state: dict, tool: str) -> bool:
    """True when `tool` is configured directly or via a managed config."""
    configured = set(state.get("available_tools") or []) | set(
        (state.get("managed_configs") or {}).keys()
    )
    return tool in configured


def _check_claude_gateway_config() -> Check | None:
    """Validate ~/.claude/ucode-settings.json against the configured workspace.

    `ug claude` launches with `--settings ucode-settings.json`; if that file is
    missing or points at another workspace's gateway, launches fail or route to
    the wrong workspace. Relayed setups (subscription OAuth via a local proxy)
    deliberately omit apiKeyHelper and use a loopback base URL, so only the
    base URL's presence is checked there.
    """
    state = load_state()
    if not _gateway_configured(state, "claude"):
        return None
    workspace = state.get("workspace")
    if not workspace:
        return None  # `_check_workspace` covers the missing workspace.
    if not CLAUDE_SETTINGS_PATH.exists():
        return Check(
            "Claude gateway config",
            "error",
            "~/.claude/ucode-settings.json is missing; ug claude cannot route to the "
            "gateway. Run `ug configure`.",
        )
    try:
        settings = json.loads(CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return Check(
            "Claude gateway config",
            "error",
            f"Claude gateway config is not valid JSON: {exc}",
        )
    if not isinstance(settings, dict):
        return Check(
            "Claude gateway config",
            "error",
            "Claude gateway config is not a JSON object.",
        )
    env = settings.get("env")
    if not isinstance(env, dict):
        env = {}
    base_url = env.get("ANTHROPIC_BASE_URL")
    if state.get("claude_relayed"):
        if not base_url:
            return Check(
                "Claude gateway config",
                "error",
                "Claude relay config has no env.ANTHROPIC_BASE_URL.",
            )
        return Check(
            "Claude gateway config",
            "ok",
            "relay config present (subscription OAuth via local proxy)",
        )
    helper = settings.get("apiKeyHelper")
    if not isinstance(helper, str) or not helper.strip():
        return Check(
            "Claude gateway config",
            "error",
            "Claude gateway config has no apiKeyHelper (gateway auth); bare/relaunched "
            "claude will not authenticate.",
        )
    if not base_url:
        return Check(
            "Claude gateway config",
            "error",
            "Claude gateway config has no env.ANTHROPIC_BASE_URL.",
        )
    expected = build_tool_base_url("claude", str(workspace))
    if str(base_url) != expected:
        return Check(
            "Claude gateway config",
            "error",
            f"Claude gateway config points at a stale gateway URL ({base_url}); "
            f"expected {expected} for the configured workspace.",
        )
    return Check(
        "Claude gateway config",
        "ok",
        "ucode-settings.json routes to the configured gateway",
    )


def _check_codex_gateway_config() -> Check | None:
    """Validate ~/.codex/ucode.config.toml against the configured workspace."""
    state = load_state()
    if not _gateway_configured(state, "codex"):
        return None
    workspace = state.get("workspace")
    if not workspace:
        return None  # `_check_workspace` covers the missing workspace.
    if not CODEX_CONFIG_PATH.exists():
        return Check(
            "Codex gateway config",
            "error",
            "~/.codex/ucode.config.toml is missing; run `ug configure`.",
        )
    try:
        doc = tomlkit.parse(CODEX_CONFIG_PATH.read_text(encoding="utf-8"))
    except ParseError as exc:
        return Check(
            "Codex gateway config",
            "error",
            f"Codex config is not valid TOML: {exc}",
        )
    provider_name = doc.get("model_provider")
    if provider_name != CODEX_MODEL_PROVIDER_NAME:
        return Check(
            "Codex gateway config",
            "error",
            f"Codex config model_provider is {provider_name}; "
            f"expected {CODEX_MODEL_PROVIDER_NAME}.",
        )
    providers = doc.get("model_providers")
    provider = providers.get(CODEX_MODEL_PROVIDER_NAME) if isinstance(providers, dict) else None
    base_url = provider.get("base_url") if isinstance(provider, dict) else None
    expected = build_tool_base_url("codex", str(workspace))
    if base_url is None or str(base_url) != expected:
        return Check(
            "Codex gateway config",
            "error",
            f"Codex config points at a stale/absent gateway URL ({base_url}); expected {expected}.",
        )
    catalog = doc.get("model_catalog_json")
    if isinstance(catalog, str) and catalog.strip():
        catalog_path = Path(str(catalog)).expanduser()
        if not catalog_path.is_file():
            return Check(
                "Codex gateway config",
                "error",
                f"Codex references a model catalog that is missing: {catalog_path}.",
            )
    return Check(
        "Codex gateway config",
        "ok",
        "ucode.config.toml routes to the configured gateway",
    )


def _check_claude_managed_policy() -> Check | None:
    """Diagnose whether bare claude / VS Code is OS-enforced to route via the gateway.

    Separate from `ug claude` (wrapped), which is covered by the ucode-settings.json
    check. Reads the real managed files via effective_managed_policy() rather than
    relying on state fingerprints, so it detects drop-in-only enforcement and
    drop-in overrides even when the base file's fingerprint is unchanged.
    Never offers a fix that deletes MDM policy.
    """
    state = load_state()
    if not _gateway_configured(state, "claude"):
        return None
    workspace = state.get("workspace")
    if not workspace:
        return None
    policy = effective_managed_policy()
    if not policy.supported:
        return Check(
            "Claude OS-managed policy",
            "info",
            "OS-managed policy is not available on this platform; ug claude (wrapped) still "
            "routes via ucode-settings.json.",
        )
    if policy.unreadable or policy.invalid:
        parts: list[str] = []
        if policy.unreadable:
            parts.append(f"unreadable: {', '.join(str(p) for p in policy.unreadable)}")
        if policy.invalid:
            parts.append(f"not valid JSON: {', '.join(str(p) for p in policy.invalid)}")
        return Check(
            "Claude OS-managed policy",
            "warn",
            f"cannot confirm bare-claude enforcement. OS-managed policy file(s) {'; '.join(parts)}. "
            "ug claude (wrapped) still routes.",
        )
    relayed = bool(state.get("claude_relayed"))
    if relayed:
        conflicts = []
        if policy.api_key_helper.value:
            conflicts.append("apiKeyHelper")
        if policy.base_url.value:
            conflicts.append("env.ANTHROPIC_BASE_URL")
        if conflicts:
            return Check(
                "Claude OS-managed policy",
                "error",
                f"OS-managed policy defines {', '.join(conflicts)}, which conflicts with Claude "
                "subscription relay; ug claude will refuse to launch. Ask your administrator to "
                "remove these entries.",
            )
        return Check(
            "Claude OS-managed policy",
            "info",
            "relay mode: no conflicting OS-managed policy; ug claude uses subscription OAuth.",
        )
    expected = build_tool_base_url("claude", str(workspace))
    base_url_val = policy.base_url.value
    base_url_src = policy.base_url.source
    helper_value = policy.api_key_helper.value
    helper_present = isinstance(helper_value, str) and bool(helper_value.strip())
    _, status, _ = managed_settings_status(state)
    ownership = (
        "managed"
        if status in {"current", "compatible (local settings)", "compatible (relay settings)"}
        else "externally owned"
    )
    if base_url_src is None and policy.api_key_helper.source is None:
        return Check(
            "Claude OS-managed policy",
            "info",
            f"bare claude / VS Code is not OS-enforced to use the gateway ({ownership}). "
            f"ug claude (wrapped) still routes.",
        )
    if base_url_src is not None:
        src_desc = (
            "via managed-settings.json"
            if base_url_src == policy.base_path
            else f"via drop-in {base_url_src.name}"
        )
        if str(base_url_val) != expected:
            return Check(
                "Claude OS-managed policy",
                "warn",
                f"bare claude / VS Code would route to a different gateway ({base_url_val}) set "
                f"{src_desc}; expected {expected}. ug claude (wrapped) still routes correctly.",
            )
        if helper_present:
            return Check(
                "Claude OS-managed policy",
                "ok",
                f"bare claude / VS Code is enforced to route via the gateway ({src_desc}; "
                f"{ownership}).",
            )
        return Check(
            "Claude OS-managed policy",
            "warn",
            "OS-managed policy sets the gateway URL but no apiKeyHelper; bare claude may not "
            "authenticate.",
        )
    return Check(
        "Claude OS-managed policy",
        "info",
        "bare claude / VS Code is not OS-enforced to use the gateway.",
    )


def _check_codex_managed_policy() -> Check | None:
    """Diagnose whether bare codex is OS-enforced to route via the gateway.

    Separate from `ug codex` (wrapped), which is covered by the ucode config check.
    Codex has no drop-in directory, so this is base-file-only.
    """
    state = load_state()
    if not _gateway_configured(state, "codex"):
        return None
    workspace = state.get("workspace")
    if not workspace:
        return None
    _, status, _ = managed_config_status(state)
    status_map: dict[str, tuple[str, str]] = {
        "unsupported": (
            "info",
            "Codex OS-managed policy is not available on this platform; ug codex (wrapped) still "
            "routes.",
        ),
        "not configured": (
            "info",
            "bare codex is not OS-enforced to use the gateway; ug codex (wrapped) still routes.",
        ),
        "missing": (
            "info",
            "bare codex is not OS-enforced to use the gateway; ug codex (wrapped) still routes.",
        ),
        "current": (
            "ok",
            "Codex OS-managed policy is present and current (ug-owned).",
        ),
        "compatible (local settings)": (
            "info",
            "Codex OS-managed policy is compatible; enforced via local settings.",
        ),
        "compatible (relay settings)": (
            "info",
            "Codex OS-managed policy is compatible (relay settings).",
        ),
        "drifted": (
            "warn",
            "Codex OS-managed policy drifted from ug's last write.",
        ),
        "unreadable": (
            "warn",
            "Codex OS-managed policy file is unreadable.",
        ),
        "invalid": (
            "warn",
            "Codex OS-managed policy is not valid TOML.",
        ),
    }
    status_tuple = status_map.get(status)
    if status_tuple:
        status_level, detail = status_tuple
        return Check("Codex OS-managed policy", status_level, detail)
    return Check("Codex OS-managed policy", "info", f"Codex OS-managed policy status: {status}.")


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

    # Branch 1: Static DATABRICKS_BEARER set
    bearer = os.environ.get("DATABRICKS_BEARER", "").strip()
    if bearer:
        return Check(
            "Databricks auth",
            "warn",
            "DATABRICKS_BEARER is set but ug cannot verify it here; launches will fail if it is invalid or expired (gateway access not verified).",
        )

    # Branch 2: DATABRICKS_BEARER_COMMAND set
    command = os.environ.get("DATABRICKS_BEARER_COMMAND", "").strip()
    if command:
        with spinner("Verifying Databricks credentials..."):
            try:
                token = get_databricks_token(workspace, profile)
                obtained = bool(token)
            except RuntimeError:
                obtained = False
        if obtained:
            return Check(
                "Databricks auth",
                "ok",
                "DATABRICKS_BEARER_COMMAND produces a token (gateway access not verified).",
            )
        else:
            return Check(
                "Databricks auth",
                "error",
                "DATABRICKS_BEARER_COMMAND did not produce a token; launches will fail to authenticate.",
            )

    # Branch 3: Custom OAuth configured
    if isinstance(state.get("custom_oauth"), dict):
        custom_profile = state["custom_oauth"].get("profile")
        if isinstance(custom_profile, str) and custom_profile.strip():
            custom_profile = custom_profile.strip()
            with spinner("Verifying Databricks credentials..."):
                ok = has_valid_databricks_auth(workspace, custom_profile)
            if ok:
                return Check(
                    "Databricks auth",
                    "ok",
                    "custom OAuth client credentials are obtainable (gateway access not verified).",
                )
            else:
                return Check(
                    "Databricks auth",
                    "warn",
                    "custom OAuth client has no obtainable token yet; run a ug command that authenticates (gateway access not verified).",
                )
        else:
            return Check(
                "Databricks auth",
                "warn",
                "custom OAuth is configured but not yet authenticated (unverified).",
            )

    # Branch 4: Standard OAuth / PAT
    with spinner("Verifying Databricks credentials..."):
        ok = has_valid_databricks_auth(workspace, profile)
    if ok:
        return Check(
            "Databricks auth", "ok", "credentials are obtainable (gateway access not verified)."
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
        "no obtainable credentials for this workspace (launches will fail to authenticate)",
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
        ("Claude gateway config", _check_claude_gateway_config),
        ("Codex gateway config", _check_codex_gateway_config),
        ("Claude OS-managed policy", _check_claude_managed_policy),
        ("Codex OS-managed policy", _check_codex_managed_policy),
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
