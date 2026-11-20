import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

import { joinSession } from "@github/copilot-sdk/extension";

import { removeInBackground } from "./app-remove.mjs";
import { launchRestart, tellRestart } from "./app-restart.mjs";
import { syncInBackground } from "./app-rows.mjs";
import { createTracer } from "./app-tracing.mjs";
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

async function withRetries(attempt) {
  let result = await attempt();
  for (const ms of RETRY_DELAYS_MS) {
    if (!["not_running", "failed"].includes(result)) break;
    await delay(ms);
    result = await attempt();
  }
  return result;
}

if (config.remove) {
  const removal = withRetries(() => removeInBackground(config, syncOptions, logSyncFailure));
  try {
    await joinSession();
  } catch (error) {
    logSyncFailure(`Databricks models: couldn't join the session: ${error?.message ?? error}`);
  }
  await removal;
} else {
  const restart = withRetries(() => syncInBackground(config, syncOptions, logSyncFailure)).then((synced) =>
    synced === "changed" ? launchRestart({ ...syncOptions, ugPath: config.ugPath }) : null,
  );
  let session;
  // session.log is async, and a rejected one must not end the extension.
  const say = (message) =>
    Promise.resolve()
      .then(() => session.log(message, { level: "error" }))
      .catch(() => {});
  const tokenSource = createTokenSource(config);
  const bearerTokenProvider = withLoginHint(tokenSource, (error) => {
    const hint = hintFor(error, config);
    if (!session || !hint) return false;
    say(hint);
    return true;
  });
  const tracer = createTracer(config, tokenSource, { log: logSyncFailure });
  session = await joinSession({
    providers: buildProviders(config, bearerTokenProvider),
    models: config.models,
  });
  session.on((event) => {
    tracer?.handle(event);
    if (!isAuthRejection(event)) return;
    tokenSource.invalidate();
    // The app's own message for a 401 names Copilot's API-key settings, not Databricks sign-in.
    if (event.type === "session.error") say(config.loginHint);
  });
  tracer?.replay(() => session.getEvents());
  const launched = await restart;
  if (launched) await tellRestart(session, launched);
}
