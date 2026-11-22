import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test as nodeTest } from "node:test";

import { LOCK_FILE, LOCK_STALE_MS, copilotHome, readEndpoint, removalRequested, syncInBackground, syncOnce, tryLock } from "../src/ucode/agents/desktop/copilot_app_plugin/app-rows.mjs";
import { FakeApp } from "./copilot_app_fake.mjs";

// The Copilot runtime's Node has a global WebSocket; Node before 22 doesn't.
const skip = typeof WebSocket === "function" ? false : "this Node has no global WebSocket";
const test = (name, fn) => nodeTest(name, { skip }, fn);

const CLAUDE = "11111111-1111-5111-8111-111111111111";
const GPT = "22222222-2222-5222-8222-222222222222";
const OPEN = "33333333-3333-5333-8333-333333333333";

function providerRows(id, name, models, secret = null) {
  return {
    provider: { id, name, kind: secret ? "anthropic" : "custom", settings: { baseUrl: `https://ws/${name}` } },
    secret,
    models: models.map((modelId) => ({ id: `${id}/${modelId}`, providerId: id, modelId, wireModel: modelId, displayName: modelId })),
  };
}

const PLACEHOLDER = { kind: "apiKey", value: "ug-placeholder-not-a-secret" };
const ROWS = {
  providers: [providerRows(CLAUDE, "claude", ["claude-sonnet-4.6"], PLACEHOLDER), providerRows(GPT, "gpt", ["gpt-5-2"])],
  ownedProviderIds: [CLAUDE, GPT, OPEN],
};

async function setup(t) {
  const app = new FakeApp();
  await app.listen();
  t.after(() => app.close());
  const home = mkdtempSync(join(tmpdir(), "ug-copilot-home-"));
  const stateDir = mkdtempSync(join(tmpdir(), "ug-copilot-state-"));
  app.writeRunFiles(home);
  return { app, options: { home, stateDir, timeoutMs: 2000 } };
}

test("readEndpoint reads the first value of each run file and honours COPILOT_HOME", () => {
  const home = mkdtempSync(join(tmpdir(), "ug-copilot-home-"));
  assert.equal(readEndpoint(home), null);
  mkdirSync(join(home, "run"));
  writeFileSync(join(home, "run", "ws.port"), "4321\n99\n");
  writeFileSync(join(home, "run", "ws.token"), "tok\n99\n");
  assert.deepEqual(readEndpoint(home), { port: 4321, token: "tok" });
  assert.equal(copilotHome({ COPILOT_HOME: home }), home);
});

test("syncOnce writes the rows, then does nothing until they or the app launch change", async (t) => {
  const { app, options } = await setup(t);
  app.providers.set("mine", { id: "mine", name: "Mine", kind: "custom" });

  assert.equal(await syncOnce({ app: ROWS }, options), "changed");
  assert.deepEqual([...app.providers.keys()].sort(), [CLAUDE, GPT, "mine"].sort());
  assert.deepEqual([...app.secrets], [[CLAUDE, PLACEHOLDER.value]]);
  assert.deepEqual([...app.models.keys()].sort(), [`${CLAUDE}/claude-sonnet-4.6`, `${GPT}/gpt-5-2`]);
  assert.ok(app.handshakes.every((headers) => !headers.origin));
  const ids = app.received.map((message) => message.request_id);
  assert.equal(new Set(ids).size, ids.length);

  const received = app.received.length;
  assert.equal(await syncOnce({ app: ROWS }, options), "current");
  assert.equal(app.received.length, received);

  app.token = "next-launch";
  app.writeRunFiles(options.home);
  assert.equal(await syncOnce({ app: ROWS }, options), "synced");
  assert.ok(app.sent("upsert_model_provider").slice(-2).every((message) => !message.secret));
});

function relaunch(app, options, token) {
  app.token = token;
  app.writeRunFiles(options.home);
}

test("only the first sync that changes the app's rows asks for a restart", async (t) => {
  const { app, options } = await setup(t);
  assert.equal(await syncOnce({ app: ROWS }, options), "changed");
  assert.ok(existsSync(join(options.stateDir, "app-restart.json")));

  relaunch(app, options, "restarted");
  assert.equal(await syncOnce({ app: ROWS }, options), "synced");

  const next = { ...ROWS, providers: [providerRows(GPT, "gpt", ["gpt-6"])] };
  assert.equal(await syncOnce({ app: next }, options), "changed");
  relaunch(app, options, "restarted-again");
  assert.equal(await syncOnce({ app: next }, options), "synced");
});

