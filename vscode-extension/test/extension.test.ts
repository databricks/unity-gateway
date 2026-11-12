import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { afterEach, describe, it } from "node:test";
import type * as vscode from "vscode";
import packageJson from "../package.json";
import { Tokens } from "../src/auth";
import {
  DatabricksModels,
  NOTICE_SHOWN_KEY,
  PROVIDER_HEADER,
  readConfig,
  requestHeaders,
  SESSION_ID_HEADER,
  sessionHeaders,
  SWITCHED_KEY,
  switchToDefaultModel,
  userAgent,
  whenCopilotChatIsOn,
  type Settings,
} from "../src/extension";
import { streamEvents } from "../src/gateway";
import { collect, message, rejectsWith, text, tokenCommand, USER } from "./support";
import { host, LanguageModelChatToolMode, LanguageModelTextPart } from "./vscode";

const sse = (...data: string[]) => data.map((item) => `data: ${item}\n\n`).join("");

/** Replaces fetch with one that answers each request with the next status and SSE body. */
const stubFetch = (...answers: [number, string, string?][]) => {
  const requests: {
    url: string;
    authorization: string;
    userAgent: string;
    provider: string | null;
    session: string | null;
    body: Record<string, unknown>;
  }[] = [];
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    const headers = new Headers(init.headers);
    requests.push({
      url,
      authorization: headers.get("authorization") ?? "",
      userAgent: headers.get("user-agent") ?? "",
      provider: headers.get(PROVIDER_HEADER),
      session: headers.get(SESSION_ID_HEADER),
      body: JSON.parse(String(init.body)),
    });
    const [status, body, contentType] = answers[Math.min(requests.length - 1, answers.length - 1)] ?? [500, ""];
    // Split the body mid-line so the reader has to join chunks.
    const bytes = new TextEncoder().encode(body);
    const middle = Math.floor(bytes.length / 2);
    return new Response(new Blob([bytes.slice(0, middle), bytes.slice(middle)]).stream(), {
      status,
      ...(contentType ? { headers: { "content-type": contentType } } : {}),
    });
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
      "Run `ug configure --agents copilot` to sign in again",
    );
    assert.equal(requests.length, 2);
  });

  it("retries once with the output limit the gateway allows, and reports any other error", async () => {
    const tooBig = [400, '{"message":"max_tokens: 32000 > 8192, which is the maximum allowed number of output tokens for claude-3-5-sonnet"}'] as [number, string];
    let requests = stubFetch(tooBig, [200, sse('{"a":1}', "[DONE]")]);
    const events = await collect(
      streamEvents("https://ws", "/m", { model: "claude-3-5-sonnet", max_tokens: 32_000 }, new Tokens(tokenCommand(["t"]).command), {}, new AbortController().signal),
    );
    assert.deepEqual(events, [{ a: 1 }]);
    assert.deepEqual(
      requests.map((request) => request.body.max_tokens),
      [32_000, 8_192],
    );

    requests = stubFetch(tooBig, tooBig);
    await rejectsWith(
      collect(streamEvents("https://ws", "/m", { max_tokens: 32_000 }, new Tokens(tokenCommand(["t"]).command), {}, new AbortController().signal)),
      "Unity Gateway returned 400: {\"message\":\"max_tokens: 32000 > 8192",
    );
    assert.equal(requests.length, 2);
  });

  it("reports a web page, such as a sign-in page, instead of reading it as a cut-off reply", async () => {
    stubFetch([200, "<!doctype html><title>Databricks - Sign In</title>", "text/html; charset=utf-8"]);
    await rejectsWith(
      collect(streamEvents("https://ws", "/r", {}, new Tokens(tokenCommand(["t"]).command), {}, new AbortController().signal)),
      "Unity Gateway answered with a web page instead of the model's reply. Run `ug configure --agents copilot` again, then retry.",
    );
  });

  it("reports other gateway errors with their body", async () => {
    stubFetch([400, '{"message":"bad tools"}']);
    await rejectsWith(
      collect(streamEvents("https://ws", "/r", {}, new Tokens(tokenCommand(["t"]).command), {}, new AbortController().signal)),
      'Unity Gateway returned 400: {"message":"bad tools"}',
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

describe("requestHeaders", () => {
  it("adds the admin's headers to the User-Agent, and theirs wins by name", () => {
    assert.deepEqual(requestHeaders("0.71.0"), { "User-Agent": `ucode/${packageJson.version} githubcopilotchat/0.71.0` });
    assert.deepEqual(requestHeaders("0.71.0", { "X-Team": "ml" }), {
      "User-Agent": `ucode/${packageJson.version} githubcopilotchat/0.71.0`,
      "X-Team": "ml",
    });
    assert.deepEqual(requestHeaders("0.71.0", { "user-agent": "admin" }), { "user-agent": "admin" });
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
        { id: "e", api: "anthropic" },
        { id: "b", api: "messages" },
        { api: "responses" },
        null,
        { id: "c", name: " Model C ", api: "completions", maxInputTokens: 936_000, maxOutputTokens: "32000", extra: true },
        { id: "d", name: 7, api: "completions", maxInputTokens: 0, maxOutputTokens: 1.5 },
      ],
    };
    assert.deepEqual(readConfig(JSON.stringify(config)), {
      workspace: "https://ws",
      authCommand: "ug auth-token",
      provider: "main.default.mps",
      models: [
        { id: "a", api: "responses" },
        { id: "gpt-5.5", family: "gpt-5.5", api: "responses" },
        { id: "e", api: "anthropic" },
        { id: "c", name: "Model C", api: "completions", maxInputTokens: 936_000 },
        { id: "d", api: "completions" },
      ],
    });
  });

  it("keeps the string headers except Authorization", () => {
    const config = {
      workspace: "https://ws",
      authCommand: "ug auth-token",
      models: [],
      headers: { "X-Team": "ml", authorization: "Bearer x", "X-Count": 3, " ": "blank" },
    };
    assert.deepEqual(readConfig(JSON.stringify(config)), {
      workspace: "https://ws",
      authCommand: "ug auth-token",
      provider: "",
      models: [],
      headers: { "X-Team": "ml" },
    });
    assert.equal(readConfig(JSON.stringify({ ...config, headers: ["X-Team"] })).headers, undefined);
  });

  it("keeps a well-formed trace receiver setting only", () => {
    const base = { workspace: "https://ws", authCommand: "ug auth-token", models: [] };
    assert.deepEqual(readConfig(JSON.stringify({ ...base, otel: { port: 4555, secret: "s3_c-r" } })).otel, { port: 4555, secret: "s3_c-r" });
    for (const otel of [{ port: 0, secret: "s" }, { port: 70_000, secret: "s" }, { port: 4555, secret: "a/b" }, { port: "4555", secret: "s" }, "x"]) {
      assert.equal(readConfig(JSON.stringify({ ...base, otel })).otel, undefined, JSON.stringify(otel));
    }
  });

  it("keeps a default model only when it is one of the models", () => {
    const base = { workspace: "https://ws", authCommand: "ug auth-token", models: [{ id: "system.ai.opus", api: "completions" }, { id: "system.ai.sonnet", api: "unknown" }] };
    assert.equal(readConfig(JSON.stringify({ ...base, defaultModel: "system.ai.opus" })).defaultModel, "system.ai.opus");
    for (const defaultModel of ["system.ai.sonnet", "", 3, null]) {
      assert.equal(readConfig(JSON.stringify({ ...base, defaultModel })).defaultModel, undefined, JSON.stringify(defaultModel));
    }
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
      { id: "system.ai.claude-sonnet-5-5", name: "Claude Sonnet 5.5", api: "anthropic", maxInputTokens: 872_000, maxOutputTokens: 128_000 },
      { id: "system.ai.gpt-6-luna", api: "responses" },
      { id: "gpt-4.1", family: "gpt-4.1", api: "completions" },
    ],
  });

  it("tells VS Code to list the models again on refresh", () => {
    const provider = new DatabricksModels(() => settings("true"), () => {});
    let changes = 0;
    provider.onDidChangeLanguageModelChatInformation(() => changes++);
    provider.refresh();
    assert.equal(changes, 1);
  });

  it("lists the configured models by name, or by id without one, or none with a hint when settings are missing", async () => {
    const logs: string[] = [];
    const models = await new DatabricksModels(() => settings("cmd"), (line) => logs.push(line)).provideLanguageModelChatInformation();
    assert.deepEqual(
      models.map((model) => [model.id, model.name, model.family, model.api, model.maxInputTokens, model.maxOutputTokens]),
      [
        ["system.ai.claude-sonnet-5-5", "Claude Sonnet 5.5", "claude-sonnet-5-5", "anthropic", 872_000, 128_000],
        ["system.ai.gpt-6-luna", "system.ai.gpt-6-luna", "gpt-6-luna", "responses", 128_000, 32_000],
        ["gpt-4.1", "gpt-4.1", "gpt-4.1", "completions", 128_000, 32_000],
      ],
    );
    assert.deepEqual(await new DatabricksModels(() => ({ ...settings(""), models: [] }), (line) => logs.push(line)).provideLanguageModelChatInformation(), []);
    assert.deepEqual(logs, ["No Databricks models: run `ug configure --agents copilot` to set them up."]);
  });

  const answers: [number, string][] = [
    [200, sse('{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}', '{"type":"message_stop"}')],
    [200, sse('{"type":"response.output_text.delta","delta":"hi"}', '{"type":"response.completed","response":{}}')],
    [200, sse('{"choices":[{"delta":{"content":"hey"},"finish_reason":"stop"}]}', "[DONE]")],
  ];

  const askEach = async (provider: DatabricksModels, modelOptions?: Record<string, unknown>) => {
    const reported: unknown[] = [];
    for (const model of await provider.provideLanguageModelChatInformation()) {
      await provider.provideLanguageModelChatResponse(
        model,
        [message(USER, text("hi"))],
        { toolMode: LanguageModelChatToolMode.Auto, modelOptions } as unknown as vscode.ProvideLanguageModelChatResponseOptions,
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

  it("sends the chat session's id on every API, and none when Copilot Chat passes none", async () => {
    let requests = stubFetch(...answers);
    await askEach(new DatabricksModels(() => settings(tokenCommand(["t"]).command), () => {}), { _conversationId: "chat-1" });
    assert.deepEqual(requests.map((request) => request.session), ["chat-1", "chat-1", "chat-1"]);
    requests = stubFetch(...answers);
    await askEach(new DatabricksModels(() => settings(tokenCommand(["t"]).command), () => {}));
    assert.deepEqual(requests.map((request) => request.session), [null, null, null]);
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

describe("whenCopilotChatIsOn", () => {
  const TURN_ON_COMMAND = "workbench.action.chat.manage";
  const context = () => {
    const state = new Map<string, unknown>();
    return {
      subscriptions: [] as { dispose(): void }[],
      globalState: { get: (key: string) => state.get(key), update: async (key: string, value: unknown) => void state.set(key, value) },
    } as unknown as vscode.ExtensionContext;
  };
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  afterEach(() => host.reset());

  it("is ready at once, with no notice, when Copilot Chat is on", async () => {
    host.copilotChat = { activate: async () => {} };
    const calls: boolean[] = [];
    whenCopilotChatIsOn(context(), (late) => calls.push(late), () => {});
    await settle();
    assert.deepEqual(calls, [false]);
    assert.deepEqual(host.notices, []);
  });

  it("offers to turn Copilot Chat on once, and is ready after it starts", async () => {
    host.commands = [TURN_ON_COMMAND];
    host.choice = "Turn On Copilot Chat";
    const calls: boolean[] = [];
    const ctx = context();
    whenCopilotChatIsOn(ctx, (late) => calls.push(late), () => {});
    await settle();
    assert.deepEqual(host.notices, [{ message: "Databricks models need Copilot Chat, which is turned off.", actions: ["Turn On Copilot Chat"] }]);
    assert.deepEqual(host.executed, [TURN_ON_COMMAND]);
    assert.equal(ctx.globalState.get(NOTICE_SHOWN_KEY), true);
    assert.deepEqual(calls, []);

    let started = false;
    host.copilotChat = { activate: async () => void (started = true) };
    for (const listener of [...host.extensionListeners]) listener();
    await settle();
    assert.equal(started, true);
    assert.deepEqual(calls, [true]);
    assert.deepEqual(host.extensionListeners, []);

    host.reset();
    whenCopilotChatIsOn(ctx, () => {}, () => {});
    await settle();
    assert.deepEqual(host.notices, []);
  });

  it("says how to turn Copilot Chat on when VS Code lacks the command", async () => {
    host.choice = "Turn On Copilot Chat";
    whenCopilotChatIsOn(context(), () => {}, () => {});
    await settle();
    assert.deepEqual(
      host.notices.map((notice) => notice.message),
      ["Databricks models need Copilot Chat, which is turned off.", "To turn on Copilot Chat, click + in the chat's model picker."],
    );
    assert.deepEqual(host.executed, []);
  });

  it("does nothing more when the user dismisses the notice", async () => {
    host.commands = [TURN_ON_COMMAND];
    whenCopilotChatIsOn(context(), () => {}, () => {});
    await settle();
    assert.equal(host.notices.length, 1);
    assert.deepEqual(host.executed, []);
  });

  it("is ready at once, with no notice, in a remote window, where Copilot Chat runs out of sight", async () => {
    host.remoteName = "ssh-remote";
    const calls: boolean[] = [];
    whenCopilotChatIsOn(context(), (late) => calls.push(late), () => {});
    await settle();
    assert.deepEqual(calls, [false]);
    assert.deepEqual(host.notices, []);
  });

  it("lists the models even when Copilot Chat fails to start", async () => {
    const calls: boolean[] = [];
    whenCopilotChatIsOn(context(), (late) => calls.push(late), () => {});
    host.copilotChat = { activate: async () => Promise.reject(new Error("boom")) };
    for (const listener of [...host.extensionListeners]) listener();
    await settle();
    assert.deepEqual(calls, [true]);
  });

  it("asks again next time when the notice got no answer", async () => {
    const ctx = context();
    let answer: (choice: string | undefined) => void = () => {};
    const show = host.showNotice;
    host.showNotice = () => new Promise((resolve) => (answer = resolve));
    whenCopilotChatIsOn(ctx, () => {}, () => {});
    await settle();
    assert.equal(ctx.globalState.get(NOTICE_SHOWN_KEY), undefined);
    answer(undefined);
    await settle();
    assert.equal(ctx.globalState.get(NOTICE_SHOWN_KEY), true);
    host.showNotice = show;
  });

  it("shows no notice when the user turned AI features off", async () => {
    host.settings = { "chat.disableAIFeatures": true };
    host.commands = [TURN_ON_COMMAND];
    whenCopilotChatIsOn(context(), () => {}, () => {});
    await settle();
    assert.deepEqual(host.notices, []);
  });
});

describe("switchToDefaultModel", () => {
  const CHANGE = "workbench.action.chat.open";
  const selector = (model: { vendor: string; id: string }) => ({ modelSelector: { vendor: model.vendor, id: model.id } });
  const VENDOR = "databricks-unity-gateway";
  const sonnet = { vendor: VENDOR, id: "system.ai.claude-sonnet-5-5", family: "claude-sonnet-5-5" };
  const luna = { vendor: VENDOR, id: "system.ai.gpt-6-luna", family: "gpt-6-luna" };
  const settings = (defaultModel?: string): Settings => ({
    workspace: "https://ws",
    authCommand: "true",
    provider: "",
    models: [
      { id: sonnet.id, api: "anthropic" },
      { id: luna.id, api: "responses" },
    ],
    ...(defaultModel ? { defaultModel } : {}),
  });
  const context = () => {
    const state = new Map<string, unknown>();
    return {
      subscriptions: [],
      globalState: { get: (key: string) => state.get(key), update: async (key: string, value: unknown) => void state.set(key, value) },
    } as unknown as vscode.ExtensionContext;
  };
  afterEach(() => host.reset());

  it("opens the chat on the default model once per configuration", async () => {
    host.commands = [CHANGE];
    host.chatModels = [sonnet, luna];
    host.settings = { "chat.defaultModel": luna.id };
    const ctx = context();
    await switchToDefaultModel(ctx, settings(luna.id), "config-1", () => {});
    await switchToDefaultModel(ctx, settings(luna.id), "config-1", () => {});
    assert.deepEqual(host.commandArgs, [selector(luna)]);
    assert.equal(ctx.globalState.get(SWITCHED_KEY), "config-1");
    await switchToDefaultModel(ctx, settings(luna.id), "config-2", () => {});
    assert.deepEqual(host.commandArgs, [selector(luna), selector(luna)]);
  });

  it("opens it on the first model without a default", async () => {
    host.commands = [CHANGE];
    host.chatModels = [sonnet, luna];
    await switchToDefaultModel(context(), settings(), "config-1", () => {});
    assert.deepEqual(host.commandArgs, [selector(sonnet)]);
  });

  it("leaves the chats alone when the developer chose another default model", async () => {
    host.commands = [CHANGE];
    host.chatModels = [sonnet, luna];
    host.settings = { "chat.defaultModel": "gpt-4.1" };
    const ctx = context();
    await switchToDefaultModel(ctx, settings(luna.id), "config-1", () => {});
    assert.deepEqual(host.executed, []);
    assert.equal(ctx.globalState.get(SWITCHED_KEY), undefined);
  });

  it("waits for a later try while VS Code doesn't list the model", async () => {
    host.commands = [CHANGE];
    const ctx = context();
    await switchToDefaultModel(ctx, settings(luna.id), "config-1", () => {});
    assert.deepEqual(host.executed, []);
    assert.equal(ctx.globalState.get(SWITCHED_KEY), undefined);
  });

  it("logs, and doesn't retry, when VS Code has no command to switch", async () => {
    host.chatModels = [sonnet];
    const lines: string[] = [];
    const ctx = context();
    await switchToDefaultModel(ctx, settings(sonnet.id), "config-1", (line) => lines.push(line));
    assert.match(lines[0] ?? "", /Couldn't switch the chat to system.ai.claude-sonnet-5-5/);
    assert.equal(ctx.globalState.get(SWITCHED_KEY), "config-1");
  });
});

describe("sessionHeaders", () => {
  it("sends Copilot Chat's chat session id, and nothing without one", () => {
    assert.deepEqual(sessionHeaders({ _conversationId: "c-1", _capturingTokenCorrelationId: "r-1" }), { "session-id": "c-1" });
    assert.deepEqual(sessionHeaders({ _conversationId: 7 }), {});
    assert.deepEqual(sessionHeaders({ _conversationId: "" }), {});
    assert.deepEqual(sessionHeaders(undefined), {});
  });
});
