import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdtempSync, readFileSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { after, before, test } from "node:test";

import {
  REQUESTS_FILE,
  REVERT_TIMEOUT_MS,
  SANDBOX_ENV,
  SESSIONS_FILE,
  clearRedirect,
  createSeatRedirect,
  keepAliveOnErrors,
  noteRedirect,
  redirectEnabled,
  redirectSkipReason,
  seatUserAgent,
} from "../src/ucode/agents/desktop/copilot_app_plugin/app-seat-redirect.mjs";
import { detectLoopbackProxy } from "../src/ucode/agents/desktop/copilot_app_plugin/app-proxy.mjs";

const GITHUB_AUTH = "Bearer gho_app";
const gateway = { requests: [], status: 200 };
const server = http.createServer((req, res) => {
  const chunks = [];
  req.on("data", (chunk) => chunks.push(chunk));
  req.on("end", () => {
    gateway.requests.push({ url: req.url, headers: req.headers, body: Buffer.concat(chunks).toString() });
    res.writeHead(gateway.status, { "content-type": "application/json" });
    res.end("{}");
  });
});

before(() => new Promise((resolve) => server.listen(0, "127.0.0.1", resolve)));
after(() => new Promise((resolve) => server.close(resolve)));

function config(overrides = {}) {
  return {
    models: [{ id: "system.ai.claude-sonnet-4-6" }],
    loginHint: "Run databricks auth login.",
    missingUgHint: "ug is missing.",
    seatRelay: {
      gatewayBase: `http://127.0.0.1:${server.address().port}`,
      providerService: "main.default.copilot_seat",
      askGitHub: false,
      headers: { "x-tag": "a" },
      userAgent: "ucode/1.0 (macos 15; arm64)",
    },
    ...overrides,
  };
}

function fakeSession({ update, status = { host: "github.com", copilotPlan: "business" } } = {}) {
  const updates = [];
  return {
    updates,
    rpc: {
      options: {
        update: update ?? (async (options) => (updates.push(options), { success: true })),
      },
      gitHubAuth: { getStatus: async () => status },
    },
  };
}

async function start(t, overrides = {}, options = {}) {
  const entries = [];
  const requests = [];
  const logged = [];
  const token = async () => "dbx";
  token.invalidate = () => {};
  const redirect = createSeatRedirect(config(overrides), {
    tokenSource: token,
    log: (line) => logged.push(line),
    sessionId: "session-1",
    env: {},
    now: () => 1_700_000_000_000,
    record: (entry) => entries.push(entry),
    recordRequest: (entry) => requests.push(entry),
    proxyCheck: async () => ({ captured: false, reason: null }),
    ...options,
  });
  t.after(() => redirect?.close());
  gateway.requests.length = 0;
  gateway.status = 200;
  return { redirect, entries, requests, logged };
}

const RUNTIME_UA = "copilot/1.0.106-8 term/unknown client/github-app (darwin; arm64)";
const infer = (url) =>
  fetch(`${url}/responses`, {
    method: "POST",
    headers: { authorization: GITHUB_AUTH, "content-type": "application/json", "user-agent": RUNTIME_UA },
    body: JSON.stringify({ model: "gpt-5", input: "hi" }),
  });

test("redirectEnabled needs the seatRelay config, no remove mode and no sandbox", () => {
  const on = config();
  assert.equal(redirectEnabled(on, {}), true);
  assert.equal(redirectEnabled({ ...on, seatRelay: null }, {}), false);
  assert.equal(redirectEnabled({ ...on, seatRelay: { gatewayBase: "https://ws" } }, {}), false);
  assert.equal(redirectEnabled({ ...on, remove: true }, {}), false);
  assert.equal(redirectEnabled(on, { [SANDBOX_ENV]: "1" }), false);
  assert.equal(redirectEnabled(on, { [SANDBOX_ENV]: "0" }), true);
  assert.equal(redirectEnabled({}, {}), false);
});

