# Admin-managed agent settings reference

A managed run is `ug configure --global` on a workspace that publishes an agent configuration. It
rebuilds each supported agent's machine-wide files from that configuration. It runs as root, often
from an MDM policy, a provisioning script, or a fleet tool. This page lists the files, fields, exit codes, report outcomes,
and keys that Unity Gateway owns. For why it works this way, see
[About admin-managed agent settings](admin-managed-settings-explained.md).

The supported agents are Claude Code and Codex.

## Files

| Agent | File | Developers can override |
| --- | --- | --- |
| Claude Code | `/Library/Application Support/ClaudeCode/managed-settings.json` on macOS, `/etc/claude-code/managed-settings.json` on Linux | No |
| Codex | `/etc/codex/requirements.toml` | No |
| Codex | `/etc/codex/config.toml` | Yes |
| Unity Gateway | `/etc/unitygateway/managed-settings.json`: the saved agent configuration | — |
| Unity Gateway | `/etc/unitygateway/agents-state.json`: the marker file | — |

The run builds each agent file from these layers. A later layer replaces an earlier one:

| File | Layers, lowest first |
| --- | --- |
| `managed-settings.json` | Unity Gateway defaults, `agent_native_settings` and `claude_code.managed_settings`, feature settings, gateway settings |
| `requirements.toml` | `codex.requirements`, the gateway provider |
| `config.toml` | `codex.config`, `model` from `default_models.default_model` |

While the marker file exists, no other `ug` run writes these files, and other runs read the saved
agent configuration instead of fetching it.

## Unity Gateway fields

| Field | Claude Code | Codex |
| --- | --- | --- |
| `default_models` | Each family pin becomes `env.ANTHROPIC_DEFAULT_<FAMILY>_MODEL` | `default_model` becomes `model` in `config.toml` |
| `models.model_services` | Becomes `availableModels`, `enforceAvailableModels`, and `modelPicker`. A family with no pin gets the newest model of that family in the list | Resolved per developer by `ug codex` |
| `models.unity_catalog_location` | Adds the `Databricks-Model-Service-Parent-Schema` header. Models are resolved per developer by `ug claude` | Resolved per developer by `ug codex` |
| `models.model_provider_service` | Adds the `Databricks-Model-Provider-Service` header. Family pins become `env.ANTHROPIC_DEFAULT_<FAMILY>_MODEL` | Resolved per developer by `ug codex` |
| `http_headers` | Added to `env.ANTHROPIC_CUSTOM_HEADERS` | Added to the provider's `http_headers` |
| `tracing` | Seven OTEL `env` keys and `otelHeadersHelper` | Set per launch by `ug codex` |
| `smart_routing` | The model is chosen per launch by `ug claude` | The model is chosen per launch by `ug codex` |
| `mcp_servers` | `managedMcpServers` | Registered per developer. Added to an allowlist that `codex.requirements` sets |

## Native fields

| Field | Agent | Written to | Developers can override |
| --- | --- | --- | --- |
| `agent_native_settings` | Claude Code | `managed-settings.json` | No |
| `claude_code.managed_settings` | Claude Code | `managed-settings.json` | No |
| `codex.config` | Codex | `/etc/codex/config.toml` | Yes |
| `codex.requirements` | Codex | `/etc/codex/requirements.toml` | No |

`agent_native_config` has one variant per agent, such as `claude_code` and `codex`. The server
accepts only Claude Code's MCP controls in `agent_native_settings`, and rejects an entry that sets
both `agent_native_settings` and `agent_native_config`. `ug` writes whichever is set.

Each field is a JSON object. Codex fields have the same structure as the TOML file. Values are
written as given, including `null` for Claude Code. Codex drops `null` values, because TOML has no
`null`, and the run report lists each dropped key.

## Command

```bash
ug configure --global --workspace https://<workspace> [--agents claude,codex] [--dry-run] [--from-file draft.json]
```