test("rows the app already has don't ask for a restart", async (t) => {
  const { app, options } = await setup(t);
  await syncOnce({ app: ROWS }, { ...options, stateDir: mkdtempSync(join(tmpdir(), "ug-copilot-state-")) });
  assert.equal(await syncOnce({ app: structuredClone(ROWS) }, options), "synced");
  assert.ok(!existsSync(join(options.stateDir, "app-restart.json")));
});

test("a deleted row, a new row or a missing secret is a change", async (t) => {
  const { app, options } = await setup(t);
  const fresh = () => ({ ...options, stateDir: mkdtempSync(join(tmpdir(), "ug-copilot-state-")) });
  await syncOnce({ app: ROWS }, fresh());

  app.models.set(`${GPT}/old`, { id: `${GPT}/old`, providerId: GPT, modelId: "old" });
  assert.equal(await syncOnce({ app: ROWS }, fresh()), "changed");

  app.models.delete(`${GPT}/gpt-5-2`);
  assert.equal(await syncOnce({ app: ROWS }, fresh()), "changed");

  app.models.get(`${CLAUDE}/claude-sonnet-4.6`).displayName = "renamed in the app";
  assert.equal(await syncOnce({ app: ROWS }, fresh()), "changed");

  app.secrets.clear();
  app.providers.set(CLAUDE, { ...app.providers.get(CLAUDE), hasSecret: false });
  assert.equal(await syncOnce({ app: ROWS }, fresh()), "changed");

  app.providers.set(OPEN, { id: OPEN, name: "open", kind: "custom" });
  assert.equal(await syncOnce({ app: ROWS }, fresh()), "changed");
  assert.equal(await syncOnce({ app: ROWS }, fresh()), "synced");
});

test("an app that reports the rows differently asks for one restart, not one per launch", async (t) => {
  const { app, options } = await setup(t);
  assert.equal(await syncOnce({ app: ROWS }, options), "changed");
  for (const [launch, token] of ["first", "second"].entries()) {
    for (const model of app.models.values()) delete model.displayName;
    relaunch(app, options, token);
    assert.equal(await syncOnce({ app: ROWS }, options), "synced", `launch ${launch}`);
  }
});

test("of several extension processes, one asks for the restart", async (t) => {
  const { options } = await setup(t);
  const results = await Promise.all([0, 1, 2, 3].map(() => syncOnce({ app: ROWS }, options)));
  assert.equal(results.filter((result) => result === "changed").length, 1);
  assert.ok(results.every((result) => ["changed", "busy", "current"].includes(result)));
});

test("syncOnce deletes ug's stale providers and rows but no one else's", async (t) => {
  const { app, options } = await setup(t);
  await syncOnce({ app: ROWS }, options);
  app.providers.set("mine", { id: "mine", name: "Mine", kind: "custom" });
  app.models.set("user-row", { id: "user-row", providerId: GPT, modelId: "x" });
  app.models.set("odd", { providerId: GPT, modelId: "no-id" });

  const next = {
    providers: [providerRows(GPT, "gpt", ["gpt-6"]), providerRows(OPEN, "open", ["glm-5"])],
    ownedProviderIds: ROWS.ownedProviderIds,
  };
  assert.equal(await syncOnce({ app: next }, options), "changed");

  assert.deepEqual([...app.providers.keys()].sort(), [GPT, OPEN, "mine"].sort());
  assert.deepEqual([...app.models.keys()].sort(), [`${GPT}/gpt-6`, `${OPEN}/glm-5`, "odd"].sort());
});

test("repeated syncs leave exactly one copy of each row", async (t) => {
  const { app, options } = await setup(t);
  const stateDirs = [0, 1, 2].map(() => mkdtempSync(join(tmpdir(), "ug-copilot-state-")));
  await Promise.all(stateDirs.map((stateDir) => syncOnce({ app: ROWS }, { ...options, stateDir })));
  assert.equal(app.providers.size, 2);
  assert.equal(app.models.size, 2);
});

test("only one extension process syncs at a time; a stale lock is taken over", async (t) => {
  const { app, options } = await setup(t);
  const lock = join(options.stateDir, LOCK_FILE);
  writeFileSync(lock, "");
  assert.equal(await syncOnce({ app: ROWS }, options), "busy");
  assert.equal(app.received.length, 0);

  const old = new Date(Date.now() - 2 * LOCK_STALE_MS);
  utimesSync(lock, old, old);
  assert.equal(await syncOnce({ app: ROWS }, options), "changed");
});

test("a lock taken over as stale is released only by its new owner", () => {
  const dir = mkdtempSync(join(tmpdir(), "ug-copilot-lock-"));
  const lock = join(dir, LOCK_FILE);
  const first = tryLock(lock, Date.now);
  assert.equal(tryLock(lock, Date.now), null);
  const old = new Date(Date.now() - 2 * LOCK_STALE_MS);
  utimesSync(lock, old, old);
  const second = tryLock(lock, Date.now);
  assert.ok(second);
  first();
  assert.ok(existsSync(lock));
  second();
  assert.equal(existsSync(lock), false);
});

