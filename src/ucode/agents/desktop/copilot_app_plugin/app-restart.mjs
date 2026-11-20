import { execFile, spawn } from "node:child_process";
import { appendFileSync, existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

import { connectApp, copilotHome, readAppPid, readEndpoint } from "./app-rows.mjs";

export const RESTARTING_MESSAGE = "Restarting the GitHub Copilot app once to finish loading the Databricks models.";
export const RESTART_HINT = "Restart the GitHub Copilot app to show the Databricks model names and icons.";
export const RESTART_LOG_FILE = "app-restart.log";
export const QUIET_MS = 15_000;
export const IDLE_TIMEOUT_MS = 180_000;
export const EXIT_TIMEOUT_MS = 15_000;
const IDLE_POLL_MS = 1_000;
const EXIT_POLL_MS = 100;
const CLOSE_DELAY_MS = 1_000;
const RELAUNCH_RETRY_MS = 2_000;
const LAUNCH_CHECK_MS = 3_000;
const COMMAND_TIMEOUT_MS = 10_000;
const LINUX_APP = "/usr/bin/github";
const MAC_BUNDLE_ID = "com.github.githubapp";
const HELPER = fileURLToPath(new URL("./restart-helper.mjs", import.meta.url));
const TURN_STARTS = new Set(["user.message", "assistant.turn_start"]);

// A running turn sends events all the time, so the app is idle once no turn is open and no session
// has sent anything for a while. That also covers a turn that started before the watch did.
export function createActivity(now = Date.now) {
  const running = new Set();
  let last = now();
  return {
    note(sessionId, type) {
      last = now();
      if (type === "session.idle") running.delete(sessionId);
      else if (TURN_STARTS.has(type)) running.add(sessionId);
    },
    idle: (quietMs) => running.size === 0 && now() - last >= quietMs,
  };
}

export async function waitForIdle(
  activity,
  { quietMs = QUIET_MS, timeoutMs = IDLE_TIMEOUT_MS, now = Date.now, sleep = delay } = {},
) {
  const deadline = now() + timeoutMs;
  for (;;) {
    if (activity.idle(quietMs)) return true;
    if (now() >= deadline) return false;
    await sleep(IDLE_POLL_MS);
  }
}

export function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === "EPERM";
  }
}

// Resolves to the command's output, or null when it failed.
function runCommand(command, args) {
  return new Promise((resolve) => {
    execFile(command, args, { timeout: COMMAND_TIMEOUT_MS, windowsHide: true }, (error, stdout) =>
      resolve(error ? null : String(stdout)),
    );
  });
}

// What restartApp needs to close the app and start it again, or null when it can't here.
export async function planRestart({
  home = copilotHome(),
  platform = process.platform,
  env = process.env,
  alive = isAlive,
  exists = existsSync,
  run = runCommand,
} = {}) {
  const pid = readAppPid(home);
  if (!pid || pid === process.pid || !alive(pid)) return null;
  if (platform === "linux") {
    return (env.DISPLAY || env.WAYLAND_DISPLAY) && exists(LINUX_APP) ? { platform, pid, app: LINUX_APP } : null;
  }
  if (platform === "darwin") return { platform, pid, app: MAC_BUNDLE_ID };
  if (platform === "win32") {
    const command = `(Get-Process -Id ${pid}).Path`;
    const app = (await run("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", command]))?.trim();
    return app ? { platform, pid, app } : null;
  }
  return null;
}

// The extension exits with the app, so the restart runs in a detached process of its own.
export function startHelper(target, stateDir) {
  return new Promise((resolve) => {
    const argument = JSON.stringify({ ...target, log: join(stateDir, RESTART_LOG_FILE) });
    const child = spawn(process.execPath, [HELPER, argument], { detached: true, stdio: "ignore", windowsHide: true });
    child.once("spawn", () => {
      child.unref();
      resolve(true);
    });
    child.once("error", () => resolve(false));
  });
}

async function tryRestart(
  activity,
  say,
  { home = copilotHome(), stateDir, plan = planRestart, start = startHelper, now, sleep, quietMs, timeoutMs, ...connect },
) {
  const target = await plan({ home });
  if (!target) return "unsupported";
  const endpoint = readEndpoint(home);
  const onMessage = (message) => {
    if (message.type === "session_event") activity.note(message.session_id, message.event?.type);
  };
  // Without this connection, only this session's own turns are known.
  const watch = endpoint ? await connectApp(endpoint, { ...connect, onMessage }).catch(() => null) : null;
  try {
    if (!(await waitForIdle(activity, { quietMs, timeoutMs, now, sleep }))) return "busy";
  } finally {
    watch?.close();
  }
  await say(RESTARTING_MESSAGE);
  return (await start(target, stateDir)) ? "restarting" : "failed";
}

