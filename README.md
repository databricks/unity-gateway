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

Claude and Codex also accept a local coding-agent configuration on each launch:

```bash
ug claude --config-file './agent config.json' -- --settings ./claude-settings.json
ug codex -f ./agent-config.json exec 'Explain this project'
ucode codex -f ./agent-config.json -- 'Start a session'
```

Put `-f` / `--config-file` before the `--` separator. Arguments after it belong to
the agent, including its own `-f`. The file uses the published `CodingAgentConfig`
JSON shape, for example:

```json
{
  "spec_version": 1,
  "enabled_agents": [
    {
      "agent": "CODING_AGENT_CODEX",
      "config": {
        "default_models": {"default_model": "system.ai.databricks-gpt-5-2"}
      }
    }
  ]
}
```

The requested agent must be enabled. Every launch reads and validates the entire
file before bootstrap or settings writes, including first use. Invalid files stop
the launch without falling back to workspace policy. File contents never enter
the five-minute API cache at `~/.ucode/managed-config.json`; omitting `-f` restores
the ordinary workspace API source. `--refresh` bypasses that API cache. File
launches always reread their input and skip workspace budget recommendations.

Local files reject nonempty `mcp_servers`, `skills`, `smart_defaults`, and
legacy `spend_tiers`. Isaac plugin and MCP assets remain separate. Existing
same-workspace UC resources are retained outside the file's ownership. A workspace
transition requiring UC MCP or managed-skill cleanup stops with migration guidance.

File applications own their declared effects under the stable owner `local-file`,
independent of filename and workspace. Updating or omitting a field reconciles its
previously owned value, including edited values, while preserving unrelated fields.
Ownership, original backups, applied source metadata, and recovery journals live
together under `~/.ucode/managed-backups`. A failed application blocks launch and
retains its journal for retry. Status and doctor report the applied file source;
`ug export` continues to export workspace API configuration.

Integrations can supply an optional top-level `handoff` object:

```json
{
  "schema_version": 1,
  "owner": "integration-name",
  "migration_version": 1,
  "agents": {
    "claude": {
      "adopt": [{"target": "private_settings", "path": ["apiKeyHelper"]}],
      "retire": [{"target": "managed_settings", "path": ["env", "RETIRED_VARIABLE"]}]
    }
  }
}
```

Adoption requires a field the current application declares. Targets are fixed
agent surfaces: `user_settings`, `private_settings`, and `managed_settings`.
The file cannot choose filesystem destinations. Array declarations use an
`elements` list of exact contributions, preserving other elements and sibling
hook handlers. Changing migration declarations requires a new positive
`migration_version`; replaying a completed retirement preserves later user values.
The `process_env` target addresses one environment name, for example
`{"target": "process_env", "path": ["RETIRED_VARIABLE"]}`. It does not accept
`elements`. Adoption requires a currently declared custom variable; retirement
suppresses an inherited variable once under the named owner. Codex also supports
the fixed `requirements` target for declared native requirements.

An integration can remove its current generated settings, routing, authentication,
and catalog effects with `ug managed-config release --owner integration-name --agent claude`
(or `--agent codex`). Release is idempotent, does not discover models or launch an
agent, and preserves effects transferred to another owner. It may require an
interactive terminal to clean machine-wide settings. Release removes current
effects; the separate `ug revert` command restores original backups. Disabled
agents with outstanding effects must be released before changing the enabled set.

Each agent's `config.custom_env` is a map of environment names to exact strings:

```json
"custom_env": {
  "TEAM_CONTEXT": "engineering",
  "OTEL_RESOURCE_ATTRIBUTES": "service.namespace=development",
  "EMPTY_VALUE": ""
}
```

Claude receives these through its settings and child process environment. Codex
receives an actual process environment overlay, including its TUI and app-server;
`shell_environment_policy.set` is not used as a substitute. Values are transient
UG launch inputs, not saved developer preferences. Removed owned keys stay absent
from subsequent agent launches even when the parent shell still supplies them.
Unrelated inherited variables are preserved.

UG rejects non-string values, invalid names, NULs, generated authentication,
routing and config-home controls, and conflicts with explicitly enabled UG tracing.
OTEL variables otherwise follow the same rules as every custom variable. A custom
`PATH` affects the agent and its children, not UG authentication or lookup of the
initial agent executable. Bare agent launches and independently opened desktop
apps do not receive this runtime overlay.

The published `tracing.enabled` flag remains additive: `false` or omission does
not disable a saved explicit UG tracing preference. Conflicting custom exporters
are rejected against that effective preference too.

Local `config.native_settings` declares supported native Claude or Codex settings.
Codex additionally accepts `config.native_requirements`, initially only
`{"features":{"fast_mode":false}}`. Requirements are written to the actual
OS policy destination, not a private-file fallback. Required policy write failures
stop launch. Generic native settings do not transfer Isaac's conditional user
preferences unless those fields are explicitly declared.

See [the local integration contract](docs/local-managed-config.md) for the exact
native inventory, fixed destinations, migration rules, examples, and limitations.
The initial native subset is pinned to Claude 2.1.268 and Codex 0.154.0.
Nonempty native extensions are rejected on known legacy Codex layouts. Claude
org-login pins are rejected because UG does not establish compatible first-party
OAuth enforcement. Ordinary launches without these extensions remain supported.

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
