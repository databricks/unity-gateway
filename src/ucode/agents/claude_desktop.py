"""Claude Desktop / Cowork launcher: route the GUI through Databricks AI Gateway.

Unlike Claude Code (a CLI ug spawns, whose lifetime bounds the refresh proxy),
Claude Desktop is a standalone GUI with a static config file — so a token written
into that file would go stale (the Databricks OAuth token expires in ~1h). Instead
ug runs the same refresh proxy `ug claude` uses and points a token-less Desktop
config at it; the proxy owns the upstream auth (live-refreshed Databricks swap
credential + the fixed Anthropic Authorization + Model-Provider-Service header).

EXPERIMENTAL. The Desktop config schema (the Claude-3p ``configLibrary`` entry +
``_meta.json`` registry) is not a public contract; the keys here mirror what Desktop
authors and may drift across releases.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from ucode import gateway_proxy
from ucode.config_io import backup_existing_file, read_json_safe, write_json_file
from ucode.constants import LOOPBACK_HOST, MODEL_PROVIDER_SERVICE_HEADER
from ucode.databricks import get_databricks_token
from ucode.managed_files import OS, current_os
from ucode.telemetry import agent_version, ucode_version
from ucode.ui import print_note, print_success, print_warning

CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
# Scanned out of `claude setup-token` output, which wraps the token in prose.
_OAUTH_TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9._\-]+")
# Dropped before forwarding: the proxy owns the upstream auth, and a client
# `x-api-key` carrying the OAuth would be rejected by Anthropic.
_STRIP_CLIENT_AUTH_HEADERS = frozenset({"x-api-key", "authorization"})
# The proxy overwrites auth, so no real credential ever hits the config file.
_CONFIG_KEY_PLACEHOLDER = "ug-refresh-proxy-injects-credentials"
# In Desktop's registry an entry is invisible until listed in `entries` and inactive
# until it is the `appliedId`.
_META_FILENAME = "_meta.json"
_ENTRY_NAME = "Unity Gateway"
_CONFIG_FILE_MODE = 0o600  # match Desktop's own configLibrary files
_APP_BUNDLE_ID = "com.anthropic.claudefordesktop"  # stable across app rename/move


def _app_support_dir() -> Path:
    """Claude Desktop's per-OS application-support root for the gateway
    (``Claude-3p``) build. Raises on unsupported platforms."""
    system = current_os()
    if system is OS.MACOS:
        return Path.home() / "Library" / "Application Support" / "Claude-3p"
    if system is OS.WINDOWS:
        base = os.environ.get("APPDATA")
        if not base:
            raise RuntimeError("APPDATA is not set; cannot locate Claude Desktop config.")
        return Path(base) / "Claude-3p"
    raise RuntimeError(
        f"`ug claude-desktop` currently supports macOS and Windows only (detected {system.value})."
    )


def _config_library_dir() -> Path:
    return _app_support_dir() / "configLibrary"


def config_entry_id(workspace: str) -> str:
    """Deterministic entry id for ``workspace`` so re-runs reuse one stable entry
    rather than accumulating duplicates.

    The seed string is an internal, stable key — deliberately NOT tied to the
    command name, so renaming the command never changes existing entry ids.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ug-claude-cowork::{workspace}"))


def config_path(workspace: str) -> Path:
    """Path to the ug-owned Desktop gateway-config entry for ``workspace``."""
    return _config_library_dir() / f"{config_entry_id(workspace)}.json"


def register_active_config(entry_id: str, name: str = _ENTRY_NAME) -> str | None:
    """Register ``entry_id`` in Desktop's ``_meta.json`` and mark it applied.

    Preserves existing entries (e.g. the user's ``Default``) and returns the prior
    ``appliedId`` so the caller can restore it on exit.
    """
    meta_path = _config_library_dir() / _META_FILENAME
    meta = read_json_safe(meta_path) if meta_path.exists() else {}
    prior_applied = meta.get("appliedId")
    entries = meta.get("entries")
    entries = (
        [e for e in entries if isinstance(e, dict) and e.get("id") != entry_id]
        if (isinstance(entries, list))
        else []
    )
    entries.append({"id": entry_id, "name": name})
    meta["entries"] = entries
    meta["appliedId"] = entry_id
    write_json_file(meta_path, meta)
    os.chmod(meta_path, _CONFIG_FILE_MODE)
    return prior_applied if isinstance(prior_applied, str) else None


