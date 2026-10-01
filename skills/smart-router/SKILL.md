---
name: smart-router
description: Enable or disable Unity Gateway subagent model routing for the current smart-routed Claude or Codex session.
allowed-tools: Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --enable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --disable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --enable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing)
metadata:
  version: "1.1.0"
---

# Smart Router

Use the Python executable exported by the launching Unity Gateway installation.
Keep its path quoted and do not substitute `ug` or `python` from PATH.

- In Claude Code, run exactly `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --enable-smart-routing` for `on` or
  `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --disable-smart-routing` for `off`.
- In Codex, run exactly `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --enable-smart-routing` for `on` or
  `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing` for `off`.

In PowerShell, use `& "$env:UCODE_SMART_ROUTER_PYTHON"` in place of
`"$UCODE_SMART_ROUTER_PYTHON"`; keep the remaining arguments the same.
If `UCODE_SMART_ROUTER_PYTHON` or `UCODE_SESSION_ENV_FILE` is unset, ask the user
to restart through an updated Unity Gateway with smart routing enabled.

With no argument, explain that only `on` and `off` are accepted. Do not edit the state file.
This affects subsequent subagent model selection in the current session, not the root model or
first prompt. Return the command's result.