// Restarts the app once no chat is running a turn, or asks the user to restart it. Never throws.
export async function restartWhenIdle(session, activity, options = {}) {
  const say = (message) =>
    Promise.resolve()
      .then(() => session.log(message, { level: "info" }))
      .catch(() => {});
  const result = await tryRestart(activity, say, options).catch(() => "failed");
  if (result !== "restarting") await say(RESTART_HINT);
  return result;
}

// The extension has the Copilot runtime's environment, which adds the runtime's own settings and
// credentials. The app gets only the user's session environment back.
const SESSION_ENV = new Set([
  "HOME", "USER", "LOGNAME", "SHELL", "PATH", "LANG", "LANGUAGE", "TZ", "TMPDIR", "DISPLAY", "WAYLAND_DISPLAY",
  "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "DESKTOP_SESSION", "SSH_AUTH_SOCK", "COPILOT_HOME", "HTTP_PROXY",
  "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
  "USERPROFILE", "USERNAME", "USERDOMAIN", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
  "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
  "COMMONPROGRAMW6432", "ALLUSERSPROFILE", "PUBLIC", "COMPUTERNAME", "OS", "NUMBER_OF_PROCESSORS",
]);
const SESSION_ENV_PREFIXES = ["LC_", "XDG_", "PROCESSOR_"];

export function sessionEnv(env) {
  return Object.fromEntries(
    Object.entries(env).filter(([name]) => {
      const key = name.toUpperCase();
      return SESSION_ENV.has(key) || SESSION_ENV_PREFIXES.some((prefix) => key.startsWith(prefix));
    }),
  );
}

function launchDetached(command, args, env) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(command, args, { cwd: homedir(), detached: true, env, stdio: "ignore" });
    } catch {
      resolve(false);
      return;
    }
    // A launch that fails exits at once and nonzero.
    const timer = setTimeout(() => {
      child.unref();
      resolve(true);
    }, LAUNCH_CHECK_MS);
    child.once("error", () => {
      clearTimeout(timer);
      resolve(false);
    });
    child.once("exit", (code) => {
      clearTimeout(timer);
      resolve(code === 0);
    });
  });
}

function terminate(kill, pid) {
  try {
    kill(pid, "SIGTERM");
  } catch {}
}

// Closes the app and starts it again once it has exited. It's never forced closed, so an app that
// stays up is left running.
export async function restartApp(
  { platform, pid, app },
  {
    alive = isAlive,
    kill = (target, signal) => process.kill(target, signal),
    run = runCommand,
    launch = launchDetached,
    now = Date.now,
    sleep = delay,
    env = process.env,
  } = {},
) {
  await sleep(CLOSE_DELAY_MS);
  if (platform === "darwin") {
    if ((await run("/usr/bin/osascript", ["-e", `quit app id "${app}"`])) === null) terminate(kill, pid);
  } else if (platform === "win32") {
    // Without /F, taskkill closes the app's window as its close button does, which can leave the
    // app running in the tray.
    await run("taskkill", ["/PID", String(pid)]);
  } else {
    terminate(kill, pid);
  }
  const deadline = now() + EXIT_TIMEOUT_MS;
  while (alive(pid)) {
    if (now() >= deadline) return "still_running";
    await sleep(EXIT_POLL_MS);
  }
  const [command, args] = platform === "darwin" ? ["/usr/bin/open", ["-b", app]] : [app, []];
  for (let attempt = 0; attempt < 2; attempt++) {
    if (attempt) await sleep(RELAUNCH_RETRY_MS);
    if (await launch(command, args, sessionEnv(env))) return "relaunched";
  }
  return "relaunch_failed";
}

export async function runHelper(argument, deps) {
  let target;
  try {
    target = JSON.parse(argument);
  } catch {
    return;
  }
  const log = (line) => {
    try {
      appendFileSync(target.log, `${new Date().toISOString()} ${line}\n`);
    } catch {}
  };
  try {
    log(await restartApp(target, deps));
  } catch (error) {
    log(`failed: ${error?.message ?? error}`);
  }
}
