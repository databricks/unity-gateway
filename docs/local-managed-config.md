# Local managed configuration contract

This contract covers file launches introduced by AIGTWY-4864 through AIGTWY-4867.
It is a local extension of the published `CodingAgentConfig`, not a new API,
proto, EStore schema, or publishable workspace configuration. The `enabled_agents`
field remains an array of `{agent, config}` objects. The only new top-level field
is `handoff`; `custom_env`, `native_settings`, and `native_requirements` belong
inside an agent's `config`.

## Launch and source selection

```sh
ucode claude --workspace https://workspace.example.com -f /protected/config.json -- --settings ./user.json
ucode codex --workspace https://workspace.example.com -f /protected/config.json app-server --listen stdio://
```

No separate `ug configure` invocation is necessary, including first use. UG reads
the explicit file once, validates it before bootstrap/settings writes, and uses
that selection throughout the invocation. Each later invocation rereads it.
Missing or invalid input is fatal, with no API or stale-file fallback. File
launches do not update `~/.ucode/managed-config.json`; API launches retain the
five-minute cache. Omit `-f` to resume workspace API selection.

Place UG options before `--`. Claude's existing separator behavior is preserved;
Codex utility commands do not require an inserted separator. Codex app-server
stdout remains the agent's protocol stream. Existing authentication and forwarded
arguments remain in effect.

Nonempty UC MCP/skill selectors, budget recommendation inputs (`smart_defaults`
and legacy `spend_tiers`), and native MCP/skill bypasses remain unsupported. A
transition needing cleanup of existing UC-managed resources fails with guidance.
File input does not run installers or take over helper/plugin assets.

## Stable ownership and migration

Ordinary files use owner `local-file`, independent of the temporary filename,
workspace, and file bytes. An integration can supply:

```json
{
  "handoff": {
    "schema_version": 1,
    "owner": "isaac",
    "migration_version": 1,
    "agents": {
      "claude": {
        "adopt": [
          {"target": "managed_settings", "path": ["env", "TEAM_CONTEXT"]}
        ],
        "retire": [
          {"target": "process_env", "path": ["RETIRED_CONTEXT"]}
        ]
      },
      "codex": {
        "adopt": [
          {"target": "requirements", "path": ["features", "fast_mode"]}
        ]
      }
    }
  }
}
```

This fragment must be combined with enabled agent configs declaring the adopted
fields. Targets are `user_settings`, `private_settings`, `managed_settings`,
`process_env`, and Codex-only `requirements`. Paths are nonempty arrays of native
key names, never filesystem paths or numeric indexes. `process_env` accepts one
environment name and no `elements`. Array adoption/retirement uses explicit
`elements` contributions. An adoption must match a currently declared effect.

Completed destination migrations retain receipts for safe retries; overall source
success is recorded only after all required destinations succeed. Change
`migration_version` when changing migration declarations. Completed
retirements do not erase later user additions on each launch. Current source
declarations are still reconciled on every invocation: equal values are adopted,
changes replace owned values, and omissions remove owned values even after drift.
Workspace A to B to A and file/API transitions use actual destination ownership,
not content-hash or cache identity. Claude and Codex state are separate.

Permissions and hook arrays are contribution-owned. Unrelated handlers in the
same hook matcher survive cleanup. Closed arrays such as allowed channel plugins
and status-line selections preserve authored order, including explicit `[]`.
Existing unknown entries require an explicit adopt/retire migration instead of
silent whole-array replacement. Unowned incompatible ancestor shapes also fail
before writes. No partial overlay claims an entire native subtree.

Backups, ownership, successful snapshots, and pending recovery are separate
records in the existing managed-backups store. Destinations are locked and
preflighted as one application. A partial write failure prevents launch and keeps
recovery data for retry. Semantically unchanged privileged output needs no write.

```sh
ug managed-config release --owner isaac --agent claude
ug managed-config release --owner isaac --agent codex
```

