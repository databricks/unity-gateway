# GitHub Copilot desktop app

`ug configure --agents copilot` also adds the workspace's Claude and GPT models to the
[GitHub Copilot desktop app](https://github.com/github/app) when it is installed. Pass
`--no-copilot-app` to skip it. A workspace's managed configuration can turn it off; `ug status`
then shows "GitHub Copilot app: off (workspace-managed)", and configure removes ug's models from
the app.

## How it works

- The app stores its model providers itself and has no import file, so ug adds them through the
  app's local API **while the app is running**. If it is closed, configure says so: open the app,
  then run `ug configure --agents copilot` again.
- ug adds two providers, **Databricks Claude** and **Databricks GPT**, with the models `ug copilot`
  offers. Open the app's model picker (or Settings > Model providers) to see them.
- Both providers point at ug's login service, the background service that also serves Codex
  desktop, on `http://127.0.0.1:<port>`. It adds a fresh Databricks token to each request, so the
  app holds no Databricks credential; the Claude provider's key is a placeholder.
- The service only answers programs on this machine that aren't a browser page. It has no other
  secret, so use it on a single-user machine.
- When your Databricks sign-in expires, the app's requests fail with "Databricks sign-in
  expired"; run `databricks auth login` and try again.
- The app supports GitHub Copilot's own models too. Only the Databricks providers go through the
  workspace; Auto, subagents, title generation, chats whose provider was removed, and remote
  (WSL) sessions use GitHub's models.

## Requirements

- GitHub Copilot app 1.4.2 or newer (a newer app gets a warning, since its local API isn't
  versioned). Signing in to GitHub is required to chat.
- Linux: glibc 2.34+ and webkit2gtk-4.1, plus a Secret Service keyring for the Claude provider's
  placeholder key.

## Removing it

`ug revert` removes ug's providers from the app (when it is running) and stops the login service
when Codex desktop doesn't need it. With the app closed, delete **Databricks Claude** and
**Databricks GPT** in its Settings > Model providers.

## Adding the providers by hand

If configure can't reach the app's local API (for example after an app update changed it), add the
providers in Settings > Model providers, with `<port>` from `ug status`:

| Provider | Type | Base URL | Key |
|---|---|---|---|
| Databricks Claude | Anthropic | `http://127.0.0.1:<port>/copilot/anthropic` | any text |
| Databricks GPT | Custom, Responses API | `http://127.0.0.1:<port>/copilot/responses/v1` | none |

Then add the models by their workspace ids, for example `system.ai.claude-sonnet-4-6`.
