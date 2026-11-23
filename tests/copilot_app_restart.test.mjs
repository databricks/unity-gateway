import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  RESTART_HINT,
  RESTARTING_MESSAGE,
  launchRestart,
  planRestart,
  tellRestart,
} from "../src/ucode/agents/desktop/copilot_app_plugin/app-restart.mjs";

const TARGET = { platform: "linux", pid: 4242, app: "/usr/bin/github" };

function emptyHome() {
  return mkdtempSync(join(tmpdir(), "ug-copilot-home-"));
}

function homeWithPid(portFile) {
  const home = emptyHome();
  mkdirSync(join(home, "run"));
  writeFileSync(join(home, "run", "ws.port"), portFile);
  return home;
}

function fakeSession() {
  const logs = [];
  return { logs, log: async (message, options) => logs.push([message, options.level]) };
}

test("the extension starts the helper with the app's target and says so in the chat", async () => {
  const started = [];
  const home = emptyHome();
  const stateDir = mkdtempSync(join(tmpdir(), "ug-copilot-state-"));
  const result = await launchRestart({
    home,
    stateDir,
    ugPath: "/opt/ug/bin/ug",
    plan: async () => TARGET,
    start: async (target, dir, ugPath) => (started.push([target, dir, ugPath]), true),
  });
  assert.equal(result, "started");
  assert.deepEqual(started, [[{ ...TARGET, home }, stateDir, "/opt/ug/bin/ug"]]);
  const session = fakeSession();
  await tellRestart(session, result);
  assert.deepEqual(session.logs, [[RESTARTING_MESSAGE, "info"]]);
});

test("when the helper can't restart the app here, the chat gets the restart hint", async () => {
  for (const options of [
    { plan: async () => TARGET, ugPath: undefined },
    { plan: async () => null },
    { plan: async () => Promise.reject(new Error("boom")) },
    { plan: async () => TARGET, start: async () => false },
  ]) {
    const result = await launchRestart({ home: emptyHome(), stateDir: emptyHome(), ugPath: "/u/ug", ...options });
    assert.notEqual(result, "started");
    const session = fakeSession();
    await tellRestart(session, result);
    assert.deepEqual(session.logs, [[RESTART_HINT, "info"]]);
  }
  await tellRestart({ log: () => { throw new Error("closed"); } }, "failed");
});

test("planRestart takes the app's pid from its run file", async () => {
  const alive = (pid) => pid === 4242;
  const exists = () => true;
  const home = homeWithPid("1234\n4242\n");
  const linux = { home, platform: "linux", alive, exists };
  assert.deepEqual(await planRestart({ ...linux, env: { DISPLAY: ":0" } }), TARGET);
  assert.deepEqual(await planRestart({ ...linux, env: { WAYLAND_DISPLAY: "wayland-0" } }), TARGET);
  assert.deepEqual(await planRestart({ home, platform: "darwin", alive }), {
    platform: "darwin",
    pid: 4242,
    app: "com.github.githubapp",
  });
  const calls = [];
  const run = async (command, args) => (calls.push([command, ...args]), "C:\\Program Files\\GitHub\\github.exe\r\n");
  assert.deepEqual(await planRestart({ home, platform: "win32", alive, run }), {
    platform: "win32",
    pid: 4242,
    app: "C:\\Program Files\\GitHub\\github.exe",
  });
  assert.match(calls[0].at(-1), /Get-Process -Id 4242/);
});

test("planRestart reads the app's pid from app-owner.pid, as the app writes it", async () => {
  const home = emptyHome();
  mkdirSync(join(home, "run"));
  writeFileSync(join(home, "run", "ws.port"), "43339");
  writeFileSync(join(home, "run", "app-owner.pid"), "4242");
  const options = { home, platform: "linux", env: { DISPLAY: ":0" }, alive: (pid) => pid === 4242, exists: () => true };
  assert.deepEqual(await planRestart(options), TARGET);
  writeFileSync(join(home, "run", "app-owner.pid"), "not a pid");
  assert.equal(await planRestart(options), null);
});

test("planRestart gives up without a display, the app's pid or a known launcher", async () => {
  const alive = () => true;
  const exists = () => true;
  const home = homeWithPid("1234\n4242\n");
  assert.equal(await planRestart({ home, platform: "linux", env: {}, alive, exists }), null);
  assert.equal(await planRestart({ home, platform: "linux", env: { DISPLAY: ":0" }, alive, exists: () => false }), null);
  assert.equal(await planRestart({ home, platform: "linux", env: { DISPLAY: ":0" }, alive: () => false, exists }), null);
  assert.equal(await planRestart({ home: homeWithPid("1234\n"), platform: "darwin", alive }), null);
  assert.equal(await planRestart({ home: emptyHome(), platform: "darwin", alive }), null);
  assert.equal(await planRestart({ home: homeWithPid(`1234\n${process.pid}\n`), platform: "darwin", alive }), null);
  assert.equal(await planRestart({ home, platform: "win32", alive, run: async () => null }), null);
  assert.equal(await planRestart({ home, platform: "freebsd", alive }), null);
});
