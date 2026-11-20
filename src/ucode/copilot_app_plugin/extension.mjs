import { fileURLToPath } from "node:url";

import { joinSession } from "@github/copilot-sdk/extension";

import { syncInBackground } from "./app-rows.mjs";
import {
  buildProviders,
  createTokenSource,
  isAuthRejection,
  loadConfig,
  withLoginHint,
} from "./ug-plugin.mjs";

const config = loadConfig(new URL("./config.json", import.meta.url));
const syncOptions = { stateDir: fileURLToPath(new URL(".", import.meta.url)) };
const logSyncFailure = (message) => console.error(message);
const firstSync = syncInBackground(config, syncOptions, logSyncFailure);
let session;
const tokenSource = createTokenSource(config);
const bearerTokenProvider = withLoginHint(tokenSource, () =>
  session?.log(config.loginHint, { level: "error" }),
);
session = await joinSession({
  providers: buildProviders(config, bearerTokenProvider),
  models: config.models,
});
session.on((event) => {
  if (isAuthRejection(event)) tokenSource.invalidate();
});
// The extension can start before the app publishes its WebSocket endpoint.
if (["not_running", "failed"].includes(await firstSync)) {
  await syncInBackground(config, syncOptions, logSyncFailure);
}
