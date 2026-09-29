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

### Field ownership on local launches (proposed)

This is a proposed allow list, under review and not implemented. Every
`ug claude` and `ug codex` launch writes settings, even when the workspace has
no managed config (the coding agent config admins publish through the API).
Other tools, such as MDM or launch wrappers, may write the same files first.
Under this proposal, `ug` touches only the fields below and ignores every other
field.

<details>
<summary>Claude Code: <code>~/.claude/ucode-settings.json</code> and the OS managed-settings file, which takes precedence</summary>

| Field | `ug` without managed config | `ug` with managed config |
|-------|-----------------------------|--------------------------|
| `env.ANTHROPIC_BASE_URL`, `apiKeyHelper`, `env.CLAUDE_CODE_API_KEY_HELPER_TTL_MS`, `env.CLAUDE_CODE_USE_GATEWAY`, `env.ENABLE_PROMPT_CACHING_1H`, `env.ENABLE_TOOL_SEARCH` | Update | Update |
| `env.ANTHROPIC_CUSTOM_HEADERS` | Merge: replace only `x-databricks-use-coding-agent-mode`, `User-Agent`, `Databricks-Model-Provider-Service`, `Databricks-Model-Service-Parent-Schema`, and `Databricks-Smart-Router-Recipe` | Update the whole value, adding admin headers |
| `env.ANTHROPIC_DEFAULT_{FABLE,OPUS,SONNET,HAIKU}_MODEL` | Keep an existing value. Write a discovered default only if the value is unset or `ug` wrote it | The config's family default wins. Otherwise, same as without |
| `env.ANTHROPIC_MODEL` | Ignore. `--model` applies to that launch only | Update with the launch model |
| `availableModels`, `enforceAvailableModels`, `modelPicker` | Ignore | Update for a static model list or config family defaults |
| `env.CLAUDE_CODE_ENABLE_TELEMETRY`, `env.CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`, `env.OTEL_TRACES_EXPORTER`, `env.OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`, `env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `env.CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS`, `env.CLAUDE_CODE_PROPAGATE_TRACEPARENT`, `otelHeadersHelper` | Remove only values `ug` wrote | Update when the config enables tracing. Otherwise, remove only values `ug` wrote |

</details>

<details>
<summary>Codex: <code>~/.codex/ucode.config.toml</code> and <code>/etc/codex/managed_config.toml</code></summary>

| Field | `ug` without managed config | `ug` with managed config |
|-------|-----------------------------|--------------------------|
| `model_provider`, `[model_providers.Databricks]` including `http_headers` | Update | Update, adding admin headers |
| `model`, `model_reasoning_effort` | Ignore | Update `model` with the config's `default_model` |

</details>

Current behavior differs from this proposal in these ways:

- With no managed config, `ug` removes `env.CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`,
  `env.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`, `env.ANTHROPIC_MODEL`, the
  `env.ANTHROPIC_DEFAULT_*_MODEL_NAME` companions, the trace keys above, and
  `otelHeadersHelper` from both Claude files, even when another tool wrote them.
  It removes the trace keys and `otelHeadersHelper` the same way with a managed
  config that does not enable tracing.
- `ug` overwrites family defaults in `~/.claude/ucode-settings.json`. The managed
  file already keeps a family default that differs from `ug`'s last write.
- Outside the listed fields, `ug` also removes its own marked smart-routing
  hooks, adds `WebSearch` to `permissions.deny` when it replaces web search, and
  adds or removes its own `model_catalog_json` reference in `~/.codex/config.toml`.
  It never writes Codex `[otel]` to a file. Managed tracing only passes a trace
  exporter for that launch with `--config`.

Open questions:

- Switching from workspace A (managed tracing) to workspace B (no managed
  config) must not keep sending traces to A's endpoint with A's auth helper.
  The rule for recognizing values `ug` wrote is not decided. The managed file
  already has a last-applied snapshot. On launch, `ug` uses it today only for
  family defaults and picker keys.
- Today, admin headers that A's managed config adds to `ANTHROPIC_CUSTOM_HEADERS`
  stay in place after a switch to B with no managed config.
- A budget recommendation or `default_model` written as `ANTHROPIC_MODEL`
  overrides family defaults. This is likely a bug, to be fixed separately.

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
