import { randomBytes } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";

import { withSessionId } from "./ug-plugin.mjs";

// The runtime's own exporter reports this service name, so both kinds of spans land together.
export const SERVICE_NAME = "github-copilot";
export const SCOPE_NAME = "unity-gateway.copilot-app";
export const MAX_BUFFERED_SPANS = 2_000;
export const MAX_SPANS_PER_EXPORT = 500;
export const RETRY_DELAYS_MS = [1_000, 4_000];
export const EXPORT_TIMEOUT_MS = 15_000;

const SPAN_KIND_INTERNAL = 1;
const SPAN_KIND_CLIENT = 3;
const STATUS_OK = 1;
const STATUS_ERROR = 2;

const hexId = (bytes) => randomBytes(bytes).toString("hex");
export const newIds = { trace: () => hexId(16), span: () => hexId(8) };

function unixNano(timestamp, now) {
  const ms = Date.parse(timestamp);
  return `${BigInt(Math.round(Number.isFinite(ms) ? ms : now()))}000000`;
}

function attribute(key, value) {
  if (typeof value === "string") return { key, value: { stringValue: value } };
  if (typeof value === "boolean") return { key, value: { boolValue: value } };
  if (typeof value === "number") {
    return Number.isInteger(value)
      ? { key, value: { intValue: String(value) } }
      : { key, value: { doubleValue: value } };
  }
  if (Array.isArray(value)) {
    return { key, value: { arrayValue: { values: value.map((item) => attribute(key, item).value) } } };
  }
  return null;
}

function attributes(values) {
  return Object.entries(values)
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([key, value]) => attribute(key, value))
    .filter(Boolean);
}

const positive = (value) => (typeof value === "number" && value > 0 ? value : undefined);
const text = (value) => (typeof value === "string" && value ? value : undefined);

// Live events that show the replayed interaction is the one still running. An idle or a new user
// message instead means it ended before this process joined, and another process may have sent it.
const CONTINUES_INTERACTION = new Set([
  "assistant.turn_start",
  "assistant.turn_end",
  "model.call_start",
  "model.call_failure",
  "model.call_finished",
  "assistant.usage",
  "tool.execution_start",
  "tool.execution_complete",
]);

