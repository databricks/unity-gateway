"""Keep offline native fixtures independent of machine-managed Claude configuration."""

import shlex
import shutil
import sys
from pathlib import Path


def isolate_claude_command(command: list[str], root: Path) -> list[str]:
    managed = Path("/etc/claude-code")
    if not sys.platform.startswith("linux") or not managed.exists():
        return command
    unshare = shutil.which("unshare")
    if unshare is None:
        raise RuntimeError("Offline Claude fixtures require mount isolation on a managed host")
    empty = root / "empty-managed-settings"
    empty.mkdir(exist_ok=True)
    if any(empty.iterdir()):
        raise RuntimeError("Offline managed-settings directory must be empty")
    script = shlex.join(["mount", "--bind", str(empty), str(managed)])
    script += " && exec " + shlex.join(command)
    return [unshare, "--user", "--mount", "--map-root-user", "sh", "-c", script]
