"""``ug-claude-vscode``: launch Claude Code with ug's gateway settings from an IDE extension.

The Claude Code VS Code extension reads only the standard settings files, never the
``--settings ~/.claude/ucode-settings.json`` that ``ug claude`` passes. It can instead launch
Claude through a ``claudeCode.claudeProcessWrapper`` executable, passing its bundled Claude
binary first (when it has one) and then Claude's own arguments. This is that executable:
it adds ug's ``--settings`` and hands over, with stdin/stdout inherited so the extension
talks to Claude directly.

Keep startup small and write nothing to stdout: it runs on every Claude launch from the
extension, and stdout carries the extension's protocol.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from ucode.os_compatibility import subprocess_cross_os

# Must equal ``ucode.agents.claude.CLAUDE_SETTINGS_PATH``; not imported, to keep startup light.
UG_CLAUDE_SETTINGS = Path.home() / ".claude" / "ucode-settings.json"
# Troubleshooting: append each launch's argv (JSON) to this file.
LOG_ENV_VAR = "UG_CLAUDE_VSCODE_LOG"
_JS_SUFFIXES = {".js", ".mjs", ".cjs"}


def claude_command(args: list[str]) -> tuple[list[str], list[str]]:
    """Split off the Claude program the extension passed, or fall back to ``claude`` on PATH."""
    if args and Path(args[0]).is_file():
        program = Path(args[0])
        if program.suffix.lower() in _JS_SUFFIXES:
            node = shutil.which("node")
            if node is None:
                sys.exit(
                    "ug-claude-vscode: the extension passed a JavaScript Claude build, "
                    "but `node` is not on PATH."
                )
            return [node, str(program)], args[1:]
        return [str(program)], args[1:]
    claude = shutil.which("claude")
    if claude is None:
        sys.exit("ug-claude-vscode: no Claude binary was passed and `claude` is not on PATH.")
    return [claude], args


def main() -> int:
    if not UG_CLAUDE_SETTINGS.is_file():
        sys.exit(f"ug-claude-vscode: {UG_CLAUDE_SETTINGS} not found; run `ug configure` first.")
    program, args = claude_command(sys.argv[1:])
    conflicting_settings = next(
        (arg for arg in args if arg == "--settings" or arg.startswith("--settings=")), None
    )
    if conflicting_settings is not None:
        print(
            "ug-claude-vscode: refusing to launch because the caller passed "
            f"{conflicting_settings!r}; ug supplies the only supported --settings value. "
            "Remove that argument and retry.",
            file=sys.stderr,
        )
        return 2
    if log := os.environ.get(LOG_ENV_VAR):
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(sys.argv) + "\n")
    try:
        return subprocess_cross_os.run(
            [*program, "--settings", str(UG_CLAUDE_SETTINGS), *args], check=False
        ).returncode
    except KeyboardInterrupt:
        return 130
