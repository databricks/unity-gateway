import { appendFileSync, renameSync, statSync } from "node:fs";
import { join } from "node:path";

// Temporary diagnostics for the Windows rollout of the app's seat relay: one redacted JSON line per
// step in the extension's directory, to be removed once the relay is verified there.
export const DEBUG_LOG_FILE = "seat-relay-debug.log";
export const DEBUG_LOG_LIMIT_BYTES = 5 * 1024 * 1024;
export const REDACTED = "<redacted>";
export const OMITTED = "<omitted>";
const MAX_DEPTH = 6;

const TOKEN_PATTERNS = [
  /\b(?:gho|ghu|ghp|ghs|ghr)_[A-Za-z0-9_]+/g,
  /\bgithub_pat_[A-Za-z0-9_]+/g,
  /\bdapi[A-Za-z0-9-]+/g,
  /\bBearer\s+\S+/gi,
  /\beyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+){1,2}/g,
  /\btid=[^\s"']+/g,
];
// Environment-style names (COPILOT_GITHUB_TOKEN, COPILOT_API_KEY, ...) whose values are secrets.
const SECRET_KEY = /(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|KEY|AUTH|CREDENTIALS?|COOKIE)(?:_|$)/;
const SECRET_HEADER_WORDS = /token|secret|auth|key|password|cookie|credential/;
const URL_CREDENTIALS = /(\w+:\/\/)[^/\s@]+@/g;
const SECRET_HEADERS = new Set([
  "authorization",
  "proxy-authorization",
  "x-databricks-ai-gateway-token",
  "cookie",
  "set-cookie",
  "copilot-session-token",
]);
const SAFE_HEADERS = new Set([
  "accept",
  "accept-encoding",
  "content-type",
  "content-length",
  "user-agent",
  "x-github-api-version",
  "copilot-integration-id",
  "openai-intent",
  "x-request-id",
  "x-interaction-type",
  "session-id",
  "thread-id",
]);

function redactString(text, secrets) {
  let out = text;
  for (const secret of secrets) {
    if (secret) out = out.split(secret).join(REDACTED);
  }
  out = out.replace(URL_CREDENTIALS, `$1${REDACTED}@`);
  for (const pattern of TOKEN_PATTERNS) out = out.replace(pattern, REDACTED);
  return out;
}

// Strings lose anything that looks like a credential; objects and arrays are walked.
export function redact(value, secrets = [], depth = 0) {
  if (typeof value === "string") return redactString(value, secrets);
  if (value instanceof Error) return redact({ message: value.message, code: value.code, stack: value.stack }, secrets, depth);
  if (Array.isArray(value)) {
    return depth >= MAX_DEPTH ? "[...]" : value.map((item) => redact(item, secrets, depth + 1));
  }
  if (value && typeof value === "object") {
    if (depth >= MAX_DEPTH) return "{...}";
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      out[key] = SECRET_KEY.test(key) ? REDACTED : redact(item, secrets, depth + 1);
    }
    return out;
  }
  return value;
}

// Header names always, values only for the allowlist (and never the credential headers).
export function redactHeaders(headers, secrets = []) {
  const out = {};
  for (const [name, value] of Object.entries(headers ?? {})) {
    const key = name.toLowerCase();
    if (SECRET_HEADERS.has(key)) out[key] = REDACTED;
    else if (SAFE_HEADERS.has(key) || (key.startsWith("x-databricks-") && !SECRET_HEADER_WORDS.test(key))) {
      out[key] = redact(Array.isArray(value) ? value.join(", ") : String(value), secrets);
    } else out[key] = OMITTED;
  }
  return out;
}

export const NO_DEBUG = Object.assign(() => {}, { addSecret: () => {}, headers: () => ({}), path: null });

export function createDebugLog(
  stateDir,
  {
    now = Date.now,
    pid = process.pid,
    sessionId = process.env.SESSION_ID ?? null,
    secrets = [],
    limit = DEBUG_LOG_LIMIT_BYTES,
    fs = { appendFileSync, renameSync, statSync },
  } = {},
) {
  const path = join(stateDir, DEBUG_LOG_FILE);
  const known = [...secrets];
  function rotate() {
    let size = 0;
    try {
      size = fs.statSync(path).size;
    } catch {
      return;
    }
    if (size > limit) fs.renameSync(path, `${path}.1`);
  }
  function debug(event, fields = {}) {
    try {
      rotate();
      const line = JSON.stringify({ t: new Date(now()).toISOString(), sid: sessionId, pid, event, ...redact(fields, known) });
      fs.appendFileSync(path, `${line}\n`);
    } catch {
      // Diagnostics never cost the extension anything.
    }
  }
  debug.addSecret = (secret) => {
    if (secret) known.push(String(secret));
  };
  debug.path = path;
  debug.headers = (headers) => redactHeaders(headers, known);
  return debug;
}
