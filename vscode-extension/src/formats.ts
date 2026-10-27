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

const imageUrl = (part: unknown): string | undefined =>
  part instanceof vscode.LanguageModelDataPart && part.mimeType.startsWith("image/")
    ? `data:${part.mimeType};base64,${Buffer.from(part.data).toString("base64")}`
    : undefined;

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

const outputLimit = () => new Error("The model stopped at its output token limit before finishing the response.");

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

/** Splits a message into its tool results, tool calls, other text, and images. */
const sortParts = (content: readonly unknown[]) => ({
  results: content.filter((part) => part instanceof vscode.LanguageModelToolResultPart),
  calls: content.filter((part) => part instanceof vscode.LanguageModelToolCallPart),
  text: content.map(textOf).join(""),
  images: content.map(imageUrl).filter((url) => url !== undefined),
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
    const content = [
      ...(text ? [{ type: "text", text }] : []),
      ...images.map((url) => ({ type: "image_url", image_url: { url } })),
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

interface ChatChunk {
  choices?: {
    delta?: { content?: string | null; tool_calls?: { index?: number; id?: string; function?: { name?: string; arguments?: string } }[] };
    finish_reason?: string | null;
  }[];
}

export async function* chatCompletionsReplies(events: AsyncIterable<unknown>): AsyncGenerator<Reply> {
  const calls: { id: string; name: string; args: string }[] = [];
  for await (const event of events) {
    for (const choice of (event as ChatChunk).choices ?? []) {
      if (choice.delta?.content) {
        yield new vscode.LanguageModelTextPart(choice.delta.content);
      }
      for (const fragment of choice.delta?.tool_calls ?? []) {
        const call = (calls[fragment.index ?? 0] ??= { id: "", name: "", args: "" });
        call.id = fragment.id || call.id;
        call.name = fragment.function?.name || call.name;
        call.args += fragment.function?.arguments ?? "";
      }
      if (choice.finish_reason === "length") {
        throw outputLimit();
      }
    }
  }
  for (const call of calls.filter(Boolean)) {
    yield toolCall(call.id, call.name, call.args);
  }
}

// Stateless: the gateway keeps no response state, so every turn resends the whole conversation.
export function responsesRequest(turn: Turn): object {
  const input = turn.messages.flatMap((message): object[] => {
    const { results, calls, text, images } = sortParts(message.content);
    if (Number(message.role) === SYSTEM_ROLE) {
      return [{ role: "system", content: text }];
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
      ...results.map((result) => ({ type: "function_call_output", call_id: result.callId, output: result.content.map(textOf).join("") })),
      ...(content.length ? [{ role: "user", content }] : []),
    ];
  });
  return {
    model: turn.model,
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
  for await (const raw of events) {
    const event = raw as ResponseEvent;
    if (event.type === "response.output_text.delta" && event.delta) {
      yield new vscode.LanguageModelTextPart(event.delta);
    } else if (event.type === "response.output_item.done" && event.item?.type === "function_call") {
      yield toolCall(event.item.call_id ?? "", event.item.name ?? "", event.item.arguments ?? "");
    } else if (event.type === "response.incomplete" && event.response?.incomplete_details?.reason === "max_output_tokens") {
      throw outputLimit();
    } else if (event.type === "response.failed" || event.type === "error") {
      throw new Error(event.response?.error?.message ?? event.message ?? "The model response failed.");
    }
  }
}
