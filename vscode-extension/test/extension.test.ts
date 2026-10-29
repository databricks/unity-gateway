import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { afterEach, describe, it } from "node:test";
import type * as vscode from "vscode";
import packageJson from "../package.json";
import { Tokens } from "../src/auth";
import { DatabricksModels, PROVIDER_HEADER, readConfig, userAgent, type Settings } from "../src/extension";
import { streamEvents } from "../src/gateway";
import { collect, message, rejectsWith, text, tokenCommand, USER } from "./support";
import { LanguageModelChatToolMode, LanguageModelTextPart } from "./vscode";

const sse = (...data: string[]) => data.map((item) => `data: ${item}\n\n`).join("");

/** Replaces fetch with one that answers each request with the next status and SSE body. */
const stubFetch = (...answers: [number, string][]) => {
  const requests: { url: string; authorization: string; userAgent: string; provider: string | null; body: Record<string, unknown> }[] = [];
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    const headers = new Headers(init.headers);
    requests.push({
      url,
      authorization: headers.get("authorization") ?? "",
      userAgent: headers.get("user-agent") ?? "",
      provider: headers.get(PROVIDER_HEADER),
      body: JSON.parse(String(init.body)),
    });
    const [status, body] = answers[Math.min(requests.length - 1, answers.length - 1)] ?? [500, ""];
    // Split the body mid-line so the reader has to join chunks.
    const bytes = new TextEncoder().encode(body);
    const middle = Math.floor(bytes.length / 2);
    return new Response(new Blob([bytes.slice(0, middle), bytes.slice(middle)]).stream(), { status });
  }) as typeof fetch;
  return requests;
};

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

describe("streamEvents", () => {
  it("yields each event's JSON until [DONE]", async () => {
    stubFetch([200, sse('{"a":1}', '{"b":2}', "[DONE]", '{"c":3}')]);
    const tokens = new Tokens(tokenCommand(["t"]).command);
    assert.deepEqual(await collect(streamEvents("https://ws", "/r", {}, tokens, {}, new AbortController().signal)), [{ a: 1 }, { b: 2 }]);
  });

  it("keeps a last event that has no trailing newline", async () => {
    stubFetch([200, `data: {"a":1}\n\ndata: {"b":2}`]);
    const tokens = new Tokens(tokenCommand(["t"]).command);
    assert.deepEqual(await collect(streamEvents("https://ws", "/r", {}, tokens, {}, new AbortController().signal)), [{ a: 1 }, { b: 2 }]);
  });

  it("retries once with a new token after a 401, then asks the user to sign in", async () => {
    let requests = stubFetch([401, ""], [200, sse("[DONE]")]);
    await collect(streamEvents("https://ws", "/ai-gateway/mlflow/v1/chat/completions", {}, new Tokens(tokenCommand(["fresh"], "rejected").command), {}, new AbortController().signal));
    assert.deepEqual(requests.map((request) => request.authorization), ["Bearer rejected", "Bearer fresh"]);
    assert.equal(requests[0]?.url, "https://ws/ai-gateway/mlflow/v1/chat/completions");

    requests = stubFetch([401, ""]);
    await rejectsWith(
      collect(streamEvents("https://ws", "/r", {}, new Tokens(tokenCommand(["a", "b"]).command), {}, new AbortController().signal)),
      "Run `ug configure --agents vscode` to sign in again",
    );
    assert.equal(requests.length, 2);
  });

  it("reports other gateway errors with their body", async () => {
    stubFetch([400, '{"message":"bad tools"}']);
    await rejectsWith(
      collect(streamEvents("https://ws", "/r", {}, new Tokens(tokenCommand(["t"]).command), {}, new AbortController().signal)),
      'AI Gateway returned 400: {"message":"bad tools"}',
    );
  });
});

describe("provider", () => {
  // The icon lives only in the folder ug ships, next to the built bundle.
  it("ships the icon it names", () => {
    assert.ok(packageJson.contributes.languageModelChatProviders.every((provider) => existsSync(`../src/ucode/vscode/extension/${provider.icon}`)));
  });
});

describe("userAgent", () => {
  it("names ug's extension and Copilot Chat's version", () => {
    assert.equal(userAgent("0.71.0"), `ucode/${packageJson.version} githubcopilotchat/0.71.0`);
    assert.equal(userAgent(undefined), `ucode/${packageJson.version} githubcopilotchat`);
  });
});

describe("readConfig", () => {
  it("keeps well-formed models, names, families and token limits, the provider, and trims the workspace URL", () => {
    const config = {
      workspace: "https://ws/",
      authCommand: "ug auth-token",
      provider: "main.default.mps",
      models: [
        { id: "a", api: "responses" },
        { id: "gpt-5.5", family: "gpt-5.5", api: "responses" },
        { id: "e", api: "anthropic_messages" },
        { id: "b", api: "messages" },
        { api: "responses" },
        null,
        { id: "c", name: " Model C ", api: "chat_completions", maxInputTokens: 936_000, maxOutputTokens: "32000", extra: true },
        { id: "d", name: 7, api: "chat_completions", maxInputTokens: 0, maxOutputTokens: 1.5 },
      ],
    };
    assert.deepEqual(readConfig(JSON.stringify(config)), {
      workspace: "https://ws",
      authCommand: "ug auth-token",
      provider: "main.default.mps",
      models: [
        { id: "a", api: "responses" },
        { id: "gpt-5.5", family: "gpt-5.5", api: "responses" },
        { id: "e", api: "anthropic_messages" },
        { id: "c", name: "Model C", api: "chat_completions", maxInputTokens: 936_000 },
        { id: "d", api: "chat_completions" },
      ],
    });
  });

  it("gives no models for a missing or malformed file", () => {
    const none = { workspace: "", authCommand: "", models: [], provider: "" };
    for (const text of [undefined, "", "{", "[]", "null", '"text"']) {
      assert.deepEqual(readConfig(text), none, String(text));
    }
  });
});