test("seatUserAgent puts the runtime's products between ug's product and platform", () => {
  assert.equal(
    seatUserAgent("ucode/1.0 (linux ubuntu-24.04; x86_64)", RUNTIME_UA),
    "ucode/1.0 copilot/1.0.106-8 term/unknown client/github-app (linux ubuntu-24.04; x86_64)",
  );
  assert.equal(seatUserAgent("ucode/1.0 (linux; x86_64)", "GitHubCopilotChat/0.35 (x)"), "ucode/1.0 copilot/unknown GitHubCopilotChat/0.35 (linux; x86_64)");
  assert.equal(seatUserAgent("ucode/1.0 (linux; x86_64)", undefined), "ucode/1.0 copilot/unknown (linux; x86_64)");
  assert.equal(seatUserAgent(undefined, "copilot/2"), "ucode/unknown copilot/2");
});

test("redirectSkipReason tells the sandbox apart from nothing to route", () => {
  const on = config();
  assert.equal(redirectSkipReason(on, {}), null);
  assert.equal(redirectSkipReason(on, { [SANDBOX_ENV]: "1" }), "sandboxed");
  assert.equal(redirectSkipReason(on, { [SANDBOX_ENV]: "0" }), null);
  assert.equal(redirectSkipReason(on, { [SANDBOX_ENV]: "" }), null);
  assert.equal(redirectSkipReason({ ...on, remove: true }, { [SANDBOX_ENV]: "1" }), "off");
  assert.equal(redirectSkipReason({}, { [SANDBOX_ENV]: "1" }), "off");
});

test("noteRedirect records a start that couldn't redirect in the sessions file", async () => {
  const stateDir = mkdtempSync(join(tmpdir(), "ug-seat-redirect-"));
  await noteRedirect(stateDir, { redirect: "sandboxed" }, { sessionId: "s1", now: () => 1_700_000_000_000 });
  await noteRedirect(stateDir, { redirect: "not started", error: "EACCES" }, { sessionId: "s2", now: () => 1_700_000_001_000 });
  assert.deepEqual(JSON.parse(readFileSync(join(stateDir, SESSIONS_FILE), "utf8")), [
    { time: "2023-11-14T22:13:20.000Z", sessionId: "s1", redirect: "sandboxed" },
    { time: "2023-11-14T22:13:21.000Z", sessionId: "s2", redirect: "not started", error: "EACCES" },
  ]);
});

test("createSeatRedirect is null when the redirect is off", async (t) => {
  const { redirect } = await start(t, { seatRelay: undefined });
  assert.equal(redirect, null);
});

test("apply points the session at the relay, and its calls reach the gateway with the seat headers", async (t) => {
  const { redirect, entries } = await start(t);
  await redirect.listen();
  const session = fakeSession();

  assert.equal(await redirect.apply(session), true);
  assert.deepEqual(session.updates, [{ copilotUrl: redirect.url }]);
  assert.match(redirect.url, /^http:\/\/127\.0\.0\.1:\d+\/[A-Za-z0-9_-]{43}$/);

  const response = await infer(redirect.url);
  assert.equal(response.status, 200);
  const [seen] = gateway.requests;
  assert.equal(seen.url, "/ai-gateway/openai/v1/responses");
  assert.equal(seen.headers.authorization, GITHUB_AUTH);
  assert.equal(seen.headers["x-databricks-ai-gateway-token"], "Bearer dbx");
  assert.equal(seen.headers["databricks-model-provider-service"], "main.default.copilot_seat");
  assert.equal(seen.headers["x-databricks-github-copilot-api-host"], "api.business.githubcopilot.com");
  assert.equal(seen.headers["session-id"], "session-1");
  assert.equal(seen.headers["user-agent"], "ucode/1.0 copilot/1.0.106-8 term/unknown client/github-app (macos 15; arm64)");
  assert.equal(seen.headers["x-tag"], "a");
  assert.deepEqual(entries, []);
});

