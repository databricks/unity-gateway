import { join } from "node:path";

import { createOutcomeLog } from "./app-tracing.mjs";
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
// runtime's extension sandbox blocks loopback), or null when it does.
export function redirectSkipReason(config, env = process.env) {
  if (!relayConfigured(config) || config.remove) return "off";
  if (env[SANDBOX_ENV]) return "sandboxed";
  return null;
}

export function redirectEnabled(config, env = process.env) {
  return redirectSkipReason(config, env) === null;
}

// An entry in the sessions file for a start that couldn't redirect, so status can say why.
export function noteRedirect(stateDir, entry, { sessionId = process.env.SESSION_ID, now = Date.now } = {}) {
  return createOutcomeLog(join(stateDir, SESSIONS_FILE))({
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

// The relay and the session's redirect to it. `listen()` runs before joinSession and `apply()`
// right after it, so the first turn's calls already go through. `handle()` sees the session's
// events; after each turn with model calls, an entry with the counts goes to the sessions file, which
// `ug status` reads. `shutdown(ms)` points the session back at the default URL within `ms`.
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
  } = {},
) {
  if (!redirectEnabled(config, env)) return null;
  const relay = config.seatRelay;
  const ownModels = new Set((config.models ?? []).map((model) => model.id));
  let session = null;
  let redirect = null;
  let builtInCalls = 0;
  let relayedCalls = 0;
  let lastGatewayError = null;
  let dirty = false;
  const note = record ?? createOutcomeLog(join(stateDir, SESSIONS_FILE));
  const noteRequest =
    recordRequest ?? createOutcomeLog(join(stateDir, REQUESTS_FILE), { limit: REQUEST_LOG_LIMIT });

  const copilotHost = createCopilotHostResolver({
    askGitHub: relay.askGitHub !== false,
    userAgent: relay.headers?.["User-Agent"] ?? relay.headers?.["user-agent"],
    fallback: async () => (session ? copilotHostForStatus(await session.rpc.gitHubAuth.getStatus()) : null),
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
      builtInCalls,
      relayedCalls,
      ...(lastGatewayError ? { lastGatewayError } : {}),
    };
  }

  function flush() {
    if (!dirty) return;
    dirty = false;
    note(entry());
  }

  return {
    get url() {
      return server.url;
    },
    async listen() {
      loadSystemCertificates();
      await server.listen();
    },
    async apply(joined) {
      session = joined;
      try {
        const result = await within(applyTimeoutMs, updateUrl(session, server.url));
        redirect = result?.success === false ? "unavailable" : "applied";
      } catch (error) {
        redirect = "unavailable";
        log(`Databricks seat relay: the app didn't take the redirect: ${error?.message ?? error}`);
      }
      if (redirect !== "applied") {
        dirty = true;
        flush();
      }
      return redirect === "applied";
    },
    handle(event) {
      const type = event?.type;
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
      flush();
      if (!session) return;
      await within(ms, clearRedirect(session)).catch(() => {});
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
