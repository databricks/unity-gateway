import { execFile } from "node:child_process";

import { NO_DEBUG } from "./app-debug.mjs";
import { createSystemProxyResolver, parseScutil } from "./app-system-proxy.mjs";
import { windowsTool } from "./app-windows.mjs";

export { parseScutil };

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

const WINDOWS_LOOPBACK_REASON = "a Windows proxy that includes loopback";

function registryCapture(settingsOutput, connectionsOutput) {
  const settings = parseRegQuery(settingsOutput);
  const flags = connectionFlags(parseRegQuery(connectionsOutput));
  if (settings.AutoConfigURL || flags & 0x04) return "a Windows PAC proxy";
  // WinINET leaves loopback alone unless the bypass list says <-loopback>.
  if (/0x1$/i.test(settings.ProxyEnable ?? "") && /<-loopback>/i.test(settings.ProxyOverride ?? "")) {
    return WINDOWS_LOOPBACK_REASON;
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
// Windows answers for the relay's own loopback URL through the system resolver, which evaluates
// PAC files and bypass lists the way the OS does; the registry reading is the fallback when that
// fails. The relay must be listening for its port to be known.
function windowsHostOf(proxy) {
  try {
    return new URL(proxy).host;
  } catch {
    return proxy;
  }
}

// .NET Framework's resolver (Windows PowerShell) always bypasses loopback, so it can't overrule an
// explicit <-loopback>.
async function windowsDecision({ registry, port, resolve }) {
  if (registry.reason === WINDOWS_LOOPBACK_REASON || !port || !resolve) {
    return { ...registry, decision: { registry, resolver: null } };
  }
  const resolver = await resolve(`http://127.0.0.1:${port}/`);
  if (resolver.error) return { ...registry, decision: { registry, resolver } };
  const captured = Boolean(resolver.proxy) && !resolver.bypassed;
  return {
    captured,
    reason: captured ? `the Windows proxy settings route loopback through ${windowsHostOf(resolver.proxy)}` : null,
    decision: { registry, resolver },
  };
}

export async function detectLoopbackProxy({
  env = process.env,
  platform = process.platform,
  run = runCommand,
  debug = NO_DEBUG,
  port = null,
  resolve = platform === "win32" ? createSystemProxyResolver({ platform, debug }) : null,
} = {}) {
  const started = Date.now();
  const fromEnv = loopbackProxyCapture({ env, platform });
  const proxyEnv = Object.fromEntries(
    PROXY_ENV.concat("NO_PROXY").flatMap((name) => [name, name.toLowerCase()]).filter((k) => env[k]).map((k) => [k, env[k]]),
  );
  debug("proxy.env", { platform, ...fromEnv, env: proxyEnv });
  if (fromEnv.captured) return fromEnv;
  try {
    if (platform === "darwin") {
      const scutil = await run("scutil", ["--proxy"]);
      const result = loopbackProxyCapture({ env, platform, scutil });
      debug("proxy.system", { platform, scutil, ...parseScutil(scutil), ...result, ms: Date.now() - started });
      return result;
    }
    if (platform === "win32") {
      const regExe = windowsTool("reg.exe");
      const reg = await run(regExe, ["query", WININET_KEY]).catch(
        (error) => (debug("proxy.system.settings", { error: String(error?.code ?? error?.message ?? error) }), ""),
      );
      const connections = await run(regExe, ["query", `${WININET_KEY}\\Connections`, "/v", "DefaultConnectionSettings"]).catch(
        (error) => (debug("proxy.system.connections", { error: String(error?.code ?? error?.message ?? error) }), ""),
      );
      const registry = loopbackProxyCapture({ env, platform, reg, connections });
      const result = await windowsDecision({ registry, port, resolve });
      debug("proxy.system", {
        platform,
        regExe,
        reg,
        connections,
        settings: parseRegQuery(reg),
        connectionFlags: connectionFlags(parseRegQuery(connections)),
        port,
        ...result,
        ms: Date.now() - started,
      });
      return result;
    }
  } catch (error) {
    const failure = { captured: false, reason: null, error: String(error?.code ?? error?.message ?? error) };
    debug("proxy.system", { platform, ...failure, ms: Date.now() - started });
    return failure;
  }
  return fromEnv;
}