Release is idempotent and requires no config file. It removes that owner's
remaining explicit and generated routing/provider/auth/catalog effects, including
requirements and process-env ownership, but respects transfer to another owner.
It does not start an agent, rediscover models, or restore old baselines. Use the
separate `ug revert` command to restore original backups. Release may need an
interactive terminal for privileged destinations. Release outstanding effects
before removing an agent from the enabled set.

## Environment

`custom_env` is a per-agent string map. Empty strings and whitespace are preserved.
Invalid names, NULs, invalid Unicode, generated authentication/routing/config-home
controls, and conflicts with explicitly enabled UG tracing fail before writes.
OTEL names follow these same rules. Claude receives supported settings plus the
runtime overlay. Codex receives a real child-process environment on every UG
launch path, not just `shell_environment_policy.set`.

Removed owned keys suppress stale parent values across later launches. Unrelated
inheritance survives. The overlay does not change UG's own authentication or
initial agent executable lookup. A custom `PATH` may still intentionally hide
dependencies needed by the agent itself. Independently opened desktop apps and
bare agent processes do not receive UG's runtime overlay.

## Native settings

The supported subset is validated against Claude Code 2.1.268 and Codex 0.154.0.
Unknown keys and unsupported native types fail. Strings are not expanded by UG;
helper commands remain data for the agent to interpret. Absolute helper/asset
paths may be values, but cannot redirect a destination.

Claude's supported keys:

- `permissions`: `allow`, `ask`, `deny`, and `additionalDirectories` string arrays;
  `disableBypassPermissionsMode` equal to `disable`.
- `hooks`: `WorktreeCreate`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
  `SessionStart`, `SubagentStart`, and `StopFailure`. Groups have optional string
  `matcher` and a `hooks` array. Command handlers require `type: command` and
  `command`; optional fields are `args`, positive finite `timeout`, `statusMessage`,
  `once`, `async`, and `asyncRewake`.
- Top-level `sandbox`: boolean `enabled`, `failIfUnavailable`,
  `autoAllowBashIfSandboxed`, and `allowUnsandboxedCommands`.
- Boolean `allowManagedPermissionRulesOnly`, `disableWorkflows`,
  `workflowKeywordTriggerEnabled`, `autoCompactEnabled`, and `channelsEnabled`.
- `allowedChannelPlugins`: objects containing string `marketplace` and `plugin`.
- `spinnerVerbs`: `mode` equal to `append` or `replace`, and string-array `verbs`.
- `companyAnnouncements`: string array.
- `attribution`: string `commit` and `pr`, boolean `sessionUrl`.
- String `otelHeadersHelper`, separate from the reserved inference `apiKeyHelper`.
- `statusLine`: `type: command`, string `command`, optional finite numeric
  `padding`, numeric `refreshInterval` at least 1, boolean `hideVimModeIndicator`.

`forceLoginOrgUUID` has native string or nonempty string-array syntax, but UG
rejects it at launch until a compatible first-party OAuth enforcement path exists.
`prStatusFooterEnabled` is not established as native managed policy in the pinned
version and is unsupported. `sandbox` and `allowManagedPermissionRulesOnly` are
top-level, not nested under `permissions`. Claude environment declarations must
use `custom_env`; `native_settings.env` is always rejected.

Codex's supported keys:

- `otel.environment` string and `otel.log_user_prompt` boolean.
- Separate `otel.exporter`, `otel.metrics_exporter`, and `otel.trace_exporter`:
  `none`, `statsig`, or exactly one `otlp-http` or `otlp-grpc` object. Both objects
  require string `endpoint` and permit a string-valued `headers` map and a `tls` object.
  HTTP additionally requires `protocol: binary` or `protocol: json`.
  TLS keys are `ca-certificate`, `client-certificate`, and `client-private-key`,
  each an absolute path. Exporter variants cannot be combined by a partial overlay.
- Boolean `features.hooks` and `features.fast_mode`.
- `tui.status_line`: string array.
- `model_auto_compact_token_limit`: signed 64-bit integer, not boolean.
- `model_auto_compact_token_limit_scope`: `total` or `body_after_prefix`.

