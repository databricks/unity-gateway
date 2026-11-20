import { joinSession } from "@github/copilot-sdk/extension";

import { buildProviders, createTokenSource, loadConfig, withLoginHint } from "./ug-plugin.mjs";

const config = loadConfig(new URL("./config.json", import.meta.url));
let session;
const bearerTokenProvider = withLoginHint(createTokenSource(config), () =>
  session?.log(config.loginHint, { level: "error" }),
);
session = await joinSession({
  providers: buildProviders(config, bearerTokenProvider),
  models: config.models,
});
