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
ug --help
```

`--version` reports installed package metadata without loading the CLI. Use
`ug --help` (or `ucode --help`) to verify that the CLI's startup imports work;
version output alone is not an installation health check. Help does not validate
workspace authentication or agent runtime behavior.

### Migrating from ucode

Reinstall under the new distribution name:

```bash
uv tool uninstall ucode
uv tool install git+https://github.com/databricks/unity-gateway
ug --version
ucode --version
ug --help
ucode --help
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

When Claude Code or Codex is older than the minimum version `ug` supports, `ug`
asks before upgrading it in place. Set `UG_AUTO_UPGRADE_AGENT=1` to upgrade
without asking, for example from a launcher that manages the agent install.

Use `ug opencode --model system.ai.glm-5-3` (or `-m`) to select a configured
model for one launch. OpenCode's `provider/model` form is also accepted.
Unknown Databricks models produce an error; this option does not add models
to discovery or change ug's saved default.

`ug copilot` lists every Claude and GPT model you can use on the workspace in
Copilot's `/model` picker, and you can switch between them mid-session. Claude
models use the Anthropic Messages API, GPT-5 and newer use Responses, and older
GPT models use Chat Completions. Gemini and open-source models aren't offered, except under
`--model-location` (below).
`ug copilot --model claude-opus-5-5` (or `system.ai.claude-opus-5-5`, or Copilot's
own `dbx-anthropic/system.ai.claude-opus-5-5`) picks the model a session starts on;
an unknown or unsupported model fails before Copilot starts and lists the
available ones. ug passes Copilot a per-session model registry, so your own
`~/.copilot/providers.json` and `COPILOT_PROVIDER_*` settings apply only to plain
`copilot`. Use plain `/model`, not `/model --global`, inside `ug copilot`: a
global choice is saved for plain `copilot` too, where the Databricks models don't
exist. Requires Copilot CLI 1.0.91 or newer.

`ug copilot` runs Copilot against a loopback proxy for the session. Copilot holds only
a random session secret, kept in the session's `~/.copilot/ug-providers-<id>.json`
registry (readable only by you, deleted when the session ends); the proxy adds a fresh Databricks token to
each gateway request, so OAuth sessions keep working past token expiry without a
restart, and no Databricks token is written to disk. If the Databricks sign-in itself expires,
Copilot shows a "Databricks sign-in expired" error; run `databricks auth login` and
restart `ug copilot`. Proxy errors are logged to `~/.ucode/logs/copilot-proxy.log`.

`ug copilot --provider <catalog>.<schema>.<name>` routes the session through a Model Provider
Service instead, and `/model` lists that service's models: Claude models of an Anthropic or
Bedrock service, and the chat models of an OpenAI, Azure OpenAI or Microsoft Foundry service. For a service that
allows every model, `/model` lists the models the gateway reports for it, which the provider
account may not serve; name the one to start on with `--model`. Services that relay a Claude subscription work only with Claude Code. Interactive
`ug configure` offers the matching services under **External Models**, and a managed workspace
config can set one for Copilot. `ug copilot --model-location <catalog>.<schema>` uses the model
services of one schema instead: services named like Claude or GPT models use their usual API, and
the rest go through the gateway's Anthropic endpoint, as for Claude Code.

