import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import http from "node:http";
import https from "node:https";
import tls from "node:tls";

import { NO_DEBUG } from "./app-debug.mjs";
import { createSystemProxyResolver } from "./app-system-proxy.mjs";

// Loopback relay for the GitHub Copilot app's own Copilot API calls, hosted in ug's extension.
// The extension points its session's Copilot API base at `http://127.0.0.1:<port>/<secret>`. The
// relay set below goes to the workspace's AI Gateway seat relay: the runtime's own GitHub token
// stays in `Authorization`, and a Databricks token rides the swap header with the Model Provider
// Service and the caller's Copilot API host. Every other call goes straight to that Copilot API
// host with the runtime's own headers. This is the extension's counterpart of copilot_relay.py.
//
// Security invariants:
//   - Serves only requests addressed to 127.0.0.1:<port>, from no browser origin, whose first path
//     segment is the per-launch secret. GitHub MCP, which the runtime sends without the base path,
//     is the one exception and only ever goes direct.
//   - A Databricks token is added only on the exact gateway routes. A direct call carries no
//     Databricks header and goes only to a GitHub Copilot API host, without following redirects.
//   - Never logs header values or bodies.

export const GATEWAY_ROUTES = new Map([
  ["POST /chat/completions", "/ai-gateway/openai/v1/chat/completions"],
  ["POST /responses", "/ai-gateway/openai/v1/responses"],
  ["POST /v1/messages", "/ai-gateway/anthropic/v1/messages"],
  ["GET /meta", "/ai-gateway/github-copilot/v1/meta"],
  ["GET /models", "/ai-gateway/v1/models"],
  ["POST /auto", "/ai-gateway/github-copilot/v1/auto"],
]);
export const SECRETLESS_DIRECT_CALLS = new Set(["POST /mcp/readonly"]);

export const AI_GATEWAY_TOKEN_HEADER = "x-databricks-ai-gateway-token";
export const MODEL_PROVIDER_SERVICE_HEADER = "databricks-model-provider-service";
export const COPILOT_API_HOST_HEADER = "x-databricks-github-copilot-api-host";
export const SUBAGENT_ID_HEADER = "thread-id";
export const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "trailers",
  "transfer-encoding",
  "upgrade",
  "host",
  "content-length",
]);
const RESERVED_ADDED_HEADERS = new Set([
  AI_GATEWAY_TOKEN_HEADER,
  "authorization",
  MODEL_PROVIDER_SERVICE_HEADER,
  COPILOT_API_HOST_HEADER,
]);
const DATABRICKS_HEADER_PREFIXES = ["x-databricks-", "databricks-"];
const GITHUB_TOKEN_PREFIXES = ["gho_", "ghu_", "github_pat_", "tid="];
const GITHUB_LOOKUP_PREFIXES = ["gho_", "ghu_", "github_pat_"];
const COPILOT_API_HOST =
  /^(?:api(?:\.(?:individual|business|enterprise))?\.githubcopilot\.com|copilot-api\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ghe\.com)$/;
const GHE_ACCOUNT_HOST = /^([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.ghe\.com$/;
// `copilotPlan` values seen or documented: individual, individual_pro, individual_pro_plus,
// individual_max, free, business, enterprise; students and educators get an individual seat.
const INDIVIDUAL_PLAN_PREFIXES = ["individual", "free", "pro", "student", "edu"];

export const MAX_BODY_BYTES = 64 * 1024 * 1024;
export const CONNECT_TIMEOUT_MS = 10_000;
export const READ_TIMEOUT_MS = 600_000;
// Longer than the runtime's idle keep-alive, so a pooled socket isn't closed under a new request.
export const KEEP_ALIVE_TIMEOUT_MS = 120_000;
export const GITHUB_MISS_TTL_MS = 60_000;
export const GITHUB_LOOKUP_TIMEOUT_MS = 10_000;
export const HOST_CACHE_ENTRIES = 64;
export const COPILOT_USER_URL = "https://api.github.com/copilot_internal/user";
const GATEWAY_DENIAL_BODY_LIMIT = 64 * 1024;
const GENERIC_GATEWAY_DENIAL = "Unity Gateway denied this request. Check your access to the model.";
const GITHUB_DENIAL_HINT = "Check your Copilot CLI policy setting at https://github.com/settings/copilot.";
const GITHUB_MESSAGE_LIMIT = 300;
// Databricks error codes of a denial that a fresh token can't change.
const POLICY_ERROR_CODES = new Set(["PERMISSION_DENIED", "INVALID_PARAMETER_VALUE"]);
const PROXY_AUTH_NEEDED = "the system proxy requires authentication.";
const PROXY_AUTH_ERROR = /\b407\b/;
const RELAY = "the Unity Gateway Copilot relay";

// The extension's Node trusts only its bundled CAs; a workstation behind TLS inspection needs the
// system store too. Older Nodes lack the API, and then nothing changes.
const PROXY_ENV_NAMES = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"];

// The proxy variables, in either case, that the upstream agents follow.
export function proxyEnvironment(env = process.env) {
  const picked = {};
  for (const name of PROXY_ENV_NAMES) {
    for (const key of [name, name.toLowerCase()]) {
      if (env[key]) picked[key] = env[key];
    }
  }
  return picked;
}

const CREDENTIAL_KINDS = [
  ["gho_", "oauth"],
  ["ghu_", "user-to-server"],
  ["github_pat_", "pat"],
  ["tid=", "session"],
];

function credentialKind(credential) {
  return CREDENTIAL_KINDS.find(([prefix]) => credential.startsWith(prefix))?.[1] ?? "other";
}

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return "<unparsed>";
  }
}

