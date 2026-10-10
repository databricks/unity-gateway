---
name: smart-router
description: Enable or disable Unity Gateway subagent model routing for the current smart-routed Claude or Codex session, along with Smart Router Orchestrator when opted in.
allowed-tools: Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --enable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing)
metadata:
  version: "1.3.0"
---

# Smart Router

In Claude Code, the native mod handles `/smart-router on|off` locally. It changes all
routing flags and the recipe in `CLAUDE_CODE_EXTRA_BODY` in the running process.
If these instructions reach the model instead, ask the user to restart through an updated
Unity Gateway with smart routing enabled; a subprocess cannot update Claude's environment.

In Codex, use the Python executable exported by the launching Unity Gateway installation.
Keep its path quoted and do not substitute `ug` or `python` from PATH. Run exactly `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --enable-smart-routing` for `on` or
  `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing` for `off`.

In PowerShell, use `& "$env:UCODE_SMART_ROUTER_PYTHON"` in place of
`"$UCODE_SMART_ROUTER_PYTHON"`; keep the remaining arguments the same.
In Codex, if `UCODE_SMART_ROUTER_PYTHON` or `UCODE_SESSION_ENV_FILE` is unset, ask the user
to restart through an updated Unity Gateway with smart routing enabled.

With no argument, explain that only `on` and `off` are accepted. Do not edit the state file.
This changes subsequent routing decisions and, in Claude, request recipe metadata.
It does not undo an already selected root model. In sessions launched with `ENABLE_SMART_ROUTER_ORCHESTRATOR=1`, it also controls Smart Router
Orchestrator. When turned off, earlier Smart Router Orchestrator instructions
are superseded: do not start new automatic delegation or fall back to Smart Router Orchestrator role models.
Continue in the root unless the user explicitly requests a subagent. Honor that request using
native tools and normal harness model selection, without Smart Router Orchestrator; keep routing off.
Existing children can finish. When turned on, apply the `smart-router-orchestrator`
skill to further work only if the session was launched with `ENABLE_SMART_ROUTER_ORCHESTRATOR=1`.
Return the command's result.
