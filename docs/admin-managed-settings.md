# Manage Claude Code and Codex settings with an MDM

This guide is for workspace admins and IT teams. It explains how to set Claude Code and Codex
settings for your organization in the Unity Gateway agent configuration, and how to deliver them
to every managed Mac or Linux machine with an MDM such as Jamf.

When you finish, developers run `ug claude` and `ug codex` as usual. They get the AI Gateway,
your policy, and no password prompts.

## How it works

1. An admin edits the workspace's agent configuration on the AI Gateway **Coding agents** page.
2. Your MDM policy runs one command as root on each machine:

   ```bash
   ug configure --global --workspace https://<workspace> --agents claude,codex
   ```

3. When the workspace publishes an agent configuration, the command reads it and rebuilds each
   agent's machine-wide files from scratch. On macOS it also provisions Claude Desktop. A
   workspace that publishes no agent configuration gets `--global`'s earlier behavior instead,
   which this guide doesn't cover.

   | Agent | Files | Enforced |
   | --- | --- | --- |
   | Claude Code | `/Library/Application Support/ClaudeCode/managed-settings.json` (macOS), `/etc/claude-code/managed-settings.json` (Linux) | Everything in the file |
   | Codex | `/etc/codex/requirements.toml` | Yes |
   | Codex | `/etc/codex/config.toml` | No: developers can override these defaults |

4. Developers run `ug claude` or `ug codex`. Their first launch signs them in to Databricks with
   their own account. No later run on the machine writes the machine-wide files or asks for
   `sudo`.

Each file is built in layers. A higher layer wins:

```text
Claude Code managed-settings.json = Unity Gateway defaults < agent_native_settings
                                    < feature settings < gateway settings
Codex requirements.toml           = agent_native_requirements < gateway provider lock
Codex config.toml                 = agent_native_settings < default model
```

