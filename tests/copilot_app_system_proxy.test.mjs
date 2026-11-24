import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createSystemProxyResolver,
  macosSystemProxy,
  originOf,
  parseSystemProxyReply,
  windowsProxyScript,
} from "../src/ucode/agents/desktop/copilot_app_plugin/app-system-proxy.mjs";

const SCUTIL_MANUAL = `<dictionary> {
  ExceptionsList : <array> {
    0 : *.local
    1 : 169.254/16
    2 : *.corp.example
  }
  HTTPEnable : 1
  HTTPPort : 8080
  HTTPProxy : proxy.example
  HTTPSEnable : 1
  HTTPSPort : 8443
  HTTPSProxy : secure.example
}`;
const SCUTIL_PAC = "<dictionary> {\n  ProxyAutoConfigEnable : 1\n  ProxyAutoConfigURLString : http://proxy.example/proxy.pac\n}";

test("originOf keeps the scheme, host and port of an HTTP URL and nothing else", () => {
  assert.equal(originOf("https://ws.example:8443/ai-gateway/v1?x=1"), "https://ws.example:8443/");
  assert.equal(originOf("http://127.0.0.1:4242/secret/meta"), "http://127.0.0.1:4242/");
  assert.equal(originOf("ftp://x"), null);
  assert.equal(originOf("not a url"), null);
});

test("windowsProxyScript asks .NET for the proxy and the bypass of one origin", () => {
  const script = windowsProxyScript("https://ws.example/");
  assert.match(script, /GetSystemWebProxy\(\)/);
  assert.match(script, /GetProxy\(\$u\)\.AbsoluteUri/);
  assert.match(script, /IsBypassed\(\$u\)/);
  assert.match(script, /'https:\/\/ws\.example\/'/);
  assert.throws(() => windowsProxyScript("https://ws.example/';rm"), /unsupported URL/);
});

test("parseSystemProxyReply tells a proxy from a direct or bypassed answer", () => {
  const origin = "https://ws.example/";
  assert.deepEqual(parseSystemProxyReply('{"Proxy":"http://proxy.corp:8080/","Bypassed":false}', origin), {
    proxy: "http://proxy.corp:8080",
    bypassed: false,
  });
  // GetProxy hands back the request URL itself when it goes direct.
  assert.deepEqual(parseSystemProxyReply('{"Proxy":"https://ws.example/","Bypassed":false}', origin), { proxy: null, bypassed: false });
  assert.deepEqual(parseSystemProxyReply('{"Proxy":"http://proxy.corp:8080/","Bypassed":true}', origin), { proxy: null, bypassed: true });
  assert.deepEqual(parseSystemProxyReply('{"Proxy":null,"Bypassed":false}\r\n', origin), { proxy: null, bypassed: false });
});

test("macosSystemProxy reads the manual proxy per scheme, honours the exceptions, and leaves PAC unresolved", () => {
  assert.deepEqual(macosSystemProxy(SCUTIL_MANUAL, "https://ws.example/"), { proxy: "http://secure.example:8443", bypassed: false });
  assert.deepEqual(macosSystemProxy(SCUTIL_MANUAL, "http://ws.example/"), { proxy: "http://proxy.example:8080", bypassed: false });
  assert.deepEqual(macosSystemProxy(SCUTIL_MANUAL, "https://api.corp.example/"), { proxy: null, bypassed: true });
  assert.deepEqual(macosSystemProxy(SCUTIL_MANUAL, "http://127.0.0.1:4242/"), { proxy: null, bypassed: true });
  assert.deepEqual(macosSystemProxy(SCUTIL_PAC, "https://ws.example/"), { proxy: null, bypassed: false, unresolved: "pac" });
  assert.deepEqual(macosSystemProxy("<dictionary> {\n}", "https://ws.example/"), { proxy: null, bypassed: false });
  const httpOnly = SCUTIL_MANUAL.replace("HTTPSEnable : 1", "HTTPSEnable : 0");
  assert.deepEqual(macosSystemProxy(httpOnly, "https://ws.example/"), { proxy: null, bypassed: false });
  assert.deepEqual(macosSystemProxy(httpOnly, "http://ws.example/"), { proxy: "http://proxy.example:8080", bypassed: false });
});

test("the resolver runs the platform's tool once per origin and turns a failure into an answer", async () => {
  const calls = [];
  const windows = createSystemProxyResolver({
    platform: "win32",
    run: async (command, args) => {
      calls.push([command, args.at(-1)]);
      return args.at(-1).includes("127.0.0.1")
        ? '{"Proxy":"http://127.0.0.1:4242/","Bypassed":true}'
        : '{"Proxy":"http://proxy.corp:3128/","Bypassed":false}';
    },
  });
  assert.deepEqual(await windows("https://ws.example/ai-gateway/v1/models"), { proxy: "http://proxy.corp:3128", bypassed: false });
  assert.deepEqual(await windows("https://ws.example/other"), { proxy: "http://proxy.corp:3128", bypassed: false });
  assert.deepEqual(await windows("http://127.0.0.1:4242/secret/meta"), { proxy: null, bypassed: true });
  assert.equal(calls.length, 2);
  assert.ok(calls.every(([command]) => /powershell(\.exe)?$/.test(command)));

  const broken = createSystemProxyResolver({
    platform: "win32",
    run: async () => {
      throw Object.assign(new Error("timed out"), { code: "ETIMEDOUT" });
    },
  });
  assert.deepEqual(await broken("https://ws.example/"), { proxy: null, bypassed: false, error: "ETIMEDOUT" });

  let clock = 0;
  let fail = true;
  const flaky = createSystemProxyResolver({
    platform: "win32",
    now: () => clock,
    retryMs: 1000,
    run: async () => {
      if (fail) throw Object.assign(new Error("timed out"), { code: "ETIMEDOUT" });
      return '{"Proxy":"http://proxy.corp:3128/","Bypassed":false}';
    },
  });
  assert.equal((await flaky("https://ws.example/")).error, "ETIMEDOUT");
  fail = false;
  clock = 999;
  assert.equal((await flaky("https://ws.example/")).error, "ETIMEDOUT");
  clock = 1000;
  assert.deepEqual(await flaky("https://ws.example/"), { proxy: "http://proxy.corp:3128", bypassed: false });
  clock = 10_000;
  fail = true;
  assert.deepEqual(await flaky("https://ws.example/"), { proxy: "http://proxy.corp:3128", bypassed: false });

  const garbage = createSystemProxyResolver({ platform: "win32", run: async () => "not json" });
  assert.match((await garbage("https://ws.example/")).error, /JSON/);

  const linux = createSystemProxyResolver({ platform: "linux", run: async () => assert.fail("nothing to ask on Linux") });
  assert.deepEqual(await linux("https://ws.example/"), { proxy: null, bypassed: false });
  assert.deepEqual(await linux("mailto:x"), { proxy: null, bypassed: false, error: "unsupported URL" });

  const darwin = createSystemProxyResolver({ platform: "darwin", run: async () => SCUTIL_MANUAL });
  assert.deepEqual(await darwin("https://ws.example/"), { proxy: "http://secure.example:8443", bypassed: false });
});
