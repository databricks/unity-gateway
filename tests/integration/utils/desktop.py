"""Native Desktop input paths and installed Cowork plugin evidence."""

import json
import os
import sys
from pathlib import Path


def desktop_config_library(session) -> Path:
    if os.name == "nt":
        return Path(session.env["LOCALAPPDATA"]) / "Claude-3p/configLibrary"
    if sys.platform == "darwin":
        return session.home / "Library/Application Support/Claude-3p/configLibrary"
    return Path(session.env["XDG_CONFIG_HOME"]) / "Claude-3p/configLibrary"


def prepare_cowork_home(session) -> Path:
    # Pre-existing Desktop account directories are scenario inputs, not UG-generated state.
    root = desktop_config_library(session).parent / "local-agent-mode-sessions" / "account" / "org"
    root.mkdir(parents=True)
    return root


def installed_cowork_servers(session, root: Path) -> dict:
    state = json.loads((session.home / ".ucode/claude-cowork.json").read_text())
    assert Path(state["cowork_root"]) == root
    installed = json.loads((root / "cowork_plugins/installed_plugins.json").read_text())
    entries = installed["plugins"]["ug@unity-gateway-cowork"]
    assert len(entries) == 1, entries
    plugin = Path(entries[0]["installPath"])
    manifest = json.loads((plugin / ".claude-plugin/plugin.json").read_text())
    assert manifest["name"] == "ug"
    assert manifest["mcpServers"] == "./.mcp.json"
    return json.loads((plugin / manifest["mcpServers"]).read_text())["mcpServers"]
