import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { delimiter } from "node:path";

export const REFRESH_MARGIN_MS = 120_000;
export const DEFAULT_TOKEN_TTL_MS = 300_000;
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

export function childEnv(config, env = process.env) {
  const path = [...(config.pathDirs || []), env.PATH].filter(Boolean).join(delimiter);
  return { ...env, PATH: path };
}

export function runCommand(argv, env) {
  return new Promise((resolve, reject) => {
    execFile(argv[0], argv.slice(1), { env, timeout: TOKEN_COMMAND_TIMEOUT_MS }, (error, stdout, stderr) => {
      if (error) {
        const detail = String(stderr || error.message).trim().split("\n").pop();
        reject(new Error(`ug auth-token failed: ${detail}`));
        return;
      }
      const token = String(stdout).trim();
      if (token) resolve(token);
      else reject(new Error("ug auth-token printed no token"));
    });
  });
}

// One mint at a time: concurrent requests share it, and the token is reused until shortly
// before it expires.
export function createTokenSource(config, { run = runCommand, now = Date.now } = {}) {
  let cached = null;
  let pending = null;
  return function token() {
    if (cached && now() < cached.refreshAt) return Promise.resolve(cached.value);
    if (!pending) {
      pending = run(config.tokenCommand, childEnv(config))
        .then((value) => {
          const expiry = jwtExpiryMs(value);
          const refreshAt = expiry ? expiry - REFRESH_MARGIN_MS : now() + DEFAULT_TOKEN_TTL_MS;
          cached = { value, refreshAt };
          return value;
        })
        .finally(() => {
          pending = null;
        });
    }
    return pending;
  };
}

// The app retries a failing token provider several times, so the sign-in hint is shown once per
// run of failures.
export function withLoginHint(token, showHint) {
  let hinted = false;
  return async function bearerToken() {
    try {
      const value = await token();
      hinted = false;
      return value;
    } catch (error) {
      if (!hinted) {
        hinted = true;
        await Promise.resolve()
          .then(() => showHint())
          .catch(() => {});
      }
      throw error;
    }
  };
}

export function buildProviders(config, bearerTokenProvider) {
  return config.providers.map((provider) => ({ ...provider, bearerTokenProvider }));
}