Codex `native_requirements` initially accepts only
`{"features":{"fast_mode":false}}`. A conflicting native `features.fast_mode=true`
is rejected. Codex itself pins the effective feature to false even if a CLI override
requests true. Unrelated admin requirements remain intact. `features.hooks=false`
also conflicts with a selected UG smart-routing launch, which needs those hooks;
ordinary launches and commands that bypass routing may still use it.

UG reserves generated routing/provider/model selection, auth/helper, config-home,
and managed-header fields. Use the existing model/header inputs and `custom_env`
instead. Native MCP/skill declarations are rejected. Explicit UG tracing cannot
compete with Claude `otelHeadersHelper` or Codex `otel.trace_exporter`; disjoint
native log/metrics exporters remain supported. This is checked against saved
effective tracing too, since `tracing.enabled=false` is not a saved-preference reset.

## Destinations and enforcement

- Claude private settings: `~/.claude/ucode-settings.json`.
- Claude Linux managed policy: `/etc/claude-code/managed-settings.json`.
- Claude macOS managed policy:
  `/Library/Application Support/ClaudeCode/managed-settings.json`.
- Modern Codex private settings: `~/.codex/ucode.config.toml`.
- Codex Linux/macOS managed settings: `/etc/codex/managed_config.toml`.
- Codex Linux/macOS requirements: `/etc/codex/requirements.toml`.

Claude permission, hook, sandbox, channel, and workflow policies require managed
scope. Codex feature declarations require managed settings, and native requirements
require the separate requirements destination. UI/attribution/compaction/telemetry
fields can be private-consumable, but conflicting higher-precedence settings cannot
silently override the declared output. Required write failures prevent launch;
a private fallback does not count as enforcement. Unrelated native siblings survive.

Composition checks UG's actual destinations, not every native user/project/profile
layer. Codex can still reject incompatible exporter variants combined from those
external layers when it loads configuration. Explicitly retire an old declaration
the integration owns, or reconcile the external layer; UG does not silently delete
unowned settings to make the native loader accept them.

Claude relay can deliver required native policy through a native-only OS plan,
without persisting relay routing/auth or custom environment into that plan.
Surviving unowned routing/auth or custom-environment conflicts fail. Required
policy is rejected on platforms without a supported managed destination.
Known legacy Codex layouts reject nonempty native extensions; ordinary legacy
launch behavior is unchanged. Linux/macOS path and simulated Windows tests are
not proof of native execution on every platform.

## Representative combined file

This example uses no helper paths or native exporter endpoints and performs no
installer actions. Model names must exist in the selected workspace.

```json
{
  "spec_version": 1,
  "enabled_agents": [
    {
      "agent": "CODING_AGENT_CLAUDE_CODE",
      "config": {
        "default_models": {"default_model": "system.ai.claude-haiku-4-5"},
        "custom_env": {"TEAM_CONTEXT": "engineering"},
        "native_settings": {
          "permissions": {"deny": ["Read(./private/**)"]},
          "disableWorkflows": true,
          "attribution": {"commit": "", "pr": ""}
        }
      }
    },
    {
      "agent": "CODING_AGENT_CODEX",
      "config": {
        "default_models": {"default_model": "system.ai.gpt-5-6-sol"},
        "custom_env": {"TEAM_CONTEXT": "engineering"},
        "native_settings": {
          "otel": {"environment": "engineering", "metrics_exporter": "none"},
          "features": {"hooks": false}
        },
        "native_requirements": {"features": {"fast_mode": false}}
      }
    }
  ],
  "handoff": {
    "schema_version": 1,
    "owner": "isaac",
    "migration_version": 1,
    "agents": {"claude": {}, "codex": {}}
  }
}
```

Isaac retains helper/plugin assets and explicitly disjoint conditional preferences.
This generic contract does not embed an Isaac-specific ownership inventory. The
producer must supply its exact migration entries and parity fixtures. Passing UG
component tests or installed validation checks alone does not establish end-to-end
Isaac parity.
