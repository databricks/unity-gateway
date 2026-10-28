# Claude Desktop through Unity Gateway

After Claude Code is successfully configured, `ug configure` also creates and
selects a `Unity Gateway - <CLI profile name>` profile for Claude Desktop on macOS,
Windows, and Linux. This uses
the same workspace, Databricks CLI OAuth profile, model source, and managed HTTP
headers as Claude Code. There is no separate Desktop checkbox or managed field.

## Requirements and setup

- Claude Desktop installed on macOS, Windows, or Linux. On Windows/Linux, open it
  once to initialize application data.
- A named Databricks CLI OAuth profile with gateway/model access. A bearer token
  alone is not enough for Desktop's credential helper.
- At least one Claude-named model in the configured static list or gateway catalog.
- For an Anthropic subscription relay, Claude Code installed and signed in on the
  same OS user account.

Quit Claude Desktop, configure Claude Code, then reopen Desktop normally:

```bash
ug configure --profile <your-profile> --agents claude
```

No terminal process needs to stay running. UG does not install Desktop or manage
the customer's MDM process. Explicit-agent, selected-agent, and managed configure
flows all set up Desktop after successful Claude Code configuration. Ordinary agent
launches and `ug agents add` do not configure Desktop.

## Cowork workspace skills and MCP servers

Whenever Claude is configured, `ug configure` also installs a generated
`ug@unity-gateway-cowork` plugin into that account's Cowork scope. This applies
to managed and unmanaged workspaces. Start one Cowork session before configuring so Desktop
creates its active account/organization directory. If several Cowork profiles exist and UG
cannot identify one active profile, setup warns without changing any of them.

The plugin contains the current workspace's Claude-targeted MCP servers and downloaded
user-level skills, plus any managed MCP servers and skills. Add skills with `ug skills add`
(picker, `--names`, or `--location`); omit `--path` to make them available in Cowork.
Project-level, user-authored, unmanaged Databricks AI Tools, and other-workspace skills
are not copied. `ug configure --mcp`, `ug mcp add`, and `ug mcp remove` resync the plugin,
as do downloaded skill additions and removals. Start a new Cowork session
to use the updated skills. MCP entries run the absolute
`ug mcp-proxy` executable with the selected workspace and CLI profile; no bearer token is
stored in the plugin. Saved PAT mode is supported. Custom OAuth is not yet supported for
Cowork MCP servers.

The plugin's name and display name are `ug`, shortening the connector prefix to
`ug plugin:`. Reconfigure installs the new plugin before uninstalling the old
`unity-gateway@unity-gateway-cowork` registration, if present. This also changes
the tool namespace from `mcp__plugin_unity-gateway_...` to `mcp__plugin_ug_...`.

Cowork shortens generated UC connector names, for example `system-ai-slack` becomes
`slack`. If service names collide, aliases include their catalog/schema, such as
`slack-system-ai` and `slack-main-team`; numeric suffixes avoid conflicts with custom
names. Custom registration names and full proxy URLs are preserved. Reconfigure and
start a new Cowork session to refresh connector and tool names.

UG owns these local files:

- Generated marketplace: `~/.ucode/claude-cowork-marketplace`
- Selected Cowork root record: `~/.ucode/claude-cowork.json`

Start a new Cowork session after configure. Plugin MCP servers connect asynchronously and
may take a few seconds to appear, so a tool can be absent on the first model turn. Deferred
tool search or a retry after another tool call should find it. The resulting tool name follows
Claude's `mcp__plugin_<plugin>_<server>__<tool>` pattern. When the plugin has MCP servers, UG
also generates a `unity-gateway-connectors` skill whose description names them, so Claude knows
the connectors exist on the first turn instead of asking whether they are connected.

Connector sign-in opens the workspace's MCP service sign-in page. Sign in with the same
Databricks account as the selected CLI profile, then complete the provider flow. The proxy
checks that profile user's credential for up to five minutes and retries the original request
when it is active. Browser launch alone does not establish success. If the browser or provider
popup does not open, follow the URL in the MCP server log and click Login. On timeout, complete
sign-in and retry authentication in the agent. This flow does not run CLI `auth login --resource`
or replace the workspace session.