test("a reply for another request is skipped", async (t) => {
  const { app, options } = await setup(t);
  app.beforeReply.list_model_providers = [
    { type: "model_providers_list", providers: [{ id: "x" }], request_id: "ug-someone-else" },
    { type: "model_providers_list", providers: [{ id: "y" }] },
    { type: "error", message: "boom", request_id: "ug-someone-else" },
  ];
  assert.equal(await syncOnce({ app: ROWS }, options), "changed");
  assert.equal(app.providers.size, 2);
});

test("an error without a request_id answers the pending request", async (t) => {
  const { app, options } = await setup(t);
  app.beforeReply.list_model_providers = [{ type: "error", message: "Invalid message format" }];
  await assert.rejects(syncOnce({ app: ROWS }, options), /Invalid message format/);
  assert.equal(app.providers.size, 0);
});

test("an error from before a request doesn't answer it", async (t) => {
  const { app, options } = await setup(t);
  app.beforeHello = [{ type: "error", message: "Invalid message format" }];
  assert.equal(await syncOnce({ app: ROWS }, options), "changed");
  assert.equal(app.providers.size, 2);
});

test("the sync retries while the app applies another change", async (t) => {
  const { app, options } = await setup(t);
  app.busy = 2;
  assert.equal(await syncOnce({ app: ROWS }, options), "changed");
  assert.equal(app.models.size, 2);
});

test("failures are logged, never thrown, and the next run retries", async (t) => {
  const { app, options } = await setup(t);
  const logs = [];
  app.fail = "upsert_provider_model";
  assert.equal(await syncInBackground({ app: ROWS }, options, (line) => logs.push(line)), "failed");
  assert.match(logs[0], /refused upsert_provider_model/);

  app.fail = null;
  app.drop = "list_model_providers";
  assert.equal(await syncInBackground({ app: ROWS }, options, (line) => logs.push(line)), "failed");
  assert.match(logs[1], /closed the connection/);

  app.drop = null;
  assert.equal(await syncOnce({ app: ROWS }, options), "changed");
});

test("a closed app or a stale token is not synced", async (t) => {
  const { app, options } = await setup(t);
  const home = mkdtempSync(join(tmpdir(), "ug-copilot-home-"));
  assert.equal(await syncOnce({ app: ROWS }, { ...options, home }), "not_running");

  writeFileSync(join(options.home, "run", "ws.token"), "stale\n1\n");
  assert.equal(await syncInBackground({ app: ROWS }, options, () => {}), "failed");
  assert.equal(app.received.length, 0);

  mkdirSync(join(home, "run"));
  writeFileSync(join(home, "run", "ws.port"), "1\n1\n");
  writeFileSync(join(home, "run", "ws.token"), "x\n1\n");
  assert.equal(await syncInBackground({ app: ROWS }, { ...options, home }, () => {}), "failed");
});

test("no app rows in the config means nothing to sync", async (t) => {
  const { app, options } = await setup(t);
  assert.equal(await syncOnce({}, options), "skipped");
  assert.equal(await syncOnce({ app: { providers: [], ownedProviderIds: [] } }, options), "skipped");
  assert.equal(app.connections, 0);
});


test("a sync racing a revert stops before it writes ug's rows back", async (t) => {
  const { app, options } = await setup(t);
  writeFileSync(join(options.stateDir, "config.json"), JSON.stringify({ remove: false }));
  app.onRequest = (message) => {
    if (message.type !== "upsert_model_provider" || app.onRequest === null) return;
    app.onRequest = null;
    // ug marks the plugin for removal, then deletes ug's rows from the running app.
    writeFileSync(join(options.stateDir, "config.json"), JSON.stringify({ remove: true }));
    setImmediate(() => {
      app.providers.clear();
      app.models.clear();
    });
  };

  assert.equal(await syncOnce({ app: ROWS }, options), "removing");
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(app.providers.size, 0);
  assert.equal(app.models.size, 0);
  assert.equal(app.sent("upsert_model_provider").length, 1);
  assert.equal(app.sent("upsert_provider_model").length, 0);
});

test("a plugin marked for removal doesn't sync at all", async (t) => {
  const { app, options } = await setup(t);
  writeFileSync(join(options.stateDir, "config.json"), JSON.stringify({ remove: true }));

  assert.equal(removalRequested(options.stateDir), true);
  assert.equal(await syncOnce({ app: ROWS }, options), "removing");
  assert.equal(app.sent("upsert_model_provider").length, 0);
});