export function loadSystemCertificates(tlsModule = tls) {
  if (
    typeof tlsModule.getCACertificates !== "function" ||
    typeof tlsModule.setDefaultCACertificates !== "function"
  ) {
    return false;
  }
  try {
    const certificates = new Set([
      ...tlsModule.getCACertificates("default"),
      ...tlsModule.getCACertificates("system"),
    ]);
    tlsModule.setDefaultCACertificates([...certificates]);
    return true;
  } catch {
    return false;
  }
}

// The host of a Copilot `endpoints.api` URL, or null unless it is one of GitHub's Copilot API hosts
// over HTTPS on the default port.
export function copilotApiHost(url) {
  if (typeof url !== "string") return null;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (
    parsed.protocol !== "https:" ||
    parsed.username ||
    parsed.password ||
    parsed.port ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash
  ) {
    return null;
  }
  return COPILOT_API_HOST.test(parsed.hostname) ? parsed.hostname : null;
}

// The Copilot API host for a `gitHubAuth.getStatus()` result: GHE.com accounts have a tenant host,
// github.com accounts one per plan. Unknown plans give null.
export function copilotHostForStatus(status) {
  if (!status || typeof status !== "object") return null;
  const account =
    typeof status.host === "string"
      ? status.host
          .toLowerCase()
          .replace(/^[a-z]+:\/\//, "")
          .replace(/[/:].*$/, "")
      : "";
  if (account && account !== "github.com") {
    const tenant = GHE_ACCOUNT_HOST.exec(account);
    return tenant ? `copilot-api.${tenant[1]}.ghe.com` : null;
  }
  const plan = typeof status.copilotPlan === "string" ? status.copilotPlan.toLowerCase() : "";
  if (INDIVIDUAL_PLAN_PREFIXES.some((prefix) => plan.startsWith(prefix))) {
    return "api.individual.githubcopilot.com";
  }
  for (const tier of ["business", "enterprise"]) {
    if (plan.startsWith(tier)) return `api.${tier}.githubcopilot.com`;
  }
  return null;
}

// A fragment would miss the gateway routes and then be dropped on the way upstream. A percent
// escape would miss them too, and an upstream that decodes it could serve a route the gateway
// never saw.
export function isNormalizedPath(path) {
  if (typeof path !== "string" || !path.startsWith("/") || /[\\#%]/.test(path)) return false;
  return path
    .slice(1)
    .split("/")
    .every((segment) => segment !== "" && segment !== "." && segment !== "..");
}

// Readable as both an OpenAI and an Anthropic error, whichever API Copilot called.
export function errorBody(message, source) {
  const error = { type: "invalid_request_error", message };
  if (source) error.source = source;
  return Buffer.from(JSON.stringify({ type: "error", error }));
}

function githubDenial(message) {
  const text = message.split(/\s+/).join(" ").slice(0, GITHUB_MESSAGE_LIMIT).replace(/[ .]+$/, "");
  return `GitHub Copilot denied this request: ${text}. ${GITHUB_DENIAL_HINT}`;
}

const UNKNOWN_DENIAL = { source: "unknown", message: GENERIC_GATEWAY_DENIAL, final: false };

// A 403 from a gateway call: the message Copilot shows and where it came from. `gateway` is a
// Databricks error body (`error_code` and `message`); `github` is GitHub's own body, which the
// gateway passes through on the catalog routes; `unknown` gets the generic text. `final` denials
// are policy decisions a fresh Databricks token can't change, so they aren't retried.
export function classifyDenial(body) {
  if (!body || body.length === 0 || body.length > GATEWAY_DENIAL_BODY_LIMIT) return UNKNOWN_DENIAL;
  const text = body.toString("utf8");
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    payload = undefined;
  }
  if (payload && typeof payload === "object") {
    const message = typeof payload.message === "string" ? payload.message.trim() : "";
    if (!message) return UNKNOWN_DENIAL;
    if ("error_code" in payload) {
      if (typeof payload.error_code !== "string") return UNKNOWN_DENIAL;
      return { source: "gateway", message: payload.message, final: POLICY_ERROR_CODES.has(payload.error_code) };
    }
    return { source: "github", message: githubDenial(message), final: true };
  }
  const line = text.trim();
  // The Copilot API answers a policy denial with one line of plain text.
  if (line && !line.startsWith("<") && !line.includes("\n") && !/[\p{C}]/u.test(line)) {
    return { source: "github", message: githubDenial(line), final: true };
  }
  return UNKNOWN_DENIAL;
}

export function gatewayDenialMessage(body) {
  return classifyDenial(body).message;
}

function messageText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return null;
  const texts = content
    .filter((part) => part && typeof part === "object" && typeof part.text === "string")
    .map((part) => part.text);
  return texts.length ? texts.join("\n") : null;
}

// The text of the first user message of an Anthropic Messages, Chat Completions or Responses
// request.
export function firstUserText(body) {
  let payload;
  try {
    payload = JSON.parse(body ? body.toString("utf8") : "");
  } catch {
    return null;
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const turns = payload.messages ?? payload.input;
  if (typeof turns === "string") return turns;
  if (!Array.isArray(turns)) return null;
  const first = turns.find((turn) => turn && typeof turn === "object" && turn.role === "user");
  return first ? messageText(first.content) : null;
}

// Gives a subagent's requests an id of that subagent: Copilot marks them only by type, and its
// delegated prompt, the first user message, stays the same over the subagent's turns. The hash is
// keyed for this launch, so the id can't confirm a guess at the prompt.
export function subagentHeaders(sessionId, key = randomBytes(32)) {
  return (requestHeaders, body) => {
    if (requestHeaders?.["x-interaction-type"] !== "conversation-subagent") return {};
    const prompt = firstUserText(body);
    if (prompt === null) return {};
    const digest = createHmac("sha256", key).update(prompt, "utf8").digest("hex");
    return { [SUBAGENT_ID_HEADER]: `${sessionId}-${digest.slice(0, 16)}` };
  };
}

function transport(url) {
  return url.protocol === "https:" ? https : http;
}

function readBody(stream, limit) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    stream.on("data", (chunk) => {
      size += chunk.length;
      if (size > limit) {
        stream.pause();
        const failure = new Error("body over the limit");
        failure.code = "BODY_TOO_LARGE";
        reject(failure);
        return;
      }
      chunks.push(chunk);
    });
    stream.on("end", () => resolve(Buffer.concat(chunks)));
    stream.on("error", reject);
  });
}

