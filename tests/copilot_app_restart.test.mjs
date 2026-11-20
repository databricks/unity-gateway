import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";

import {
  EXIT_TIMEOUT_MS,
  IDLE_TIMEOUT_MS,
  QUIET_MS,
  RESTART_HINT,
  RESTARTING_MESSAGE,
  launchRestart,
  planRestart,
  restartApp,
  restartWhenIdle,
  runHelper,
  tellRestart,
  sessionEnv,
} from "../src/ucode/agents/desktop/copilot_app_plugin/app-restart.mjs";
import { FakeApp } from "./copilot_app_fake.mjs";

const TARGET = { platform: "linux", pid: 4242, app: "/usr/bin/github" };

function fakeClock() {
  const clock = { t: 0, onSleep: () => {} };
  clock.now = () => clock.t;
  clock.sleep = async (ms) => {
    clock.t += ms;
    await clock.onSleep(clock.t);
  };
  return clock;
}

function emptyHome() {
  return mkdtempSync(join(tmpdir(), "ug-copilot-home-"));
}

function homeWithPid(portFile) {
  const home = emptyHome();
  mkdirSync(join(home, "run"));
  writeFileSync(join(home, "run", "ws.port"), portFile);
  return home;
}

const withWebSocket = { skip: typeof WebSocket === "function" ? false : "this Node has no global WebSocket" };

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
    plan: async () => TARGET,
    start: async (target, dir) => (started.push([target, dir]), true),
  });
  assert.equal(result, "started");
  assert.deepEqual(started, [[{ ...TARGET, home }, stateDir]]);
  const session = fakeSession();
  await tellRestart(session, result);
  assert.deepEqual(session.logs, [[RESTARTING_MESSAGE, "info"]]);
});

test("when the helper can't restart the app here, the chat gets the restart hint", async () => {
  for (const options of [
    { plan: async () => null },
    { plan: async () => Promise.reject(new Error("boom")) },
    { plan: async () => TARGET, start: async () => false },
  ]) {
    const result = await launchRestart({ home: emptyHome(), stateDir: emptyHome(), ...options });
    assert.notEqual(result, "started");
    const session = fakeSession();
    await tellRestart(session, result);
    assert.deepEqual(session.logs, [[RESTART_HINT, "info"]]);
  }
  await tellRestart({ log: () => { throw new Error("closed"); } }, "failed");
});

async function helperRun(t, script, overrides = {}, seen = () => {}) {
  const app = new FakeApp();
  await app.listen();
  t.after(() => app.close());
  const home = emptyHome();
  app.writeRunFiles(home);
  const fake = fakeProcess();
  seen(fake);
  const clock = fake.clock;
  clock.onSleep = async (time) => {
    await script(time, app);
    await delay(2);
  };
  const result = await restartWhenIdle({ ...TARGET, home }, { ...fake.deps, ...overrides });
  return { result, fake, clock, app };
}

test("the helper restarts an idle app after a quiet period", withWebSocket, async (t) => {
  let restartAt;
  const { result, fake } = await helperRun(t, async (time) => {
    restartAt ??= time >= QUIET_MS ? time : undefined;
  });
  assert.equal(result, "relaunched");
  assert.equal(restartAt, QUIET_MS);
  assert.deepEqual(fake.calls[0], ["kill", 4242, "SIGTERM"]);
});

test("a turn in any chat delays the restart until that chat is idle", withWebSocket, async (t) => {
  let watchers;
  let fake;
  const kill = (pid, signal) => fake.deps.killAt.push(fake.clock.t);
  const run = helperRun(
    t,
    async (time, app) => {
      if (time === 5_000) {
        watchers = app.watchers.size;
        app.emit("ui-chat", "assistant.turn_start");
      }
      if (time === 90_000) app.emit("ui-chat", "session.idle");
    },
    { kill },
    (process) => {
      fake = process;
      fake.deps.killAt = [];
    },
  );
  const { result } = await run;
  assert.equal(watchers, 1);
  assert.equal(result, "still_running");
  assert.ok(fake.deps.killAt[0] >= 90_000 + QUIET_MS, `killed at ${fake.deps.killAt[0]}`);
  assert.ok(fake.deps.killAt[0] <= 90_000 + QUIET_MS + 5_000, `killed at ${fake.deps.killAt[0]}`);
});

