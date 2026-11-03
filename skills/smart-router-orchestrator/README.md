# Smart Router Orchestrator

UG bundles the `smart-router-orchestrator` workflow and five Claude role definitions.
Smart-routed Claude and Codex launches install and activate this skill alongside
`smart-router` with `SMART_ROUTER_CONFIG_VERSION=subagent_orch_v0` or `subagent_orch_v1`,
and with `--enable-smart-routing` or workspace smart routing when no other version is set
(both select `subagent_orch_v0`).
UG expands the selected version into the session's legacy feature flags. The `_v1` revision
enables both V2 and subagent-only routing flags alongside orchestration; subagent-only still
takes precedence, so the first prompt is not routed.
The existing `ENABLE_SMART_ROUTER_ORCHESTRATOR=1` opt-in remains supported when
`SMART_ROUTER_CONFIG_VERSION` is unset; `subagent_only_v0` explicitly leaves orchestration off.
Otherwise the feature is off; routing alone installs only `smart-router`.

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

## Claude answer delivery

Orchestrated Claude print-mode launches (`ug claude -- -p`) let the parent deliver an
accepted child answer verbatim. The parent ends with a selection or a composition:

```json
{"use_agent_answer":"<agentId>"}
{"answer_parts":["Introduction\n\n",{"use_agent_answer":"<agentId>"},"\n\nAdditional findings"]}
```

Parts are joined unchanged; the parent remains responsible for acceptance. Each child
must belong directly to this session and have completed its latest revision successfully.
A `SendMessage` resume invalidates its previous answer. Background answers must match
their completion notification. Invalid selections fail with a nonzero exit status.

The launcher buffers native stream-json output until exit, then renders the requested
text, JSON or stream-json format. It rewrites the final root text and result, retaining
usage, costs, stderr and exit status. Partial events stay raw. The result's
`ug_answer_delivery` field and session-local `claude-answer-delivery-receipts.json`
record selection receipts with hashes, without answer bodies.

`--input-format stream-json` bypasses delivery and first-prompt routing so native control
responses and multi-turn input remain live. Interactive Claude and Codex also retain
normal answers. Hooks advertise selection only while the buffering launch's capability
marker exists next to the session-control file.

## Task ownership

Both workflows keep easy work in the root and delegate only when a child can own
a useful assignment. Split work only when the deliverables are independent or a
concrete risk needs independent verification. The agent-specific instructions
differ in how they divide discovery and the final answer:

- **Claude:** choose ownership before gathering evidence. Work beyond retained
  context and a bounded lookup goes to one native owner for the connected outcome;
  the root does not investigate in order to decide whether to delegate. The owner
  acquires the evidence and produces the answer or change with its checks, while
  the root owns acceptance and cross-owner decisions. Gaps return to the same owner
  for a focused amendment, and no reviewer stage is added by default. Each child
  brief repeats the no-recursive-teams and return-to-owner boundaries.
- **Codex:** choose who will synthesize the final answer before delegating. When
  the root must deliver a delegated read-only investigation's answer, one owner
  acquires the complete evidence and the root synthesizes it. Implementation
  owners handle the complete edit, test, and fix loop.

Children return decisive evidence with compact citations and explicit unresolved
items. Keep ownership stable, batch substantive follow-ups, and use native completion
notifications or waits instead of status polling. Claude requests a complete corrected
answer from the same owner when the answer will be delivered whole; amendments remain
suitable for fragments that the parent integrates. See
[Claude answer delivery](#claude-answer-delivery) for selection and composition.

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
separate refinements. Claude uses version 1.5.0; Codex uses version 1.2.0.
Originally adapted from
[donvito/codex-astra-luna-orchestrator](https://github.com/donvito/codex-astra-luna-orchestrator/tree/21710352ec201f8634874d8298e0eca694e298a8)
under Apache-2.0; see [LICENSE.upstream](LICENSE.upstream). UG changes add shared
routing-state checks and launch-scoped activation and Claude roles, and delegate
model selection to smart routing.
