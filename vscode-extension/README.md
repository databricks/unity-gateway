# Unity Gateway for VS Code

Adds Databricks AI Gateway models to the VS Code chat model picker, for Copilot Chat in Ask and Agent
mode. The extension calls the workspace's AI Gateway directly: Anthropic Messages for Claude, Responses
for GPT-5 and later, and Chat Completions for older GPT models.

`ug configure --agents vscode` packs the extension with a `ug-config.json` that holds the workspace URL,
the models (id, picker name, family, `anthropic_messages`, `responses` or `chat_completions`, token
limits) and a command that prints a Databricks access token (`ug auth-token --host ...`), then installs
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
