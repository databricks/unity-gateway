import assert from "node:assert/strict";
import { test } from "node:test";

import {
  detectLoopbackProxy,
  loopbackProxyCapture,
  parseRegQuery,
  parseScutil,
} from "../src/ucode/agents/desktop/copilot_app_plugin/app-proxy.mjs";

const SCUTIL_MANUAL = `<dictionary> {
  ExceptionsList : <array> {
    0 : *.local
    1 : 169.254/16
  }
  FTPPassive : 1
  HTTPEnable : 1
  HTTPPort : 8080
  HTTPProxy : proxy.example
  HTTPSEnable : 1
  HTTPSPort : 8080
  HTTPSProxy : proxy.example
}`;
const SCUTIL_MANUAL_WITH_EXCEPTION = SCUTIL_MANUAL.replace("1 : 169.254/16", "1 : 169.254/16\n    2 : 127.0.0.1");
const SCUTIL_PAC = `<dictionary> {
  ExceptionsList : <array> {
    0 : *.local
  }
  ProxyAutoConfigEnable : 1
  ProxyAutoConfigURLString : http://proxy.example/proxy.pac
}`;
const SCUTIL_WPAD = "<dictionary> {\n  ProxyAutoDiscoveryEnable : 1\n}";
const SCUTIL_OFF = "<dictionary> {\n  ExceptionsList : <array> {\n    0 : *.local\n  }\n  FTPPassive : 1\n}";

const REG = (lines) =>
  ["", "HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings", ...lines.map((l) => `    ${l}`), ""].join(
    "\r\n",
  );
const REG_MANUAL = REG(["ProxyEnable    REG_DWORD    0x1", "ProxyServer    REG_SZ    proxy.example:8080", "ProxyOverride    REG_SZ    <local>;*.corp"]);
const REG_LOOPBACK = REG(["ProxyEnable    REG_DWORD    0x1", "ProxyOverride    REG_SZ    <-loopback>;*.corp"]);
const REG_PAC = REG(["ProxyEnable    REG_DWORD    0x0", "AutoConfigURL    REG_SZ    http://proxy.example/proxy.pac"]);
const REG_OFF = REG(["ProxyEnable    REG_DWORD    0x0", "ProxyOverride    REG_SZ    <local>"]);
// Byte 8 of the binary blob holds the flags: 0x09 = direct plus auto-detect, 0x01 = direct.
const CONNECTIONS = (flag) => REG([`DefaultConnectionSettings    REG_BINARY    4600000003000000${flag}000000`]);

test("parseScutil keeps the flat keys and the exceptions list apart", () => {
  const { settings, exceptions } = parseScutil(SCUTIL_MANUAL);
  assert.equal(settings.HTTPEnable, "1");
  assert.equal(settings.HTTPProxy, "proxy.example");
  assert.deepEqual(exceptions, ["*.local", "169.254/16"]);
  assert.deepEqual(parseScutil("").exceptions, []);
});

test("parseRegQuery reads name, type and value lines", () => {
  assert.deepEqual(parseRegQuery(REG_MANUAL), {
    ProxyEnable: "0x1",
    ProxyServer: "proxy.example:8080",
    ProxyOverride: "<local>;*.corp",
  });
});

test("an env proxy captures loopback unless NO_PROXY covers it", () => {
  assert.equal(loopbackProxyCapture({ env: {} }).captured, false);
  for (const name of ["HTTP_PROXY", "https_proxy", "ALL_PROXY"]) {
    const result = loopbackProxyCapture({ env: { [name]: "http://proxy.example:3128" } });
    assert.equal(result.captured, true, name);
    assert.match(result.reason, /without a loopback exception/);
  }
  const proxied = { HTTPS_PROXY: "http://proxy.example:3128" };
  assert.equal(loopbackProxyCapture({ env: { ...proxied, NO_PROXY: ".corp,127.0.0.1" } }).captured, false);
  assert.equal(loopbackProxyCapture({ env: { ...proxied, no_proxy: "localhost" } }).captured, false);
  assert.equal(loopbackProxyCapture({ env: { ...proxied, NO_PROXY: "*" } }).captured, false);
  assert.equal(loopbackProxyCapture({ env: { ...proxied, NO_PROXY: ".corp" } }).captured, true);
  assert.equal(loopbackProxyCapture({ env: { ...proxied, NO_PROXY: "127.0.0.1" }, platform: "darwin", scutil: SCUTIL_PAC }).captured, true);
});

test("macOS: a manual proxy without a loopback exception, PAC and auto-discovery capture; off or excepted don't", () => {
  const macos = (scutil) => loopbackProxyCapture({ env: {}, platform: "darwin", scutil });
  assert.equal(macos(SCUTIL_MANUAL).reason, "a macOS proxy without a loopback exception");
  assert.equal(macos(SCUTIL_MANUAL_WITH_EXCEPTION).captured, false);
  assert.equal(macos(SCUTIL_PAC).reason, "a macOS PAC proxy");
  assert.equal(macos(SCUTIL_WPAD).reason, "macOS proxy auto-discovery");
  assert.equal(macos(SCUTIL_OFF).captured, false);
  assert.equal(macos(null).captured, false);
  assert.equal(loopbackProxyCapture({ env: {}, platform: "linux", scutil: SCUTIL_PAC }).captured, false);
});

