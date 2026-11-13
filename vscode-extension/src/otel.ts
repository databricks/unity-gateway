import { timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { Tokens } from "./auth";

/** The loopback port and path secret `ug configure` picked for this machine's trace receiver. */
export interface OtelSetting {
  readonly port: number;
  readonly secret: string;
}

export const otelEndpoint = ({ port, secret }: OtelSetting): string => `http://127.0.0.1:${port}/${secret}`;

/** The settings that point both of Copilot's VS Code agents at the receiver. */
export const otelSettings = (endpoint: string): Record<string, unknown> => ({
  "github.copilot.chat.otel.enabled": true,
  "github.copilot.chat.otel.exporterType": "otlp-http",
  // The gateway takes protobuf. The Copilot CLI agent's protocol is policy-only, so it sends JSON.
  "github.copilot.chat.otel.protocol": "http/protobuf",
  "github.copilot.chat.otel.otlpEndpoint": endpoint,
  "chat.agentHost.otel.enabled": true,
  "chat.agentHost.otel.exporterType": "otlp-http",
  "chat.agentHost.otel.otlpEndpoint": endpoint,
});

const readBody = (request: IncomingMessage): Promise<Buffer> =>
  new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => resolve(Buffer.concat(chunks)));
    request.on("error", reject);
  });

const matches = (presented: string, secret: string) =>
  presented.length === secret.length && timingSafeEqual(Buffer.from(presented), Buffer.from(secret));

/**
 * A loopback OTLP receiver: traces go to the workspace's Unity Gateway with a fresh token and `headers`, and logs and
 * metrics, which the gateway doesn't take, get an empty success.
 */
export const startReceiver = (
  otel: OtelSetting,
  workspace: string,
  tokens: Pick<Tokens, "get" | "renew">,
  headers: () => Record<string, string>,
  log: (line: string) => void,
): Server => {
  const handle = async (request: IncomingMessage, response: ServerResponse) => {
    const [path = ""] = (request.url ?? "").split("?");
    const [, secret = "", version, signal] = path.split("/");
    if (request.method !== "POST" || !matches(secret, otel.secret) || version !== "v1") {
      response.writeHead(404).end();
      return;
    }
    const body = await readBody(request);
    if (signal !== "traces") {
      response.writeHead(200).end();
      return;
    }
    const contentType = request.headers["content-type"] ?? "application/x-protobuf";
    const post = (token: string) =>
      fetch(`${workspace}/ai-gateway/otel/v1/traces`, {
        method: "POST",
        headers: { ...headers(), Authorization: `Bearer ${token}`, "Content-Type": contentType },
        body: new Uint8Array(body),
      });
    let token = await tokens.get();
    let answer = await post(token);
    if (answer.status === 401) {
      await answer.body?.cancel();
      token = await tokens.renew(token);
      answer = await post(token);
    }
    const reply = Buffer.from(await answer.arrayBuffer());
    if (!answer.ok) {
      log(`Unity Gateway refused traces (${answer.status}): ${reply.toString("utf8").slice(0, 300)}`);
    }
    response.writeHead(answer.status, { "Content-Type": answer.headers.get("content-type") ?? contentType }).end(reply);
  };
  return createServer((request, response) => {
    handle(request, response).catch((error: unknown) => {
      log(`Could not forward traces: ${String(error)}`);
      if (!response.headersSent) {
        response.writeHead(502);
      }
      response.end();
    });
  });
};

/** Listens on the receiver's port; while another VS Code window holds it, retries every `retryMs`. */
export const listen = (server: Server, port: number, log: (line: string) => void, retryMs = 30_000): void => {
  server.on("error", (error: NodeJS.ErrnoException) => {
    if (error.code !== "EADDRINUSE") {
      log(`Trace receiver stopped: ${error.message}`);
      return;
    }
    setTimeout(() => server.listen(port, "127.0.0.1"), retryMs).unref();
  });
  server.listen(port, "127.0.0.1");
};
