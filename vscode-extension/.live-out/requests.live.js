"use strict";
var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __getProtoOf = Object.getPrototypeOf;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(
  // If the importer is in node compatibility mode or this is not an ESM
  // file that has been converted to a CommonJS file using a Babel-
  // compatible transform (i.e. "__esModule" has not been set), then set
  // "default" to the CommonJS "module.exports" for node compatibility.
  isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target,
  mod
));

// test/live/requests.live.ts
var import_strict2 = __toESM(require("node:assert/strict"));
var import_node_test = require("node:test");

// src/auth.ts
var import_node_child_process = require("node:child_process");
var EXPIRY_MARGIN_MS = 5 * 6e4;
var DEFAULT_LIFETIME_MS = 60 * 6e4;
var run = (command) => new Promise((resolve, reject) => {
  (0, import_node_child_process.exec)(command, { timeout: 6e4, windowsHide: true }, (error, stdout, stderr) => {
    if (error) {
      reject(new Error(`Couldn't get a Databricks token: \`${command}\` failed: ${String(stderr).trim().slice(0, 500) || error.message}`));
    } else {
      resolve(String(stdout).trim());
    }
  });
});
var expiresAt = (token) => {
  try {
    const { exp } = JSON.parse(Buffer.from(token.split(".")[1] ?? "", "base64url").toString());
    if (typeof exp === "number") {
      return exp * 1e3;
    }
  } catch {
  }
  return Date.now() + DEFAULT_LIFETIME_MS;
};
var Tokens = class {
  constructor(command, onFetch = () => {
  }) {
    this.command = command;
    this.onFetch = onFetch;
  }
  command;
  onFetch;
  cached;
  pending;
  get() {
    if (this.cached && this.cached.expiresAt - EXPIRY_MARGIN_MS > Date.now()) {
      return Promise.resolve(this.cached.value);
    }
    return this.fetch(this.command);
  }
  /** A token the gateway rejected. The CLI can hand back its cached copy while `exp` is still ahead, so force a new one. */
  async renew(rejected) {
    await this.pending?.catch(() => {
    });
    if (this.cached && this.cached.value !== rejected) {
      return this.cached.value;
    }
    this.cached = void 0;
    return this.fetch(`${this.command} --force-refresh`);
  }
  fetch(command) {
    this.pending ??= run(command).then((value) => {
      if (!value) {
        throw new Error(`Couldn't get a Databricks token: \`${command}\` printed nothing.`);
      }
      this.cached = { value, expiresAt: expiresAt(value) };
      this.onFetch(this.cached.expiresAt);
      return value;
    }).finally(() => {
      this.pending = void 0;
    });
    return this.pending;
  }
};

// test/vscode.ts
var LanguageModelTextPart = class {
  constructor(value) {
    this.value = value;
  }
  value;
};
var LanguageModelToolCallPart = class {
  constructor(callId, name, input) {
    this.callId = callId;
    this.name = name;
    this.input = input;
  }
  callId;
  name;
  input;
};
var LanguageModelToolResultPart = class {
  constructor(callId, content) {
    this.callId = callId;
    this.content = content;
  }
  callId;
  content;
};
var LanguageModelDataPart = class {
  constructor(data, mimeType) {
    this.data = data;
    this.mimeType = mimeType;
  }
  data;
  mimeType;
};
var LanguageModelPromptTsxPart = class {
  constructor(value) {
    this.value = value;
  }
  value;
};
var LanguageModelChatMessageRole = { User: 1, Assistant: 2 };

