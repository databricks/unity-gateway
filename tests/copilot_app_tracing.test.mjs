import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  MAX_BUFFERED_SPANS,
  MAX_SPANS_PER_EXPORT,
  SERVICE_NAME,
  createExporter,
  createSpanBuilder,
  createTracer,
  exportBody,
} from "../src/ucode/agents/desktop/copilot_app_plugin/app-tracing.mjs";

const ENDPOINT = "https://workspace.example/ai-gateway/otel/v1/traces";

function fixture(name) {
  return JSON.parse(readFileSync(new URL(`./fixtures/copilot_app_events/${name}.json`, import.meta.url), "utf8"));
}

function sequentialIds() {
  let traces = 0;
  let spans = 0;
  return {
    trace: () => `trace${++traces}`.padStart(32, "0"),
    span: () => `span${++spans}`.padStart(16, "0"),
  };
}

function build(name) {
  const { sessionId, events } = fixture(name);
  const spans = [];
  const builder = createSpanBuilder({
    sessionId,
    serverAddress: "workspace.example",
    emit: (span) => spans.push(span),
    ids: sequentialIds(),
  });
  for (const event of events) builder.handle(event);
  return { sessionId, events, spans };
}

function attrs(span) {
  return Object.fromEntries(
    span.attributes.map(({ key, value }) => {
      if ("intValue" in value) return [key, Number(value.intValue)];
      if ("arrayValue" in value) return [key, value.arrayValue.values.map((item) => item.stringValue)];
      return [key, Object.values(value)[0]];
    }),
  );
}

const nanos = (timestamp) => `${Date.parse(timestamp)}000000`;
const at = (events, type, index = 0) => events.filter((event) => event.type === type)[index].timestamp;

test("a Claude turn with a tool call becomes invoke_agent with chat and execute_tool children", () => {
  const { sessionId, events, spans } = build("claude-tool");

  assert.deepEqual(
    spans.map((span) => span.name),
    [
      "chat system.ai.claude-sonnet-4-6",
      "execute_tool bash",
      "chat system.ai.claude-sonnet-4-6",
      "invoke_agent",
    ],
  );
  const [firstChat, tool, secondChat, agent] = spans;
  for (const span of [firstChat, tool, secondChat]) {
    assert.equal(span.traceId, agent.traceId);
    assert.equal(span.parentSpanId, agent.spanId);
  }
  assert.equal(agent.parentSpanId, undefined);
  assert.equal(new Set(spans.map((span) => span.spanId)).size, 4);
  assert.deepEqual([agent.kind, firstChat.kind, tool.kind], [1, 3, 1]);

  assert.equal(agent.startTimeUnixNano, nanos(at(events, "user.message")));
  assert.equal(agent.endTimeUnixNano, nanos(at(events, "session.idle")));
  assert.equal(firstChat.startTimeUnixNano, nanos(at(events, "model.call_start")));
  assert.equal(firstChat.endTimeUnixNano, nanos(at(events, "model.call_finished")));
  assert.equal(tool.startTimeUnixNano, nanos(at(events, "tool.execution_start")));
  assert.equal(tool.endTimeUnixNano, nanos(at(events, "tool.execution_complete")));

  assert.deepEqual(attrs(firstChat), {
    "gen_ai.conversation.id": sessionId,
    "gen_ai.operation.name": "chat",
    "gen_ai.provider.name": "anthropic",
    "gen_ai.system": "anthropic",
    "gen_ai.request.model": "system.ai.claude-sonnet-4-6",
    "gen_ai.response.model": "system.ai.claude-sonnet-4-6",
    "gen_ai.response.id": "msg_bdrk_qfxgghw263ampexydp7ett746kinqso5t6c2kb7il2ewvq2hbx6q",
    "gen_ai.response.finish_reasons": ["tool_calls"],
    "gen_ai.request.reasoning.level": "medium",
    "gen_ai.usage.input_tokens": 15964,
    "gen_ai.usage.output_tokens": 96,
    "gen_ai.usage.cache_write.input_tokens": 15961,
    "gen_ai.usage.reasoning.output_tokens": 15,
    "gen_ai.response.time_to_first_chunk": 1.5987543309999999,
    "github.copilot.initiator": "user",
    "github.copilot.turn_id": "0",
    "github.copilot.interaction_id": "06593fe5-4bae-4a87-b148-59915b34520b",
    "server.address": "workspace.example",
  });
  assert.equal(attrs(secondChat)["github.copilot.turn_id"], "1");
  assert.equal(attrs(secondChat)["gen_ai.usage.cache_read.input_tokens"], 15961);
  assert.deepEqual(attrs(tool), {
    "gen_ai.conversation.id": sessionId,
    "gen_ai.operation.name": "execute_tool",
    "gen_ai.tool.name": "bash",
    "gen_ai.tool.call.id": "toolu_bdrk_01TznywxMj1wW21uRrmsps7t",
    "gen_ai.tool.type": "function",
    "github.copilot.turn_id": "0",
    "process.exit.code": 0,
  });
  assert.deepEqual(attrs(agent), {
    "gen_ai.operation.name": "invoke_agent",
    "gen_ai.conversation.id": sessionId,
    "github.copilot.interaction_id": "06593fe5-4bae-4a87-b148-59915b34520b",
    "server.address": "workspace.example",
    "gen_ai.provider.name": "anthropic",
    "gen_ai.system": "anthropic",
    "gen_ai.usage.input_tokens": 15964 + 16091,
    "gen_ai.usage.output_tokens": 96 + 8,
    "gen_ai.response.finish_reasons": ["stop"],
    "github.copilot.turn_count": 2,
  });
  for (const span of spans) assert.deepEqual(span.status, { code: 1 });
});

