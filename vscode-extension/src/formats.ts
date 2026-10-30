import * as vscode from "vscode";

// The stable API names only User and Assistant; Copilot Chat sends its system prompt with role 3.
const SYSTEM_ROLE = 3;

export interface Turn {
  readonly model: string;
  readonly messages: readonly vscode.LanguageModelChatRequestMessage[];
  readonly tools: readonly vscode.LanguageModelChatTool[];
  readonly toolRequired: boolean;
}

export type Reply = vscode.LanguageModelTextPart | vscode.LanguageModelToolCallPart;

const IMAGE_TOKENS = 1000;

const textOf = (part: unknown): string => {
  if (part instanceof vscode.LanguageModelTextPart) {
    return part.value;
  }
  if (part instanceof vscode.LanguageModelDataPart && !part.mimeType.startsWith("image/")) {
    return new TextDecoder().decode(part.data);
  }
  if (part instanceof vscode.LanguageModelPromptTsxPart) {
    return JSON.stringify(part.value);
  }
  return "";
};

const imageData = (part: unknown): { mimeType: string; data: string } | undefined =>
  part instanceof vscode.LanguageModelDataPart && part.mimeType.startsWith("image/")
    ? { mimeType: part.mimeType, data: Buffer.from(part.data).toString("base64") }
    : undefined;

const imageUrl = (part: unknown): string | undefined => {
  const image = imageData(part);
  return image && `data:${image.mimeType};base64,${image.data}`;
};

// The gateway requires a schema on every function tool; VS Code omits it for tools that take no input.
const parametersOf = (tool: vscode.LanguageModelChatTool): object => tool.inputSchema ?? { type: "object", properties: {} };

const toolCall = (callId: string, name: string, args: string): vscode.LanguageModelToolCallPart => {
  let input: unknown;
  try {
    input = args.trim() ? JSON.parse(args) : {};
  } catch {
    input = undefined;
  }
  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    throw new Error(`The model called ${name} with arguments that aren't a JSON object: ${args}`);
  }
  return new vscode.LanguageModelToolCallPart(callId, name, input);
};

/** Why the model stopped short, from a Responses `incomplete_details.reason`, a Chat Completions
 * `finish_reason` or an Anthropic `stop_reason`. */
const stoppedEarly = (reason: string | undefined): Error =>
  reason === "max_output_tokens" || reason === "length" || reason === "max_tokens"
    ? new Error("The model stopped at its output token limit before finishing the response.")
    : reason === "content_filter" || reason === "refusal"
      ? new Error("The model's response was stopped by a content filter.")
      : new Error(`The model stopped before finishing the response (${reason ?? "no reason given"}).`);

// A dropped connection ends the stream like a finished answer does, so only a final event counts as done.
const cutOff = () => new Error("The response ended before the model finished. Try again.");

const toolOptions = (turn: Turn, toTool: (tool: vscode.LanguageModelChatTool) => object): object =>
  turn.tools.length ? { tools: turn.tools.map(toTool), ...(turn.toolRequired ? { tool_choice: "required" } : {}) } : {};

/** A rough count: about four characters per token, and a flat cost per image. */
export const estimateTokens = (input: string | vscode.LanguageModelChatRequestMessage): number => {
  if (typeof input === "string") {
    return Math.ceil(input.length / 4);
  }
  let characters = 0;
  let images = 0;
  for (const part of input.content) {
    const items = part instanceof vscode.LanguageModelToolResultPart ? part.content : [part];
    for (const item of items) {
      characters += item instanceof vscode.LanguageModelToolCallPart ? item.name.length + JSON.stringify(item.input).length : textOf(item).length;
      images += imageUrl(item) === undefined ? 0 : 1;
    }
  }
  return Math.ceil(characters / 4) + images * IMAGE_TOKENS;
};

const imagesOf = (parts: readonly unknown[]): string[] => parts.map(imageUrl).filter((url) => url !== undefined);

/** Splits a message into its tool results, tool calls, other text, and images. */
const sortParts = (content: readonly unknown[]) => ({
  results: content.filter((part) => part instanceof vscode.LanguageModelToolResultPart),
  calls: content.filter((part) => part instanceof vscode.LanguageModelToolCallPart),
  text: content.map(textOf).join(""),
  images: imagesOf(content),
});

