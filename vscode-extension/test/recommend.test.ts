import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";
import { Tokens } from "../src/auth";
import { chooseDefault, COPILOT_AGENT, fetchRecommendation, matchModel, RECOMMEND_PATH, type Recommendation, SmartDefaults } from "../src/recommend";
import { rejectsWith, tokenCommand } from "./support";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

/** Replaces fetch with one that answers each request with the next status and body. */
const stubFetch = (...answers: [number, string][]) => {
  const requests: { url: string; method: string; headers: Headers; body: string }[] = [];
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    requests.push({ url, method: String(init.method), headers: new Headers(init.headers), body: String(init.body) });
    const [status, body] = answers[Math.min(requests.length - 1, answers.length - 1)] ?? [500, ""];
    return new Response(body, { status });
  }) as typeof fetch;
  return requests;
};

const SONNET = "system.ai.claude-sonnet-5-5";
const OPUS = "system.ai.claude-opus-5-5";
const GPT = "system.ai.gpt-6-luna";
const IDS = [SONNET, OPUS, GPT];

describe("fetchRecommendation", () => {
  it("POSTs an empty body with the token and headers, and keeps the agent and model", async () => {
    const requests = stubFetch([
      200,
      JSON.stringify({ recommended_agent: COPILOT_AGENT, recommended_model: SONNET, current_spend: "80.5", effective_threshold: "100" }),
    ]);
    const tokens = new Tokens(tokenCommand(["t1"]).command);

    const recommendation = await fetchRecommendation("https://example.cloud.databricks.com", tokens, { "User-Agent": "ucode/x", "X-Team": "ml" });

    assert.deepEqual(recommendation, { agent: COPILOT_AGENT, model: SONNET });
    assert.equal(requests[0]?.url, `https://example.cloud.databricks.com${RECOMMEND_PATH}`);
    assert.equal(requests[0]?.method, "POST");
    assert.equal(requests[0]?.body, "{}");
    assert.equal(requests[0]?.headers.get("authorization"), "Bearer t1");
    assert.equal(requests[0]?.headers.get("x-team"), "ml");
    assert.equal(requests[0]?.headers.get("user-agent"), "ucode/x");
  });

  it("drops fields that aren't strings, and gives an empty recommendation for an empty answer", async () => {
    stubFetch([200, JSON.stringify({ recommended_agent: 4, recommended_model: "" })], [200, "{}"]);
    const tokens = new Tokens(tokenCommand(["t1"]).command);

    assert.deepEqual(await fetchRecommendation("https://w", tokens, {}), {});
    assert.deepEqual(await fetchRecommendation("https://w", tokens, {}), {});
  });

  it("retries once with a new token after a 401", async () => {
    const requests = stubFetch([401, ""], [200, JSON.stringify({ recommended_model: GPT })]);
    const tokens = new Tokens(tokenCommand(["t1", "t2"], "t1").command);

    assert.deepEqual(await fetchRecommendation("https://w", tokens, {}), { model: GPT });
    assert.deepEqual(
      requests.map((request) => request.headers.get("authorization")),
      ["Bearer t1", "Bearer t2"],
    );
  });

  it("gives nothing for a 404, and reports other errors and a page that isn't JSON", async () => {
    const tokens = new Tokens(tokenCommand(["t1"]).command);
    stubFetch([404, "no config"]);
    assert.equal(await fetchRecommendation("https://w", tokens, {}), undefined);

    stubFetch([403, '{"error_code":"FEATURE_DISABLED"}']);
    await rejectsWith(fetchRecommendation("https://w", tokens, {}), "AI Gateway returned 403: {\"error_code\":\"FEATURE_DISABLED\"}");

    stubFetch([200, "<html>sign in</html>"]);
    await rejectsWith(fetchRecommendation("https://w", tokens, {}), "something other than JSON");
  });
});

describe("matchModel", () => {
  it("matches an id, then the id without case or a [1m] suffix, then the system.ai. and databricks- names", () => {
    assert.equal(matchModel(SONNET, IDS), SONNET);
    assert.equal(matchModel("System.AI.Claude-Opus-5-5[1m]", IDS), OPUS);
    assert.equal(matchModel("gpt-6-luna", IDS), GPT);
    assert.equal(matchModel("claude-opus-5-5", ["databricks-claude-opus-5-5"]), "databricks-claude-opus-5-5");
  });

  it("prefers the system.ai. model when several match, and gives nothing for a model the list lacks", () => {
    assert.equal(matchModel(`${OPUS}[1m]`, ["SYSTEM.AI.claude-opus-5-5", OPUS]), OPUS);
    assert.equal(matchModel("CLAUDE-OPUS-5-5", ["claude-opus-5-5", OPUS]), "claude-opus-5-5");
    assert.equal(matchModel("system.ai.gemini-4-argon", IDS), undefined);
  });
});

