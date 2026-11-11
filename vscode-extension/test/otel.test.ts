import assert from "node:assert/strict";
import { createServer, request as httpRequest, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, describe, it } from "node:test";
import { listen, otelEndpoint, otelSettings, startReceiver } from "../src/otel";

const SECRET = "s3cret";

const post = (port: number, path: string, body: string, method = "POST") =>
  new Promise<{ status: number; body: string }>((resolve, reject) => {
    const sent = httpRequest({ host: "127.0.0.1", port, path, method, headers: { "Content-Type": "application/x-protobuf" } }, (answer) => {
      const chunks: Buffer[] = [];
      answer.on("data", (chunk: Buffer) => chunks.push(chunk));
      answer.on("end", () => resolve({ status: answer.statusCode ?? 0, body: Buffer.concat(chunks).toString() }));
    });
    sent.on("error", reject);
    sent.end(body);
  });

const realFetch = globalThis.fetch;
const servers: Server[] = [];
afterEach(() => {
  globalThis.fetch = realFetch;
  for (const server of servers.splice(0)) {
    server.close();
  }
});

/** A running receiver whose upstream answers with each of `statuses` in turn. */
const receiver = async (...statuses: number[]) => {
  const upstream: { url: string; headers: Headers; body: string }[] = [];
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    upstream.push({ url, headers: new Headers(init.headers), body: Buffer.from(init.body as Uint8Array).toString() });
    return new Response("ok", { status: statuses[Math.min(upstream.length - 1, statuses.length - 1)] });
  }) as typeof fetch;
  const renewed: string[] = [];
  const tokens = { get: async () => "token-1", renew: async (rejected: string) => (renewed.push(rejected), "token-2") };
  const logs: string[] = [];
  const server = startReceiver({ port: 1, secret: SECRET }, "https://ws", tokens, () => ({ "X-Team": "ml" }), (line) => logs.push(line));
  servers.push(server);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { port: (server.address() as AddressInfo).port, upstream, renewed, logs };
};

describe("otelSettings", () => {
  it("points both of Copilot's agents at the receiver, protobuf for the one that takes it", () => {
    const endpoint = otelEndpoint({ port: 4555, secret: SECRET });
    assert.equal(endpoint, "http://127.0.0.1:4555/s3cret");
    assert.deepEqual(otelSettings(endpoint), {
      "github.copilot.chat.otel.enabled": true,
      "github.copilot.chat.otel.exporterType": "otlp-http",
      "github.copilot.chat.otel.protocol": "http/protobuf",
      "github.copilot.chat.otel.otlpEndpoint": endpoint,
      "chat.agentHost.otel.enabled": true,
      "chat.agentHost.otel.exporterType": "otlp-http",
      "chat.agentHost.otel.otlpEndpoint": endpoint,
    });
  });
});

describe("startReceiver", () => {
  it("forwards traces with a fresh token and the request headers", async () => {
    const { port, upstream } = await receiver(200);

    const answer = await post(port, `/${SECRET}/v1/traces`, "spans");

    assert.equal(answer.status, 200);
    assert.equal(upstream.length, 1);
    assert.equal(upstream[0]?.url, "https://ws/ai-gateway/otel/v1/traces");
    assert.equal(upstream[0]?.headers.get("authorization"), "Bearer token-1");
    assert.equal(upstream[0]?.headers.get("x-team"), "ml");
    assert.equal(upstream[0]?.headers.get("content-type"), "application/x-protobuf");
    assert.equal(upstream[0]?.body, "spans");
  });

  it("renews a rejected token once", async () => {
    const { port, upstream, renewed } = await receiver(401, 200);

    assert.equal((await post(port, `/${SECRET}/v1/traces`, "spans")).status, 200);

    assert.deepEqual(renewed, ["token-1"]);
    assert.deepEqual(upstream.map((sent) => sent.headers.get("authorization")), ["Bearer token-1", "Bearer token-2"]);
  });

  it("passes a refusal back and logs it", async () => {
    const { port, logs } = await receiver(400);

    assert.equal((await post(port, `/${SECRET}/v1/traces`, "{}")).status, 400);

    assert.ok(logs.some((line) => line.includes("AI Gateway refused traces (400)")), logs.join("\n"));
  });

  it("answers logs and metrics itself", async () => {
    const { port, upstream } = await receiver(200);

    for (const signal of ["logs", "metrics"]) {
      assert.equal((await post(port, `/${SECRET}/v1/${signal}`, "x")).status, 200);
    }
    assert.equal(upstream.length, 0);
  });

  it("refuses another secret, path or method", async () => {
    const { port, upstream } = await receiver(200);

    for (const [path, method] of [["/wrong!/v1/traces", "POST"], ["/v1/traces", "POST"], [`/${SECRET}/v2/traces`, "POST"], [`/${SECRET}/v1/traces`, "GET"]]) {
      assert.equal((await post(port, path ?? "", "", method)).status, 404, `${method} ${path}`);
    }
    assert.equal(upstream.length, 0);
  });
});

describe("listen", () => {
  it("takes the port once another window lets it go", async () => {
    const holder = createServer();
    await new Promise<void>((resolve) => holder.listen(0, "127.0.0.1", resolve));
    const port = (holder.address() as AddressInfo).port;
    const server = createServer();
    servers.push(server);

    listen(server, port, () => {}, 20);
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(server.listening, false);
    await new Promise<void>((resolve) => holder.close(() => resolve()));
    await new Promise((resolve) => setTimeout(resolve, 80));

    assert.equal(server.listening, true);
  });
});
