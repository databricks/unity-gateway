import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, utimesSync, writeFileSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test as nodeTest } from "node:test";

import { copilotHome, readEndpoint, syncInBackground, syncOnce } from "../src/ucode/copilot_app_plugin/app-rows.mjs";

// The Copilot runtime's Node has a global WebSocket; Node before 22 doesn't.
const skip = typeof WebSocket === "function" ? false : "this Node has no global WebSocket";
const test = (name, fn) => nodeTest(name, { skip }, fn);

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
// origin checks, and injectable errors.
class FakeApp {
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
    writeFileSync(join(home, "run", "ws.port"), `${this.port}\n4242\n`);
    writeFileSync(join(home, "run", "ws.token"), `${this.token}\n4242\n`);
  }

  _upgrade(request, socket) {
    this.handshakes.push(request.headers);
    const token = new URL(request.url, "http://x").searchParams.get("token");
    if (request.headers.origin || token !== this.token) {
      socket.end("HTTP/1.1 403 Forbidden\r\n\r\n");
      return;
    }
    this.connections++;
    this.sockets.add(socket);
    socket.on("close", () => this.sockets.delete(socket));
    const accept = createHash("sha1")
      .update(request.headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
      .digest("base64");
    socket.write(
      `HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`,
    );
    const send = (message) => socket.write(encode(JSON.stringify(message)));
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

const CLAUDE = "11111111-1111-5111-8111-111111111111";
const GPT = "22222222-2222-5222-8222-222222222222";
const OPEN = "33333333-3333-5333-8333-333333333333";

function providerRows(id, name, models, secret = null) {
  return {
    provider: { id, name, kind: secret ? "anthropic" : "custom", settings: { baseUrl: `https://ws/${name}` } },
    secret,
    models: models.map((modelId) => ({ id: `${id}/${modelId}`, providerId: id, modelId, wireModel: modelId, displayName: modelId })),
  };
}

const PLACEHOLDER = { kind: "apiKey", value: "ug-placeholder-not-a-secret" };
const ROWS = {
  providers: [providerRows(CLAUDE, "claude", ["claude-sonnet-4.6"], PLACEHOLDER), providerRows(GPT, "gpt", ["gpt-5-2"])],
  ownedProviderIds: [CLAUDE, GPT, OPEN],
};

async function setup(t) {
  const app = new FakeApp();
  await app.listen();
  t.after(() => app.close());
  const home = mkdtempSync(join(tmpdir(), "ug-copilot-home-"));
  const stateDir = mkdtempSync(join(tmpdir(), "ug-copilot-state-"));
  app.writeRunFiles(home);
  return { app, options: { home, stateDir, timeoutMs: 2000 } };
}

test("readEndpoint reads the first value of each run file and honours COPILOT_HOME", () => {
  const home = mkdtempSync(join(tmpdir(), "ug-copilot-home-"));
  assert.equal(readEndpoint(home), null);
  mkdirSync(join(home, "run"));
  writeFileSync(join(home, "run", "ws.port"), "4321\n99\n");
  writeFileSync(join(home, "run", "ws.token"), "tok\n99\n");
  assert.deepEqual(readEndpoint(home), { port: 4321, token: "tok" });
  assert.equal(copilotHome({ COPILOT_HOME: home }), home);
});

test("syncOnce writes the rows, then does nothing until they or the app launch change", async (t) => {
  const { app, options } = await setup(t);
  app.providers.set("mine", { id: "mine", name: "Mine", kind: "custom" });

  assert.equal(await syncOnce({ app: ROWS }, options), "synced");
  assert.deepEqual([...app.providers.keys()].sort(), [CLAUDE, GPT, "mine"].sort());
  assert.deepEqual([...app.secrets], [[CLAUDE, PLACEHOLDER.value]]);
  assert.deepEqual([...app.models.keys()].sort(), [`${CLAUDE}/claude-sonnet-4.6`, `${GPT}/gpt-5-2`]);
  assert.ok(app.handshakes.every((headers) => !headers.origin));
  const ids = app.received.map((message) => message.request_id);
  assert.equal(new Set(ids).size, ids.length);

  const received = app.received.length;
  assert.equal(await syncOnce({ app: ROWS }, options), "current");
  assert.equal(app.received.length, received);

  app.token = "next-launch";
  app.writeRunFiles(options.home);
  assert.equal(await syncOnce({ app: ROWS }, options), "synced");
  assert.ok(app.sent("upsert_model_provider").slice(-2).every((message) => !message.secret));
});

test("syncOnce deletes ug's stale providers and rows but no one else's", async (t) => {
  const { app, options } = await setup(t);
  await syncOnce({ app: ROWS }, options);
  app.providers.set("mine", { id: "mine", name: "Mine", kind: "custom" });
  app.models.set("user-row", { id: "user-row", providerId: GPT, modelId: "x" });
  app.models.set("odd", { providerId: GPT, modelId: "no-id" });

  const next = {
    providers: [providerRows(GPT, "gpt", ["gpt-6"]), providerRows(OPEN, "open", ["glm-5"])],
    ownedProviderIds: ROWS.ownedProviderIds,
  };
  assert.equal(await syncOnce({ app: next }, options), "synced");

  assert.deepEqual([...app.providers.keys()].sort(), [GPT, OPEN, "mine"].sort());
  assert.deepEqual([...app.models.keys()].sort(), [`${GPT}/gpt-6`, `${OPEN}/glm-5`, "odd"].sort());
});

test("repeated syncs leave exactly one copy of each row", async (t) => {
  const { app, options } = await setup(t);
  const stateDirs = [0, 1, 2].map(() => mkdtempSync(join(tmpdir(), "ug-copilot-state-")));
  await Promise.all(stateDirs.map((stateDir) => syncOnce({ app: ROWS }, { ...options, stateDir })));
  assert.equal(app.providers.size, 2);
  assert.equal(app.models.size, 2);
});

test("only one extension process syncs at a time; a stale lock is taken over", async (t) => {
  const { app, options } = await setup(t);
  const lock = join(options.stateDir, "app-sync.lock");
  writeFileSync(lock, "");
  assert.equal(await syncOnce({ app: ROWS }, options), "busy");
  assert.equal(app.received.length, 0);

  const old = new Date(Date.now() - 120_000);
  utimesSync(lock, old, old);
  assert.equal(await syncOnce({ app: ROWS }, options), "synced");
});

test("a reply for another request is skipped", async (t) => {
  const { app, options } = await setup(t);
  app.beforeReply.list_model_providers = [
    { type: "model_providers_list", providers: [{ id: "x" }], request_id: "ug-someone-else" },
    { type: "model_providers_list", providers: [{ id: "y" }] },
    { type: "error", message: "boom", request_id: "ug-someone-else" },
  ];
  assert.equal(await syncOnce({ app: ROWS }, options), "synced");
  assert.equal(app.providers.size, 2);
});

test("an error without a request_id answers the pending request", async (t) => {
  const { app, options } = await setup(t);
  app.beforeReply.list_model_providers = [{ type: "error", message: "Invalid message format" }];
  await assert.rejects(syncOnce({ app: ROWS }, options), /Invalid message format/);
  assert.equal(app.providers.size, 0);
});

test("the sync retries while the app applies another change", async (t) => {
  const { app, options } = await setup(t);
  app.busy = 2;
  assert.equal(await syncOnce({ app: ROWS }, options), "synced");
  assert.equal(app.models.size, 2);
});

test("failures are logged, never thrown, and the next run retries", async (t) => {
  const { app, options } = await setup(t);
  const logs = [];
  app.fail = "upsert_provider_model";
  assert.equal(await syncInBackground({ app: ROWS }, options, (line) => logs.push(line)), "failed");
  assert.match(logs[0], /refused upsert_provider_model/);

  app.fail = null;
  app.drop = "list_model_providers";
  assert.equal(await syncInBackground({ app: ROWS }, options, (line) => logs.push(line)), "failed");
  assert.match(logs[1], /closed the connection/);

  app.drop = null;
  assert.equal(await syncOnce({ app: ROWS }, options), "synced");
});

test("a closed app or a stale token is not synced", async (t) => {
  const { app, options } = await setup(t);
  const home = mkdtempSync(join(tmpdir(), "ug-copilot-home-"));
  assert.equal(await syncOnce({ app: ROWS }, { ...options, home }), "not_running");

  writeFileSync(join(options.home, "run", "ws.token"), "stale\n1\n");
  assert.equal(await syncInBackground({ app: ROWS }, options, () => {}), "failed");
  assert.equal(app.received.length, 0);

  mkdirSync(join(home, "run"));
  writeFileSync(join(home, "run", "ws.port"), "1\n1\n");
  writeFileSync(join(home, "run", "ws.token"), "x\n1\n");
  assert.equal(await syncInBackground({ app: ROWS }, { ...options, home }, () => {}), "failed");
});

test("no app rows in the config means nothing to sync", async (t) => {
  const { app, options } = await setup(t);
  assert.equal(await syncOnce({}, options), "skipped");
  assert.equal(await syncOnce({ app: { providers: [], ownedProviderIds: [] } }, options), "skipped");
  assert.equal(app.connections, 0);
});
