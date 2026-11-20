import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  SANDBOX_MARKER_ENV,
  ancestorsOf,
  hostDecision,
  parsePidTable,
  runningUnderApp,
} from "../src/ucode/agents/desktop/copilot_app_plugin/app-host.mjs";

function homeWithAppPid(pid) {
  const home = mkdtempSync(join(tmpdir(), "ug-copilot-host-"));
  if (pid !== null) {
    mkdirSync(join(home, "run"));
    writeFileSync(join(home, "run", "app-owner.pid"), `${pid}\n`);
  }
  return home;
}

test("parsePidTable reads `pid ppid` lines from ps or PowerShell and skips headers", () => {
  const parents = parsePidTable("  PID PPID\n 10 1\n  42 10 \nbroken line\n7 42\r\n");
  assert.deepEqual([...parents.entries()], [[10, 1], [42, 10], [7, 42]]);
});

test("ancestorsOf walks up to init, stops on cycles and at the limit", () => {
  const tree = new Map([[7, 42], [42, 10], [10, 1]]);
  const parentOf = (pid) => tree.get(pid) ?? null;
  assert.deepEqual(ancestorsOf(7, parentOf), [7, 42, 10]);
  assert.deepEqual(ancestorsOf(7, parentOf, 2), [7, 42]);
  assert.deepEqual(ancestorsOf(5, (pid) => (pid === 5 ? 6 : 5)), [5, 6]);
  assert.deepEqual(ancestorsOf(9, () => null), [9]);
});

test("hostDecision: the app's pid among the ancestors, else the app runtime's marker, else not the app", () => {
  assert.deepEqual(hostDecision({ appPid: 42, ancestors: [7, 42, 10] }), { app: true, via: "ancestry" });
  assert.deepEqual(hostDecision({ appPid: 42, ancestors: [7, 8, 10], env: { [SANDBOX_MARKER_ENV]: "0" } }), {
    app: true,
    via: "sandbox marker",
  });
  assert.deepEqual(hostDecision({ appPid: null, ancestors: null, env: { [SANDBOX_MARKER_ENV]: "1" } }), {
    app: true,
    via: "sandbox marker",
  });
  assert.deepEqual(hostDecision({ appPid: 42, ancestors: [7, 8, 10], env: { COPILOT_EXTENSION_PARENT_PID: "8" } }), {
    app: false,
    via: "ancestry",
  });
  assert.deepEqual(hostDecision({ appPid: 42, ancestors: null, env: {} }), { app: false, via: "no process tree" });
  assert.deepEqual(hostDecision({ appPid: null, ancestors: [7, 8], env: {} }), { app: false, via: "ancestry" });
});

test("runningUnderApp finds this process's parent as the app through the live process tree", async (t) => {
  if (process.platform !== "linux" && process.platform !== "darwin") t.skip("needs /proc or ps");
  assert.deepEqual(await runningUnderApp({ home: homeWithAppPid(process.ppid), env: {} }), { app: true, via: "ancestry" });
  assert.deepEqual(await runningUnderApp({ home: homeWithAppPid(2 ** 22 - 2), env: {} }), { app: false, via: "ancestry" });
  assert.deepEqual(await runningUnderApp({ home: homeWithAppPid(null), env: {} }), { app: false, via: "ancestry" });
});

test("runningUnderApp uses one process listing on macOS and Windows, and the marker when it fails", async () => {
  const calls = [];
  const table = async (command, args) => (calls.push([command, args[0]]), "1 0\n300 1\n301 300\n302 301\n");
  assert.deepEqual(await runningUnderApp({ home: homeWithAppPid(300), env: {}, platform: "darwin", pid: 302, run: table }), {
    app: true,
    via: "ancestry",
  });
  assert.deepEqual(await runningUnderApp({ home: homeWithAppPid(300), env: {}, platform: "win32", pid: 302, run: table }), {
    app: true,
    via: "ancestry",
  });
  assert.deepEqual(calls, [["ps", "-axo"], ["powershell.exe", "-NoProfile"]]);
  const failing = async () => null;
  assert.deepEqual(await runningUnderApp({ home: homeWithAppPid(300), env: {}, platform: "darwin", pid: 302, run: failing }), {
    app: false,
    via: "no process tree",
  });
  assert.deepEqual(
    await runningUnderApp({ home: homeWithAppPid(300), env: { [SANDBOX_MARKER_ENV]: "1" }, platform: "darwin", pid: 302, run: failing }),
    { app: true, via: "sandbox marker" },
  );
});