test("Windows: only <-loopback> or a PAC captures; a plain manual proxy and the default auto-detect leave loopback alone", () => {
  const windows = (reg, connections = null) => loopbackProxyCapture({ env: {}, platform: "win32", reg, connections });
  assert.equal(windows(REG_MANUAL).captured, false);
  assert.equal(windows(REG_LOOPBACK).reason, "a Windows proxy that includes loopback");
  assert.equal(windows(REG_PAC).reason, "a Windows PAC proxy");
  assert.equal(windows(REG_OFF, CONNECTIONS("09")).captured, false);
  assert.equal(windows(REG_OFF, CONNECTIONS("05")).reason, "a Windows PAC proxy");
  assert.equal(windows(REG_OFF, CONNECTIONS("01")).captured, false);
  assert.equal(windows(null).captured, false);
});

test("detectLoopbackProxy runs the platform's tool once, bounded, and a failure means no capture", async () => {
  const calls = [];
  const darwin = await detectLoopbackProxy({
    env: {},
    platform: "darwin",
    run: async (command, args) => (calls.push([command, ...args]), SCUTIL_MANUAL),
  });
  assert.deepEqual(darwin, { captured: true, reason: "a macOS proxy without a loopback exception" });
  assert.deepEqual(calls, [["scutil", "--proxy"]]);

  const broken = await detectLoopbackProxy({
    env: {},
    platform: "darwin",
    run: async () => {
      throw Object.assign(new Error("spawn scutil ENOENT"), { code: "ENOENT" });
    },
  });
  assert.deepEqual(broken, { captured: false, reason: null, error: "ENOENT" });

  const linux = await detectLoopbackProxy({ env: {}, platform: "linux", run: async () => assert.fail("no tool on Linux") });
  assert.deepEqual(linux, { captured: false, reason: null });

  const envFirst = await detectLoopbackProxy({
    env: { HTTP_PROXY: "http://proxy.example" },
    platform: "win32",
    run: async () => assert.fail("env decides first"),
  });
  assert.equal(envFirst.captured, true);

  const windows = await detectLoopbackProxy({
    env: {},
    platform: "win32",
    run: async (command, args) => (args.includes("/v") ? CONNECTIONS("01") : REG_LOOPBACK),
  });
  assert.equal(windows.reason, "a Windows proxy that includes loopback");
});

test("Windows asks the system resolver about the relay's own URL, and falls back to the registry when it fails", async () => {
  const reg = async (command, args) => (args.includes("/v") ? CONNECTIONS("05") : REG_PAC);
  const check = (resolve) => detectLoopbackProxy({ env: {}, platform: "win32", run: reg, port: 4242, resolve });

  // A PAC that leaves loopback alone: the registry alone would have skipped.
  const bypassed = await check(async (url) => (assert.equal(url, "http://127.0.0.1:4242/"), { proxy: null, bypassed: true }));
  assert.equal(bypassed.captured, false);
  assert.deepEqual(bypassed.decision, {
    registry: { captured: true, reason: "a Windows PAC proxy" },
    resolver: { proxy: null, bypassed: true },
  });

  const routed = await check(async () => ({ proxy: "http://proxy.corp:3128", bypassed: false }));
  assert.equal(routed.captured, true);
  assert.equal(routed.reason, "the Windows proxy settings route loopback through proxy.corp:3128");

  const failed = await check(async () => ({ proxy: null, bypassed: false, error: "ETIMEDOUT" }));
  assert.equal(failed.captured, true);
  assert.equal(failed.reason, "a Windows PAC proxy");
  assert.equal(failed.decision.resolver.error, "ETIMEDOUT");

  // An explicit <-loopback> stands: the resolver always bypasses loopback, whatever the settings say.
  const loopback = await detectLoopbackProxy({
    env: {},
    platform: "win32",
    run: async (command, args) => (args.includes("/v") ? CONNECTIONS("01") : REG_LOOPBACK),
    port: 4242,
    resolve: async () => assert.fail("not asked"),
  });
  assert.equal(loopback.reason, "a Windows proxy that includes loopback");

  // An unreadable registry still asks the resolver.
  const noRegistry = await detectLoopbackProxy({
    env: {},
    platform: "win32",
    run: async () => {
      throw Object.assign(new Error("spawn reg.exe ENOENT"), { code: "ENOENT" });
    },
    port: 4242,
    resolve: async () => ({ proxy: "http://proxy.corp:3128", bypassed: false }),
  });
  assert.equal(noRegistry.reason, "the Windows proxy settings route loopback through proxy.corp:3128");

  // Without a port there is nothing to ask about, so the registry decides.
  const noPort = await detectLoopbackProxy({ env: {}, platform: "win32", run: reg, resolve: async () => assert.fail("no port") });
  assert.equal(noPort.reason, "a Windows PAC proxy");
  assert.deepEqual(noPort.decision.resolver, null);
});
