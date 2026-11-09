"""Console-script entry point for ``ug`` / ``ucode``.

Launchers such as isaac run ``ug --version`` on every start. Importing ``ucode.cli`` (Typer, Rich,
questionary, every subcommand module) costs far more than printing the version, so a bare version
request is answered here without importing it. The per-agent MCP proxy also skips agent setup
imports. Other invocations go to ``ucode.cli.main``.
"""

from __future__ import annotations

import os
import sys
import tempfile

_DISTRIBUTION = "unity_gateway"
_VERSION_ARGV = (["--version"], ["-V"])
_HELPER_COMMANDS = {
    "auth-token",
    "claude-desktop-auth",
    "claude-router-hook",
    "codex-router-hook",
    "cowork-hook",
    "mcp-proxy",
    "otel-headers",
    "otel-proxy",
    "smart-router-describe",
}


def _ensure_accessible_cwd() -> str | None:
    """Move to an accessible home or temporary directory if the current one is unusable."""
    try:
        os.getcwd()
        return None
    except OSError:
        pass

    try:
        home = os.path.expanduser("~")
        os.chdir(home)
        return home
    except OSError:
        pass

    try:
        temporary = tempfile.gettempdir()
        os.chdir(temporary)
        return temporary
    except OSError:
        return None


def _warn_about_cwd_fallback(directory: str) -> None:
    """Tell interactive callers when a failed cwd probe changed path resolution."""
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if sys.argv[1:] in _VERSION_ARGV or command in _HELPER_COMMANDS:
        return
    print(
        f"ug: current working directory is not accessible; using {directory!r} instead. "
        "Relative paths and project context may differ.",
        file=sys.stderr,
    )


def _read_installed_version() -> str | None:
    """Return the installed ``unity-gateway`` version from its dist-info ``METADATA``.

    Mirrors the lookup ``importlib.metadata.version`` does for a plain directory ``sys.path``
    (first matching ``*.dist-info`` wins) without importing ``importlib.metadata``, which alone
    costs several times a bare interpreter start. Returns None for anything it does not handle
    (zip/egg entries, missing or unreadable metadata) so the caller uses the standard lookup.
    """
    for entry in sys.path:
        try:
            names = os.listdir(entry or ".")
        except (FileNotFoundError, PermissionError):
            continue
        except NotADirectoryError:
            # Windows puts the console-script launcher (e.g. Scripts\ug.exe) on sys.path; it holds
            # no distribution metadata. Other files may be zip archives, so they fall back.
            if entry.lower().endswith(".exe"):
                continue
            return None
        except OSError:
            return None
        for name in names:
            lowered = name.lower()
            if lowered.endswith((".egg-info", ".egg")) and lowered.startswith(_DISTRIBUTION):
                return None
            if not lowered.endswith(".dist-info"):
                continue
            if lowered.rpartition(".")[0].partition("-")[0].replace(".", "_") != _DISTRIBUTION:
                continue
            try:
                with open(os.path.join(entry, name, "METADATA"), encoding="utf-8") as metadata:
                    for line in metadata:
                        if not line.strip():
                            break
                        key, _, value = line.partition(":")
                        if key.strip().lower() == "version":
                            return value.strip() or None
            except OSError:
                return None
            return None
    return None


def main() -> None:
    fallback_directory = _ensure_accessible_cwd()
    if fallback_directory is not None:
        _warn_about_cwd_fallback(fallback_directory)

    if sys.argv[1:] in _VERSION_ARGV:
        version = _read_installed_version()
        if version is None:
            from ucode.telemetry import ug_version

            version = ug_version()
        print(version)
        return

    if sys.argv[1:2] == ["mcp-proxy"]:
        from ucode.mcp_proxy_runtime import main as proxy_main

        proxy_main()
        return

    from ucode.cli import main as cli_main

    cli_main()