// Starts an upstream request and resolves with its response headers. The connect timeout covers
// the TCP and TLS handshake; the read timeout is the longest idle gap after that.
function startUpstream(url, { method, headers, body, agent, connectTimeoutMs, readTimeoutMs }) {
  const request = transport(url).request(url, { method, headers, agent });
  const fail = (message) => () => request.destroy(new Error(message));
  const connectTimer = setTimeout(fail("upstream connect timed out"), connectTimeoutMs);
  const connected = () => clearTimeout(connectTimer);
  request.on("socket", (socket) => {
    if (!socket.connecting) return connected();
    socket.once(socket.encrypted ? "secureConnect" : "connect", connected);
  });
  request.on("close", connected);
  request.setTimeout(readTimeoutMs, fail("upstream timed out"));
  const response = new Promise((resolve, reject) => {
    request.once("response", resolve);
    request.once("error", reject);
  });
  request.end(body ?? undefined);
  return { request, response };
}

// A client gone before the upstream request starts (a turn stopped during the host lookup or a
// mint) must not start one: nothing would cancel it.
function startTracked(cancel, url, options) {
  if (cancel.closed) {
    const failure = new Error("client went away");
    failure.code = "CLIENT_CLOSED";
    throw failure;
  }
  const started = startUpstream(url, options);
  cancel.current = started.request;
  return started;
}

