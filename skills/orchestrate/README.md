# UG model orchestrator

UG bundles the `orchestrate` workflow, five Claude role definitions, and the
existing role-preference helper from `model-orchestrator` 0.4.10 for use in
smart-routed Claude and Codex sessions.

The bundle does not install itself into agent configuration or register launch
hooks. A launcher must install the skill and load its Claude role definitions
before activating the workflow. User instructions take precedence, and easy
tasks remain in the root.

The model-resolution helper requires a UG smart-routing session and reads the
same session controls as the routing hooks. Installed skill files and saved
model preferences cannot enable routing or authorize delegation while it is off.

The bundled Claude roles use the `ug-smart-router:<role>` namespace. Codex uses
native spawning with per-call model preferences.
Role instructions belong in each task prompt because routing may replace the
requested Claude role or Codex model.

## Existing installations and preferences

Existing `.model-orchestrator.json` project preferences and
`$XDG_CONFIG_HOME/model-orchestrator/config.json` user preferences keep their
format and precedence. Claude custom agent names and ownership hashes are
unchanged. Bundled defaults remain Sonnet for Claude and `gpt-5.6-luna` at `max`
effort for Codex; routing determines the final model. The helper reads Codex's
catalog using UG's managed, profile, then user config precedence, including
`CODEX_HOME`, rather than Isaac's catalog environment variable.

The [skill](SKILL.md) documents `show`, `set`, and `unconfigure`. Run its helper
with the launching `UCODE_SMART_ROUTER_PYTHON`, not an arbitrary Python on PATH.
Only `show` requires enabled routing; changing or removing preferences does not
activate orchestration. Configuration retains the original ownership checks,
nonblocking writer lock, and interrupted-write recovery. User-edited agents are
preserved and reported for reconciliation.

## Attribution

Migrated from the Databricks `model-orchestrator` plugin 0.4.10 by Arnav Singhvi.
Originally adapted from
[donvito/codex-astra-luna-orchestrator](https://github.com/donvito/codex-astra-luna-orchestrator/tree/21710352ec201f8634874d8298e0eca694e298a8)
under Apache-2.0; see [LICENSE.upstream](LICENSE.upstream). UG changes add shared
routing-state checks, the Claude role namespace, UG catalog discovery, and
cross-platform locking.
