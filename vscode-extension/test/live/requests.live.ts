// Sends the requests the extension builds to a real AI Gateway and checks the replies it gets back.
// Run with `npm run live`; the offline suite globs `test/*.test.ts` and never collects this.
//
// Needs UG_LIVE_WORKSPACE and either UG_LIVE_AUTH_COMMAND or UG_LIVE_TOKEN. The models and Model
// Provider Services come from the UG_LIVE_* variables below; a case whose variables are unset isn't
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

const WORKSPACE = env("UG_LIVE_WORKSPACE").replace(/\/+$/, "");
const AUTH_COMMAND = env("UG_LIVE_AUTH_COMMAND") || (env("UG_LIVE_TOKEN") && `printf %s "$UG_LIVE_TOKEN"`);
if (!WORKSPACE || !AUTH_COMMAND) {
  throw new Error("Set UG_LIVE_WORKSPACE and UG_LIVE_AUTH_COMMAND (or UG_LIVE_TOKEN) to run the live tests.");
}

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

const CLAUDE = env("UG_LIVE_CLAUDE_MODEL");
const GPT = env("UG_LIVE_GPT_MODEL");
// Unset in CI: every OpenAI-type model there is GPT-5 or newer, which ug routes to Responses.
const CHAT_GPT = env("UG_LIVE_CHAT_MODEL");
const MPS = env("UG_LIVE_MPS");
const MPS_MODEL = env("UG_LIVE_MPS_MODEL");
const OPENAI_MPS = env("UG_LIVE_OPENAI_MPS");
const OPENAI_MPS_MODEL = env("UG_LIVE_OPENAI_MPS_MODEL");

describe("the workspace's own models", () => {
  if (CLAUDE) {
    it("streams a Claude answer on Anthropic Messages, and reads the prompt cache on a repeat", async () => {
      const mark = marker();
      // Long enough that the gateway caches it; the cache point is on the system prompt.
      const system = text(`Answer in as few words as possible. ${"Ignore this sentence. ".repeat(400)}`);
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

  if (GPT) {
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

  if (CHAT_GPT) {
    it("streams an answer on Chat Completions", async () => {
      const mark = marker();
      assert.match(textOf(await chat(echoTurn(CHAT_GPT, mark))), new RegExp(mark));
    });
  }
});

describe("a Model Provider Service", () => {
  if (MPS && MPS_MODEL) {
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

  if (OPENAI_MPS && OPENAI_MPS_MODEL) {
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
