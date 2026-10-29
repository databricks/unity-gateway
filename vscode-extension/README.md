# Unity Gateway for VS Code

Adds Databricks AI Gateway models to the VS Code chat model picker, for Copilot Chat in Ask and Agent
mode. The extension calls the workspace's AI Gateway directly: Chat Completions for Claude, Responses
for GPT-5 and later.

`ug configure --agents vscode` packs the extension with a `ug-config.json` that holds the workspace URL,
the models (id, picker name, `chat_completions` or `responses`, token limits) and a command that prints
a Databricks access token (`ug auth-token --host ...`), then installs it. To change the models or the
workspace, run `ug configure --agents vscode` again and restart VS Code.

The token is kept in memory until it nears expiry, and fetched again when the gateway answers 401.
Token fetches and failed requests are logged to the **Unity Gateway** output channel.

## Development

`npm ci`, then `npm run typecheck`, `npm test` and `npm run build`. The build writes the bundle and
`package.json` to `../src/ucode/vscode/extension/`, which ug packs from; commit them (`just vscode-build`).
