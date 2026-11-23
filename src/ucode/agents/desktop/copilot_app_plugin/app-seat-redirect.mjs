import { join } from "node:path";

import { NO_DEBUG } from "./app-debug.mjs";
import { detectLoopbackProxy } from "./app-proxy.mjs";
import { createSystemProxyResolver } from "./app-system-proxy.mjs";
import { createStateLog } from "./app-state.mjs";
import {
  copilotHostForStatus,
  createCopilotHostResolver,
  createSeatRelay,
  loadSystemCertificates,
  subagentHeaders,
} from "./app-seat-relay.mjs";
import { withSessionId } from "./ug-plugin.mjs";

// Points the app session this extension serves at the seat relay in this process, the way
// `ug copilot` sets COPILOT_API_URL for the CLI. The runtime applies `options.update({copilotUrl})`
// to the session only; it isn't kept across an app restart, so every start that doesn't redirect
// clears it instead, which makes a stale port from an earlier run harmless.
export const SESSIONS_FILE = "seat-relay-sessions.json";
// Every request the relay served: route, method, path and status, never a header value or body.
export const REQUESTS_FILE = "seat-relay-requests.json";
export const REQUEST_LOG_LIMIT = 200;
export const SANDBOX_ENV = "COPILOT_EXTENSION_SANDBOXED";
export const APPLY_TIMEOUT_MS = 5_000;
const PRODUCT_TOKEN = /^[A-Za-z0-9][A-Za-z0-9._-]*\/[^\s(),;/]+$/;
const USER_AGENT_COMMENT = /\([^()]*\)/g;

// ug's User-Agent for one of the runtime's calls: the runtime's own product tokens (such as
// `copilot/1.0.106-8 ... client/github-app`) between ug's product and platform, so the gateway
// attributes the call to Copilot at the runtime's version.
export function seatUserAgent(template, runtimeUserAgent) {
  const [ucode, ...rest] = (template ?? "ucode/unknown").split(" ");
  const platform = rest.join(" ");
  const tokens = (runtimeUserAgent ?? "")
    .replace(USER_AGENT_COMMENT, " ")
    .split(/\s+/)
    .filter((token) => PRODUCT_TOKEN.test(token));
  if (!tokens[0]?.toLowerCase().startsWith("copilot/")) tokens.unshift("copilot/unknown");
  return [ucode, ...tokens, platform].filter(Boolean).join(" ");
}
export const REVERT_TIMEOUT_MS = 2_000;
const INFERENCE_CALLS = new Set(["POST /chat/completions", "POST /responses", "POST /v1/messages"]);

function relayConfigured(config) {
  const relay = config?.seatRelay;
  return Boolean(
    relay &&
      typeof relay === "object" &&
      typeof relay.gatewayBase === "string" &&
      typeof relay.providerService === "string",
  );
}

// Why this start doesn't redirect: "off" (nothing to route, or remove mode), "sandboxed" (the
// runtime's extension sandbox blocks loopback), or null when it does. The runtime sets the
// variable on every extension, "0" outside the sandbox and "1" inside, and its own bootstrap
// reads only "1" as sandboxed.
export function redirectSkipReason(config, env = process.env) {
  if (!relayConfigured(config) || config.remove) return "off";
  if (env[SANDBOX_ENV] === "1") return "sandboxed";
  return null;
}

export function redirectEnabled(config, env = process.env) {
  return redirectSkipReason(config, env) === null;
}

// An entry in the sessions file for a start that couldn't redirect, so status can say why.
export function noteRedirect(stateDir, entry, { sessionId = process.env.SESSION_ID, now = Date.now } = {}) {
  return createStateLog(join(stateDir, SESSIONS_FILE))({
    time: new Date(now()).toISOString(),
    ...(sessionId ? { sessionId } : {}),
    ...entry,
  });
}

function updateUrl(session, copilotUrl) {
  return Promise.resolve().then(() => session.rpc.options.update({ copilotUrl }));
}

