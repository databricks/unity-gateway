# Unity Gateway (`ug`)

Unity Gateway runs coding agents through Databricks AI Gateway. It configures
Codex, Claude Code, Gemini CLI, OpenCode, GitHub Copilot CLI, and Pi, and can
register Databricks MCP servers for Cursor Agent.

The command is `ug`. Existing `ucode` commands remain supported, and the Python
package is still named `ucode` for compatibility.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- `npm`, when an agent CLI needs automatic installation

## Install

```bash
uv tool install git+https://github.com/databricks/unity-gateway
ug --version
```

### Migrating from ucode

Reinstall under the new distribution name:

```bash
uv tool uninstall ucode
uv tool install git+https://github.com/databricks/unity-gateway
ug --version
ucode --version
```

Future upgrades can use `ug upgrade` or `uv tool upgrade unity-gateway`.

## Launch Agents

Run the tool you want:

```bash
ug codex
ug claude
ug gemini
ug opencode
ug copilot
ug pi
ug cursor   # Cursor Agent, MCP-only
```

On first launch of a model-backed agent, `ug` prompts for a Databricks
workspace, authenticates, and writes local agent config. Later launches reuse
the saved workspace and credentials.

Use `ug opencode --model system.ai.glm-5-3` (or `-m`) to select a configured
model for one launch. OpenCode's `provider/model` form is also accepted.
Unknown Databricks models produce an error; this option does not add models
to discovery or change ug's saved default.

Without a managed workspace config, `ug claude` automatically discovers gateway
models for Claude Code's `/model` picker. Discovery defaults to `system.ai` when
no provider or model location is selected. Use `--provider` or `--model-location`
to select another model source; managed workspace configs control their own sources.

`ug codex` validates discovered models with the installed Codex binary and publishes
them to `~/.ucode/codex-model-catalog.json`, referenced by shared `~/.codex/config.toml`
for Codex App. Managed static lists use the same path during `ug configure`. The
latest refresh supplies the app's catalog; custom catalogs (including Isaac's) and
custom providers are preserved. The app's gateway provider and authentication must
already be configured. Validation covers the local Codex binary.

Codex loads the catalog at app-server startup. When ug reports a catalog change,
finish active tasks, restart the app server on the **connected host**, then reconnect.
Use `codex app-server daemon restart` for a standalone managed daemon; otherwise
restart the process or application that owns the server. Reconnecting or reopening
the desktop app can reuse a remote server with the old list.

ug removes its shared reference on discovery/validation failure, reconfiguration,
revert, or before installing/updating Codex. After an update, run `ug codex` to refresh
discovery or `ug configure` for a managed static list, then restart the app server.

## Configure

```bash
ug configure
ug configure --agents claude,codex
ug configure --workspace https://first.databricks.com
ug configure --profile DEFAULT --agents claude,codex
```

Available coding agents are `codex`, `claude`, `gemini`, `opencode`,
`copilot`, and `pi`. `cursor` can be included in `--agents` for MCP-only setup;
Cursor models still run through your Cursor account.

`UG_WORKSPACE` can provide the default workspace. An explicit `--workspace` or
`--profile` takes precedence.

## MCP Servers

Register Databricks MCP servers for configured MCP-capable agents. Cursor Agent
is MCP-only and is included when `cursor-agent` is installed:

Use `ug mcp add` to add servers without removing existing registrations:

```bash
ug mcp add --location system.ai
ug mcp add --names system.ai.slack,system.ai.github
ug mcp add --agents claude,codex --location system.ai
```

Remove configured servers:

```bash
ug mcp remove
ug mcp remove --agents codex
```

List configured servers and their connection status:

```bash
ug mcp list
ug mcp list --agents claude,codex
```

Every Databricks MCP server is registered as a local stdio server that runs
`ug mcp-proxy`; the proxy refreshes Databricks OAuth tokens from your CLI
profile. V2 AI Gateway servers can be added with typed selectors such as
`vector-search:main.docs`, `uc-functions:main.tools`, `external:<name>`,
`genie-space:<space-id>`, or `app:<name>`.

