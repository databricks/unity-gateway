import { execFile, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

import { copilotHome, readAppPid } from "./app-rows.mjs";
import { windowsTool } from "./app-windows.mjs";

export const RESTARTING_MESSAGE =
  "The GitHub Copilot app restarts once, as soon as no chat is running a turn, to finish loading the Databricks models.";
export const RESTART_HINT = "Restart the GitHub Copilot app to show the Databricks model names and icons.";
export const RESTART_LOG_FILE = "app-restart.log";
const COMMAND_TIMEOUT_MS = 10_000;
const LINUX_APP = "/usr/bin/github";
const MAC_BUNDLE_ID = "com.github.githubapp";

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

// What the restart needs to close the app and start it again, or null when it can't here.
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
    const app = (await run(windowsTool("powershell.exe"), ["-NoProfile", "-NonInteractive", "-Command", command]))?.trim();
    return app ? { platform, pid, app } : null;
  }
  return null;
}

// The app stops the extension soon after its rows change, so the wait for idle and the restart run
// in a detached `ug copilot-app-restart`. The extension's own executable is the Copilot runtime,
// which runs no scripts.
export function startHelper(target, stateDir, ugPath) {
  return new Promise((resolve) => {
    const argument = JSON.stringify({ ...target, log: join(stateDir, RESTART_LOG_FILE) });
    const child = spawn(ugPath, ["copilot-app-restart", argument], {
      detached: true,
      stdio: "ignore",
      windowsHide: true,
    });
    child.once("spawn", () => {
      child.unref();
      resolve(true);
    });
    child.once("error", () => resolve(false));
  });
}

// Started as soon as the sync returns, before the app can stop this process. Never throws.
export async function launchRestart({ home = copilotHome(), stateDir, ugPath, plan = planRestart, start = startHelper } = {}) {
  try {
    const target = ugPath ? await plan({ home }) : null;
    if (!target) return "unsupported";
    return (await start({ ...target, home }, stateDir, ugPath)) ? "started" : "failed";
  } catch {
    return "failed";
  }
}

// Posts the line for ``launchRestart``'s result. Never throws.
export async function tellRestart(session, result) {
  try {
    await session.log(result === "started" ? RESTARTING_MESSAGE : RESTART_HINT, { level: "info" });
  } catch {}
}
