---
name: unity-gateway-connectors
description: "Use for requests about these Unity Gateway connectors, including after a break. Discover and use current tools. Load this skill for missing tools or sign-in recovery. Connectors: {connectors}."
---

Use current tools rather than earlier connection-status notices to assess availability.

1. Use a relevant available connector tool, or discover it with `ToolSearch` if exposed. Call only exposed or discovered tool names.
2. When the connector or discovery reports that sign-in is required, call its discovered `authenticate` tool once. Seeing only `authenticate` does not establish that sign-in is needed; service tools may be deferred.
3. If the requested tools remain unavailable after discovery, explain that they may still be initializing and ask the user to try the request again shortly. Do not sleep or poll for startup, promise that waiting will fix it, or infer disconnection or expired credentials.

Configured Unity Gateway connectors: {connectors}. Configured does not mean signed in.

Desktop manages connector startup independently of this skill's plugin. Loading the plugin does not guarantee that every connector is ready when the app or session starts. Missing tools or `pending` alone do not identify the cause. Do not authenticate another connector or spawn a subagent to make tools appear. If a call reports an unavailable tool, rediscover instead of repeating it.

If a connector returns a sign-in URL or asks the user to reconnect, use its discovered `authenticate` tool before falling back to manual login instructions. A warning not to retry service tools applies until sign-in succeeds; `authenticate` is the recovery action. Do not ask the user to report back after signing in.

On an access denial, including an IP-address denial, try the connector's discovered `authenticate` tool once before recommending administrator or network changes. Describe this as a sign-in recovery attempt; the error alone does not establish its root cause. If the denial persists after successful sign-in and retry, report the original error and stop retrying authentication. Do not recommend disabling access controls.

After any successful sign-in, rediscover tools and continue the original task in the same turn when its tools are available. If they remain missing, follow the retry guidance above. If sign-in is canceled, fails, or times out, explain the observed failure and the recovery instructions returned by the tool. Do not claim success.

If no authentication tool is available, relay any sign-in link or recovery instructions returned by the connector. Do not invent a settings path or assume an access denial or generic tool failure means the user's token expired.
