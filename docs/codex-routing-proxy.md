# Codex Smart Router inference proxy

Smart-routed Codex launches use a local gateway proxy to attach the current recipe
to every inference request. Changing `$smart-router off` or `$smart-router on`
does not interrupt commands, unload conversations, restart Codex, or inject a
continuation prompt. Existing and new children keep using the same proxy.

## Request path

```text
Native Codex TUI / app-server, including native subagents
  -> per-launch authenticated loopback HTTP proxy
  -> configured Databricks AI Gateway workspace
```

The skill atomically replaces the existing session-control JSON file. The proxy
reads that file once for each request and sets both:

- Header: `Databricks-Smart-Router-Recipe`.
- Responses API body:
  `client_metadata["x-codex-turn-metadata"].smart_router_recipe_name`.
  The turn metadata may be a JSON string or an object; its existing form and other
  fields are preserved. `/responses/compact` gets the same treatment.

Off sends `DISABLED`; on restores the launch recipe. This applies to all model
traffic through the proxy, including tasks the root handles itself. A request
already being forwarded keeps its snapshot, including across an authentication
retry. Subsequent requests observe the new state. No backend session state or
per-thread registration is required.

Unreadable or invalid controls return HTTP 503 instead of silently forwarding
stale attribution. Invalid request metadata returns HTTP 400. Normal gateway
status codes and streamed responses are forwarded through the shared proxy.

## Native agent-message compatibility

Codex v2 puts assignments, follow-ups and child results in `agent_message` input
items. The gateway's OSS Responses-to-Chat converter silently drops this item
type. Seeing an assignment in a local Codex transcript therefore does not prove
that Kimi received it. A direct comparison reproduced HTTP 200 with `NO_TASK`
for an `agent_message`, and the correct answer for identical text in a standard
user message.

For UG's recognized OSS families (Kimi, GLM and DeepSeek), the proxy converts
plaintext agent messages to standard user messages in place, retaining their
content and recipient label. This applies with routing both on and off, without
changing native histories or reloading agents. Other input items and GPT's native
agent-message format remain unchanged. Encrypted-only OSS agent messages fail
with an upgrade/new-session hint instead of silently losing the assignment.

## Launch and configuration

Each launch gets its own controls, ephemeral loopback port, and random secret.
Codex receives a launch-only `DatabricksSmartRouter` provider with the proxy URL
and an environment-backed credential. Neither the port nor the secret is written
to Codex settings. The proxy replaces the session credential with the current
Databricks token, using the same selected workspace, profile, PAT or custom OAuth
authentication as the launcher. The shared gateway proxy handles refresh and one
authentication retry. It accepts only the configured API path prefix, enforces
the session credential, and retains workspace selector headers.

The proxy disables WebSocket inference and forwards HTTP/SSE. Subagent-only
presets launch the native TUI directly. First-prompt routing retains the existing
app-server control interposer solely for model selection; inference still uses
the gateway proxy. No tree-reload controller remains.

Proxy lifetime follows the launched Codex process. Normal exit and launch errors
close the listener, token refresher and HTTP client. Refresh failures log only the
exception type to `~/.ucode/codex-gateway-proxy.log` and return a sign-in hint.

Run `ug configure` once after upgrading. Older UG-managed machine settings pin
`model_provider = "Databricks"`; that conflicts with the launch provider and can
make the native TUI read-only. Ordinary configure removes this UG-owned default
while preserving the per-user default, Databricks provider definition, and other
managed policies. Explicit administrator provider selections are preserved and
block routed startup with an actionable error. Explicit `ug configure --global`
retains the machine default because it creates no per-user default; that setup
requires an administrator to allow the launch provider before routing can run.

To reopen proxy-backed history, launch `ug codex` with the same routing preset
and use native `/resume`. This supplies a fresh proxy address and credential.
Native CLI subcommands such as `ug codex -- resume ID` bypass smart routing and
do not supply this provider. Routing controls belong to a launch, so a new launch
starts from its selected preset, not the previous launch's off/on override.

## Validation

Linux installed-wheel busy-child tests against `eng-ml-inference` passed on Codex
0.164.0, 0.165.0 and 0.167.0. The actual skill turned routing off while two children ran commands,
then on while three existing children ran commands. Every command started once
and finished with its original PID; thread IDs and permissions stayed unchanged.
New children after each toggle inherited current attribution. Each run verified
six post-off requests across four threads and seven post-on requests across five
threads, including successful responses and matching header/body recipes.

Independent mitmproxy on 0.165.0 captured 30 inference requests, all with matching
recipe fields and HTTP 200 responses. Captures retained only allowlisted metadata,
not credentials or prompts. First-prompt model routing passed on 0.164.0. On 0.167.0, native agent-picker navigation and a fresh
launch followed by `/resume` also passed. The diagnostic recorders and mitmproxy
are test tools, separate from the production gateway proxy.

Component coverage includes isolated concurrent sessions, state changes during
authentication retry, invalid controls and recovery, metadata preservation,
streaming, configuration migration, provider restrictions and listener cleanup.
The existing Codex minimum (0.164.0) and routing CI pin (0.165.0) are unchanged.

The Kimi task-delivery regression fails against the previous installed package:
the first child's assignment is absent from standard message input. With the fix,
Codex 0.167.0 completes distinct file tasks in two Kimi children, then new tasks
through `followup_task` under the same IDs. Six post-off and six post-on requests
carry matching header/body recipes. Native histories keep their original message
items; only outgoing OSS request bodies are translated. This test deliberately
pins child models while routing is off to isolate delivery from route selection.

A second installed-wheel test on Codex 0.165.0 verifies actual router-selected Kimi
children during the busy-command off/on journey. The original commands finish
once with unchanged PIDs and thread IDs. Six post-off requests across four threads
and seven post-on requests across five threads carry matching recipes, including
existing Kimi children and newly spawned children. GLM and DeepSeek translation
has component coverage only; it has not been live-tested.

Native macOS/Windows TUI behavior, live compaction, proxy failure during an active
stream, and full live CI coverage remain unverified. Older tree-reload results do
not establish coverage of this replacement.