test("GPT and GLM turns carry their request and response models", () => {
  const gpt = build("gpt").spans;
  assert.deepEqual(gpt.map((span) => span.name), ["chat system.ai.gpt-6-luna", "invoke_agent"]);
  assert.equal(attrs(gpt[0])["gen_ai.provider.name"], "openai");
  assert.equal(attrs(gpt[0])["gen_ai.usage.input_tokens"], 13013);
  assert.equal(attrs(gpt[0])["gen_ai.usage.output_tokens"], 5);

  const glm = build("glm").spans;
  assert.equal(attrs(glm[0])["gen_ai.request.model"], "system.ai.glm-5-2");
  assert.equal(attrs(glm[0])["gen_ai.response.model"], "glm-5-2");
  assert.equal(attrs(glm[0])["gen_ai.request.reasoning.level"], undefined);
  assert.equal(attrs(glm[1])["github.copilot.turn_count"], 1);
});

test("a failed model call marks the chat and the turn as errors", () => {
  const { spans } = build("error");

  const [chat, agent] = spans;
  assert.deepEqual(chat.status, { code: 2, message: "Model call failed" });
  assert.equal(attrs(chat)["error.type"], "404");
  assert.equal(attrs(chat)["http.response.status_code"], 404);
  assert.deepEqual(attrs(chat)["gen_ai.response.finish_reasons"], ["error"]);
  assert.equal(attrs(chat)["gen_ai.provider.name"], "openai");
  assert.deepEqual(agent.status, { code: 2, message: "Session error" });
  assert.equal(attrs(agent)["error.type"], "query");
  assert.deepEqual(attrs(agent)["gen_ai.response.finish_reasons"], ["error"]);
});

test("no prompt, response, tool argument or error text reaches a span", () => {
  for (const name of ["claude-tool", "gpt", "glm", "error"]) {
    const body = JSON.stringify(exportBody(build(name).spans));
    assert.ok(!body.includes("<content>"), name);
  }
});

