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
acceptance; a delegated owner owns its complete discovery or implementation
assignment and its checks. Choose who synthesizes the final answer before
assigning the work.
Never change providers, credentials, permissions, sandbox, unrelated settings,
or concurrency limits.
Report conflicts with existing mandatory orchestration rules or model policies
before using a different role map.

## Delegation gate

Choose direct execution or a delegated owner for each complete deliverable.
Direct execution fits work the root can finish from available evidence and a
small amount of checking. Knowing the task's vocabulary or file locations does
not mean its investigation is already done. For substantial discovery, consider
one owner for the complete investigation and answer, including its dependent
questions. Compare that owner's work plus acceptance with doing it in the root.
A natural zero-child outcome remains valid; an explicit no-delegation request
takes precedence.

For a connected read-only investigation whose final answer the root must deliver,
assign the complete evidence acquisition to one owner and keep the final synthesis
in the root. The owner traces the relevant conditions and dependencies and returns
the decisive evidence for every caller requirement; it need not write a second
polished answer. The root does not retain a parallel source investigation. For
implementation, transfer the complete edit, test, and fix loop with the change.
Split only assignments that can each be accepted independently, or a required
independent check. Launch ready independent assignments together.
If spawning is unavailable or prohibited, continue within the caller's instructions.
Reuse complete instructions already received and recover only missing portions.

| Role | Scope | Codex default |
| --- | --- | --- |
| explorer | Read code and callers; map existing patterns/tests; no edits | Luna |
| researcher | Verify external/API facts with primary sources; no edits | Luna |
| worker | Implement one bounded change in explicitly owned files | Luna |
| tester | Independently run checks and report failures; edit tests only if assigned | Luna |
| reviewer | Review the actual diff for correctness, regressions, security, and missing tests; no edits | Luna |

## Assign and coordinate

Brief each owner on the caller's deliverables, context, authority, role constraints,
acceptance criteria, and established evidence. Preserve the caller's scope without
adding optional inventories, background surveys, or speculative requirements.
Include the following execution contract in the actual child assignment; do not
assume the child inherits this skill. The root uses the same contract when working
directly.

> Start from supplied evidence and entrypoints. Before each tool call, identify
> what it can settle that retained evidence cannot. Obtain the smallest sufficient
> evidence: locate with file names or symbols,
> then inspect the relevant definition and connected conditions or callers.
> Read the complete function or configuration block needed to support the claim;
> read a whole file when its flow is required. Batch independent queries and
> combine overlapping windows. Reuse established facts and citation spans.
> Preserve explicit searches, exhaustive coverage, required checks, and material
> qualifications. Continue for an uncovered requirement, contradiction, or
> required validation; related names alone do not expand the assignment.
> Return the complete assigned evidence or change, with decisive cited excerpts,
> their relevant conditions, validation evidence, and explicit unresolved items.
> For an evidence assignment, place each finding beside its supporting excerpt
> and necessary interpretation. Cover every caller requirement without a second
> reader-facing narrative; the parent must be able to answer from this handoff
> without repeating discovery. Preserve requested quotations and inventories.
> Do not turn a terminal answer into a progress update.

Pass known locations, decisions, and validation commands with the assignment.
Do not rewrite the task as a larger investigation or attach a second overlapping
checklist. Use workers for implementation and one writer per file. The parent
must not repeat work that remains assigned to a child.

Assign common prerequisites to one discovery owner and share its concise cited
findings with dependent owners. Merge assignments that would reconstruct the same
investigation; preserve targeted checks rather than banning shared file reads.

Keep the assigned research or edit/test/fix loop with its owner. An evidence owner
returns the complete findings and support needed for the root's final synthesis;
an implementation owner returns the completed change and validation. Preserve
decisive excerpts, citations, relevant conditions, and explicit unresolved issues.
Use one finding-with-evidence handoff rather than both a polished draft answer
and an evidence report. Raw search transcripts alone are not a complete handoff.
The brief must request this evidence, rather than asking for conclusions and links
alone. Preserve requested quotations and complete caller coverage.
Check that each important claim follows from its evidence before returning; a
nearby symbol or default is not proof of an entire execution path. Continue only
for a missing caller requirement, material contradiction, or required validation.
A terminal reply delivers the finished result, not a progress update; if blocked,
state completed coverage, the dependency, and explicit partial status. Preserve
the requested presentation and all required checks.

Keep private inter-agent source references lossless but compact. When parent and
child share an unambiguous workspace root, declare it once and cite relative paths
with necessary line numbers; this changes reference encoding, not coverage. Preserve
complete evidence, quotations, conclusions, unresolved limits, distinct evidence,
and qualifications; use qualified roots when files span workspaces. Preserve
required paths and citation formats in final output, use concise clickable anchored
links where permitted, avoid repeating findings in a second source catalog, and
follow higher-priority handoff formats.

Use the owner's current state to choose the next action:

