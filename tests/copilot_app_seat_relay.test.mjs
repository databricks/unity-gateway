import assert from "node:assert/strict";
import http from "node:http";
import net from "node:net";
import { setTimeout as delay } from "node:timers/promises";
import { after, before, beforeEach, test } from "node:test";

import {
  classifyDenial,
  AI_GATEWAY_TOKEN_HEADER,
  COPILOT_API_HOST_HEADER,
  GATEWAY_ROUTES,
  MODEL_PROVIDER_SERVICE_HEADER,
  SUBAGENT_ID_HEADER,
  copilotApiHost,
  copilotHostForStatus,
  createCopilotHostResolver,
  createSeatRelay,
  firstUserText,
  gatewayDenialMessage,
  isNormalizedPath,
  loadSystemCertificates,
  subagentHeaders,
} from "../src/ucode/agents/desktop/copilot_app_plugin/app-seat-relay.mjs";

const GITHUB_AUTH = "Bearer gho_the-apps-token";
const HOST = "api.business.githubcopilot.com";
const MPS = "main.default.copilot_seat";

// A recording HTTP upstream: `reply(req, res, body)` answers each request.
class Upstream {
  constructor() {
    this.requests = [];
    this.reply = (req, res) => res.end("ok");
    this.server = http.createServer((req, res) => {
      const chunks = [];
      req.on("data", (chunk) => chunks.push(chunk));
      req.on("end", () => {
        const body = Buffer.concat(chunks);
        const record = { method: req.method, url: req.url, headers: req.headers, body, closed: false };
        res.on("close", () => {
          record.closed = true;
        });
        this.requests.push(record);
        this.reply(req, res, body, record);
      });
    });
  }

  listen() {
    return new Promise((resolve) => this.server.listen(0, "127.0.0.1", resolve));
  }

  get base() {
    return `http://127.0.0.1:${this.server.address().port}`;
  }

  get last() {
    return this.requests.at(-1);
  }

  close() {
    this.server.closeAllConnections();
    return new Promise((resolve) => this.server.close(resolve));
  }
}

function fakeToken(values = ["dbx-1", "dbx-2", "dbx-3"]) {
  let index = 0;
  const token = async () => {
    if (token.fail) throw token.fail;
    return values[Math.min(index, values.length - 1)];
  };
  token.invalidated = 0;
  token.fail = null;
  token.invalidate = () => {
    token.invalidated++;
    index++;
  };
  return token;
}

const gateway = new Upstream();
const copilot = new Upstream();
let relay;
let token;
let observed;
let logged;
let hosts;

before(async () => {
  await Promise.all([gateway.listen(), copilot.listen()]);
});

after(async () => {
  await relay?.close();
  await Promise.all([gateway.close(), copilot.close()]);
});

async function start(options = {}) {
  await relay?.close();
  token = fakeToken();
  observed = [];
  logged = [];
  hosts = [];
  relay = createSeatRelay({
    gatewayBase: gateway.base,
    providerService: MPS,
    token,
    copilotHost: async (authorization) => {
      hosts.push(authorization);
      return HOST;
    },
    headers: { "session-id": "session-1", "User-Agent": "unity-gateway/1.0", "x-tag": "a" },
    requestHeaders: subagentHeaders("session-1", Buffer.alloc(32, 7)),
    directBase: () => copilot.base,
    hintFor: (error) => (error?.code === "ENOENT" ? "ug is missing." : "Run databricks auth login."),
    log: (line) => logged.push(line),
    observe: (event) => observed.push(event),
    env: {},
    ...options,
  });
  await relay.listen();
  return relay;
}

// A forward proxy that records absolute-form requests and relays them to their host.
class FakeProxy {
  constructor() {
    this.requests = [];
    this.server = http.createServer((req, res) => {
      this.requests.push(req.url);
      const target = new URL(req.url);
      const upstream = http.request(
        target,
        { method: req.method, headers: { ...req.headers, host: target.host }, agent: false },
        (response) => {
          res.writeHead(response.statusCode, response.headers);
          response.pipe(res);
        },
      );
      req.pipe(upstream);
    });
  }

  listen() {
    return new Promise((resolve) => this.server.listen(0, "127.0.0.1", resolve));
  }

  get url() {
    return `http://127.0.0.1:${this.server.address().port}`;
  }

  close() {
    this.server.closeAllConnections();
    return new Promise((resolve) => this.server.close(resolve));
  }
}

beforeEach(async () => {
  gateway.requests.length = 0;
  copilot.requests.length = 0;
  gateway.reply = (req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
  };
  copilot.reply = (req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ direct: true }));
  };
  await start();
});

const call = (path, init = {}) =>
  fetch(`${relay.url}${path}`, {
    ...init,
    headers: { authorization: GITHUB_AUTH, ...(init.headers ?? {}) },
  });

const json = async (response) => JSON.parse(await response.text());

// A request written byte by byte, for headers Node's client refuses to send.
function rawSocket(text) {
  return new Promise((resolve) => {
    const socket = net.connect(relay.port, "127.0.0.1", () => socket.write(text));
    let data = "";
    socket.on("data", (chunk) => (data += chunk));
    socket.on("close", () => resolve(data));
    socket.on("error", () => resolve(data));
  });
}

// A request with headers fetch can't send: duplicates, an odd Host, or an Upgrade.
function raw({ method = "GET", path, headers = {}, body }) {
  return new Promise((resolve, reject) => {
    const request = http.request(
      { host: "127.0.0.1", port: relay.port, method, path, headers, setHost: !("host" in headers) },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () =>
          resolve({ status: response.statusCode, headers: response.headers, body: Buffer.concat(chunks).toString() }),
        );
      },
    );
    request.on("error", reject);
    request.end(body);
  });
}