def restore_active_config(prior_applied: str | None) -> None:
    """Point ``appliedId`` back to ``prior_applied`` (or clear it), so Desktop's
    next restart doesn't boot into the now-dead loopback-proxy config."""
    meta_path = _config_library_dir() / _META_FILENAME
    if not meta_path.exists():
        return
    meta = read_json_safe(meta_path)
    if prior_applied is not None:
        meta["appliedId"] = prior_applied
    else:
        meta.pop("appliedId", None)
    write_json_file(meta_path, meta)
    os.chmod(meta_path, _CONFIG_FILE_MODE)


def render_config(base_url: str, models: list[str]) -> dict:
    """The Desktop gateway config pointing at the loopback proxy.

    Mirrors the schema Desktop authors for a gateway config. Holds no real
    credentials and no ``inferenceCustomHeaders`` — the proxy injects the
    Authorization, swap, and Model-Provider-Service headers per request.
    ``modelDiscoveryEnabled`` is false and ``models`` are listed explicitly,
    because the relayed ``/v1/models`` probe isn't served.
    """
    config: dict = {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": base_url,
        "inferenceCredentialKind": "static",
        "inferenceGatewayApiKey": _CONFIG_KEY_PLACEHOLDER,
        "modelDiscoveryEnabled": False,
    }
    if models:
        config["inferenceModels"] = [{"name": model} for model in models]
    return config


