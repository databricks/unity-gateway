# Unity Gateway for VS Code

Adds Databricks AI Gateway models to the VS Code chat model picker, for Copilot Chat in Ask and Agent
mode. The extension calls the workspace's AI Gateway directly: Anthropic Messages for Claude, Responses
for GPT-5 and later, and Chat Completions for older GPT models.

`ug configure --agents vscode` packs the extension with a `ug-config.json` that holds the workspace URL,
the models (id, picker name, family, the wire to call it on - `anthropic`, `responses` or
`completions` - and token limits) and a command that prints a Databricks access token (`ug auth-token --host ...`), then installs
it. To change the models or the workspace, run `ug configure --agents vscode` again and restart VS Code.

The models are the workspace's own (`system.ai`) by default. With `--provider <catalog.schema.name>`
they are a Model Provider Service's: the config then names the service, every request carries the
`Databricks-Model-Provider-Service` header, and OpenAI models use the gateway's `/openai/v1` route.
With `--model-location <catalog.schema>` they are the model services in that schema.

The token is kept in memory until it nears expiry, and fetched again when the gateway answers 401.
Token fetches and failed requests are logged to the **Unity Gateway** output channel.

## Development

`npm ci`, then `npm run typecheck`, `npm test` and `npm run build`. The build writes the bundle and
`package.json` to `../src/ucode/vscode/extension/`, which ug packs from; commit them (`just vscode-build`).

`npm run live` sends the requests this extension builds to a real AI Gateway and checks the replies,
covering what a mocked test can't: the gateway accepting each route's request shape, tool calls,
images inside tool results, prompt caching, and that a Model Provider Service's models need its
routing header. It takes the workspace and models from the environment:

```
UG_LIVE_WORKSPACE=https://<workspace> \
UG_LIVE_AUTH_COMMAND='ug auth-token --host https://<workspace>' \
UG_LIVE_MPS= UG_LIVE_OPENAI_MPS= \
npm run live
```

CI runs it as the last step of the `VS Code extension` job, from `UCODE_TEST_WORKSPACE` and
`DATABRICKS_BEARER`; with neither set the suite skips itself, so a fork's pull request stays green.
`UG_LIVE_WORKSPACE` and `UG_LIVE_TOKEN` override them for a one-off local run.

The models and Model Provider Services default to the ones the integration CUJs use, so CI passes
only the two secrets. Any of `UG_LIVE_CLAUDE_MODEL`, `UG_LIVE_GPT_MODEL`, `UG_LIVE_MPS`,
`UG_LIVE_MPS_MODEL`, `UG_LIVE_OPENAI_MPS` and `UG_LIVE_OPENAI_MPS_MODEL` overrides one; setting it to
the empty string leaves that case out, which is what another workspace needs for the services it
doesn't have. Each case needs its model or service to be set; the rest are left out. A run costs a few cents.