describe("DatabricksModels", () => {
  const settings = (command: string, provider = ""): Settings => ({
    workspace: "https://ws",
    authCommand: command,
    provider,
    models: [
      { id: "system.ai.claude-sonnet-5-5", name: "Claude Sonnet 5.5", api: "anthropic_messages", maxInputTokens: 872_000, maxOutputTokens: 128_000 },
      { id: "system.ai.gpt-6-luna", api: "responses" },
      { id: "gpt-4.1", family: "gpt-4.1", api: "chat_completions" },
    ],
  });

  it("lists the configured models by name, or by id without one, or none with a hint when settings are missing", async () => {
    const logs: string[] = [];
    const models = await new DatabricksModels(() => settings("cmd"), (line) => logs.push(line)).provideLanguageModelChatInformation();
    assert.deepEqual(
      models.map((model) => [model.id, model.name, model.family, model.api, model.maxInputTokens, model.maxOutputTokens]),
      [
        ["system.ai.claude-sonnet-5-5", "Claude Sonnet 5.5", "claude-sonnet-5-5", "anthropic_messages", 872_000, 128_000],
        ["system.ai.gpt-6-luna", "system.ai.gpt-6-luna", "gpt-6-luna", "responses", 128_000, 32_000],
        ["gpt-4.1", "gpt-4.1", "gpt-4.1", "chat_completions", 128_000, 32_000],
      ],
    );
    assert.deepEqual(await new DatabricksModels(() => ({ ...settings(""), models: [] }), (line) => logs.push(line)).provideLanguageModelChatInformation(), []);
    assert.deepEqual(logs, ["No Databricks models: run `ug configure --agents vscode` to set them up."]);
  });

  const answers: [number, string][] = [
    [200, sse('{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}', '{"type":"message_stop"}')],
    [200, sse('{"type":"response.output_text.delta","delta":"hi"}', '{"type":"response.completed","response":{}}')],
    [200, sse('{"choices":[{"delta":{"content":"hey"},"finish_reason":"stop"}]}', "[DONE]")],
  ];

  const askEach = async (provider: DatabricksModels) => {
    const reported: unknown[] = [];
    for (const model of await provider.provideLanguageModelChatInformation()) {
      await provider.provideLanguageModelChatResponse(
        model,
        [message(USER, text("hi"))],
        { toolMode: LanguageModelChatToolMode.Auto } as unknown as vscode.ProvideLanguageModelChatResponseOptions,
        { report: (part) => reported.push(part) },
        { isCancellationRequested: false, onCancellationRequested: () => ({ dispose() {} }) } as unknown as vscode.CancellationToken,
      );
    }
    return reported;
  };

  it("sends each model's turns to its API with the User-Agent and its output limit", async () => {
    const requests = stubFetch(...answers);
    const provider = new DatabricksModels(() => settings(tokenCommand(["t"]).command), () => {}, () => ({ "User-Agent": "ucode/0 githubcopilotchat" }));
    const reported = await askEach(provider);
    assert.deepEqual(
      requests.map((request) => [request.url, request.body.model, request.userAgent, request.provider]),
      [
        ["https://ws/ai-gateway/anthropic/v1/messages", "system.ai.claude-sonnet-5-5", "ucode/0 githubcopilotchat", null],
        ["https://ws/ai-gateway/mlflow/v1/responses", "system.ai.gpt-6-luna", "ucode/0 githubcopilotchat", null],
        ["https://ws/ai-gateway/mlflow/v1/chat/completions", "gpt-4.1", "ucode/0 githubcopilotchat", null],
      ],
    );
    assert.equal(requests[0]?.body.max_tokens, 128_000);
    assert.deepEqual(reported, [new LanguageModelTextPart("hello"), new LanguageModelTextPart("hi"), new LanguageModelTextPart("hey")]);
  });

  it("routes a Model Provider Service's models with its header, and OpenAI ones on the OpenAI route", async () => {
    const requests = stubFetch(...answers);
    await askEach(new DatabricksModels(() => settings(tokenCommand(["t"]).command, "main.default.mps"), () => {}));
    assert.deepEqual(
      requests.map((request) => [request.url, request.provider]),
      [
        ["https://ws/ai-gateway/anthropic/v1/messages", "main.default.mps"],
        ["https://ws/ai-gateway/openai/v1/responses", "main.default.mps"],
        ["https://ws/ai-gateway/openai/v1/chat/completions", "main.default.mps"],
      ],
    );
  });
});
