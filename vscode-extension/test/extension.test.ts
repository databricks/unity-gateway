import { afterEach, describe, expect, it, vi } from "vitest";
import type * as vscode from "vscode";
import packageJson from "../package.json";
import { Tokens } from "../src/auth";
import { DatabricksModels, readSettings, type Settings } from "../src/extension";
import { streamEvents } from "../src/gateway";
import { collect, message, text, tokenCommand, USER } from "./support";
import { LanguageModelChatToolMode, LanguageModelTextPart } from "./vscode";

const sse = (...data: string[]) => data.map((item) => `data: ${item}\n\n`).join("");

/** Replaces fetch with one that answers each request with the next status and SSE body. */
const stubFetch = (...answers: [number, string][]) => {
  const requests: { url: string; authorization: string; body: Record<string, unknown> }[] = [];
  vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
    const headers = new Headers(init.headers);
    requests.push({ url, authorization: headers.get("authorization") ?? "", body: JSON.parse(String(init.body)) });
    const [status, body] = answers[Math.min(requests.length - 1, answers.length - 1)] ?? [500, ""];
    // Split the body mid-line so the reader has to join chunks.
    const bytes = new TextEncoder().encode(body);
    const middle = Math.floor(bytes.length / 2);
    return new Response(new Blob([bytes.slice(0, middle), bytes.slice(middle)]).stream(), { status });
  });
  return requests;
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("streamEvents", () => {
  it("yields each event's JSON until [DONE]", async () => {
    stubFetch([200, sse('{"a":1}', '{"b":2}', "[DONE]", '{"c":3}')]);
    const tokens = new Tokens(tokenCommand(["t"]).command);
    expect(await collect(streamEvents("https://ws", "responses", {}, tokens, {}, new AbortController().signal))).toEqual([{ a: 1 }, { b: 2 }]);
  });

  it("keeps a last event that has no trailing newline", async () => {
    stubFetch([200, `data: {"a":1}\n\ndata: {"b":2}`]);
    const tokens = new Tokens(tokenCommand(["t"]).command);
    expect(await collect(streamEvents("https://ws", "responses", {}, tokens, {}, new AbortController().signal))).toEqual([{ a: 1 }, { b: 2 }]);
  });

  it("retries once with a new token after a 401, then asks the user to sign in", async () => {
    let requests = stubFetch([401, ""], [200, sse("[DONE]")]);
    await collect(streamEvents("https://ws", "chat/completions", {}, new Tokens(tokenCommand(["fresh"], "rejected").command), {}, new AbortController().signal));
    expect(requests.map((request) => request.authorization)).toEqual(["Bearer rejected", "Bearer fresh"]);
    expect(requests[0]?.url).toBe("https://ws/ai-gateway/mlflow/v1/chat/completions");

    requests = stubFetch([401, ""]);
    await expect(
      collect(streamEvents("https://ws", "responses", {}, new Tokens(tokenCommand(["a", "b"]).command), {}, new AbortController().signal)),
    ).rejects.toThrow("Run `databricks auth login --host https://ws`");
    expect(requests).toHaveLength(2);
  });

  it("reports other gateway errors with their body", async () => {
    stubFetch([400, '{"message":"bad tools"}']);
    await expect(
      collect(streamEvents("https://ws", "responses", {}, new Tokens(tokenCommand(["t"]).command), {}, new AbortController().signal)),
    ).rejects.toThrow('AI Gateway returned 400: {"message":"bad tools"}');
  });
});

describe("settings", () => {
  // Workspace settings mustn't redirect the token or choose the command that prints it.
  it("are read from user settings only", () => {
    const properties = Object.values(packageJson.contributes.configuration.properties);
    expect(properties.map((property) => property.scope)).toEqual(["application", "application", "application"]);
  });
});

describe("readSettings", () => {
  it("keeps well-formed models and trims the workspace URL", () => {
    const values: Record<string, unknown> = {
      workspace: "https://ws/",
      authCommand: "ug auth-token",
      models: [{ id: "a", api: "responses" }, { id: "b", api: "messages" }, { api: "responses" }, null, { id: "c", api: "chat_completions" }],
    };
    expect(readSettings((key) => values[key])).toEqual({
      workspace: "https://ws",
      authCommand: "ug auth-token",
      models: [
        { id: "a", api: "responses" },
        { id: "c", api: "chat_completions" },
      ],
    });
  });
});

describe("DatabricksModels", () => {
  const settings = (command: string): Settings => ({
    workspace: "https://ws",
    authCommand: command,
    models: [
      { id: "system.ai.claude-sonnet-5-5", api: "chat_completions" },
      { id: "system.ai.gpt-6-luna", api: "responses" },
    ],
  });

  it("lists the configured models, or none with a hint when settings are missing", async () => {
    const logs: string[] = [];
    const models = await new DatabricksModels(() => settings("cmd"), (line) => logs.push(line)).provideLanguageModelChatInformation();
    expect(models.map((model) => [model.id, model.family, model.api])).toEqual([
      ["system.ai.claude-sonnet-5-5", "claude-sonnet-5-5", "chat_completions"],
      ["system.ai.gpt-6-luna", "gpt-6-luna", "responses"],
    ]);
    expect(await new DatabricksModels(() => ({ ...settings(""), models: [] }), (line) => logs.push(line)).provideLanguageModelChatInformation()).toEqual([]);
    expect(logs).toEqual(["No Databricks models: run `ug configure --agents vscode` to set them up."]);
  });

  it("sends each model's turns to its API with the User-Agent", async () => {
    const requests = stubFetch(
      [200, sse('{"choices":[{"delta":{"content":"hello"}}]}', "[DONE]")],
      [200, sse('{"type":"response.output_text.delta","delta":"hi"}')],
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
    expect(requests.map((request) => [request.url, request.body.model])).toEqual([
      ["https://ws/ai-gateway/mlflow/v1/chat/completions", "system.ai.claude-sonnet-5-5"],
      ["https://ws/ai-gateway/mlflow/v1/responses", "system.ai.gpt-6-luna"],
    ]);
    expect(reported).toEqual([new LanguageModelTextPart("hello"), new LanguageModelTextPart("hi")]);
  });
});
