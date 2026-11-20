import { fileURLToPath } from "node:url";

import { joinSession } from "@github/copilot-sdk/extension";

import { removeInBackground } from "./app-remove.mjs";
import { launchRestart, tellRestart } from "./app-restart.mjs";
import { syncInBackground } from "./app-rows.mjs";
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
const retry = (result) => ["not_running", "failed"].includes(result);

if (config.remove) {
  const firstRemove = removeInBackground(config, syncOptions, logSyncFailure);
  try {
    await joinSession();
  } catch (error) {
    logSyncFailure(`Databricks models: couldn't join the session: ${error?.message ?? error}`);
  }
  if (retry(await firstRemove)) await removeInBackground(config, syncOptions, logSyncFailure);
} else {
  const firstSync = syncInBackground(config, syncOptions, logSyncFailure);
  const restartFor = (synced) => (synced === "changed" ? launchRestart(syncOptions) : null);
  const firstRestart = firstSync.then(restartFor);
  let session;
  const tokenSource = createTokenSource(config);
  const bearerTokenProvider = withLoginHint(tokenSource, (error) => {
    const hint = hintFor(error, config);
    if (!session || !hint) return false;
    session.log(hint, { level: "error" });
    return true;
  });
  session = await joinSession({
    providers: buildProviders(config, bearerTokenProvider),
    models: config.models,
  });
  session.on((event) => {
    if (!isAuthRejection(event)) return;
    tokenSource.invalidate();
    // The app's own message for a 401 names Copilot's API-key settings, not Databricks sign-in.
    if (event.type === "session.error") session.log(config.loginHint, { level: "error" });
  });
  let restart = await firstRestart;
  if (retry(await firstSync)) restart = await restartFor(await syncInBackground(config, syncOptions, logSyncFailure));
  if (restart) await tellRestart(session, restart);
}
