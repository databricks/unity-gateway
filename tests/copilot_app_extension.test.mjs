import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { copyFileSync, existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { dirname, join, join as joinPath } from "node:path";
import { test as nodeTest } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

import { RESTART_HINT } from "../src/ucode/agents/desktop/copilot_app_plugin/app-restart.mjs";
import { DEBUG_LOG_FILE } from "../src/ucode/agents/desktop/copilot_app_plugin/app-debug.mjs";
import { SESSIONS_FILE } from "../src/ucode/agents/desktop/copilot_app_plugin/app-seat-redirect.mjs";
import { FakeApp } from "./copilot_app_fake.mjs";

// The entry point runs in a child process, the way the Copilot runtime starts it, with the SDK
// import redirected to a fake that writes every call to a log file this test reads.
const skip = typeof WebSocket === "function" ? false : "this Node has no global WebSocket";
const test = (name, fn) => nodeTest(name, { skip }, fn);

const SOURCE = join(dirname(fileURLToPath(import.meta.url)), "..", "src", "ucode", "agents", "desktop", "copilot_app_plugin");
const PLUGIN_FILES = [
  "extension.mjs",
  "ug-plugin.mjs",
  "app-rows.mjs",
  "app-remove.mjs",
  "app-restart.mjs",
  "app-state.mjs",
  "app-host.mjs",
  "app-proxy.mjs",
  "app-windows.mjs",
  "app-debug.mjs",
  "app-system-proxy.mjs",
  "app-seat-relay.mjs",
  "app-seat-redirect.mjs",
];
const CLAUDE = "11111111-1111-5111-8111-111111111111";
const LOGIN_HINT = "Run `databricks auth login` and restart the app.";
const PROXY_ENV = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "COPILOT_EXTENSION_SANDBOXED"];

const FAKE_SDK = `
import { appendFileSync } from "node:fs";
const record = (entry) => appendFileSync(process.env.UG_FAKE_LOG, JSON.stringify({ ...entry, at: Date.now() }) + "\\n");
export async function joinSession(options = {}) {
  record({
    kind: "join",
    providers: (options.providers ?? []).map((p) => ({ name: p.name, token: typeof p.bearerTokenProvider })),
    models: options.models ?? null,
  });
  const events = JSON.parse(process.env.UG_FAKE_EVENTS || "[]");
  return {
    on(handler) {
      record({ kind: "on" });
      setTimeout(async () => {
        for (const event of events) handler(event);
        if (process.env.UG_FAKE_MINT && options.providers?.[0]) {
          try {
            record({ kind: "mint", token: await options.providers[0].bearerTokenProvider() });
          } catch (error) {
            record({ kind: "mint", error: error.message });
          }
        }
        record({ kind: "ready" });
      }, 50);
    },
    async log(message, opts) {
      record({ kind: "log", message, level: opts?.level });
    },
    rpc: {
      options: {
        update: async (opts) => (record({ kind: "update", options: opts }), { success: true }),
      },
      gitHubAuth: { getStatus: async () => ({ host: "github.com", copilotPlan: "business" }) },
    },
  };
}
`;
// Slow host and proxy checks stand in for a Windows machine whose PowerShell takes seconds.
const REGISTER = `
import { registerHooks } from "node:module";
import { pathToFileURL } from "node:url";
const fake = pathToFileURL(process.env.UG_FAKE_SDK).href;
const slow = { "./app-host.mjs": process.env.UG_FAKE_HOST, "./app-proxy.mjs": process.env.UG_FAKE_PROXY };
registerHooks({
  resolve: (specifier, context, next) => {
    if (specifier === "@github/copilot-sdk/extension") return { url: fake, shortCircuit: true };
    if (slow[specifier]) return { url: pathToFileURL(slow[specifier]).href, shortCircuit: true };
    return next(specifier, context);
  },
});
`;
const SLOW_HOST = `
import { appendFileSync } from "node:fs";
import { setTimeout as delay } from "node:timers/promises";
const record = (entry) => appendFileSync(process.env.UG_FAKE_LOG, JSON.stringify({ ...entry, at: Date.now() }) + "\\n");
export async function runningUnderApp() {
  record({ kind: "host-start" });
  await delay(Number(process.env.UG_FAKE_HOST_MS));
  record({ kind: "host-done" });
  return { app: true, via: "ancestry" };
}
`;
const SLOW_PROXY = `
import { appendFileSync } from "node:fs";
import { setTimeout as delay } from "node:timers/promises";
const record = (entry) => appendFileSync(process.env.UG_FAKE_LOG, JSON.stringify({ ...entry, at: Date.now() }) + "\\n");
export async function detectLoopbackProxy() {
  record({ kind: "proxy-start" });
  await delay(Number(process.env.UG_FAKE_PROXY_MS));
  record({ kind: "proxy-done" });
  return { captured: false, reason: null };
}
`;
const TOKEN_COMMAND = `
import { appendFileSync } from "node:fs";
appendFileSync(process.env.UG_FAKE_LOG, JSON.stringify({ kind: "token-command", args: process.argv.slice(2) }) + "\\n");
process.stdout.write("dbx");
`;