// Maps the session's events to OTel GenAI spans: one `invoke_agent` per user message, with a
// `chat <model>` per model call and an `execute_tool <name>` per tool call as its children. Only
// metadata is read; prompts, responses, tool arguments and results never reach a span.
//
// Events replayed from the session's history are held until a live event shows their interaction
// is still running, so a resumed chat's finished turns aren't sent twice.
export function createSpanBuilder({ sessionId, serverAddress, emit: send, ids = newIds, now = Date.now }) {
  let interaction = null;
  const chats = new Map();
  const tools = new Map();
  let held = null;
  const emit = (span) => (held ? held.push(span) : send(span));

  function reset() {
    interaction = null;
    chats.clear();
    tools.clear();
    held = null;
  }

  const span = (open, endTimestamp) => ({
    traceId: open.traceId,
    spanId: open.spanId,
    ...(open.parentSpanId ? { parentSpanId: open.parentSpanId } : {}),
    name: open.name,
    kind: open.kind,
    startTimeUnixNano: open.start,
    endTimeUnixNano: unixNano(endTimestamp, now),
    attributes: attributes(open.attributes),
    status: open.error ? { code: STATUS_ERROR, message: open.error.message } : { code: STATUS_OK },
  });

  const fail = (open, message, type) => {
    open.error = { message };
    open.attributes["error.type"] = type;
  };

  function startInteraction(event) {
    const data = event.data ?? {};
    interaction = {
      traceId: ids.trace(),
      spanId: ids.span(),
      name: "invoke_agent",
      kind: SPAN_KIND_INTERNAL,
      start: unixNano(event.timestamp, now),
      attributes: {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.conversation.id": sessionId,
        "github.copilot.interaction_id": text(data.interactionId),
        "server.address": serverAddress,
      },
      inputTokens: 0,
      outputTokens: 0,
      calls: 0,
      finishReason: undefined,
      provider: undefined,
    };
    return interaction;
  }

  const current = (event) => interaction ?? startInteraction(event);

  function child(event, name, kind, extra) {
    const parent = current(event);
    return {
      traceId: parent.traceId,
      spanId: ids.span(),
      parentSpanId: parent.spanId,
      name,
      kind,
      start: unixNano(event.timestamp, now),
      attributes: { "gen_ai.conversation.id": sessionId, ...extra },
    };
  }

  const chatKey = (event, turnId) => `${event.agentId ?? ""}\u0000${turnId ?? ""}`;

  function latestChat(event) {
    const prefix = `${event.agentId ?? ""}\u0000`;
    let found = null;
    for (const [key, open] of chats) if (key.startsWith(prefix)) found = open;
    return found;
  }

  // The first model call of a new chat can start before the extension joins; its usage still
  // carries the call's measured duration.
  function chatFromUsage(event) {
    const { model, duration } = event.data ?? {};
    const end = Date.parse(event.timestamp);
    if (!text(model) || typeof duration !== "number" || !Number.isFinite(end)) return null;
    const key = chatKey(event, "usage");
    const open = child({ ...event, timestamp: new Date(end - duration).toISOString() }, `chat ${model}`, SPAN_KIND_CLIENT, {
      "gen_ai.operation.name": "chat",
      "gen_ai.request.model": model,
      "github.copilot.interaction_id": interaction?.attributes["github.copilot.interaction_id"],
      "server.address": serverAddress,
    });
    chats.set(key, open);
    return { key, open };
  }

  function endChat(key, open, timestamp) {
    chats.delete(key);
    const parent = interaction;
    if (parent && parent.traceId === open.traceId) {
      parent.calls += 1;
      parent.inputTokens += open.attributes["gen_ai.usage.input_tokens"] ?? 0;
      parent.outputTokens += open.attributes["gen_ai.usage.output_tokens"] ?? 0;
      parent.finishReason = open.attributes["gen_ai.response.finish_reasons"]?.[0] ?? parent.finishReason;
      parent.provider = open.attributes["gen_ai.provider.name"] ?? parent.provider;
    }
    emit(span(open, timestamp));
  }

  function endInteraction(timestamp) {
    for (const [key, open] of chats) endChat(key, open, timestamp);
    for (const [key, open] of tools) {
      tools.delete(key);
      emit(span(open, timestamp));
    }
    if (!interaction) return;
    const open = interaction;
    interaction = null;
    Object.assign(open.attributes, {
      "gen_ai.provider.name": open.provider,
      "gen_ai.system": open.provider,
      "gen_ai.usage.input_tokens": open.inputTokens,
      "gen_ai.usage.output_tokens": open.outputTokens,
      "gen_ai.response.finish_reasons": open.finishReason ? [open.finishReason] : undefined,
      "github.copilot.turn_count": open.calls,
    });
    emit(span(open, timestamp));
  }

  const handlers = {
    "user.message": (event) => {
      if (interaction) endInteraction(event.timestamp);
      startInteraction(event);
    },
    "assistant.turn_start": (event) => {
      const open = current(event);
      open.attributes["github.copilot.interaction_id"] ??= text(event.data?.interactionId);
    },
    "model.call_start": (event) => {
      const { turnId, model } = event.data ?? {};
      chats.set(
        chatKey(event, turnId),
        child(event, text(model) ? `chat ${model}` : "chat", SPAN_KIND_CLIENT, {
          "gen_ai.operation.name": "chat",
          "gen_ai.request.model": text(model),
          "github.copilot.turn_id": text(turnId),
          "github.copilot.interaction_id": interaction?.attributes["github.copilot.interaction_id"],
          "server.address": serverAddress,
        }),
      );
    },
    "assistant.usage": (event) => {
      const started = latestChat(event);
      const created = started ? null : chatFromUsage(event);
      const open = started ?? created?.open;
      if (!open) return;
      const data = event.data ?? {};
      const provider = text(data.providerType);
      Object.assign(open.attributes, {
        "gen_ai.provider.name": provider,
        "gen_ai.system": provider,
        "gen_ai.request.model": open.attributes["gen_ai.request.model"] ?? text(data.model),
        "gen_ai.response.model": text(data.responseModel),
        "gen_ai.response.id": text(data.apiCallId),
        "gen_ai.response.finish_reasons": text(data.finishReason) ? [data.finishReason] : undefined,
        "gen_ai.request.reasoning.level": text(data.reasoningEffort),
        "gen_ai.usage.input_tokens": typeof data.inputTokens === "number" ? data.inputTokens : undefined,
        "gen_ai.usage.output_tokens": typeof data.outputTokens === "number" ? data.outputTokens : undefined,
        "gen_ai.usage.cache_read.input_tokens": positive(data.cacheReadTokens),
        "gen_ai.usage.cache_write.input_tokens": positive(data.cacheWriteTokens),
        "gen_ai.usage.reasoning.output_tokens": positive(data.reasoningTokens),
        "gen_ai.response.time_to_first_chunk":
          typeof data.timeToFirstTokenMs === "number" ? data.timeToFirstTokenMs / 1000 : undefined,
        "github.copilot.initiator": text(data.initiator),
      });
      if (created) endChat(created.key, created.open, event.timestamp);
    },
    "model.call_failure": (event) => {
      const open = latestChat(event);
      if (!open) return;
      const { statusCode, failureKind, providerType } = event.data ?? {};
      const provider = text(providerType);
      open.attributes["gen_ai.provider.name"] ??= provider;
      open.attributes["gen_ai.system"] ??= provider;
      if (typeof statusCode === "number") open.attributes["http.response.status_code"] = statusCode;
      open.attributes["gen_ai.response.finish_reasons"] = ["error"];
      fail(open, "Model call failed", typeof statusCode === "number" ? String(statusCode) : text(failureKind) ?? "_OTHER");
    },
    "model.call_finished": (event) => {
      const key = chatKey(event, event.data?.turnId);
      const open = chats.get(key);
      if (!open) return;
      if (event.data?.outcome === "error" && !open.error) fail(open, "Model call failed", "_OTHER");
      endChat(key, open, event.timestamp);
    },
    "tool.execution_start": (event) => {
      const { toolCallId, toolName } = event.data ?? {};
      if (!text(toolCallId)) return;
      const name = text(toolName);
      tools.set(
        toolCallId,
        child(event, name ? `execute_tool ${name}` : "execute_tool", SPAN_KIND_INTERNAL, {
          "gen_ai.operation.name": "execute_tool",
          "gen_ai.tool.name": name,
          "gen_ai.tool.call.id": toolCallId,
          "gen_ai.tool.type": "function",
          "github.copilot.turn_id": text(event.data?.turnId),
        }),
      );
    },
    "tool.execution_complete": (event) => {
      const data = event.data ?? {};
      const open = tools.get(data.toolCallId);
      if (!open) return;
      tools.delete(data.toolCallId);
      const exitCode = data.shellExecution?.exitCode;
      if (typeof exitCode === "number") open.attributes["process.exit.code"] = exitCode;
      if (data.success === false) fail(open, "Tool call failed", text(data.error?.code) ?? "_OTHER");
      emit(span(open, event.timestamp));
    },
    "session.error": (event) => {
      if (!interaction) return;
      fail(interaction, "Session error", text(event.data?.errorType) ?? "_OTHER");
    },
    "session.idle": (event) => endInteraction(event.timestamp),
    "session.shutdown": (event) => endInteraction(event.timestamp),
  };

  return {
    handle(event) {
      const handler = handlers[event?.type];
      if (!handler) return;
      if (held) {
        if (CONTINUES_INTERACTION.has(event.type)) {
          const spans = held;
          held = null;
          for (const span of spans) send(span);
        } else {
          reset();
        }
      }
      handler(event);
    },
    replay(events) {
      reset();
      held = [];
      for (const event of events) handlers[event?.type]?.(event);
    },
    end(timestamp = new Date(now()).toISOString()) {
      if (held) reset();
      endInteraction(timestamp);
    },
  };
}

