---
name: smart-router-orchestrator
description: Smart Router Orchestrator coordinates substantive development with native subagents when ENABLE_SMART_ROUTER_ORCHESTRATOR=1 and Unity Gateway smart routing is enabled. Follow UG's activation context. Skip easy tasks and explicit no-subagent requests.
model: inherit
argument-hint: "[task]"
metadata:
  version: "1.2.0"
---

# Smart Router Orchestrator

## Activation

UG's prompt and compaction hooks activate this workflow only in an eligible
smart-routing session launched with `ENABLE_SMART_ROUTER_ORCHESTRATOR=1` while routing is on.
Follow the latest UG activation context and successful Smart Router toggles;
installed skill files and old context do not enable it. Use that context without
running a separate pre-delegation check. Do not set flags or create a session to
activate this workflow.

Turning Smart Router off through its skill stops this workflow and supersedes
earlier orchestration instructions. Do not start new automatic delegation or use
Smart Router Orchestrator role models as a fallback. Continue in the root unless the user
explicitly requests a subagent; honor that request using the native tool and normal harness
model selection, without this workflow. Keep routing off
and collect results from existing children. Turning Smart Router back on restores
this workflow only if the session was launched with `ENABLE_SMART_ROUTER_ORCHESTRATOR=1`.
Use the `smart-router` skill only when the user asks to change routing.

## Workflow

Follow user overrides. Keep the active root model and reasoning effort. The root
owns the delegation choice, cross-deliverable integration, conflicts, and
acceptance; a delegated owner owns task-level planning, discovery, execution,
checks, and the draft result. Smart routing selects child models;
do not apply separate role-model preferences or reasoning-effort overrides.
Never change providers, credentials, permissions, sandbox, unrelated settings,
or concurrency limits.
Report conflicts with existing mandatory orchestration rules or model policies.

Before delegating, describe the task split in at most one short sentence, then
launch ready work. Explain adapter details only
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

Before substantive work, identify the complete root work the child would replace.
For a cohesive task, give one owner the original request, acceptance criteria,
constraints, and authority to discover, check, and draft the result. Keep a
connected reasoning path with that owner even when it crosses directories or
layers; do not pre-solve it in the parent. Split only when each result is useful
and verifiable without first resolving the other child's findings, or a concrete
risk requires independent verification. Numbered questions, files, and languages
alone do not make independent deliverables.
Do not reserve broad discovery for the root merely to give it work while waiting.
A delegated owner replaces the broad discovery, execution, and checks assigned to
it. While the child owns that scope, root work is limited to cross-deliverable
integration and targeted acceptance-gap checks; do not create an automatic
parallel root scope. After a complete report arrives, consume it as the owner's
evidence and do not restart broad discovery or reread the same area; read or
execute only what is needed to check a concrete acceptance gap or contradiction.
When no such independent check is ready, finalize or use a native wait for other
assigned work; do not invent root work to fill the interval. Use the lightest
sufficient delegation and allow a natural zero-child outcome when the root can
cover the task.
Avoid serial chains when inputs exist. Do not add a reviewer or tester to a trivial
fix or require a fixed pipeline; size fan-out to the work.

| Role | Scope |
| --- | --- |
| explorer | Read code and callers; map existing patterns/tests; no edits |
| researcher | Verify external/API facts with primary sources; no edits |
| worker | Implement one bounded change in explicitly owned files |
| tester | Independently run checks and report failures; edit tests only if assigned |
| reviewer | Review the actual diff for correctness, regressions, security, and missing tests; no edits |

## Assign and coordinate

Brief each owner on the outcome, context, scope, constraints, authority,
acceptance criteria, evidence, and delivery rule below. Include role constraints
and these research rules so they survive routing. Start from the user's named
symbols, entrypoints, files, and result surfaces; locate each and trace the
relationships needed for the requested conclusion before expanding into adjacent
areas. Keep any unlocated target explicit rather than substituting a related
surface. Use filenames and counts to find unknown locations, then scoped symbols
and line windows; refine truncated queries, reuse evidence, and read whole files
when the flow requires it. Requested inventories still require full coverage.
Use workers for implementation, one writer per file; the root must not duplicate
their work.

Each child assignment includes a return contract: own the bounded assignment through
its named checks, map every original acceptance item to an answer with the minimal
cited evidence, and mark unresolved items. For a cohesive delegation covering the
whole caller request, return an integration-ready draft that satisfies the original
caller contract, including its requested presentation, with concise separate notes
for unresolved issues or evidence gaps. For a partial delegation, return only the
assigned part in the presentation needed for the parent to integrate it, with
concise unresolved notes. Do not make the parent reconstruct the requested draft
from raw findings. Return one consolidated, decision-relevant result; do not
require duplicate full reports or mandatory verbosity. Keep broad searches and
full-source reading in the owning child context; the parent requests scoped
excerpts needed for integration or verification without hiding required proof.
Do not request an interim answer solely for a progress update; report the known
state while the child finishes and wait for its consolidated result. Do not return
raw search dumps unless they expose a blocker or plan-changing evidence. Preserve
every requested deliverable and verification step.