function hasDatabricksPrefix(name) {
  return DATABRICKS_HEADER_PREFIXES.some((prefix) => name.startsWith(prefix));
}

function forwardedHeaders(incoming, strip) {
  const headers = {};
  for (const [name, value] of Object.entries(incoming)) {
    if (HOP_BY_HOP_HEADERS.has(name) || hasDatabricksPrefix(name) || strip.has(name)) continue;
    headers[name] = value;
  }
  return headers;
}

function withoutReserved(headers) {
  return Object.fromEntries(
    Object.entries(headers ?? {}).filter(
      ([name, value]) =>
        typeof value === "string" && !RESERVED_ADDED_HEADERS.has(name.toLowerCase()),
    ),
  );
}

function responseHeaders(upstream) {
  const headers = {};
  for (const [name, value] of Object.entries(upstream.headers)) {
    if (!HOP_BY_HOP_HEADERS.has(name)) headers[name] = value;
  }
  return headers;
}

function githubAuthorization(distinct) {
  const values = distinct.authorization ?? [];
  if (values.length !== 1) return null;
  const [scheme, credential = ""] = values[0].split(/\s+/, 2);
  if (scheme.toLowerCase() !== "bearer") return null;
  return GITHUB_TOKEN_PREFIXES.some((prefix) => credential.startsWith(prefix))
    ? `Bearer ${credential}`
    : null;
}

// Finds the caller's Copilot API host, once per GitHub credential: GitHub's own answer for the
// credential first (`/copilot_internal/user`, which Copilot itself calls at launch), then the
// fallback, such as the session's sign-in status. A GitHub miss isn't asked again for a while, so
// every request doesn't wait on it; the fallback's answer is never remembered, because the
// signed-in account can change.
export function createCopilotHostResolver({
  askGitHub = true,
  fallback = async () => null,
  userUrl = COPILOT_USER_URL,
  userAgent,
  now = Date.now,
  missTtlMs = GITHUB_MISS_TTL_MS,
  timeoutMs = GITHUB_LOOKUP_TIMEOUT_MS,
  maxEntries = HOST_CACHE_ENTRIES,
  debug = NO_DEBUG,
  agentFor = async () => undefined,
} = {}) {
  const hosts = new Map();
  const missedAt = new Map();

  // GitHub MCP is served without the secret, so any local caller can add entries.
  function remember(map, key, value) {
    map.delete(key);
    map.set(key, value);
    if (map.size > maxEntries) map.delete(map.keys().next().value);
  }

  async function fromGitHub(authorization) {
    const credential = authorization.slice("Bearer ".length);
    if (!GITHUB_LOOKUP_PREFIXES.some((prefix) => credential.startsWith(prefix))) return null;
    const headers = { authorization, accept: "application/json" };
    if (userAgent) headers["user-agent"] = userAgent;
    let reply;
    try {
      const target = new URL(userUrl);
      const { response } = startUpstream(target, {
        method: "GET",
        headers,
        agent: await agentFor(target),
        connectTimeoutMs: timeoutMs,
        readTimeoutMs: timeoutMs,
      });
      reply = await response;
      reply.on("error", () => {});
      if (reply.statusCode !== 200) {
        reply.resume();
        debug("host.github", { status: reply.statusCode, host: null });
        return null;
      }
      const payload = JSON.parse((await readBody(reply, GATEWAY_DENIAL_BODY_LIMIT)).toString("utf8"));
      const host = copilotApiHost(payload?.endpoints?.api);
      debug("host.github", { status: 200, host, plan: payload?.copilot_plan ?? null });
      return host;
    } catch (error) {
      reply?.destroy();
      debug("host.github", { error: String(error?.code ?? error?.message ?? error), host: null });
      return null;
    }
  }

  return async function copilotHost(authorization) {
    const key = createHash("sha256").update(authorization).digest("hex");
    const credential = credentialKind(authorization.slice("Bearer ".length));
    const known = hosts.get(key);
    if (known) {
      debug("host.resolve", { cache: "hit", host: known, credential });
      return known;
    }
    const missed = missedAt.get(key);
    if (missed !== undefined && now() - missed < missTtlMs) {
      const fromFallback = await fallback(authorization);
      debug("host.resolve", { cache: "recent miss", host: fromFallback, credential });
      return fromFallback;
    }
    const host = askGitHub ? await fromGitHub(authorization) : null;
    if (!host) {
      remember(missedAt, key, now());
      const fromFallback = await fallback(authorization);
      debug("host.resolve", { cache: "miss", askGitHub, host: fromFallback, via: "fallback", credential });
      return fromFallback;
    }
    remember(hosts, key, host);
    missedAt.delete(key);
    debug("host.resolve", { cache: "miss", host, via: "github", credential });
    return host;
  };
}

