import assert from "node:assert/strict";
import {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  statSync,
  symlinkSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import nodeModule from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test as nodeTest } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import { removeInBackground, removeOnce, removeSettingsKeys, settingsPath } from "../src/ucode/agents/desktop/copilot_app_plugin/app-remove.mjs";
import { LOCK_FILE, LOCK_STALE_MS, copilotHome, syncOnce } from "../src/ucode/agents/desktop/copilot_app_plugin/app-rows.mjs";
import { FakeApp } from "./copilot_app_fake.mjs";

const skip = typeof WebSocket === "function" ? false : "this Node has no global WebSocket";
const test = (name, fn) => nodeTest(name, { skip }, fn);

const ROOT = "/home/me/.ucode/copilot-app-plugin";
const CLAUDE = "11111111-1111-5111-8111-111111111111";
const GPT = "22222222-2222-5222-8222-222222222222";
const UG_KEYS = {
  extraKnownMarketplaces: { "databricks-unity-gateway": { source: { source: "directory", path: ROOT } } },
  enabledPlugins: { "unity-gateway@databricks-unity-gateway": true },
};
const LEGACY_KEYS = {
  extraKnownMarketplaces: { "unity-gateway": { source: { source: "directory", path: ROOT } } },
  enabledPlugins: { "databricks@unity-gateway": true },
};
const CONFIG = { remove: true, pluginRoot: ROOT, app: { providers: [], ownedProviderIds: [CLAUDE, GPT] } };

function seedApp(app) {
  for (const id of [CLAUDE, GPT, "mine"]) {
    app.providers.set(id, { id, name: id, kind: "custom" });
    app.models.set(`${id}/m`, { id: `${id}/m`, providerId: id, modelId: "m" });
  }
}

async function setup(t, settings = UG_KEYS) {
  const app = new FakeApp();
  await app.listen();
  t.after(() => app.close());
  const home = mkdtempSync(join(tmpdir(), "ug-copilot-home-"));
  const stateDir = mkdtempSync(join(tmpdir(), "ug-copilot-state-"));
  app.writeRunFiles(home);
  seedApp(app);
  const path = settingsPath(home);
  if (settings !== null) writeFileSync(path, typeof settings === "string" ? settings : JSON.stringify(settings));
  return { app, path, options: { home, stateDir, timeoutMs: 2000 } };
}

const read = (path) => JSON.parse(readFileSync(path, "utf8"));

test("removeOnce deletes ug's providers, then its settings keys, keeping everything else", async (t) => {
  const settings = {
    model: "gpt-5",
    extraKnownMarketplaces: { other: { source: { source: "github", repo: "o/r" } }, ...UG_KEYS.extraKnownMarketplaces },
    enabledPlugins: { "x@other": false, ...UG_KEYS.enabledPlugins },
  };
  const { app, path, options } = await setup(t, settings);
  chmodSync(path, 0o640);

  assert.equal(await removeOnce(CONFIG, options), "removed");

  assert.deepEqual([...app.providers.keys()], ["mine"]);
  assert.deepEqual([...app.models.keys()], ["mine/m"]);
  assert.deepEqual(read(path), {
    model: "gpt-5",
    extraKnownMarketplaces: { other: { source: { source: "github", repo: "o/r" } } },
    enabledPlugins: { "x@other": false },
  });
  assert.equal(statSync(path).mode & 0o777, 0o640);
  assert.deepEqual(readdirSync(options.home).sort(), ["run", "settings.json"]);
});

test("removeOnce drops a parent object it leaves empty", async (t) => {
  const { path, options } = await setup(t, { model: "gpt-5", ...UG_KEYS });
  assert.equal(await removeOnce(CONFIG, options), "removed");
  assert.deepEqual(read(path), { model: "gpt-5" });
});

test("once the keys are gone, another session's extension does nothing", async (t) => {
  const { app, options } = await setup(t);
  assert.equal(await removeOnce(CONFIG, options), "removed");
  const connections = app.connections;
  assert.equal(await removeOnce(CONFIG, options), "done");
  assert.equal(app.connections, connections);
  assert.equal(app.sent("delete_model_provider").length, 2);
});

test("only one extension process removes at a time; a stale lock is taken over", async (t) => {
  const { app, path, options } = await setup(t);
  const lock = join(options.stateDir, LOCK_FILE);
  writeFileSync(lock, "");
  assert.equal(await removeOnce(CONFIG, options), "busy");
  assert.equal(app.connections, 0);
  assert.deepEqual(read(path), UG_KEYS);

  const old = new Date(Date.now() - 2 * LOCK_STALE_MS);
  utimesSync(lock, old, old);
  assert.equal(await removeOnce(CONFIG, options), "removed");
  assert.equal(existsSync(lock), false);
});

test("concurrent extension processes delete each provider once", async (t) => {
  const { app, options } = await setup(t);
  const results = await Promise.all([0, 1, 2].map(() => removeOnce(CONFIG, options)));
  assert.equal(results.filter((result) => result === "removed").length, 1);
  assert.equal(app.sent("delete_model_provider").length, 2);
});