export function exportBody(spans) {
  return {
    resourceSpans: [
      {
        resource: { attributes: attributes({ "service.name": SERVICE_NAME }) },
        scopeSpans: [{ scope: { name: SCOPE_NAME }, spans }],
      },
    ],
  };
}

const RETRYABLE_STATUSES = new Set([408, 429, 502, 503, 504]);

// Spans wait in memory, at most MAX_BUFFERED_SPANS, and go out in one export at a time. An export
// that keeps failing is dropped and logged; nothing here throws or holds up a turn.
export function createExporter({
  endpoint,
  headers,
  token,
  log,
  fetchImpl = fetch,
  retryDelaysMs = RETRY_DELAYS_MS,
  wait = delay,
  timeoutMs = EXPORT_TIMEOUT_MS,
}) {
  const queue = [];
  let dropped = 0;
  let running = null;

  async function post(body) {
    const send = async (value) =>
      fetchImpl(endpoint, {
        method: "POST",
        headers: { ...headers, Authorization: `Bearer ${value}`, "Content-Type": "application/json" },
        body,
        signal: AbortSignal.timeout(timeoutMs),
      });
    let response = await send(await token());
    if (response.status === 401) {
      await response.body?.cancel();
      token.invalidate();
      response = await send(await token());
    }
    await response.body?.cancel();
    return response.status;
  }

  // Only an export the gateway clearly turned away is sent again: the table keeps every copy of a
  // span, and a timeout or dropped connection may follow a write.
  async function sendBatch(spans) {
    const body = JSON.stringify(exportBody(spans));
    let problem = "";
    for (let attempt = 0; attempt <= retryDelaysMs.length; attempt++) {
      if (attempt > 0) await wait(retryDelaysMs[attempt - 1]);
      let status;
      try {
        status = await post(body);
      } catch (error) {
        problem = error?.message ?? String(error);
        break;
      }
      if (status >= 200 && status < 300) return;
      problem = `HTTP ${status}`;
      if (!RETRYABLE_STATUSES.has(status)) break;
    }
    log(`Databricks tracing: dropped ${spans.length} spans (${problem}).`);
  }

  async function drain() {
    while (queue.length > 0) {
      if (dropped > 0) {
        log(`Databricks tracing: dropped ${dropped} spans that didn't fit in the buffer.`);
        dropped = 0;
      }
      await sendBatch(queue.splice(0, MAX_SPANS_PER_EXPORT));
    }
  }

  return {
    add(span) {
      queue.push(span);
      if (queue.length > MAX_BUFFERED_SPANS) {
        queue.shift();
        dropped++;
      }
    },
    flush() {
      running ??= drain()
        .catch((error) => log(`Databricks tracing: export failed: ${error?.message ?? error}`))
        .finally(() => {
          running = null;
        });
      return running;
    },
    get pending() {
      return queue.length;
    },
  };
}