| Option | Effect |
| --- | --- |
| `--workspace` | The workspace. `UG_WORKSPACE`, `--profile`, or the marker file can supply it instead |
| `--agents` | The agents to rebuild. The default is every supported agent. Other agents are skipped. When the workspace differs from the last run's, every supported agent is rebuilt |
| `--dry-run` | Lists each file the run would create, replace, or remove, prints the new contents and the report, and writes nothing |
| `--from-file` | With `--dry-run`, reads a draft agent configuration in the format that `ug export` writes |

These options exit 2 in a managed run: `--use-pat`,
`--client-id`, `--mcp`, `--provider`, `--model-location`, `--enable-databricks-ai-tools`, and
`--enable-server-side-web-search`.

On a machine without the marker file, `--global` merges its settings into the existing
machine-wide files when the workspace publishes no agent configuration.

## Exit codes

| Exit | Meaning | Files |
| --- | --- | --- |
| 0 | Written, already current, or removed | Updated |
| 2 | Usage or setup error, such as a `ug` installation that other users can't run | Unchanged |
| 3 | Not root, `sudo -n` fails, and no terminal can approve a sudo prompt | Unchanged |
| 4 | The agent configuration, or its MCP servers couldn't be read | Unchanged |
| 5 | No agent configuration is published, or coding agent configs are off, on a machine with the marker file | Unchanged |
| 6 | A write failed, or another process changed a file during the write | Each file is replaced atomically. After a failed write, the run restores every file it changed. A file that another process changed is left as that process wrote it |

## Run report outcomes

| Outcome | Meaning |
| --- | --- |
| `[applied]` | The key was written as set |
| `[skipped]` | Unity Gateway owns the key, so the value was dropped. The note gives the reason |
| `[overrode Unity Gateway default]` | The value replaced a Unity Gateway default |
| `dropped` | The agent isn't in `enabled_agents`, so its files were deleted |

## Developer commands after a published agent configuration is used

| Command | Result |
| --- | --- |
| `ug claude` or `ug codex`, first run | Signs in to Databricks and writes the developer's own settings. Prints "This machine's machine-wide settings come from your workspace's published agent configuration." |
| `ug claude` or `ug codex`, later runs | Launches without checking the machine-wide files |
| `ug configure` | Updates the developer's own settings |
| `ug status` | Shows `Machine-wide settings: From the published agent configuration` |
| `ug revert` | Reverts the developer's own settings and leaves the machine-wide files |
| `ug revert` as root | Deletes the machine-wide files, the saved agent configuration, and the marker file. It then removes what an earlier `ug configure --global` without a published agent configuration left, such as the header mod |
| `claude` or `codex` | Uses the gateway, the sign-in, and the native fields, after one `ug claude` or `ug codex` run. Discovered models, tracing, and smart routing need `ug` |

Codex reads its files when it starts. Claude Code rereads `managed-settings.json` during a session,
except keys that it reads only at startup, such as `model`.

## Key classes

| Class | Your value | Unity Gateway's value |
| --- | --- | --- |
| Reserved | Dropped, and listed as `[skipped]` | Written when **Written by Unity Gateway** is Yes |
| Reserved while a feature is on | Dropped while the feature is on. Written while it is off | Written while the feature is on |
| Unity Gateway default | Written, and listed as `[overrode Unity Gateway default]` | Written when you don't set the key |
| Shared list | Kept | Added to your entries |
| Shared table | Kept, except entries with a name that Unity Gateway registers | Added to your entries |

**Written by Unity Gateway: No** marks a key that Unity Gateway never writes into the file but
still drops from your settings, so that a value set per launch or per developer takes effect.

A value that replaces a whole object counts as a value for every listed key inside it. For example,
`"env": null` is dropped, because it would remove the gateway keys.

Keys that the tables don't list are written as given. Later `ug` releases can reserve more keys.
Check the tables for the `ug` version that your fleet runs.

<!-- BEGIN GENERATED OWNERSHIP TABLES: run `uv run python -m ucode.ownership --write docs/admin-managed-settings-reference.md` -->

### Claude Code: `claude_code.managed_settings`