Keep private inter-agent source references lossless but compact. When parent and
child share an unambiguous workspace root, declare that root once in the handoff
and cite relative paths with the necessary line numbers instead of repeating the
full root in every citation. Preserve complete evidence, quotations, conclusions,
and unresolved limits; this changes reference encoding, not coverage. Use qualified
roots when files span workspaces or a relative reference would be ambiguous.
For final output, preserve required paths and citation format. Where that format
permits, introduce one clickable file link for a local group or table of findings
from that file, retaining each finding's precise line numbers. Keep separate
anchored links when required or needed to avoid ambiguity. Use concise labels;
avoid repeating the same findings in an additional source catalog. Shorten path
repetition and exposition while preserving every requested inventory, distinct
piece of evidence, and qualification. Follow higher-priority handoff formats too.

Assign one owner to each independent investigation question, including read-only
work. While that child owns its scope, the parent does not run a parallel broad
investigation there; parent work stays independent, integrative, or targeted to
correctness. The parent accepts sufficient cited evidence and reopens only an
explicit missing claim, contradiction, or required check. Transfer a missing
question once to one owner rather than repeatedly reopening completed scope.
Keep ownership stable while a report is pending. For a necessary transfer, reuse
evidence already delivered or accessible, identify the remaining gap, and stop
the prior owner's work on that question if it is still active. Request missing
handoff evidence only when needed and the owner can respond. If the owner has
failed or is unavailable, continue authorized work from available evidence and
record what is missing instead of waiting for a handoff; the recovery rules below
still apply. Reuse completed discovery. Narrow independent checks of a concrete
correctness or integration risk remain allowed.

Batch independent coordination. Let an active owner finish before sending
non-blocking findings or requests for progress or extra checks. Send earlier
messages only for a concrete blocker, changed requirement, or time-sensitive
dependency. Elapsed time alone is not evidence of a stall. Give required user
updates from the last known state without asking the child for a status report.

When only child completion remains, use automatic native completion notifications
if the host supports them. Otherwise use one native wait for the outstanding
children, with the longest supported duration allowed by the next user update or
actionable deadline. A wrapping execution call must cover that duration within
its own supported limits. For a host exposing the first-line `// @exec` pragma
and `tools.multi_agent_v1__wait_agent`, when 45 seconds fits both limits and the
next required update, the matching call is:

```js
// @exec: {"yield_time_ms": 45000}
text(await tools.multi_agent_v1__wait_agent({
  targets: ["<returned-child-id>"], timeout_ms: 45000
}));
```

Use the actual returned child ID and the host's available wait tool. Shorten both
durations together if a bound is sooner. After an early outer yield, resume the
exact returned `cell_id` under the same bounds; do not start another inner wait,
fabricate IDs, or alter output limits to simulate progress. Do not insert status
queries or new investigation between empty wait results. Preserve independent
work, targeted acceptance checks, required updates, and immediate blocker handling.

Preserve the owed deliverable across follow-ups. Do not send scope reminders
that merely restate an active assignment without new evidence or a changed
requirement. Substantive follow-ups remain owed; acknowledgment or progress is not the result.
Until the parent explicitly confirms receipt of the full report or access to its
artifact, put that report, with later requests folded in, in the final response
itself. An earlier child final is not proof of delivery. Reuse existing evidence.
The parent includes that confirmation in an existing substantive follow-up when
requesting an amendment; do not add a confirmation-only round trip. After that
confirmation, the child returns only the requested amendment; the parent integrates
it with the retained report, preserving every acceptance item. Require neither a
new file nor repetition of a report the parent has confirmed receiving or accessing.
Explicit cancellation, replacement of the assignment, or a caller request for
acknowledgment alone governs the remaining deliverable.

Every follow-up names the user-required decision that remains unresolved, the
question and evidence to check, and the stop condition. A follow-up supplements
the original assignment unless explicitly rescoping it; batch independent
unresolved questions into one message per child, and transfer each missing question
once to a single owner. Accept a complete report without a status request or extra
confirmation; use the native wait when only completion is outstanding. Preserve
targeted correctness verification and every requested deliverable. Finalize once
the requested coverage and checks are complete; do not add unrequested
investigation.

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

Build the final answer from the draft and amendments. Reconcile every original
acceptance item against the evidence before reopening sources. Preserve conditions,
exceptions, and limitations that change what the caller can conclude or do; make
these distinctions explicit rather than leaving them implicit in an excerpt or
table label. A scoped amendment updates that part of the retained report without
discarding its other findings.

Read child evidence, inspect worker diffs, and spot-check cited paths without
redoing a broad child-owned scope. Accept sufficient cited evidence and run only
the targeted correctness or risk checks needed by the requested outcome. Keep any
additional check independent of the child work or tied to a concrete integration
risk. Resolve conflicts and findings before handoff. Account for every required
child; a launch or success-shaped summary alone is not completion. For empty or
unrelated results, or an already-supplied task request, clarify once with the same
child. Verify its evidence; if still unusable, report the unmet assignment without
respawning. Report unavailable models, tools, and substitutions.

Finish with the concrete result, verification actually performed, and material
remaining limitations. Do not claim cost or speed improvements without measurements.

Use runtime evidence to identify the model that ran; submitting a delegation
alone does not prove that its routing hook executed.
