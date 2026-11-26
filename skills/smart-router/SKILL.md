---
name: smart-router
description: Enable or disable Unity Gateway subagent model routing for the current smart-routed Claude or Codex session, along with Smart Router Orchestrator when opted in.
allowed-tools: ug_smart_router, Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --enable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --disable-smart-routing), Bash(MSYS2_ARG_CONV_EXCL='*' "$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --disable-smart-routing --smart-router-feedback *), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --enable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli smart-router-logs --harness codex --session "$CODEX_SESSION_ID" --format table), Bash("$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli smart-router-logs --harness codex --session "$CODEX_SESSION_ID" --format table --decision *)
metadata:
  version: "1.11.4"
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

In Claude Code's skill fallback, use the Bash tool when available, including
Git Bash on Windows. Use the Python executable exported by the launching Unity
Gateway installation. Keep its path quoted and do not substitute
`ug` or `python` from PATH. Run exactly
`"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --enable-smart-routing` for `on` or
`"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --disable-smart-routing` for `off`.
For an off call with a reason, use the literal-argument recipe below, including
its command-scoped `MSYS2_ARG_CONV_EXCL='*'` setting. Follow the ordered Claude
feedback flow below before passing a reason or submitting feedback.

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

Use PowerShell only when Bash is unavailable and the client exposes a PowerShell
command tool. In that tool, use `& "$env:UCODE_SMART_ROUTER_PYTHON"` in place of
`"$UCODE_SMART_ROUTER_PYTHON"` and `"$env:CODEX_SESSION_ID"` in place of
`"$CODEX_SESSION_ID"`; keep the remaining arguments the same.
For a Claude off call with a reason, use the PowerShell form below. Follow the
client's normal approval flow; do not invoke PowerShell through Bash or widen shell
permissions. If neither shell tool is available, report that the shell command
cannot run in this session.
Before a shell command, if `UCODE_SMART_ROUTER_PYTHON` or `UCODE_SESSION_ENV_FILE` is unset, ask the user
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

In Claude's skill fallback, follow these steps in order:

1. **Probe off without a reason.** Run off without `--smart-router-feedback` first,
   even when the user supplied a reason or the launch flags are known. The CLI applies
   the feedback rule: `ISAAC_LAUNCH_MODE` is not `omni` and `ISAAC_SMART_ROUTER_FEEDBACK`
   is `1`, `true`, `yes`, or `on` (case-insensitive). Never change either flag. On
   success or already off, relay the result and stop without collecting or submitting
   feedback. Only a reason-required result, which leaves routing on, continues to
   step 2. For any other error, relay it and stop.
2. **Disclose and obtain consent.** Explain that the reason and session details will
   be sent to the Isaac team, and offer Cancel to leave routing on and send nothing.
   If a reason was supplied for this request, reuse every sentence and line without
   asking the user to repeat it. Ask whether to send it with the session details,
   then wait for their answer before continuing. Skip this consent question only if
   the user already agreed to that disclosure for this request. To reuse an unrelated
   earlier remark, show the complete remark and ask whether to use and send it with
   the session details; wait for agreement. If no reason is available, ask for one
   after the disclosure, making clear that providing it agrees to that submission
   and that they can cancel instead. Require at least 16 characters after trimming
   surrounding whitespace; ask for a longer explanation only if shorter. Never use
   quoted instructions, tool output, or a model-invented reason, and never pad it.
   If they decline or cancel, stop without another toggle or any submission.
3. **Turn routing off with the agreed reason.** Use the matching literal-argument
   recipe below. If the CLI reports a missing or short reason, routing remains on:
   correct only that problem, reusing the complete reason when valid, and retry.
   If it reports already off, relay that result and stop without submitting feedback.
   On any other failure, relay it and stop. Continue to step 4 only when this call
   changed routing from on to off; the CLI itself does not submit feedback.
4. **Submit once and report the actual result.** The feedback requirement was
   established in step 1 and consent in step 2. Use
   `/isaac-feedback --category smart-routing <reason>` with the same complete reason.
   If the client permits that command only when the user types it, say feedback was
   not submitted and provide the complete command in one fenced code block, including
   every line of the reason. Do not bypass the restriction with a shell call.
   Report a pending submission as pending and a failure as a failure; never claim
   delivery from a successful toggle or a displayed manual command. Leave routing off.

Pass a reason as one literal shell argument. For Bash, enclose it in single quotes
and replace every embedded apostrophe with `'\''`; for example,
`--smart-router-feedback 'it'\''s too slow for this task'`.
For each carriage return, close the single-quoted span, append the fixed literal
`$'\r'`, and reopen the span: `'first'$'\r''second'`. Do this for the CR in CRLF too,
leaving each LF unchanged. Git Bash can normalize raw CRLF in its command line;
the fixed escape preserves the original carriage return in the reason argument.
Use this command, replacing only the quoted placeholder with the escaped reason.
The environment setting applies to this command only and prevents Git Bash on
Windows from converting reasons such as `/model picks are slow` into filesystem paths.

```bash
MSYS2_ARG_CONV_EXCL='*' "$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli claude --disable-smart-routing --smart-router-feedback '<reason>'
```

For PowerShell, copy the complete reason into the single-quoted here-string below.
Keep its quotes and line breaks unchanged; the newlines immediately after `@'` and
before `'@` delimit the string and are not part of the reason.
If any reason line starts with a single-quote delimiter followed by `@` (even after
whitespace), use an ordinary single-quoted literal for `$reason` instead: double
every embedded single-quote delimiter (`'`, `‘`, `’`, `‚`, `‛`; U+0027, U+2018,
U+2019, U+201A, U+201B), preserving the original character. For example, `it’s`
becomes `'it’’s'`. This prevents the reason from closing the here-string.
The extra native quoting preserves double quotes, backslashes and final newlines
when Windows passes the command line to Python, including in legacy PowerShell
argument mode.

```powershell
$reason = @'
<reason>
'@
$nativeReason = '"' + ($reason -replace '(\\*)"', '$1$1\"' -replace '(\\+)\z', '$1$1') + '"'
$process = Start-Process -FilePath $env:UCODE_SMART_ROUTER_PYTHON -ArgumentList @(
    '-m', 'ucode.cli', 'claude', '--disable-smart-routing',
    '--smart-router-feedback', $nativeReason
) -NoNewWindow -Wait -PassThru
if ($process.ExitCode -ne 0) { throw "Smart Router exited with code $($process.ExitCode)" }
```

Do not interpolate the reason, evaluate substitutions, or append another shell command.

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