const FLUSH_EVENTS = new Set(["assistant.turn_end", "session.idle", "session.shutdown"]);

export function createTracer(config, token, { sessionId = process.env.SESSION_ID, log, fetchImpl, ids, now } = {}) {
  const tracing = config.tracing;
  if (!tracing?.endpoint) return null;
  let serverAddress;
  try {
    serverAddress = new URL(tracing.endpoint).hostname;
  } catch {
    return null;
  }
  const exporter = createExporter({
    endpoint: tracing.endpoint,
    headers: withSessionId(tracing.headers, sessionId),
    token,
    log,
    ...(fetchImpl ? { fetchImpl } : {}),
  });
  const builder = createSpanBuilder({
    sessionId,
    serverAddress,
    emit: (span) => exporter.add(span),
    ...(ids ? { ids } : {}),
    ...(now ? { now } : {}),
  });
  const flush = () => exporter.flush().catch(() => {});
  let waiting = null;
  const replayed = new Set();
  function processEvent(event) {
    try {
      if (event?.id && replayed.has(event.id)) return;
      builder.handle(event);
      if (FLUSH_EVENTS.has(event?.type)) flush();
    } catch (error) {
      log(`Databricks tracing: skipped an event: ${error?.message ?? error}`);
    }
  }
  return {
    handle(event) {
      if (waiting) waiting.push(event);
      else processEvent(event);
    },
    // A new chat's first message starts this process, so its first events come only from the
    // session's history. Live events wait until the history is read.
    async replay(loadHistory) {
      waiting = [];
      try {
        const history = latestInteraction(await loadHistory());
        for (const event of history) if (event?.id) replayed.add(event.id);
        builder.replay(history);
      } catch (error) {
        log(`Databricks tracing: couldn't read the session's earlier events: ${error?.message ?? error}`);
      }
      const live = waiting;
      waiting = null;
      for (const event of live) processEvent(event);
    },
    async shutdown() {
      try {
        builder.end();
      } catch {
        // Ending open spans must not stop the final flush.
      }
      await flush();
      if (exporter.pending > 0) await flush();
    },
    exporter,
  };
}

export function latestInteraction(events) {
  if (!Array.isArray(events)) return [];
  let start = -1;
  events.forEach((event, index) => {
    if (event?.type === "user.message") start = index;
  });
  return start < 0 ? [] : events.slice(start);
}
