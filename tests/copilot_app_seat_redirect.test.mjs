import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdtempSync, readFileSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { after, before, test } from "node:test";

import {
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
import { shutdownBeforeTerminating } from "../src/ucode/agents/desktop/copilot_app_plugin/app-tracing.mjs";

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
    ...options,
  });
  t.after(() => redirect?.close());
  gateway.requests.length = 0;
  gateway.status = 200;
  return { redirect, entries, logged };
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
    { time: "2023-11-14T22:13:20.000Z", sessionId: "session-1", redirect: "unavailable", builtInCalls: 0, relayedCalls: 0 },
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
      builtInCalls: 1,
      relayedCalls: 2,
      lastGatewayError: "500",
    },
  ]);
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

test("an app that never answers the redirect is unavailable within the bound", async (t) => {
  const { redirect, entries, logged } = await start(t, {}, { applyTimeoutMs: 50 });
  await redirect.listen();
  assert.equal(await redirect.apply(fakeSession({ update: () => new Promise(() => {}) })), false);
  assert.equal(entries[0].redirect, "unavailable");
  assert.match(logged[0], /no answer within 50 ms/);
});

test("the sessions file is written in the state dir by default", async (t) => {
  const stateDir = mkdtempSync(join(tmpdir(), "ug-seat-redirect-"));
  const { redirect } = await start(t, {}, { record: undefined, stateDir });
  await redirect.listen();
  await redirect.apply(fakeSession());
  redirect.handle({ type: "model.call_start", data: { model: "gpt-5" } });
  redirect.handle({ type: "session.idle", data: {} });
  await delay(100);

  const [entry] = JSON.parse(readFileSync(join(stateDir, SESSIONS_FILE), "utf8"));
  assert.equal(entry.builtInCalls, 1);
  assert.equal(entry.redirect, "applied");
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

test("shutdownBeforeTerminating runs every step together, then raises SIGTERM again", async () => {
  const proc = new EventEmitter();
  proc.pid = 4242;
  const killed = [];
  proc.kill = (pid, signal) => killed.push([pid, signal]);
  const order = [];
  shutdownBeforeTerminating(
    [
      async (ms) => {
        order.push(["a", ms]);
        await delay(20);
        order.push("a done");
      },
      async (ms) => {
        order.push(["b", ms]);
        throw new Error("revert failed");
      },
    ],
    proc,
    500,
  );
  proc.emit("SIGTERM");
  for (let i = 0; i < 50 && killed.length === 0; i++) await delay(5);

  assert.deepEqual(order, [["a", 500], ["b", 500], "a done"]);
  assert.deepEqual(killed, [[4242, "SIGTERM"]]);
});
