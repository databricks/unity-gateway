# Run coding agents in Databricks Sandbox

`ug cloud` runs the whole Claude or Codex process remotely while you use your local terminal.
It uses your configured ug workspace, or an explicit `--profile` / `--workspace` before the agent.
Requires a Sandbox-capable Databricks CLI, OpenSSH, and a Sandbox image with ug and the agent.

| Command | Behavior |
|---|---|
| `ug cloud claude` | Start a new Claude conversation in a new Sandbox; no session picker |
| `ug cloud codex` | Start a new Codex conversation in a new Sandbox; no session picker |
| `ug cloud --resume claude` | Resume the last Claude conversation in its retained Sandbox |
| `ug cloud --resume codex` | Resume the latest Codex conversation in the last retained Sandbox |
| `ug cloud --sandbox ID claude` | Start a new Claude conversation in an existing Sandbox |
| `ug cloud claude -p "task"` | Run headlessly; delete the newly created Sandbox when finished |
| `ug cloud codex exec "task"` | Run Codex headlessly with the same cleanup behavior |
| `ug cloud --sandbox ID claude --resume UUID` | Resume an exact Claude conversation |
| `ug cloud --sandbox ID codex resume --last` | Resume Codex in an existing Sandbox |
| `ug cloud list` | List your Sandboxes |
| `ug cloud ssh ID` | Open a remote shell |
| `ug cloud delete ID` | Delete with confirmation; add `--yes` for unattended cleanup |

Cloud options go before `claude`/`codex`; native agent arguments go after it.
Use `--directory /absolute/remote/path` to select a remote project. Local files are not uploaded.

Interactive Sandboxes are retained. A plain launch always creates a new Sandbox and conversation.
Pass `--resume` before the agent to reopen the last conversation directly, or `--sandbox ID`
to start a new conversation in a specific environment. No session picker is shown. A missing
previous Sandbox or conversation produces an error instead of silently creating a new one.
Startup shows brief progress and the Sandbox ID; routine remote ug setup messages are hidden.
Warnings and prompts remain visible. Resume/delete instructions print once, on exit.
Claude uses an exact session ID for sessions created by this launcher; Codex currently uses `resume --last`. The cloud `--resume` shortcut is
interactive-only and cannot be combined with `--sandbox` or native resume arguments.

New headless Sandboxes, including their files, are deleted after success or failure. Sandboxes
explicitly selected with `--sandbox` are never auto-deleted. Save stdout/stderr locally or reuse a
Sandbox when you need to retain files: automatic artifact/transcript export is not included.

The launcher uses the Sandbox's service-mounted credential and repairs missing ug auth configuration;
it does not upload your credentials or ask you to complete remote browser login. Quota rejections
stop with the service's configured limit. Terminal capabilities and initial dimensions are forwarded;
native onboarding and trust prompts remain. Live resize, detached/background runs, disconnect
survival, shared-host workspace routing and automatic project sync are outside this initial scope.

See [implementation details](cloud-preview.md).

[Watch the recorded CLI journeys](https://github.com/databricks/unity-gateway/pull/1689#recorded-cli-journeys).
