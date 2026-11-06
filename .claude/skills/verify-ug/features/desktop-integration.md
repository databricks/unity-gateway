---
feature: desktop-integration
title: Claude Desktop profile, Cowork plugin and Code plugin
platform: cli
paths: [src/ucode/agents/desktop/**, docs/claude-desktop.md, src/ucode/cli.py]
status: 'unverified: configure is not drivable here (forced browser login); Desktop and Cowork are not installed. Checked at f83f1c65f61aeefe2884ad40891efa06ccafe2ae 2026-11-06'
---

# Claude Desktop integration

`ug configure` for Claude also writes a Desktop third-party profile, a Cowork plugin and a Claude Code plugin (`agents/desktop/setup.py:32,50,67,109`).

## Reach

`ug configure --agent claude --user --workspace <url>` (see `configure-agent`). `--global` skips these. Revert: `ug revert` (`setup.py:89,99,213`).

## Drive

1. [verified, component only] `uv run pytest tests/test_agent_vscode.py tests/test_code_plugin.py tests/test_desktop_setup.py tests/test_desktop_configure_cli.py tests/test_cowork_plugin.py -q` gave `389 passed` (that command also covers vscode; it is not a live check).
2. [unverified] Real configure with `--user`. It was refused or timed out; see `configure-agent`.
3. [verified] The launch auto-configure path (`ug claude -- --version`) did not run the Code-plugin step: no `.ucode/claude-code-marketplace` or `claude-code-plugin.json` appeared in the temp HOME.

## Expect (from AGENTS.md and docs, not observed)

- Desktop profile UUID `5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6` under `Claude-3p/configLibrary` (Linux: `~/.config`), with `claudeAiImport.enabled: true`, `toolSearchEnabled`, `managedMcpServers`. Metadata saved in `~/.ucode/claude-desktop.json`.
- Cowork plugin `ug@unity-gateway-cowork` from `~/.ucode/claude-cowork-marketplace`; state in `~/.ucode/claude-cowork.json`. Needs Desktop's account directory and an existing sessions directory.
- Code plugin `ug@unity-gateway-code` from `~/.ucode/claude-code-marketplace`; state `~/.ucode/claude-code-plugin.json`; only the `unity-gateway-connectors` skill.
- Revert removes the profile, wrapper and both plugins, keeping unrelated ones.

## Gotchas

- Setup is best-effort: failures warn and configure continues.
- Hidden commands `claude-desktop-auth` and `otel-headers` mint tokens; not driven.
- See `docs/claude-desktop.md` for native validation limits.