test("a chat that never goes idle makes the helper give up", withWebSocket, async (t) => {
  const { result, fake, clock } = await helperRun(t, async (time, app) => {
    if (time === 1_000) app.emit("ui-chat", "user.message");
  });
  assert.equal(result, "busy");
  assert.equal(clock.t, IDLE_TIMEOUT_MS);
  assert.deepEqual(fake.calls, []);
});

test("events that keep coming hold the restart off even without a turn start", withWebSocket, async (t) => {
  const { result, fake } = await helperRun(t, async (time, app) => app.emit("chat", "assistant.streaming_delta"));
  assert.equal(result, "busy");
  assert.deepEqual(fake.calls, []);
});

test("an app that closes while the helper waits isn't relaunched", withWebSocket, async (t) => {
  const { result, fake } = await helperRun(t, async (time, app) => {
    if (time === 1_000) {
      app.emit("chat", "user.message");
      app.close();
    }
  });
  assert.equal(result, "stopped");
  assert.deepEqual(fake.calls, []);
});

test("without the app's session events the helper gives up", async () => {
  const fake = fakeProcess();
  assert.equal(await restartWhenIdle({ ...TARGET, home: emptyHome() }, fake.deps), "not_running");
  const home = homeWithPid("1");
  writeFileSync(join(home, "run", "ws.token"), "t");
  assert.equal(await restartWhenIdle({ ...TARGET, home }, fake.deps), "no_session_events");
  assert.deepEqual(fake.calls, []);
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

function fakeProcess({ exitAfterPolls = 2, launches = [true] } = {}) {
  const clock = fakeClock();
  const calls = [];
  let closed = false;
  let polls = 0;
  let osascriptFails = false;
  const outcomes = [...launches];
  return {
    clock,
    calls,
    deps: {
      now: clock.now,
      sleep: clock.sleep,
      alive: () => !(closed && ++polls > exitAfterPolls),
      kill: (pid, signal) => {
        calls.push(["kill", pid, signal]);
        closed = true;
      },
      run: async (command, args) => {
        calls.push(["run", command, ...args]);
        if (command === "/usr/bin/osascript" && osascriptFails) return null;
        closed = true;
        return "";
      },
      launch: async (command, args, env) => {
        calls.push(["launch", command, args, env]);
        return outcomes.shift() ?? false;
      },
      env: { HOME: "/home/u", PATH: "/usr/bin", DISPLAY: ":0", GH_TOKEN: "secret", COPILOT_SDK_AUTH_TOKEN: "x" },
    },
    setOsascriptFails() {
      osascriptFails = true;
    },
  };
}

const CLEAN_ENV = { HOME: "/home/u", PATH: "/usr/bin", DISPLAY: ":0" };

test("on Linux the app gets SIGTERM, then is relaunched with the session's environment once it exits", async () => {
  const fake = fakeProcess();
  assert.equal(await restartApp(TARGET, fake.deps), "relaunched");
  assert.deepEqual(fake.calls, [
    ["kill", 4242, "SIGTERM"],
    ["launch", "/usr/bin/github", [], CLEAN_ENV],
  ]);
});

test("a failed relaunch is retried once", async () => {
  const twice = fakeProcess({ launches: [false, true] });
  assert.equal(await restartApp(TARGET, twice.deps), "relaunched");
  assert.equal(twice.calls.filter(([kind]) => kind === "launch").length, 2);

  const never = fakeProcess({ launches: [false, false, true] });
  assert.equal(await restartApp(TARGET, never.deps), "relaunch_failed");
  assert.equal(never.calls.filter(([kind]) => kind === "launch").length, 2);
});

test("an app that doesn't exit is left running, not relaunched", async () => {
  const fake = fakeProcess({ exitAfterPolls: Infinity });
  assert.equal(await restartApp(TARGET, fake.deps), "still_running");
  assert.ok(fake.clock.t <= EXIT_TIMEOUT_MS + 2_000);
  assert.deepEqual(
    fake.calls.map(([kind]) => kind),
    ["kill"],
  );
});

test("on macOS the app is asked to quit, then opened by its bundle id", async () => {
  const target = { platform: "darwin", pid: 4242, app: "com.github.githubapp" };
  const fake = fakeProcess();
  assert.equal(await restartApp(target, fake.deps), "relaunched");
  assert.deepEqual(fake.calls, [
    ["run", "/usr/bin/osascript", "-e", 'quit app id "com.github.githubapp"'],
    ["launch", "/usr/bin/open", ["-b", "com.github.githubapp"], CLEAN_ENV],
  ]);

  const refused = fakeProcess();
  refused.setOsascriptFails();
  assert.equal(await restartApp(target, refused.deps), "relaunched");
  assert.deepEqual(refused.calls[1], ["kill", 4242, "SIGTERM"]);
});

test("on Windows the app's window is closed without /F, and an app left in the tray isn't relaunched", async () => {
  const target = { platform: "win32", pid: 4242, app: "C:\\Program Files\\GitHub\\github.exe" };
  const fake = fakeProcess();
  assert.equal(await restartApp(target, fake.deps), "relaunched");
  assert.deepEqual(fake.calls[0], ["run", "taskkill", "/PID", "4242"]);
  assert.deepEqual(fake.calls[1].slice(0, 3), ["launch", target.app, []]);

  const tray = fakeProcess({ exitAfterPolls: Infinity });
  assert.equal(await restartApp(target, tray.deps), "still_running");
  assert.ok(tray.calls.every(([kind, command]) => kind === "run" && command === "taskkill"));
});

test("sessionEnv keeps the user's session variables and drops the runtime's", () => {
  const env = {
    HOME: "/h",
    PATH: "/usr/bin",
    DISPLAY: ":0",
    WAYLAND_DISPLAY: "wayland-0",
    XDG_RUNTIME_DIR: "/run/user/1",
    DBUS_SESSION_BUS_ADDRESS: "unix:path=/run/user/1/bus",
    LC_ALL: "C.UTF-8",
    https_proxy: "http://proxy",
    COPILOT_HOME: "/h/.copilot",
    COPILOT_SDK_AUTH_TOKEN: "x",
    COPILOT_AGENT_SESSION_ID: "s",
    COPILOT_PLUGIN_ROOT: "/p",
    GH_TOKEN: "t",
    GIT_CONFIG_COUNT: "1",
    SESSION_ID: "s",
    NODE_OPTIONS: "--x",
    ELECTRON_RUN_AS_NODE: "1",
    OTEL_EXPORTER_OTLP_ENDPOINT: "http://otel",
  };
  assert.deepEqual(Object.keys(sessionEnv(env)).sort(), [
    "COPILOT_HOME",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "HOME",
    "LC_ALL",
    "PATH",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "https_proxy",
  ]);
});

test("the helper logs the outcome and never throws", async () => {
  const log = join(mkdtempSync(join(tmpdir(), "ug-copilot-state-")), "app-restart.log");
  const fake = fakeProcess();
  await runHelper(JSON.stringify({ ...TARGET, home: emptyHome(), log }), fake.deps);
  assert.match(readFileSync(log, "utf8"), /not_running\n$/);
  await runHelper("not json", fake.deps);
  const home = homeWithPid("1");
  writeFileSync(join(home, "run", "ws.token"), "t");
  const failing = { ...fake.deps, WebSocketImpl: class { constructor() { throw new Error("boom"); } } };
  await runHelper(JSON.stringify({ ...TARGET, home, log }), failing);
  assert.match(readFileSync(log, "utf8"), /no_session_events\n$/);
});