async function within(ms, promise) {
  let timer;
  const bound = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`no answer within ${ms} ms`)), ms);
  });
  try {
    return await Promise.race([promise, bound]);
  } finally {
    clearTimeout(timer);
  }
}

// Idempotent, and harmless on a session that never was redirected.
export function clearRedirect(session) {
  return updateUrl(session, null).catch(() => {});
}

// The relay and the session's redirect to it. `listen()` runs before joinSession and starts the
// proxy check; `apply()` right after it waits for that check, so the first turn's calls already go
// through, or never get redirected when a proxy could capture them. `handle()` sees the session's
// events; after each turn with model calls, an entry with the counts goes to the sessions file.
// `shutdown(ms)` points the session back at the default URL within `ms`.
export function createSeatRedirect(
  config,
  {
    tokenSource,
    log,
    stateDir,
    sessionId = process.env.SESSION_ID,
    env = process.env,
    now = Date.now,
    record,
    recordRequest,
    applyTimeoutMs = APPLY_TIMEOUT_MS,
    githubUserUrl,
    debug = NO_DEBUG,
    systemProxy = createSystemProxyResolver({ debug }),
    proxyCheck = ({ port } = {}) => detectLoopbackProxy({ env, debug, port, resolve: systemProxy }),
    hostCheck = null,
  } = {},
) {
  if (!redirectEnabled(config, env)) return null;
  const relay = config.seatRelay;
  const ownModels = new Set((config.models ?? []).map((model) => model.id));
  let session = null;
  let redirect = null;
  let proxy = null;
  let proxyPending = null;
  let proxyMs = null;
  let hostMs = null;
  let builtInCalls = 0;
  let relayedCalls = 0;
  let lastGatewayError = null;
  let dirty = false;
  const note = record ?? createStateLog(join(stateDir, SESSIONS_FILE));
  const noteRequest =
    recordRequest ?? createStateLog(join(stateDir, REQUESTS_FILE), { limit: REQUEST_LOG_LIMIT });

  // GitHub refuses a request without a User-Agent before it reads the credential.
  const copilotHost = createCopilotHostResolver({
    askGitHub: relay.askGitHub !== false,
    userAgent: seatUserAgent(relay.userAgent, "copilot/app"),
    ...(githubUserUrl ? { userUrl: githubUserUrl } : {}),
    fallback: async () => (session ? copilotHostForStatus(await session.rpc.gitHubAuth.getStatus()) : null),
    debug,
    agentFor: (url) => server.agentFor(url),
  });
  const subagents = subagentHeaders(sessionId ?? "");
  const server = createSeatRelay({
    gatewayBase: relay.gatewayBase,
    providerService: relay.providerService,
    token: tokenSource,
    copilotHost,
    headers: withSessionId(relay.headers, sessionId),
    requestHeaders: (requestHeaders, body) => ({
      "User-Agent": seatUserAgent(relay.userAgent, requestHeaders["user-agent"]),
      ...subagents(requestHeaders, body),
    }),
    hintFor: (error) => (error?.code === "ENOENT" ? config.missingUgHint : config.loginHint),
    log,
    env,
    debug,
    systemProxy,
    observe: (event) => {
      noteRequest({ time: new Date(now()).toISOString(), ...(sessionId ? { sessionId } : {}), ...event });
      if (event.route !== "gateway" || !INFERENCE_CALLS.has(`${event.method} ${event.path}`)) return;
      relayedCalls++;
      lastGatewayError = event.error ?? (event.status >= 400 ? String(event.status) : null);
      dirty = true;
    },
  });

  function entry() {
    return {
      time: new Date(now()).toISOString(),
      ...(sessionId ? { sessionId } : {}),
      redirect,
      ...(proxy?.reason ? { reason: proxy.reason } : {}),
      ...(proxy?.error ? { proxyCheck: proxy.error } : {}),
      ...(proxy?.decision ? { proxyDecision: proxy.decision } : {}),
      ...(hostMs !== null ? { hostMs } : {}),
      ...(proxyMs !== null ? { proxyMs } : {}),
      builtInCalls,
      relayedCalls,
      ...(lastGatewayError ? { lastGatewayError } : {}),
    };
  }

  function checkProxy() {
    if (proxyPending) return proxyPending;
    const started = now();
    proxyPending = Promise.resolve()
      .then(() => proxyCheck({ port: server.port }))
      .catch((error) => ({ captured: false, reason: null, error: String(error?.message ?? error) }))
      .then((result) => {
        proxyMs = now() - started;
        return result;
      });
    return proxyPending;
  }

  function flush() {
    if (!dirty) return;
    dirty = false;
    const current = entry();
    debug("redirect.entry", current);
    note(current);
  }

  return {
    get url() {
      return server.url;
    },
    get state() {
      return redirect;
    },
    async listen() {
      debug("relay.certificates", { systemStore: loadSystemCertificates() });
      await server.listen();
      debug("relay.listening", { port: server.port });
      // The proxy check asks about the relay's own URL, so it needs the port.
      checkProxy();
    },
    async apply(joined) {
      session = joined;
      debug("redirect.apply.start", {});
      hostMs = (await hostCheck)?.ms ?? null;
      proxy = await checkProxy();
      debug("redirect.apply.checks", { hostMs, proxyMs, proxy });
      if (proxy.error) log(`Databricks seat relay: proxy check failed: ${proxy.error}`);
      if (proxy.captured) {
        redirect = "proxied";
        log(`Databricks seat relay: not redirecting, ${proxy.reason}`);
        const cleared = now();
        await within(applyTimeoutMs, clearRedirect(session)).catch(() => {});
        debug("redirect.clear", { why: "proxied", ms: now() - cleared });
        dirty = true;
        flush();
        return false;
      }
      const applying = now();
      try {
        const result = await within(applyTimeoutMs, updateUrl(session, server.url));
        redirect = result?.success === false ? "unavailable" : "applied";
        debug("redirect.apply.done", { redirect, result, ms: now() - applying });
      } catch (error) {
        redirect = "unavailable";
        debug("redirect.apply.failed", { error: String(error?.message ?? error), ms: now() - applying });
        log(`Databricks seat relay: the app didn't take the redirect: ${error?.message ?? error}`);
        // An update that answers after the bound may still have applied.
        clearRedirect(session);
      }
      if (redirect !== "applied") {
        dirty = true;
        flush();
      }
      return redirect === "applied";
    },
    handle(event) {
      const type = event?.type;
      debug("session.event", {
        type,
        statusCode: event?.data?.statusCode,
        isByok: event?.data?.isByok,
        provider: event?.data?.provider ?? event?.data?.providerId,
        model: event?.data?.model,
      });
      if (type === "model.call_start") {
        if (!ownModels.has(event.data?.model)) {
          builtInCalls++;
          dirty = true;
        }
      } else if (type === "session.idle" || type === "session.shutdown") {
        flush();
      }
    },
    async shutdown(ms = REVERT_TIMEOUT_MS) {
      debug("redirect.shutdown", { redirect, ms });
      flush();
      if (!session) return;
      await within(ms, clearRedirect(session)).catch(() => {});
      debug("redirect.clear", { why: "shutdown" });
    },
    close() {
      return server.close();
    },
  };
}

// A thrown error in any of ug's extension code would otherwise end this process and, with it, the
// session's route to its models. Installed only while a redirect is live.
export function keepAliveOnErrors(log, proc = process) {
  const report = (kind) => (error) => log(`Databricks seat relay: ${kind}: ${error?.message ?? error}`);
  proc.on("uncaughtException", report("uncaught exception"));
  proc.on("unhandledRejection", report("unhandled rejection"));
}