for (const [name, content] of [
  ["invalid JSON", "{not json"],
  ["a commented file", "// comment\n{}"],
  ["a non-object", "[]"],
]) {
  test(`settings that are ${name} are left alone, but the rows still go`, async (t) => {
    const { app, path, options } = await setup(t, content);
    const logs = [];
    assert.equal(await removeInBackground(CONFIG, options, (line) => logs.push(line)), "refused");
    assert.equal(readFileSync(path, "utf8"), content);
    assert.deepEqual([...app.providers.keys()], ["mine"]);
    assert.match(logs[0], /not valid JSON|not a JSON object/);
  });
}

test("a symlinked settings file is refused", async (t) => {
  const { path, options } = await setup(t, null);
  const target = join(mkdtempSync(join(tmpdir(), "ug-copilot-target-")), "settings.json");
  writeFileSync(target, JSON.stringify(UG_KEYS));
  symlinkSync(target, path);
  const logs = [];
  assert.equal(await removeInBackground(CONFIG, options, (line) => logs.push(line)), "refused");
  assert.deepEqual(read(target), UG_KEYS);
  assert.match(logs[0], /symlink/);
  assert.throws(() => removeSettingsKeys(path, ROOT), /symlink/);
});

test("an earlier build's keys go too", async (t) => {
  const both = {
    model: "m",
    extraKnownMarketplaces: { ...UG_KEYS.extraKnownMarketplaces, ...LEGACY_KEYS.extraKnownMarketplaces },
    enabledPlugins: { ...UG_KEYS.enabledPlugins, ...LEGACY_KEYS.enabledPlugins },
  };
  const { path } = await setup(t, both);
  assert.equal(removeSettingsKeys(path, ROOT), true);
  assert.deepEqual(read(path), { model: "m" });
});

test("a foreign marketplace of ug's name and its plugin key stay", async (t) => {
  const elsewhere = { source: { source: "directory", path: "/elsewhere" } };
  const foreign = {
    extraKnownMarketplaces: { "databricks-unity-gateway": elsewhere, "unity-gateway": elsewhere },
    enabledPlugins: { "unity-gateway@databricks-unity-gateway": true, "databricks@unity-gateway": true },
  };
  const { app, path, options } = await setup(t, foreign);
  assert.equal(await removeOnce(CONFIG, options), "done");
  assert.deepEqual(read(path), foreign);
  assert.equal(app.connections, 0);
});

test("the keys stay until the rows are gone", async (t) => {
  const { app, path, options } = await setup(t);
  const closed = { ...options, home: mkdtempSync(join(tmpdir(), "ug-copilot-home-")) };
  writeFileSync(settingsPath(closed.home), JSON.stringify(UG_KEYS));
  assert.equal(await removeOnce(CONFIG, closed), "not_running");
  assert.deepEqual(read(settingsPath(closed.home)), UG_KEYS);

  const logs = [];
  app.fail = "delete_model_provider";
  assert.equal(await removeInBackground(CONFIG, options, (line) => logs.push(line)), "failed");
  assert.match(logs[0], /refused delete_model_provider/);
  assert.deepEqual(read(path), UG_KEYS);

  app.fail = null;
  assert.equal(await removeInBackground(CONFIG, options, (line) => logs.push(line)), "removed");
  assert.deepEqual(read(path), {});
});

test("a dropped connection is logged, not thrown", async (t) => {
  const { app, path, options } = await setup(t);
  app.drop = "list_model_providers";
  const logs = [];
  assert.equal(await removeInBackground(CONFIG, options, (line) => logs.push(line)), "failed");
  assert.match(logs[0], /closed the connection/);
  assert.deepEqual(read(path), UG_KEYS);
  const throwingLog = () => {
    throw new Error("log failed");
  };
  assert.equal(await removeInBackground(CONFIG, options, throwingLog), "failed");
});

test("without owned providers the keys go without connecting", async (t) => {
  const { app, path, options } = await setup(t);
  const config = { ...CONFIG, app: { providers: [], ownedProviderIds: [] } };
  assert.equal(await removeOnce(config, options), "removed");
  assert.equal(app.connections, 0);
  assert.deepEqual(read(path), {});
});

test("a later install syncs its rows and restarts the app again", async (t) => {
  const { app, options } = await setup(t);
  const rows = {
    providers: [{ provider: { id: GPT, name: "gpt", kind: "custom" }, secret: null, models: [] }],
    ownedProviderIds: [CLAUDE, GPT],
  };
  assert.equal(await syncOnce({ app: rows }, options), "changed");
  assert.equal(await removeOnce({ ...CONFIG, app: rows }, options), "removed");
  assert.equal(app.providers.has(GPT), false);
  assert.equal(await syncOnce({ app: rows }, options), "changed");
  assert.equal(app.providers.has(GPT), true);
});