describe("chooseDefault", () => {
  it("starts new chats on the tier's model when the tier is for Copilot", () => {
    assert.equal(chooseDefault({ agent: COPILOT_AGENT, model: "claude-sonnet-5-5" }, IDS, OPUS), SONNET);
    assert.equal(chooseDefault({ model: GPT }, IDS, OPUS), GPT);
  });

  it("keeps the config's default for another agent's tier, a model Copilot lacks, or no recommendation", () => {
    const cases: (Recommendation | undefined)[] = [
      { agent: "CODING_AGENT_CLAUDE_CODE", model: SONNET },
      { agent: "CODING_AGENT_CODEX", model: GPT },
      { agent: COPILOT_AGENT, model: "system.ai.gemini-4-argon" },
      { agent: COPILOT_AGENT },
      {},
      undefined,
    ];
    for (const recommendation of cases) {
      assert.equal(chooseDefault(recommendation, IDS, OPUS), OPUS);
      assert.equal(chooseDefault(recommendation, IDS, undefined), undefined);
    }
  });
});

describe("SmartDefaults", () => {
  /** `initial` is what the startup sync already set; `answers` are the gateway's, one per check. */
  const harness = (configDefault: string | undefined, initial: string | undefined, ...answers: (Recommendation | undefined | Error)[]) => {
    const applied: (string | undefined)[] = [];
    const logs: string[] = [];
    let calls = 0;
    const smart = new SmartDefaults(
      async () => {
        const answer = answers[Math.min(calls++, answers.length - 1)];
        if (answer instanceof Error) {
          throw answer;
        }
        return answer;
      },
      IDS,
      configDefault,
      initial,
      async (model: string | undefined) => {
        applied.push(model);
      },
      (line: string) => logs.push(line),
    );
    const checks = async (count: number) => {
      for (let i = 0; i < count; i++) {
        await smart.check();
      }
    };
    return { checks, applied, logs };
  };
  const copilotTier = { agent: COPILOT_AGENT, model: SONNET };

  it("moves to the tier's model, then writes again only when the choice changes", async () => {
    const { checks, applied, logs } = harness(OPUS, OPUS, copilotTier, copilotTier, { agent: "CODING_AGENT_CLAUDE_CODE", model: SONNET }, undefined);

    await checks(4);

    // The tier, the same tier again, a tier for Claude Code (the default comes back), then no config.
    assert.deepEqual(applied, [SONNET, OPUS]);
    assert.deepEqual(logs, [`Your workspace's budget tier starts new chats on ${SONNET}.`, `New chats start on ${OPUS} again.`]);
  });

  it("writes nothing while the answer matches what the startup sync set", async () => {
    const atDefault = harness(OPUS, OPUS, { agent: COPILOT_AGENT, model: OPUS });
    await atDefault.checks(2);
    assert.deepEqual(atDefault.applied, []);
    assert.deepEqual(atDefault.logs, []);

    const atTier = harness(OPUS, SONNET, copilotTier);
    await atTier.checks(2);
    assert.deepEqual(atTier.applied, []);
  });

  it("keeps the value already set when a check fails, logging each distinct failure once", async () => {
    const offline = harness(OPUS, SONNET, new Error("network error"));
    await offline.checks(2);
    assert.deepEqual(offline.applied, []);
    assert.deepEqual(offline.logs, [`Could not check your workspace's budget tier (network error); new chats keep starting on ${SONNET}.`]);

    const later = harness(OPUS, OPUS, copilotTier, new Error("network error"), new Error("network error"), new Error("HTTP 503"));
    await later.checks(4);
    assert.deepEqual(later.applied, [SONNET]);
    assert.equal(later.logs.filter((line) => line.includes("network error")).length, 1);
    assert.equal(later.logs.filter((line) => line.includes("HTTP 503")).length, 1);
  });

  it("says once when a Copilot tier names a model VS Code doesn't have, and keeps the default", async () => {
    const gemini = { agent: COPILOT_AGENT, model: "system.ai.gemini-4-argon" };
    const { checks, applied, logs } = harness(OPUS, OPUS, gemini, gemini, { agent: "CODING_AGENT_CLAUDE_CODE", model: "system.ai.gemini-4-argon" });

    await checks(3);

    assert.deepEqual(applied, []);
    assert.equal(logs.length, 1);
    assert.ok(logs[0]?.includes("system.ai.gemini-4-argon, which isn't one of the Databricks models in VS Code"), logs[0]);
  });

  it("removes the tier's model once the tier clears when the config names no default", async () => {
    const { checks, applied } = harness(undefined, undefined, copilotTier, {});

    await checks(2);

    assert.deepEqual(applied, [SONNET, undefined]);
  });
});
