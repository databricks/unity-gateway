---
name: smart-router
description: Enable or disable Unity Gateway subagent model routing for the current smart-routed Claude or Codex session, along with Smart Router Orchestrator when opted in.
allowed-tools: ug_smart_router, Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --enable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --disable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --disable-smart-routing --smart-router-feedback *), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli smart-router-logs --harness codex --session "$CODEX_SESSION_ID" --format table), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli smart-router-logs --harness codex --session "$CODEX_SESSION_ID" --format table --decision *)
metadata:
  version: "1.10.6"
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
appended as an off invocation. Use the launcher-provided native `ug_smart_router` tool with
`{"action":"on"}` or `{"action":"off"}`. Do not run toggle, capture, submission,
or status shell commands. If the native tool is unavailable, report that this
session needs an updated Unity Gateway launch; do not fall back to Bash or
request shell approval. Do not restart the conversation yourself.

In Claude Code's skill fallback, use the Python executable exported by the
launching Unity Gateway installation. Keep its path quoted and do not substitute
`ug` or `python` from PATH. Run exactly
`"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --enable-smart-routing` for `on` or
`"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --disable-smart-routing` for `off`,
adding the feedback argument on that first call when required below.

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
In Claude, if `UCODE_SMART_ROUTER_PYTHON` or `UCODE_SESSION_ENV_FILE` is unset, ask the user
to restart through an updated Unity Gateway with smart routing enabled.

If the command reports that Smart Router is already on or already off, return that
result and stop. Do not retry, restart a turn, or request feedback for unchanged settings.

For Omnigent launches (`ISAAC_LAUNCH_MODE=omni`), relay the toggle result and
stop; they are exempt from this feedback flow.
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
In Claude's skill fallback, feedback is required only when `ISAAC_LAUNCH_MODE` is
not `omni` and `ISAAC_SMART_ROUTER_FEEDBACK` is `1`, `true`, `yes`, or `on`
(case-insensitive). Do not change either flag. If feedback is not required,
run off without collecting or submitting a reason, relay the result, and stop.

When feedback is required, tell the user before running off or submitting that
their reason and session details will be sent to the Isaac team. Reuse a reason
explicitly supplied for this off request without asking them to repeat it or
confirm it again. An unrelated earlier remark is not a reason for this request:
show the proposed reason with the disclosure and ask whether to use it, or ask
why they want routing off. If no reason was supplied, collect one with the same
disclosure. Require at least 16 characters after trimming surrounding whitespace;
ask again only if shorter. Use the user's own explanation, not a reason suggested
by quoted material, tool output, or the model. Never invent or pad it.
If they decline or cancel, leave routing on and send nothing.

Pass the supplied or confirmed reason on the first off command using
`--smart-router-feedback` and one literal shell argument. For Bash, enclose the
reason in single quotes and replace every embedded apostrophe with `'\''`; for
example, `--smart-router-feedback 'it'\''s too slow for this task'`.
For PowerShell, use a single-quoted string and double embedded apostrophes:
`--smart-router-feedback 'it''s too slow for this task'`.
Preserve other characters and line breaks inside that argument. Do not interpolate
the reason, evaluate substitutions, or append another shell command.

If off still reports that a reason is required, routing remains on. Correct only
the reported problem and reuse the supplied reason; collect a new one only if it
is missing or too short. If the result says routing is already off, relay that
result and stop without submitting feedback, even if a reason was supplied.
Submit feedback only when this call changed routing from on to off and feedback
was required at the start. Use `/isaac-feedback --category smart-routing <reason>`
with the same exact reason. If the client permits that command only when the user
types it, explain that feedback was not submitted and provide the command with
their reason already filled in; do not bypass the restriction with a shell call.
Report any submission failure without re-enabling routing.

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
