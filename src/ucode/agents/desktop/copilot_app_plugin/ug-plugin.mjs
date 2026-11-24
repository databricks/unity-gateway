import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { delimiter, dirname } from "node:path";

export const REFRESH_MARGIN_MS = 120_000;
export const DEFAULT_TOKEN_TTL_MS = 300_000;
export const MIN_TOKEN_REUSE_MS = 30_000;
const TOKEN_COMMAND_TIMEOUT_MS = 30_000;

export function loadConfig(url) {
  return JSON.parse(readFileSync(url, "utf8"));
}

export function jwtExpiryMs(token) {
  const payload = token.split(".")[1];
  if (!payload) return null;
  try {
    const { exp } = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    return typeof exp === "number" ? exp * 1000 : null;
  } catch {
    return null;
  }
}

// On Windows the variable is usually `Path`; a second, differently cased key would leave the child
// with two.
export function childEnv(config, env = process.env) {
  const name = Object.keys(env).find((key) => key.toUpperCase() === "PATH") ?? "PATH";
  const databricksDir = config.databricksPath ? [dirname(config.databricksPath)] : [];
  const path = [...(config.pathDirs || []), ...databricksDir, env[name]].filter(Boolean).join(delimiter);
  return { ...env, [name]: path };
}

export function runCommand(argv, env) {
  return new Promise((resolve, reject) => {
    execFile(argv[0], argv.slice(1), { env, timeout: TOKEN_COMMAND_TIMEOUT_MS }, (error, stdout, stderr) => {
      if (error) {
        const detail = String(stderr || error.message).trim().split("\n").pop();
        const failure = new Error(`ug auth-token failed: ${detail}`);
        // A number is ug's exit status; a string such as ENOENT means it didn't start.
        failure.code = error.killed ? "TIMEOUT" : error.code;
        reject(failure);
        return;
      }
      const token = String(stdout).trim();
      if (token) resolve(token);
      else reject(new Error("ug auth-token printed no token"));
    });
  });
}

export const FORCE_REFRESH_FLAG = "--force-refresh";

// One mint at a time: concurrent requests share it, and the token is reused until shortly
// before it expires. After invalidate(), the next mint asks for a new token rather than the
// Databricks CLI's cached one, and a mint already running can't cache the rejected one.
export function createTokenSource(config, { run = runCommand, now = Date.now } = {}) {
  let cached = null;
  let pending = null;
  let forceRefresh = false;
  let generation = 0;
  function token() {
    if (cached && now() < cached.refreshAt) return Promise.resolve(cached.value);
    if (!pending) {
      const argv = forceRefresh ? [...config.tokenCommand, FORCE_REFRESH_FLAG] : config.tokenCommand;
      forceRefresh = false;
      const started = generation;
      const mint = run(argv, childEnv(config))
        .then((value) => {
          if (started === generation) {
            const expiry = jwtExpiryMs(value);
            // A token the CLI hands out close to its expiry is still reused briefly, rather than
            // minting again for every request.
            const refreshAt = expiry
              ? Math.max(expiry - REFRESH_MARGIN_MS, Math.min(expiry, now() + MIN_TOKEN_REUSE_MS))
              : now() + DEFAULT_TOKEN_TTL_MS;
            cached = { value, refreshAt };
          }
          return value;
        })
        .finally(() => {
          if (pending === mint) pending = null;
        });
      pending = mint;
    }
    return pending;
  }
  // `rejected` is the token the upstream refused; when a newer one is already cached, another
  // request's refresh has done the work and this one keeps it instead of minting again.
  token.invalidate = (rejected) => {
    if (rejected !== undefined && cached && cached.value !== rejected) return false;
    generation++;
    cached = null;
    pending = null;
    forceRefresh = true;
    return true;
  };
  return token;
}

export function isAuthRejection(event) {
  return (
    (event?.type === "session.error" || event?.type === "model.call_failure") &&
    event.data?.statusCode === 401
  );
}

// The hint for a failed `ug auth-token`: sign in again after an auth failure, or rerun configure
// when ug is gone. A timeout or other failure gets none.
export function hintFor(error, config) {
  if (error?.code === "ENOENT") return config.missingUgHint;
  if (typeof error?.code === "number") return config.loginHint;
  return null;
}

// The app retries a failing token provider several times, so a hint is shown once per run of
// failures. ``showHint(error)`` returns whether it showed one; until it does, the next failure
// tries again.
export function withLoginHint(token, showHint) {
  let hinted = false;
  return async function bearerToken() {
    try {
      const value = await token();
      hinted = false;
      return value;
    } catch (error) {
      if (!hinted) {
        hinted = await Promise.resolve()
          .then(() => Boolean(showHint(error)))
          .catch(() => false);
      }
      throw error;
    }
  };
}

// The gateway logs it as the usage table's client session id. An admin's header of that name wins.
export const SESSION_ID_HEADER = "session-id";

export function withSessionId(headers = {}, sessionId) {
  if (!sessionId || Object.keys(headers).some((name) => name.toLowerCase() === SESSION_ID_HEADER)) {
    return { ...headers };
  }
  return { [SESSION_ID_HEADER]: sessionId, ...headers };
}

// ``sessionId`` is the app session this extension process serves, which the runtime passes in
// SESSION_ID: joinSession resumes that session.
export function buildProviders(config, bearerTokenProvider, sessionId = process.env.SESSION_ID) {
  return config.providers.map((provider) => ({
    ...provider,
    headers: withSessionId(provider.headers, sessionId),
    bearerTokenProvider,
  }));
}
