import { execFile } from "node:child_process";

import { windowsTool } from "./app-windows.mjs";

// The relay URL is plain HTTP on 127.0.0.1. A proxy the runtime routes loopback through would get
// every relay request with the GitHub token in it, so the redirect is skipped when one may.
const PROXY_ENV = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"];
const LOOPBACK_HOSTS = ["127.0.0.1", "localhost"];
const COMMAND_TIMEOUT_MS = 1_500;
const WININET_KEY = "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings";

function envValue(env, name) {
  return env[name] ?? env[name.toLowerCase()] ?? "";
}

function coversLoopback(list) {
  const entries = list
    .split(/[,;]/)
    .map((entry) => entry.trim().toLowerCase())
    .filter(Boolean);
  return entries.includes("*") || entries.some((entry) => LOOPBACK_HOSTS.some((host) => entry.startsWith(host)));
}

function envCapture(env) {
  const set = PROXY_ENV.filter((name) => envValue(env, name));
  if (set.length === 0) return null;
  const exceptions = [envValue(env, "NO_PROXY")].filter(Boolean).join(",");
  return coversLoopback(exceptions) ? null : `${set[0]} without a loopback exception in NO_PROXY`;
}

// `scutil --proxy`: `  Key : value` lines, with ExceptionsList as a nested `<array>` block.
export function parseScutil(output) {
  const settings = {};
  const exceptions = [];
  let inExceptions = false;
  for (const raw of String(output ?? "").split(/\r?\n/)) {
    const line = raw.trim();
    if (inExceptions) {
      if (line === "}") inExceptions = false;
      else exceptions.push(line.replace(/^\d+\s*:\s*/, ""));
      continue;
    }
    const match = /^(\w+)\s*:\s*(.*)$/.exec(line);
    if (!match) continue;
    if (match[1] === "ExceptionsList") inExceptions = true;
    else settings[match[1]] = match[2];
  }
  return { settings, exceptions };
}

// System proxies stay a skip reason even with a loopback exception: whether the runtime bypasses
// loopback for them is unverified, unlike the env variables.
function scutilCapture(output) {
  const { settings, exceptions } = parseScutil(output);
  if (settings.ProxyAutoConfigEnable === "1") return "a macOS PAC proxy";
  if (settings.ProxyAutoDiscoveryEnable === "1") return "macOS proxy auto-discovery";
  const manual = settings.HTTPEnable === "1" || settings.HTTPSEnable === "1";
  if (manual && !coversLoopback(exceptions.join(","))) return "a macOS proxy without a loopback exception";
  return null;
}

// `reg query` output: `    Name    REG_TYPE    value` lines.
export function parseRegQuery(output) {
  const values = {};
  for (const line of String(output ?? "").split(/\r?\n/)) {
    const match = /^\s+(\S+)\s+(REG_\w+)\s*(.*)$/.exec(line);
    if (match) values[match[1]] = match[3].trim();
  }
  return values;
}

// Byte 8 of DefaultConnectionSettings holds the connection flags: 0x04 PAC, 0x08 auto-detect.
// Auto-detect is on by default on Windows and only matters on a network that publishes a PAC, so it
// isn't a capture on its own.
function connectionFlags(values) {
  const hex = values.DefaultConnectionSettings ?? "";
  return hex.length >= 18 ? Number.parseInt(hex.slice(16, 18), 16) : 0;
}

function registryCapture(settingsOutput, connectionsOutput) {
  const settings = parseRegQuery(settingsOutput);
  const flags = connectionFlags(parseRegQuery(connectionsOutput));
  if (settings.AutoConfigURL || flags & 0x04) return "a Windows PAC proxy";
  // WinINET leaves loopback alone unless the bypass list says <-loopback>.
  if (/0x1$/i.test(settings.ProxyEnable ?? "") && /<-loopback>/i.test(settings.ProxyOverride ?? "")) {
    return "a Windows proxy that includes loopback";
  }
  return null;
}

// Pure: { captured, reason }. `scutil` and `reg` are the system outputs, null when not read.
export function loopbackProxyCapture({ env = {}, platform = "linux", scutil = null, reg = null, connections = null } = {}) {
  const reason =
    envCapture(env) ??
    (platform === "darwin" ? scutilCapture(scutil) : null) ??
    (platform === "win32" ? registryCapture(reg, connections) : null);
  return { captured: reason !== null, reason };
}

function runCommand(command, args) {
  return new Promise((resolve, reject) => {
    execFile(command, args, { timeout: COMMAND_TIMEOUT_MS, windowsHide: true }, (error, stdout) =>
      error ? reject(error) : resolve(String(stdout)),
    );
  });
}

// Reads the system proxy settings for this platform, then decides. A failed read counts as no
// capture, reported in `error`, so a broken tool doesn't turn the relay off.
export async function detectLoopbackProxy({ env = process.env, platform = process.platform, run = runCommand } = {}) {
  const fromEnv = loopbackProxyCapture({ env, platform });
  if (fromEnv.captured) return fromEnv;
  try {
    if (platform === "darwin") {
      return loopbackProxyCapture({ env, platform, scutil: await run("scutil", ["--proxy"]) });
    }
    if (platform === "win32") {
      const regExe = windowsTool("reg.exe");
      const reg = await run(regExe, ["query", WININET_KEY]);
      const connections = await run(regExe, ["query", `${WININET_KEY}\\Connections`, "/v", "DefaultConnectionSettings"]).catch(
        () => "",
      );
      return loopbackProxyCapture({ env, platform, reg, connections });
    }
  } catch (error) {
    return { captured: false, reason: null, error: String(error?.code ?? error?.message ?? error) };
  }
  return fromEnv;
}
