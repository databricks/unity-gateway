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

MCP servers let your coding agents use Databricks-governed tools — like GitHub, Slack,
Vector Search, and Genie. `ug` sets them up for all your installed agents at once.

```bash
# Add tools — a whole catalog.schema, or specific ones by name.
ug mcp add --location system.ai
ug mcp add --names system.ai.github,system.ai.slack

# See what's set up, and whether each is signed in.
ug mcp list

# Sign in to a tool that needs it (opens your browser). Plain form shows a picker.
ug mcp login
ug mcp login --names system.ai.github

# Remove tools (plain form shows a picker).
ug mcp remove
```

Here's what a typical session looks like — add two tools, check their status, then sign in:

```text
$ ug mcp add --names system.ai.github,system.ai.slack
✔ Added 2 MCP servers across Claude Code, Codex

$ ug mcp list
  NAME              LOCATION          AGENTS         STATUS
  system-ai-github  system.ai.github  claude, codex  needs sign-in
  system-ai-slack   system.ai.slack   claude, codex  needs sign-in

$ ug mcp login --names system.ai.github
Signing in to 'system.ai.github' — opening your browser to finish sign-in…
✔ Signed in to 1 MCP service(s)
```

**You only sign in once.** Some tools (like `system.ai.github`) ask you to sign in to the
underlying service the first time. Do it once — through any agent or `ug mcp login` — and the
tool works everywhere: Claude, Cursor, Codex, and the rest.

<details><summary>Advanced options</summary>

- Limit any command to certain agents: add `--agents claude,codex`.
- Add other AI Gateway tools by typed name: `vector-search:main.docs`,
  `uc-functions:main.tools`, `external:<name>`, `genie-space:<space-id>`, `app:<name>`.
- Each tool runs locally through `ug mcp-proxy`, which keeps your Databricks sign-in fresh —
  no tokens to copy or manage.
- Sign-in works for any connection-backed tool, not just `system.ai.*`, and needs a recent
  Databricks CLI (`ug mcp login` tells you if yours is too old).

</details>

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
| `ug mcp login` | Sign in to connection-backed MCP services (interactive, or `--names`) |
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
