---
name: smart-router-orchestrator

description: >-
  Delegate substantive coding, debugging, reviews, repository questions,
  research, and verification to cheaper native subagents, in
  parallel where the work splits. Use by default for development tasks when
  UG activates orchestration; skip easy tasks the root can finish faster
  itself and explicit no-subagent requests.

model: inherit
argument-hint: "[task]"
metadata:
  version: "1.3.0"

---

# Smart Router Orchestrator

## Activation

If this workflow is truncated and the full instructions are not already in
context, read `SKILL.claude.md` from the skill directory shown in the activation
context before proceeding.

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
checks, and the draft result.
Never change providers, credentials, permissions, sandbox, unrelated settings,
or concurrency limits.
Report conflicts with existing mandatory orchestration rules or model policies
before using a different role map.

## Delegation gate

Choose ownership from the unresolved work before investigating it. A connected
investigation, including its dependent questions, can itself be one complete
owner assignment; it need not contain a separate parallel branch. Having the
necessary context means already having decisive evidence for the caller's
requirements, not merely knowing the vocabulary, file names, or entrypoints.
Finish in the root when retained evidence and a bounded check suffice, or when
briefing and acceptance would repeat the work. Otherwise compare complete
child ownership plus acceptance with acquiring and checking all that evidence
in the root. Delegate when that assignment replaces enough root work to justify
its execution and acceptance; name the displaced work and keep it with its owner.
Broad scope, many files, or a cheaper model alone is insufficient. Natural
zero-child outcomes remain valid, and explicit no-delegation requests take
precedence.

Choose the execution path after the gate settles. Do not split by numbered questions,
files, or languages alone, and do not reserve a connected branch merely to keep
the parent busy. Launch ready independent assignments together. Keep required
independent checks, but do not add an automatic reviewer or manager stage.
If spawning is unavailable or prohibited, continue locally within the caller's
instructions. Reuse complete instructions already received; recover only missing
or truncated portions instead of rereading them as a setup ritual.

| Role | Scope | Claude default |
| --- | --- | --- |
| explorer | Read code and callers; map existing patterns/tests; no edits | Sonnet |
| researcher | Verify external/API facts with primary sources; no edits | Sonnet |
| worker | Implement one bounded change in explicitly owned files | Sonnet |
| tester | Independently run checks and report failures; edit tests only if assigned | Sonnet |
| reviewer | Review the actual diff for correctness, regressions, security, and missing tests; no edits | Sonnet |

## Assign and coordinate

Brief each owner on the outcome, context, scope, constraints, authority,
acceptance criteria, evidence, and delivery rule below. Pass established findings,
decisions, relevant locations, and known validation commands so the child need
not rediscover solved work. Repeat the existing no-recursive-teams and
return-to-named-owner boundary in every child brief; inherited policy text is
insufficient. Include role constraints and these research rules so
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

Keep the assigned research or edit/test/fix loop with its owner. Return a
single answer-shaped handoff mapping every assigned caller requirement to
decisive evidence, validation results, unresolved issues, and the requested
presentation. Before acceptance, every caller-required evidence item must be cited. If one is missing, the same owner must make a focused amendment to obtain it or state the concrete unavailable dependency before returning partial status; do not restart the whole investigation. Do not restate the parent's plan, assignment, or source narrative
when it does not substantiate a caller requirement. Preserve every material
qualification and required check; do not add a duplicate checklist or speculative
scope. Check coverage and important claims before returning. A terminal reply
delivers the result, not a progress update; if blocked, state completed coverage,
the dependency, and explicit partial status. The root may assemble the final
cross-owner response from these handoffs without reconstructing the answer from
raw facts.

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
**Omit `model`**: role frontmatter selects the configured alias or full ID. Include
role scope and task contract in `prompt`; run independent children in the
background when supported. Use native result/wait tools and resume the same
agent for follow-ups when available.

Omitted role tool lists inherit parent tools, including deferred MCP tools;
parent permissions and hooks still apply. Read-only scope is instructional.
Configured agents have distinct names. Report missing definitions as requiring
reload/restart; do not substitute built-ins. Per-call model overrides are alias-only
on the tested host; custom IDs belong in definitions. Managed forced-model policy
takes precedence; report conflicts without clearing it.

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
