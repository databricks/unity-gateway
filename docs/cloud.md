# Run coding agents in Databricks Sandbox

`ug cloud` runs the whole Claude or Codex process remotely while you use your local terminal.
It uses your configured ug workspace, or an explicit `--profile` / `--workspace` before the agent.
Requires a Sandbox-capable Databricks CLI, OpenSSH, and a Sandbox image with ug and the agent.

| Command | Behavior |
|---|---|
| `ug cloud claude` | Start interactive Claude; offer previous-session reuse when available |
| `ug cloud codex` | Start interactive Codex; reuse can select its latest conversation |
| `ug cloud claude -p "task"` | Run headlessly; delete the newly created Sandbox when finished |
| `ug cloud codex exec "task"` | Run Codex headlessly with the same cleanup behavior |
| `ug cloud --sandbox ID claude --resume UUID` | Resume an exact Claude conversation |
| `ug cloud --sandbox ID codex resume --last` | Resume Codex in an existing Sandbox |
| `ug cloud list` | List your Sandboxes |
| `ug cloud ssh ID` | Open a remote shell |
| `ug cloud delete ID` | Delete with confirmation; add `--yes` for unattended cleanup |

Cloud options go before `claude`/`codex`; native agent arguments go after it.
Use `--directory /absolute/remote/path` to select a remote project. Local files are not uploaded.

Interactive Sandboxes are retained. Subsequent launches offer resume, a fresh conversation in
that Sandbox, or a fresh Sandbox. Resume/delete instructions print on exit. Claude uses an exact
session ID for sessions created by this launcher; Codex currently uses `resume --last`.

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
