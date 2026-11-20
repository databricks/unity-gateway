import assert from "node:assert/strict";
import { delimiter } from "node:path";
import { test } from "node:test";

import {
  DEFAULT_TOKEN_TTL_MS,
  FORCE_REFRESH_FLAG,
  REFRESH_MARGIN_MS,
  buildProviders,
  childEnv,
  createTokenSource,
  isAuthRejection,
  jwtExpiryMs,
  withLoginHint,
} from "../src/ucode/copilot_app_plugin/ug-plugin.mjs";

const config = {
  providers: [{ name: "p1", type: "anthropic", baseUrl: "https://ws/ai-gateway/anthropic", headers: { A: "1" } }],
  tokenCommand: ["/opt/ug/bin/ug", "auth-token", "--host", "https://ws"],
  pathDirs: ["/opt/ug/bin", "/opt/databricks"],
};

const jwt = (exp) => `h.${Buffer.from(JSON.stringify({ exp })).toString("base64url")}.s`;

test("jwtExpiryMs reads exp and tolerates opaque tokens", () => {
  assert.equal(jwtExpiryMs(jwt(1000)), 1_000_000);
  assert.equal(jwtExpiryMs("dapi-opaque"), null);
  assert.equal(jwtExpiryMs("a.not-base64-json.b"), null);
});

test("childEnv puts the Databricks CLI's dir on PATH", () => {
  const env = childEnv({ ...config, databricksPath: "/opt/homebrew/bin/databricks" }, { PATH: "/usr/bin" });
  assert.equal(env.PATH, ["/opt/ug/bin", "/opt/databricks", "/opt/homebrew/bin", "/usr/bin"].join(delimiter));
});

test("childEnv puts the configured dirs ahead of PATH", () => {
  const env = childEnv(config, { PATH: "/usr/bin", HOME: "/h" });
  assert.equal(env.PATH, ["/opt/ug/bin", "/opt/databricks", "/usr/bin"].join(delimiter));
  assert.equal(env.HOME, "/h");
});

test("createTokenSource caches until shortly before expiry", async () => {
  let clock = 0;
  const calls = [];
  const run = async (argv, env) => {
    calls.push({ argv, path: env.PATH });
    return jwt(3600);
  };
  const token = createTokenSource(config, { run, now: () => clock });
  assert.equal(await token(), jwt(3600));
  clock = 3600 * 1000 - REFRESH_MARGIN_MS - 1;
  await token();
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].argv, config.tokenCommand);
  assert.ok(calls[0].path.startsWith("/opt/ug/bin"));
  clock = 3600 * 1000 - REFRESH_MARGIN_MS;
  await token();
  assert.equal(calls.length, 2);
});

test("createTokenSource gives opaque tokens a default lifetime", async () => {
  let clock = 0;
  let calls = 0;
  const token = createTokenSource(config, { run: async () => (calls++, "opaque"), now: () => clock });
  await token();
  clock = DEFAULT_TOKEN_TTL_MS - 1;
  await token();
  assert.equal(calls, 1);
  clock = DEFAULT_TOKEN_TTL_MS;
  await token();
  assert.equal(calls, 2);
});

test("concurrent requests share one mint, and a failure is not cached", async () => {
  let calls = 0;
  let fail = true;
  const run = () =>
    new Promise((resolve, reject) => {
      calls++;
      setImmediate(() => (fail ? reject(new Error("no login")) : resolve("t")));
    });
  const token = createTokenSource(config, { run });
  const results = await Promise.allSettled([token(), token()]);
  assert.equal(calls, 1);
  assert.ok(results.every((r) => r.status === "rejected"));
  fail = false;
  assert.equal(await token(), "t");
  assert.equal(calls, 2);
});

test("withLoginHint shows the hint once per run of failures", async () => {
  let fail = true;
  let hints = 0;
  const bearer = withLoginHint(
    async () => {
      if (fail) throw new Error("no login");
      return "t";
    },
    () => hints++,
  );
  await assert.rejects(bearer(), /no login/);
  await assert.rejects(bearer(), /no login/);
  assert.equal(hints, 1);
  fail = false;
  assert.equal(await bearer(), "t");
  fail = true;
  await assert.rejects(bearer(), /no login/);
  assert.equal(hints, 2);
});

test("withLoginHint still throws the token error when the hint fails", async () => {
  const bearer = withLoginHint(
    async () => {
      throw new Error("no login");
    },
    () => {
      throw new Error("log failed");
    },
  );
  await assert.rejects(bearer(), /no login/);
});

test("buildProviders attaches the token provider to every provider", () => {
  const provide = async () => "t";
  const [provider] = buildProviders(config, provide);
  assert.equal(provider.bearerTokenProvider, provide);
  assert.equal(provider.baseUrl, "https://ws/ai-gateway/anthropic");
  assert.equal(config.providers[0].bearerTokenProvider, undefined);
});

test("invalidate drops the cached token and force-refreshes the next mint once", async () => {
  const calls = [];
  const token = createTokenSource(config, {
    run: async (argv) => (calls.push(argv), jwt(3600)),
    now: () => 0,
  });
  await token();
  await token();
  token.invalidate();
  await token();
  await token();
  token.invalidate();
  await token();
  assert.deepEqual(calls, [
    config.tokenCommand,
    [...config.tokenCommand, FORCE_REFRESH_FLAG],
    [...config.tokenCommand, FORCE_REFRESH_FLAG],
  ]);
});

test("isAuthRejection matches only 401 errors and call failures", () => {
  assert.equal(isAuthRejection({ type: "session.error", data: { statusCode: 401 } }), true);
  assert.equal(isAuthRejection({ type: "model.call_failure", data: { statusCode: 401 } }), true);
  assert.equal(isAuthRejection({ type: "model.call_failure", data: { statusCode: 429 } }), false);
  assert.equal(isAuthRejection({ type: "session.error", data: {} }), false);
  assert.equal(isAuthRejection({ type: "assistant.usage", data: { statusCode: 401 } }), false);
});