export function chatCompletionsRequest(turn: Turn): object {
  const messages = turn.messages.flatMap((message): object[] => {
    const { results, calls, text, images } = sortParts(message.content);
    if (Number(message.role) === SYSTEM_ROLE) {
      return [{ role: "system", content: text }];
    }
    if (message.role === vscode.LanguageModelChatMessageRole.Assistant) {
      const toolCalls = calls.map((call) => ({
        id: call.callId,
        type: "function",
        function: { name: call.name, arguments: JSON.stringify(call.input) },
      }));
      return text || toolCalls.length ? [{ role: "assistant", content: text || null, ...(toolCalls.length ? { tool_calls: toolCalls } : {}) }] : [];
    }
    // The gateway rejects images in a tool message, so a tool's images follow its result in the user message.
    const content = [
      ...(text ? [{ type: "text", text }] : []),
      ...[...results.flatMap((result) => imagesOf(result.content)), ...images].map((url) => ({ type: "image_url", image_url: { url } })),
    ];
    return [
      ...results.map((result) => ({ role: "tool", tool_call_id: result.callId, content: result.content.map(textOf).join("") })),
      ...(content.length ? [{ role: "user", content }] : []),
    ];
  });
  return {
    model: turn.model,
    messages,
    stream: true,
    ...toolOptions(turn, (tool) => ({ type: "function", function: { name: tool.name, description: tool.description, parameters: parametersOf(tool) } })),
  };
}

type ChatContent = string | { type?: string; text?: string }[] | null;

interface ChatChunk {
  error?: string | { message?: string };
  choices?: {
    delta?: { content?: ChatContent; tool_calls?: { index?: number; id?: string; function?: { name?: string; arguments?: string } }[] };
    finish_reason?: string | null;
  }[];
}

/** While a Claude model thinks, the gateway sends `content` as blocks rather than a string. Only the
 * text ones are the reply: passing a block along would make Copilot Chat fail the turn. */
const deltaText = (content: ChatContent | undefined): string =>
  typeof content === "string"
    ? content
    : (content ?? [])
        .filter((block) => block.type === "text")
        .map((block) => block.text ?? "")
        .join("");

const CHAT_FINISHED = new Set(["stop", "tool_calls", "function_call"]);