Cowork plugin support can still be disabled by Anthropic account feature flags or managed
Desktop policy. UG treats installation as best-effort: failure warns but does not fail Claude
Code setup. If the current workspace has no Claude-targeted MCP servers or managed skills, UG
keeps the plugin installed but replaces its contents with an empty plugin so a prior workspace's
tools and skills do not remain active. If Cowork has never been started and UG has no prior plugin
state, setup skips silently. Only `ug revert` removes the plugin registration and generated
marketplace. The native Desktop gateway profile is separate and remains under the cleanup rules
below.

## Models and authentication

UG uses an admin's static model list when supplied. Otherwise it obtains the
Anthropic-compatible gateway catalog, scoped to the same provider service or
Unity Catalog location when applicable. For Desktop only, either source is filtered
with case-insensitive `\bclaude-`: IDs such as `system.ai.claude-sonnet-5`,
`databricks-claude-opus-5`, and `us.anthropic.claude-3-7-sonnet-20250219-v1:0`
are retained; GPT/OSS IDs and their `anthropic-aigw-*` aliases are excluded unless
their names also match. Retained IDs and their order are unchanged. This is a name
filter, not verification of the underlying model-service destinations.

The filtered list is written into the profile with Desktop's model discovery
disabled. It refreshes when `ug configure` runs again, not when the app opens or
on a timer. If no Claude-named models remain, setup warns without changing Desktop
files. Shared discovery and Claude Code's model list are unchanged.

