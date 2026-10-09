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

## Delegation gate

Start with the smallest plan that can deliver the caller's complete request.
The skill being available does not itself require a child. Keep a connected
investigation in the root when it already has the necessary context or when
briefing plus review would repeat the same work. Delegate when a child can own
an independently useful deliverable and replace enough root work to justify
its execution and acceptance. Name that displaced work before spawning; broad
scope, many files, or a cheaper model alone is insufficient. A natural zero-child
outcome is valid. An explicit no-delegation request takes precedence.

Choose ownership before solving the task. Do not split by numbered questions,
files, or languages alone, and do not reserve a connected branch merely to keep
the parent busy. Launch ready independent assignments together. Keep required
independent checks, but do not add an automatic reviewer or manager stage.
If spawning is unavailable or prohibited, continue locally within the caller's
instructions. Reuse complete instructions already received; recover only missing
or truncated portions instead of rereading them as a setup ritual.

| Role | Scope |
| --- | --- |
| explorer | Read code and callers; map existing patterns/tests; no edits |
| researcher | Verify external/API facts with primary sources; no edits |
| worker | Implement one bounded change in explicitly owned files |
| tester | Independently run checks and report failures; edit tests only if assigned |
| reviewer | Review the actual diff for correctness, regressions, security, and missing tests; no edits |

## Assign and coordinate

Brief each owner on the outcome, context, scope, constraints, authority,
acceptance criteria, evidence, and delivery rule below. Pass established findings,
decisions, relevant locations, and known validation commands so the child need
not rediscover solved work. Include role constraints and these research rules so
they survive routing.

Tie each investigation step to an unresolved caller requirement. Start at the
named entrypoints and locate their definitions or call sites. Batch independent
lookups, inspect the relevant function and its connected call path, and expand
only when that evidence leaves a specific relationship unresolved. A related
name or directory is not by itself a reason to investigate it. Use filename-only
search to locate files, then scoped matches and source windows; refine truncated
results instead of dumping more broad output. Read a whole file when its flow
requires it. Reuse established facts and check referenced evidence directly.
Before another tool call, identify what it can settle that the retained evidence
cannot. Once the requested claims are supported and required checks pass,
produce the result. Requested inventories still require full coverage; retain
unlocated targets, unresolved gaps, conditions, and qualifications explicitly.
Use workers for implementation, one writer per file; the root must not duplicate
their work.

Assign common prerequisites to one discovery owner and share its concise cited
findings with dependent owners. Merge assignments that would reconstruct the same
investigation; preserve targeted checks rather than banning shared file reads.

Keep the assigned research or edit/test/fix loop with its owner. Return the
integration-ready answer or change, decisive evidence, validation results, and
unresolved issues, not routine logs or search dumps unless needed for verification
or requested. Cover every assigned caller requirement and material qualification
without speculative scope expansion, arbitrary caps, or a duplicate checklist.
Check coverage and important claims before returning. A terminal reply delivers
the result, not a progress update; if blocked, state completed coverage, the
dependency, and explicit partial status. Preserve the requested presentation and
all required checks; do not make the parent reconstruct the answer from raw facts.

Keep private inter-agent source references lossless but compact. When parent and
child share an unambiguous workspace root, declare it once and cite relative paths
with necessary line numbers; this changes reference encoding, not coverage. Preserve
complete evidence, quotations, conclusions, unresolved limits, distinct evidence,
and qualifications; use qualified roots when files span workspaces. Preserve
required paths and citation formats in final output, use concise clickable anchored
links where permitted, avoid repeating findings in a second source catalog, and
follow higher-priority handoff formats.

Keep each missing claim with its investigator until the correction returns or the
root explicitly takes over; stop prior work when transferring ownership. Do not
investigate the same unresolved claim concurrently under a correctness label.
If its owner fails or is unavailable, continue from available evidence and record
what remains. Preserve required independent checks.

Let an active owner finish; send early messages only for a blocker, changed
requirement, or time-sensitive dependency, not to request an interim answer for
status. When only completion remains, use native completion notifications where
supported, otherwise the host's documented wait within the next required update
or actionable deadline. Use the longest supported wait fitting that bound and
coordinate any wrapping execution limit with it.

Use actual returned child IDs; do not fabricate IDs or start duplicate waits.
Empty timeouts alone do not justify status queries or new investigation. Preserve
independent work, acceptance checks, required updates, and immediate blocker
handling; elapsed time alone does not prove a stall.

Continue substantive corrections with the same available owner. Name the missing
decision, evidence to check, and stop condition; batch independent questions.
Follow-ups supplement the original assignment unless explicitly rescoped. Return
only the amendment, or reconstruct a missing deliverable from existing evidence.
Do not add confirmation-only rounds or repeat the entire report.

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

## Integrate and verify

Parent acceptance is the default review. Add a reviewer only for a distinct risk,
an explicit request, or a required independent check, not as an automatic stage.
Inspect actual changes and verify important claims against cited evidence and the
caller contract; a completion message alone is not proof. Reopen a named gap,
contradiction, changed input, or required check from the smallest useful source
window instead of repeating broad discovery. Preserve substantive correctness
checks, conditions, exceptions, limitations, citations, and requested presentation.

Integrate the owner's draft and narrow amendments without restarting the
investigation or regenerating the full narrative. Deliver the complete answer;
if blocked, report completed coverage and explicit partial status. Resolve
conflicts and findings before handoff. Account for every required
child; a launch or success-shaped summary alone is not completion. For empty or
unrelated results, or an already-supplied task request, clarify once with the same
child. Verify its evidence; if still unusable, report the unmet assignment without
respawning. Report unavailable models, tools, and substitutions.

Finish with the concrete result, verification actually performed, and material
remaining limitations. Do not claim cost or speed improvements without measurements.

Use runtime evidence to identify the model that ran; submitting a delegation
alone does not prove that its routing hook executed.
