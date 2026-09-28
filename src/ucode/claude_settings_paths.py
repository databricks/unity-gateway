"""Claude Code settings locations.

A leaf module so `ucode.agents.claude` and `ucode.smart_routing.v2` can share them
without an import cycle.
"""

from __future__ import annotations

from pathlib import Path

from ucode.managed_files import OS, current_os

CLAUDE_CONFIG_DIR = Path.home() / ".claude"
CLAUDE_SETTINGS_PATH = CLAUDE_CONFIG_DIR / "ucode-settings.json"
# The default model is stored in Claude's default user settings, not the ucode settings.
CLAUDE_USER_SETTINGS_PATH = CLAUDE_CONFIG_DIR / "settings.json"


def managed_settings_path() -> Path | None:
    """OS-specific location of Claude Code's enterprise managed-settings.json.
    Returns None on unsupported platforms."""
    if current_os() is OS.LINUX:
        return Path("/etc/claude-code/managed-settings.json")
    if current_os() is OS.MACOS:
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return None