When the catalog provides a display name, UG writes it as
[`labelOverride`](https://claude.com/docs/third-party/claude-desktop/configuration#models).
For example, `system.ai.claude-sonnet-5` can appear as `Claude Sonnet 5` in the
picker. The `name` field remains the exact model ID sent to the gateway. Missing
or empty display names keep ID-only entries. For admin static lists, UG fetches
the catalog only to attach matching display names; the admin's model IDs and
order remain authoritative. Labels refresh on `ug configure` alongside the
model list.

The profile uses a credential helper bound to the Python installation running UG.
It stores only the workspace/profile name and command arguments, never a bearer
token. The helper asks the Databricks CLI for a cached or silently refreshed token.
If authentication is missing, it opens browser login only when Desktop supplies
`CLAUDE_HELPER_CONTEXT=interactive`. Other contexts fail with instructions to start
a new Cowork task. Browser login has a bounded timeout and concurrent sign-ins are
serialized. If the original message failed during login, resend it.

For an Anthropic subscription relay, the same helper returns the Claude subscription
token as Desktop's bearer token and the Databricks token in
`X-Databricks-AI-Gateway-Token`. On macOS it reads the Claude credential from Keychain.
On Linux and Windows it reads `.claude/.credentials.json` from the user's home directory,
or `.credentials.json` under `CLAUDE_CONFIG_DIR` when set; macOS selects the matching custom
Keychain entry. An expired or soon-to-expire token first runs `claude auth status --json` so
Claude Code can use its refresh token without opening a browser. If silent refresh does not
produce a valid token, browser login remains limited to an interactive helper context. Claude
refresh and login share the helper time budget and lock. The profile stores neither credential,
and no proxy or background service is needed.

## OpenTelemetry tracing

When the managed coding-agent configuration enables Claude tracing, `ug configure`
also enables Desktop's [native OTLP export](https://claude.com/docs/third-party/claude-desktop/configuration#otlp).
The profile sets:

```json
{
  "otlpEndpoint": "https://<workspace>/ai-gateway/otel",
  "otlpProtocol": "http/protobuf",
  "otlpHeadersHelper": "<absolute-path-to-UG-headers-helper>",
  "otlpTracesEnabled": true
}
```

Desktop appends `/v1/traces` to the collector base URL. UG creates
`~/.ucode/claude-desktop-otel-headers.sh` on macOS/Linux or
`%USERPROFILE%\.ucode\claude-desktop-otel-headers.cmd` on Windows. This small wrapper
runs the configuring Python installation's `ug otel-headers` implementation with
the explicit workspace and CLI profile. It prints only a JSON `Authorization`
header, using the existing cached-token/silent-refresh mechanism. Neither the
profile nor wrapper stores a token. No proxy or background service is needed;
Codex's Linux-only OTEL service is unchanged.
The Windows wrapper selects UTF-8 and preserves literal arguments, including
non-ASCII profile names and trailing backslashes.

Requirements: Desktop must support `otlpTracesEnabled` (added in 1.22209.0) and
`otlpHeadersHelper` (added in 1.30096.1), Claude tracing must be enabled in the managed
configuration, and the workspace must accept OTLP trace ingestion with the user's
credentials. Disable Claude tracing and rerun configure to remove the OTLP keys
and helper; `ug revert` also deletes the helper and UG profile. Writes and rollback
cover the wrapper alongside the profile and selection files. Other profiles remain
untouched. If Desktop's application-data directory has already been removed,
revert still deletes the helper and previous-selection state without recreating
Desktop directories.

Restart Desktop and start a new Cowork task or Code session after configuring.
Code and Cowork sessions receive the headers helper and rerun it as tokens rotate,
rather than retaining an inference token copied at session start. Desktop caches
helper output for a few minutes, so rotation is not immediate. The telemetry helper
does not open browser login; if silent refresh fails, reauthenticate through Desktop's
inference sign-in or the Databricks CLI. Claude documents that helper failures can
send telemetry without authentication; check Desktop's logs. UG does not enable
raw-content capture. Desktop can also attempt logs and metrics exports to the collector base;
the gateway currently accepts only `/v1/traces`, not `/v1/logs` or `/v1/metrics`.

Component tests verify the generated configuration and lifecycle, not native export
or ingestion. To validate natively, configure a tracing-enabled workspace, restart
Desktop, complete a small Cowork task, then confirm that its session spans reached
the workspace's configured trace table. Check Desktop logs for authentication or
trace-export errors, and verify further spans from the same open Code session after
token refresh and the helper cache interval. Do not share
unredacted credentials or diagnostic bundles.

## Existing profiles and rollback

Profiles use Anthropic's [documented locations](https://claude.com/docs/third-party/claude-desktop/data-storage#where-data-lives):

- macOS: `~/Library/Application Support/Claude-3p/configLibrary`
- Windows: `%LOCALAPPDATA%\Claude-3p\configLibrary`
- Linux: `~/.config/Claude-3p/configLibrary`

Older Windows releases used `%APPDATA%\Claude-3p`; update and open Desktop to let
the app migrate its data before configuring. UG does not move app data.

UG reserves one profile UUID: `5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6`.
UG supports one configured workspace at a time, so switching workspaces replaces
the same profile rather than creating another one. The display name remains
`Unity Gateway - <CLI profile name>`.

Each `ug configure` replaces the entire reserved profile and updates its metadata
entry and selection, whether or not the file or previous UG state exists.
Unknown fields, manual edits, and omitted optional fields in that profile are not
preserved. This profile is fully managed by UG; do not edit it manually. UG writes
only supported Desktop configuration keys: unknown keys such as `_comment` trigger
a Desktop warning. Reconfigure removes that field from profiles generated by older builds.

`ug revert` deletes the reserved profile and its metadata entry, even without
previous UG state. No previous version of that file is restored. Other profiles,
their entries, and unrelated metadata are left intact. If UG is still selected,
revert restores the previous selection only when its profile and entry still exist;
otherwise it clears the selection. A later selection of another profile is preserved.
Only this previous-selection ID is saved in `~/.ucode/claude-desktop.json`.
Repeated configure retains it while UG remains selected.

Revert also uninstalls `ug@unity-gateway-cowork` and any legacy
`unity-gateway@unity-gateway-cowork` registration, removes their marketplace
registration, and deletes UG's generated marketplace. This removes the MCP connectors and
skills delivered by that plugin. It also cleans up an exact UG registration left in another
Cowork profile by an older failed revert. Unrelated Desktop plugins, connectors, and skills are
left untouched.

The revert summary reports `Claude Desktop profile: removed`, `unchanged`, or `failed`.

Old random-ID and workspace-derived profiles from earlier builds are left untouched;
there is no automatic migration or cleanup. Workspace-specific UUID normalization
is no longer needed; normal CLI URL handling is unchanged.

Files are replaced atomically one at a time with best-effort rollback of their exact
previous bytes on write failure. This is not a crash-atomic transaction across files.
Workspace and Cowork cleanup state is retained if Desktop revert fails, so cleanup can be
retried with `ug revert`. The generated Cowork marketplace is also retained until its plugin
and marketplace registrations are removed successfully. A Desktop cleanup failure produces
one warning and exit code 1; the summary reports the failed cleanup.
Profile configure/revert waits at most five seconds for its file lock. A timeout
does not interrupt the holder or change profile, metadata, or selection-state files.

Desktop setup failures only warn: successful Claude Code configuration still returns exit 0.
`--dry-run` performs no Desktop or Cowork discovery, writes, or plugin commands. Removing Claude
from managed configuration preserves both the Cowork plugin and a previously written native
Desktop profile until `ug revert`. Desktop does not run UG's launch-time managed-agent checks;
gateway authorization remains the enforcement boundary.

## Validation limits

The native profile format comes from a working macOS Claude Desktop/Cowork
2.19675.0 export. Tests cover configuration, profile cleanup, helper behavior, and CLI
orchestration; they do not prove GUI inference on every app version.

- Windows and Linux profile paths follow the documented shared `configLibrary`
  layout. Component tests exercise filesystem configure/refresh/revert using all
  three platform selections; they do not establish native GUI, helper execution,
  browser-login, or inference coverage on Windows or Linux.
- Anthropic's [Linux Desktop beta](https://code.claude.com/docs/en/desktop-linux)
  supports Ubuntu 22.04+ and Debian 12+. Cowork additionally requires QEMU/KVM.
- A profile selection is not proof that a fresh ordinary installation has switched
  to third-party inference mode. Fresh-install activation remains a native test gate.
- UG does not configure Desktop's default-model selection.
  Inheriting a particular Claude Code default needs further native evidence; UG
  does not guess a field or claim that list order selects the default.
- Inspected Desktop code rejects model IDs containing other vendors' model names,
  including `glm` and `kimi`, before inference. Gateway protocol compatibility does
  not remove this client restriction. UG filters Desktop's catalog to Claude-named
  models without renaming them; that name filter does not reproduce every native
  validation rule.
- Custom OAuth/PAT profiles, Desktop Smart Routing, and third-party connectors outside
  UG's generated Cowork plugin are not implemented here. Cowork's MCP proxy supports saved
  PAT mode but not custom OAuth. Native OTEL settings are generated, but native export and
  ingestion remain unverified; session auth limits are described above.
- Anthropic subscription relay has native macOS proof-of-concept validation. Windows and
  Linux credential-file handling has component coverage, but native Desktop login, refresh,
  and inference remain rollout gates.
- Observed inference applies to Cowork. Ordinary Chat is not claimed to use the
  same gateway path.

Before native rollout on Windows and Linux, verify that the app reads the generated
profile after restart, completes a gateway task, refreshes credentials silently,
opens browser login when interactive recovery is needed, and preserves unrelated
settings through repeat configure and revert. Run these checks on the supported
app version and packaging used by your organization.

Browser connector recovery requires the workspace's `/mcp-service-login` page and
`/api/2.1/unity-catalog/mcp-services/<service>/user-credentials` API. A credential already
marked ACTIVE before login must change (for example its expiry metadata), or leave ACTIVE
and return, before UG confirms reauthentication. PROVISIONING, UPDATING, and DELETING
continue polling. Missing services fail before launching a browser; a credential 404
means the selected user has not signed in yet and continues polling until the timeout.

Automated coverage executes the helper command from the generated profile and checks its
credential against the gateway models API using CI's supplied bearer. Separate subprocess
component tests exercise the generated MCP command with controlled HTTP/browser boundaries;
the MCP fixture CUJ also checks discovery and tool calls against a real workspace. Both CUJ3
and the Desktop integration journey read commands from a Cowork plugin installed by the real
Claude CLI and require fresh receipts from both live fixture tools. The Desktop case runs in
dedicated Linux/Windows jobs against CUJ3's workspace. Separate relay and non-relayed Anthropic
MPS journeys use the generated helper credentials and profile headers for real Messages requests.
These checks do not launch Desktop or validate native OAuth consent, skill use, or app reconnects.
