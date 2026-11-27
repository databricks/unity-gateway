---
feature: vscode-copilot
title: Copilot Chat in VS Code via the Unity Gateway extension
platform: cli
paths: [src/ucode/agents/vscode.py, src/ucode/agents/copilot.py, vscode-extension/**, src/ucode/vscode/**]
status: 'unverified: VS Code is not installed here; static read only. Checked at f83f1c65f61aeefe2884ad40891efa06ccafe2ae 2026-11-06'
---

# VS Code Copilot extension

`ug configure --agents copilot` packs a VSIX with a `ug-config.json` and installs it into VS Code (`agents/vscode.py:101,159,250`).

## Reach

`ug configure --agents copilot --workspace <url>`. `--no-vscode` sets up only the Copilot CLI (`cli.py:5013`; `request_vscode_with_copilot` at `agents/__init__.py:706`). `--provider` and `--model-location` change the model list. Revert: `ug revert` (`vscode.revert`, `vscode.py:296`).

## Drive

1. [unverified] `ug configure --agents copilot --workspace $UG_TEST_WORKSPACE`. Blocked by forced login. `code` on the mapping machine was a remote-editor stub (`code --version` exits with "unsupported option"), not a VS Code install.
2. [unverified] `ug --dry-run configure ... copilot`: prints "Dry run: VS Code isn't changed." (`agents/__init__.py:718`, from code).
3. [verified, component only] `uv run pytest tests/test_agent_vscode.py -q` is included in the 389 passed run noted in `desktop-integration`. Not a live check.

## Expect (from code, not observed)

- `ug-config.json` holds workspace URL, models (id, name, family, wire `anthropic|responses|completions`, limits), an auth command (`ug auth-token --host ...`) and managed headers.
- After install, restart VS Code and pick models in the chat picker; token and request errors go to the "Unity Gateway" output channel.
- Rebuild the bundled extension with `just vscode-build` (`vscode-extension/README.md`).

## Gotchas

- Models match the Copilot CLI's list; reconfigure to change them.
- Needs a real `code` binary; `find_code` searches install locations (`vscode.py:55-90`).
