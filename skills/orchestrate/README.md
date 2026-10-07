# UG model orchestrator

UG bundles the `orchestrate` workflow, five Claude role definitions, and the
existing role-preference helper from `model-orchestrator` 0.4.10. Smart-routed
Claude and Codex launches install this skill alongside `smart-router`.

The workflow is injected before root prompts and after compaction. Its activation
and model-resolution checks require a UG smart-routing session and read the same
session controls as the routing hooks. Turning Smart Router off stops new
automatic delegation and supersedes the previous workflow. Turning it on restores
both features. An installed skill or saved model preference cannot enable them.
Explicit user requests for subagents still use native harness behavior while routing
is off, without the orchestrator's model-resolution helper or role models.
User instructions take precedence, and easy tasks remain in the root.

Claude loads the bundled roles as `ug-smart-router:<role>` in its temporary
routing plugin. Codex uses native spawning with per-call model preferences.
Role instructions belong in each task prompt because routing may replace the
requested Claude role or Codex model. Hook approval in the native `/hooks` UI
is still required where the harness prompts for it.

## Existing installations and preferences

UG suppresses installed `model-orchestrator` marketplace plugins for every Claude
and Codex launch, including Isaac-synced Codex registrations and launches with
smart routing off. The old activation hook does not check routing state, so its
plugin is disabled through native per-launch settings. Saved registrations,
unrelated plugins and hooks, and launches outside UG are unaffected.

This covers marketplace installations; manually copied activation hooks or
development copies passed through `--plugin-dir` need to be removed separately.

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
activate orchestration. Configuration retains the original ownership checks
and interrupted-write recovery. Preference operations use UG's existing file
lock and wait for an operation in the same scope to finish. User-edited agents
are preserved and reported for reconciliation.

## Attribution

Migrated from the Databricks `model-orchestrator` plugin 0.4.10 by Arnav Singhvi.
Originally adapted from
[donvito/codex-astra-luna-orchestrator](https://github.com/donvito/codex-astra-luna-orchestrator/tree/21710352ec201f8634874d8298e0eca694e298a8)
under Apache-2.0; see [LICENSE.upstream](LICENSE.upstream). UG changes add shared
routing-state checks, launch-scoped activation and Claude roles, UG catalog
discovery, and cross-platform locking.