test("outside remove mode nothing is removed", async (t) => {
  const { app, path, options } = await setup(t);
  assert.equal(await removeOnce({ ...CONFIG, remove: false }, options), "skipped");
  assert.equal(app.connections, 0);
  assert.deepEqual(read(path), UG_KEYS);
});

test("the settings file is found under COPILOT_HOME", () => {
  assert.equal(settingsPath(copilotHome({ COPILOT_HOME: "/custom" })), join("/custom", "settings.json"));
});

const SOURCE = join(dirname(fileURLToPath(import.meta.url)), "..", "src", "ucode", "agents", "desktop", "copilot_app_plugin");
const FAKE_SDK = `data:text/javascript,${encodeURIComponent(`
  export async function joinSession(...args) {
    globalThis.ugJoinCalls.push(args);
    if (globalThis.ugJoinFails) throw new Error("no session");
    return { on() {}, log() {}, rpc: { options: { update: async (options) => (globalThis.ugOptionUpdates.push(options), { success: true }) } } };
  }
`)}`;

async function runExtension(t, options, { joinFails = false, config = {}, appPid } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "ug-copilot-extension-"));
  for (const name of [
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
  ]) {
    copyFileSync(join(SOURCE, name), join(dir, name));
  }
  writeFileSync(join(dir, "config.json"), JSON.stringify({ ...CONFIG, providers: [{ name: CLAUDE }], models: [{ id: "m" }], ...config }));
  if (appPid !== undefined) writeFileSync(join(options.home, "run", "app-owner.pid"), `${appPid}`);
  const hooks = nodeModule.registerHooks({
    resolve: (specifier, context, next) =>
      specifier === "@github/copilot-sdk/extension" ? { url: FAKE_SDK, shortCircuit: true } : next(specifier, context),
  });
  const previousHome = process.env.COPILOT_HOME;
  process.env.COPILOT_HOME = options.home;
  globalThis.ugJoinCalls = [];
  globalThis.ugOptionUpdates = [];
  globalThis.ugJoinFails = joinFails;
  t.after(() => {
    hooks.deregister();
    if (previousHome === undefined) delete process.env.COPILOT_HOME;
    else process.env.COPILOT_HOME = previousHome;
  });
  const errors = [];
  t.mock.method(console, "error", (line) => errors.push(line));
  await import(pathToFileURL(join(dir, "extension.mjs")).href);
  return { dir, joins: globalThis.ugJoinCalls, errors, optionUpdates: globalThis.ugOptionUpdates };
}

const hookSkip = skip || (typeof nodeModule.registerHooks === "function" ? false : "this Node can't stub the SDK import");
const extensionTest = (name, fn) => nodeTest(name, { skip: hookSkip }, fn);

extensionTest("in remove mode the extension removes ug and joins its session without serving", async (t) => {
  const { app, path, options } = await setup(t);
  const { joins, errors, optionUpdates } = await runExtension(t, options);
  assert.deepEqual(joins, [[]]);
  assert.deepEqual(errors, []);
  assert.deepEqual(optionUpdates, [{ copilotUrl: null }]);
  assert.deepEqual([...app.providers.keys()], ["mine"]);
  assert.deepEqual(read(path), {});
});

extensionTest("outside the desktop app the extension joins nothing and leaves the session's models alone", async (t) => {
  const { app, path, options } = await setup(t);
  // The Copilot CLI and VS Code's runtime load the same plugin; neither is a descendant of the app.
  const { dir, joins, errors, optionUpdates } = await runExtension(t, options, {
    config: { remove: false, seatRelay: { gatewayBase: "http://127.0.0.1:9", providerService: "main.default.seat", askGitHub: false } },
    appPid: 2 ** 22 - 2,
  });
  assert.deepEqual(joins, []);
  assert.deepEqual(optionUpdates, []);
  assert.deepEqual(errors, []);
  assert.equal(app.connections, 0);
  assert.deepEqual(read(path), UG_KEYS);
  const [entry] = JSON.parse(readFileSync(join(dir, "seat-relay-sessions.json"), "utf8"));
  assert.equal(entry.redirect, "not the app");
  assert.equal(entry.host, "ancestry");
});

extensionTest("outside the desktop app remove mode still removes ug, without joining", async (t) => {
  const { path, options } = await setup(t);
  const { joins, optionUpdates } = await runExtension(t, options, { appPid: 2 ** 22 - 2 });
  assert.deepEqual(joins, []);
  assert.deepEqual(optionUpdates, []);
  assert.deepEqual(read(path), {});
});

extensionTest("in remove mode a failed join is logged, not thrown", async (t) => {
  const { app, path, options } = await setup(t);
  const { errors } = await runExtension(t, options, { joinFails: true });
  assert.match(errors[0], /couldn't join the session: no session/);
  assert.deepEqual([...app.providers.keys()], ["mine"]);
  assert.deepEqual(read(path), {});
});