test("the relay's own calls follow the proxy variables, in either case, and NO_PROXY", async (t) => {
  const proxy = new FakeProxy();
  await proxy.listen();
  t.after(() => proxy.close());

  await start({ env: { HTTP_PROXY: proxy.url, HTTPS_PROXY: proxy.url, NO_PROXY: "localhost" } });
  assert.equal((await call("/chat/completions", { method: "POST", body: "{}" })).status, 200);
  assert.equal((await call("/agents/swe/internal/memory/v0/enabled")).status, 200);
  assert.deepEqual(proxy.requests, [`${gateway.base}/ai-gateway/openai/v1/chat/completions`, `${copilot.base}/agents/swe/internal/memory/v0/enabled`]);
  assert.equal(gateway.last.headers.authorization, GITHUB_AUTH);
  assert.equal(gateway.last.headers[AI_GATEWAY_TOKEN_HEADER], "Bearer dbx-1");

  await start({ env: { http_proxy: proxy.url } });
  assert.equal((await call("/meta")).status, 200);
  assert.equal(proxy.requests.length, 3);

  await start({ env: { HTTP_PROXY: proxy.url, no_proxy: ".example,127.0.0.1" } });
  assert.equal((await call("/meta")).status, 200);
  await start({ env: {} });
  assert.equal((await call("/meta")).status, 200);
  assert.equal(proxy.requests.length, 3);
  assert.equal(gateway.requests.length, 4);
});

// A proxy that answers every request with 407.
class AuthProxy {
  constructor() {
    this.server = http.createServer((req, res) => {
      res.writeHead(407, { "proxy-authenticate": "Basic realm=corp", "content-length": "0" });
      res.end();
    });
  }

  listen() {
    return new Promise((resolve) => this.server.listen(0, "127.0.0.1", resolve));
  }

  get url() {
    return `http://127.0.0.1:${this.server.address().port}`;
  }

  close() {
    this.server.closeAllConnections();
    return new Promise((resolve) => this.server.close(resolve));
  }
}

test("without proxy variables each upstream follows the system proxy the OS names for it", async (t) => {
  const corporate = new FakeProxy();
  const other = new FakeProxy();
  await Promise.all([corporate.listen(), other.listen()]);
  t.after(() => Promise.all([corporate.close(), other.close()]));
  const asked = [];
  const systemProxy = async (origin) => {
    asked.push(origin);
    if (origin === new URL(gateway.base).origin) return { proxy: corporate.url, bypassed: false };
    return { proxy: null, bypassed: true };
  };

  await start({ env: {}, systemProxy });
  assert.equal((await call("/chat/completions", { method: "POST", body: "{}" })).status, 200);
  assert.equal((await call("/agents/swe/internal/memory/v0/enabled")).status, 200);
  assert.deepEqual(corporate.requests, [`${gateway.base}/ai-gateway/openai/v1/chat/completions`]);
  assert.equal(copilot.requests.length, 1);
  assert.equal(gateway.last.headers[AI_GATEWAY_TOKEN_HEADER], "Bearer dbx-1");
  assert.deepEqual(new Set(asked), new Set([new URL(gateway.base).origin, new URL(copilot.base).origin]));

  // Two different proxies: two agents, one per proxy.
  await start({
    env: {},
    systemProxy: async (origin) => ({ proxy: origin === new URL(gateway.base).origin ? corporate.url : other.url, bypassed: false }),
  });
  assert.equal((await call("/meta")).status, 200);
  assert.equal((await call("/agents/swe/internal/memory/v0/enabled")).status, 200);
  assert.equal(corporate.requests.length, 2);
  assert.deepEqual(other.requests, [`${copilot.base}/agents/swe/internal/memory/v0/enabled`]);

  // The variables win over the system settings.
  await start({ env: { HTTP_PROXY: other.url }, systemProxy: async () => assert.fail("env proxies decide first") });
  assert.equal((await call("/meta")).status, 200);
  assert.equal(other.requests.length, 2);
});

test("a proxy that wants credentials is a 400 that says so, not a retry", async (t) => {
  const proxy = new AuthProxy();
  await proxy.listen();
  t.after(() => proxy.close());

  await start({ env: {}, systemProxy: async () => ({ proxy: proxy.url, bypassed: false }) });
  const gatewayCall = await call("/chat/completions", { method: "POST", body: "{}" });
  assert.equal(gatewayCall.status, 400);
  assert.match((await json(gatewayCall)).error.message, /system proxy requires authentication/);
  assert.equal(token.invalidated, 0);
  assert.equal(gateway.requests.length, 0);
  const direct = await call("/agents/swe/internal/memory/v0/enabled");
  assert.equal(direct.status, 400);
  assert.match((await json(direct)).error.message, /GitHub Copilot couldn't be reached: the system proxy requires authentication/);
  assert.deepEqual(observed.map((event) => event.status), [407, 407]);
});

test("inference goes to the gateway with the GitHub token, the swap header and the relay headers", async () => {
  const body = JSON.stringify({ model: "gpt-5", input: "hi" });
  const response = await call("/responses", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "copilot-integration-id": "copilot-developer-app",
      "x-databricks-ai-gateway-token": "Bearer forged",
      "databricks-model-provider-service": "other",
      "user-agent": "GitHubCopilotChat/1.0",
      "accept-encoding": "gzip, br",
    },
    body,
  });

  assert.equal(response.status, 200);
  assert.deepEqual(await json(response), { ok: true });
  assert.equal(gateway.requests.length, 1);
  const seen = gateway.last;
  assert.equal(seen.method, "POST");
  assert.equal(seen.url, GATEWAY_ROUTES.get("POST /responses"));
  assert.equal(seen.body.toString(), body);
  assert.equal(seen.headers.authorization, GITHUB_AUTH);
  assert.equal(seen.headers[AI_GATEWAY_TOKEN_HEADER], "Bearer dbx-1");
  assert.equal(seen.headers[MODEL_PROVIDER_SERVICE_HEADER], MPS);
  assert.equal(seen.headers[COPILOT_API_HOST_HEADER], HOST);
  assert.equal(seen.headers["session-id"], "session-1");
  assert.equal(seen.headers["user-agent"], "unity-gateway/1.0");
  assert.equal(seen.headers["x-tag"], "a");
  assert.equal(seen.headers["copilot-integration-id"], "copilot-developer-app");
  assert.equal(seen.headers["accept-encoding"], "identity");
  assert.equal(seen.headers["content-length"], String(Buffer.byteLength(body)));
  assert.equal(seen.headers.host, new URL(gateway.base).host);
  assert.deepEqual(hosts, [GITHUB_AUTH]);
  assert.deepEqual(observed, [{ route: "gateway", method: "POST", path: "/responses", status: 200 }]);
  assert.deepEqual(logged, []);
});

