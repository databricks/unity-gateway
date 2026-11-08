# Claude Desktop through Unity Gateway

For macOS fleet provisioning, use
[`ug configure --agents claude --global`](claude-desktop-mdm.md).
The behavior below describes configure without a scope flag or with `--user`.
Explicit `--global` uses the macOS MDM path and leaves Desktop's user files unchanged;
on Windows and Linux it skips Desktop.

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

## Import existing projects and chats

UG enables Claude Desktop's import wizard with `claudeAiImport.enabled: true`.
After configuring and reopening Desktop, go to **Settings → Import & export → Import…**.
Sign in to claude.ai and fetch your export, or choose a downloaded export ZIP,
then review and import. For Team or Enterprise workspaces, a workspace owner must
enable member data export first.

Projects, chats, and custom project instructions are copied locally. Accept imported
instructions before using them. Personal/member exports omit uploaded project knowledge
files and conversation attachments; add those separately. If an imported project has a
folder, add it again through **Context → + → Choose a folder → Choose a different folder**
and start a new chat to let Claude read it.

Import is a one-time copy that you start explicitly. Original data stays intact,
and later activity on claude.ai requires another import. See Anthropic's
[import guide](https://claude.com/docs/third-party/claude-desktop/import) for details.

## Code tab connector guidance

`ug configure` also installs `ug@unity-gateway-code` in the regular Claude Code
plugin scope. The Code tab and terminal Claude Code load its
`unity-gateway-connectors` skill, with Code-specific discovery and authentication
instructions. Cowork's generated skill text is unchanged. Start a new Code session
after configuring.

Both variants use the skill name `unity-gateway-connectors`. Their descriptions and
instructions live in packaged Markdown templates under
`src/ucode/agents/desktop/connector_skills/`: `code.md` and `cowork.md`.
Generation inserts the current connector names and bounds the discovery description.

Before the first connector call, the skill checks current connection/authentication
status; loaded tool definitions do not establish that sign-in is complete. If the
client exposes no status, it uses the installed Claude CLI's `mcp list`, keeping the
session's working directory and `CLAUDE_CONFIG_DIR`. A current sign-in requirement
takes priority even when service tools are already visible.

For proxy connections, Claude uses the discovered `authenticate` tool. Native HTTP
OAuth may expose no authentication tool: the skill checks CLI support, then uses
`claude mcp login <registered-name>` with the exact native server name and relays
browser/callback instructions. After confirmed success it rediscovers tools and
resumes the request. Failed or canceled sign-in stops service calls. If login is
unavailable or needs user interaction, Claude explains the blocker and points to
that server's Authenticate button when Code displays one. The button is a client
action, not an MCP tool. The terminal Claude Code fallback is
`/mcp` → select the server → Authenticate. That is an interactive client command,
not a shell command or an assumed Desktop menu.
An operation stuck at “Waiting for the result” alone does not establish an auth
failure and must not trigger duplicate calls or repeated login attempts.

Code already reads the MCP servers registered by UG in its native configuration,
and downloaded skills from its own skill directory. Its UG plugin contains only
the generated connector guidance, using the original MCP registration names such
as `system-ai-slack`. It does not add another copy of the servers, downloaded
skills, or Cowork's session hooks.

The generated marketplace lives at `~/.ucode/claude-code-marketplace`;
`~/.ucode/claude-code-plugin.json` records its installation scope for cleanup.
Installation uses `CLAUDE_CONFIG_DIR` when set, otherwise `~/.claude`; it does not require a
Cowork session or account directory. Configure and MCP additions/removals refresh
the guidance. Removing the last MCP leaves an empty plugin so stale connector names
are no longer advertised. `ug revert` removes this plugin, its marketplace
registration, and generated source while preserving unrelated plugins.
When the Code config directory changes, installation in the new directory must succeed
before UG removes the old registration or updates the saved root. A failed installation
keeps the old registration and saved root available for cleanup or retry.

MCP-only Claude setup (`ug mcp add --agents claude`) is also eligible for Code
guidance without model setup. During managed configure, successful MCP reconciliation
refreshes Code even if skill downloads fail. Native Desktop connectors also sync independently
of skill-download success after model setup; failed downloads leave the existing Cowork skill
plugin intact. Failed MCP reconciliation preserves both plugins and the native connector
list for the same workspace. Desktop uses the CLI's resolved MCP list: successful refreshes
apply additions, updates, and removals; failed discovery keeps the previous list. Configure
does not test live connector connections. Switching workspaces does not retain the old
workspace's connectors.

Code and Cowork plugin installation are independent and best-effort. A Cowork
setup failure does not block the Code plugin or native MCP configuration. The
component checks do not establish Desktop GUI discovery or successful connector
calls; validate those in a fresh Code session.

The opt-in `tests/connector_behavior/` suite includes Code requests with cached Gmail
tools and native HTTP/proxy authentication success or cancellation. It uses a real
Claude model with synthetic connector tools and CLI results; this measures adherence
to the generated guidance, not Desktop browser sign-in or live Gmail access.

## Cowork workspace skills and MCP servers

Default `ug configure` and `ug configure --user` both write only user-level Desktop
files: the 3P JSON profile, generated marketplace at `~/.ucode/claude-cowork-marketplace`,
and the normal Cowork registration/cache. This applies even when UG's saved configuration
scope is `global`. Neither path publishes or removes a root organization plugin or managed plist.
Claude Code and other CLI agents retain their own scope handling.

Explicit `--global` skips these per-user Desktop writers. Machine policy, shared-runtime
bindings, connectors in the managed plist, and a root organization plugin use the
[macOS MDM deployment path](claude-desktop-mdm.md). It uses these machine locations:

- Policy: `/Library/Managed Preferences/com.anthropic.claudefordesktop.plist`
- Skills/hooks plugin: `/Library/Application Support/Claude/org-plugins/ug`

Ordinary configure uses `claude plugin ... --cowork --json` with one
`ug@unity-gateway-cowork` registration for the selected account/org. If an earlier UG
installation registered a root source, configure switches that registration to the user
source without deleting the root files or creating a second plugin. Other plugins are preserved.

Start one Cowork task before configuring so Desktop creates its account/org directory.
If Cowork has not created its sessions directory, first-time ordinary configure skips the
plugin. Run configure again once the account directory exists.
Quit Desktop, run configure as your normal user, then reopen it and start a new task.
Ambiguous profiles warn without changes. Desktop's user-level writers need no elevation;
other requested agents may still require it.

The source contains `.claude-plugin/marketplace.json`, and under `plugins/ug` the
plugin manifest, `hooks/hooks.json`, and included `skills/` assets. MCP declarations
live in the user 3P profile, not the plugin.
The plugin version changes with its contents. Configure explicitly updates the marketplace
and installs or updates the user-cached plugin: changing source files alone is insufficient.
The 3P connection profile, telemetry helper, runtime authentication, and UG state remain
user-local. An installed `Claude/org-plugins/ug` organization plugin blocks user plugin
installation to avoid duplicates. Remove the managed deployment through its owner before
switching to the user plugin.

UG records the account/org in `~/.ucode/claude-cowork.json`.
UG owns the generated user marketplace: configure replaces its contents, including manual
edits, and ordinary `ug revert` removes it and UG's user registration. Root files are left
alone. The existing user-source writer retains staged replacement and rollback.
A native refresh failure leaves the user source and tracking state for retry or revert.
After a successful plugin update, failure to remove the legacy UG plugin only warns:
the updated plugin and native connectors remain configured. Run `ug configure` again
to retry cleanup; the old plugin may remain installed until cleanup succeeds.
If a source switch fails, UG attempts to restore the previous marketplace/plugin through
native Claude commands. A failed restoration reports instructions to repair the installation.
Malformed directory paths are ignored during UG's source inspection. The native
Claude CLI still validates its registry. If it reports corruption, restore the
account's `cowork_plugins/known_marketplaces.json` from a backup before retrying;
UG does not rewrite a corrupted native registry automatically.

Run configure and revert one at a time. Account changes use the existing cleanup flow
before installing for the new account; no pending-cleanup queue or state migration is added.

Root bundle publication and its privileged writer belong to the
[MDM follow-up](https://github.com/databricks/unity-gateway/pull/1374).
Ordinary Desktop configuration writes only the user source.

Manual testing on macOS with Claude Desktop 4.4212.0 previously verified configure and
reconfigure, skill invocation, connector calls, matching source/cache contents, and one UG
registration without duplicates. Those tests used the earlier user/root publication flow.
Skills and connector calls also worked with the user marketplace source temporarily moved
aside; the installed cache and authentication remained in place. The new default user-only
scope has component coverage but has not been rerun in native Desktop.

The UG Desktop profile declares the current workspace's Claude-targeted MCP servers
in `managedMcpServers`. The plugin contains downloaded user-level and managed skills,
connector guidance, and session hooks. Add skills with `ug skills add`
(picker, `--names`, or `--location`); omit `--path` to make them available in Cowork.
Project-level, user-authored, unmanaged Databricks AI Tools, and other-workspace skills
are not copied. `ug configure --mcp`, `ug mcp add`, and `ug mcp remove` resync the profile and plugin,
as do downloaded skill additions and removals. Start a new Cowork session
to use the updated skills. MCP entries run the absolute
`ug mcp-proxy` executable with the selected workspace and CLI profile; no bearer token is
stored in the profile. Proxy authentication is unchanged. A UG Desktop profile for the
same workspace must exist before connector registration; native profile setup still requires
a Databricks CLI OAuth profile. Custom OAuth is not yet supported for Cowork MCP servers.

Reconfigure removes MCP declarations from the generated plugin and registers them in
the profile instead. Skills and hooks remain in `ug@unity-gateway-cowork`; the old
`unity-gateway@unity-gateway-cowork` registration is removed if present. Restart Desktop
and start a new task after migrating. Use the tool names exposed by Desktop; old
plugin-prefixed tool names are no longer authoritative.

Cowork shortens generated UC connector names, for example `system-ai-slack` becomes
`slack`. If service names collide, aliases include their catalog/schema, such as
`slack-system-ai` and `slack-main-team`; numeric suffixes avoid conflicts with custom
names. Custom registration names and full proxy URLs are preserved. Reconfigure and
start a new Cowork session to refresh connector and tool names.

UG owns these local files:

- User-scope generated marketplace: `~/.ucode/claude-cowork-marketplace`
- Installation/cleanup record: `~/.ucode/claude-cowork.json`

The profile enables [`toolSearchEnabled`](https://claude.com/docs/third-party/claude-desktop/configuration)
so Claude loads MCP tool definitions into context on demand. This does not promise lazy
server startup: the existing hooks still probe configured connectors. The gateway/provider
must support Anthropic's tool-search protocol; unsupported providers can reject requests.
Connector registration works before the first Cowork session; plugin installation still
requires an initialized Cowork account.

When connectors are configured, the plugin includes:

- Hooks running `ug cowork-hook`. They cannot make Cowork connect its own MCP servers
  sooner, and they never open a browser.
  - On `SessionStart`, the hook refreshes the profile's Databricks token, sends each upstream
    MCP server an `initialize` and `tools/list` (15 seconds total), and tells Claude which
    connectors exist. It does not report which ones answered, because that goes stale after
    the first turn. If a probe gets a sign-in error (`-32006`, `-32042`, or HTTP 401), the
    note adds a timestamped line telling Claude to call that connector's `authenticate` tool.
  - On `UserPromptSubmit`, the hook reads the session transcript. If Claude's last reply was
    under 3 minutes ago, it does nothing. After a longer idle gap it probes every connector
    (10 seconds total) and tells Claude that earlier "MCP servers disconnected" notices may
    be stale, which connectors answered or needed sign-in, and to check current tools before
    calling one unavailable.
- A `unity-gateway-connectors` skill. On every connector request, including after a break,
  Claude uses current tools or discovers them with `ToolSearch` when available. Seeing only
  `authenticate` does not trigger login: service tools may be deferred. A reported sign-in
  requirement triggers authentication. If discovery still finds no requested tools, Claude
  explains that they may still be initializing and asks the user to retry shortly. It does
  not sleep or poll for startup, promise recovery, or infer an authentication failure.

Desktop manages connector startup independently of the plugin; loading the plugin does not
guarantee every connector is ready. Idle recovery remains separate: the prompt hook refreshes
credentials and probes upstream servers, while the stdio proxy starts the local shared
backend if it has exited when Desktop reconnects. The prompt hook's probes can delay the
prompt for up to their 10-second budget, in addition to credential refresh.

Native Cowork execution of these hooks still needs Desktop validation.

The opt-in `tests/connector_behavior/` suite sends the generated skill to a real Claude model
with synthetic Slack tools. It checks a stale disconnect notice with tools already available,
deferred tools found through discovery without unnecessary login, missing tools prompting a
retry-later response without sleeps, and a reported sign-in requirement followed by login and
task completion. Available, deferred, and missing-tool cases run with both the skill description
alone and the skill loaded before the break. These tests measure model compliance; they do
not exercise Desktop's native tool exposure, sleep/resume, or proxy transport recovery. See
`tests/README.md` for the explicit workspace, profile, and model inputs.

The skill distinguishes configured connectors from authenticated connections. It directs
Claude to check exposed tools and use supported discovery actions before calling them, using
only the exact names exposed or returned. It forbids guessing tool names, repeating an unavailable
call, authenticating another connector, or spawning a subagent solely to make tools appear.
After successful sign-in, Claude rediscovers tools and continues the original request in the same
turn if its tools are available. Otherwise, it asks the user to retry shortly.
An unavailable tool name and permission denials alone do not establish an authentication failure.
When access is denied and an authentication tool is available, the guidance directs Claude
to attempt sign-in once before recommending administrator or network changes. This includes
IP-address denials: in a reported Atlassian session, the same Jira query succeeded after
fresh sign-in despite that error. If the denial persists after sign-in and retry, Claude
must report the original error and stop retrying authentication. This guidance does not
reclassify upstream permission/network errors or validate their root cause.

For OAuth U2M services, the proxy keeps a local `authenticate` tool alongside the service's
normal tools, including after successful login. Calling it starts a fresh login even if an
earlier login succeeded in the same session. When discovery reports an authentication error,
the proxy explicitly says "Sign-in is required" in that tool's description. A successful tool
listing or sign-in clears that requirement, so the persistent recovery tool alone does not
imply a need to log in. Discovery checks service metadata without
opening a browser or waiting for an interactive login already in progress. Service metadata
is looked up again on each discovery or authentication error, so failed requests can recover
on the next operation.
PAT and non-U2M services do not gain this action, and an upstream tool
named `authenticate` keeps its own behavior. Discovery auth errors (`-32006`) and login-URL
challenges (`-32042`) expose recovery, including after earlier login success. Login challenges
must target `/mcp-service-login` on the configured workspace with this service's exact name;
mixed challenges containing policy approval retain their upstream responses.

A tool call that returns such a login challenge, or a failed tool result containing that
service-scoped sign-in URL, triggers the existing browser-login flow and replays the original
call once. This handles Gmail/Calendar errors that otherwise tell Claude to show a link and
wait for the user. Automatic login is still limited to one attempt per proxy session, and
requires confirmed U2M metadata. Canceled or failed login and persistent errors are returned
to the client. Successful results, ordinary errors, and other URLs do not trigger this path.

Only an actual `tools/call` returning HTTP 401 can trigger connector login. Initialization,
tool discovery, notifications, and background HTTP requests never start connector sign-in;
persistent HTTP errors remain errors. PAT mode does not open connector login.
Databricks workspace login through the Desktop credential helper is separate from connector
login: restoring the workspace session must not sign in unrelated connectors.

The skill provides guidance; native recovery still depends on Claude discovering and using
the action when automatic recovery is unavailable. The proxy does not infer expired credentials
from generic error wording.

Connector sign-in opens the workspace's MCP service sign-in page. Sign in with the same
Databricks account as the selected CLI profile, then complete the provider flow. The proxy
checks that profile user's credential for up to five minutes and retries the original request
when it is active. Browser launch alone does not establish success. If the browser or provider
popup does not open, follow the URL in the MCP server log and click Login. On timeout, complete
sign-in and retry authentication in the agent. This flow does not run CLI `auth login --resource`
or replace the workspace session.

To validate recovery in native Cowork, start a fresh task with a connector that needs sign-in.
Request a real operation and confirm Claude discovers the sign-in action, prompts for browser
authentication, refreshes the tools, and completes the original operation without another user
message. Repeat after credential expiry and with canceled sign-in. Record the UG/Desktop
versions, configured proxy executable, and redacted discovery/tool errors for failures; exclude
tokens and private tool results. Generated-plugin component tests do not validate this flow.
Also test Gmail and Google Calendar with missing connection credentials: request mail or today's
events and confirm the browser opens and the original operation finishes without a second message.
To check workspace-login isolation, log out of the configured Databricks CLI session, start a
new Cowork task, and send an unrelated prompt such as "What is 2 + 2?". The credential helper
may restore Databricks sign-in, but no Atlassian or other connector login should open. Then
request Jira issues and verify that connector sign-in opens only if that operation needs it.
After sleep/wake or a connection-status notice, request another connector operation. Verify
Claude rediscovers the tools and tries a relevant call before claiming the connector is unavailable.


Cowork plugin support can still be disabled by Anthropic account feature flags or managed
Desktop policy. UG treats installation as best-effort: failure warns but does not fail Claude
Code setup. If the current workspace has no Claude-targeted MCP servers or managed skills, UG
keeps the plugin installed but replaces its contents with an empty plugin so a prior workspace's
tools and skills do not remain active. User-scope setup skips silently if Cowork has never been
started and UG has no prior plugin state. `ug revert` removes UG's registration and owned
marketplace sources through the cleanup described below. Quit and reopen Desktop after
revert. The native gateway profile has separate cleanup rules. This plugin is registered
in Cowork scope; Claude Code's existing configuration is unchanged.

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
or empty display names, or display names equal to the model ID, use UG's model-name formatter. For example,
`us.anthropic.claude-sonnet-4-5-20250929-v1:0` becomes `Claude Sonnet 4.5`.
Distinct model IDs with the same label show their full IDs to keep routes distinguishable.
For admin static lists, UG fetches the catalog only to attach matching display names;
fallback labels also work when discovery is unavailable. The admin's model IDs and
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

Each `ug configure` replaces the reserved profile and updates its metadata
entry and selection, whether or not the file or previous UG state exists.
Existing same-workspace connectors are retained until their replacement succeeds.
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

Ordinary revert also attempts macOS MDM cleanup through the existing sudo path. If it
cannot remove machine files, it warns and continues user cleanup; retry with sudo available.
It uninstalls
`ug@unity-gateway-cowork` and any legacy
`unity-gateway@unity-gateway-cowork` registration, removes their marketplace
registration, and deletes UG's generated marketplace. Deleting the reserved Desktop profile
removes its MCP registrations; plugin cleanup removes its skills and hooks.
It also cleans up an exact UG registration left in another
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
a warning and exit code 1; the summary reports the failed cleanup.
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
- Custom OAuth/PAT profiles, Desktop Smart Routing, and native HTTP/OAuth connectors that
  bypass UG's MCP proxy are not implemented here. Native OTEL settings are generated, but native export and
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

The generated connector skill keeps its discovery description within 1,024 characters;
the body lists every configured connector and carries sign-in recovery instructions.
Service metadata is looked up when needed, without caching or a retry cooldown.
Slow metadata discovery does not delay unrelated MCP responses.

Two automated Desktop journeys run in the existing Linux/Windows Claude integration jobs:
system.ai models and subscription-relay MPS. Each asserts the generated profile, executes its
credential helper, completes real Messages inference, installs Cowork through the Claude CLI,
and discovers/calls web_search using the profile's stdio registration to find Databricks' official website.
They also verify that the installed plugin contains no duplicate MCP declarations.
Both check repeated configuration, preservation of unrelated settings, and revert cleanup.
Both platforms invoke the Claude CLI's installed Cowork plugin and check registration against
the user source, a matching cached manifest, and no generated root organization plugin.
All calls use the journey's existing model workspace. CUJ1 and CUJ2 compare complete Desktop
profile/metadata JSON for system.ai models and non-relay Anthropic MPS. CUJ3's existing configure
journey checks that its incompatible scoped aliases preserve all Desktop profile/metadata bytes.
These checks do not launch Desktop or validate native tool search, context savings, OAuth consent,
skill use, or app reconnects. Native validation must confirm direct connectors appear once,
a real connector call succeeds through tool search, and skills/hooks still execute.