Without a managed workspace config, `ug claude` automatically discovers gateway
models for Claude Code's `/model` picker. Discovery defaults to `system.ai` when
no provider or model location is selected. Use `--provider` or `--model-location`
to select another model source; managed workspace configs control their own sources.
Interactive `ug configure` also offers a **Custom Model Location** (`<catalog>.<schema>`) for
Claude Code, Codex and Copilot alongside Databricks Hosted and External Models. The choice is saved
with the rest of the configuration, so later `ug claude` / `ug codex` / `ug copilot` launches use it; an
explicit `--provider` or `--model-location` still overrides it for that launch.

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
ug configure --user
```

`--global` (the default) writes the machine-wide Claude Code and Codex managed settings with sudo.
`--user` skips them, like `UG_SKIP_OS_SETTINGS`, and needs no sudo; use it when you launch through
`ug`. `UG_SKIP_OS_SETTINGS=1` neither writes nor checks the OS-managed files: ug trusts whatever is
there and the agents apply their native precedence. Configure fails while a managed file for the agent exists, since managed settings take
precedence; later launches use whatever files are present.
The choice is saved for launches after a successful configure; `ug configure` without `--user`
and `ug revert` return to `--global`.
`--user` also fails for a workspace that publishes a managed config, which must be installed
machine-wide; use `--global` for it.

### Extra headers for one session

```bash
ug --header "x-databricks-traffic-id: testenv://liteswap/<pod>" claude
UG_HEADERS="X-Team: eng" ug codex
```

`--header` (repeatable) and `UG_HEADERS` (one `Name: value` per line) add headers to that launch's
Databricks requests only: Claude Code's and Codex's gateway requests, and ug's own requests
(workspace APIs, MCP proxy, web search, smart routing). A header already sent keeps its value.
Nothing is written to a config file.

- **Codex**: added to the launch's `-c` provider headers.
- **Claude Code**: added by a mod, even when managed settings set `ANTHROPIC_CUSTOM_HEADERS`.
  Only `ug configure` installs or upgrades it beside the managed settings (Linux and macOS, with
  sudo, not under `--user`); a launch never writes there. When it is missing or out of date the
  launch warns and Claude doesn't send them.

Available coding agents are `codex`, `claude`, `gemini`, `opencode`,
`copilot`, and `pi`. `cursor` can be included in `--agents` for MCP-only setup;
Cursor models still run through your Cursor account.

`UG_WORKSPACE` can provide the default workspace. An explicit `--workspace` or
`--profile` takes precedence.

On macOS, Windows, and Linux, successful Claude Code configuration also creates and
selects a dedicated Claude Desktop gateway profile. Its model list includes only
Claude-named IDs; Claude Code's catalog is unchanged. Desktop setup errors warn
without failing Claude Code configuration. See [Claude Desktop setup](docs/claude-desktop.md)
for authentication, profile preservation, and native validation limits.
When managed Claude tracing is enabled, the same profile configures Desktop's native
OTLP trace export with a refreshing headers helper; no background service is needed.
Anthropic subscription relay profiles use one helper to supply the short-lived Claude
subscription token and Databricks gateway token on macOS, Windows, and Linux.

## MCP Servers

Register Databricks MCP servers for configured MCP-capable agents. Cursor Agent
is MCP-only and is included when `cursor-agent` is installed:

Run `ug mcp add` without selectors to browse a table of services by name, UC location,
and status. ✓ means configured (or managed by the workspace); ✗ means not configured.
Configured and managed services stay visible as read-only rows. `[ ]`/`[x]` indicate
what you select for this operation; `[-]` marks a read-only row.

Highlight a service to see its description, creator, and creation/update dates. Type
to filter names or UC locations, use Space to select, and Enter to save. Page Up/Down
scrolls the detail pane. Narrow terminals shorten cells and show status symbols;
the detail pane retains the full location and status.

Use `ug mcp add` to add servers without removing existing registrations:

```bash
ug mcp add --location system.ai
ug mcp add --names system.ai.slack,system.ai.github
ug mcp add --agents claude,codex --location system.ai
ug mcp add --agents cursor --names system.ai.github --workspace https://first.databricks.com
ug mcp add --agents codex --names system.ai.github --profile DEFAULT
```

With `--agents`, agents that aren't set up yet are set up first. Pass `--workspace`
or `--profile` to pick the workspace instead of the picker; it becomes the current
workspace.

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

Connection-backed AI Gateway MCP services use native HTTP OAuth in Claude Code,
Codex, and Cursor when the workspace publishes a compatible client. Claude Code
uses its URL-based client ID metadata document; if it is unavailable, `ug` uses
the local `ug mcp-proxy` stdio server with the Databricks CLI's OAuth client.
Other servers and agents also use that proxy. When a proxied connection needs
provider authentication, UG opens the workspace's MCP service sign-in page and
waits up to five minutes for the selected CLI profile's user credential, then retries
the original request. Use the same Databricks account in the browser and CLI profile.
If no browser opens, use the URL in the MCP server log; click Login if the provider
popup is blocked. This flow requires the workspace's MCP service login page and user-credential API;
it does not require CLI `auth login --resource` or a CLI upgrade.
V2 AI Gateway servers can be added with typed selectors such as
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
agent skill directories. Run `ug skills add` without selectors to browse individual
skills in the same table and metadata pane as the MCP browser, plus the bundle name.
✓ Installed requires a recorded download of that exact UC skill with `SKILL.md` still
present at every target agent directory (`--path`, or your home directory). A matching
bundle name from another schema or metastore does not qualify. Unattributed local
bundles and incomplete installations show ✗ Not installed, with an explanation in the
detail pane. Installed skills remain selectable for updates.
Missing metadata is shown explicitly. `Created by` is the UC creator, which may
differ from the author of the bundle.

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
| `ug agents add <agent>` | Allow a non-admin-enabled agent to run self-managed |
| `ug agents remove <agent>` | Remove an agent from your self-managed list |
| `ug agents list` | Show the admin-managed and self-managed agents |
| `ug skills` | Set up the Databricks skills MCP so agents can create and manage skills |
| `ug skills list` | List configured skills and how each was configured |
| `ug skills add` | Add skill MCP scopes or download skills |
| `ug skills remove` | Remove skill MCP scopes or downloaded skills |
| `ug export` | Print or write portable managed config JSON |
| `ug doctor` | Diagnose local setup and offer fixes |
| `ug usage` | Show Unity Gateway spend and budget |
| `ug revert` | Clear saved state and restore backed-up config files |
| `ug upgrade` | Upgrade Unity Gateway |

`ug usage` shows your Unity Gateway budget usage. To try the expanded budget view:

```bash
ENABLE_UG_USAGE_SPEND_API=1 ug usage
```

The expanded view shows up to five budget limits with the highest usage percentages,
with a short budget ID on each entry. It tells you when more limits exist.
Add `--all` to show every limit: `ENABLE_UG_USAGE_SPEND_API=1 ug usage --all`.
It uses your configured workspace login; no separate login is needed.
Leave `ENABLE_UG_USAGE_SPEND_API` unset to keep the standard summary.

A launch (`ug claude`, `ug codex`) uses the workspace's managed coding agent config that
`ug configure` last saved and does not refetch it. After an admin changes that config, run
`ug configure` to pick it up.

Databricks AI Tools are installed only by `ug configure`, never by agent launch
commands. Use `--enable-databricks-ai-tools` or `--disable-databricks-ai-tools`
with `ug configure` to control installation.

## Claude Routing Plugin

Smart routing passes generated agents through a per-launch `--plugin-dir`,
alongside `--settings`, without persistent plugin registration. One temporary
directory holds the settings, socket, and plugin and is removed when the launch
finishes or fails. Existing hook configuration and disable/revert behavior are
unchanged. Native daemon/background propagation of the plugin remains unverified.

The routing notice names the generated agent selected for launch and its model.
Claude Code's own task header can still show the agent type the parent originally
requested. That name may contain a different model; it is not the routing result.

The local `~/.ucode/<agent>-smart-routing-decisions.jsonl` logs also retain the
candidate models, their harnesses, and the router name sent with each recorded
decision. Feedback investigations can compare the selection with that original
menu without querying the router again.

Claude and Codex routing reasons summarize the task profile in plain language, such as
"This task has a clear target and limited scope." A separate sentence explains when
model availability requires a higher tier. Custom gateway notices are retained, and
older or unrecognized responses keep their original wording. Diagnostic records keep
the displayed reason in `rationale` and the gateway's full explanation in `raw_rationale`.

On Windows, Claude smart routing uses subagent hooks only. If first-prompt routing
is enabled, ug warns and falls back to subagent routing because the first-prompt
wrapper requires a Unix terminal.
The generated shell hooks expect Git Bash; PowerShell-only setups are not covered.

`--enable-smart-routing` and a workspace config with `smart_routing.enabled` turn routing on
through `SMART_ROUTER_CONFIG_VERSION`: they keep a known version and otherwise select
`subagent_orch_v0`, which routes subagents and turns on Smart Router Orchestrator while the
main session stays on its default model.
Like any version, it overrides individually set routing env vars. `--disable-smart-routing`
turns routing off for the launch, including over a workspace config.

### Smart Router Orchestrator

Use `SMART_ROUTER_CONFIG_VERSION` at launch to select a smart-routing configuration.
Each version defines separate Claude Code and Codex flag mappings; the existing versions
use identical mappings for both agents. The Claude-only version disables routing for Codex:

| Version | Routed agents | Subagent routing | First-prompt routing | Orchestrator |
| --- | --- | --- | --- | --- |
| `first_prompt_and_subagent_no_orch_v0` | Claude, Codex | On | On | Off |
| `subagent_only_v0` | Claude, Codex | On | Off | Off |
| `subagent_only_v1` | Claude, Codex | On | Off | Off |
| `subagent_orch_v0` | Claude, Codex | On | Off | On |
| `subagent_orch_v1` | Claude, Codex | On | Off | On |
| `subagent_orch_v0_claude_only` | Claude | On | Off | On |

`first_prompt_and_subagent_no_orch_v0` routes the first prompt and subagents without
orchestration: `ENABLE_SMART_ROUTING_V2=1`, `ENABLE_SMART_ROUTING_SUBAGENT_ONLY=0`, and
`ENABLE_SMART_ROUTER_ORCHESTRATOR=0`. Every other version routes subagents only;
`subagent_only_v1` and `subagent_orch_v1` route the same way as their `_v0` versions.

`subagent_orch_v0_claude_only` keeps Claude's `subagent_orch_v0` flags (`0`, `1`, `1`)
and sets all three flags to `"0"` for Codex. Codex uses its ordinary launch and
managed default model, without routing or orchestration. `--enable-smart-routing` and
workspace smart routing keep this version, so Codex stays unrouted.

`SMART_ROUTER_NAME` still selects the router independently of the preset.

Smart-routed Claude and Codex sessions install `smart-router`. The `subagent_orch_v0`
and `subagent_orch_v1` versions also install and activate the bundled `smart-router-orchestrator` skill,
as do `--enable-smart-routing` and workspace smart routing when they select `subagent_orch_v0`.
For example:

```bash
SMART_ROUTER_CONFIG_VERSION=subagent_orch_v0 ug claude
```

The version takes precedence over conflicting legacy flags. Once the agent is selected,
UG expands its mapping into
`ENABLE_SMART_ROUTING_V2`, `ENABLE_SMART_ROUTING_SUBAGENT_ONLY`, and
`ENABLE_SMART_ROUTER_ORCHESTRATOR` for the launched session. When the version is
unset or empty, these legacy flags retain their existing behavior, including
first-prompt routing through `ENABLE_SMART_ROUTING_V2=1`. Unknown versions are ignored:
no preset is applied, the inherited environment is unchanged, and commands continue normally.
`--disable-smart-routing` and session on/off controls apply after expansion;
`--enable-smart-routing` and workspace smart routing keep a known version.
Orchestration is off by default unless `--enable-smart-routing` or workspace smart routing
selects `subagent_orch_v0`.

Version names require an explicit suffix. Future revisions use new `_v1`, `_v2`,
etc. names without changing existing versions.

Version definitions require both Claude and Codex mappings and fail validation at module import if any flag in
`SMART_ROUTING_ENV_KEYS` is missing, has a value other than `"0"` or `"1"`,
or an unknown flag is present. Register new managed flags in that tuple and
explicitly set them for both agents in every version.

Use `ug codex` in the same command for Codex. Smart Router Orchestrator assigns bounded work
to explorer, researcher, worker, tester, and reviewer roles while the root plans,
integrates, and verifies results. Easy tasks and explicit requests not to delegate
stay in the root.

Claude and Codex routing panels add `[orchestrator on]` to the `Subagent` line
only when orchestration is active. When it is off, they show the normal subagent
name. The label reports the session mode; it does not identify whether a particular
delegation came from the workflow or an explicit user request.

Once opted in, orchestration follows the existing smart-routing launch eligibility
and session controls. Turning Smart Router off through its skill stops new automatic delegation;
turning it on restores orchestration only in opted-in sessions. Explicit user
requests for subagents still use normal harness behavior while routing is off.
Stored skill files do not activate orchestration without an opted-in configuration,
or in non-routed sessions. Existing Isaac pilot gating
and UG launch exclusions still apply.

Hooks refresh orchestration state before each prompt and after compaction. A
state change made outside the conversation is observed at the next hook; model
routing still checks the controls for each subagent.

UG supplies its own hooks; Codex combines them with existing hooks and applies
project trust. Smart routing selects subagent models; separate role-model
preferences are ignored and their files are left untouched. See the bundled
[Smart Router Orchestrator documentation](skills/smart-router-orchestrator/README.md) for details.

## Managed Files

`ug` backs up files before overwriting them. `ug revert` restores backups.

| Tool | Managed files |
|------|---------------|
| Codex | `~/.codex/ucode.config.toml`, shared catalog reference in `~/.codex/config.toml`, `~/.ucode/codex-model-catalog.json`, `/etc/codex/managed_config.toml` (Linux and macOS), `C:\ProgramData\OpenAI\Codex\requirements.toml` (Windows, written only from an elevated terminal; enforced by Codex 0.156.0 and later) |
| Claude Code | `~/.claude/ucode-settings.json`, `~/.claude.json`, `/etc/claude-code/managed-settings.json` (Linux), `/Library/Application Support/ClaudeCode/managed-settings.json` (macOS), `C:\Program Files\ClaudeCode\managed-settings.json` (Windows, written only from an elevated terminal) |
| Gemini CLI | `~/.gemini/ucode.env`, `~/.ucode/.gemini-home/.gemini/settings.json` |
| OpenCode | `~/.ucode/opencode-xdg/opencode/opencode.json`, `~/.ucode/opencode-xdg/opencode/plugin/ucode-auth.js` |
| GitHub Copilot CLI | `~/.copilot/ucode.env`, `~/.copilot/ucode-mcp-config.json` |
| Pi | `~/.ucode/pi-home/.pi/agent/models.json`, `~/.ucode/pi-home/.pi/agent/settings.json` |
| Cursor Agent | `~/.cursor/mcp.json` |
| Unity Gateway | `/etc/unitygateway/managed-settings.json` (Linux and macOS), `~/.ucode/refresh_ts.json`, `~/.ucode/managed-state.json`, `~/.ucode/managed-backups/` |

Workspace policy is root-owned but readable without sudo. Configure installs it only when the
workspace publishes a policy; launches check for updates every five minutes. Launches never
write workspace policy, even as root: they warn about changes and keep the installed policy
until `ug configure` (by an administrator or an IT provisioning run) updates it. Each distinct pending change warns once.
The installed workspace is authoritative: selecting a different one fails launch instead of
falling back to unmanaged behavior.
Refresh timestamps and pending-change hashes live in user-owned `~/.ucode/refresh_ts.json`,
not in the protected policy file.
This policy is separate from agent-file backups and is not removed by `ug revert`.
See [managed workspace policy](docs/managed-workspace-policy.md) for permissions and trade-offs.
Managed policy headers must not contain secrets: the policy is readable by all local users.

### Codex desktop ChatGPT relay

For a relayed OpenAI provider, `ug configure --agents codex --provider <mps>` also points the Codex
app's `~/.codex/config.toml` at a loopback relay served by the same login service below (plain
`codex` then uses it too). Restart the app and sign in with ChatGPT. A provider you already set is
left alone; `ug revert` removes ug's entries.

### Codex desktop tracing

With workspace tracing enabled, run `ug configure --agents codex` on the machine
where Codex desktop's app server runs, then restart the app server once. This points
Codex's trace exporter in `~/.codex/config.toml` at the existing CLI OTEL proxy, run
as a per-user service that starts at login; `ug codex` sessions keep their own proxy.
An exporter you already set is left alone. Disabling tracing or `ug revert` removes both.

| OS | Service | Diagnostics |
|----|---------|-------------|
| Linux | systemd user service `ug-codex-otel.service` | `journalctl --user -u ug-codex-otel.service` |
| macOS | LaunchAgent `com.databricks.ug-codex-otel` | `launchctl print gui/$(id -u)/com.databricks.ug-codex-otel` |
| Windows | Scheduled task `com.databricks.ug-codex-otel` | `schtasks /Query /TN com.databricks.ug-codex-otel /V /FO LIST` |

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

To add a new CLI agent, implement `src/ucode/agents/<name>.py`, register it in
`src/ucode/agents/__init__.py`, and add focused tests.
Desktop-app adapters live separately in `src/ucode/agents/desktop/`.
Desktop rendering consumes model IDs and headers from shared discovery/managed-config
parsing without revalidating or deduplicating them.
Claude Desktop profiles use Anthropic's [documented application-data locations](https://claude.com/docs/third-party/claude-desktop/data-storage#where-data-lives)
on macOS, Windows, and Linux, with no profile-directory override. Windows and Linux require the app to have been opened once;
their native GUI, credential-helper, and inference flows still need validation.
Profiles are named `Unity Gateway - <CLI profile name>` and use one reserved UUID:
`5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6`. Configure replaces that whole profile,
updates its metadata entry, and selects it. Switching workspaces replaces the same file.
Revert deletes that profile and entry, preserving unrelated profiles and metadata.
Only the previous selection is recorded in `~/.ucode/claude-desktop.json`;
no original profile contents are saved for revert. Old random-ID and workspace-derived
profiles are left untouched. A valid JSON `_comment` field warns against manual edits;
native Desktop acceptance of that field still needs a smoke test.
Workspace-specific UUID normalization is removed; existing CLI URL handling is unchanged.

Claude Desktop profile updates wait up to five seconds for the file lock across
platforms. A timeout leaves the operation holding the lock undisturbed. The Desktop
auth helper retains its existing shared deadline.

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
