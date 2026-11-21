---
name: unity-gateway-connectors
description: "Use for requests about these Unity Gateway connectors, including after a break. Load this skill before the first connector call for tool discovery and sign-in recovery. Loaded tools do not prove authentication; resolve a reported sign-in requirement first. Connectors: {connectors}."
---

Act on the requested connector in this turn, including after a break. Do not ask whether to check or retry. Never end the turn promising to wait or retry: perform the action. If sign-in needs user interaction, explain what is needed and stop service calls until it succeeds. Old connection-status notices do not establish current availability.

## Claude Code and Desktop Code tab authentication

Before the first service call, check current connection/authentication status even if tool definitions are cached. If no status is exposed, resolve the installed Claude CLI's absolute executable path through an exposed shell and run its `mcp list` once. Keep the session's working directory and CLAUDE_CONFIG_DIR for all CLI commands. Use the requested service's registration from the configured list below; a similarly named connector on another MCP server has separate authentication.

If status says `Needs authentication`, or discovery or a tool result requires sign-in, authenticate before calling service tools. Prefer the connector's exact exposed `authenticate` tool. Native HTTP OAuth may have no such tool: check the resolved CLI's `mcp login --help`, then run the quoted executable with `mcp login <registered-name>` once if supported. Use the exact native registration name, not a Cowork alias or tool name. Relay browser URL/callback instructions; do not read credential files or copy tokens. Do not start another login while one is pending.

After confirmed sign-in success, rediscover tools and resume the task. If tools stay absent, use an exposed reconnect action if available and the bounded waits below; report when a new session is needed instead of repeating login. If the CLI is missing, login is unsupported, or user interaction is required, explain the blocker and direct the user to that server's Authenticate button if Code displays one. This button is a client action, not an MCP tool. The interactive terminal Claude Code fallback is `/mcp`, select the registered server, then Authenticate; `/mcp` is not a shell command. Failed, canceled, or timed-out login is not success. `Waiting for the result` alone does not establish an auth failure; do not start duplicate service calls or login loops based on that message.

1. Check the tools exposed now. Use supported discovery if available (`ToolSearch` only if exposed). Use exact exposed or discovered tool names only; plugin/catalog search results alone do not make service tools callable. Check any current authentication status before a service call. A current sign-in requirement takes priority over available tool definitions; use the client's authentication flow first. Otherwise, call a relevant service tool if present, even if an earlier notice says the connector disconnected.
2. If the connector requires sign-in and exposes an `authenticate` tool, call that exact tool once, even when service tools are also listed. If only `authenticate` appears, call it once.
3. If no matching tools are exposed at startup, after resume, or while `pending`, and no current sign-in requirement is reported, your next action is an actual wait: wait 5 seconds by running `sleep 5` through an exposed shell or an exposed wait tool, then recheck tools. If they are still missing, wait another 5 seconds and recheck once more. Stop waiting as soon as tools appear.
4. If tools remain absent after both waits, or no supported wait action exists, report only that the connector's tools are not exposed in this session. Do not infer disconnection, missing sign-in, or expired credentials.

Configured Unity Gateway connectors: {connectors}. Configured does not mean signed in.

Connectors start in the background when the session opens, and a session-start note may list ones that had not answered yet. Tools may appear asynchronously after startup or resume. Missing tools or `pending` alone do not identify the cause. Base connection and sign-in claims on current discovery and actual tool errors. Never claim to have waited or refreshed tools without doing so. Do not guess tool names, authenticate another connector, or spawn a subagent to make tools appear. If a call reports an unavailable tool, do not repeat it; recheck tools. An unavailable name alone does not establish an authentication failure.

If a connector returns a sign-in URL or asks the user to reconnect, use its discovered `authenticate` tool before falling back to manual login instructions. A warning not to retry service tools applies until sign-in succeeds; `authenticate` is the recovery action. Do not ask the user to report back after signing in.

On an access denial, including an IP-address denial, try the connector's discovered `authenticate` tool once before recommending administrator or network changes. Describe this as a sign-in recovery attempt; the error alone does not establish its root cause. If the denial persists after successful sign-in and retry, report the original error and stop retrying authentication. Do not recommend disabling access controls.

After any successful sign-in, rediscover tools and continue the original task in the same turn. Do not ask the user to send another message. If sign-in is canceled, fails, or times out, explain the observed failure and the recovery instructions returned by the tool. Do not claim success.

If authentication cannot be completed with the client's supported actions, relay any sign-in link or recovery instructions returned by the connector. Do not invent a settings path or assume an access denial or generic tool failure means the user's token expired.
