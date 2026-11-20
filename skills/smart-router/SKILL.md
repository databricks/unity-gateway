---
name: smart-router
description: Enable or disable Unity Gateway subagent model routing for the current smart-routed Claude or Codex session, along with Smart Router Orchestrator when opted in.
allowed-tools: ug_smart_router, Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --enable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --disable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --disable-smart-routing --smart-router-feedback *), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --enable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli smart-router-logs --harness codex --session "$CODEX_SESSION_ID" --format table), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli smart-router-logs --harness codex --session "$CODEX_SESSION_ID" --format table --decision *)
metadata:
  version: "1.11.0"
---

# Smart Router

Keep toggle and feedback replies in ordinary product language. Do not announce
skill usage, cite instructions, or show skill names, file paths, or internal tool
names unless the user asks about the implementation. Asking for a feedback reason
is ordinary user input within the requested toggle, not a permission or approval
request. Ask the user directly without explaining which instruction requires it.

In Codex, only the explicit `$smart-router off` skill command may start the off
flow. A natural-language request must leave routing unchanged; tell the user to
invoke the command. Do not interpret a quoted command or a command with a reason
appended as an off invocation. When this session has the launcher-provided native
`ug_smart_router` tool, call it with `{"action":"on"}` or `{"action":"off"}` and run no
toggle, capture, submission, or status shell commands. If the native tool is unavailable,
run exactly `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --enable-smart-routing` for `on`
or `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing` for `off`, with
no other arguments, and relay its result. If that off reports that a reason is required,
report that this session needs an updated Unity Gateway launch to turn Smart Router off;
do not ask for or pass a reason. If it refuses for another reason, relay its message and
stop. Do not restart the conversation yourself.

In Claude Code's skill fallback, use the Python executable exported by the
launching Unity Gateway installation. Keep its path quoted and do not substitute
`ug` or `python` from PATH. Run exactly
`"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --enable-smart-routing` for `on` or
  `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --disable-smart-routing` for `off`.

- In Codex only, for `logs`, run exactly
  `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli smart-router-logs --harness codex --session "$CODEX_SESSION_ID" --format table`
  and reply with ONLY a fenced code block (```text) containing its stdout exactly,
  with no text before or after it. Without the fence, Codex reflows the text, loses
  the alignment, and prepends a bullet to the first line. Do not reformat,
  summarize, or drop rows, because Codex collapses long command output.
  If `CODEX_SESSION_ID` is unset, ask the user to restart through an updated
  Unity Gateway with smart routing enabled. Claude Code handles `/smart-router logs`
  natively; do not run this command there.
- In Codex only, for `logs --id <ID>`, first check that `<ID>` is 4 to 32 hex
  characters (`0-9a-f`) and nothing else; otherwise ask for an ID from the table
  and run nothing. Then run exactly
  `"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli smart-router-logs --harness codex --session "$CODEX_SESSION_ID" --format table --decision <ID>`
  and reply with ONLY a fenced code block (```text) containing its stdout exactly,
  with no text before or after it, like the overview.

In PowerShell, use `& "$env:UCODE_SMART_ROUTER_PYTHON"` in place of
`"$UCODE_SMART_ROUTER_PYTHON"` and `"$env:CODEX_SESSION_ID"` in place of
`"$CODEX_SESSION_ID"`; keep the remaining arguments the same.
If `UCODE_SMART_ROUTER_PYTHON` or `UCODE_SESSION_ENV_FILE` is unset, ask the user
to restart through an updated Unity Gateway with smart routing enabled.

If the command reports that Smart Router is already on or already off, return that
result and stop. Do not retry, restart a turn, or request feedback for unchanged settings.

Omnigent launches (`ISAAC_LAUNCH_MODE=omni`) are exempt from this feedback flow.
Relay the toggle result and stop if no feedback is requested.
In Codex, the off call opens a native selection prompt when feedback is required:
"Provide a reason" opens text entry, and "Cancel" keeps routing on and sends nothing.
The launcher requires at least 16 characters, discloses that the reason and session
details go to the Isaac team, and handles cancellation. Wait for the tool result;
do not ask a duplicate chat question, call a separate question tool, or switch to
Plan mode. Always collect the reason through this disclosed prompt, including when
the user mentioned a reason earlier. Never supply a reason yourself, invent one,
or pad their input.
If the result reports a validation error, relay it and stop. A short reason ends
this attempt with routing still on and nothing sent. Do not reopen the prompt,
ask another question, or retry automatically; wait for the user's next request.
The launcher identifies the preceding task, freezes its routing context, starts
Isaac's background feedback submission, and turns routing off, as Claude does.
Delivery failures leave routing off.
Reason submission needs no additional shell approval. Do not submit the same
report through `/isaac-feedback` or another command.
Relay the native result briefly, including any notices. If delivery is pending,
say the user can keep working; do not poll or block the conversation. The launcher
reports completion. Keep routing off throughout delivery; never claim delivery
succeeded merely because routing is off.
In Claude's skill fallback, if the off result requires feedback, routing is still
on. Ask why the user is turning Smart Router off and explain that their reason and
session details will be sent to the Isaac team. Require at least 16 characters
after trimming surrounding whitespace; ask again if shorter. Never invent or pad
their reason. Reuse a reason already supplied for this toggle. If they cancel,
stop without disabling routing or submitting feedback.
Repeat off with `--smart-router-feedback '<reason>'`
appended, quoting the exact text as one shell argument, then submit it through
`/isaac-feedback --category smart-routing <reason>` as before. Report a submission
failure without re-enabling routing. This requirement applies outside Omnigent
when Isaac sets `ISAAC_SMART_ROUTER_FEEDBACK` to `1`, `true`, `yes`, or `on`
(case-insensitive); do not change that flag to bypass it.

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
`ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI=0` hides routing details in Claude's main terminal;
Auto's and the blind orch v0 config versions set this flag. Hidden details do not mean
routing is off.
In Codex, the current main turn keeps its original routing attribution; the next user turn
gets the new state. Do not interrupt, restart, or reload any conversation.
