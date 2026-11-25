import { execFile } from "node:child_process";

import { NO_DEBUG } from "./app-debug.mjs";
import { windowsTool } from "./app-windows.mjs";

// The proxy the operating system would use for a URL, for machines whose proxy lives in the system
// settings rather than the environment. Windows answers through .NET, which evaluates manual
// settings, bypass lists, PAC files and auto-detect the way the OS does; macOS gives its manual
// proxy, with PAC left unresolved; elsewhere there is nothing to ask.
export const SYSTEM_PROXY_TIMEOUT_MS = 10_000;
export const SYSTEM_PROXY_RETRY_MS = 30_000;
const SAFE_ORIGIN = /^https?:\/\/[A-Za-z0-9.\-_:[\]]+\/$/;

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

export function originOf(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? `${parsed.origin}/` : null;
  } catch {
    return null;
  }
}

export function windowsProxyScript(origin) {
  if (!SAFE_ORIGIN.test(origin)) throw new Error("unsupported URL for the proxy lookup");
  return (
    `$u=[Uri]'${origin}';$p=[System.Net.WebRequest]::GetSystemWebProxy();` +
    "@{Proxy=$p.GetProxy($u).AbsoluteUri;Bypassed=$p.IsBypassed($u)}|ConvertTo-Json -Compress"
  );
}

// `GetProxy` hands back the request URL itself when the request goes direct.
export function parseSystemProxyReply(output, origin) {
  const parsed = JSON.parse(String(output).trim());
  const bypassed = parsed.Bypassed === true;
  const proxy = typeof parsed.Proxy === "string" ? originOf(parsed.Proxy) : null;
  if (!proxy || bypassed || proxy === origin) return { proxy: null, bypassed };
  return { proxy: proxy.slice(0, -1), bypassed };
}

function hostMatches(host, pattern) {
  const wanted = pattern.toLowerCase().replace(/^\*\./, ".");
  return wanted.startsWith(".") ? host.endsWith(wanted) || host === wanted.slice(1) : host === wanted;
}

export function macosSystemProxy(output, origin) {
  const { settings, exceptions } = parseScutil(output);
  if (settings.ProxyAutoConfigEnable === "1" || settings.ProxyAutoDiscoveryEnable === "1") {
    return { proxy: null, bypassed: false, unresolved: "pac" };
  }
  const url = new URL(origin);
  const host = url.hostname.toLowerCase();
  if (host === "127.0.0.1" || host === "localhost" || exceptions.some((entry) => hostMatches(host, entry))) {
    return { proxy: null, bypassed: true };
  }
  const secure = url.protocol === "https:" && settings.HTTPSEnable === "1" && settings.HTTPSProxy;
  const plain = url.protocol === "http:" && settings.HTTPEnable === "1" && settings.HTTPProxy;
  if (secure) return { proxy: `http://${settings.HTTPSProxy}:${settings.HTTPSPort || 80}`, bypassed: false };
  if (plain) return { proxy: `http://${settings.HTTPProxy}:${settings.HTTPPort || 80}`, bypassed: false };
  return { proxy: null, bypassed: false };
}

function runCommand(command, args) {
  return new Promise((resolve, reject) => {
    execFile(command, args, { timeout: SYSTEM_PROXY_TIMEOUT_MS, windowsHide: true }, (error, stdout) =>
      error ? reject(error) : resolve(String(stdout)),
    );
  });
}

// One lookup per origin for the life of the process; an answer can't be assumed stable longer. A
// failed lookup reads as direct, so it is asked again after a while.
export function createSystemProxyResolver({
  platform = process.platform,
  run = runCommand,
  debug = NO_DEBUG,
  now = Date.now,
  retryMs = SYSTEM_PROXY_RETRY_MS,
} = {}) {
  const cache = new Map();
  const failedAt = new Map();
  async function lookup(origin) {
    if (platform === "win32") {
      const output = await run(windowsTool("powershell.exe"), [
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        windowsProxyScript(origin),
      ]);
      return parseSystemProxyReply(output, origin);
    }
    if (platform === "darwin") return macosSystemProxy(await run("scutil", ["--proxy"]), origin);
    return { proxy: null, bypassed: false };
  }
  return function resolveSystemProxy(url) {
    const origin = originOf(url);
    if (!origin) return Promise.resolve({ proxy: null, bypassed: false, error: "unsupported URL" });
    const failed = failedAt.get(origin);
    if (cache.has(origin) && (failed === undefined || now() - failed < retryMs)) return cache.get(origin);
    failedAt.delete(origin);
    const started = Date.now();
    const pending = lookup(origin)
      .catch((error) => {
        failedAt.set(origin, now());
        return { proxy: null, bypassed: false, error: String(error?.code ?? error?.message ?? error) };
      })
      .then((result) => {
        debug("proxy.resolve", { url: origin, platform, ...result, ms: Date.now() - started });
        return result;
      });
    cache.set(origin, pending);
    return pending;
  };
}
