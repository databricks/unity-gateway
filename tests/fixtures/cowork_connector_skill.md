---
name: unity-gateway-connectors
description: "Use for requests about these Unity Gateway connectors, including after a break. Check current tools and call a relevant available tool before answering. Load this skill for missing tools or sign-in recovery; never stop at a promise to retry. Connectors: `slack`, `gmail`."
---

Act on the requested connector in this turn, including after a break. Do not ask whether to check or retry. Never end the turn promising to wait or retry: perform the action. Old connection-status notices do not establish current availability.

1. Check the tools exposed now. Use supported discovery if available (`ToolSearch` only if exposed). Use exact exposed or discovered tool names only; plugin/catalog search results alone do not make service tools callable. Call a relevant service tool if present, even if an earlier notice says the connector disconnected.
2. If only the connector's `authenticate` tool appears, call that exact tool once.
3. If no matching tools are exposed at startup, after resume, or while `pending`, your next action is an actual wait: wait 5 seconds by running `sleep 5` through an exposed shell or an exposed wait tool, then recheck tools. If they are still missing, wait another 5 seconds and recheck once more. Stop waiting as soon as tools appear.
4. If tools remain absent after both waits, or no supported wait action exists, report that the connector's tools are not exposed in this session. Explain that they may still be initializing and ask the user to try the request again shortly. Do not promise that waiting will fix it or infer disconnection, missing sign-in, or expired credentials.

Configured Unity Gateway connectors: `slack`, `gmail`. Configured does not mean signed in.

Desktop manages connector startup independently of this skill's plugin. Loading the plugin does not guarantee that every connector is ready when the app or session starts. Tools may appear asynchronously after startup or resume. Use available tools immediately; only wait when the requested connector's tools are missing. Missing tools or `pending` alone do not identify the cause. Base connection and sign-in claims on current discovery and actual tool errors. Never claim to have waited or refreshed tools without doing so. Do not guess tool names, authenticate another connector, or spawn a subagent to make tools appear. If a call reports an unavailable tool, do not repeat it; recheck tools. An unavailable name alone does not establish an authentication failure.

If a connector returns a sign-in URL or asks the user to reconnect, use its discovered `authenticate` tool before falling back to manual login instructions. A warning not to retry service tools applies until sign-in succeeds; `authenticate` is the recovery action. Do not ask the user to report back after signing in.

On an access denial, including an IP-address denial, try the connector's discovered `authenticate` tool once before recommending administrator or network changes. Describe this as a sign-in recovery attempt; the error alone does not establish its root cause. If the denial persists after successful sign-in and retry, report the original error and stop retrying authentication. Do not recommend disabling access controls.

After any successful sign-in, rediscover tools and continue the original task in the same turn when its tools are available. If they remain missing, follow the bounded waits and retry guidance above. If sign-in is canceled, fails, or times out, explain the observed failure and the recovery instructions returned by the tool. Do not claim success.

If no authentication tool is available, relay any sign-in link or recovery instructions returned by the connector. Do not invent a settings path or assume an access denial or generic tool failure means the user's token expired.
