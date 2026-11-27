import assert from "node:assert/strict";
import { delimiter } from "node:path";
import { test } from "node:test";

import {
  DEFAULT_TOKEN_TTL_MS,
  FORCE_REFRESH_FLAG,
  REFRESH_MARGIN_MS,
  SESSION_ID_HEADER,
  buildProviders,
  childEnv,
  createTokenSource,
  hintFor,
  isAuthRejection,
  jwtExpiryMs,
  MIN_TOKEN_REUSE_MS,
  runCommand,
  withLoginHint,
  withSessionId,
} from "../src/ucode/agents/desktop/copilot_app_plugin/ug-plugin.mjs";

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

test("childEnv keeps Windows' Path casing", () => {
  const env = childEnv(config, { Path: "C:\\bin", HOME: "/h" });
  assert.deepEqual(Object.keys(env).filter((key) => key.toUpperCase() === "PATH"), ["Path"]);
  assert.equal(env.Path, ["/opt/ug/bin", "/opt/databricks", "C:\\bin"].join(delimiter));
});

test("a token close to its expiry is still reused briefly", async () => {
  let clock = 3600 * 1000 - 60_000;
  let calls = 0;
  const token = createTokenSource(config, { run: async () => (calls++, jwt(3600)), now: () => clock });
  await token();
  clock += MIN_TOKEN_REUSE_MS - 1;
  await token();
  assert.equal(calls, 1);
  clock += 1;
  await token();
  assert.equal(calls, 2);
});

test("runCommand returns the token, and fails on a nonzero exit, no output or no program", async () => {
  const script = (code) => [process.execPath, "-e", code];
  assert.equal(await runCommand(script("console.log(' tok ')"), process.env), "tok");
  await assert.rejects(runCommand(script("console.error('first\\nlast'); process.exit(3)"), process.env), (error) => {
    assert.equal(error.code, 3);
    assert.match(error.message, /last/);
    return true;
  });
  await assert.rejects(runCommand(script(""), process.env), /printed no token/);
  await assert.rejects(runCommand(["/nonexistent/ug"], process.env), (error) => error.code === "ENOENT");
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
    () => (hints++, true),
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

test("withLoginHint tries again until a hint is shown", async () => {
  const shown = [false, true];
  let calls = 0;
  const bearer = withLoginHint(
    async () => {
      throw new Error("no login");
    },
    () => shown[calls++],
  );
  await assert.rejects(bearer());
  await assert.rejects(bearer());
  await assert.rejects(bearer());
  assert.equal(calls, 2);
});

test("hintFor picks the hint by how ug auth-token failed", () => {
  const hints = { loginHint: "login", missingUgHint: "missing" };
  assert.equal(hintFor(Object.assign(new Error(), { code: 1 }), hints), "login");
  assert.equal(hintFor(Object.assign(new Error(), { code: "ENOENT" }), hints), "missing");
  assert.equal(hintFor(Object.assign(new Error(), { code: "TIMEOUT" }), hints), null);
  assert.equal(hintFor(new Error("printed no token"), hints), null);
});

test("an invalidate during a mint keeps its token out of the cache", async () => {
  const calls = [];
  let release;
  const token = createTokenSource(config, {
    run: (argv) => {
      calls.push(argv);
      if (calls.length === 1) return new Promise((resolve) => (release = resolve));
      return Promise.resolve(jwt(3600));
    },
    now: () => 0,
  });
  const first = token();
  token.invalidate();
  release(jwt(7200));
  assert.equal(await first, jwt(7200));
  assert.equal(await token(), jwt(3600));
  assert.equal(await token(), jwt(3600));
  assert.deepEqual(calls, [config.tokenCommand, [...config.tokenCommand, FORCE_REFRESH_FLAG]]);
});

test("buildProviders attaches the token provider to every provider", () => {
  const provide = async () => "t";
  const [provider] = buildProviders(config, provide, "app-session");
  assert.equal(provider.bearerTokenProvider, provide);
  assert.equal(provider.baseUrl, "https://ws/ai-gateway/anthropic");
  assert.equal(config.providers[0].bearerTokenProvider, undefined);
});

test("invalidate with a token another request already replaced keeps the fresh one", async () => {
  let minted = 0;
  const token = createTokenSource(config, { run: async () => jwt(3600 + ++minted), now: () => 0 });
  const first = await token();
  assert.equal(token.invalidate(first), true);
  const second = await token();
  assert.notEqual(second, first);
  // A second request that was refused with the old token must not throw the new one away.
  assert.equal(token.invalidate(first), false);
  assert.equal(await token(), second);
  assert.equal(minted, 2);
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

test("buildProviders sends the app session's id on every provider's requests", () => {
  const providers = buildProviders(
    { providers: [config.providers[0], { name: "p2", type: "openai", baseUrl: "https://ws/ai-gateway/openai" }] },
    async () => "t",
    "app-session",
  );
  assert.deepEqual(
    providers.map((provider) => provider.headers),
    [{ [SESSION_ID_HEADER]: "app-session", A: "1" }, { [SESSION_ID_HEADER]: "app-session" }],
  );
  assert.deepEqual(config.providers[0].headers, { A: "1" });
});

test("buildProviders takes the session id the runtime gives the extension", (t) => {
  const previous = process.env.SESSION_ID;
  process.env.SESSION_ID = "from-runtime";
  t.after(() => {
    if (previous === undefined) delete process.env.SESSION_ID;
    else process.env.SESSION_ID = previous;
  });
  assert.equal(buildProviders(config, async () => "t")[0].headers[SESSION_ID_HEADER], "from-runtime");
});

test("an admin's session-id header wins, in any case", () => {
  assert.deepEqual(withSessionId({ "Session-Id": "admin" }, "app-session"), { "Session-Id": "admin" });
  assert.deepEqual(withSessionId({ "session-id": "admin", B: "2" }, "app-session"), { "session-id": "admin", B: "2" });
});

test("without a session id the headers stay as configured", () => {
  assert.deepEqual(withSessionId({ A: "1" }, undefined), { A: "1" });
  assert.deepEqual(withSessionId(undefined, ""), {});
});