test("a failed tool call is an error span", () => {
  const spans = [];
  const builder = createSpanBuilder({ sessionId: "s", serverAddress: "h", emit: (span) => spans.push(span) });
  const ts = "2026-01-01T00:00:00.000Z";
  builder.handle({ type: "user.message", timestamp: ts, data: { interactionId: "i" } });
  builder.handle({ type: "tool.execution_start", timestamp: ts, data: { toolCallId: "t1", toolName: "view" } });
  builder.handle({
    type: "tool.execution_complete",
    timestamp: ts,
    data: { toolCallId: "t1", success: false, error: { code: "denied", message: "<content>" } },
  });

  assert.deepEqual(spans[0].status, { code: 2, message: "Tool call failed" });
  assert.equal(attrs(spans[0])["error.type"], "denied");
});

test("spans still open at shutdown end there", () => {
  const spans = [];
  const builder = createSpanBuilder({ sessionId: "s", serverAddress: "h", emit: (span) => spans.push(span) });
  builder.handle({ type: "user.message", timestamp: "2026-01-01T00:00:00.000Z", data: {} });
  builder.handle({ type: "model.call_start", timestamp: "2026-01-01T00:00:01.000Z", data: { turnId: "0", model: "m" } });
  builder.handle({ type: "session.shutdown", timestamp: "2026-01-01T00:00:02.000Z", data: {} });

  assert.deepEqual(spans.map((span) => span.name), ["chat m", "invoke_agent"]);
  assert.ok(spans.every((span) => span.endTimeUnixNano === "1767225602000000000"));
});

test("a turn the extension joined midway still gets a parent span", () => {
  const spans = [];
  const builder = createSpanBuilder({ sessionId: "s", serverAddress: "h", emit: (span) => spans.push(span) });
  const ts = "2026-01-01T00:00:00.000Z";
  builder.handle({ type: "model.call_start", timestamp: ts, data: { turnId: "3", model: "m" } });
  builder.handle({ type: "model.call_finished", timestamp: ts, data: { turnId: "3", outcome: "success" } });
  builder.handle({ type: "session.idle", timestamp: ts, data: {} });

  assert.equal(spans[0].parentSpanId, spans[1].spanId);
});

test("export body is OTLP/JSON under Copilot's service name", () => {
  const body = exportBody([{ name: "x" }]);
  assert.deepEqual(body.resourceSpans[0].resource.attributes, [
    { key: "service.name", value: { stringValue: SERVICE_NAME } },
  ]);
  assert.equal(SERVICE_NAME, "github-copilot");
  assert.deepEqual(body.resourceSpans[0].scopeSpans[0].spans, [{ name: "x" }]);
});

function tokenSource(values) {
  const token = async () => values[Math.min(token.calls++, values.length - 1)];
  token.calls = 0;
  token.invalidated = 0;
  token.invalidate = () => token.invalidated++;
  return token;
}

function fakeFetch(statuses) {
  const requests = [];
  const fetchImpl = async (url, init) => {
    requests.push({ url, ...init, body: JSON.parse(init.body) });
    const status = statuses[Math.min(requests.length - 1, statuses.length - 1)];
    if (status instanceof Error) throw status;
    return new Response(null, { status });
  };
  return { fetchImpl, requests };
}

const exporterWith = (statuses, token = tokenSource(["t1", "t2"]), logs = []) => {
  const { fetchImpl, requests } = fakeFetch(statuses);
  const exporter = createExporter({
    endpoint: ENDPOINT,
    headers: { "X-Databricks-Workspace-Id": "123", "x-admin": "a" },
    token,
    log: (message) => logs.push(message),
    fetchImpl,
    wait: async () => {},
  });
  return { exporter, requests, token, logs };
};

test("flush posts the buffered spans with the token and the configured headers", async () => {
  const { exporter, requests } = exporterWith([200]);
  exporter.add({ name: "a" });
  exporter.add({ name: "b" });

  await exporter.flush();

  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, ENDPOINT);
  assert.equal(requests[0].method, "POST");
  assert.deepEqual(requests[0].headers, {
    "X-Databricks-Workspace-Id": "123",
    "x-admin": "a",
    Authorization: "Bearer t1",
    "Content-Type": "application/json",
  });
  assert.deepEqual(requests[0].body.resourceSpans[0].scopeSpans[0].spans, [{ name: "a" }, { name: "b" }]);
  assert.equal(exporter.pending, 0);
});

