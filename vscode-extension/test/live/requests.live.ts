// Sends the requests the extension builds to a real AI Gateway and checks the replies it gets back.
// Run with `npm run live`; the offline suite globs `test/*.test.ts` and never collects this.
//
// Needs UCODE_TEST_WORKSPACE and DATABRICKS_BEARER, as the Python e2e tests do; without them the
// suite skips rather than fails, so a fork's pull request stays green. The models and Model Provider
// Services come from the UG_LIVE_* variables below; a case whose variables are unset isn't
// registered, so the CI job decides what gets covered.
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { Tokens } from "../../src/auth";
import {
  anthropicMessagesReplies,
  anthropicMessagesRequest,
  chatCompletionsReplies,
  chatCompletionsRequest,
  responsesReplies,
  responsesRequest,
  type Reply,
  type Turn,
} from "../../src/formats";
import { PROVIDER_HEADER } from "../../src/extension";
import { streamEvents } from "../../src/gateway";
import { ASSISTANT, call, collect, message, rejectsWith, result, SYSTEM, text, USER } from "../support";
import { LanguageModelDataPart, LanguageModelTextPart, LanguageModelToolCallPart } from "../vscode";

const env = (name: string): string => process.env[name] ?? "";

// The same variables the Python e2e tests take, with UG_LIVE_* overrides for running one by hand.
const WORKSPACE = (env("UG_LIVE_WORKSPACE") || env("UCODE_TEST_WORKSPACE")).replace(/\/+$/, "");
const TOKEN_VAR = env("UG_LIVE_TOKEN") ? "UG_LIVE_TOKEN" : "DATABRICKS_BEARER";
const AUTH_COMMAND = env("UG_LIVE_AUTH_COMMAND") || (env(TOKEN_VAR) && `printf %s "$${TOKEN_VAR}"`);

const MLFLOW = "/ai-gateway/mlflow/v1";
const OPENAI = "/ai-gateway/openai/v1";
const MESSAGES = "/ai-gateway/anthropic/v1/messages";
const ANTHROPIC_VERSION = { "anthropic-version": "2023-06-01" };

const tokens = new Tokens(AUTH_COMMAND);

const send = (path: string, body: object, headers: Record<string, string> = {}) =>
  streamEvents(WORKSPACE, path, body, tokens, { ...headers }, new AbortController().signal);

const turnOf = (over: Partial<Turn> & { model: string }): Turn => ({
  messages: [],
  tools: [],
  toolRequired: false,
  ...over,
});

const textOf = (replies: Reply[]): string =>
  replies
    .filter((reply): reply is LanguageModelTextPart => reply instanceof LanguageModelTextPart)
    .map((part) => part.value)
    .join("");

const callsOf = (replies: Reply[]): LanguageModelToolCallPart[] =>
  replies.filter((reply): reply is LanguageModelToolCallPart => reply instanceof LanguageModelToolCallPart);

// A 16x16 red PNG, so a model asked about the image has something real to look at.
const RED_PNG = "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAFklEQVR4nGO4IyJCEmIY1TCqYfhqAAACcQQQFwFJQgAAAABJRU5ErkJggg==";
const redImage = () => new LanguageModelDataPart(Buffer.from(RED_PNG, "base64"), "image/png");

const viewImageTool = { name: "view_image", description: "Show the image at a path", inputSchema: { type: "object", properties: { path: { type: "string" } } } };

/** The model must echo this back, so a reply can't pass on stray text alone. */
const marker = () => `UG${Math.floor(Math.random() * 1e6)}`;

const echoTurn = (model: string, mark: string): Turn =>
  turnOf({
    model,
    messages: [message(SYSTEM, text("Answer in as few words as possible.")), message(USER, text(`Reply with exactly: ${mark}`))],
  });

const imageTurn = (model: string): Turn =>
  turnOf({
    model,
    tools: [viewImageTool],
    messages: [
      message(USER, text("Look at a.png with the tool, then name its main colour in one lowercase word.")),
      message(ASSISTANT, call("c1", "view_image", { path: "a.png" })),
      message(USER, result("c1", text("a.png:"), redImage())),
    ],
  });

const anthropic = (turn: Turn, maxTokens: number, headers: Record<string, string> = {}) =>
  collect(anthropicMessagesReplies(send(MESSAGES, anthropicMessagesRequest(turn, maxTokens), { ...ANTHROPIC_VERSION, ...headers })));

const responses = (turn: Turn, base = MLFLOW, headers: Record<string, string> = {}) =>
  collect(responsesReplies(send(`${base}/responses`, responsesRequest(turn), headers)));

const chat = (turn: Turn, base = MLFLOW, headers: Record<string, string> = {}) =>
  collect(chatCompletionsReplies(send(`${base}/chat/completions`, chatCompletionsRequest(turn), headers)));

// What to exercise, defaulted to the models and services the integration CUJs use (the same values
// scripts/run_integration.py defaults its --claude-provider and --codex-provider flags to). Override
// any of these for another workspace, or set one to "" to leave that case out.
const setting = (name: string, fallback: string): string => (name in process.env ? env(name) : fallback);

