# UG model orchestrator

UG bundles the `orchestrate` workflow and five Claude role definitions from
`model-orchestrator` 0.4.10. Smart-routed Claude and Codex launches install and
activate this skill alongside `smart-router` only with `ENABLE_ORCHESTRATION=1`.
The feature is off by default; routing alone installs only `smart-router`.

The workflow is injected before root prompts and after compaction. Its activation
and pre-delegation checks require the feature flag and a UG smart-routing session,
and read the same session controls as the routing hooks. Turning Smart Router off
stops new automatic delegation and supersedes the previous workflow. Turning it on restores
orchestration only in opted-in sessions. An installed skill or saved model
preference cannot enable it.
Explicit user requests for subagents still use native harness behavior while routing
is off, without the orchestrator's workflow or routing check.
User instructions take precedence, and easy tasks remain in the root.

Claude loads the bundled roles as `ug-smart-router:<role>` in its temporary
routing plugin. Both agents delegate without model or reasoning-effort overrides;
the routing hook selects the model.
Role instructions belong in each task prompt because routing may replace the
requested Claude role or Codex model. Hook approval in the native `/hooks` UI
is still required where the harness prompts for it.

## Routing check

Separate role-model preferences are not used by the UG workflow. Existing
`.model-orchestrator.json` project preferences,
`$XDG_CONFIG_HOME/model-orchestrator/config.json` user preferences, and generated
custom Claude agents are left untouched. The bundled workflow uses the router's
model selection and requires no preference setup, locking, or recovery.

The [skill](SKILL.md) checks eligibility with
`"$UCODE_SMART_ROUTER_PYTHON" -m ucode.smart_routing.orchestrator --check` before
delegating. This command reads session controls without reading or writing
preference files, succeeds silently when both orchestration and routing are enabled,
and exits nonzero otherwise. Retained skill files stay inactive when the feature
flag is unset or `0`.

## Attribution

Migrated from the Databricks `model-orchestrator` plugin 0.4.10 by Arnav Singhvi.
Originally adapted from
[donvito/codex-astra-luna-orchestrator](https://github.com/donvito/codex-astra-luna-orchestrator/tree/21710352ec201f8634874d8298e0eca694e298a8)
under Apache-2.0; see [LICENSE.upstream](LICENSE.upstream). UG changes add shared
routing-state checks and launch-scoped activation and Claude roles, and delegate
model selection to smart routing.