test("a 401 refreshes the token once and resends", async () => {
  const { exporter, requests, token, logs } = exporterWith([401, 200]);
  exporter.add({ name: "a" });

  await exporter.flush();

  assert.equal(token.invalidated, 1);
  assert.deepEqual(
    requests.map((request) => request.headers.Authorization),
    ["Bearer t1", "Bearer t2"],
  );
  assert.deepEqual(logs, []);
});

test("a server error is retried a bounded number of times, then dropped and logged", async () => {
  const { exporter, requests, logs } = exporterWith([503]);
  exporter.add({ name: "a" });

  await exporter.flush();

  assert.equal(requests.length, 3);
  assert.equal(exporter.pending, 0);
  assert.deepEqual(logs, ["Databricks tracing: dropped 1 spans (HTTP 503)."]);
});

test("a rejected export isn't retried", async () => {
  const { exporter, requests, logs } = exporterWith([403]);
  exporter.add({ name: "a" });

  await exporter.flush();

  assert.equal(requests.length, 1);
  assert.match(logs[0], /HTTP 403/);
});

test("an export that may have been written is never sent again", async () => {
  for (const outcome of [500, new Error("The operation was aborted due to timeout")]) {
    const { exporter, requests, logs } = exporterWith([outcome, 200]);
    exporter.add({ name: "a" });
    await exporter.flush();
    assert.equal(requests.length, 1);
    assert.match(logs[0], /dropped 1 spans/);
  }
});

test("a network error or a failing token never throws", async () => {
  const network = exporterWith([new Error("connect ECONNREFUSED")]);
  network.exporter.add({ name: "a" });
  await network.exporter.flush();
  assert.match(network.logs[0], /ECONNREFUSED/);

  const failing = async () => {
    throw new Error("ug auth-token failed: not signed in");
  };
  failing.invalidate = () => {};
  const signedOut = exporterWith([200], failing);
  signedOut.exporter.add({ name: "a" });
  await signedOut.exporter.flush();
  assert.equal(signedOut.requests.length, 0);
  assert.match(signedOut.logs[0], /not signed in/);
});

test("the buffer is bounded and large batches go out in parts", async () => {
  const { exporter, requests, logs } = exporterWith([200]);
  for (let i = 0; i < MAX_BUFFERED_SPANS + 5; i++) exporter.add({ name: String(i) });
  assert.equal(exporter.pending, MAX_BUFFERED_SPANS);

  await exporter.flush();

  const sent = requests.flatMap((request) => request.body.resourceSpans[0].scopeSpans[0].spans);
  assert.equal(sent.length, MAX_BUFFERED_SPANS);
  assert.equal(sent[0].name, "5");
  assert.ok(requests.every((request) => request.body.resourceSpans[0].scopeSpans[0].spans.length <= MAX_SPANS_PER_EXPORT));
  assert.match(logs[0], /dropped 5 spans/);
});

test("the tracer is off without a tracing config", () => {
  assert.equal(createTracer({}, tokenSource(["t"]), { log: () => {} }), null);
  assert.equal(createTracer({ tracing: null }, tokenSource(["t"]), { log: () => {} }), null);
});

test("the tracer exports a turn at idle with the session id header", async () => {
  const { sessionId, events } = fixture("gpt");
  const { fetchImpl, requests } = fakeFetch([200]);
  const tracer = createTracer(
    { tracing: { endpoint: ENDPOINT, headers: { "x-admin": "a" } } },
    tokenSource(["t"]),
    { sessionId, log: () => {}, fetchImpl },
  );

  for (const event of events) tracer.handle(event);
  await tracer.shutdown();

  const sent = requests.flatMap((request) => request.body.resourceSpans[0].scopeSpans[0].spans);
  assert.deepEqual(sent.map((span) => span.name).sort(), ["chat system.ai.gpt-6-luna", "invoke_agent"]);
  assert.equal(requests[0].headers["session-id"], sessionId);
  assert.equal(requests[0].headers["x-admin"], "a");
});