| Key | Class | Feature | Written by Unity Gateway | Value | Note |
| --- | --- | --- | --- | --- | --- |
| `apiKeyHelper` | Reserved |  | Yes |  | Runs `ug auth-token`, which signs in as the developer. |
| `env.ANTHROPIC_BASE_URL` | Reserved |  | Yes |  | The AI Gateway endpoint. |
| `env.ANTHROPIC_CUSTOM_HEADERS` | Reserved |  | Yes |  | Use `http_headers` in the agent configuration. |
| `env.CLAUDE_CODE_USE_GATEWAY` | Reserved |  | Yes |  | Routes Claude Code through the gateway. |
| `env.CLAUDE_CODE_API_KEY_HELPER_TTL_MS` | Reserved |  | Yes |  | It matches the token lifetime. |
| `env.ANTHROPIC_AUTH_TOKEN` | Reserved |  | Yes |  | Unity Gateway writes it blank, so a credential in the shell can't replace the gateway sign-in. |
| `env.ANTHROPIC_API_KEY` | Reserved |  | Yes |  | Unity Gateway writes it blank, so a credential in the shell can't replace the gateway sign-in. |
| `env.CLAUDE_CODE_USE_BEDROCK` | Reserved |  | Yes |  | Unity Gateway writes it blank, so developers can't bypass the gateway. |
| `env.CLAUDE_CODE_USE_VERTEX` | Reserved |  | Yes |  | Unity Gateway writes it blank, so developers can't bypass the gateway. |
| `awsAuthRefresh` | Reserved |  | No |  | It has no effect while `env.CLAUDE_CODE_USE_BEDROCK` is blank. |
| `awsCredentialExport` | Reserved |  | No |  | It has no effect while `env.CLAUDE_CODE_USE_BEDROCK` is blank. |
| `gcpAuthRefresh` | Reserved |  | No |  | It has no effect while `env.CLAUDE_CODE_USE_VERTEX` is blank. |
| `forceLoginMethod` | Reserved |  | No |  | The gateway handles sign-in. |
| `env.ANTHROPIC_DEFAULT_FABLE_MODEL` | Reserved |  | Yes |  | Use `default_models`. Unity Gateway writes it when the agent configuration pins the family. |
| `env.ANTHROPIC_DEFAULT_OPUS_MODEL` | Reserved |  | Yes |  | Use `default_models`. Unity Gateway writes it when the agent configuration pins the family. |
| `env.ANTHROPIC_DEFAULT_SONNET_MODEL` | Reserved |  | Yes |  | Use `default_models`. Unity Gateway writes it when the agent configuration pins the family. |
| `env.ANTHROPIC_DEFAULT_HAIKU_MODEL` | Reserved |  | Yes |  | Use `default_models`. Unity Gateway writes it when the agent configuration pins the family. |
| `env.ANTHROPIC_DEFAULT_FABLE_MODEL_NAME` | Reserved |  | No |  | The model picker shows the routable model ID. |
| `env.ANTHROPIC_DEFAULT_OPUS_MODEL_NAME` | Reserved |  | No |  | The model picker shows the routable model ID. |
| `env.ANTHROPIC_DEFAULT_SONNET_MODEL_NAME` | Reserved |  | No |  | The model picker shows the routable model ID. |
| `env.ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME` | Reserved |  | No |  | The model picker shows the routable model ID. |
| `env.CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` | Reserved |  | No |  | It would turn off 1-hour prompt caching. |
| `env.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY` | Reserved |  | No |  | `ug claude` sets it per launch when the developer opts in. |
| `env.ANTHROPIC_MODEL` | Reserved while a feature is on | `smart_routing` | No |  | `ug claude` sets it per launch while smart routing is on. |
| `availableModels` | Reserved while a feature is on | `models` | Yes |  | Use `models`. Unity Gateway writes it when `models.model_services` lists the models. |
| `enforceAvailableModels` | Reserved while a feature is on | `models` | Yes |  | Use `models`. Unity Gateway writes it when `models.model_services` lists the models. |
| `modelPicker` | Reserved while a feature is on | `models` | Yes |  | Use `models`. Unity Gateway writes it when `models.model_services` lists the models. |
| `env.CLAUDE_CODE_ENABLE_TELEMETRY` | Reserved while a feature is on | `tracing` | Yes |  | Unity Gateway tracing and your own OTEL export can't run together. |
| `env.CLAUDE_CODE_ENHANCED_TELEMETRY_BETA` | Reserved while a feature is on | `tracing` | Yes |  | Unity Gateway tracing and your own OTEL export can't run together. |
| `env.OTEL_TRACES_EXPORTER` | Reserved while a feature is on | `tracing` | Yes |  | Unity Gateway tracing and your own OTEL export can't run together. |
| `env.OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | Reserved while a feature is on | `tracing` | Yes |  | Unity Gateway tracing and your own OTEL export can't run together. |
| `env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Reserved while a feature is on | `tracing` | Yes |  | Unity Gateway tracing and your own OTEL export can't run together. |
| `env.CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS` | Reserved while a feature is on | `tracing` | Yes |  | Unity Gateway tracing and your own OTEL export can't run together. |
| `env.CLAUDE_CODE_PROPAGATE_TRACEPARENT` | Reserved while a feature is on | `tracing` | Yes |  | Unity Gateway tracing and your own OTEL export can't run together. |
| `otelHeadersHelper` | Reserved while a feature is on | `tracing` | Yes |  | Claude Code applies this helper to every OTEL signal, so your exporters would get Unity Gateway's headers. |
| `managedMcpServers` | Reserved |  | Yes |  | Unity Gateway writes the Databricks MCP servers from `mcp_servers` in the agent configuration. |
| `allowedMcpServers` | Reserved |  | No |  | To block servers, use `deniedMcpServers`, which you can set. |
| `env.ENABLE_PROMPT_CACHING_1H` | Unity Gateway default |  | Yes | `"1"` | 1-hour prompt caching. |
| `env.ENABLE_TOOL_SEARCH` | Unity Gateway default |  | Yes | `"true"` | Loads MCP tools on demand. |
| `permissions.deny` | Shared list |  | Yes |  | Unity Gateway adds `WebSearch`, because the gateway rejects Claude's hosted web search. |

