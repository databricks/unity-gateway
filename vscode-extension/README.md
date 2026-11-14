# Unity Gateway for VS Code

Adds Databricks Unity Gateway models to the VS Code chat model picker, for Copilot Chat in Ask and Agent
mode. The extension calls the workspace's Unity Gateway directly: Anthropic Messages for Claude, Responses
for GPT-5 and later, and Chat Completions for older GPT models.

`ug configure --agents copilot` packs the extension with a `ug-config.json` that holds the workspace URL,
the models (id, picker name, family, the wire to call it on - `anthropic`, `responses` or
`completions` - and token limits), a command that prints a Databricks access token (`ug auth-token --host ...`)
and any `headers` to send with every request, then installs it. To change the models, the headers or the
workspace, run `ug configure --agents copilot` again and restart VS Code.

The models are the ones the Copilot CLI gets: the workspace's managed model list when its managed
config has one, else the models `ug configure` discovered. With `--provider <catalog.schema.name>`
they are a Model Provider Service's: the config then names the service, every request carries the
`Databricks-Model-Provider-Service` header, and OpenAI models use the gateway's `/openai/v1` route.
Managed provider wildcards also mark native models individually, so a mixed hosted/MPS list
keeps hosted OpenAI models on `/mlflow/v1` and MPS models on `/openai/v1` with their qualified IDs.
With `--model-location <catalog.schema>` they are the model services in that schema.

`headers` are the workspace's managed headers, which win by name over the extension's User-Agent, and on
a host that serves several workspaces, the selected workspace's `X-Databricks-Org-Id` and
`X-Databricks-Workspace-Id`. The extension never takes `Authorization` from the config: it signs every
request itself.

When the workspace's managed config traces Copilot, `ug-config.json` also holds `otel`: a loopback
port and path secret that `ug configure` picks once. The extension then starts with VS Code, receives
OTLP on `http://127.0.0.1:<port>/<secret>`, forwards `/v1/traces` to the gateway's
`/ai-gateway/otel/v1/traces` with a fresh token and the `headers`, and answers `/v1/logs` and
`/v1/metrics` itself. It points both of Copilot's agents at it in the user settings
(`github.copilot.chat.otel.*`, protobuf, and `chat.agentHost.otel.*`), and removes what it wrote once a
configure drops `otel`. The Copilot CLI agent's protocol is a VS Code policy, not a user setting, so that
agent sends OTLP/JSON.

When the managed config names a default model for Copilot and it is one of `models`, `ug-config.json` also
holds `defaultModel`, and the extension sets VS Code's `chat.defaultModel` to it, so new chat conversations
start on it. A `chat.defaultModel` the developer set themselves stays, and the extension removes its own
value once a configure drops `defaultModel`. Deleting the extension's value only lasts until VS Code next
starts, when the managed default is set again; to start on another model, set `chat.defaultModel` to it.

The token is kept in memory until it nears expiry, and fetched again when the gateway answers 401.
Token fetches and failed requests are logged to the **Unity Gateway** output channel.

## Development

`npm ci`, then `npm run typecheck`, `npm test` and `npm run build`. The build writes the bundle and
`package.json` to `../src/ucode/vscode/extension/`, which ug packs from; commit them (`just vscode-build`).
