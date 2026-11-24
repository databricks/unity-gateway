import { existsSync } from "node:fs";
import { join } from "node:path";

// The app may start its extensions with a trimmed PATH, so the Windows tools the extension needs
// are run from the system folder when they are there.
const TOOL_DIRS = {
  "powershell.exe": ["System32", "WindowsPowerShell", "v1.0"],
  "reg.exe": ["System32"],
};
const DEFAULT_SYSTEM_ROOT = "C:\\Windows";

export function windowsTool(name, { env = process.env, exists = existsSync } = {}) {
  const dirs = TOOL_DIRS[name];
  if (!dirs) return name;
  const root = env.SystemRoot || env.SYSTEMROOT || DEFAULT_SYSTEM_ROOT;
  const path = join(root, ...dirs, name);
  return exists(path) ? path : name;
}
