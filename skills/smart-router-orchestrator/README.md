# Smart Router Orchestrator

UG bundles the `smart-router-orchestrator` workflow and five Claude role definitions.
Smart-routed Claude and Codex launches install and activate this skill alongside
`smart-router` with `SMART_ROUTER_CONFIG_VERSION=subagent_orch_v0`, `subagent_orch_v1`, or
`subagent_orch_v2`. UG expands the selected version into the session's feature flags.
The `_v1` revision
enables both V2 and subagent-only routing flags alongside orchestration; subagent-only still
takes precedence, so the first prompt is not routed.
`subagent_orch_v2` keeps the V1 mappings for Claude and adds the Codex V10 opt-in for
Codex only. `ENABLE_CODEX_ORCHESTRATOR_V10` and `subagent_orch_v2` are UG-owned names,
not native Codex settings. The Codex path requires Codex 0.160.0 or newer; this scoped
guard does not change UG's global minimum.
The existing `ENABLE_SMART_ROUTER_ORCHESTRATOR=1` opt-in remains supported when
`SMART_ROUTER_CONFIG_VERSION` is unset; `subagent_only_v0` explicitly leaves orchestration off.
The feature is off by default; routing alone installs only `smart-router`.

## Codex V10 scope

The V10 flag is effective only in an eligible `ug codex` session with Smart Router
routing and orchestration enabled. It does not enable routing by itself, and the
existing Smart Router off/on controls continue to apply. The active path uses the full
bundled skill, carries the trusted child contract into delegated work, waits for
completed child evidence on eligible controller tasks instead of requesting provider-generated
replacement work, and shares the default prompt-cache key. Its provider relay is
loopback-only and configured at launch.

Handoff-only tools, report publication, compaction tool restoration, and the benchmark
`/workspace` alias are outside this port. The new config and registry behavior has
offline component coverage only; no live equivalence, quality, or savings claim is made.

`SKILL.claude.md` and `SKILL.codex.md` hold each agent's instructions. UG installs
the selected file as `SKILL.md` and injects that same version through the hooks.
Role definitions and other bundle resources remain shared.

The workflow is injected before root prompts and after compaction. The hook checks
the feature flag, UG session, and current routing controls. The skill uses that
activation context without running a separate check before delegation.
Turning Smart Router off through its skill stops new automatic delegation and
supersedes the previous workflow. Turning it on restores orchestration only in
opted-in sessions. A change made outside the conversation is observed at the next
prompt or compaction; model routing still checks the controls for each subagent.
An installed skill or saved model preference cannot enable orchestration.
Explicit user requests for subagents still use native harness behavior while routing
is off, without the Smart Router Orchestrator workflow.
User instructions take precedence, and easy tasks remain in the root.

Claude loads the bundled roles as `ug-smart-router:<role>` in its temporary
routing plugin. Claude omits model overrides; its bundled roles default to Sonnet.
Codex requests Luna without a reasoning effort override. The routing hook can
replace the requested model.
Role instructions belong in each task prompt because routing may replace the
requested Claude role or Codex model. Hook approval in the native `/hooks` UI
is still required where the harness prompts for it.

## Task ownership

Both workflows keep easy work in the root and delegate only when a child can own
a useful assignment. Split work only when the deliverables are independent or a
concrete risk needs independent verification. The agent-specific instructions
differ in how they divide discovery and the final answer:

- **Claude:** choose ownership before discovery. A connected investigation can be
  one complete assignment; keep it in the root when retained evidence and a bounded
  check suffice or briefing and acceptance would repeat the work. Delegated owners
  return an answer covering every assigned requirement with cited evidence. Missing
  evidence stays with the same owner for a focused amendment. Each child brief
  repeats the no-recursive-teams and return-to-owner boundaries.
- **Codex:** choose who will synthesize the final answer before delegating. When
  the root must deliver a delegated read-only investigation's answer, one owner
  acquires the complete evidence and the root synthesizes it. Implementation
  owners handle the complete edit, test, and fix loop.

Children return decisive evidence with compact citations and explicit unresolved
items. Keep ownership stable, batch substantive follow-ups, and use native completion
notifications or waits instead of status polling. Amendments update the retained
findings without repeating the full report or restarting the same investigation.

## Model selection

Separate role-model preferences are not used by the UG workflow. Existing
`.model-orchestrator.json` project preferences,
`$XDG_CONFIG_HOME/model-orchestrator/config.json` user preferences, and generated
custom Claude agents are left untouched. The bundled workflow uses the router's
model selection and requires no preference setup or locking.

## Attribution

Migrated from the Databricks `model-orchestrator` plugin by Arnav Singhvi. The base
workflow came from version 0.4.21 (source commit
`474b08e5809205d0aa9abbd059b82d5ab541ca92`); the Claude and Codex workflows now carry
separate refinements. Claude uses the C12 workflow at version 1.3.0; Codex uses
version 1.3.0.
Originally adapted from
[donvito/codex-astra-luna-orchestrator](https://github.com/donvito/codex-astra-luna-orchestrator/tree/21710352ec201f8634874d8298e0eca694e298a8)
under Apache-2.0; see [LICENSE.upstream](LICENSE.upstream). UG changes add shared
routing-state checks and launch-scoped activation and Claude roles, and delegate
model selection to smart routing.