test("every relay route maps to its gateway path, with the query kept", async () => {
  const expected = [
    ["POST", "/chat/completions", "/ai-gateway/openai/v1/chat/completions"],
    ["POST", "/responses", "/ai-gateway/openai/v1/responses"],
    ["POST", "/v1/messages", "/ai-gateway/anthropic/v1/messages"],
    ["GET", "/meta", "/ai-gateway/github-copilot/v1/meta"],
    ["GET", "/models", "/ai-gateway/v1/models"],
    ["POST", "/auto", "/ai-gateway/github-copilot/v1/auto"],
  ];
  assert.deepEqual(
    [...GATEWAY_ROUTES].sort(),
    expected.map(([method, route, path]) => [`${method} ${route}`, path]).sort(),
  );
  for (const [method, route, path] of expected) {
    const key = `${method} ${route}`;
    gateway.requests.length = 0;
    const response = await call(`${route}?a=1&b=2`, { method, body: method === "POST" ? "{}" : undefined });
    assert.equal(response.status, 200, key);
    assert.equal(gateway.last.method, method, key);
    assert.equal(gateway.last.url, `${path}?a=1&b=2`, key);
  }
  assert.equal(copilot.requests.length, 0);
});

test("other calls go direct to the Copilot API host with the runtime's own headers", async () => {
  const response = await call("/agents/swe/internal/memory/v0/enabled?x=1", {
    headers: {
      "x-databricks-ai-gateway-token": "Bearer forged",
      "databricks-anything": "1",
      connection: "keep-alive",
      "user-agent": "GitHubCopilotChat/1.0",
      "accept-encoding": "gzip",
    },
  });

  assert.equal(response.status, 200);
  assert.deepEqual(await json(response), { direct: true });
  assert.equal(gateway.requests.length, 0);
  const seen = copilot.last;
  assert.equal(seen.url, "/agents/swe/internal/memory/v0/enabled?x=1");
  assert.equal(seen.headers.authorization, GITHUB_AUTH);
  assert.equal(seen.headers["user-agent"], "GitHubCopilotChat/1.0");
  assert.equal(seen.headers["accept-encoding"], "gzip");
  for (const name of Object.keys(seen.headers)) {
    assert.ok(!name.startsWith("x-databricks-") && !name.startsWith("databricks-"), name);
  }
  assert.equal(seen.headers["session-id"], undefined);
  assert.deepEqual(observed, [
    { route: "direct", method: "GET", path: "/agents/swe/internal/memory/v0/enabled", status: 200 },
  ]);
});

test("a direct call without an Accept-Encoding asks for identity", async () => {
  const response = await raw({ path: `/${relay.secret}/other`, headers: { authorization: GITHUB_AUTH } });
  assert.equal(response.status, 200);
  assert.equal(copilot.last.headers["accept-encoding"], "identity");
});

test("GitHub MCP arrives without the secret and only ever goes direct", async () => {
  const response = await raw({
    method: "POST",
    path: "/mcp/readonly",
    headers: { authorization: GITHUB_AUTH, "content-type": "application/json" },
    body: "{}",
  });
  assert.equal(response.status, 200);
  assert.equal(copilot.last.url, "/mcp/readonly");
  assert.equal(gateway.requests.length, 0);
  assert.equal(copilot.last.headers[AI_GATEWAY_TOKEN_HEADER], undefined);
});

test("a gateway 401 refreshes the Databricks token and retries once", async () => {
  let attempts = 0;
  gateway.reply = (req, res) => {
    attempts++;
    if (attempts === 1) {
      res.writeHead(401, { "content-type": "application/json" });
      res.end(JSON.stringify({ error_code: "UNAUTHENTICATED", message: "expired" }));
      return;
    }
    res.writeHead(200);
    res.end("fresh");
  };
  const response = await call("/chat/completions", { method: "POST", body: "{}" });

  assert.equal(response.status, 200);
  assert.equal(await response.text(), "fresh");
  assert.equal(token.invalidated, 1);
  assert.deepEqual(
    gateway.requests.map((request) => request.headers[AI_GATEWAY_TOKEN_HEADER]),
    ["Bearer dbx-1", "Bearer dbx-2"],
  );
  assert.deepEqual(observed, [{ route: "gateway", method: "POST", path: "/chat/completions", status: 200 }]);
});