### Codex: `codex.requirements`

| Key | Class | Feature | Written by Unity Gateway | Value | Note |
| --- | --- | --- | --- | --- | --- |
| `model_provider` | Reserved |  | Yes |  | Unity Gateway locks the provider to the gateway. |
| `model_providers` | Reserved |  | Yes |  | Unity Gateway defines the gateway provider. |
| `model_catalog_json` | Reserved |  | No |  | `ug codex` builds a model catalog for each developer, and a required one would replace it. |
| `models.new_thread` | Reserved |  | No |  | The default model comes from `default_models.default_model`. |
| `config_server` | Reserved |  | No |  | It would let another server outrank Unity Gateway's requirements. |
| `otel.trace_exporter` | Reserved while a feature is on | `tracing` | No |  | It would replace the exporter `ug codex` points at its tracing proxy. |
| `allow_managed_hooks_only` | Reserved while a feature is on | `smart_routing` | No |  | It would turn off the hooks that smart routing installs. |
| `features.hooks` | Reserved while a feature is on | `smart_routing` | No |  | It would turn off the hooks that smart routing installs. |
| `mcp_servers` | Shared table |  | Yes |  | When you set an allowlist, Unity Gateway adds its MCP servers to it. |

### Codex: `codex.config`

| Key | Class | Feature | Written by Unity Gateway | Value | Note |
| --- | --- | --- | --- | --- | --- |
| `model_provider` | Reserved |  | No |  | Unity Gateway sets it in requirements.toml, so a value here has no effect. |
| `model_providers.Databricks` | Reserved |  | No |  | Unity Gateway sets it in requirements.toml, so a value here has no effect. |
| `model` | Reserved |  | Yes |  | The default model comes from `default_models.default_model`. |
| `model_catalog_json` | Reserved |  | No |  | `ug codex` builds a model catalog for each developer. |
| `mcp_servers` | Shared table |  | Yes |  | Unity Gateway registers its MCP servers for each developer. Your entries with the same names are dropped. |

<!-- END GENERATED OWNERSHIP TABLES -->
