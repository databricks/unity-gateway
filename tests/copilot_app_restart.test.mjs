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
  createActivity,
  planRestart,
  restartApp,
  restartWhenIdle,
  runHelper,
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

function fakeSession() {
  const logs = [];
  return { logs, log: async (message, options) => logs.push([message, options.level]) };
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

function restartOptions(clock, overrides = {}) {
  const started = [];
  return {
    started,
    options: {
      home: emptyHome(),
      stateDir: mkdtempSync(join(tmpdir(), "ug-copilot-state-")),
      plan: async () => TARGET,
      start: async (target) => (started.push({ target, at: clock.t }), true),
      now: clock.now,
      sleep: clock.sleep,
      ...overrides,
    },
  };
}

test("an idle app restarts after a quiet period, with one line in the chat first", async () => {
  const clock = fakeClock();
  const session = fakeSession();
  const activity = createActivity(clock.now);
  const { started, options } = restartOptions(clock);
  assert.equal(await restartWhenIdle(session, activity, options), "restarting");
  assert.deepEqual(started, [{ target: TARGET, at: QUIET_MS }]);
  assert.deepEqual(session.logs, [[RESTARTING_MESSAGE, "info"]]);
});

test("a running turn delays the restart until its session is idle", async () => {
  const clock = fakeClock();
  const session = fakeSession();
  const activity = createActivity(clock.now);
  activity.note("chat", "assistant.turn_start");
  clock.onSleep = (t) => {
    if (t === 60_000) activity.note("chat", "session.idle");
  };
  const { started, options } = restartOptions(clock);
  assert.equal(await restartWhenIdle(session, activity, options), "restarting");
  assert.deepEqual(
    started.map(({ at }) => at),
    [60_000 + QUIET_MS],
  );
});

test("a session that never goes idle gives up after the bound and asks for a restart", async () => {
  const clock = fakeClock();
  const session = fakeSession();
  const activity = createActivity(clock.now);
  activity.note("chat", "user.message");
  const { started, options } = restartOptions(clock);
  assert.equal(await restartWhenIdle(session, activity, options), "busy");
  assert.equal(clock.t, IDLE_TIMEOUT_MS);
  assert.deepEqual(started, []);
  assert.deepEqual(session.logs, [[RESTART_HINT, "info"]]);
});

test("events that keep coming hold the restart off even without a turn start", async () => {
  const clock = fakeClock();
  const activity = createActivity(clock.now);
  clock.onSleep = () => activity.note("chat", "assistant.streaming_delta");
  const { started, options } = restartOptions(clock);
  assert.equal(await restartWhenIdle(fakeSession(), activity, options), "busy");
  assert.deepEqual(started, []);
});

test("when the app can't be restarted here, the chat gets the restart hint", async () => {
  const clock = fakeClock();
  for (const overrides of [
    { plan: async () => null },
    { plan: async () => Promise.reject(new Error("boom")) },
    { start: async () => false },
  ]) {
    const session = fakeSession();
    const { started, options } = restartOptions(clock, overrides);
    const result = await restartWhenIdle(session, createActivity(clock.now), options);
    assert.notEqual(result, "restarting");
    assert.deepEqual(started, []);
    assert.deepEqual(session.logs.at(-1), [RESTART_HINT, "info"]);
  }
});

test("a session whose log fails doesn't make the restart throw", async () => {
  const clock = fakeClock();
  const { options } = restartOptions(clock, { plan: async () => null });
  const session = {
    log: () => {
      throw new Error("closed");
    },
  };
  assert.equal(await restartWhenIdle(session, createActivity(clock.now), options), "unsupported");
});

test(
  "a turn in a chat the app's window runs delays the restart",
  { skip: typeof WebSocket === "function" ? false : "this Node has no global WebSocket" },
  async (t) => {
    const app = new FakeApp();
    await app.listen();
    t.after(() => app.close());
    const clock = fakeClock();
    const activity = createActivity(clock.now);
    const { started, options } = restartOptions(clock, { timeoutMs: 600_000 });
    app.writeRunFiles(options.home);
    let watchers;
    clock.onSleep = async (time) => {
      if (time === 5_000) {
        watchers = app.watchers.size;
        app.emit("ui-chat", "assistant.turn_start");
      }
      if (time === 90_000) app.emit("ui-chat", "session.idle");
      await delay(2);
    };
    assert.equal(await restartWhenIdle(fakeSession(), activity, options), "restarting");
    assert.ok(started[0].at >= 90_000 + QUIET_MS, `restarted at ${started[0].at}`);
    assert.ok(started[0].at <= 90_000 + QUIET_MS + 5_000, `restarted at ${started[0].at}`);
    assert.equal(watchers, 1);
  },
);

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
  await runHelper(JSON.stringify({ ...TARGET, log }), fake.deps);
  assert.match(readFileSync(log, "utf8"), /relaunched\n$/);
  await runHelper("not json", fake.deps);
  const failing = { ...fake.deps, kill: () => {}, alive: () => true, sleep: () => Promise.reject(new Error("boom")) };
  await runHelper(JSON.stringify({ ...TARGET, log }), failing);
  assert.match(readFileSync(log, "utf8"), /failed: boom\n$/);
});