## Skills

Unity Catalog Skills can be registered as MCP tools or downloaded into local
agent skill directories.

```bash
# Set up the Databricks skills MCP so your agents can create and manage skills through ug.
ug skills

# List configured skills and how each was configured.
ug skills list

# Download every skill in a schema into your local agent skill directories.
ug skills add --location main.default

# Download specific skills by fully-qualified name (may span schemas).
ug skills add --names main.default.my-skill,ml.prod.other-skill

# Add a schema to the MCP connection scope, exposing its skills as MCP tools.
ug skills add --location main.default --via mcp

# Interactively pick downloaded skills to delete.
ug skills remove

# Delete a specific downloaded skill by fully-qualified name.
ug skills remove --names main.default.my-skill

# Drop a schema from the MCP connection scope.
ug skills remove --location main.default --via mcp
```

## Commands

| Command | Description |
|---------|-------------|
| `ug status` | Show workspace, generated files, models, and skill MCP scope |
| `ug configure` | Configure workspace, models, agent files, and optional Databricks AI tools |
| `ug configure --dry-run` | Preview config changes without writing files |
| `ug mcp add` | Add MCP servers without removing existing registrations |
| `ug mcp remove` | Unregister configured MCP servers |
| `ug mcp list` | List configured MCP servers and connection status |
| `ug skills` | Set up the Databricks skills MCP so agents can create and manage skills |
| `ug skills list` | List configured skills and how each was configured |
| `ug skills add` | Add skill MCP scopes or download skills |
| `ug skills remove` | Remove skill MCP scopes or downloaded skills |
| `ug export` | Print or write portable managed config JSON |
| `ug doctor` | Diagnose local setup and offer fixes |
| `ug usage` | Show AI Gateway spend and budget |
| `ug revert` | Clear saved state and restore backed-up config files |
| `ug upgrade` | Upgrade Unity Gateway |

Databricks AI Tools are installed only by `ug configure`, never by agent launch
commands. Use `--enable-databricks-ai-tools` or `--disable-databricks-ai-tools`
with `ug configure` to control installation.

## Claude Routing Plugin

Smart routing passes generated agents through a per-launch `--plugin-dir`,
alongside `--settings`, without persistent plugin registration. One temporary
directory holds the settings, socket, and plugin and is removed when the launch
finishes or fails. Existing hook configuration and disable/revert behavior are
unchanged. Native daemon/background propagation of the plugin remains unverified.

## Managed Files

`ug` backs up files before overwriting them. `ug revert` restores backups.

| Tool | Managed files |
|------|---------------|
| Codex | `~/.codex/ucode.config.toml`, shared catalog reference in `~/.codex/config.toml`, `~/.ucode/codex-model-catalog.json`, `/etc/codex/managed_config.toml` (Linux and macOS) |
| Claude Code | `~/.claude/ucode-settings.json`, `~/.claude.json`, `/etc/claude-code/managed-settings.json` (Linux), `/Library/Application Support/ClaudeCode/managed-settings.json` (macOS) |
| Gemini CLI | `~/.gemini/ucode.env`, `~/.ucode/.gemini-home/.gemini/settings.json` |
| OpenCode | `~/.ucode/opencode-xdg/opencode/opencode.json`, `~/.ucode/opencode-xdg/opencode/plugin/ucode-auth.js` |
| GitHub Copilot CLI | `~/.copilot/ucode.env`, `~/.copilot/ucode-mcp-config.json` |
| Pi | `~/.ucode/pi-home/.pi/agent/models.json`, `~/.ucode/pi-home/.pi/agent/settings.json` |
| Cursor Agent | `~/.cursor/mcp.json` |
| Unity Gateway | `~/.ucode/managed-state.json`, `~/.ucode/managed-backups/` |

### Fields `ug configure` writes