test("a repeated 401 is relayed as it is", async () => {
  gateway.reply = (req, res) => {
    res.writeHead(401, { "content-type": "application/json", "www-authenticate": "Bearer" });
    res.end(JSON.stringify({ error_code: "UNAUTHENTICATED", message: "still expired" }));
  };
  const response = await call("/responses", { method: "POST", body: "{}" });

  assert.equal(response.status, 401);
  assert.equal(response.headers.get("www-authenticate"), "Bearer");
  assert.deepEqual(await json(response), { error_code: "UNAUTHENTICATED", message: "still expired" });
  assert.equal(gateway.requests.length, 2);
  assert.equal(token.invalidated, 1);
});

test("a gateway 403 becomes a 400 carrying the gateway's message", async () => {
  gateway.reply = (req, res) => {
    res.writeHead(403, { "content-type": "application/json" });
    res.end(JSON.stringify({ error_code: "PERMISSION_DENIED", message: "Model gpt-5 is not allowed." }));
  };
  const response = await call("/responses", { method: "POST", body: "{}" });

  assert.equal(response.status, 400);
  assert.deepEqual(await json(response), {
    type: "error",
    error: { type: "invalid_request_error", message: "Model gpt-5 is not allowed.", source: "gateway" },
  });
  // A policy denial is final: no token refresh and no second gateway call.
  assert.equal(gateway.requests.length, 1);
  assert.equal(token.invalidated, 0);
  assert.deepEqual(observed, [
    { route: "gateway", method: "POST", path: "/responses", status: 403, denial: "gateway" },
  ]);
});

test("a 403 that may be a stale token is retried once", async () => {
  gateway.reply = (req, res) => {
    res.writeHead(403, { "content-type": "application/json" });
    res.end(JSON.stringify({ error_code: "UNAUTHENTICATED", message: "expired" }));
  };
  const response = await call("/responses", { method: "POST", body: "{}" });

  assert.equal(response.status, 400);
  assert.equal((await json(response)).error.message, "expired");
  assert.equal(gateway.requests.length, 2);
  assert.equal(token.invalidated, 1);
});

for (const [name, body, type] of [
  ["text", "unauthorized: not authorized to use this Copilot feature\n", "text/plain"],
  [
    "json",
    JSON.stringify({
      message: "unauthorized: not authorized to use this Copilot feature",
      documentation_url: "https://docs.github.com/rest",
    }),
    "application/json",
  ],
]) {
  test(`a GitHub 403 passed through by the gateway reads as GitHub's (${name})`, async () => {
    // The catalog routes pass GitHub's 403 through raw; it is GitHub's policy, not ours.
    gateway.reply = (req, res) => {
      res.writeHead(403, { "content-type": type });
      res.end(body);
    };
    const response = await call("/models");

    assert.equal(response.status, 400);
    assert.deepEqual((await json(response)).error, {
      type: "invalid_request_error",
      message:
        "GitHub Copilot denied this request: unauthorized: not authorized to use this Copilot " +
        "feature. Check your Copilot CLI policy setting at https://github.com/settings/copilot.",
      source: "github",
    });
    assert.equal(gateway.requests.length, 1);
    assert.equal(token.invalidated, 0);
    assert.deepEqual(observed, [{ route: "gateway", method: "GET", path: "/models", status: 403, denial: "github" }]);
  });
}

test("a 403 without a readable error body gets the generic denial, after one retry", async () => {
  gateway.reply = (req, res) => {
    res.writeHead(403, { "content-type": "text/html" });
    res.end("<h1>forbidden</h1>");
  };
  const response = await call("/meta");
  assert.equal(response.status, 400);
  const { error } = await json(response);
  assert.match(error.message, /denied this request/);
  assert.equal(error.source, "unknown");
  assert.equal(gateway.requests.length, 2);
  assert.equal(token.invalidated, 1);
});

test("a failed Databricks mint is a 400 with the hint, before anything reaches the gateway", async () => {
  token.fail = Object.assign(new Error("ug auth-token failed"), { code: 1 });
  const response = await call("/responses", { method: "POST", body: "{}" });

  assert.equal(response.status, 400);
  assert.equal((await json(response)).error.message, "Run databricks auth login.");
  assert.equal(gateway.requests.length, 0);
  assert.deepEqual(observed, [{ route: "gateway", method: "POST", path: "/responses", status: 400, error: "token" }]);
});

test("a mint that fails on the retry gets the hint too", async () => {
  gateway.reply = (req, res) => {
    res.writeHead(401);
    res.end();
  };
  token.invalidate = () => {
    token.fail = Object.assign(new Error("no ug"), { code: "ENOENT" });
  };
  const response = await call("/responses", { method: "POST", body: "{}" });
  assert.equal(response.status, 400);
  assert.equal((await json(response)).error.message, "ug is missing.");
  assert.equal(gateway.requests.length, 1);
});

test("responses stream through as they arrive, without hop-by-hop headers", async () => {
  let ended = false;
  gateway.reply = (req, res) => {
    res.writeHead(200, {
      "content-type": "text/event-stream",
      "keep-alive": "timeout=5",
      trailer: "x-end",
      "x-request-id": "r1",
    });
    res.write("data: first\n\n");
    setTimeout(() => {
      res.end("data: second\n\n");
      ended = true;
    }, 300);
  };
  const response = await call("/responses", { method: "POST", body: "{}" });

  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-request-id"), "r1");
  assert.notEqual(response.headers.get("keep-alive"), "timeout=5");
  assert.equal(response.headers.get("trailer"), null);
  const reader = response.body.getReader();
  const first = await reader.read();
  assert.equal(Buffer.from(first.value).toString(), "data: first\n\n");
  assert.equal(ended, false);
  let rest = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    rest += Buffer.from(value).toString();
  }
  assert.equal(rest, "data: second\n\n");
});

