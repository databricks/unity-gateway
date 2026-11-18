import { existsSync } from "node:fs";
import { chmod, mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

/** The user setting that makes VS Code's Copilot agent host run another executable as its Copilot runtime. */
export const RUNTIME_PATH_SETTING = "chat.agentHost.copilot.runtimePath";

/** ug's revert finds the wrapper by this name (RUNTIME_WRAPPER_NAME in src/ucode/agents/vscode.py). */
export const RUNTIME_WRAPPER_NAME = "ug-copilot-runtime";

export interface SeatRelaySetting {
  /** The ug command that runs the runtime through the seat relay; the wrapper adds `--runtime <path> -- <args>`. */
  readonly command: readonly string[];
}

export const seatRelaySetting = (value: unknown): SeatRelaySetting | undefined => {
  const command = value && typeof value === "object" ? (value as Record<string, unknown>).command : undefined;
  return Array.isArray(command) && command.length > 0 && command.every((part) => typeof part === "string" && part !== "")
    ? { command }
    : undefined;
};

/** The Copilot runtime VS Code bundles under `appRoot` (`vscode.env.appRoot`). */
export const bundledRuntime = (appRoot: string, platform: string = process.platform, arch: string = process.arch): string => {
  const target = `${platform}-${arch}`;
  const binary = platform === "win32" ? "copilot-runtime.exe" : "copilot-runtime";
  return join(appRoot, "node_modules.asar.unpacked", "@github", `copilot-sdk-${target}`, "prebuilds", target, binary);
};

const shellQuote = (part: string): string => `'${part.replaceAll("'", `'\\''`)}'`;

/** A POSIX shell script that runs `command` on `runtime` with the arguments VS Code passes. */
export const wrapperScript = (command: readonly string[], runtime: string): string =>
  `#!/bin/sh\nexec ${[...command, "--runtime", runtime, "--"].map(shellQuote).join(" ")} "$@"\n`;

/**
 * Writes the runtime wrapper into `dir` and returns its path, or undefined when it can't be used here: VS Code has no
 * bundled runtime, or runs on Windows, where it can't start a script as the runtime.
 */
export const writeWrapper = async (
  dir: string,
  setting: SeatRelaySetting,
  appRoot: string,
  log: (line: string) => void,
  platform: string = process.platform,
  arch: string = process.arch,
): Promise<string | undefined> => {
  if (platform === "win32") {
    log("GitHub Copilot can't go through the Unity Gateway seat relay in VS Code on Windows yet.");
    return undefined;
  }
  const runtime = bundledRuntime(appRoot, platform, arch);
  if (!existsSync(runtime)) {
    log(`This VS Code has no Copilot runtime at ${runtime}, so GitHub Copilot doesn't go through Unity Gateway.`);
    return undefined;
  }
  await mkdir(dir, { recursive: true });
  const path = join(dir, RUNTIME_WRAPPER_NAME);
  await writeFile(path, wrapperScript(setting.command, runtime), { mode: 0o700 });
  // `mode` applies only when the file is created.
  await chmod(path, 0o700);
  return path;
};
