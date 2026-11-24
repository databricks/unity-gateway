import { existsSync } from "node:fs";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

import { joinSession } from "@github/copilot-sdk/extension";

import { createDebugLog } from "./app-debug.mjs";
import { runningUnderApp } from "./app-host.mjs";
import { removeInBackground } from "./app-remove.mjs";
import { launchRestart, tellRestart } from "./app-restart.mjs";
import { syncInBackground } from "./app-rows.mjs";
import {
  clearRedirect,
  createSeatRedirect,
  keepAliveOnErrors,
  noteRedirect,
  redirectSkipReason,
} from "./app-seat-redirect.mjs";
import { shutdownBeforeTerminating } from "./app-state.mjs";
import {
  buildProviders,
  createTokenSource,
  hintFor,
  isAuthRejection,
  loadConfig,
  withLoginHint,
} from "./ug-plugin.mjs";

const stateDir = fileURLToPath(new URL(".", import.meta.url));
const debug = createDebugLog(stateDir);
const START_ENV_NAMES = [
  "SESSION_ID",
  "COPILOT_HOME",
  "SystemRoot",
  "SYSTEMROOT",
  "PATH",
  "Path",
  "HTTP_PROXY",
  "HTTPS_PROXY",
  "ALL_PROXY",
  "NO_PROXY",
  "http_proxy",
  "https_proxy",
  "all_proxy",
  "no_proxy",
  "NODE_USE_ENV_PROXY",
  "NODE_EXTRA_CA_CERTS",
];

function startEnvironment(env) {
  const picked = {};
  for (const [name, value] of Object.entries(env)) {
    if (name.startsWith("COPILOT_") || START_ENV_NAMES.includes(name)) picked[name] = value;
  }
  return picked;
}

function configSummary(config) {
  let gatewayHost = null;
  try {
    gatewayHost = config.seatRelay?.gatewayBase ? new URL(config.seatRelay.gatewayBase).host : null;
  } catch {
    gatewayHost = "<unparsed>";
  }
  return {
    remove: config.remove === true,
    seatRelay: Boolean(config.seatRelay),
    providerService: config.seatRelay?.providerService ?? null,
    gatewayHost,
    askGitHub: config.seatRelay?.askGitHub ?? null,
    ugPath: config.ugPath ?? null,
    ugPathExists: typeof config.ugPath === "string" ? existsSync(config.ugPath) : null,
    tokenCommand: config.tokenCommand?.[0] ?? null,
    databricksPath: config.databricksPath ?? null,
    databricksPathExists: typeof config.databricksPath === "string" ? existsSync(config.databricksPath) : null,
    pathDirs: config.pathDirs ?? null,
    providers: config.providers?.length ?? 0,
    models: config.models?.length ?? 0,
    appProviders: config.app?.providers?.length ?? 0,
  };
}

debug("start", {
  platform: process.platform,
  arch: process.arch,
  node: process.version,
  ppid: process.ppid,
  cwd: process.cwd(),
  execPath: process.execPath,
  argv: process.argv,
  stateDir,
  env: startEnvironment(process.env),
});

const fatalHandlers = {};
const fatal = (kind) => (error) => {
  debug("fatal", { kind, message: String(error?.message ?? error), code: error?.code ?? null, stack: error?.stack ?? null });
  // With no other listener the process still ends the way it would have without this one. The
  // handlers go first, or the rethrow lands back here.
  if (process.listenerCount(kind) === 1) {
    for (const [name, handler] of Object.entries(fatalHandlers)) process.off(name, handler);
    process.nextTick(() => {
      throw error;
    });
  }
};
for (const kind of ["uncaughtException", "unhandledRejection"]) {
  fatalHandlers[kind] = fatal(kind);
  process.on(kind, fatalHandlers[kind]);
}
process.on("exit", (code) => debug("exit", { code }));

let config;
try {
  config = loadConfig(new URL("./config.json", import.meta.url));
} catch (error) {
  debug("fatal", { kind: "config", message: String(error?.message ?? error), code: error?.code ?? null });
  throw error;
}
debug("config", configSummary(config));

const syncOptions = { stateDir };
const logSyncFailure = (message) => {
  debug("log", { message });
  console.error(message);
};
// The extension can start before the app publishes its WebSocket endpoint.
const RETRY_DELAYS_MS = [2_000, 5_000, 10_000];
const PROXIED_MESSAGE =
  "Databricks: this session's GitHub Copilot models aren't going through Unity Gateway, because a proxy " +
  "setting could route the relay's loopback requests through the proxy.";