test("a client that stops mid-stream ends the gateway request", async () => {
  let upstreamResponse;
  gateway.reply = (req, res) => {
    upstreamResponse = res;
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.write("data: first\n\n");
  };
  const controller = new AbortController();
  const response = await call("/responses", { method: "POST", body: "{}", signal: controller.signal });
  const reader = response.body.getReader();
  await reader.read();
  controller.abort();

  for (let i = 0; i < 50 && !gateway.last.closed; i++) await delay(20);
  assert.equal(gateway.last.closed, true);
  upstreamResponse.end();
});

async function waitFor(until) {
  for (let waited = 0; !until() && waited < 1000; waited += 10) await delay(10);
}

function gate() {
  let open;
  const opened = new Promise((resolve) => (open = resolve));
  return { opened, open, waiting: false };
}

async function abandon(path, init, isWaiting) {
  const controller = new AbortController();
  const pending = call(path, { ...init, signal: controller.signal }).catch(() => null);
  await waitFor(isWaiting);
  assert.equal(isWaiting(), true);
  controller.abort();
  await pending;
  await delay(50);
}

test("a client that goes away during the host lookup starts no upstream request", async () => {
  for (const [path, init] of [
    ["/chat/completions", { method: "POST", body: "{}" }],
    ["/_ping", { method: "GET" }],
  ]) {
    const lookup = gate();
    await start({
      copilotHost: async () => {
        lookup.waiting = true;
        await lookup.opened;
        return HOST;
      },
    });
    await abandon(path, init, () => lookup.waiting);
    lookup.open();
    await delay(50);
    assert.equal(gateway.requests.length + copilot.requests.length, 0, path);
    assert.deepEqual(logged, [], path);
  }
});

test("a client that goes away during a mint starts no gateway request", async () => {
  for (const gatedCall of [1, 2]) {
    const mint = gate();
    let calls = 0;
    const gatedToken = async () => {
      calls++;
      if (calls === gatedCall) {
        mint.waiting = true;
        await mint.opened;
      }
      return `dbx-${calls}`;
    };
    gatedToken.invalidate = () => {};
    await start({ token: gatedToken });
    gateway.requests.length = 0;
    gateway.reply = (req, res) => {
      res.writeHead(401, { "content-type": "application/json" });
      res.end(JSON.stringify({ error_code: "UNAUTHENTICATED", message: "expired" }));
    };
    await abandon("/chat/completions", { method: "POST", body: "{}" }, () => mint.waiting);
    mint.open();
    await delay(50);
    assert.equal(gateway.requests.length, gatedCall - 1, `mint ${gatedCall}`);
    assert.deepEqual(logged, [], `mint ${gatedCall}`);
  }
});