def _is_wsl() -> bool:
    """True when running under WSL (where 'Linux' is a shell on Windows and the
    Desktop app is the Windows one)."""
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def _macos_app_is_running() -> bool:
    result = subprocess.run(
        ["osascript", "-e", f'application id "{_APP_BUNDLE_ID}" is running'],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    return result.stdout.strip() == "true"


def relaunch_desktop_app() -> None:
    """(Re)launch Claude Desktop so it re-reads the freshly-written config.

    Desktop only reads ``configLibrary/`` at startup, so a running instance is
    gracefully quit (a normal Quit event — the app still gets to save/prompt) and
    reopened. macOS is implemented and verified; Windows and WSL need a real box to
    validate their launch path, and native Linux has no Desktop app at all, so those
    print manual guidance instead of guessing.
    """
    system = current_os()
    if system is OS.MACOS:
        if _macos_app_is_running():
            print_note("Restarting Claude Desktop to pick up the config...")
            subprocess.run(
                ["osascript", "-e", f'quit app id "{_APP_BUNDLE_ID}"'], check=False, timeout=30
            )
            for _ in range(40):  # wait for exit before relaunching
                if not _macos_app_is_running():
                    break
                time.sleep(0.25)
        else:
            print_note("Launching Claude Desktop...")
        subprocess.run(["open", "-b", _APP_BUNDLE_ID], check=False, timeout=30)
        return
    if _is_wsl():
        print_warning(
            "Auto-launch under WSL isn't wired up yet (the Desktop app is the Windows one). "
            "Open or restart Claude Desktop on Windows to pick up the config."
        )
        return
    if system is OS.WINDOWS:
        print_warning(
            "Auto-launch on Windows isn't wired up yet — open or restart Claude Desktop manually."
        )
        return
    print_warning(
        "Claude Desktop isn't available on Linux; run `ug claude-desktop` on the Mac or Windows "
        "machine where Desktop is installed."
    )


def _resolve_anthropic_oauth() -> str:
    """Return the Anthropic subscription OAuth token, launching the auth session
    when needed.

    Prefers a pre-supplied ``CLAUDE_CODE_OAUTH_TOKEN`` (headless / CI); otherwise
    runs ``claude setup-token`` (opens a browser to the Claude subscription) and
    parses its long-lived token from the output.
    """
    preset = os.environ.get(CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR, "").strip()
    if preset:
        return preset
    print_note("Launching Claude subscription sign-in (`claude setup-token`)...")
    try:
        result = subprocess.run(
            ["claude", "setup-token"],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "`claude` was not found on PATH. Install Claude Code "
            "(npm i -g @anthropic-ai/claude-code), or set "
            f"{CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR} to a token from `claude setup-token`."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "`claude setup-token` failed; cannot obtain a subscription token."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("`claude setup-token` timed out.") from exc
    match = _OAUTH_TOKEN_RE.search(result.stdout or "")
    if not match:
        raise RuntimeError(
            "Could not read a subscription token from `claude setup-token` output. "
            f"Run it manually and pass the token via {CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR}."
        )
    print_success("Claude subscription authenticated")
    return match.group(0)


def _ensure_databricks_session(workspace: str, profile: str | None) -> None:
    """Make sure a Databricks OAuth session exists so the proxy can mint swap
    credentials, launching `databricks auth login` if the token fetch fails."""
    if shutil.which("databricks") is None:
        raise RuntimeError(
            "The `databricks` CLI was not found on PATH. Install it "
            "(https://docs.databricks.com/dev-tools/cli/install.html), then re-run."
        )
    try:
        get_databricks_token(workspace, profile)
        return
    except (RuntimeError, FileNotFoundError):
        # No/expired session (or the CLI vanished after the which() check) — log in.
        pass
    print_note(f"Launching Databricks sign-in for {workspace}...")
    cmd = ["databricks", "auth", "login", "--host", workspace]
    if profile:
        cmd += ["--profile", profile]
    try:
        subprocess.run(cmd, check=True, timeout=300)
    except FileNotFoundError as exc:
        raise RuntimeError("`databricks` CLI was not found on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"`databricks auth login` failed for {workspace}.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("`databricks auth login` timed out.") from exc
    # Surface a half-finished login now, not as a silent proxy 401 later.
    get_databricks_token(workspace, profile)


def launch(
    workspace: str,
    profile: str | None,
    provider: str,
    models: list[str] | None = None,
    open_app: bool = True,
) -> None:
    """Configure Claude Desktop for ``provider`` and run the refresh proxy.

    Blocks until interrupted: the proxy must outlive the call, since Desktop is a
    separate long-lived GUI process (not a child we exec). When ``open_app`` is set
    (the default), (re)launch Desktop so it picks up the config with no manual step.
    """
    anthropic_oauth = _resolve_anthropic_oauth()
    _ensure_databricks_session(workspace, profile)

    extra_headers = {
        gateway_proxy.AUTHORIZATION_HEADER: f"Bearer {anthropic_oauth}",
        MODEL_PROVIDER_SERVICE_HEADER: provider,
        "User-Agent": f"ucode/{ucode_version()} claude-desktop/{agent_version('claude')}",
    }
    server, cache, client = gateway_proxy.start_proxy(
        workspace,
        profile,
        port=0,  # OS picks a free port
        token_header=gateway_proxy.AI_GATEWAY_TOKEN_HEADER,
        force_refresh_near_expiry=False,
        extra_headers=extra_headers,
        strip_client_headers=_STRIP_CLIENT_AUTH_HEADERS,
    )
    bound_port = server.server_address[1]
    base_url = f"http://{LOOPBACK_HOST}:{bound_port}"

    path = config_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_existing_file(path, path.with_suffix(".json.ug-backup"))
    write_json_file(path, render_config(base_url, models or []))
    os.chmod(path, _CONFIG_FILE_MODE)
    # A bare file is invisible to Desktop until registered + applied; keep the prior.
    prior_applied = register_active_config(config_entry_id(workspace))

    print_success(f"Gateway refresh proxy running at {base_url}")
    print_note(f"Registered + applied the '{_ENTRY_NAME}' gateway config.\nConfig: {path}")
    if open_app:
        relaunch_desktop_app()
    else:
        print_note(
            "Fully quit Claude Desktop (Cmd-Q) and reopen it to pick up the config — it reads "
            "the config only at startup. (Pass --open to have ug (re)launch it for you.)"
        )
    print_note("Keep this command running while you use Desktop; closing it stops the proxy.")
    try:
        # serve_forever() unwinds only on shutdown()/KeyboardInterrupt, not on stray
        # signals — signal.pause() here dropped the proxy on the first inference's SIGCHLD.
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cache.stop()
        server.shutdown()
        client.close()
        # Don't leave Desktop pinned to the now-dead proxy.
        restore_active_config(prior_applied)
        print_warning(
            "Gateway refresh proxy stopped; Claude Desktop can no longer reach the gateway. "
            "Restored the previously-applied config."
        )