export async function* chatCompletionsReplies(events: AsyncIterable<unknown>): AsyncGenerator<Reply> {
  const calls: { id: string; name: string; args: string }[] = [];
  let finish: string | undefined;
  for await (const event of events) {
    const { error } = event as ChatChunk;
    if (error) {
      throw new Error(typeof error === "string" ? error : (error.message ?? JSON.stringify(error)));
    }
    for (const choice of (event as ChatChunk).choices ?? []) {
      const text = deltaText(choice.delta?.content);
      if (text) {
        yield new vscode.LanguageModelTextPart(text);
      }
      for (const fragment of choice.delta?.tool_calls ?? []) {
        const call = (calls[fragment.index ?? 0] ??= { id: "", name: "", args: "" });
        call.id = fragment.id || call.id;
        call.name = fragment.function?.name || call.name;
        call.args += fragment.function?.arguments ?? "";
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
  for (const call of calls.filter(Boolean)) {
    yield toolCall(call.id, call.name, call.args);
  }
}

const functionOutput = (content: readonly unknown[]): string | object[] => {
  const text = content.map(textOf).join("");
  const images = imagesOf(content);
  return images.length
    ? [...(text ? [{ type: "input_text", text }] : []), ...images.map((url) => ({ type: "input_image", image_url: url, detail: "auto" }))]
    : text;
};

// Stateless: the gateway keeps no response state, so every turn resends the whole conversation.
export function responsesRequest(turn: Turn): object {
  const instructions: string[] = [];
  const input = turn.messages.flatMap((message): object[] => {
    const { results, calls, text, images } = sortParts(message.content);
    if (Number(message.role) === SYSTEM_ROLE) {
      // Microsoft Foundry rejects a system-role input item outright, whatever its content, while
      // every OpenAI-compatible surface takes the prompt here.
      instructions.push(text);
      return [];
    }
    if (message.role === vscode.LanguageModelChatMessageRole.Assistant) {
      return [
        ...(text ? [{ role: "assistant", content: text }] : []),
        ...calls.map((call) => ({ type: "function_call", call_id: call.callId, name: call.name, arguments: JSON.stringify(call.input) })),
      ];
    }
    const content = [
      ...(text ? [{ type: "input_text", text }] : []),
      ...images.map((url) => ({ type: "input_image", image_url: url, detail: "auto" })),
    ];
    return [
      ...results.map((result) => ({ type: "function_call_output", call_id: result.callId, output: functionOutput(result.content) })),
      ...(content.length ? [{ role: "user", content }] : []),
    ];
  });
  return {
    model: turn.model,
    ...(instructions.length ? { instructions: instructions.join("\n\n") } : {}),
    input,
    stream: true,
    store: false,
    ...toolOptions(turn, (tool) => ({ type: "function", name: tool.name, description: tool.description, parameters: parametersOf(tool), strict: false })),
  };
}

interface ResponseEvent {
  type: string;
  delta?: string;
  item?: { type?: string; call_id?: string; name?: string; arguments?: string };
  response?: { incomplete_details?: { reason?: string } | null; error?: { message?: string } | null };
  message?: string;
}

export async function* responsesReplies(events: AsyncIterable<unknown>): AsyncGenerator<Reply> {
  // Held back until the response completes, so a cut-off stream runs no tool.
  const calls: vscode.LanguageModelToolCallPart[] = [];
  let completed = false;
  for await (const raw of events) {
    const event = raw as ResponseEvent;
    if (event.type === "response.output_text.delta" && event.delta) {
      yield new vscode.LanguageModelTextPart(event.delta);
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

const textBlocks = (text: string): object[] => (text ? [{ type: "text", text }] : []);

const imageBlocks = (parts: readonly unknown[]): object[] =>
  parts.flatMap((part) => {
    const image = imageData(part);
    return image ? [{ type: "image", source: { type: "base64", media_type: image.mimeType, data: image.data } }] : [];
  });

const CACHE_POINT = { cache_control: { type: "ephemeral" } };

/** Marks the last block, so the gateway caches the prompt up to it for the next turn. */
const cacheUpTo = (blocks: object[]): void => {
  if (blocks.length) {
    blocks[blocks.length - 1] = { ...blocks[blocks.length - 1], ...CACHE_POINT };
  }
};

export function anthropicMessagesRequest(turn: Turn, maxTokens: number): object {
  const system: object[] = [];
  const messages: { role: "user" | "assistant"; content: object[] }[] = [];
  for (const message of turn.messages) {
    const { results, calls, text } = sortParts(message.content);
    if (Number(message.role) === SYSTEM_ROLE) {
      system.push(...textBlocks(text));
      continue;
    }
    const role = message.role === vscode.LanguageModelChatMessageRole.Assistant ? "assistant" : "user";
    const content =
      role === "assistant"
        ? [...textBlocks(text), ...calls.map((call) => ({ type: "tool_use", id: call.callId, name: call.name, input: call.input }))]
        : [
            // Tool results have to come first in the user message that follows the calls.
            ...results.map((result) => {
              const blocks = [...textBlocks(result.content.map(textOf).join("")), ...imageBlocks(result.content)];
              return { type: "tool_result", tool_use_id: result.callId, ...(blocks.length ? { content: blocks } : {}) };
            }),
            ...textBlocks(text),
            ...imageBlocks(message.content),
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
    ...(system.length ? { system } : {}),
    messages,
    stream: true,
    ...(turn.tools.length
      ? {
          tools: turn.tools.map((tool) => ({ name: tool.name, description: tool.description, input_schema: parametersOf(tool) })),
          ...(turn.toolRequired ? { tool_choice: { type: "any" } } : {}),
        }
      : {}),
  };
}

interface AnthropicEvent {
  type: string;
  index?: number;
  content_block?: { type?: string; id?: string; name?: string };
  delta?: { type?: string; text?: string; partial_json?: string; stop_reason?: string | null };
  error?: { message?: string };
}

const ANTHROPIC_FINISHED = new Set(["end_turn", "tool_use", "stop_sequence"]);

export async function* anthropicMessagesReplies(events: AsyncIterable<unknown>): AsyncGenerator<Reply> {
  // Held back until the message stops, so a cut-off stream runs no tool.
  const calls = new Map<number, { id: string; name: string; args: string }>();
  let stopped = false;
  for await (const raw of events) {
    const event = raw as AnthropicEvent;
    if (event.type === "content_block_start" && event.content_block?.type === "tool_use") {
      calls.set(event.index ?? 0, { id: event.content_block.id ?? "", name: event.content_block.name ?? "", args: "" });
    } else if (event.type === "content_block_delta" && event.delta?.type === "text_delta" && event.delta.text) {
      yield new vscode.LanguageModelTextPart(event.delta.text);
    } else if (event.type === "content_block_delta" && event.delta?.type === "input_json_delta") {
      const call = calls.get(event.index ?? 0);
      if (call) {
        call.args += event.delta.partial_json ?? "";
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
  for (const call of calls.values()) {
    yield toolCall(call.id, call.name, call.args);
  }
}