function rows(id, name) {
  return {
    provider: { id, name, kind: "custom", settings: { baseUrl: `https://ws/${name}` } },
    secret: null,
    models: [{ id: `${id}/m`, providerId: id, modelId: "m", wireModel: "m", displayName: "m" }],
  };
}

function startGateway(t) {
  const gateway = { requests: [] };
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      gateway.requests.push({ url: req.url, headers: req.headers, body: Buffer.concat(chunks).toString() });
      res.writeHead(200, { "content-type": "application/json" });
      res.end("{}");
    });
  });
  t.after(() => server.close());
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({ ...gateway, base: `http://127.0.0.1:${server.address().port}` })));
}

async function setup(t, { config = {}, rawConfig, env = {}, events = [], mint = false, appPid, slowHostMs, slowProxyMs } = {}) {
  const app = new FakeApp();
  await app.listen();
  t.after(() => app.close());
  const home = mkdtempSync(join(tmpdir(), "ug-copilot-home-"));
  const dir = mkdtempSync(join(tmpdir(), "ug-copilot-extension-"));
  app.writeRunFiles(home);
  if (appPid !== undefined) writeFileSync(join(home, "run", "app-owner.pid"), `${appPid}`);
  for (const name of PLUGIN_FILES) copyFileSync(join(SOURCE, name), join(dir, name));
  writeFileSync(join(dir, "fake-sdk.mjs"), FAKE_SDK);
  writeFileSync(join(dir, "register.mjs"), REGISTER);
  writeFileSync(join(dir, "token.mjs"), TOKEN_COMMAND);
  writeFileSync(join(dir, "slow-host.mjs"), SLOW_HOST);
  writeFileSync(join(dir, "slow-proxy.mjs"), SLOW_PROXY);
  const gateway = await startGateway(t);
  writeFileSync(
    join(dir, "config.json"),
    JSON.stringify({
      remove: false,
      pluginRoot: dir,
      providers: [{ name: CLAUDE, baseUrl: "https://ws/claude", headers: {} }],
      models: [{ id: "m" }],
      tokenCommand: [process.execPath, join(dir, "token.mjs")],
      loginHint: LOGIN_HINT,
      missingUgHint: "ug is missing.",
      app: { providers: [rows(CLAUDE, "claude")], ownedProviderIds: [CLAUDE] },
      seatRelay: { gatewayBase: gateway.base, providerService: "main.default.seat", askGitHub: false, userAgent: "ucode/1.0 (linux; x86_64)" },
      ...config,
    }),
  );
  if (rawConfig !== undefined) writeFileSync(join(dir, "config.json"), rawConfig);
  const log = join(dir, "fake.log");
  writeFileSync(log, "");
  const childEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !PROXY_ENV.includes(key.toUpperCase())));
  const child = spawn(process.execPath, ["--import", join(dir, "register.mjs"), join(dir, "extension.mjs")], {
    env: {
      ...childEnv,
      COPILOT_HOME: home,
      SESSION_ID: "session-1",
      UG_FAKE_LOG: log,
      UG_FAKE_SDK: join(dir, "fake-sdk.mjs"),
      UG_FAKE_EVENTS: JSON.stringify(events),
      ...(mint ? { UG_FAKE_MINT: "1" } : {}),
      ...(slowHostMs ? { UG_FAKE_HOST: join(dir, "slow-host.mjs"), UG_FAKE_HOST_MS: String(slowHostMs) } : {}),
      ...(slowProxyMs ? { UG_FAKE_PROXY: join(dir, "slow-proxy.mjs"), UG_FAKE_PROXY_MS: String(slowProxyMs) } : {}),
      ...env,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (chunk) => (stderr += chunk));
  const exited = new Promise((resolve) => child.on("exit", (code, signal) => resolve({ code, signal })));
  t.after(() => child.kill("SIGKILL"));
  const timed = () =>
    readFileSync(log, "utf8")
      .split("\n")
      .filter(Boolean)
      .map((line) => JSON.parse(line));
  const entries = () => timed().map(({ at, ...entry }) => entry);
  const sessions = () => (existsSync(join(dir, SESSIONS_FILE)) ? JSON.parse(readFileSync(join(dir, SESSIONS_FILE), "utf8")) : []);
  // Resolves once the log satisfies `predicate` or the child has exited.
  async function waitFor(predicate, ms = 10_000) {
    const deadline = Date.now() + ms;
    let done = false;
    exited.then(() => (done = true));
    while (Date.now() < deadline) {
      if (predicate(entries())) return true;
      if (done) return false;
      await delay(25);
    }
    throw new Error(`timed out waiting; log: ${JSON.stringify(entries())} stderr: ${stderr}`);
  }
  async function terminate() {
    child.kill("SIGTERM");
    return exited;
  }
  return { app, dir, home, gateway, child, entries, timed, sessions, waitFor, terminate, exited, stderr: () => stderr };
}

const kinds = (entries, kind) => entries.filter((entry) => entry.kind === kind);
const sawRestartHint = (entries) => kinds(entries, "log").some((entry) => entry.message === RESTART_HINT);

test("under the app the extension serves its rows, redirects the session to the relay and reverts on SIGTERM", async (t) => {
  const run = await setup(t);
  await run.waitFor(sawRestartHint);

  const entries = run.entries();
  const [join] = kinds(entries, "join");
  assert.deepEqual(join, { kind: "join", providers: [{ name: CLAUDE, token: "function" }], models: [{ id: "m" }] });
  const [update] = kinds(entries, "update");
  assert.match(update.options.copilotUrl, /^http:\/\/127\.0\.0\.1:\d+\/[A-Za-z0-9_-]{43}$/);
  // The redirect applies right after the join, before the session's events are handled.
  assert.ok(entries.indexOf(join) < entries.indexOf(update) && entries.indexOf(update) < entries.indexOf(kinds(entries, "on")[0]));
  assert.deepEqual([...run.app.providers.keys()], [CLAUDE]);
  assert.equal(kinds(entries, "log").at(-1).level, "info");

  const response = await fetch(`${update.options.copilotUrl}/responses`, {
    method: "POST",
    headers: { authorization: "Bearer gho_app", "content-type": "application/json", "user-agent": "copilot/1.0 client/github-app (linux)" },
    body: JSON.stringify({ model: "gpt-5", input: "hi" }),
  });
  assert.equal(response.status, 200);
  const [relayed] = run.gateway.requests;
  assert.equal(relayed.url, "/ai-gateway/openai/v1/responses");
  assert.equal(relayed.headers.authorization, "Bearer gho_app");
  assert.equal(relayed.headers["x-databricks-ai-gateway-token"], "Bearer dbx");
  assert.equal(relayed.headers["databricks-model-provider-service"], "main.default.seat");
  assert.equal(relayed.headers["session-id"], "session-1");
  assert.deepEqual(kinds(run.entries(), "token-command")[0].args, []);

  const { signal } = await run.terminate();
  assert.equal(signal, "SIGTERM");
  assert.deepEqual(kinds(run.entries(), "update").at(-1).options, { copilotUrl: null });
  assert.equal(run.stderr(), "");

  // The diagnostics log has every step, and never a credential or the relay secret.
  const debugText = readFileSync(joinPath(run.dir, DEBUG_LOG_FILE), "utf8");
  const events = debugText.trim().split("\n").map((line) => JSON.parse(line).event);
  for (const expected of ["start", "config", "host.check", "proxy.env", "relay.listening", "session.joined", "redirect.apply.done", "relay.mint", "relay.request", "redirect.shutdown"]) {
    assert.ok(events.includes(expected), `${expected} missing from ${events}`);
  }
  const secret = update.options.copilotUrl.split("/").at(-1);
  assert.ok(!debugText.includes(secret), "relay secret in the debug log");
  assert.ok(!debugText.includes("gho_"), "GitHub token in the debug log");
  assert.ok(!debugText.includes("Bearer dbx"), "Databricks token in the debug log");
  const start = JSON.parse(debugText.split("\n")[0]);
  assert.equal(start.event, "start");
  assert.equal(start.env.SESSION_ID, "session-1");
  assert.equal(start.sid, "session-1");
});

test("a 401 from the session invalidates the token and shows the sign-in hint once", async (t) => {
  const run = await setup(t, { events: [{ type: "session.error", data: { statusCode: 401 } }], mint: true });
  await run.waitFor((entries) => kinds(entries, "ready").length > 0);
  await run.terminate();

  const entries = run.entries();
  assert.ok(kinds(entries, "log").some((entry) => entry.message === LOGIN_HINT && entry.level === "error"));
  assert.deepEqual(kinds(entries, "mint"), [{ kind: "mint", token: "dbx" }]);
  assert.deepEqual(kinds(entries, "token-command")[0].args, ["--force-refresh"]);
});

test("a sandboxed session keeps its rows and clears the redirect instead of applying one", async (t) => {
  const run = await setup(t, { env: { COPILOT_EXTENSION_SANDBOXED: "1" } });
  await run.waitFor(sawRestartHint);
  const { code } = await run.terminate();

  const entries = run.entries();
  assert.equal(kinds(entries, "join").length, 1);
  assert.deepEqual(kinds(entries, "update"), [{ kind: "update", options: { copilotUrl: null } }]);
  assert.deepEqual(run.sessions().map((entry) => entry.redirect), ["sandboxed"]);
  // Nothing was listening, so the plain SIGTERM ends the process.
  assert.equal(code, null);
});

test("a proxy that could capture loopback skips the redirect, clears it and warns in the session", async (t) => {
  const run = await setup(t, { env: { HTTP_PROXY: "http://127.0.0.1:9" } });
  await run.waitFor(sawRestartHint);
  await run.terminate();

  const entries = run.entries();
  assert.equal(kinds(entries, "join").length, 1);
  assert.deepEqual(kinds(entries, "update")[0].options, { copilotUrl: null });
  assert.ok(kinds(entries, "update").every((entry) => entry.options.copilotUrl === null));
  const warning = kinds(entries, "log").find((entry) => entry.level === "warning");
  assert.match(warning.message, /proxy/);
  const [entry] = run.sessions();
  assert.equal(entry.redirect, "proxied");
  assert.equal(entry.reason, "HTTP_PROXY without a loopback exception in NO_PROXY");
});

test("with NO_PROXY covering loopback the env proxy doesn't stop the redirect", async (t) => {
  const run = await setup(t, { env: { HTTP_PROXY: "http://127.0.0.1:9", NO_PROXY: "localhost,127.0.0.1" } });
  await run.waitFor(sawRestartHint);
  await run.terminate();

  assert.match(kinds(run.entries(), "update")[0].options.copilotUrl, /^http:\/\/127\.0\.0\.1:/);
  assert.deepEqual(run.sessions(), []);
});

test("outside the app, as in a Copilot CLI session, the extension joins nothing and exits", async (t) => {
  const run = await setup(t, { appPid: 2 ** 22 - 2 });
  const { code } = await run.exited;

  assert.equal(code, 0);
  assert.deepEqual(run.entries(), []);
  assert.equal(run.app.connections, 0);
  const [entry] = run.sessions();
  assert.equal(entry.redirect, "not the app");
  assert.equal(entry.host, "ancestry");
  assert.equal(typeof entry.hostMs, "number");
  assert.equal(run.stderr(), "");
  const events = readFileSync(join(run.dir, DEBUG_LOG_FILE), "utf8").trim().split("\n").map((line) => JSON.parse(line));
  const host = events.find((event) => event.event === "host.check");
  assert.equal(host.app, false);
  assert.equal(events.at(-1).event, "exit");
});

test("an unreadable config ends the extension once, as an uncaught error would", async (t) => {
  const run = await setup(t, { rawConfig: "not json" });
  const { code } = await run.exited;

  assert.equal(code, 1);
  assert.match(run.stderr(), /SyntaxError/);
  const events = readFileSync(join(run.dir, DEBUG_LOG_FILE), "utf8").trim().split("\n").map((line) => JSON.parse(line));
  assert.deepEqual(
    events.filter((event) => event.event === "fatal").map((event) => event.kind),
    ["config", "uncaughtException"],
  );
  assert.equal(events.at(-1).event, "exit");
});

test("slow host and proxy checks overlap with the relay's start, and the applied entry records their timing", async (t) => {
  const run = await setup(t, {
    slowHostMs: 4_000,
    slowProxyMs: 1_500,
    events: [{ type: "model.call_start", data: { model: "gpt-5" } }, { type: "session.idle" }],
  });
  await run.waitFor((entries) => run.sessions().length > 0, 15_000);
  await run.terminate();

  const entries = run.timed();
  const at = (kind) => kinds(entries, kind)[0].at;
  // The relay (and with it the proxy check) started before the host decision came back.
  assert.ok(at("proxy-start") < at("host-done"), JSON.stringify(entries));
  // The session is joined only once the host is known, and without the checks adding up.
  assert.ok(at("join") >= at("host-done"));
  assert.ok(at("join") - at("host-start") < 4_000 + 1_000, `join took ${at("join") - at("host-start")} ms`);
  const [entry] = run.sessions();
  assert.equal(entry.redirect, "applied");
  assert.ok(entry.hostMs >= 4_000, `hostMs ${entry.hostMs}`);
  assert.ok(entry.proxyMs >= 1_500 && entry.proxyMs < 4_000, `proxyMs ${entry.proxyMs}`);
  assert.equal(entry.builtInCalls, 1);
});