// src/formats.ts
var SYSTEM_ROLE = 3;
var textOf = (part) => {
  if (part instanceof LanguageModelTextPart) {
    return part.value;
  }
  if (part instanceof LanguageModelDataPart && !part.mimeType.startsWith("image/")) {
    return new TextDecoder().decode(part.data);
  }
  if (part instanceof LanguageModelPromptTsxPart) {
    return JSON.stringify(part.value);
  }
  return "";
};
var imageData = (part) => part instanceof LanguageModelDataPart && part.mimeType.startsWith("image/") ? { mimeType: part.mimeType, data: Buffer.from(part.data).toString("base64") } : void 0;
var imageUrl = (part) => {
  const image = imageData(part);
  return image && `data:${image.mimeType};base64,${image.data}`;
};
var parametersOf = (tool) => tool.inputSchema ?? { type: "object", properties: {} };
var toolCall = (callId, name, args) => {
  let input;
  try {
    input = args.trim() ? JSON.parse(args) : {};
  } catch {
    input = void 0;
  }
  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    throw new Error(`The model called ${name} with arguments that aren't a JSON object: ${args}`);
  }
  return new LanguageModelToolCallPart(callId, name, input);
};
var stoppedEarly = (reason) => reason === "max_output_tokens" || reason === "length" || reason === "max_tokens" ? new Error("The model stopped at its output token limit before finishing the response.") : reason === "content_filter" || reason === "refusal" ? new Error("The model's response was stopped by a content filter.") : new Error(`The model stopped before finishing the response (${reason ?? "no reason given"}).`);
var cutOff = () => new Error("The response ended before the model finished. Try again.");
var toolOptions = (turn, toTool) => turn.tools.length ? { tools: turn.tools.map(toTool), ...turn.toolRequired ? { tool_choice: "required" } : {} } : {};
var imagesOf = (parts) => parts.map(imageUrl).filter((url) => url !== void 0);
var sortParts = (content) => ({
  results: content.filter((part) => part instanceof LanguageModelToolResultPart),
  calls: content.filter((part) => part instanceof LanguageModelToolCallPart),
  text: content.map(textOf).join(""),
  images: imagesOf(content)
});
function chatCompletionsRequest(turn) {
  const messages = turn.messages.flatMap((message2) => {
    const { results, calls, text: text2, images } = sortParts(message2.content);
    if (Number(message2.role) === SYSTEM_ROLE) {
      return [{ role: "system", content: text2 }];
    }
    if (message2.role === LanguageModelChatMessageRole.Assistant) {
      const toolCalls = calls.map((call2) => ({
        id: call2.callId,
        type: "function",
        function: { name: call2.name, arguments: JSON.stringify(call2.input) }
      }));
      return text2 || toolCalls.length ? [{ role: "assistant", content: text2 || null, ...toolCalls.length ? { tool_calls: toolCalls } : {} }] : [];
    }
    const content = [
      ...text2 ? [{ type: "text", text: text2 }] : [],
      ...[...results.flatMap((result2) => imagesOf(result2.content)), ...images].map((url) => ({ type: "image_url", image_url: { url } }))
    ];
    return [
      ...results.map((result2) => ({ role: "tool", tool_call_id: result2.callId, content: result2.content.map(textOf).join("") })),
      ...content.length ? [{ role: "user", content }] : []
    ];
  });
  return {
    model: turn.model,
    messages,
    stream: true,
    ...toolOptions(turn, (tool) => ({ type: "function", function: { name: tool.name, description: tool.description, parameters: parametersOf(tool) } }))
  };
}
var deltaText = (content) => typeof content === "string" ? content : (content ?? []).filter((block) => block.type === "text").map((block) => block.text ?? "").join("");
var CHAT_FINISHED = /* @__PURE__ */ new Set(["stop", "tool_calls", "function_call"]);
async function* chatCompletionsReplies(events) {
  const calls = [];
  let finish;
  for await (const event of events) {
    const { error } = event;
    if (error) {
      throw new Error(typeof error === "string" ? error : error.message ?? JSON.stringify(error));
    }
    for (const choice of event.choices ?? []) {
      const text2 = deltaText(choice.delta?.content);
      if (text2) {
        yield new LanguageModelTextPart(text2);
      }
      for (const fragment of choice.delta?.tool_calls ?? []) {
        const call2 = calls[fragment.index ?? 0] ??= { id: "", name: "", args: "" };
        call2.id = fragment.id || call2.id;
        call2.name = fragment.function?.name || call2.name;
        call2.args += fragment.function?.arguments ?? "";
      }
      if (choice.finish_reason) {
        finish = choice.finish_reason;
        if (!CHAT_FINISHED.has(finish)) {
          throw stoppedEarly(finish);
        }
      }
    }
  }
  if (!finish) {
    throw cutOff();
  }
  for (const call2 of calls.filter(Boolean)) {
    yield toolCall(call2.id, call2.name, call2.args);
  }
}
var functionOutput = (content) => {
  const text2 = content.map(textOf).join("");
  const images = imagesOf(content);
  return images.length ? [...text2 ? [{ type: "input_text", text: text2 }] : [], ...images.map((url) => ({ type: "input_image", image_url: url, detail: "auto" }))] : text2;
};
function responsesRequest(turn) {
  const input = turn.messages.flatMap((message2) => {
    const { results, calls, text: text2, images } = sortParts(message2.content);
    if (Number(message2.role) === SYSTEM_ROLE) {
      return [{ role: "system", content: text2 }];
    }
    if (message2.role === LanguageModelChatMessageRole.Assistant) {
      return [
        ...text2 ? [{ role: "assistant", content: text2 }] : [],
        ...calls.map((call2) => ({ type: "function_call", call_id: call2.callId, name: call2.name, arguments: JSON.stringify(call2.input) }))
      ];
    }
    const content = [
      ...text2 ? [{ type: "input_text", text: text2 }] : [],
      ...images.map((url) => ({ type: "input_image", image_url: url, detail: "auto" }))
    ];
    return [
      ...results.map((result2) => ({ type: "function_call_output", call_id: result2.callId, output: functionOutput(result2.content) })),
      ...content.length ? [{ role: "user", content }] : []
    ];
  });
  return {
    model: turn.model,
    input,
    stream: true,
    store: false,
    ...toolOptions(turn, (tool) => ({ type: "function", name: tool.name, description: tool.description, parameters: parametersOf(tool), strict: false }))
  };
}
async function* responsesReplies(events) {
  const calls = [];
  let completed = false;
  for await (const raw of events) {
    const event = raw;
    if (event.type === "response.output_text.delta" && event.delta) {
      yield new LanguageModelTextPart(event.delta);
    } else if (event.type === "response.output_item.done" && event.item?.type === "function_call") {
      calls.push(toolCall(event.item.call_id ?? "", event.item.name ?? "", event.item.arguments ?? ""));
    } else if (event.type === "response.completed") {
      completed = true;
    } else if (event.type === "response.incomplete") {
      throw stoppedEarly(event.response?.incomplete_details?.reason);
    } else if (event.type === "response.failed" || event.type === "error") {
      throw new Error(event.response?.error?.message ?? event.message ?? "The model response failed.");
    }
  }
  if (!completed) {
    throw cutOff();
  }
  yield* calls;
}
var textBlocks = (text2) => text2 ? [{ type: "text", text: text2 }] : [];
var imageBlocks = (parts) => parts.flatMap((part) => {
  const image = imageData(part);
  return image ? [{ type: "image", source: { type: "base64", media_type: image.mimeType, data: image.data } }] : [];
});
var CACHE_POINT = { cache_control: { type: "ephemeral" } };
var cacheUpTo = (blocks) => {
  if (blocks.length) {
    blocks[blocks.length - 1] = { ...blocks[blocks.length - 1], ...CACHE_POINT };
  }
};
function anthropicMessagesRequest(turn, maxTokens) {
  const system = [];
  const messages = [];
  for (const message2 of turn.messages) {
    const { results, calls, text: text2 } = sortParts(message2.content);
    if (Number(message2.role) === SYSTEM_ROLE) {
      system.push(...textBlocks(text2));
      continue;
    }
    const role = message2.role === LanguageModelChatMessageRole.Assistant ? "assistant" : "user";
    const content = role === "assistant" ? [...textBlocks(text2), ...calls.map((call2) => ({ type: "tool_use", id: call2.callId, name: call2.name, input: call2.input }))] : [
      // Tool results have to come first in the user message that follows the calls.
      ...results.map((result2) => {
        const blocks = [...textBlocks(result2.content.map(textOf).join("")), ...imageBlocks(result2.content)];
        return { type: "tool_result", tool_use_id: result2.callId, ...blocks.length ? { content: blocks } : {} };
      }),
      ...textBlocks(text2),
      ...imageBlocks(message2.content)
    ];
    const last = messages[messages.length - 1];
    if (!content.length) {
      continue;
    } else if (last?.role === role) {
      last.content.push(...content);
    } else {
      messages.push({ role, content });
    }
  }
  cacheUpTo(system);
  cacheUpTo(messages[messages.length - 1]?.content ?? []);
  return {
    model: turn.model,
    max_tokens: maxTokens,
    ...system.length ? { system } : {},
    messages,
    stream: true,
    ...turn.tools.length ? {
      tools: turn.tools.map((tool) => ({ name: tool.name, description: tool.description, input_schema: parametersOf(tool) })),
      ...turn.toolRequired ? { tool_choice: { type: "any" } } : {}
    } : {}
  };
}
var ANTHROPIC_FINISHED = /* @__PURE__ */ new Set(["end_turn", "tool_use", "stop_sequence"]);
async function* anthropicMessagesReplies(events) {
  const calls = /* @__PURE__ */ new Map();
  let stopped = false;
  for await (const raw of events) {
    const event = raw;
    if (event.type === "content_block_start" && event.content_block?.type === "tool_use") {
      calls.set(event.index ?? 0, { id: event.content_block.id ?? "", name: event.content_block.name ?? "", args: "" });
    } else if (event.type === "content_block_delta" && event.delta?.type === "text_delta" && event.delta.text) {
      yield new LanguageModelTextPart(event.delta.text);
    } else if (event.type === "content_block_delta" && event.delta?.type === "input_json_delta") {
      const call2 = calls.get(event.index ?? 0);
      if (call2) {
        call2.args += event.delta.partial_json ?? "";
      }
    } else if (event.type === "message_delta" && event.delta?.stop_reason && !ANTHROPIC_FINISHED.has(event.delta.stop_reason)) {
      throw stoppedEarly(event.delta.stop_reason);
    } else if (event.type === "message_stop") {
      stopped = true;
    } else if (event.type === "error") {
      throw new Error(event.error?.message ?? "The model response failed.");
    }
  }
  if (!stopped) {
    throw cutOff();
  }
  for (const call2 of calls.values()) {
    yield toolCall(call2.id, call2.name, call2.args);
  }
}