async function withRetries(attempt) {
  let result = await attempt();
  for (const ms of RETRY_DELAYS_MS) {
    if (!["not_running", "failed"].includes(result)) break;
    await delay(ms);
    result = await attempt();
  }
  return result;
}

async function main() {
  // The host check lists the processes, which on Windows can take seconds, so the relay starts
  // listening and checks for a proxy meanwhile; the session is only joined once both are known.
  const hostStarted = Date.now();
  const hostCheck = runningUnderApp({ debug }).then((host) => ({ ...host, ms: Date.now() - hostStarted }));
  if (config.remove) {
    const host = await hostCheck;
    const removal = withRetries(() => removeInBackground(config, syncOptions, logSyncFailure)).then((result) => {
      debug("remove", { result });
      return result;
    });
    if (host.app) {
      try {
        const session = await joinSession();
        debug("session.joined", { mode: "remove" });
        await clearRedirect(session);
        debug("redirect.clear", { why: "remove" });
      } catch (error) {
        logSyncFailure(`Databricks models: couldn't join the session: ${error?.message ?? error}`);
      }
    }
    await removal;
    return;
  }
  let redirect = null;
  let listening = null;
  const tokenSource = createTokenSource(config);
  const skip = redirectSkipReason(config);
  const sandboxed = skip === "sandboxed";
  debug("redirect.gate", { skip });
  if (!sandboxed) {
    try {
      redirect = createSeatRedirect(config, {
        tokenSource,
        log: logSyncFailure,
        stateDir,
        hostCheck,
        debug,
      });
      listening = redirect?.listen().then(() => null, (error) => error) ?? null;
    } catch (error) {
      listening = Promise.resolve(error);
    }
  }
  const host = await hostCheck;
  if (!host.app) {
    // A Copilot CLI or VS Code session that loaded the app's plugin keeps its own models.
    await listening;
    await redirect?.close();
    await noteRedirect(stateDir, { redirect: "not the app", host: host.via, hostMs: host.ms });
    return;
  }
  const restart = withRetries(() => syncInBackground(config, syncOptions, logSyncFailure)).then((synced) => {
    debug("sync", { result: synced });
    if (synced !== "changed") return null;
    return launchRestart({ ...syncOptions, ugPath: config.ugPath }).then((launched) => {
      debug("restart", { result: launched });
      return launched;
    });
  });
  let session;
  // session.log is async, and a rejected one must not end the extension.
  const say = (message, level = "error") =>
    Promise.resolve()
      .then(() => session.log(message, { level }))
      .catch(() => {});
  const bearerTokenProvider = withLoginHint(tokenSource, (error) => {
    const hint = hintFor(error, config);
    debug("token.hint", { code: error?.code ?? null, hinted: Boolean(session && hint) });
    if (!session || !hint) return false;
    say(hint);
    return true;
  });
  if (sandboxed) noteRedirect(stateDir, { redirect: "sandboxed", hostMs: host.ms });
  const listenError = await listening;
  if (listenError) {
    logSyncFailure(`Databricks seat relay: not started: ${listenError?.message ?? listenError}`);
    noteRedirect(stateDir, {
      redirect: "not started",
      error: String(listenError?.code ?? listenError?.message ?? listenError),
      hostMs: host.ms,
    });
    redirect = null;
  }
  if (redirect) shutdownBeforeTerminating([(ms) => redirect.shutdown(ms)]);
  const joining = Date.now();
  session = await joinSession({
    providers: buildProviders(config, bearerTokenProvider),
    models: config.models,
  });
  debug("session.joined", { mode: "serve", ms: Date.now() - joining, redirect: Boolean(redirect) });
  // Before anything else: the first turn's calls must already go through.
  if (redirect) {
    if (await redirect.apply(session)) keepAliveOnErrors(logSyncFailure);
    else if (redirect.state === "proxied") say(PROXIED_MESSAGE, "warning");
  } else {
    await clearRedirect(session);
    debug("redirect.clear", { why: sandboxed ? "sandboxed" : "not started" });
  }
  session.on((event) => {
    redirect?.handle(event);
    if (!isAuthRejection(event)) return;
    debug("token.invalidate", { type: event.type, statusCode: event.data?.statusCode });
    tokenSource.invalidate();
    // The app's own message for a 401 names Copilot's API-key settings, not Databricks sign-in.
    if (event.type === "session.error") say(config.loginHint);
  });
  const launched = await restart;
  if (launched) await tellRestart(session, launched);
}

await main();
