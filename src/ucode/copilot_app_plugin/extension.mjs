import { joinSession } from "@github/copilot-sdk/extension";

import {
  buildProviders,
  createTokenSource,
  isAuthRejection,
  loadConfig,
  withLoginHint,
} from "./ug-plugin.mjs";

const config = loadConfig(new URL("./config.json", import.meta.url));
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
