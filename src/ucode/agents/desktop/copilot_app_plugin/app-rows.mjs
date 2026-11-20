import { createHash, randomUUID } from "node:crypto";
import { closeSync, openSync, readFileSync, renameSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const REQUEST_TIMEOUT_MS = 15_000;
export const LOCK_STALE_MS = 60_000;
export const LOCK_FILE = "app-sync.lock";
export const SYNC_STAMP_FILE = "app-sync.json";

export function copilotHome(env = process.env) {
  return env.COPILOT_HOME || join(homedir(), ".copilot");
}

// Each run file holds the value, then the app's pid.
function runValue(home, name) {
  return readFileSync(join(home, "run", name), "utf8").trim().split(/\s+/)[0] || null;
}

export function readEndpoint(home) {
  try {
    const port = Number.parseInt(runValue(home, "ws.port"), 10);
    const token = runValue(home, "ws.token");
    return Number.isInteger(port) && token ? { port, token } : null;
  } catch {
    return null;
  }
}

export class AppError extends Error {}

class AppConnection {
  constructor(ws, timeoutMs) {
    this._ws = ws;
    this._timeoutMs = timeoutMs;
    this._backlog = [];
    this._waiter = null;
    this.closed = false;
    ws.onmessage = (event) => {
      let message;
      try {
        message = JSON.parse(typeof event.data === "string" ? event.data : String(event.data));
      } catch {
        return;
      }
      if (message && typeof message === "object") this._deliver(message);
    };
    ws.onclose = () => {
      this.closed = true;
      this._waiter?.reject(new AppError("The GitHub Copilot app closed the connection."));
    };
  }

  _deliver(message) {
    if (this._waiter?.matches(message)) {
      const waiter = this._waiter;
      this._waiter = null;
      waiter.resolve(message);
    } else {
      this._backlog.push(message);
    }
  }

  wait(matches) {
    const index = this._backlog.findIndex(matches);
    if (index >= 0) return Promise.resolve(this._backlog.splice(index, 1)[0]);
    if (this.closed) return Promise.reject(new AppError("The GitHub Copilot app closed the connection."));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._waiter = null;
        reject(new AppError("The GitHub Copilot app didn't answer."));
      }, this._timeoutMs);
      const done = (fn) => (value) => {
        clearTimeout(timer);
        fn(value);
      };
      this._waiter = { matches, resolve: done(resolve), reject: done(reject) };
    });
  }

  async request(message, reply, match = {}) {
    for (let attempt = 0; ; attempt++) {
      const requestId = `ug-${randomUUID()}`;
      this._ws.send(JSON.stringify({ ...message, request_id: requestId }));
      // The app echoes request_id on its replies, but not on errors about the message's format,
      // which then answer the pending request.
      const answer = await this.wait((candidate) =>
        candidate.type === "error"
          ? candidate.request_id === requestId || candidate.request_id === undefined
          : candidate.request_id === requestId &&
            candidate.type === reply &&
            Object.entries(match).every(([name, value]) => candidate[name] === value),
      );
      if (answer.type !== "error") return answer;
      const error = String(answer.message ?? "");
      // The app refuses a provider change while another one is being applied.
      if (!error.includes("still in progress") || attempt === 5) {
        throw new AppError(`The GitHub Copilot app refused ${message.type}: ${error}`);
      }
      await new Promise((resolve) => setTimeout(resolve, 200 * 2 ** attempt));
    }
  }

  close() {
    try {
      this._ws.close();
    } catch {}
  }
}

export async function connectApp(endpoint, { WebSocketImpl, timeoutMs = REQUEST_TIMEOUT_MS } = {}) {
  const Impl = WebSocketImpl ?? globalThis.WebSocket;
  if (!Impl) throw new AppError("This Copilot runtime has no WebSocket client.");
  const url = `ws://127.0.0.1:${endpoint.port}/?token=${encodeURIComponent(endpoint.token)}&requester_id=ug&session_channels=false`;
  // No Origin: the app refuses any origin but its own window. Node's WebSocket sends none.
  const ws = new Impl(url);
  const app = new AppConnection(ws, timeoutMs);
  try {
    await app.wait((message) => message.type === "server_hello");
  } catch (error) {
    app.close();
    throw error;
  }
  return app;
}