Unity Gateway reserves only the keys it needs for the gateway and its features. Every other key
passes through as you wrote it, so you can use a new Claude Code or Codex setting the day it
ships. The [reference tables](#reference-keys-unity-gateway-owns) list the reserved keys.

The files hold no per-user values. Each developer's sign-in, discovered models, and Databricks MCP
registrations come from their own `ug` run.

## Before you start

- **`ug` in a shared location.** The machine-wide files run `ug` by its full path for every user,
  so install it where every user can run it, such as `/usr/local/bin`. Pin the version. See
  [scripts/mdm/macos-setup.md](../scripts/mdm/macos-setup.md) for an install script.
- **Agent versions.** Claude Code 2.1.290 or later. Codex 0.156.0 or later: older Codex ignores
  the provider lock in `requirements.toml`.
- **A machine credential.** The MDM run reads the agent configuration and resolves its MCP servers
  with a service principal's token in `DATABRICKS_BEARER`. It never decides which models a
  developer can use: each request carries the developer's own token.
- **No other tool writes these files.** The MDM run owns them and rebuilds them on every run.
  Don't deliver `managed-settings.json` or `requirements.toml` through another tool or a
  configuration profile.

## Write the agent configuration

Each enabled agent has Unity Gateway fields and native settings.

### Unity Gateway fields

| Field | Effect on the machine |
| --- | --- |
| `default_models` | Claude Code: each family pin becomes `env.ANTHROPIC_DEFAULT_<FAMILY>_MODEL`. Codex: `default_model` becomes `model` in `/etc/codex/config.toml`. |
| `models.model_services` | Claude Code: the list becomes `availableModels`, `enforceAvailableModels`, and the `/model` picker. A family you don't pin gets the newest model of that family in the list. |
| `models.unity_catalog_location`, `models.model_provider_service` | Models are resolved for each developer when they run `ug claude` or `ug codex`, not in the machine-wide files. |
| `http_headers` | Added to every gateway request. |
| `tracing` | Claude Code: seven OTEL `env` keys and `otelHeadersHelper`. Codex: `ug codex` points its trace exporter at a local proxy. |
| `smart_routing` | `ug claude` and `ug codex` choose the model per launch. |
| `mcp_servers` (workspace level) | Claude Code: `managedMcpServers` in the managed file. Codex: registered in each developer's config. |

### Native settings

- **Claude Code `agent_native_settings`:** any `managed-settings.json` key. Everything you set is
  enforced; developers can't override it.
- **Codex `agent_native_settings`:** any `config.toml` key. These are defaults written to
  `/etc/codex/config.toml`; developers can override them in their own config.
- **Codex `agent_native_requirements`:** any `requirements.toml` key, such as
  `allowed_sandbox_modes`. These are enforced.

Codex fields are JSON objects with the same structure as the TOML. Codex has two fields because the
two files use some of the same keys with different meanings: `mcp_servers` defines servers in
`config.toml` and is an allowlist in `requirements.toml`.

Values pass through as written, including `null`. If the agent rejects a value, fix it in the agent
configuration. TOML has no `null`, so Codex drops `null` values and the run report lists them.

### Example

```json
{
  "enabled_agents": [
    {
      "agent": "CODING_AGENT_CLAUDE_CODE",
      "config": {
        "default_models": {
          "default_model": "system.ai.claude-opus-5-5",
          "default_opus_model": "system.ai.claude-opus-5-5",
          "default_sonnet_model": "system.ai.claude-sonnet-5-5"
        },
        "agent_native_settings": {
          "companyAnnouncements": ["Use ug claude for every session."],
          "permissions": { "deny": ["Bash(curl *)"] },
          "env": { "ENABLE_PROMPT_CACHING_1H": "0" }
        }
      }
    },
    {
      "agent": "CODING_AGENT_CODEX",
      "config": {
        "default_models": { "default_model": "system.ai.gpt-6-1-sol" },
        "agent_native_settings": {
          "model_reasoning_effort": "high",
          "history": { "persistence": "none" }
        },
        "agent_native_requirements": {
          "allowed_sandbox_modes": ["read-only", "workspace-write"]
        }
      }
    }
  ]
}
```

In this example:

- Claude Code gets your announcement and your deny rule. Unity Gateway adds `WebSearch` to the
  same deny list.
- `ENABLE_PROMPT_CACHING_1H` overrides a Unity Gateway default, and the run report says so.
- Codex developers start on `system.ai.gpt-6-1-sol` with high reasoning effort, and can't choose
  the `danger-full-access` sandbox.

## Preview a draft

Preview the files a draft produces before you publish it. Use the same `ug` version your fleet
runs:

```bash
ug configure --global --dry-run --workspace https://<workspace> \
  --agents claude,codex --from-file draft.json
```

`draft.json` is an agent configuration in the format `ug export` writes. The preview prints each
file and the report, and writes nothing. It needs no root and no service principal.

## Run it from your MDM

1. Install `ug`, the Databricks CLI, Claude Code, and Codex at a shared path. On macOS,
   [scripts/mdm/macos-provision.sh](../scripts/mdm/macos-provision.sh) does steps 1 to 3; see
   [scripts/mdm/macos-setup.md](../scripts/mdm/macos-setup.md).
2. Exchange the service principal's client ID and secret for a short-lived token, and export it as
   `DATABRICKS_BEARER`.
3. Run as root:

   ```bash
   ug configure --global --workspace https://<workspace> --agents claude,codex
   ```

   `--agents` lists the agents this run handles; without it, the run handles both. Agents you
   don't list are left as they are. Other agents in the list, such as `gemini`, are skipped:
   they have no machine-wide settings.
4. Schedule the policy, for example once a day. A change to the agent configuration reaches a
   machine on its next run.

Run unattended, the command never prompts: it needs root, or `sudo -n` that needs no password.
From a terminal, an admin can approve the sudo prompt instead. Once it has run, the machine is
managed from the agent configuration: if the workspace later stops publishing one, the run exits
5 and leaves the files in place rather than switching back.

### Exit codes

| Exit | Meaning | Files |
| --- | --- | --- |
| 0 | Success: written, unchanged, or dropped | Updated |
| 2 | Usage or setup error, such as `ug` installed where other users can't run it | Untouched |
| 3 | Not root, `sudo -n` isn't available, and there's no terminal to approve a sudo prompt | Untouched |
| 4 | Couldn't read the agent configuration or its MCP servers (network, auth) | Untouched |
| 5 | The workspace publishes no agent configuration, or coding agent configs are off | Untouched |
| 6 | A write failed, or a file changed during the write | Previous file kept |

### The run report

Every run prints, for each agent, the files it wrote and what happened to each native setting:

| Outcome | Meaning |
| --- | --- |
| `applied` | Written as you set it |
| `skipped` | A key Unity Gateway owns. Your value was dropped; the note says why and what to use instead |
| `overrode Unity Gateway default` | Your value replaced a Unity Gateway default |
| `dropped` | The agent isn't enabled in the agent configuration any more, so its files were removed |

Publishing an agent configuration doesn't block reserved keys yet. Read the report in your MDM's
policy logs.

## What developers see

| Developer runs | Result |
| --- | --- |
| `ug claude` or `ug codex`, first time | Signs in to Databricks and writes their own settings, with no `sudo`. Prints "Machine-wide settings are managed by your IT team." |
| `ug claude` or `ug codex`, later | Launches. No check of the machine-wide files |
| `ug configure` | Updates their own settings only |
| `ug status` | Shows `Machine-wide settings: Managed by IT` |
| `ug revert` | Reverts their own settings; leaves the machine-wide files |
| Bare `claude` or `codex` | Gets the gateway, sign-in, and your native settings once the developer has run `ug claude` or `ug codex` once. It doesn't get discovered models, tracing, or smart routing |

Developers can't route around the gateway: Claude Code's Bedrock and Vertex switches are written
blank, and Codex's provider is locked in `requirements.toml`.

Codex reads its files when it starts, so a change applies to the next Codex session. Claude Code
reloads its managed file during a session, except a few keys such as `model`.

## Common tasks

### Turn off an agent

Remove the agent from `enabled_agents`. Its next MDM run deletes that agent's machine-wide files
and reports `dropped`.

### Remove Unity Gateway from a machine

Run `ug revert` as root through your MDM policy. It deletes the machine-wide files, the saved agent
configuration, and the MDM marker. Unpublishing the agent configuration alone leaves the files in
place, and every run exits 5.

### Use your own OTEL export

Unity Gateway tracing and your own OTEL export can't run together. While `tracing` is on, Unity
Gateway's trace keys and `otelHeadersHelper` replace yours, and Claude Code applies that helper to
every OTEL signal. Turn off `tracing` to keep your own export.

### Choose models

- **Explicit list:** set `models.model_services`. Claude Code enforces it with `availableModels`
  for every launch, including bare `claude`.
- **Unity Catalog location or model provider service:** each developer's `ug` resolves the models.
  Run Claude Code and Codex through `ug claude` and `ug codex`.
- Pin every Claude family you use in `default_models`, and set `default_models.default_model` for
  Codex, so bare `claude` and `codex` start on a model the gateway serves.

Codex has no model allowlist. The gateway enforces which models each developer can use.

### Limit Codex MCP servers

Only an allowlist in `agent_native_requirements.mcp_servers` limits which servers Codex starts.
When you set one, Unity Gateway adds its own servers to it. Each entry matches a server by name
and identity:

```json
"agent_native_requirements": {
  "mcp_servers": { "jira": { "identity": { "url": "https://mcp.example.com/jira" } } }
}
```

For servers started by a generic launcher such as `npx`, match the arguments too. Turn off plugin
servers with `features.plugins = false` in `agent_native_settings`.

### Block Claude Code MCP servers

Use `deniedMcpServers`, which passes through. `managedMcpServers` and `allowedMcpServers` are
reserved: Databricks MCP servers come from the agent configuration's `mcp_servers`.

### Don't combine other Codex policy sources

Codex ranks other policy sources above `/etc/codex/requirements.toml`: cloud requirements
(including a self-hosted `config_server`), legacy `managed_config.toml` fields, and a macOS
`com.openai.codex` configuration profile. Don't use them alongside Unity Gateway. The MDM run
removes a `managed_config.toml` that an earlier `ug configure` wrote.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| A setting has no effect | The run report: `skipped` means Unity Gateway owns the key |
| Exit 2 about a home folder | Reinstall `ug` at a shared path and run that installation |
| Exit 4 | The service principal's token, and network access to the workspace |
| Exit 5 | The workspace publishes an agent configuration, and coding agent configs are on |
| Codex doesn't use the gateway | `codex --version` is 0.156.0 or later |
| Which settings Claude Code applied | `/status` in Claude Code, under **Setting sources**, or `claude doctor` |
| What `ug` sees | `ug status` |

## Reference: keys Unity Gateway owns

| Class | Your value | Unity Gateway's value |
| --- | --- | --- |
| Reserved | Dropped, and listed in the report | Written, unless **Written by Unity Gateway** is No |
| Reserved while a feature is on | Dropped while the feature is on; passed through while it's off | Written while the feature is on |
| Unity Gateway default | Wins, and the report says "overrode Unity Gateway default" | Written only when you don't set the key |
| Shared list | Your entries are kept | Unity Gateway's entries are added |
| Shared table | Your entries are kept, except ones that use a name Unity Gateway registers | Unity Gateway's entries are added |

**Written by Unity Gateway: No** marks keys Unity Gateway never writes into the machine-wide file,
but still drops from your settings, so a per-launch or per-user value can take effect.

Setting a whole object counts as setting every key in it that the tables list. For example,
setting `env` to `null` is dropped, because it would remove the gateway keys.

Unity Gateway may reserve more keys in later releases. Each release's notes list new reservations.

<!-- BEGIN GENERATED OWNERSHIP TABLES: run `uv run python -m ucode.ownership --write docs/admin-managed-settings.md` -->

### Claude Code: `agent_native_settings` (managed-settings.json)

| Key | Class | Feature | Written by Unity Gateway | Value | Note |
| --- | --- | --- | --- | --- | --- |
| `apiKeyHelper` | Reserved |  | Yes |  | Runs `ug auth-token`, which signs in as the developer. |
| `env.ANTHROPIC_BASE_URL` | Reserved |  | Yes |  | The AI Gateway endpoint. |
| `env.ANTHROPIC_CUSTOM_HEADERS` | Reserved |  | Yes |  | Use `http_headers` in the agent config. |
| `env.CLAUDE_CODE_USE_GATEWAY` | Reserved |  | Yes |  | Routes Claude Code through the gateway. |
| `env.CLAUDE_CODE_API_KEY_HELPER_TTL_MS` | Reserved |  | Yes |  | Tied to the token lifetime. |
| `env.ANTHROPIC_AUTH_TOKEN` | Reserved |  | Yes |  | Written blank, so a stray shell credential can't replace the gateway sign-in. |
| `env.ANTHROPIC_API_KEY` | Reserved |  | Yes |  | Written blank, so a stray shell credential can't replace the gateway sign-in. |
| `env.CLAUDE_CODE_USE_BEDROCK` | Reserved |  | Yes |  | Written blank, so developers can't bypass the gateway. |
| `env.CLAUDE_CODE_USE_VERTEX` | Reserved |  | Yes |  | Written blank, so developers can't bypass the gateway. |
| `awsAuthRefresh` | Reserved |  | No |  | Has no effect while the Bedrock switch is blank. |
| `awsCredentialExport` | Reserved |  | No |  | Has no effect while the Bedrock switch is blank. |
| `gcpAuthRefresh` | Reserved |  | No |  | Has no effect while the Vertex switch is blank. |
| `forceLoginMethod` | Reserved |  | No |  | The gateway handles sign-in. |
| `env.ANTHROPIC_DEFAULT_FABLE_MODEL` | Reserved |  | Yes |  | Use `default_models`. Written when the agent config supplies one. |
| `env.ANTHROPIC_DEFAULT_OPUS_MODEL` | Reserved |  | Yes |  | Use `default_models`. Written when the agent config supplies one. |
| `env.ANTHROPIC_DEFAULT_SONNET_MODEL` | Reserved |  | Yes |  | Use `default_models`. Written when the agent config supplies one. |
| `env.ANTHROPIC_DEFAULT_HAIKU_MODEL` | Reserved |  | Yes |  | Use `default_models`. Written when the agent config supplies one. |
| `env.ANTHROPIC_DEFAULT_FABLE_MODEL_NAME` | Reserved |  | No |  | The model picker shows the routable model ID. |
| `env.ANTHROPIC_DEFAULT_OPUS_MODEL_NAME` | Reserved |  | No |  | The model picker shows the routable model ID. |
| `env.ANTHROPIC_DEFAULT_SONNET_MODEL_NAME` | Reserved |  | No |  | The model picker shows the routable model ID. |
| `env.ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME` | Reserved |  | No |  | The model picker shows the routable model ID. |
| `env.CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` | Reserved |  | No |  | Would turn off 1-hour prompt caching. |
| `env.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY` | Reserved |  | No |  | Set per launch when the developer opts in. |
| `env.ANTHROPIC_MODEL` | Reserved while a feature is on | `smart_routing` | No |  | Set per launch while smart routing is on. |
| `availableModels` | Reserved while a feature is on | `models` | Yes |  | Use `models`. Written when the agent config lists models explicitly. |
| `enforceAvailableModels` | Reserved while a feature is on | `models` | Yes |  | Use `models`. Written when the agent config lists models explicitly. |
| `modelPicker` | Reserved while a feature is on | `models` | Yes |  | Use `models`. Written when the agent config lists models explicitly. |
| `env.CLAUDE_CODE_ENABLE_TELEMETRY` | Reserved while a feature is on | `tracing` | Yes |  | Unity Gateway tracing can't run alongside your own OTEL export. |
| `env.CLAUDE_CODE_ENHANCED_TELEMETRY_BETA` | Reserved while a feature is on | `tracing` | Yes |  | Unity Gateway tracing can't run alongside your own OTEL export. |
| `env.OTEL_TRACES_EXPORTER` | Reserved while a feature is on | `tracing` | Yes |  | Unity Gateway tracing can't run alongside your own OTEL export. |
| `env.OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | Reserved while a feature is on | `tracing` | Yes |  | Unity Gateway tracing can't run alongside your own OTEL export. |
| `env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Reserved while a feature is on | `tracing` | Yes |  | Unity Gateway tracing can't run alongside your own OTEL export. |
| `env.CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS` | Reserved while a feature is on | `tracing` | Yes |  | Unity Gateway tracing can't run alongside your own OTEL export. |
| `env.CLAUDE_CODE_PROPAGATE_TRACEPARENT` | Reserved while a feature is on | `tracing` | Yes |  | Unity Gateway tracing can't run alongside your own OTEL export. |
| `otelHeadersHelper` | Reserved while a feature is on | `tracing` | Yes |  | Claude Code applies this helper to every OTEL signal, so your exporters would receive Unity Gateway's headers. |
| `managedMcpServers` | Reserved |  | Yes |  | Databricks MCP servers come from `mcp_servers` in the agent config. |
| `allowedMcpServers` | Reserved |  | No |  | Block servers with `deniedMcpServers`, which passes through. |
| `env.ENABLE_PROMPT_CACHING_1H` | Unity Gateway default |  | Yes | `"1"` | 1-hour prompt caching. |
| `env.ENABLE_TOOL_SEARCH` | Unity Gateway default |  | Yes | `"true"` | Loads MCP tools on demand. |
| `permissions.deny` | Shared list |  | Yes |  | Unity Gateway adds `WebSearch`, because the gateway rejects Claude's hosted web search. |

### Codex: `agent_native_requirements` (/etc/codex/requirements.toml)

| Key | Class | Feature | Written by Unity Gateway | Value | Note |
| --- | --- | --- | --- | --- | --- |
| `model_provider` | Reserved |  | Yes |  | Unity Gateway locks the provider to the gateway. |
| `model_providers` | Reserved |  | Yes |  | Unity Gateway defines the gateway provider. |
| `model_catalog_json` | Reserved |  | No |  | `ug codex` builds a model catalog for each developer, and a required one would replace it. |
| `models.new_thread` | Reserved |  | No |  | The default model comes from `default_models.default_model`. |
| `config_server` | Reserved |  | No |  | It would let another server outrank Unity Gateway's requirements. |
| `otel.trace_exporter` | Reserved while a feature is on | `tracing` | No |  | It would replace the exporter `ug codex` points at its tracing proxy. |
| `allow_managed_hooks_only` | Reserved while a feature is on | `smart_routing` | No |  | It turns off smart routing's hooks. |
| `features.hooks` | Reserved while a feature is on | `smart_routing` | No |  | It turns off smart routing's hooks. |
| `mcp_servers` | Shared table |  | Yes |  | When you set an allowlist, Unity Gateway adds its own MCP servers to it. |

### Codex: `agent_native_settings` (/etc/codex/config.toml)

| Key | Class | Feature | Written by Unity Gateway | Value | Note |
| --- | --- | --- | --- | --- | --- |
| `model_provider` | Reserved |  | No |  | Set in requirements, so here it would have no effect. |
| `model_providers.Databricks` | Reserved |  | No |  | Set in requirements, so here it would have no effect. |
| `model` | Reserved |  | Yes |  | The default model comes from `default_models.default_model`. |
| `model_catalog_json` | Reserved |  | No |  | Model catalogs are built for each developer. |
| `mcp_servers` | Shared table |  | Yes |  | Unity Gateway registers its own MCP servers for each developer; your entries with the same names are dropped. |

<!-- END GENERATED OWNERSHIP TABLES -->
