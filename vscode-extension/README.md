# Unity Gateway for VS Code

Adds Databricks AI Gateway models to the VS Code chat model picker, for Copilot Chat in Ask and Agent
mode. The extension calls the workspace's AI Gateway directly: Chat Completions for Claude and open
models, Responses for GPT-5 and later.

`ug configure` installs it and writes its settings:

| Setting | Value |
|---|---|
| `unityGateway.workspace` | The workspace URL |
| `unityGateway.models` | `[{"id": "<model service>", "api": "chat_completions" \| "responses"}]` |
| `unityGateway.authCommand` | A command that prints a Databricks access token (`ug auth-token --host ...`) |

The token is kept in memory until it nears expiry, and fetched again when the gateway answers 401.
Run **Unity Gateway: Refresh Models** after changing the settings by hand. Token fetches and failed
requests are logged to the **Unity Gateway** output channel.

## Development

`npm ci`, then `npm run typecheck`, `npm test` and `npm run build`. The build writes the bundle and
`package.json` to `../src/ucode/vscode/extension/`, which ug packs from; commit them (`just vscode-build`).