test("an unreachable gateway is a 502", async () => {
  const closed = new Upstream();
  await closed.listen();
  const base = closed.base;
  await closed.close();
  await start({ gatewayBase: base });

  const response = await call("/responses", { method: "POST", body: "{}" });
  assert.equal(response.status, 502);
  assert.match((await json(response)).error.message, /Unity Gateway couldn't be reached/);
  assert.equal(observed.at(-1).status, 502);
  assert.equal(observed.at(-1).error, "ECONNREFUSED");
  assert.equal(logged.length, 1);
  assert.ok(!logged[0].includes(base), logged[0]);
});

test("an unreachable Copilot host is a 502", async () => {
  await start({ directBase: () => "http://127.0.0.1:1" });
  const response = await call("/other");
  assert.equal(response.status, 502);
  assert.match((await json(response)).error.message, /GitHub Copilot couldn't be reached/);
});

test("a gateway that never answers times out into a 502", async () => {
  gateway.reply = () => {};
  await start({ readTimeoutMs: 200 });
  const response = await call("/meta");
  assert.equal(response.status, 502);
});

test("a body over the limit is a 413", async () => {
  await start({ maxBodyBytes: 16 });
  const response = await call("/responses", { method: "POST", body: "x".repeat(17) });
  assert.equal(response.status, 413);
  assert.equal(gateway.requests.length, 0);
  assert.deepEqual(observed, [{ route: "rejected", method: "POST", path: "/responses", status: 413 }]);
});

test("the wrong or missing secret is a 404", async () => {
  for (const path of [
    "/not-the-secret/meta",
    `/${relay.secret}`,
    `/${relay.secret.slice(0, -1)}x/meta`,
    "/meta",
    "/mcp/readonly",
  ]) {
    const response = await raw({ path, headers: { authorization: GITHUB_AUTH } });
    assert.equal(response.status, 404, path);
  }
  assert.equal(gateway.requests.length + copilot.requests.length, 0);
});

test("paths that aren't normalized are a 404 before anything else", async () => {
  for (const path of [
    `/${relay.secret}/../meta`,
    `/${relay.secret}//meta`,
    `/${relay.secret}/./meta`,
    `/${relay.secret}/%6deta`,
    `/${relay.secret}/meta#frag`,
    `/${relay.secret}/me\\ta`,
    `/${relay.secret}/meta/`,
  ]) {
    const response = await raw({ path, headers: { authorization: GITHUB_AUTH } });
    assert.equal(response.status, 404, path);
  }
  assert.equal(gateway.requests.length + copilot.requests.length, 0);
});

test("a request not addressed to the relay, or from a browser origin, is a 403", async () => {
  const path = `/${relay.secret}/meta`;
  const wrongHost = await raw({ path, headers: { authorization: GITHUB_AUTH, host: "localhost:1" } });
  assert.equal(wrongHost.status, 403);
  const host = `127.0.0.1:${relay.port}`;
  const twoHosts = await rawSocket(
    `GET ${path} HTTP/1.1\r\nHost: ${host}\r\nHost: ${host}\r\nAuthorization: ${GITHUB_AUTH}\r\nConnection: close\r\n\r\n`,
  );
  assert.match(twoHosts, /^HTTP\/1\.1 403 /);
  const browser = await raw({ path, headers: { authorization: GITHUB_AUTH, origin: "https://evil.example" } });
  assert.equal(browser.status, 403);
  assert.equal(gateway.requests.length, 0);
});

test("WebSockets and GET /responses are a 426", async () => {
  const get = await call("/responses");
  assert.equal(get.status, 426);
  const upgrade = await rawSocket(
    `GET /${relay.secret}/responses HTTP/1.1\r\nHost: 127.0.0.1:${relay.port}\r\nAuthorization: ${GITHUB_AUTH}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n`,
  );
  assert.match(upgrade, /^HTTP\/1\.1 426 /);
  assert.equal(gateway.requests.length, 0);
});

test("a missing, duplicate or non-GitHub credential is a 401", async () => {
  const path = `/${relay.secret}/meta`;
  assert.equal((await raw({ path })).status, 401);
  assert.equal((await raw({ path, headers: { authorization: "Bearer dapi-databricks" } })).status, 401);
  assert.equal((await raw({ path, headers: { authorization: "Basic Z2hvXzE=" } })).status, 401);
  assert.equal((await raw({ path, headers: { authorization: [GITHUB_AUTH, GITHUB_AUTH] } })).status, 401);
  assert.equal(gateway.requests.length, 0);
  assert.deepEqual(hosts, []);
});

test("the bearer scheme is read case-insensitively and normalized", async () => {
  const response = await raw({ path: `/${relay.secret}/meta`, headers: { authorization: "bearer gho_x" } });
  assert.equal(response.status, 200);
  assert.equal(gateway.last.headers.authorization, "Bearer gho_x");
});

test("an unknown Copilot API host is a 400", async () => {
  await start({ copilotHost: async () => null });
  const response = await call("/meta");
  assert.equal(response.status, 400);
  assert.match((await json(response)).error.message, /couldn't find your GitHub Copilot API host/);
  assert.equal(gateway.requests.length, 0);
});

test("a host resolver that throws is a 400, not a crash", async () => {
  await start({
    copilotHost: async () => {
      throw new Error("rpc down");
    },
  });
  assert.equal((await call("/meta")).status, 400);
});

test("a subagent request carries a thread id; the user's headers can't take the reserved names", async () => {
  const body = JSON.stringify({ input: [{ role: "user", content: "delegated task" }] });
  await call("/responses", {
    method: "POST",
    headers: { "x-interaction-type": "conversation-subagent", [SUBAGENT_ID_HEADER]: "spoofed" },
    body,
  });
  const expected = subagentHeaders("session-1", Buffer.alloc(32, 7))(
    { "x-interaction-type": "conversation-subagent" },
    Buffer.from(body),
  )[SUBAGENT_ID_HEADER];
  assert.match(expected, /^session-1-[0-9a-f]{16}$/);
  assert.equal(gateway.last.headers[SUBAGENT_ID_HEADER], expected);

  await start({
    headers: {
      authorization: "Bearer stolen",
      [AI_GATEWAY_TOKEN_HEADER]: "Bearer stolen",
      [MODEL_PROVIDER_SERVICE_HEADER]: "other",
      [COPILOT_API_HOST_HEADER]: "evil",
      "x-ok": "1",
    },
    requestHeaders: () => ({ authorization: "Bearer stolen", "x-ok": "2", "x-per-request": "3" }),
  });
  await call("/meta");
  const seen = gateway.last.headers;
  assert.equal(seen.authorization, GITHUB_AUTH);
  assert.equal(seen[AI_GATEWAY_TOKEN_HEADER], "Bearer dbx-1");
  assert.equal(seen[MODEL_PROVIDER_SERVICE_HEADER], MPS);
  assert.equal(seen[COPILOT_API_HOST_HEADER], HOST);
  assert.equal(seen["x-ok"], "1");
  assert.equal(seen["x-per-request"], "3");
});

test("a request-headers adder that throws costs nothing but a log line", async () => {
  await start({
    requestHeaders: () => {
      throw new Error("boom");
    },
  });
  assert.equal((await call("/meta")).status, 200);
  assert.equal(logged.length, 1);
  assert.ok(!logged[0].includes("boom"));
});

test("subagentHeaders marks only subagent requests with a first user message", () => {
  const key = Buffer.alloc(32, 1);
  const headers = subagentHeaders("s", key);
  const body = Buffer.from(JSON.stringify({ messages: [{ role: "system", content: "x" }, { role: "user", content: "p" }] }));
  const id = headers({ "x-interaction-type": "conversation-subagent" }, body)[SUBAGENT_ID_HEADER];
  assert.match(id, /^s-[0-9a-f]{16}$/);
  assert.equal(headers({ "x-interaction-type": "conversation-subagent" }, body)[SUBAGENT_ID_HEADER], id);
  assert.notEqual(subagentHeaders("s", Buffer.alloc(32, 2))({ "x-interaction-type": "conversation-subagent" }, body)[SUBAGENT_ID_HEADER], id);
  assert.deepEqual(headers({ "x-interaction-type": "conversation-agent" }, body), {});
  assert.deepEqual(headers({ "x-interaction-type": "conversation-subagent" }, Buffer.from("{}")), {});
  assert.deepEqual(headers({ "x-interaction-type": "conversation-subagent" }, Buffer.from("not json")), {});
});

test("firstUserText reads Messages, Chat Completions and Responses bodies", () => {
  const text = (payload) => firstUserText(Buffer.from(JSON.stringify(payload)));
  assert.equal(text({ messages: [{ role: "user", content: "hi" }] }), "hi");
  assert.equal(text({ input: "plain" }), "plain");
  assert.equal(text({ input: [{ role: "user", content: [{ type: "input_text", text: "a" }, { type: "input_image" }, { text: "b" }] }] }), "a\nb");
  assert.equal(text({ input: [{ role: "user", content: [{ type: "input_image" }] }] }), null);
  assert.equal(text({ input: [{ role: "assistant", content: "x" }] }), null);
  assert.equal(text([]), null);
  assert.equal(text({}), null);
  assert.equal(firstUserText(null), null);
});

test("copilotApiHost accepts only GitHub's Copilot API hosts over plain HTTPS", () => {
  assert.equal(copilotApiHost("https://api.githubcopilot.com"), "api.githubcopilot.com");
  assert.equal(copilotApiHost("https://api.individual.githubcopilot.com/"), "api.individual.githubcopilot.com");
  assert.equal(copilotApiHost("https://api.business.githubcopilot.com:443"), "api.business.githubcopilot.com");
  assert.equal(copilotApiHost("https://copilot-api.acme-corp.ghe.com"), "copilot-api.acme-corp.ghe.com");
  for (const bad of [
    "http://api.githubcopilot.com",
    "https://api.githubcopilot.com:8443",
    "https://user@api.githubcopilot.com",
    "https://api.githubcopilot.com/v1",
    "https://api.githubcopilot.com?x=1",
    "https://api.githubcopilot.com#f",
    "https://api.githubcopilot.com.evil.example",
    "https://copilot-api.-bad.ghe.com",
    "https://evil.example",
    42,
    null,
    "not a url",
  ]) {
    assert.equal(copilotApiHost(bad), null, String(bad));
  }
});

test("copilotHostForStatus maps the signed-in account and plan to a Copilot API host", () => {
  assert.equal(copilotHostForStatus({ host: "github.com", copilotPlan: "individual" }), "api.individual.githubcopilot.com");
  assert.equal(copilotHostForStatus({ host: "https://github.com/", copilotPlan: "Business" }), "api.business.githubcopilot.com");
  assert.equal(copilotHostForStatus({ copilotPlan: "enterprise" }), "api.enterprise.githubcopilot.com");
  assert.equal(copilotHostForStatus({ host: "https://acme.ghe.com", copilotPlan: "enterprise" }), "copilot-api.acme.ghe.com");
  assert.equal(copilotHostForStatus({ host: "github.example.com", copilotPlan: "enterprise" }), null);
  for (const plan of ["individual_pro", "individual_pro_plus", "individual_max", "free_trial", "student", "edu"]) {
    assert.equal(copilotHostForStatus({ host: "github.com", copilotPlan: plan }), "api.individual.githubcopilot.com", plan);
  }
  assert.equal(copilotHostForStatus({ host: "github.com", copilotPlan: "business_plus" }), "api.business.githubcopilot.com");
  assert.equal(copilotHostForStatus({ host: "github.com", copilotPlan: "unknown" }), null);
  assert.equal(copilotHostForStatus({ host: "github.com" }), null);
  assert.equal(copilotHostForStatus(null), null);
});

test("isNormalizedPath rejects escapes, fragments, backslashes and dot or empty segments", () => {
  assert.equal(isNormalizedPath("/a/b"), true);
  for (const bad of ["a/b", "/", "/a//b", "/a/./b", "/a/../b", "/a/%2e/b", "/a#b", "/a\\b", "/a/", 7]) {
    assert.equal(isNormalizedPath(bad), false, String(bad));
  }
});

test("classifyDenial tells a Databricks, a GitHub and an unreadable 403 body apart", () => {
  const dbx = classifyDenial(Buffer.from(JSON.stringify({ error_code: "PERMISSION_DENIED", message: "no" })));
  assert.deepEqual(dbx, { source: "gateway", message: "no", final: true });
  assert.equal(classifyDenial(Buffer.from(JSON.stringify({ error_code: "UNAUTHENTICATED", message: "x" }))).final, false);
  const github = classifyDenial(Buffer.from("unauthorized:  not authorized to use this Copilot feature.\n"));
  assert.equal(github.source, "github");
  assert.equal(github.final, true);
  assert.match(github.message, /^GitHub Copilot denied this request: unauthorized: not authorized to use this Copilot feature\. Check/);
  assert.equal(classifyDenial(Buffer.from(JSON.stringify({ message: "no code" }))).source, "github");
  const long = classifyDenial(Buffer.from("x".repeat(400))).message;
  assert.equal(long.includes("x".repeat(300)) && !long.includes("x".repeat(301)), true);
  for (const bad of [null, Buffer.alloc(0), Buffer.from("<html>"), Buffer.from("line\ntwo"), Buffer.from(JSON.stringify({ error_code: 7, message: "m" }))]) {
    assert.deepEqual(classifyDenial(bad), { source: "unknown", message: gatewayDenialMessage(null), final: false }, String(bad));
  }
});

test("gatewayDenialMessage reads a Databricks error body and falls back otherwise", () => {
  assert.equal(gatewayDenialMessage(Buffer.from(JSON.stringify({ error_code: "X", message: "no" }))), "no");
  const generic = gatewayDenialMessage(null);
  assert.match(generic, /denied this request/);
  assert.equal(gatewayDenialMessage(Buffer.from(JSON.stringify({ error_code: "X", message: "" }))), generic);
  assert.equal(gatewayDenialMessage(Buffer.from("<html>")), generic);
  assert.equal(gatewayDenialMessage(Buffer.alloc(65 * 1024, 32)), generic);
});

test("the host resolver asks GitHub once per credential, with a miss TTL and the fallback", async () => {
  const github = new Upstream();
  await github.listen();
  let answer = () => ({ endpoints: { api: "https://api.enterprise.githubcopilot.com" } });
  github.reply = (req, res) => {
    const payload = answer();
    if (payload === null) {
      res.writeHead(401);
      res.end();
      return;
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(payload));
  };
  let clock = 0;
  const fallbacks = [];
  const resolve = createCopilotHostResolver({
    userUrl: `${github.base}/copilot_internal/user`,
    userAgent: "unity-gateway/test",
    now: () => clock,
    missTtlMs: 1000,
    fallback: async (authorization) => {
      fallbacks.push(authorization);
      return "api.individual.githubcopilot.com";
    },
  });
  try {
    assert.equal(await resolve("Bearer gho_a"), "api.enterprise.githubcopilot.com");
    assert.equal(await resolve("Bearer gho_a"), "api.enterprise.githubcopilot.com");
    assert.equal(github.requests.length, 1);
    assert.equal(github.last.headers.authorization, "Bearer gho_a");
    assert.equal(github.last.headers["user-agent"], "unity-gateway/test");
    assert.equal(github.last.headers.accept, "application/json");

    answer = () => null;
    assert.equal(await resolve("Bearer gho_b"), "api.individual.githubcopilot.com");
    assert.equal(await resolve("Bearer gho_b"), "api.individual.githubcopilot.com");
    assert.equal(github.requests.length, 2);
    assert.deepEqual(fallbacks, ["Bearer gho_b", "Bearer gho_b"]);
    clock = 1000;
    answer = () => ({ endpoints: { api: "https://api.business.githubcopilot.com" } });
    assert.equal(await resolve("Bearer gho_b"), "api.business.githubcopilot.com");
    assert.equal(github.requests.length, 3);

    answer = () => ({ endpoints: { api: "https://evil.example" } });
    assert.equal(await resolve("Bearer gho_c"), "api.individual.githubcopilot.com");
    assert.equal(github.requests.length, 4);

    assert.equal(await resolve("Bearer tid=vscode"), "api.individual.githubcopilot.com");
    assert.equal(github.requests.length, 4);
  } finally {
    await github.close();
  }
});

test("the host resolver can skip GitHub and use only the fallback", async () => {
  const resolve = createCopilotHostResolver({
    askGitHub: false,
    userUrl: "http://127.0.0.1:1/never",
    fallback: async () => "api.business.githubcopilot.com",
  });
  assert.equal(await resolve("Bearer gho_a"), "api.business.githubcopilot.com");
});

test("the host resolver treats an unreachable GitHub as a miss", async () => {
  const resolve = createCopilotHostResolver({ userUrl: "http://127.0.0.1:1/user", fallback: async () => null });
  assert.equal(await resolve("Bearer gho_a"), null);
});

test("the host resolver remembers a bounded number of credentials", async () => {
  const github = new Upstream();
  await github.listen();
  github.reply = (req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ endpoints: { api: "https://api.business.githubcopilot.com" } }));
  };
  const resolve = createCopilotHostResolver({ userUrl: `${github.base}/user`, maxEntries: 2 });
  try {
    for (const credential of ["gho_a", "gho_b", "gho_c", "gho_c", "gho_b"]) {
      assert.equal(await resolve(`Bearer ${credential}`), HOST);
    }
    assert.equal(github.requests.length, 3);
    assert.equal(await resolve("Bearer gho_a"), HOST);
    assert.equal(github.requests.length, 4);
  } finally {
    await github.close();
  }
});

