"""Console-script entry point for ``ug`` / ``ucode``.

Launchers such as isaac run ``ug --version`` on every start. Importing ``ucode.cli`` (Typer, Rich,
questionary, every subcommand module) costs far more than printing the version, so a bare version
request is answered here without importing it. Every other invocation goes to ``ucode.cli.main``.
"""

from __future__ import annotations

import os
import sys

_DISTRIBUTION = "unity_gateway"
_VERSION_ARGV = (["--version"], ["-V"])


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
    if sys.argv[1:] in _VERSION_ARGV:
        version = _read_installed_version()
        if version is None:
            from ucode.telemetry import ug_version

            version = ug_version()
        print(version)
        return

    from ucode.startup_timing import startup_mark, startup_phase

    startup_mark("entrypoint")
    with startup_phase("cli_import"):
        from ucode.cli import main as cli_main

    cli_main()
