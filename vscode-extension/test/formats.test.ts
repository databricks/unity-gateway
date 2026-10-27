import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { chatCompletionsReplies, chatCompletionsRequest, estimateTokens, responsesReplies, responsesRequest } from "../src/formats";
import { ASSISTANT, call, collect, events, image, message, rejectsWith, result, SYSTEM, text, USER, weatherTool } from "./support";
import { LanguageModelDataPart, LanguageModelTextPart, LanguageModelToolCallPart } from "./vscode";

const conversation = [
  message(SYSTEM, text("Be brief.")),
  message(USER, text("Weather?"), image(7)),
  message(ASSISTANT, text("Checking."), call("c1", "get_weather", { city: "Paris" })),
  message(USER, result("c1", text("Sunny"))),
];
const turn = { model: "m", messages: conversation, tools: [weatherTool], toolRequired: true };

describe("Chat Completions", () => {
  it("sends the conversation, images and tools", () => {
    assert.deepEqual(chatCompletionsRequest(turn), {
      model: "m",
      stream: true,
      messages: [
        { role: "system", content: "Be brief." },
        { role: "user", content: [{ type: "text", text: "Weather?" }, { type: "image_url", image_url: { url: "data:image/png;base64,Bw==" } }] },
        { role: "assistant", content: "Checking.", tool_calls: [{ id: "c1", type: "function", function: { name: "get_weather", arguments: '{"city":"Paris"}' } }] },
        { role: "tool", tool_call_id: "c1", content: "Sunny" },
      ],
      tools: [{ type: "function", function: { name: "get_weather", description: "Weather for a city", parameters: weatherTool.inputSchema } }],
      tool_choice: "required",
    });
  });

  it("gives a tool without an input schema an empty one, and omits tools when there are none", () => {
    const noSchema = chatCompletionsRequest({ ...turn, tools: [{ name: "list", description: "List", inputSchema: undefined }], toolRequired: false });
    assert.deepEqual((noSchema as { tools: unknown[] }).tools, [
      { type: "function", function: { name: "list", description: "List", parameters: { type: "object", properties: {} } } },
    ]);
    assert.equal("tool_choice" in noSchema, false);
    assert.equal("tools" in chatCompletionsRequest({ ...turn, tools: [] }), false);
  });

  it("streams text and assembles a tool call from its fragments", async () => {
    const replies = await collect(
      chatCompletionsReplies(
        events(
          { choices: [{ delta: { content: "Checking." } }] },
          { choices: [{ delta: { tool_calls: [{ index: 0, id: "c1", function: { name: "get_weather", arguments: '{"ci' } }] } }] },
          { choices: [{ delta: { tool_calls: [{ index: 0, function: { arguments: 'ty":"Paris"}' } }] }, finish_reason: "tool_calls" }] },
        ),
      ),
    );
    assert.deepEqual(replies, [new LanguageModelTextPart("Checking."), new LanguageModelToolCallPart("c1", "get_weather", { city: "Paris" })]);
  });

  it("fails on a length stop and on arguments that aren't an object", async () => {
    await rejectsWith(collect(chatCompletionsReplies(events({ choices: [{ finish_reason: "length" }] }))), "output token limit");
    await rejectsWith(
      collect(chatCompletionsReplies(events({ choices: [{ delta: { tool_calls: [{ index: 0, id: "c", function: { name: "t", arguments: "[1]" } }] } }] }))),
      "aren't a JSON object",
    );
  });
});

describe("estimateTokens", () => {
  it("counts text and gives each image a flat cost instead of its bytes", () => {
    const screenshot = new Uint8Array(400_000);
    const withImage = message(USER, text("abcd".repeat(10)), result("c1", text("ok"), image(1)), new LanguageModelDataPart(screenshot, "image/png"));
    assert.equal(estimateTokens("abcdefgh"), 2);
    assert.equal(estimateTokens(withImage), 11 + 2000);
  });
});

describe("Responses", () => {
  it("sends the whole conversation statelessly", () => {
    assert.deepEqual(responsesRequest(turn), {
      model: "m",
      stream: true,
      store: false,
      input: [
        { role: "system", content: "Be brief." },
        { role: "user", content: [{ type: "input_text", text: "Weather?" }, { type: "input_image", image_url: "data:image/png;base64,Bw==", detail: "auto" }] },
        { role: "assistant", content: "Checking." },
        { type: "function_call", call_id: "c1", name: "get_weather", arguments: '{"city":"Paris"}' },
        { type: "function_call_output", call_id: "c1", output: "Sunny" },
      ],
      tools: [{ type: "function", name: "get_weather", description: "Weather for a city", parameters: weatherTool.inputSchema, strict: false }],
      tool_choice: "required",
    });
  });

  it("streams text and each finished function call", async () => {
    const replies = await collect(
      responsesReplies(
        events(
          { type: "response.output_text.delta", delta: "Checking." },
          { type: "response.function_call_arguments.delta", delta: '{"city"' },
          { type: "response.output_item.done", item: { type: "function_call", call_id: "c1", name: "get_weather", arguments: '{"city":"Paris"}' } },
          { type: "response.completed", response: {} },
        ),
      ),
    );
    assert.deepEqual(replies, [new LanguageModelTextPart("Checking."), new LanguageModelToolCallPart("c1", "get_weather", { city: "Paris" })]);
  });

  it("fails on a token-limit stop and on a failed response", async () => {
    await rejectsWith(
      collect(responsesReplies(events({ type: "response.incomplete", response: { incomplete_details: { reason: "max_output_tokens" } } }))),
      "output token limit",
    );
    await rejectsWith(collect(responsesReplies(events({ type: "response.failed", response: { error: { message: "overloaded" } } }))), "overloaded");
  });
});
