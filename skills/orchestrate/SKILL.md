---
name: orchestrate
description: Coordinate substantive development with native subagents when ENABLE_ORCHESTRATION=1 and Unity Gateway smart routing is enabled. Follow the routing-state check before using this workflow. Skip easy tasks and explicit no-subagent requests.
model: inherit
argument-hint: "[task]"
metadata:
  version: "1.1.0"
---

# Model orchestrator

## Smart-routing gate

This workflow is active only in a UG-launched smart-routing session with
`ENABLE_ORCHESTRATION=1` and routing enabled. Installed skill files and old
context do not enable it. Before **every new delegation under this workflow**,
check the same session controls
as the routing hooks with the launching interpreter:

```text
"$UCODE_SMART_ROUTER_PYTHON" -m ucode.smart_routing.orchestrator --check
```

The command succeeds silently when both orchestration and routing are enabled.
In PowerShell, use `& $env:UCODE_SMART_ROUTER_PYTHON` in place of
`"$UCODE_SMART_ROUTER_PYTHON"`.
If the interpreter is absent or the command fails, do not use this workflow;
report the problem and continue authorized work in the root. Never choose
another Python from PATH, set orchestration or routing flags, or create a session
to bypass this check.

Turning Smart Router off also turns this workflow off immediately and supersedes
earlier orchestration instructions. Do not start new automatic delegation or use
orchestrator role models as a fallback. Continue in the root unless the user
explicitly requests a subagent; honor that request using the native tool and normal harness
model selection, without this workflow or its routing check. Keep routing off
and collect results from existing children. Turning Smart Router back on restores
this workflow only if the session was launched with `ENABLE_ORCHESTRATION=1`.
Use the `smart-router` skill only when the user asks to change routing.

## Workflow

Follow user overrides. Keep the active root model and reasoning effort. The root
owns planning, architecture, decomposition, integration, conflicts, and final
verification; children execute bounded tasks. Smart routing selects child models;
do not apply separate role-model preferences or reasoning-effort overrides.
Never change providers, credentials, permissions, sandbox, unrelated settings,
or concurrency limits.
Report conflicts with existing mandatory orchestration rules or model policies.

Perform all required setup checks without narrating successful results. Before
delegating, describe the task split in at most one short sentence, then launch
ready work. Explain interpreter, routing-gate, or adapter details only
when requested or needed to explain a failure or blocker. Keep later updates
focused on findings, blockers, and results.

## Delegation gate

Delegate to save the root's context and overall cost: cheaper children return
concise results instead of raw tool output. Give them the bulk of broad searches,
multi-area investigations, implementation, external research, and verification.
Keep latency low by running independent children in parallel and easy work in the
root.
Users need not mention this skill or request agents.

Keep a task in the root when briefing, waiting for, and integrating a child would
take longer: a self-contained answer, mechanical edit, explanation or review of a
small file already read, small single-scope change with obvious verification, or
quick check. These save little cost and add little root context.
An explicit request not to delegate takes precedence. If spawning is unavailable
or policy prevents it, explain and continue locally within the user's instructions.
Do not invent work to increase the agent count.

Before substantive work, identify the root's share and independent pieces worth
delegating. Launch ready pieces together and do the root's share while they run.
Avoid serial chains when inputs exist. Do not add a reviewer or tester to a trivial
fix or split a small change across workers. Size fan-out to the work; do not require
a fixed pipeline.

| Role | Scope |
| --- | --- |
| explorer | Read code and callers; map existing patterns/tests; no edits |
| researcher | Verify external/API facts with primary sources; no edits |
| worker | Implement one bounded change in explicitly owned files |
| tester | Independently run checks and report failures; edit tests only if assigned |
| reviewer | Review the actual diff for correctness, regressions, security, and missing tests; no edits |

## Assign and coordinate

Give each independent lane an owner and outcome. Brief children on context,
file scope, constraints, authority, acceptance criteria, and evidence. Include
role constraints and research rules in each task prompt so routing preserves
them. Use workers for implementation, one writer per file; the root must not
duplicate their work.

Research needs sources and a deadline or request budget. Name tools exactly,
with verified capability/auth status; children discover deferred tools in their
own catalog. After auth failure or denial, stop that operation and report its
exact tool and redacted error. Await the supervisor before fallback; no unchanged
retries or tool/provider/shell evasion. Independent authorized work may continue.
Fetch supplied/discovered links. After a 404, discover the actual link via permitted
search/site navigation or report it unavailable; no guessed paths or budget
expansion. Return partial evidence if blocked.

Use native peer messaging for concrete dependencies, or relay through the root.
Children report plan-changing outcomes, unresolved dependencies, and final
results with evidence, checks, and limitations. Reuse children for follow-ups when
supported; no recursive teams. Return architectural, API, security, scope, or
ambiguous decisions to the root for integration, conflict resolution, and final
verification.

### Claude Code adapter

Use native `Agent` (`Task` on older hosts) with `subagent_type="ug-smart-router:<role>"`.
**Omit `model`**: the routing hook selects it. Include role scope and task contract
in `prompt`, because routing may replace the requested agent definition. Run
independent children in the background when supported. Use native result/wait
tools and resume the same agent for follow-ups when available.

Omitted role tool lists inherit parent tools, including deferred MCP tools;
parent permissions and hooks still apply. Read-only scope is instructional.
Report missing bundled definitions as requiring reload/restart; do not substitute
custom agents with saved model preferences. Managed forced-model policy takes
precedence; report conflicts without clearing it.

### Codex adapter

Use native `spawn_agent` without `model` or `reasoning_effort` overrides. The routing
hook selects the model. Use fresh task context (`fork_turns="none"` when exposed)
so the hook can supply a model override; full-history forks require the inherited
model. Send the needed context, role scope, and contract in `message`; use the
host's native follow-up, message, wait, and close tools. Do not choose a custom
role that pins a model or effort.

Native spawning needs no role TOMLs or global `[agents]` defaults. Never simulate
delegation with nested CLIs. Read-only role scope is instructional unless the host
enforces per-child restrictions.

If spawning fails, report the error and continue authorized work in the root.
Do not retry with model aliases or changed permissions, credentials, or limits.
Once a child ID is returned, collect that child's result rather than spawning a
replacement for the same assignment.

## Integrate and verify

Read child evidence, inspect worker diffs, and spot-check cited paths without
redoing their scope. Run the smallest independent checks of the requested outcome.
Resolve conflicts and findings before handoff. Account for every required child;
a launch or success-shaped summary alone is not completion. For empty or unrelated
results, or an already-supplied task request, clarify once with the same child.
Verify its evidence; if still unusable, report the unmet assignment without
respawning. Report unavailable models, tools, and substitutions.

Finish with the concrete result, verification actually performed, and material
remaining limitations. Do not claim cost or speed improvements without measurements.

Use runtime evidence to identify the model that ran; submitting a delegation
alone does not prove that its routing hook executed.
