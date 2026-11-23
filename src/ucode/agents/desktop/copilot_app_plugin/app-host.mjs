import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";

import { NO_DEBUG } from "./app-debug.mjs";
import { copilotHome, readAppPid, readAppPidSource } from "./app-rows.mjs";
import { windowsTool } from "./app-windows.mjs";

// The Copilot CLI and VS Code's Copilot runtime read the same settings file as the desktop app,
// so with Copilot's experimental features on they start this extension too. Only the app's own
// sessions get ug's rows, restart and redirect: in a CLI session they would replace the session's
// models. The app is the ancestor process whose pid it writes to run/app-owner.pid; the app's
// runtime also marks its extensions with COPILOT_EXTENSION_SANDBOXED, which covers a sandboxed
// session whose process tree is hidden.
export const SANDBOX_MARKER_ENV = "COPILOT_EXTENSION_SANDBOXED";
export const MAX_ANCESTORS = 12;
// A cold PowerShell with endpoint security can take several seconds to list the processes.
const COMMAND_TIMEOUT_MS = 10_000;

function parentFromProc(pid) {
  try {
    const stat = readFileSync(`/proc/${pid}/stat`, "utf8");
    const fields = stat.slice(stat.lastIndexOf(")") + 2).split(" ");
    const ppid = Number.parseInt(fields[1], 10);
    return Number.isInteger(ppid) && ppid > 0 ? ppid : null;
  } catch {
    return null;
  }
}

// Resolves to the command's output, or null when it failed; `details` collects what happened.
function runCommand(command, args, details = {}) {
  const started = Date.now();
  return new Promise((resolve) => {
    execFile(command, args, { timeout: COMMAND_TIMEOUT_MS, windowsHide: true }, (error, stdout, stderr) => {
      details.command = command;
      details.args = args;
      details.ms = Date.now() - started;
      details.stdoutLength = String(stdout ?? "").length;
      if (error) {
        details.error = String(error.code ?? error.message);
        details.killed = Boolean(error.killed);
        details.stderr = String(stderr ?? "").slice(0, 300);
      }
      resolve(error ? null : String(stdout));
    });
  });
}

// `pid ppid` per line, from `ps` or PowerShell.
export function parsePidTable(output) {
  const parents = new Map();
  for (const line of String(output ?? "").split(/\r?\n/)) {
    const match = /^\s*(\d+)\s+(\d+)\s*$/.exec(line);
    if (match) parents.set(Number(match[1]), Number(match[2]));
  }
  return parents;
}

async function parentLookup(platform, run, details = {}) {
  if (platform === "linux") return parentFromProc;
  let output = null;
  if (platform === "darwin") output = await run("ps", ["-axo", "pid=,ppid="], details);
  if (platform === "win32") {
    output = await run(
      windowsTool("powershell.exe"),
      [
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId)\" }",
      ],
      details,
    );
  }
  if (output === null) return null;
  const parents = parsePidTable(output);
  details.pairs = parents.size;
  details.firstPairs = [...parents.entries()].slice(0, 20);
  return (pid) => parents.get(pid) ?? null;
}

export function ancestorsOf(pid, parentOf, limit = MAX_ANCESTORS) {
  const ancestors = [];
  let current = pid;
  while (ancestors.length < limit && Number.isInteger(current) && current > 1) {
    ancestors.push(current);
    const parent = parentOf(current);
    if (parent === null || parent === current || ancestors.includes(parent)) break;
    current = parent;
  }
  return ancestors;
}

// `ancestors` null means the process tree couldn't be read.
export function hostDecision({ appPid, ancestors, env = {} }) {
  if (appPid && ancestors?.includes(appPid)) return { app: true, via: "ancestry" };
  if (SANDBOX_MARKER_ENV in env) return { app: true, via: "sandbox marker" };
  return { app: false, via: ancestors === null ? "no process tree" : "ancestry" };
}

export async function runningUnderApp({
  home = copilotHome(),
  env = process.env,
  platform = process.platform,
  pid = process.pid,
  run = runCommand,
  debug = NO_DEBUG,
} = {}) {
  const started = Date.now();
  const appPid = readAppPid(home);
  const details = {};
  const parentOf = await parentLookup(platform, run, details);
  const ancestors = parentOf ? ancestorsOf(pid, parentOf) : null;
  const decision = hostDecision({ appPid, ancestors, env });
  debug("host.check", {
    platform,
    pid,
    appPid,
    appPidSource: readAppPidSource(home),
    home,
    ancestors,
    sandboxMarker: env[SANDBOX_MARKER_ENV] ?? null,
    ...details,
    ...decision,
    ms: Date.now() - started,
  });
  return decision;
}
