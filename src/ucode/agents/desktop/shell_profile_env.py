"""Exports variables only to the GitHub Copilot app's login-shell environment probe.

On macOS and Linux the app builds its runtime's environment by running the user's login shell
with ``COPILOT_RESOLVING_ENVIRONMENT`` set, so a block guarded on it reaches the app and nothing
else: not terminals, VS Code or a bare ``copilot``. The app probes by the basename of ``$SHELL``.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from ucode.config_io import write_text_file
from ucode.os_compatibility import subprocess_cross_os

GUARD_VAR = "COPILOT_RESOLVING_ENVIRONMENT"
BEGIN = "# >>> unity-gateway: GitHub Copilot app >>>"
END = "# <<< unity-gateway: GitHub Copilot app <<<"
FISH_FILE_NAME = "unity-gateway.fish"
SUPPORTED_SHELLS = ("bash", "zsh", "sh", "dash", "pwsh", "fish")
_BLOCK = re.compile(rf"\n?{re.escape(BEGIN)}\n.*?{re.escape(END)}\n?", re.DOTALL)
# A profile that can stop early would skip a block appended after it.
_EARLY_EXIT = re.compile(r"(^|[;&|])\s*(return|exit)\b", re.MULTILINE)


def login_shells() -> list[str]:
    """The shells the app may probe with: the account's login shell, and ``$SHELL`` if it
    differs."""
    shells = []
    if sys.platform != "win32":
        import pwd

        try:
            shells.append(pwd.getpwuid(os.getuid()).pw_shell)
        except KeyError:
            pass
    shells.append(os.environ.get("SHELL", ""))
    names = [Path(shell).name for shell in shells if shell]
    return list(dict.fromkeys(names))


def _home() -> Path:
    return Path.home()


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or _home() / ".config").expanduser()


def _system_zdotdir() -> str | None:
    """The ``ZDOTDIR`` zsh has before it reads any user file (e.g. from /etc/zshenv)."""
    zsh = shutil.which("zsh")
    if not zsh:
        return None
    try:
        result = subprocess_cross_os.run(
            [zsh, "-f", "-c", "print -r -- ${ZDOTDIR-}"],
            env={key: value for key, value in os.environ.items() if key != "ZDOTDIR"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def profile_file(shell: str) -> Path | None:
    """The file the app's probe reads for ``shell``, or None for a shell it doesn't probe."""
    home = _home()
    if shell == "bash":
        # bash -l reads only the first of these that exists, never .bashrc.
        candidates = (home / ".bash_profile", home / ".bash_login", home / ".profile")
        return next((path for path in candidates if path.is_file()), home / ".profile")
    if shell == "zsh":
        # A ZDOTDIR set in ~/.zshenv (the common setup) still has zsh read ~/.zshenv first.
        zdotdir = _system_zdotdir()
        return Path(zdotdir).expanduser() / ".zshenv" if zdotdir else home / ".zshenv"
    if shell in ("sh", "dash", "pwsh"):
        # pwsh -Login sources ~/.profile through /bin/sh -l.
        return home / ".profile"
    if shell == "fish":
        return _config_home() / "fish" / "conf.d" / FISH_FILE_NAME
    return None


def _fish_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _block(path: Path, env: Mapping[str, str]) -> str:
    if path.name == FISH_FILE_NAME:
        lines = [f"if set -q {GUARD_VAR}"]
        lines += [f"    set -gx {name} {_fish_quote(value)}" for name, value in env.items()]
        lines.append("end")
    else:
        lines = [f'if [ -n "${{{GUARD_VAR}:-}}" ]; then']
        lines += [f"  export {name}={shlex.quote(value)}" for name, value in env.items()]
        lines.append("fi")
    return "\n".join([BEGIN, *lines, END, ""])


def _without_block(text: str) -> str:
    return _BLOCK.sub("\n", text).strip("\n") + "\n" if _BLOCK.search(text) else text


def write(path: Path, env: Mapping[str, str]) -> bool:
    """Put ug's guarded block for ``env`` in ``path``, replacing any earlier one. Returns whether
    the file changed."""
    if path.is_symlink() and not path.exists():
        raise RuntimeError(f"Refusing to write through a broken symlink: {path}")
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    rest = _without_block(text) if BEGIN in text else text
    block = _block(path, env)
    if not rest.strip():
        new = block
    elif _EARLY_EXIT.search(rest):
        new = f"{block}\n{rest}"
    else:
        new = f"{rest.rstrip(chr(10))}\n\n{block}"
    if new == text:
        return False
    write_text_file(path, new)
    return True


def remove(path: Path) -> bool:
    """Take ug's block out of ``path``; ug's own fish file is deleted. Returns whether it
    changed."""
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if BEGIN not in text:
        return False
    rest = _without_block(text)
    if path.name == FISH_FILE_NAME and not rest.strip():
        path.unlink()
    else:
        write_text_file(path, rest if rest.strip() else "")
    return True