// The relay for one app session. `token()` mints the Databricks token and `token.invalidate()`
// drops it (a token source from ug-plugin.mjs). `copilotHost(authorization)` finds the caller's
// Copilot API host. `headers` go on every gateway call and `requestHeaders(headers, body)` on one;
// neither can replace the credential, Model Provider Service or Copilot API host headers. Direct
// calls go to `directBase(host)`. `hintFor(error)` words a failed mint. `observe` sees each
// request's route, method, path and outcome, never its headers or body; `log` gets failure lines
// without values.
export function createSeatRelay({
  gatewayBase,
  providerService,
  token,
  copilotHost,
  headers: addedHeaders = {},
  requestHeaders = () => ({}),
  directBase = (host) => `https://${host}`,
  hintFor = () => null,
  log = () => {},
  observe = () => {},
  maxBodyBytes = MAX_BODY_BYTES,
  connectTimeoutMs = CONNECT_TIMEOUT_MS,
  readTimeoutMs = READ_TIMEOUT_MS,
  keepAliveTimeoutMs = KEEP_ALIVE_TIMEOUT_MS,
  env = process.env,
  debug = NO_DEBUG,
  systemProxy = createSystemProxyResolver({ debug }),
}) {
  const gateway = new URL(gatewayBase);
  const secret = randomBytes(32).toString("base64url");
  const secretBytes = Buffer.from(secret);
  debug.addSecret?.(secret);
  const added = withoutReserved(addedHeaders);
  // Custom agents don't read the proxy variables on their own; `proxyEnv` (Node 24.5+) makes
  // them, and an older Node ignores it and connects directly as before. The variables win; without
  // them each upstream gets the proxy the system settings name for it, or none.
  const proxyEnv = proxyEnvironment(env);
  const envProxied = Object.keys(proxyEnv).some((name) => name.toUpperCase() !== "NO_PROXY");
  debug("relay.proxyEnv", {
    variables: Object.keys(proxyEnv),
    hosts: Object.fromEntries(
      Object.entries(proxyEnv).map(([name, value]) => [name, name.toUpperCase() === "NO_PROXY" ? value : hostOf(value)]),
    ),
    gateway: gateway.host,
    source: envProxied ? "env" : "system",
  });
  const pools = new Map();
  function pool(key, agentProxyEnv) {
    if (!pools.has(key)) {
      const options = agentProxyEnv ? { keepAlive: true, proxyEnv: agentProxyEnv } : { keepAlive: true };
      pools.set(key, { "http:": new http.Agent(options), "https:": new https.Agent(options) });
    }
    return pools.get(key);
  }
  async function agentFor(url) {
    if (envProxied) return pool("env", proxyEnv)[url.protocol];
    const { proxy } = await systemProxy(url.origin);
    if (!proxy) return pool("direct", null)[url.protocol];
    return pool(proxy, { HTTP_PROXY: proxy, HTTPS_PROXY: proxy })[url.protocol];
  }
  const upstreamOptions = async (url) => ({ agent: await agentFor(url), connectTimeoutMs, readTimeoutMs });
  const server = http.createServer({
    keepAliveTimeout: keepAliveTimeoutMs,
    headersTimeout: keepAliveTimeoutMs + 5_000,
  });
  let port = 0;

  function sendJson(res, status, message, { close = false, source } = {}) {
    const body = errorBody(message, source);
    const headers = { "content-type": "application/json", "content-length": String(body.length) };
    if (close) headers.connection = "close";
    res.writeHead(status, headers);
    res.end(body);
  }

  function reject(req, res, status, message, label) {
    req.resume();
    observe({ route: "rejected", method: req.method, path: label, status });
    debug("relay.request", { route: "rejected", method: req.method, path: label, status, requestHeaders: Object.keys(req.headers) });
    sendJson(res, status, message);
  }

  // The Copilot API path below the session secret, or null when the secret is missing.
  function relayPath(method, path) {
    if (SECRETLESS_DIRECT_CALLS.has(`${method} ${path}`)) return path;
    const end = path.indexOf("/", 1);
    if (end < 0) return null;
    const candidate = Buffer.from(path.slice(1, end));
    if (candidate.length !== secretBytes.length || !timingSafeEqual(candidate, secretBytes)) {
      return null;
    }
    return path.slice(end);
  }

  function perRequestHeaders(incoming, body) {
    try {
      return withoutReserved(requestHeaders(incoming, body));
    } catch (error) {
      // The headers only label the request, so a failure must not cost the request.
      log(`Databricks seat relay: request headers failed: ${error?.code ?? error?.name ?? "error"}`);
      return {};
    }
  }

  function gatewayHeaders(req, authorization, body, host, databricksToken) {
    const extra = { ...added };
    for (const [name, value] of Object.entries(perRequestHeaders(req.headers, body))) {
      if (!Object.keys(extra).some((existing) => existing.toLowerCase() === name.toLowerCase())) {
        extra[name] = value;
      }
    }
    const strip = new Set([AI_GATEWAY_TOKEN_HEADER, ...Object.keys(extra).map((n) => n.toLowerCase())]);
    const headers = { ...forwardedHeaders(req.headers, strip), ...extra };
    headers.authorization = authorization;
    headers[MODEL_PROVIDER_SERVICE_HEADER] = providerService;
    headers[COPILOT_API_HOST_HEADER] = host;
    headers[AI_GATEWAY_TOKEN_HEADER] = `Bearer ${databricksToken}`;
    // Rewritten error bodies must be readable, and the response is relayed as it comes.
    headers["accept-encoding"] = "identity";
    if (body) headers["content-length"] = String(body.length);
    return headers;
  }

  function directHeaders(req, authorization, body) {
    const headers = forwardedHeaders(req.headers, new Set());
    headers.authorization = authorization;
    if (!headers["accept-encoding"]) headers["accept-encoding"] = "identity";
    if (body) headers["content-length"] = String(body.length);
    return headers;
  }

  // Relays an upstream response as it arrives; a client that goes away (a stopped turn) ends the
  // upstream request, so the seat stops generating.
  function relay(res, response) {
    res.writeHead(response.statusCode, responseHeaders(response));
    response.on("error", () => res.destroy());
    let bytes = 0;
    response.on("data", (chunk) => (bytes += chunk.length));
    response.pipe(res);
    return new Promise((resolve) => {
      res.once("close", () => resolve(bytes));
      response.once("end", () => resolve(bytes));
    });
  }

  async function mintToken(res, outcome) {
    const started = Date.now();
    try {
      const value = await token();
      debug("relay.mint", { ok: true, ms: Date.now() - started, tokenLength: value?.length ?? 0 });
      return value;
    } catch (error) {
      debug("relay.mint", {
        ok: false,
        ms: Date.now() - started,
        code: error?.code ?? null,
        error: String(error?.message ?? error).slice(0, 300),
      });
      const hint = hintFor(error) ?? "Databricks sign-in needed.";
      // Copilot hides the body of a 401 or 403, so the hint needs a status it shows.
      observe({ ...outcome, status: 400, error: "token" });
      sendJson(res, 400, hint);
      return null;
    }
  }

  async function forwardGateway(req, res, { authorization, body, host, call, query, cancel }) {
    const url = new URL(GATEWAY_ROUTES.get(`${req.method} ${call}`) + query, gateway);
    const outcome = { route: "gateway", method: req.method, path: call };
    const started = Date.now();
    const trace = (fields) =>
      debug("relay.request", {
        ...outcome,
        upstream: url.host,
        upstreamPath: url.pathname,
        copilotHost: host,
        requestHeaders: Object.keys(req.headers),
        bytesIn: body?.length ?? 0,
        ms: Date.now() - started,
        ...fields,
      });
    const send = async (databricksToken) =>
      startTracked(cancel, url, {
        method: req.method,
        headers: gatewayHeaders(req, authorization, body, host, databricksToken),
        body,
        ...(await upstreamOptions(url)),
      }).response;
    let databricksToken = await mintToken(res, outcome);
    if (databricksToken === null) {
      trace({ status: 400, error: "token" });
      return;
    }
    try {
      let response = await send(databricksToken);
      let retried = false;
      if (response.statusCode === 407) {
        response.resume();
        observe({ ...outcome, status: 407 });
        trace({ status: 407, clientStatus: 400 });
        sendJson(res, 400, `Unity Gateway couldn't be reached: ${PROXY_AUTH_NEEDED}`);
        return;
      }
      let denial = null;
      if (response.statusCode === 401 || response.statusCode === 403) {
        const body = await readBody(response, GATEWAY_DENIAL_BODY_LIMIT).catch(() => (response.destroy(), null));
        denial = response.statusCode === 403 ? classifyDenial(body) : null;
        if (!denial?.final) {
          // A stale Databricks swap token and a refused GitHub credential look the same from the
          // status alone. Refresh once and retry; a repeat is relayed.
          debug("relay.retry", { ...outcome, status: response.statusCode });
          token.invalidate(databricksToken);
          databricksToken = await mintToken(res, outcome);
          if (databricksToken === null) {
            trace({ status: 400, error: "token", retried: true });
            return;
          }
          response = await send(databricksToken);
          retried = true;
          denial = null;
        }
      }
      if (response.statusCode === 403) {
        if (denial === null) {
          const body = await readBody(response, GATEWAY_DENIAL_BODY_LIMIT).catch(() => (response.destroy(), null));
          denial = classifyDenial(body);
        }
        observe({ ...outcome, status: 403, denial: denial.source });
        trace({ status: 403, retried, clientStatus: 400, denial: denial.source, responseHeaders: debug.headers?.(response.headers) });
        sendJson(res, 400, denial.message, { source: denial.source });
        return;
      }
      observe({ ...outcome, status: response.statusCode });
      const bytesOut = await relay(res, response);
      trace({ status: response.statusCode, retried, bytesOut, responseHeaders: debug.headers?.(response.headers) });
    } catch (error) {
      if (res.headersSent || res.destroyed) {
        trace({ error: String(error?.code ?? error?.message ?? error), aborted: true });
        return;
      }
      if (PROXY_AUTH_ERROR.test(String(error?.message ?? ""))) {
        observe({ ...outcome, status: 407 });
        trace({ status: 407, clientStatus: 400, error: String(error?.message ?? error) });
        sendJson(res, 400, `Unity Gateway couldn't be reached: ${PROXY_AUTH_NEEDED}`);
        return;
      }
      observe({ ...outcome, status: 502, error: error?.code ?? "upstream" });
      trace({ status: 502, error: String(error?.code ?? error?.message ?? error) });
      log(`Databricks seat relay: gateway request failed: ${error?.code ?? error?.name ?? "error"}`);
      sendJson(res, 502, "Unity Gateway couldn't be reached.");
    }
  }

  async function forwardDirect(req, res, { authorization, body, host, call, query, cancel }) {
    const url = new URL(call + query, directBase(host));
    const outcome = { route: "direct", method: req.method, path: call };
    const started = Date.now();
    const trace = (fields) =>
      debug("relay.request", {
        ...outcome,
        upstream: url.host,
        requestHeaders: Object.keys(req.headers),
        bytesIn: body?.length ?? 0,
        ms: Date.now() - started,
        ...fields,
      });
    try {
      const reply = await startTracked(cancel, url, {
        method: req.method,
        headers: directHeaders(req, authorization, body),
        body,
        ...(await upstreamOptions(url)),
      }).response;
      if (reply.statusCode === 407) {
        reply.resume();
        observe({ ...outcome, status: 407 });
        trace({ status: 407, clientStatus: 400 });
        sendJson(res, 400, `GitHub Copilot couldn't be reached: ${PROXY_AUTH_NEEDED}`);
        return;
      }
      observe({ ...outcome, status: reply.statusCode });
      const bytesOut = await relay(res, reply);
      trace({ status: reply.statusCode, bytesOut });
    } catch (error) {
      if (res.headersSent || res.destroyed) {
        trace({ error: String(error?.code ?? error?.message ?? error), aborted: true });
        return;
      }
      if (PROXY_AUTH_ERROR.test(String(error?.message ?? ""))) {
        observe({ ...outcome, status: 407 });
        trace({ status: 407, clientStatus: 400, error: String(error?.message ?? error) });
        sendJson(res, 400, `GitHub Copilot couldn't be reached: ${PROXY_AUTH_NEEDED}`);
        return;
      }
      observe({ ...outcome, status: 502, error: error?.code ?? "upstream" });
      trace({ status: 502, error: String(error?.code ?? error?.message ?? error) });
      log(`Databricks seat relay: direct request failed: ${error?.code ?? error?.name ?? "error"}`);
      sendJson(res, 502, "GitHub Copilot couldn't be reached.");
    }
  }

  async function serve(req, res) {
    const cancel = { current: null, closed: false };
    res.once("close", () => {
      if (res.writableFinished) return;
      cancel.closed = true;
      cancel.current?.destroy();
    });
    const [rawPath, ...rest] = req.url.split("?");
    const query = rest.length ? `?${rest.join("?")}` : "";
    if (!isNormalizedPath(rawPath)) {
      return reject(req, res, 404, `This path is not served by ${RELAY}.`, "path");
    }
    const distinct = req.headersDistinct;
    const hosts = distinct.host ?? [];
    if (hosts.length !== 1 || hosts[0] !== `127.0.0.1:${port}`) {
      return reject(req, res, 403, `Requests must be addressed to ${RELAY}.`, "host");
    }
    if (distinct.origin) {
      return reject(req, res, 403, `Browser requests are not served by ${RELAY}.`, "origin");
    }
    const call = relayPath(req.method, rawPath);
    if (call === null) {
      return reject(req, res, 404, `This path is not served by ${RELAY}.`, "secret");
    }
    if (distinct.upgrade || `${req.method} ${call}` === "GET /responses") {
      // Copilot retries a refused WebSocket as a plain POST /responses.
      return reject(req, res, 426, `WebSockets are not served by ${RELAY}.`, call);
    }
    const authorization = githubAuthorization(distinct);
    if (authorization === null) {
      return reject(req, res, 401, `Sign in to GitHub Copilot to use ${RELAY}.`, call);
    }
    let body;
    try {
      body = await readBody(req, maxBodyBytes);
    } catch (error) {
      if (error?.code !== "BODY_TOO_LARGE") throw error;
      observe({ route: "rejected", method: req.method, path: call, status: 413 });
      res.once("finish", () => req.destroy());
      sendJson(res, 413, `This request is too large for ${RELAY}.`, { close: true });
      return;
    }
    if (body.length === 0 && req.method === "GET") body = null;
    const host = await Promise.resolve()
      .then(() => copilotHost(authorization))
      .catch(() => null);
    if (!host) {
      observe({ route: "rejected", method: req.method, path: call, status: 400, error: "host" });
      debug("relay.request", { route: "rejected", method: req.method, path: call, status: 400, error: "host" });
      sendJson(
        res,
        400,
        "Unity Gateway couldn't find your GitHub Copilot API host. Sign in to GitHub Copilot again, then restart it.",
      );
      return;
    }
    const target = { authorization, body, host, call, query, cancel };
    const gatewayRoute =
      !SECRETLESS_DIRECT_CALLS.has(`${req.method} ${call}`) && GATEWAY_ROUTES.has(`${req.method} ${call}`);
    if (gatewayRoute) await forwardGateway(req, res, target);
    else await forwardDirect(req, res, target);
  }

  server.on("request", (req, res) => {
    serve(req, res).catch((error) => {
      log(`Databricks seat relay: request failed: ${error?.code ?? error?.name ?? "error"}`);
      if (res.headersSent) res.destroy();
      else sendJson(res, 502, "The Unity Gateway Copilot relay hit an internal error.");
    });
  });
  // A WebSocket handshake never reaches the request handler.
  server.on("upgrade", (req, socket) => {
    const body = errorBody(`WebSockets are not served by ${RELAY}.`);
    observe({ route: "rejected", method: req.method, path: "upgrade", status: 426 });
    socket.end(
      `HTTP/1.1 426 Upgrade Required\r\ncontent-type: application/json\r\ncontent-length: ${body.length}\r\nconnection: close\r\n\r\n${body}`,
    );
  });

  return {
    secret,
    agentFor,
    get port() {
      return port;
    },
    get url() {
      return `http://127.0.0.1:${port}/${secret}`;
    },
    listen() {
      return new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(0, "127.0.0.1", () => {
          port = server.address().port;
          resolve(this.url);
        });
      });
    },
    close() {
      for (const agents of pools.values()) for (const agent of Object.values(agents)) agent.destroy();
      return new Promise((resolve) => {
        server.close(() => resolve());
        server.closeAllConnections();
      });
    },
  };
}
