---
name: smart-router
description: Enable or disable Unity Gateway subagent model routing for the current smart-routed Claude or Codex session, along with Smart Router Orchestrator when opted in.
allowed-tools: Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --enable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --disable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --disable-smart-routing --smart-router-feedback *), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --enable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing --smart-router-feedback *), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli smart-router-logs --harness codex --session "$CODEX_SESSION_ID" --format table), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli smart-router-logs --harness codex --session "$CODEX_SESSION_ID" --format table --decision *)
metadata:
  version: "1.8.0"
---

# Smart Router

Use the Python executable exported by the launching Unity Gateway installation.
Keep its path quoted and do not substitute `ug` or `python` from PATH.

- In Claude Code, run exactly `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --enable-smart-routing` for `on` or
  `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --disable-smart-routing` for `off`.
- In Codex, run exactly `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --enable-smart-routing` for `on` or
  `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing` for `off`.
- In Codex only, for `logs`, run exactly
  `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli smart-router-logs --harness codex --session "$CODEX_SESSION_ID" --format table`
  and reply with its stdout verbatim in a fenced code block. Do not reformat,
  summarize, or drop rows, because Codex collapses long command output.
  If `CODEX_SESSION_ID` is unset, ask the user to restart through an updated
  Unity Gateway with smart routing enabled. Claude Code handles `/smart-router logs`
  natively; do not run this command there.
- In Codex only, for `logs --id <ID>`, first check that `<ID>` is 4 to 32 hex
  characters (`0-9a-f`) and nothing else; otherwise ask for an ID from the table
  and run nothing. Then run exactly
  `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli smart-router-logs --harness codex --session "$CODEX_SESSION_ID" --format table --decision <ID>`
  and reply with its stdout verbatim in a fenced code block, like the overview.

In PowerShell, use `& "$env:UCODE_SMART_ROUTER_PYTHON"` in place of
`"$UCODE_SMART_ROUTER_PYTHON"` and `"$env:CODEX_SESSION_ID"` in place of
`"$CODEX_SESSION_ID"`; keep the remaining arguments the same.
If `UCODE_SMART_ROUTER_PYTHON` or `UCODE_SESSION_ENV_FILE` is unset, ask the user
to restart through an updated Unity Gateway with smart routing enabled.

If the command reports that Smart Router is already on or already off, return that
result and stop. Do not retry, restart a turn, or request feedback for unchanged settings.

If Codex reports permission denied reading the session controls, request native
approval for the same command with `sandbox_permissions="require_escalated"` and
retry only after approval. If approval is unavailable or denied, report that routing
was not changed. Do not change file permissions or disable the sandbox.

If the off command requires feedback, routing is still on. Ask why the user is
turning Smart Router off. Require at least 16 characters after trimming surrounding
whitespace; ask again if the reason is shorter. Never invent or pad their reason.
If they cancel, stop without disabling routing. Once they provide a valid reason,
repeat the off command with `--smart-router-feedback '<reason>'` appended, quoting
the exact text as one shell argument. After a successful toggle, submit that text
through `/isaac-feedback --category smart-routing <reason>`. Report a submission
failure without re-enabling routing. This requirement applies when Isaac sets
`ISAAC_SMART_ROUTER_FEEDBACK` to `1`, `true`, `yes`, or `on` (case-insensitive);
do not change that flag to bypass it.

With no argument, explain that only `on`, `off`, and (in Codex) `logs` and `logs --id <ID>` are accepted. Do not edit the state file.
This affects subsequent subagent model selection in the current session, not the root model
or first prompt. In sessions launched with `ENABLE_SMART_ROUTER_ORCHESTRATOR=1`, it also controls Smart Router
Orchestrator. When turned off, earlier Smart Router Orchestrator instructions
are superseded: do not start new automatic delegation or fall back to Smart Router Orchestrator role models.
Continue in the root unless the user explicitly requests a subagent. Honor that request using
native tools and normal harness model selection, without Smart Router Orchestrator; keep routing off.
Existing children can finish. When turned on, apply the `smart-router-orchestrator`
skill to further work only if the session was launched with `ENABLE_SMART_ROUTER_ORCHESTRATOR=1`.
Do not change the orchestration feature flag. Return the command's result briefly, then stop.
In Codex, the current main turn keeps its original routing attribution; the next user turn
gets the new state. Do not interrupt, restart, or reload any conversation.