test("a malformed event never throws out of the tracer", () => {
  const logs = [];
  const tracer = createTracer({ tracing: { endpoint: ENDPOINT, headers: {} } }, tokenSource(["t"]), {
    sessionId: "s",
    log: (message) => logs.push(message),
    fetchImpl: async () => new Response(null, { status: 200 }),
  });

  tracer.handle(null);
  tracer.handle({ type: "assistant.usage" });
  tracer.handle({ type: "model.call_start", data: null, timestamp: "not a time" });
  tracer.handle({
    type: "assistant.usage",
    get data() {
      throw new Error("boom");
    },
  });

  assert.deepEqual(logs, ["Databricks tracing: skipped an event: boom"]);
});

function liveTracer(sessionId) {
  const { fetchImpl, requests } = fakeFetch([200]);
  const tracer = createTracer({ tracing: { endpoint: ENDPOINT, headers: {} } }, tokenSource(["t"]), {
    sessionId,
    log: () => {},
    fetchImpl,
  });
  const sent = () => requests.flatMap((request) => request.body.resourceSpans[0].scopeSpans[0].spans);
  return { tracer, sent };
}

test("a new chat's first turn, which started before the extension joined, comes from its history", async () => {
  const { sessionId, events } = fixture("claude-tool");
  const firstLive = events.findIndex((event) => event.type === "assistant.usage");
  const persisted = events.slice(0, firstLive).filter((event) => event.ephemeral !== true);
  const { tracer, sent } = liveTracer(sessionId);

  const replay = tracer.replay(async () => persisted);
  for (const event of events.slice(firstLive)) tracer.handle(event);
  await replay;
  await tracer.shutdown();

  const spans = sent();
  const agent = spans.find((span) => span.name === "invoke_agent");
  assert.equal(agent.startTimeUnixNano, nanos(at(events, "user.message")));
  assert.deepEqual(
    spans.map((span) => span.name).sort(),
    ["chat system.ai.claude-sonnet-4-6", "chat system.ai.claude-sonnet-4-6", "execute_tool bash", "invoke_agent"],
  );
  assert.equal(new Set(spans.map((span) => span.spanId)).size, spans.length);
  assert.ok(spans.every((span) => span.traceId === agent.traceId));
  const firstChat = spans.find((span) => attrs(span)["gen_ai.response.id"]?.startsWith("msg_bdrk_qfx"));
  const usage = events.find((event) => event.type === "assistant.usage");
  assert.equal(firstChat.startTimeUnixNano, nanos(new Date(Date.parse(usage.timestamp) - usage.data.duration).toISOString()));
  assert.equal(firstChat.endTimeUnixNano, nanos(usage.timestamp));
});

test("a resumed chat's finished turns aren't sent again", async () => {
  const { sessionId, events } = fixture("gpt");
  const persisted = events.filter((event) => event.ephemeral !== true);
  const { tracer, sent } = liveTracer(sessionId);

  await tracer.replay(async () => persisted);
  tracer.handle({ type: "session.idle", timestamp: "2026-11-20T23:00:00.000Z", data: {} });
  await tracer.shutdown();
  assert.deepEqual(sent(), []);

  const next = liveTracer(sessionId);
  await next.tracer.replay(async () => persisted);
  for (const event of fixture("glm").events) next.tracer.handle(event);
  await next.tracer.shutdown();
  assert.deepEqual(next.sent().map((span) => span.name), ["chat system.ai.glm-5-2", "invoke_agent"]);
});

test("an unreadable history leaves the live events traced", async () => {
  const { sessionId, events } = fixture("gpt");
  const { tracer, sent } = liveTracer(sessionId);

  await tracer.replay(async () => {
    throw new Error("disconnected");
  });
  for (const event of events) tracer.handle(event);
  await tracer.shutdown();

  assert.equal(sent().length, 2);
});