`ug configure` writes only the fields below and leaves every other field as it
is. For Claude Code, the fields live in `~/.claude/ucode-settings.json` (the
private file) and the OS managed-settings file, which takes precedence. For
Codex, they live in `~/.codex/ucode.config.toml` and
`/etc/codex/managed_config.toml`. "Managed config" is the coding agent config
admins publish through the API. ⚠️ marks behavior that can remove or replace a
value another tool wrote.

<details>
<summary>Claude Code</summary>

| Field | Without managed config | With managed config |
|-------|------------------------|---------------------|
| `apiKeyHelper` | Set to the `ug auth-token` helper. ⚠️ Removed for relayed subscription auth | Same |
| `env.ANTHROPIC_BASE_URL` | Set to the workspace gateway URL | Same |
| `env.ANTHROPIC_CUSTOM_HEADERS` | Merged by header name: replaces `x-databricks-use-coding-agent-mode`, `User-Agent`, `Databricks-Model-Provider-Service`, `Databricks-Model-Service-Parent-Schema`, and `Databricks-Smart-Router-Recipe`, and keeps other lines | ⚠️ Whole value replaced with `ug` headers plus admin headers |
| `env.CLAUDE_CODE_API_KEY_HELPER_TTL_MS` | Set | Same |
| `env.ENABLE_PROMPT_CACHING_1H` | Set | Same |
| `env.ENABLE_TOOL_SEARCH` | Set | Same |
| `env.CLAUDE_CODE_USE_GATEWAY` | Set | Same |
| `env.ANTHROPIC_MODEL` | ⚠️ Removed | ⚠️ Removed. `ug claude` then sets it to the budget recommendation or the config's `default_model` |
| `env.ANTHROPIC_DEFAULT_FABLE_MODEL` | Private file: set to the discovered default. Managed file: keeps a value that differs from `ug`'s last write, otherwise set to the discovered default | The config's family default wins. Otherwise, same as without |
| `env.ANTHROPIC_DEFAULT_FABLE_MODEL_NAME` | ⚠️ Removed | ⚠️ Removed |
| `env.ANTHROPIC_DEFAULT_OPUS_MODEL` | Private file: set to the discovered default. Managed file: keeps a value that differs from `ug`'s last write, otherwise set to the discovered default | The config's family default wins. Otherwise, same as without |
| `env.ANTHROPIC_DEFAULT_OPUS_MODEL_NAME` | ⚠️ Removed | ⚠️ Removed |
| `env.ANTHROPIC_DEFAULT_SONNET_MODEL` | Private file: set to the discovered default. Managed file: keeps a value that differs from `ug`'s last write, otherwise set to the discovered default | The config's family default wins. Otherwise, same as without |
| `env.ANTHROPIC_DEFAULT_SONNET_MODEL_NAME` | ⚠️ Removed | ⚠️ Removed |
| `env.ANTHROPIC_DEFAULT_HAIKU_MODEL` | Private file: set to the discovered default. Managed file: keeps a value that differs from `ug`'s last write, otherwise set to the discovered default | The config's family default wins. Otherwise, same as without |
| `env.ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME` | ⚠️ Removed | ⚠️ Removed |
| `env.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY` | ⚠️ Removed | ⚠️ Removed |
| `env.CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` | ⚠️ Removed | ⚠️ Removed |
| `env.CLAUDE_CODE_ENABLE_TELEMETRY` | ⚠️ Removed | Set when the config enables tracing. ⚠️ Otherwise removed |
| `env.CLAUDE_CODE_ENHANCED_TELEMETRY_BETA` | ⚠️ Removed | Set when the config enables tracing. ⚠️ Otherwise removed |
| `env.OTEL_TRACES_EXPORTER` | ⚠️ Removed | Set when the config enables tracing. ⚠️ Otherwise removed |
| `env.OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | ⚠️ Removed | Set when the config enables tracing. ⚠️ Otherwise removed |
| `env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | ⚠️ Removed | Set to the workspace trace endpoint when the config enables tracing. ⚠️ Otherwise removed |
| `env.CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS` | ⚠️ Removed | Set when the config enables tracing. ⚠️ Otherwise removed |
| `env.CLAUDE_CODE_PROPAGATE_TRACEPARENT` | ⚠️ Removed | Set when the config enables tracing. ⚠️ Otherwise removed |
| `otelHeadersHelper` | ⚠️ Removed | Set to the `ug otel-headers` helper when the config enables tracing. ⚠️ Otherwise removed |
| `availableModels` | Normally not written. ⚠️ The private file removes it. The managed file keeps it unless it matches `ug`'s last write | Set for a static model list |
| `enforceAvailableModels` | Same as `availableModels` | Same as `availableModels` |
| `modelPicker` | Same as `availableModels` | Set for a static model list. `ug claude` also sets it for config family defaults |
| `permissions.deny` | Adds `WebSearch` when `ug` replaces web search. The managed file keeps existing rules. ⚠️ The private file replaces the list | Same |
| `managedMcpServers` (managed file only) | ⚠️ Whole key removed | ⚠️ Whole key replaced with the config's MCP servers |
| `hooks.PreToolUse` | Removes only `ug`'s own marked smart-routing hooks | Same |
| `hooks.SessionStart` | Same as `hooks.PreToolUse` | Same |
| `hooks.SubagentStart` | Same as `hooks.PreToolUse` | Same |