test("the GitHub host lookup carries ug's User-Agent, with configure's header shape", async (t) => {
  const github = { requests: [] };
  const server = http.createServer((req, res) => {
    github.requests.push(req.headers);
    if (!req.headers["user-agent"]) {
      res.writeHead(403, { "content-type": "application/json" });
      res.end(JSON.stringify({ message: "Request forbidden by administrative rules." }));
      return;
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ endpoints: { api: "https://api.enterprise.githubcopilot.com" } }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => server.close());
  const { redirect } = await start(
    t,
    { seatRelay: { ...config().seatRelay, askGitHub: true, headers: { "x-databricks-use-coding-agent-mode": "true" } } },
    { githubUserUrl: `http://127.0.0.1:${server.address().port}/copilot_internal/user` },
  );
  await redirect.listen();
  await redirect.apply(fakeSession({ status: { host: "github.com", copilotPlan: "unknown" } }));

  const response = await infer(redirect.url);
  assert.equal(response.status, 200);
  assert.equal(github.requests.length, 1);
  assert.match(github.requests[0]["user-agent"], /^ucode\/1\.0 copilot\/app \(macos 15; arm64\)$/);
  assert.equal(github.requests[0].authorization, GITHUB_AUTH);
  assert.equal(gateway.requests[0].headers["x-databricks-github-copilot-api-host"], "api.enterprise.githubcopilot.com");
});

test("a paid individual seat resolves to the individual host from the sign-in status", async (t) => {
  const { redirect } = await start(t);
  await redirect.listen();
  await redirect.apply(fakeSession({ status: { host: "github.com", copilotPlan: "individual_pro" } }));
  assert.equal((await infer(redirect.url)).status, 200);
  assert.equal(gateway.requests[0].headers["x-databricks-github-copilot-api-host"], "api.individual.githubcopilot.com");
});

test("a session whose GitHub status gives no Copilot host gets the 400 hint", async (t) => {
  const { redirect } = await start(t);
  await redirect.listen();
  await redirect.apply(fakeSession({ status: { host: "github.com", copilotPlan: "unknown" } }));

  const response = await infer(redirect.url);
  assert.equal(response.status, 400);
  assert.match((await response.json()).error.message, /couldn't find your GitHub Copilot API host/);
  assert.deepEqual(gateway.requests, []);
});

test("a redirect the app refuses or fails is recorded as unavailable", async (t) => {
  const refused = await start(t);
  await refused.redirect.listen();
  assert.equal(await refused.redirect.apply(fakeSession({ update: async () => ({ success: false }) })), false);
  assert.deepEqual(refused.entries, [
    { time: "2023-11-14T22:13:20.000Z", sessionId: "session-1", redirect: "unavailable", proxyMs: 0, builtInCalls: 0, relayedCalls: 0 },
  ]);
  assert.deepEqual(refused.logged, []);

  const failed = await start(t);
  await failed.redirect.listen();
  const thrown = async () => {
    throw new Error("Unknown option");
  };
  assert.equal(await failed.redirect.apply(fakeSession({ update: thrown })), false);
  assert.equal(failed.entries[0].redirect, "unavailable");
  assert.match(failed.logged[0], /didn't take the redirect: Unknown option/);
});

test("after a turn, the session's built-in and relayed call counts are recorded once", async (t) => {
  const { redirect, entries } = await start(t);
  await redirect.listen();
  await redirect.apply(fakeSession());

  redirect.handle({ type: "model.call_start", data: { model: "gpt-5", turnId: "t1" } });
  redirect.handle({ type: "model.call_start", data: { model: "system.ai.claude-sonnet-4-6", turnId: "t1" } });
  await infer(redirect.url);
  gateway.status = 500;
  await infer(redirect.url);
  redirect.handle({ type: "session.idle", data: {} });
  redirect.handle({ type: "session.idle", data: {} });

  assert.deepEqual(entries, [
    {
      time: "2023-11-14T22:13:20.000Z",
      sessionId: "session-1",
      redirect: "applied",
      proxyMs: 0,
      builtInCalls: 1,
      relayedCalls: 2,
      lastGatewayError: "500",
    },
  ]);
});

test("every request the relay serves is recorded without values", async (t) => {
  const { redirect, requests } = await start(t);
  await redirect.listen();
  await redirect.apply(fakeSession());
  await infer(redirect.url);
  await fetch(`${redirect.url}/meta`);

  assert.deepEqual(requests, [
    { time: "2023-11-14T22:13:20.000Z", sessionId: "session-1", route: "gateway", method: "POST", path: "/responses", status: 200 },
    { time: "2023-11-14T22:13:20.000Z", sessionId: "session-1", route: "rejected", method: "GET", path: "/meta", status: 401 },
  ]);
  assert.equal(JSON.stringify(requests).includes("gho_"), false);
});

test("a session without a turn, such as a warm-up, records nothing", async (t) => {
  const { redirect, entries } = await start(t);
  await redirect.listen();
  const session = fakeSession();
  await redirect.apply(session);
  redirect.handle({ type: "session.tools_updated", data: {} });
  redirect.handle({ type: "session.idle", data: {} });
  await redirect.shutdown();

  assert.deepEqual(entries, []);
  assert.deepEqual(session.updates, [{ copilotUrl: redirect.url }, { copilotUrl: null }]);
});

test("shutdown points the session back at the default URL within its bound", async (t) => {
  const { redirect } = await start(t);
  await redirect.listen();
  let calls = 0;
  const hung = fakeSession({ update: () => (calls++ === 0 ? Promise.resolve({ success: true }) : new Promise(() => {})) });
  await redirect.apply(hung);
  const started = Date.now();
  await redirect.shutdown(50);
  assert.ok(Date.now() - started < REVERT_TIMEOUT_MS);
  assert.equal(calls, 2);

  const fresh = await start(t);
  await fresh.redirect.listen();
  await fresh.redirect.shutdown();
});

test("an app that never answers the redirect is unavailable within the bound, and told to clear it", async (t) => {
  const { redirect, entries, logged } = await start(t, {}, { applyTimeoutMs: 50 });
  await redirect.listen();
  const updates = [];
  const session = fakeSession({
    update: (options) => {
      updates.push(options);
      return options.copilotUrl === null ? Promise.resolve({ success: true }) : new Promise(() => {});
    },
  });
  assert.equal(await redirect.apply(session), false);
  assert.equal(entries[0].redirect, "unavailable");
  assert.match(logged[0], /no answer within 50 ms/);
  await delay(10);
  assert.deepEqual(updates, [{ copilotUrl: redirect.url }, { copilotUrl: null }]);
});

test("the sessions file is written in the state dir by default", async (t) => {
  const stateDir = mkdtempSync(join(tmpdir(), "ug-seat-redirect-"));
  const { redirect } = await start(t, {}, { record: undefined, recordRequest: undefined, stateDir });
  await redirect.listen();
  await redirect.apply(fakeSession());
  redirect.handle({ type: "model.call_start", data: { model: "gpt-5" } });
  redirect.handle({ type: "session.idle", data: {} });
  await delay(100);

  const [entry] = JSON.parse(readFileSync(join(stateDir, SESSIONS_FILE), "utf8"));
  assert.equal(entry.builtInCalls, 1);
  assert.equal(entry.redirect, "applied");
  await infer(redirect.url);
  await delay(100);
  const [request] = JSON.parse(readFileSync(join(stateDir, REQUESTS_FILE), "utf8"));
  assert.equal(request.route, "gateway");
  assert.equal(request.path, "/responses");
});

test("a proxy that could capture loopback skips the redirect, clears it, and is recorded as proxied", async (t) => {
  const { redirect, entries, logged } = await start(t, {}, {
    proxyCheck: async () => ({ captured: true, reason: "HTTP_PROXY without a loopback exception in NO_PROXY" }),
  });
  await redirect.listen();
  const session = fakeSession();

  assert.equal(await redirect.apply(session), false);
  assert.equal(redirect.state, "proxied");
  assert.deepEqual(session.updates, [{ copilotUrl: null }]);
  assert.deepEqual(entries, [
    {
      time: "2023-11-14T22:13:20.000Z",
      sessionId: "session-1",
      redirect: "proxied",
      proxyMs: 0,
      reason: "HTTP_PROXY without a loopback exception in NO_PROXY",
      builtInCalls: 0,
      relayedCalls: 0,
    },
  ]);
  assert.match(logged[0], /not redirecting, HTTP_PROXY without a loopback exception/);
});

test("a proxy check that fails or throws still redirects, and says so", async (t) => {
  const failed = await start(t, {}, { proxyCheck: async () => ({ captured: false, reason: null, error: "ETIMEDOUT" }) });
  await failed.redirect.listen();
  assert.equal(await failed.redirect.apply(fakeSession()), true);
  assert.match(failed.logged[0], /proxy check failed: ETIMEDOUT/);

  const thrown = await start(t, {}, {
    proxyCheck: async () => {
      throw new Error("boom");
    },
  });
  await thrown.redirect.listen();
  assert.equal(await thrown.redirect.apply(fakeSession()), true);
  thrown.redirect.handle({ type: "model.call_start", data: { model: "gpt-5" } });
  thrown.redirect.handle({ type: "session.idle", data: {} });
  assert.equal(thrown.entries[0].proxyCheck, "boom");
  assert.equal(thrown.entries[0].redirect, "applied");
});

test("on Windows a PAC that leaves loopback alone no longer skips the redirect, and both decisions are recorded", async (t) => {
  const REG_PAC = ["", "HKEY_CURRENT_USER\\...\\Internet Settings", "    AutoConfigURL    REG_SZ    http://proxy.example/proxy.pac", ""].join("\r\n");
  let asked = null;
  const { redirect, entries } = await start(t, {}, {
    proxyCheck: ({ port }) =>
      detectLoopbackProxy({
        env: {},
        platform: "win32",
        port,
        run: async () => REG_PAC,
        resolve: async (url) => ((asked = url), { proxy: null, bypassed: true }),
      }),
  });
  await redirect.listen();
  assert.equal(asked, null);
  assert.equal(await redirect.apply(fakeSession()), true);
  assert.equal(asked, `http://127.0.0.1:${new URL(redirect.url).port}/`);
  redirect.handle({ type: "model.call_start", data: { model: "gpt-5" } });
  redirect.handle({ type: "session.idle", data: {} });
  assert.equal(entries[0].redirect, "applied");
  assert.deepEqual(entries[0].proxyDecision, {
    registry: { captured: true, reason: "a Windows PAC proxy" },
    resolver: { proxy: null, bypassed: true },
  });
});

test("by default the proxy check reads the extension's environment", async (t) => {
  const { redirect, entries } = await start(t, {}, { proxyCheck: undefined, env: { HTTPS_PROXY: "http://proxy.example:3128" } });
  await redirect.listen();
  assert.equal(await redirect.apply(fakeSession()), false);
  assert.equal(entries[0].redirect, "proxied");
});

test("clearRedirect swallows a session that refuses", async () => {
  await clearRedirect({ rpc: { options: { update: async () => { throw new Error("gone"); } } } });
  await clearRedirect({ rpc: {} });
});

test("keepAliveOnErrors logs instead of ending the process", () => {
  const proc = new EventEmitter();
  const logged = [];
  keepAliveOnErrors((line) => logged.push(line), proc);
  proc.emit("uncaughtException", new Error("boom"));
  proc.emit("unhandledRejection", "nope");
  assert.deepEqual(logged, [
    "Databricks seat relay: uncaught exception: boom",
    "Databricks seat relay: unhandled rejection: nope",
  ]);
});