// src/gateway.ts
async function* streamEvents(workspace, path, body, tokens2, headers, signal) {
  const post = (token2, payload) => fetch(`${workspace}${path}`, {
    method: "POST",
    headers: { ...headers, Authorization: `Bearer ${token2}`, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal
  });
  let token = await tokens2.get();
  let response = await post(token, body);
  if (response.status === 401) {
    await response.body?.cancel();
    token = await tokens2.renew(token);
    response = await post(token, body);
  }
  if (response.status === 401) {
    await response.body?.cancel();
    throw new Error("Databricks rejected the sign-in. Run `ug configure --agents vscode` to sign in again, then retry.");
  }
  if (!response.ok || !response.body) {
    const detail = (await response.text()).slice(0, 300);
    const allowed = Number(/max_tokens: \d+ > (\d+)/.exec(detail)?.[1]);
    if (!allowed) {
      throw new Error(`AI Gateway returned ${response.status}: ${detail}`);
    }
    response = await post(token, { ...body, max_tokens: allowed });
    if (!response.ok || !response.body) {
      throw new Error(`AI Gateway returned ${response.status}: ${(await response.text()).slice(0, 300)}`);
    }
  }
  const decoder = new TextDecoder();
  let buffer = "";
  for await (const chunk of response.body) {
    buffer += decoder.decode(chunk, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      const data = dataOf(line);
      if (data === "[DONE]") {
        return;
      }
      if (data) {
        yield JSON.parse(data);
      }
    }
  }
  const last = dataOf(buffer + decoder.decode());
  if (last && last !== "[DONE]") {
    yield JSON.parse(last);
  }
}
var dataOf = (line) => line.startsWith("data:") ? line.slice(5).trim() : "";

// src/extension.ts
var PROVIDER_HEADER = "Databricks-Model-Provider-Service";

// test/support.ts
var import_strict = __toESM(require("node:assert/strict"));
var SYSTEM = 3;
var USER = 1;
var ASSISTANT = 2;
var message = (role, ...content) => ({ role, content, name: void 0 });
var text = (value) => new LanguageModelTextPart(value);
var call = (id, name, input) => new LanguageModelToolCallPart(id, name, input);
var result = (id, ...content) => new LanguageModelToolResultPart(id, content);
var collect = async (items) => {
  const out = [];
  for await (const item of items) {
    out.push(item);
  }
  return out;
};
var rejectsWith = (promise, text2) => import_strict.default.rejects(promise, (error) => {
  import_strict.default.ok(error.message.includes(text2), `expected "${error.message}" to contain "${text2}"`);
  return true;
});

// test/live/requests.live.ts
var env = (name) => process.env[name] ?? "";
var WORKSPACE = env("UG_LIVE_WORKSPACE").replace(/\/+$/, "");
var AUTH_COMMAND = env("UG_LIVE_AUTH_COMMAND") || env("UG_LIVE_TOKEN") && `printf %s "$UG_LIVE_TOKEN"`;
if (!WORKSPACE || !AUTH_COMMAND) {
  throw new Error("Set UG_LIVE_WORKSPACE and UG_LIVE_AUTH_COMMAND (or UG_LIVE_TOKEN) to run the live tests.");
}
var MLFLOW = "/ai-gateway/mlflow/v1";
var OPENAI = "/ai-gateway/openai/v1";
var MESSAGES = "/ai-gateway/anthropic/v1/messages";
var ANTHROPIC_VERSION = { "anthropic-version": "2023-06-01" };
var tokens = new Tokens(AUTH_COMMAND);
var send = (path, body, headers = {}) => streamEvents(WORKSPACE, path, body, tokens, { ...headers }, new AbortController().signal);
var turnOf = (over) => ({
  messages: [],
  tools: [],
  toolRequired: false,
  ...over
});
var textOf2 = (replies) => replies.filter((reply) => reply instanceof LanguageModelTextPart).map((part) => part.value).join("");
var callsOf = (replies) => replies.filter((reply) => reply instanceof LanguageModelToolCallPart);
var RED_PNG = "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAFklEQVR4nGO4IyJCEmIY1TCqYfhqAAACcQQQFwFJQgAAAABJRU5ErkJggg==";
var redImage = () => new LanguageModelDataPart(Buffer.from(RED_PNG, "base64"), "image/png");
var viewImageTool = { name: "view_image", description: "Show the image at a path", inputSchema: { type: "object", properties: { path: { type: "string" } } } };
var marker = () => `UG${Math.floor(Math.random() * 1e6)}`;
var echoTurn = (model, mark) => turnOf({
  model,
  messages: [message(SYSTEM, text("Answer in as few words as possible.")), message(USER, text(`Reply with exactly: ${mark}`))]
});
var imageTurn = (model) => turnOf({
  model,
  tools: [viewImageTool],
  messages: [
    message(USER, text("Look at a.png with the tool, then name its main colour in one lowercase word.")),
    message(ASSISTANT, call("c1", "view_image", { path: "a.png" })),
    message(USER, result("c1", text("a.png:"), redImage()))
  ]
});
var anthropic = (turn, maxTokens, headers = {}) => collect(anthropicMessagesReplies(send(MESSAGES, anthropicMessagesRequest(turn, maxTokens), { ...ANTHROPIC_VERSION, ...headers })));
var responses = (turn, base = MLFLOW, headers = {}) => collect(responsesReplies(send(`${base}/responses`, responsesRequest(turn), headers)));
var chat = (turn, base = MLFLOW, headers = {}) => collect(chatCompletionsReplies(send(`${base}/chat/completions`, chatCompletionsRequest(turn), headers)));
var CLAUDE = env("UG_LIVE_CLAUDE_MODEL");
var GPT = env("UG_LIVE_GPT_MODEL");
var CHAT_GPT = env("UG_LIVE_CHAT_MODEL");
var MPS = env("UG_LIVE_MPS");
var MPS_MODEL = env("UG_LIVE_MPS_MODEL");
var OPENAI_MPS = env("UG_LIVE_OPENAI_MPS");
var OPENAI_MPS_MODEL = env("UG_LIVE_OPENAI_MPS_MODEL");
(0, import_node_test.describe)("the workspace's own models", () => {
  if (CLAUDE) {
    (0, import_node_test.it)("streams a Claude answer on Anthropic Messages, and reads the prompt cache on a repeat", async () => {
      const mark = marker();
      const system = text(`Answer in as few words as possible. ${"Ignore this sentence. ".repeat(400)}`);
      const turn = turnOf({ model: CLAUDE, messages: [message(SYSTEM, system), message(USER, text(`Reply with exactly: ${mark}`))] });
      import_strict2.default.match(textOf2(await anthropic(turn, 64)), new RegExp(mark));
      const events = await collect(send(MESSAGES, anthropicMessagesRequest(turn, 64), ANTHROPIC_VERSION));
      const start = events.find((event) => event.type === "message_start");
      import_strict2.default.ok((start?.message?.usage?.cache_read_input_tokens ?? 0) > 0, JSON.stringify(start?.message?.usage));
    });
    (0, import_node_test.it)("gets a Claude tool call, and an image inside its result reaches the model", async () => {
      const forced = turnOf({
        model: CLAUDE,
        tools: [viewImageTool],
        toolRequired: true,
        messages: [message(USER, text("Look at a.png."))]
      });
      const [only, ...rest] = callsOf(await anthropic(forced, 256));
      import_strict2.default.equal(rest.length, 0);
      import_strict2.default.equal(only?.name, "view_image");
      import_strict2.default.equal(typeof only?.input, "object");
      import_strict2.default.match(textOf2(await anthropic(imageTurn(CLAUDE), 256)).toLowerCase(), /red/);
    });
    (0, import_node_test.it)("retries with the output limit the gateway allows instead of failing the turn", async () => {
      import_strict2.default.ok(textOf2(await anthropic(echoTurn(CLAUDE, marker()), 999999)).length > 0);
    });
  }
  if (GPT) {
    (0, import_node_test.it)("streams a GPT answer and a tool call on Responses", async () => {
      const mark = marker();
      import_strict2.default.match(textOf2(await responses(echoTurn(GPT, mark))), new RegExp(mark));
      const forced = turnOf({ model: GPT, tools: [viewImageTool], toolRequired: true, messages: [message(USER, text("Look at a.png."))] });
      const [only] = callsOf(await responses(forced));
      import_strict2.default.equal(only?.name, "view_image");
    });
    (0, import_node_test.it)("sends a tool result's image in the request Responses accepts", async () => {
      import_strict2.default.match(textOf2(await responses(imageTurn(GPT))).toLowerCase(), /red/);
    });
  }
  if (CHAT_GPT) {
    (0, import_node_test.it)("streams an answer on Chat Completions", async () => {
      const mark = marker();
      import_strict2.default.match(textOf2(await chat(echoTurn(CHAT_GPT, mark))), new RegExp(mark));
    });
  }
});
(0, import_node_test.describe)("a Model Provider Service", () => {
  if (MPS && MPS_MODEL) {
    const header = { [PROVIDER_HEADER]: MPS };
    (0, import_node_test.it)("serves its Claude models on Anthropic Messages, with tool calls", async () => {
      const mark = marker();
      import_strict2.default.match(textOf2(await anthropic(echoTurn(MPS_MODEL, mark), 256, header)), new RegExp(mark));
      const forced = turnOf({ model: MPS_MODEL, tools: [viewImageTool], toolRequired: true, messages: [message(USER, text("Look at a.png."))] });
      const [only] = callsOf(await anthropic(forced, 256, header));
      import_strict2.default.equal(only?.name, "view_image");
    });
    (0, import_node_test.it)("needs the routing header, and isn't served on the mlflow route", async () => {
      const turn = echoTurn(MPS_MODEL, marker());
      await rejectsWith(anthropic(turn, 64), "AI Gateway returned");
      await rejectsWith(chat(turn, MLFLOW, header), "AI Gateway returned");
    });
  }
  if (OPENAI_MPS && OPENAI_MPS_MODEL) {
    const header = { [PROVIDER_HEADER]: OPENAI_MPS };
    (0, import_node_test.it)("serves its OpenAI models on the gateway's OpenAI route", async () => {
      const mark = marker();
      import_strict2.default.match(textOf2(await responses(echoTurn(OPENAI_MPS_MODEL, mark), OPENAI, header)), new RegExp(mark));
    });
    (0, import_node_test.it)("isn't served on the mlflow route", async () => {
      await rejectsWith(responses(echoTurn(OPENAI_MPS_MODEL, marker()), MLFLOW, header), "AI Gateway returned");
    });
  }
});