test("the host resolver drops an oversized GitHub answer's connection", async () => {
  const github = new Upstream();
  await github.listen();
  github.reply = (req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.write(Buffer.alloc(70 * 1024, 32));
  };
  const resolve = createCopilotHostResolver({
    userUrl: `${github.base}/user`,
    timeoutMs: 5000,
    fallback: async () => null,
  });
  try {
    assert.equal(await resolve("Bearer gho_a"), null);
    for (let waited = 0; !github.last.closed && waited < 1000; waited += 10) await delay(10);
    assert.equal(github.last.closed, true);
  } finally {
    await github.close();
  }
});

test("loadSystemCertificates merges the system store once the API exists", () => {
  let set = null;
  const modern = {
    getCACertificates: (kind) => (kind === "default" ? ["A", "B"] : ["B", "C"]),
    setDefaultCACertificates: (certificates) => {
      set = certificates;
    },
  };
  assert.equal(loadSystemCertificates(modern), true);
  assert.deepEqual(set, ["A", "B", "C"]);
  assert.equal(loadSystemCertificates({}), false);
  assert.equal(
    loadSystemCertificates({
      getCACertificates: () => {
        throw new Error("no store");
      },
      setDefaultCACertificates: () => {},
    }),
    false,
  );
});

test("the relay listens on a fresh loopback port under a per-launch secret", async () => {
  assert.match(relay.url, new RegExp(`^http://127\\.0\\.0\\.1:${relay.port}/${relay.secret}$`));
  assert.equal(relay.secret.length, 43);
  const other = createSeatRelay({ gatewayBase: gateway.base, providerService: MPS, token, copilotHost: async () => HOST });
  await other.listen();
  try {
    assert.notEqual(other.secret, relay.secret);
    assert.notEqual(other.port, relay.port);
  } finally {
    await other.close();
  }
});