| Owner state | Parent action |
| --- | --- |
| Running | Work on an independent deliverable or handle a concrete dependency. Otherwise wait for completion. |
| Returned | Check requested coverage and decisive evidence. Accept supported conclusions and integrate the answer. |
| Correction assigned | Send the missing claim, evidence to check, and completion condition together. That claim remains owned by the child until its amendment returns. |
| Unavailable | Explicitly take over using retained evidence and record unresolved gaps. |

During a pending correction, do not concurrently reconstruct that same claim.
Required independent verification remains a separate, explicitly scoped check.
Return only the amendment on a follow-up; do not regenerate an accepted report.
Send early messages only for a changed requirement, blocker, or time-sensitive
dependency. An elapsed timeout alone is not such a dependency.

Use one pending completion wait for the actual returned child IDs. Start with
the longest documented wait that fits the next required update or actionable
deadline; omit short readiness probes before it. Where the host requires polling
and supports a tool-side loop, keep those readiness checks inside that operation
and return on completion, a concrete error, or the update deadline. Never busy-loop.
Match the outer tool's wait duration to the pending inner operation. If the outer
call yields, resume its exact returned cell ID before starting another wait.
Do not re-enter investigation or alter output limits because a wait yielded.
Preserve required progress updates, immediate blocker handling, and acceptance.

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

### Codex adapter

By default, every initial native `spawn_agent` call must explicitly carry
`model="gpt-5.6-luna"`. Omit `reasoning_effort` so the model selected by
routing uses its supported default.
Attempt this model even if absent from the tool's partial preview. Do not retry
another spelling, invent aliases, or substitute a successor. Send the role scope
and contract in `message`; use `fork_turns="none"` if overrides require fresh context. Use the
host's native follow-up, message, wait, and close tools. Do not choose a custom
role that pins a different model or effort.

Native spawning needs no role TOMLs or global `[agents]` defaults. Never simulate
delegation with nested CLIs. Read-only role scope is instructional unless the host
enforces per-child restrictions.

### Recover a Codex delegation

Before every retry, check these conditions in order:

1. Did `spawn_agent` return a child ID for this assignment? If yes, **never spawn
   a replacement**, even after closing it. An error from wait, notification, or
   the child provider is a child failure, not a rejected spawn. Report it unmet.
2. Is the error permission, authentication, or capacity related? Stop. No alias
   retry, inherited fallback, or changes to permissions, credentials, or limits.
3. Did `spawn_agent` itself reject the model/effort before returning any child ID?
   Only this selection failure (or a schema without overrides) permits recovery.

Allow fallback only for the bundled defaults. Honor explicit settings and
conversation/policy constraints; never change roles or configuration to evade them.

If eligible, disclose the failure and **attempt one native spawn omitting both
`model` and `reasoning_effort`**, with the same contract and fresh context (`fork_turns="none"`
when exposed). A routing hook may select a model; otherwise harness defaults or
inheritance apply. Do not assume routing ran or fallback will succeed. If forbidden
or unsuccessful, stop retrying and report the error and unmet assignment.

## Integrate and verify

Maintain one mapping from the caller's required deliverables to their current
owner and returned evidence. Use it to check coverage; it need not be an extra
user-facing checklist or a new artifact. A completed child covers its assignment,
not automatically the entire request.

Collect ready child results without publishing each as a separate full answer.
Progress updates report progress and concrete dependencies. At finalization,
assemble one complete answer from all accepted results and necessary amendments.
Earlier progress text does not substitute for content required in the final answer.
Preserve the caller's requested presentation, evidence, conditions, and exceptions.

Review the returned evidence before deciding whether another tool call is needed.
For each important conclusion, check that the quoted source conditions, actual diff,
or validation result support its scope and that the caller's requirement is covered.
A bare conclusion or citation without sufficient supporting evidence is a gap;
ask the owner for that evidence or retrieve the specific missing source window.
When the returned evidence is sufficient, accept it without fetching the same
implementation again. Retrieving every cited file is not a default review phase.
Reopen a concrete contradiction, missing condition, changed input, or required check.
Keep explicitly required independent verification and substantive tests. Add another
reviewer only for a distinct risk or a requested independent check. Integrate accepted
components without repeating their discovery or generating a second full draft.

Before delivery, check the assembled answer against the complete caller request
and every accepted handoff. Correct missing or conflicting components with their
owners. If a component cannot be completed, state the exact gap and explicit
partial status; do not imply that the latest returned component completes the task.
For an empty or unrelated result, clarify once with the same child and verify the
amendment. If it remains unusable, report the unmet assignment without respawning.
Account for each required child and report unavailable tools or substitutions.

Finish with the concrete result, verification performed, and material remaining
limitations. Do not claim cost or speed improvements without measurements.

Use runtime evidence to identify the model that ran; submitting a delegation
alone does not prove that its routing hook executed.
