import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import http from "node:http";
import { join } from "node:path";

function encode(text) {
  const payload = Buffer.from(text);
  const length = payload.length;
  let header;
  if (length < 126) header = Buffer.from([0x81, length]);
  else if (length < 65536) {
    header = Buffer.from([0x81, 126, 0, 0]);
    header.writeUInt16BE(length, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x81;
    header[1] = 127;
    header.writeBigUInt64BE(BigInt(length), 2);
  }
  return Buffer.concat([header, payload]);
}

function parser(onText, onClose) {
  let buffer = Buffer.alloc(0);
  return (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    while (buffer.length >= 2) {
      const opcode = buffer[0] & 0x0f;
      let length = buffer[1] & 0x7f;
      let offset = 2;
      if (length === 126) {
        if (buffer.length < 4) return;
        length = buffer.readUInt16BE(2);
        offset = 4;
      } else if (length === 127) {
        if (buffer.length < 10) return;
        length = Number(buffer.readBigUInt64BE(2));
        offset = 10;
      }
      if (buffer.length < offset + 4 + length) return;
      const key = buffer.subarray(offset, offset + 4);
      const payload = Buffer.from(buffer.subarray(offset + 4, offset + 4 + length));
      for (let i = 0; i < length; i++) payload[i] ^= key[i % 4];
      buffer = buffer.subarray(offset + 4 + length);
      if (opcode === 1) onText(payload.toString("utf8"));
      if (opcode === 8) onClose();
    }
  };
}

// The app's WebSocket API as recorded from app 1.4.2: provider and model CRUD, its token and
// origin checks, session events for connections with session channels on, and injectable errors.
export class FakeApp {
  constructor() {
    this.token = "per-launch-token";
    this.providers = new Map();
    this.models = new Map();
    this.secrets = new Map();
    this.received = [];
    this.handshakes = [];
    this.connections = 0;
    this.sockets = new Set();
    this.busy = 0;
    this.fail = null;
    this.drop = null;
    this.beforeReply = {};
    this.beforeHello = [];
    // Called with each request before it's answered, as another client acting meanwhile.
    this.onRequest = null;
    this.server = http.createServer();
    this.server.on("upgrade", (request, socket) => this._upgrade(request, socket));
  }

  listen() {
    return new Promise((resolve) => this.server.listen(0, "127.0.0.1", resolve));
  }

  get port() {
    return this.server.address().port;
  }

  writeRunFiles(home) {
    mkdirSync(join(home, "run"), { recursive: true });
    writeFileSync(join(home, "run", "ws.port"), `${this.port}`);
    writeFileSync(join(home, "run", "ws.token"), `${this.token}`);
    // This process stands in for the app, so an extension run in-process is one of its descendants.
    writeFileSync(join(home, "run", "app-owner.pid"), `${process.pid}`);
  }

  _upgrade(request, socket) {
    this.handshakes.push(request.headers);
    const params = new URL(request.url, "http://x").searchParams;
    const token = params.get("token");
    if (request.headers.origin || token !== this.token) {
      socket.end("HTTP/1.1 403 Forbidden\r\n\r\n");
      return;
    }
    this.connections++;
    this.sockets.add(socket);
    socket.on("close", () => {
      this.sockets.delete(socket);
    });
    const accept = createHash("sha1")
      .update(request.headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
      .digest("base64");
    socket.write(
      `HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`,
    );
    const send = (message) => socket.write(encode(JSON.stringify(message)));
    for (const frame of this.beforeHello) send(frame);
    send({ type: "server_hello", connection_id: this.connections });
    send({ type: "keep_awake_changed" });
    socket.on("error", () => {});
    socket.on(
      "data",
      parser((text) => {
        const message = JSON.parse(text);
        this.received.push(message);
        if (message.type === this.drop) {
          socket.destroy();
          return;
        }
        this.onRequest?.(message);
        for (const frame of this.beforeReply[message.type] ?? []) send(frame);
        delete this.beforeReply[message.type];
        const reply = this._reply(message);
        if (!("request_id" in reply)) reply.request_id = message.request_id;
        send(reply);
      }, () => socket.end(Buffer.from([0x88, 0]))),
    );
  }

  _reply(message) {
    const kind = message.type;
    if (this.fail === kind) return { type: "error", message: "refused", request_id: message.request_id };
    if (kind.startsWith("upsert") && this.busy) {
      this.busy--;
      return { type: "error", message: "provider update still in progress" };
    }
    if (kind === "list_model_providers") return { type: "model_providers_list", providers: [...this.providers.values()] };
    if (kind === "upsert_model_provider") {
      const { provider } = message;
      if (message.secret) this.secrets.set(provider.id, message.secret.value);
      this.providers.set(provider.id, { ...provider, hasSecret: this.secrets.has(provider.id) });
      return { type: "model_provider_upserted", provider: this.providers.get(provider.id) };
    }
    if (kind === "delete_model_provider") {
      this.providers.delete(message.provider_id);
      this.secrets.delete(message.provider_id);
      for (const [id, model] of this.models) if (model.providerId === message.provider_id) this.models.delete(id);
      return { type: "model_provider_deleted", provider_id: message.provider_id };
    }
    if (kind === "list_provider_models") return { type: "provider_models_list", models: [...this.models.values()] };
    if (kind === "upsert_provider_model") {
      this.models.set(message.model.id, message.model);
      return { type: "provider_model_upserted", model: message.model };
    }
    if (kind === "delete_provider_model") {
      this.models.delete(message.model_id);
      return { type: "provider_model_deleted", model_id: message.model_id };
    }
    return { type: "error", message: `unknown variant \`${kind}\`` };
  }

  sent(kind) {
    return this.received.filter((message) => message.type === kind);
  }

  close() {
    for (const socket of this.sockets) socket.destroy();
    this.server.close();
  }
}