</details>

<details>
<summary>Codex</summary>

| Field | Without managed config | With managed config |
|-------|------------------------|---------------------|
| `model_provider` | Set to `Databricks` | Same |
| `model` | ⚠️ Removed, unless smart routing is enabled | Set to the config's default model |
| `model_reasoning_effort` | ⚠️ Removed, unless smart routing is enabled | Same, unless the config sets a default model |
| `model_providers.Databricks` | Set: name, gateway base URL, wire API, and the `ug` auth command | Same |
| `model_providers.Databricks.http_headers` | ⚠️ Whole table replaced with `User-Agent` and routing headers | ⚠️ Whole table replaced with those plus admin headers |
| `model_catalog_json` (`~/.codex/config.toml`) | Adds or removes only `ug`'s own catalog reference | Same |
| `mcp_servers` (managed file only) | ⚠️ Whole table removed | ⚠️ Whole table replaced with the config's MCP servers |

Codex `[otel]` is never written to a file.

</details>

Open questions:

- Switching from workspace A (managed tracing) to workspace B (no managed
  config) must not leave A's trace endpoint and `otelHeadersHelper` in place.
  Today `ug` prevents that by removing the tracing fields by name. The rule
  for removing only values `ug` wrote is not decided.
- Admin headers that A's managed config adds to `ANTHROPIC_CUSTOM_HEADERS`
  stay in place after a switch to B with no managed config.
- `ANTHROPIC_MODEL` set by `ug claude` overrides the family defaults. This is
  likely a bug.

## Development

```bash
git clone https://github.com/databricks/unity-gateway
cd unity-gateway
uv sync
uv run pytest
uv run ruff check .
```

For integration tests against installed `ug` and agent versions, see
[tests/integration/README.md](tests/integration/README.md). The existing e2e
tests remain available separately:

```bash
UCODE_TEST_WORKSPACE=<db_workspace_url> uv run pytest tests/test_e2e.py -v
```

To add a new agent, implement `src/ucode/agents/<name>.py`, register it in
`src/ucode/agents/__init__.py`, and add focused tests.

## Documentation

- [Databricks AI Gateway overview](https://docs.databricks.com/aws/en/ai-gateway/overview-beta)
- [Databricks AI Gateway coding agent integration](https://docs.databricks.com/aws/en/ai-gateway/coding-agent-integration-beta)
- [Databricks CLI authentication](https://docs.databricks.com/aws/en/dev-tools/cli/authentication)
- [Monitor AI Gateway usage](https://docs.databricks.com/aws/en/ai-gateway/configure-ai-gateway-endpoints#track-usage-of-an-endpoint)

## Security

Please report security vulnerabilities to security@databricks.com rather than
opening a public issue.

## License

See [LICENSE.md](./LICENSE.md) and [NOTICE.md](./NOTICE.md).
