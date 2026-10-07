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

`ug copilot` uses the Responses API for GPT-6 and newer model IDs, and Chat
Completions for other models. The model selected at launch (including `--model`)
determines the API. An inherited `COPILOT_PROVIDER_WIRE_MODEL` takes precedence
because it overrides the model sent to the gateway. Restart Copilot through `ug`
to change the wire model or API; in-session model selection does not rebuild its
provider configuration.

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

Claude-only setup also discovers GPT models for its generated `web_search`
server; installing or configuring Codex is not required. Search registration
requires an available Responses-capable model.

Claude's generated `web_search` server uses the same saved custom OAuth CLI
profile as its harness, when configured. It stores the profile name, not an
access token, and refreshes credentials for search requests. This does not
change search permissions.

The built-in search server runs up to four searches concurrently. A slow search
does not block tool discovery or another search's result. Additional searches
wait for a worker. Cancelling a queued search prevents it from running; an
active search retains its worker until its blocking request finishes, and its
response is discarded. Closing the input stream drains accepted searches, so
shutdown can wait for the existing authentication and HTTP timeouts. Input
failure or interruption cancels queued searches and waits for active requests
to finish. This removes local serialization without changing the backend model
or speeding up an individual backend request.

Launchers that supply their own search server can first query
`ug mcp web-search --capabilities`. Contract version 1 supports setting
`UCODE_CLAUDE_WEB_SEARCH_PROVIDER=external-if-safe` on the ug child process only
when the capability response advertises that `automatic_provider` value.
The default (unset or `ucode`) retains standalone ug search. External mode
does not create, update, or delete saved search registrations. Generated
helpers carry `--managed-by-ucode`. Only the launch override created after
ownership verification exposes no tools; copied helpers and custom registrations
retain their tools even when they inherit external provider selection.

For an existing generated `web_search` entry, ug verifies its saved ownership
fingerprint and uses a launch-only MCP override pointing at the current ug
installation. This also handles older helper executables. Disabled entries
are preserved without launch overrides, and `--strict-mcp-config` excludes saved registrations without
an override. Unknown ownership, overlapping config scopes, or command-based
MCP policies produce a warning and retain the existing provider; duplicate
providers may remain. Explicit `external` selection instead fails on a conflict.
Standalone refreshes preserve edited user entries and project/local servers.
If no search model is available, they retain ownership of the installed entry so a
later refresh or external-provider launch can still verify it.
The selection does not authorize search or remove any permission denial.
Concurrent setup still uses ug's existing whole-workspace state writes. A stale
ownership fingerprint safely preserves the server and prevents automatic handoff;
this contract does not make shared setup transactional.

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

On Windows, Claude smart routing uses subagent hooks only. If first-prompt routing
is enabled, ug warns and falls back to subagent routing because the first-prompt
wrapper requires a Unix terminal.
The generated shell hooks expect Git Bash; PowerShell-only setups are not covered.

### Smart Router Orchestrator

Smart-routed Claude and Codex sessions install `smart-router`. Set
`ENABLE_ORCHESTRATION=1` at launch to also install and activate Smart Router
Orchestrator through the bundled `orchestrate` skill; orchestration is off by
default. For example:

```bash
ENABLE_ORCHESTRATION=1 ENABLE_SMART_ROUTING_SUBAGENT_ONLY=1 ug claude
```

Use `ug codex` in the same command for Codex. The orchestrator assigns bounded work
to explorer, researcher, worker, tester, and reviewer roles while the root plans,
integrates, and verifies results. Easy tasks and explicit requests not to delegate
stay in the root.

Once opted in, orchestration follows the existing smart-routing launch eligibility
and session controls. Turning Smart Router off through its skill stops new automatic delegation;
turning it on restores orchestration only in opted-in sessions. Explicit user
requests for subagents still use normal harness behavior while routing is off.
Stored skill files do not activate orchestration when the feature flag is unset or
`ENABLE_ORCHESTRATION=0`, or in non-routed sessions. Existing Isaac pilot gating
and UG launch exclusions still apply.

Hooks refresh orchestration state before each prompt and after compaction. A
state change made outside the conversation is observed at the next hook; model
routing still checks the controls for each subagent.

UG supplies its own hooks; Codex combines them with existing hooks and applies
project trust. Smart routing selects subagent models; separate role-model
preferences are ignored and their files are left untouched. See the bundled
[Smart Router Orchestrator documentation](skills/orchestrate/README.md) for details.

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
