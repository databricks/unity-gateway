import { createHash, randomUUID } from "node:crypto";
import { readFileSync, renameSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const REQUEST_TIMEOUT_MS = 15_000;
export const LOCK_STALE_MS = 60_000;
export const LOCK_FILE = "app-sync.lock";
export const SYNC_STAMP_FILE = "app-sync.json";
export const RESTART_MARKER_FILE = "app-restart.json";

export function copilotHome(env = process.env) {
  return env.COPILOT_HOME || join(homedir(), ".copilot");
}

// A run file holds its value, and in some app versions the app's pid after it.
function runFields(home, name) {
  return readFileSync(join(home, "run", name), "utf8").trim().split(/\s+/);
}

function runValue(home, name) {
  return runFields(home, name)[0] || null;
}

function pidField(home, name, index) {
  try {
    const pid = Number.parseInt(runFields(home, name)[index], 10);
    return Number.isInteger(pid) && pid > 1 ? pid : null;
  } catch {
    return null;
  }
}

// The app writes its own pid to run/app-owner.pid; ws.port carries none in app 1.4.2.
export function readAppPid(home) {
  return pidField(home, "app-owner.pid", 0) ?? pidField(home, "ws.port", 1);
}

export function readAppPidSource(home) {
  if (pidField(home, "app-owner.pid", 0) !== null) return "app-owner.pid";
  return pidField(home, "ws.port", 1) !== null ? "ws.port" : null;
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
      // Requests run one at a time, so nothing received before this send answers it; an id-less
      // error left over from earlier would otherwise read as a refusal.
      this._backlog.length = 0;
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

// The app's list replies can carry fields of their own, so a row matches when every field ug writes
// has the same value.
function matches(wanted, actual) {
  if (wanted === null || typeof wanted !== "object") return wanted === actual;
  if (Array.isArray(wanted)) {
    return Array.isArray(actual) && actual.length === wanted.length && wanted.every((value, i) => matches(value, actual[i]));
  }
  return (
    actual !== null &&
    typeof actual === "object" &&
    !Array.isArray(actual) &&
    Object.entries(wanted).every(([name, value]) => matches(value, actual[name]))
  );
}

// Writes ``rows`` (config.app) to the app, and deletes the providers and models of ug's that
// aren't in it. Upserts with fixed ids, so running it again changes nothing. Returns whether the
// app's rows differed from ``rows``.
class RemovalRequested extends Error {}

// ug marks the plugin's config for removal before it deletes ug's rows from a running app, so a
// sync already under way stops before it writes them back.
export function removalRequested(stateDir) {
  try {
    return JSON.parse(readFileSync(join(stateDir, "config.json"), "utf8")).remove === true;
  } catch {
    return false;
  }
}

export async function syncRows(app, rows, stop = () => false) {
  const write = (message, reply, match) => {
    if (stop()) throw new RemovalRequested();
    return app.request(message, reply, match);
  };
  const existing = await listProviders(app);
  const wanted = new Set(rows.providers.map((entry) => entry.provider.id));
  let changed = false;
  for (const id of rows.ownedProviderIds) {
    if (existing.has(id) && !wanted.has(id)) {
      await deleteProvider(app, id);
      changed = true;
    }
  }
  for (const { provider, secret, models } of rows.providers) {
    const message = { type: "upsert_model_provider", provider };
    if (secret && !existing.get(provider.id)?.hasSecret) message.secret = secret;
    if (message.secret || !matches(provider, existing.get(provider.id))) changed = true;
    await write(message, "model_provider_upserted");
    const wantedModels = new Set(models.map((model) => model.id));
    const listed = new Map();
    for (const model of await listModels(app, provider.id)) {
      listed.set(model.id, model);
      if (!wantedModels.has(model.id)) {
        await app.request({ type: "delete_provider_model", model_id: model.id }, "provider_model_deleted", {
          model_id: model.id,
        });
        changed = true;
      }
    }
    for (const model of models) {
      if (!matches(model, listed.get(model.id))) changed = true;
      await write({ type: "upsert_provider_model", model }, "provider_model_upserted");
    }
  }
  return changed;
}

// Returns the function that releases the lock, or null while another extension process holds it.
// The lock file names its owner, so a process whose lock was taken over as stale can't release the
// new owner's.
export function tryLock(path, now) {
  const owner = `${process.pid}-${randomUUID()}`;
  const release = () => {
    try {
      if (readFileSync(path, "utf8") === owner) unlinkSync(path);
    } catch {}
  };
  const take = () => {
    writeFileSync(path, owner, { flag: "wx" });
    return release;
  };
  try {
    return take();
  } catch (error) {
    if (error.code !== "EEXIST") throw error;
  }
  try {
    const stale = readFileSync(path, "utf8");
    if (now() - statSync(path).mtimeMs < LOCK_STALE_MS) return null;
    // Only one process can move the stale lock away. If it was replaced in the meantime, the new
    // owner's lock goes back.
    const moved = `${path}.${owner}`;
    renameSync(path, moved);
    if (readFileSync(moved, "utf8") !== stale) {
      renameSync(moved, path);
      return null;
    }
    unlinkSync(moved);
    return take();
  } catch {
    return null;
  }
}

function readStamp(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8")).digest;
  } catch {
    return null;
  }
}

function writeStamp(path, digest) {
  const temp = `${path}.${process.pid}.tmp`;
  writeFileSync(temp, JSON.stringify({ digest }));
  renameSync(temp, path);
}

// Every session runs its own extension process. Each one syncs only when the app's launch token or
// the rows changed since the last sync, and one at a time. "changed" tells the one process that
// first wrote these rows to the app to restart it: the window loads the picker's providers only
// when it starts, before any extension runs.
export async function syncOnce(config, { home = copilotHome(), stateDir, now = Date.now, ...connect } = {}) {
  const rows = config.app;
  if (!rows || !rows.providers?.length && !rows.ownedProviderIds?.length) return "skipped";
  const endpoint = readEndpoint(home);
  if (!endpoint) return "not_running";
  const rowsDigest = createHash("sha256").update(JSON.stringify(rows)).digest("hex");
  const digest = createHash("sha256").update(JSON.stringify(rows)).update("\0").update(endpoint.token).digest("hex");
  const stampPath = join(stateDir, SYNC_STAMP_FILE);
  if (readStamp(stampPath) === digest) return "current";
  const release = tryLock(join(stateDir, LOCK_FILE), now);
  if (!release) return "busy";
  try {
    const stop = () => removalRequested(stateDir);
    if (stop()) return "removing";
    const app = await connectApp(endpoint, connect);
    let changed;
    try {
      changed = await syncRows(app, rows, stop);
    } catch (error) {
      if (error instanceof RemovalRequested) return "removing";
      throw error;
    } finally {
      app.close();
    }
    // Keyed by the rows alone, so the restarted app's sync can't restart it again, even if the
    // app's lists never match the rows exactly.
    const markerPath = join(stateDir, RESTART_MARKER_FILE);
    const restart = changed && readStamp(markerPath) !== rowsDigest;
    if (restart) writeStamp(markerPath, rowsDigest);
    writeStamp(stampPath, digest);
    return restart ? "changed" : "synced";
  } finally {
    release();
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