const CLAUDE = setting("UG_LIVE_CLAUDE_MODEL", "system.ai.claude-haiku-4-5");
const GPT = setting("UG_LIVE_GPT_MODEL", "system.ai.gpt-5-4-nano");
// No default: every OpenAI-type model on the CI workspace is GPT-5 or newer, which ug routes to
// Responses, so nothing there exercises Chat Completions.
const CHAT_GPT = env("UG_LIVE_CHAT_MODEL");
const MPS = setting("UG_LIVE_MPS", "main.ucode.ci_e2e_anthropic_nonrelay_mps");
const MPS_MODEL = setting("UG_LIVE_MPS_MODEL", "claude-haiku-4-5-20251001");
const OPENAI_MPS = setting("UG_LIVE_OPENAI_MPS", "main.ucode.ci_openai_mps");
const OPENAI_MPS_MODEL = setting("UG_LIVE_OPENAI_MPS_MODEL", "gpt-5-nano");

if (!WORKSPACE || !AUTH_COMMAND) {
  it.skip("set UCODE_TEST_WORKSPACE and DATABRICKS_BEARER to run the live tests", () => {});
}

describe("the workspace's own models", () => {
  if (CLAUDE && WORKSPACE && AUTH_COMMAND) {
    it("streams a Claude answer on Anthropic Messages, and reads the prompt cache on a repeat", async () => {
      const mark = marker();
      // Anthropic caches a prompt only above a minimum length, 2048 tokens for Haiku models, so this
      // has to clear that with room to spare; the cache point itself sits on the system prompt.
      const system = text(`Answer in as few words as possible. ${"Ignore this sentence. ".repeat(1500)}`);
      const turn = turnOf({ model: CLAUDE, messages: [message(SYSTEM, system), message(USER, text(`Reply with exactly: ${mark}`))] });

      assert.match(textOf(await anthropic(turn, 64)), new RegExp(mark));
      // The second turn is identical, so its prompt should be served from the cache.
      const events = await collect(send(MESSAGES, anthropicMessagesRequest(turn, 64), ANTHROPIC_VERSION));
      const start = events.find((event) => (event as { type?: string }).type === "message_start") as
        | { message?: { usage?: Record<string, number> } }
        | undefined;
      assert.ok((start?.message?.usage?.cache_read_input_tokens ?? 0) > 0, JSON.stringify(start?.message?.usage));
    });

    it("gets a Claude tool call, and an image inside its result reaches the model", async () => {
      const forced = turnOf({
        model: CLAUDE,
        tools: [viewImageTool],
        toolRequired: true,
        messages: [message(USER, text("Look at a.png."))],
      });
      const [only, ...rest] = callsOf(await anthropic(forced, 256));
      assert.equal(rest.length, 0);
      assert.equal(only?.name, "view_image");
      assert.equal(typeof only?.input, "object");

      assert.match(textOf(await anthropic(imageTurn(CLAUDE), 256)).toLowerCase(), /red/);
    });

    it("retries with the output limit the gateway allows instead of failing the turn", async () => {
      // No Claude model serves this many output tokens, so the first request is refused.
      assert.ok(textOf(await anthropic(echoTurn(CLAUDE, marker()), 999_999)).length > 0);
    });
  }

  if (GPT && WORKSPACE && AUTH_COMMAND) {
    it("streams a GPT answer and a tool call on Responses", async () => {
      const mark = marker();
      assert.match(textOf(await responses(echoTurn(GPT, mark))), new RegExp(mark));

      const forced = turnOf({ model: GPT, tools: [viewImageTool], toolRequired: true, messages: [message(USER, text("Look at a.png."))] });
      const [only] = callsOf(await responses(forced));
      assert.equal(only?.name, "view_image");
    });

    it("sends a tool result's image in the request Responses accepts", async () => {
      assert.match(textOf(await responses(imageTurn(GPT))).toLowerCase(), /red/);
    });
  }

  if (CHAT_GPT && WORKSPACE && AUTH_COMMAND) {
    it("streams an answer on Chat Completions", async () => {
      const mark = marker();
      assert.match(textOf(await chat(echoTurn(CHAT_GPT, mark))), new RegExp(mark));
    });
  }
});

describe("a Model Provider Service", () => {
  if (MPS && MPS_MODEL && WORKSPACE && AUTH_COMMAND) {
    const header = { [PROVIDER_HEADER]: MPS };

    it("serves its Claude models on Anthropic Messages, with tool calls", async () => {
      const mark = marker();
      assert.match(textOf(await anthropic(echoTurn(MPS_MODEL, mark), 256, header)), new RegExp(mark));

      const forced = turnOf({ model: MPS_MODEL, tools: [viewImageTool], toolRequired: true, messages: [message(USER, text("Look at a.png."))] });
      const [only] = callsOf(await anthropic(forced, 256, header));
      assert.equal(only?.name, "view_image");
    });

    it("needs the routing header, and isn't served on the mlflow route", async () => {
      const turn = echoTurn(MPS_MODEL, marker());
      // Without this the gateway could be answering from system.ai, and a pass would prove nothing.
      await rejectsWith(anthropic(turn, 64), "AI Gateway returned");
      await rejectsWith(chat(turn, MLFLOW, header), "AI Gateway returned");
    });
  }

  if (OPENAI_MPS && OPENAI_MPS_MODEL && WORKSPACE && AUTH_COMMAND) {
    const header = { [PROVIDER_HEADER]: OPENAI_MPS };

    it("serves its OpenAI models on the gateway's OpenAI route", async () => {
      const mark = marker();
      assert.match(textOf(await responses(echoTurn(OPENAI_MPS_MODEL, mark), OPENAI, header)), new RegExp(mark));
    });

    it("isn't served on the mlflow route", async () => {
      await rejectsWith(responses(echoTurn(OPENAI_MPS_MODEL, marker()), MLFLOW, header), "AI Gateway returned");
    });
  }
});
