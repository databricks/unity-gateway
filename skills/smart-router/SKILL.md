---
name: smart-router
description: Enable or disable Unity Gateway subagent model routing for the current smart-routed Claude or Codex session, along with Smart Router Orchestrator when opted in.
allowed-tools: Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --enable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --disable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --enable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing)
metadata:
  version: "1.2.0"
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
This affects subsequent subagent model selection in the current session, not the root model
or first prompt. In sessions launched with `ENABLE_SMART_ROUTER_ORCHESTRATOR=1`, it also controls Smart Router
Orchestrator. When turned off, earlier Smart Router Orchestrator instructions
are superseded: do not start new automatic delegation or fall back to Smart Router Orchestrator role models.
Continue in the root unless the user explicitly requests a subagent. Honor that request using
native tools and normal harness model selection, without Smart Router Orchestrator; keep routing off.
Existing children can finish. When turned on, apply the `smart-router-orchestrator`
skill to further work only if the session was launched with `ENABLE_SMART_ROUTER_ORCHESTRATOR=1`.
Do not change the orchestration feature flag. Return the command's result.