async function listModels(app, providerId) {
  const { models } = await app.request(
    { type: "list_provider_models", provider_id: providerId },
    "provider_models_list",
  );
  if (!Array.isArray(models)) throw new AppError("The GitHub Copilot app listed its models in a new shape.");
  return models.filter((m) => m && typeof m.id === "string" && m.providerId === providerId);
}

async function deleteProvider(app, providerId) {
  await app.request({ type: "delete_model_provider", provider_id: providerId }, "model_provider_deleted", {
    provider_id: providerId,
  });
}

async function listProviders(app) {
  const { providers } = await app.request({ type: "list_model_providers" }, "model_providers_list");
  if (!Array.isArray(providers)) throw new AppError("The GitHub Copilot app listed its providers in a new shape.");
  return new Map(providers.filter((p) => p && typeof p.id === "string").map((p) => [p.id, p]));
}

export async function deleteProviders(app, providerIds) {
  const existing = await listProviders(app);
  for (const id of providerIds) {
    if (existing.has(id)) await deleteProvider(app, id);
  }
}

// Writes ``rows`` (config.app) to the app, and deletes the providers and models of ug's that
// aren't in it. Upserts with fixed ids, so running it again changes nothing.
export async function syncRows(app, rows) {
  const existing = await listProviders(app);
  const wanted = new Set(rows.providers.map((entry) => entry.provider.id));
  for (const id of rows.ownedProviderIds) {
    if (existing.has(id) && !wanted.has(id)) await deleteProvider(app, id);
  }
  for (const { provider, secret, models } of rows.providers) {
    const message = { type: "upsert_model_provider", provider };
    if (secret && !existing.get(provider.id)?.hasSecret) message.secret = secret;
    await app.request(message, "model_provider_upserted");
    const wantedModels = new Set(models.map((model) => model.id));
    for (const model of await listModels(app, provider.id)) {
      if (!wantedModels.has(model.id)) {
        await app.request({ type: "delete_provider_model", model_id: model.id }, "provider_model_deleted", {
          model_id: model.id,
        });
      }
    }
    for (const model of models) {
      await app.request({ type: "upsert_provider_model", model }, "provider_model_upserted");
    }
  }
}

export function tryLock(path, now) {
  try {
    closeSync(openSync(path, "wx"));
    return true;
  } catch (error) {
    if (error.code !== "EEXIST") throw error;
  }
  try {
    if (now() - statSync(path).mtimeMs < LOCK_STALE_MS) return false;
    unlinkSync(path);
    closeSync(openSync(path, "wx"));
    return true;
  } catch {
    return false;
  }
}

function readStamp(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8")).digest;
  } catch {
    return null;
  }
}

// Every session runs its own extension process. Each one syncs only when the app's launch token or
// the rows changed since the last sync, and one at a time.
export async function syncOnce(config, { home = copilotHome(), stateDir, now = Date.now, ...connect } = {}) {
  const rows = config.app;
  if (!rows || !rows.providers?.length && !rows.ownedProviderIds?.length) return "skipped";
  const endpoint = readEndpoint(home);
  if (!endpoint) return "not_running";
  const digest = createHash("sha256").update(JSON.stringify(rows)).update("\0").update(endpoint.token).digest("hex");
  const stampPath = join(stateDir, SYNC_STAMP_FILE);
  if (readStamp(stampPath) === digest) return "current";
  const lockPath = join(stateDir, LOCK_FILE);
  if (!tryLock(lockPath, now)) return "busy";
  try {
    const app = await connectApp(endpoint, connect);
    try {
      await syncRows(app, rows);
    } finally {
      app.close();
    }
    const temp = `${stampPath}.${process.pid}.tmp`;
    writeFileSync(temp, JSON.stringify({ digest }));
    renameSync(temp, stampPath);
    return "synced";
  } finally {
    try {
      unlinkSync(lockPath);
    } catch {}
  }
}

export async function syncInBackground(config, options, log) {
  try {
    return await syncOnce(config, options);
  } catch (error) {
    try {
      log(`Databricks models: couldn't update the GitHub Copilot app's model picker: ${error?.message ?? error}`);
    } catch {}
    return "failed";
  }
}
