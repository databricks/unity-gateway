import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

import { joinSession } from "@github/copilot-sdk/extension";

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

const config = loadConfig(new URL("./config.json", import.meta.url));
const syncOptions = { stateDir: fileURLToPath(new URL(".", import.meta.url)) };
const logSyncFailure = (message) => console.error(message);
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

const host = await runningUnderApp();
if (config.remove) {
  const removal = withRetries(() => removeInBackground(config, syncOptions, logSyncFailure));
  if (host.app) {
    try {
      const session = await joinSession();
      await clearRedirect(session);
    } catch (error) {
      logSyncFailure(`Databricks models: couldn't join the session: ${error?.message ?? error}`);
    }
  }
  await removal;
} else if (!host.app) {
  // A Copilot CLI or VS Code session that loaded the app's plugin keeps its own models.
  await noteRedirect(syncOptions.stateDir, { redirect: "not the app", host: host.via });
} else {
  const restart = withRetries(() => syncInBackground(config, syncOptions, logSyncFailure)).then((synced) =>
    synced === "changed" ? launchRestart({ ...syncOptions, ugPath: config.ugPath }) : null,
  );
  let session;
  // session.log is async, and a rejected one must not end the extension.
  const say = (message, level = "error") =>
    Promise.resolve()
      .then(() => session.log(message, { level }))
      .catch(() => {});
  const tokenSource = createTokenSource(config);
  const bearerTokenProvider = withLoginHint(tokenSource, (error) => {
    const hint = hintFor(error, config);
    if (!session || !hint) return false;
    say(hint);
    return true;
  });
  let redirect = null;
  if (redirectSkipReason(config) === "sandboxed") {
    noteRedirect(syncOptions.stateDir, { redirect: "sandboxed" });
  }
  try {
    redirect = createSeatRedirect(config, { tokenSource, log: logSyncFailure, stateDir: syncOptions.stateDir });
    await redirect?.listen();
  } catch (error) {
    logSyncFailure(`Databricks seat relay: not started: ${error?.message ?? error}`);
    noteRedirect(syncOptions.stateDir, { redirect: "not started", error: String(error?.code ?? error?.message ?? error) });
    redirect = null;
  }
  if (redirect) shutdownBeforeTerminating([(ms) => redirect.shutdown(ms)]);
  session = await joinSession({
    providers: buildProviders(config, bearerTokenProvider),
    models: config.models,
  });
  // Before anything else: the first turn's calls must already go through.
  if (redirect) {
    if (await redirect.apply(session)) keepAliveOnErrors(logSyncFailure);
    else if (redirect.state === "proxied") say(PROXIED_MESSAGE, "warning");
  } else {
    await clearRedirect(session);
  }
  session.on((event) => {
    redirect?.handle(event);
    if (!isAuthRejection(event)) return;
    tokenSource.invalidate();
    // The app's own message for a 401 names Copilot's API-key settings, not Databricks sign-in.
    if (event.type === "session.error") say(config.loginHint);
  });
  const launched = await restart;
  if (launched) await tellRestart(session, launched);
}
