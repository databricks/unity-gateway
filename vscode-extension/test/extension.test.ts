import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { afterEach, describe, it } from "node:test";
import type * as vscode from "vscode";
import packageJson from "../package.json";
import { Tokens } from "../src/auth";
import { DatabricksModels, readSettings, userAgent, type Settings } from "../src/extension";
import { streamEvents } from "../src/gateway";
import { collect, message, rejectsWith, text, tokenCommand, USER } from "./support";
import { LanguageModelChatToolMode, LanguageModelTextPart } from "./vscode";

const sse = (...data: string[]) => data.map((item) => `data: ${item}\n\n`).join("");

/** Replaces fetch with one that answers each request with the next status and SSE body. */
const stubFetch = (...answers: [number, string][]) => {
  const requests: { url: string; authorization: string; userAgent: string; body: Record<string, unknown> }[] = [];
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    const headers = new Headers(init.headers);
    requests.push({ url, authorization: headers.get("authorization") ?? "", userAgent: headers.get("user-agent") ?? "", body: JSON.parse(String(init.body)) });
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
    assert.deepEqual(await collect(streamEvents("https://ws", "responses", {}, tokens, {}, new AbortController().signal)), [{ a: 1 }, { b: 2 }]);
  });

  it("keeps a last event that has no trailing newline", async () => {
    stubFetch([200, `data: {"a":1}\n\ndata: {"b":2}`]);
    const tokens = new Tokens(tokenCommand(["t"]).command);
    assert.deepEqual(await collect(streamEvents("https://ws", "responses", {}, tokens, {}, new AbortController().signal)), [{ a: 1 }, { b: 2 }]);
  });

  it("retries once with a new token after a 401, then asks the user to sign in", async () => {
    let requests = stubFetch([401, ""], [200, sse("[DONE]")]);
    await collect(streamEvents("https://ws", "chat/completions", {}, new Tokens(tokenCommand(["fresh"], "rejected").command), {}, new AbortController().signal));
    assert.deepEqual(requests.map((request) => request.authorization), ["Bearer rejected", "Bearer fresh"]);
    assert.equal(requests[0]?.url, "https://ws/ai-gateway/mlflow/v1/chat/completions");

    requests = stubFetch([401, ""]);
    await rejectsWith(
      collect(streamEvents("https://ws", "responses", {}, new Tokens(tokenCommand(["a", "b"]).command), {}, new AbortController().signal)),
      "Run `ug configure --agents vscode` to sign in again",
    );
    assert.equal(requests.length, 2);
  });

  it("reports other gateway errors with their body", async () => {
    stubFetch([400, '{"message":"bad tools"}']);
    await rejectsWith(
      collect(streamEvents("https://ws", "responses", {}, new Tokens(tokenCommand(["t"]).command), {}, new AbortController().signal)),
      'AI Gateway returned 400: {"message":"bad tools"}',
    );
  });
});

describe("settings", () => {
  // Workspace settings mustn't redirect the token or choose the command that prints it.
  it("are read from user settings only", () => {
    const properties = Object.values(packageJson.contributes.configuration.properties);
    assert.deepEqual(properties.map((property) => property.scope), ["application", "application", "application"]);
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

describe("readSettings", () => {
  it("keeps well-formed models, names and token limits and trims the workspace URL", () => {
    const values: Record<string, unknown> = {
      workspace: "https://ws/",
      authCommand: "ug auth-token",
      models: [
        { id: "a", api: "responses" },
        { id: "b", api: "messages" },
        { api: "responses" },
        null,
        { id: "c", name: " Model C ", api: "chat_completions", maxInputTokens: 936_000, maxOutputTokens: "32000", extra: true },
        { id: "d", name: 7, api: "chat_completions", maxInputTokens: 0, maxOutputTokens: 1.5 },
      ],
    };
    assert.deepEqual(readSettings((key) => values[key]), {
      workspace: "https://ws",
      authCommand: "ug auth-token",
      models: [
        { id: "a", api: "responses" },
        { id: "c", name: "Model C", api: "chat_completions", maxInputTokens: 936_000 },
        { id: "d", api: "chat_completions" },
      ],
    });
  });
});

describe("DatabricksModels", () => {
  const settings = (command: string): Settings => ({
    workspace: "https://ws",
    authCommand: command,
    models: [
      { id: "system.ai.claude-sonnet-5-5", name: "Claude Sonnet 5.5", api: "chat_completions", maxInputTokens: 872_000, maxOutputTokens: 128_000 },
      { id: "system.ai.gpt-6-luna", api: "responses" },
    ],
  });

  it("lists the configured models by name, or by id without one, or none with a hint when settings are missing", async () => {
    const logs: string[] = [];
    const models = await new DatabricksModels(() => settings("cmd"), (line) => logs.push(line)).provideLanguageModelChatInformation();
    assert.deepEqual(
      models.map((model) => [model.id, model.name, model.family, model.api, model.maxInputTokens, model.maxOutputTokens]),
      [
        ["system.ai.claude-sonnet-5-5", "Claude Sonnet 5.5", "claude-sonnet-5-5", "chat_completions", 872_000, 128_000],
        ["system.ai.gpt-6-luna", "system.ai.gpt-6-luna", "gpt-6-luna", "responses", 128_000, 32_000],
      ],
    );
    assert.deepEqual(await new DatabricksModels(() => ({ ...settings(""), models: [] }), (line) => logs.push(line)).provideLanguageModelChatInformation(), []);
    assert.deepEqual(logs, ["No Databricks models: run `ug configure --agents vscode` to set them up."]);
  });

  it("sends each model's turns to its API with the User-Agent", async () => {
    const requests = stubFetch(
      [200, sse('{"choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}]}', "[DONE]")],
      [200, sse('{"type":"response.output_text.delta","delta":"hi"}', '{"type":"response.completed","response":{}}')],
    );
    const provider = new DatabricksModels(() => settings(tokenCommand(["t"]).command), () => {}, () => ({ "User-Agent": "ucode/0 githubcopilotchat" }));
    const [claude, gpt] = await provider.provideLanguageModelChatInformation();
    const reported: unknown[] = [];
    for (const model of [claude!, gpt!]) {
      await provider.provideLanguageModelChatResponse(
        model,
        [message(USER, text("hi"))],
        { toolMode: LanguageModelChatToolMode.Auto } as unknown as vscode.ProvideLanguageModelChatResponseOptions,
        { report: (part) => reported.push(part) },
        { isCancellationRequested: false, onCancellationRequested: () => ({ dispose() {} }) } as unknown as vscode.CancellationToken,
      );
    }
    assert.deepEqual(
      requests.map((request) => [request.url, request.body.model, request.userAgent]),
      [
        ["https://ws/ai-gateway/mlflow/v1/chat/completions", "system.ai.claude-sonnet-5-5", "ucode/0 githubcopilotchat"],
        ["https://ws/ai-gateway/mlflow/v1/responses", "system.ai.gpt-6-luna", "ucode/0 githubcopilotchat"],
      ],
    );
    assert.deepEqual(reported, [new LanguageModelTextPart("hello"), new LanguageModelTextPart("hi")]);
  });
});
