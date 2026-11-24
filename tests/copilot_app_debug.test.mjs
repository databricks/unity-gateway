import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, existsSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  DEBUG_LOG_FILE,
  NO_DEBUG,
  OMITTED,
  REDACTED,
  createDebugLog,
  redact,
  redactHeaders,
} from "../src/ucode/agents/desktop/copilot_app_plugin/app-debug.mjs";

test("redact strips credentials from strings, nested values and errors", () => {
  assert.equal(redact("Bearer gho_abcDEF123 and gho_x"), `${REDACTED} and ${REDACTED}`);
  assert.equal(redact("http://user:pw@proxy.example:8080/x"), `http://${REDACTED}@proxy.example:8080/x`);
  assert.equal(redact("token dapiABCDEF0123456789"), `token ${REDACTED}`);
  assert.equal(redact("jwt eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.sig_part"), `jwt ${REDACTED}`);
  assert.equal(redact("auth tid=abc123;exp=1"), `auth ${REDACTED}`);
  assert.equal(redact('"tid=a1;exp=2;sku=s;proxy-ep=p.example;8kp=1:0f9e" next'), `"${REDACTED}" next`);
  assert.equal(redact("/s3cr3t-segment/meta", ["s3cr3t-segment"]), `/${REDACTED}/meta`);
  assert.deepEqual(redact({ a: ["ghu_1", { b: "github_pat_ABC" }], n: 3, ok: true }), {
    a: [REDACTED, { b: REDACTED }],
    n: 3,
    ok: true,
  });
  assert.deepEqual(redact({ env: { COPILOT_API_KEY: "sk-1", GH_TOKEN: "t", COPILOT_HOME: "/h" }, tokenLength: 4, credential: "oauth" }), {
    env: { COPILOT_API_KEY: REDACTED, GH_TOKEN: REDACTED, COPILOT_HOME: "/h" },
    tokenLength: 4,
    credential: "oauth",
  });
  const error = Object.assign(new Error("mint failed: Bearer dapi123"), { code: 1 });
  const out = redact(error);
  assert.equal(out.message, `mint failed: ${REDACTED}`);
  assert.equal(out.code, 1);
  assert.ok(typeof out.stack === "string");
});

test("redactHeaders keeps names, allowlisted values, and never a credential", () => {
  const headers = redactHeaders({
    Authorization: "Bearer gho_x",
    "x-databricks-ai-gateway-token": "Bearer dapi1",
    Cookie: "a=b",
    "Content-Type": "application/json",
    "User-Agent": "copilot/1.0",
    "x-databricks-github-copilot-api-host": "api.githubcopilot.com",
    "x-databricks-session-secret": "s",
    "x-custom": "value",
  });
  assert.deepEqual(headers, {
    authorization: REDACTED,
    "x-databricks-ai-gateway-token": REDACTED,
    cookie: REDACTED,
    "content-type": "application/json",
    "user-agent": "copilot/1.0",
    "x-databricks-github-copilot-api-host": "api.githubcopilot.com",
    "x-databricks-session-secret": OMITTED,
    "x-custom": OMITTED,
  });
  assert.deepEqual(NO_DEBUG.headers({ authorization: "Bearer gho_x" }), {});
});

test("createDebugLog appends one redacted JSON line per event and rotates at the cap", () => {
  const dir = mkdtempSync(join(tmpdir(), "ug-debug-"));
  let clock = 1_700_000_000_000;
  const debug = createDebugLog(dir, { now: () => (clock += 1000), pid: 42, sessionId: "s1", limit: 200 });
  debug.addSecret("relay-secret-value");
  debug("start", { url: "http://127.0.0.1:1/relay-secret-value/meta", token: "gho_abc", n: 1 });
  debug("relay.request", { headers: debug.headers({ authorization: "Bearer gho_abc", "content-type": "a" }) });

  const path = join(dir, DEBUG_LOG_FILE);
  const lines = readFileSync(path, "utf8").trim().split("\n").map((line) => JSON.parse(line));
  assert.deepEqual(lines[0], {
    t: "2023-11-14T22:13:21.000Z",
    sid: "s1",
    pid: 42,
    event: "start",
    url: `http://127.0.0.1:1/${REDACTED}/meta`,
    token: REDACTED,
    n: 1,
  });
  assert.deepEqual(lines[1].headers, { authorization: REDACTED, "content-type": "a" });
  assert.ok(!readFileSync(path, "utf8").includes("gho_"));

  // Over the cap: the next write rotates the file first.
  debug("big", { pad: "x".repeat(300) });
  assert.ok(existsSync(`${path}.1`));
  assert.equal(JSON.parse(readFileSync(path, "utf8").trim()).event, "big");
  assert.ok(statSync(`${path}.1`).size > 0);

  // Nothing throws when the directory is gone.
  const orphan = createDebugLog(join(dir, "missing"), {});
  assert.doesNotThrow(() => orphan("start", {}));
  assert.ok(!existsSync(join(dir, "missing")));
});
