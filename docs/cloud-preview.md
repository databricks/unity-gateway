# Cloud launch preview

Run the entire Claude or Codex agent in a Databricks Sandbox while using your local terminal.
This preview supports Linux and macOS clients with OpenSSH and a Databricks CLI that includes
`sandbox create/register/ssh/delete/list`. Authenticate the client first. The Sandbox image must
provide ug, the selected harness, and its workspace authentication. Local credentials are not copied.

Cloud options go **before** the agent name. Agent arguments go after it:

```bash
ug cloud --profile my-workspace claude
ug cloud --profile my-workspace codex
ug cloud --profile my-workspace claude -p "Run Python to calculate 10 + 20 + 30"
ug cloud --profile my-workspace codex exec --skip-git-repo-check "Calculate 10 + 20 + 30"
```

You can use `--workspace URL` instead of `--profile`. With neither, ug uses `UG_WORKSPACE` or
its configured workspace. An explicit profile and workspace must agree. Shared-host workspace
selection is not supported by this preview. Workspace selectors must not silently select a different workspace.

The launcher registers the existing/generated Sandbox SSH key, creates a Sandbox, prints its ID,
creates `/home/sandbox-agent/project`, and starts remote ug. Interactive launches use OpenSSH plus remote util-linux `script` to allocate a PTY;
Claude `-p` and Codex `exec` use a non-PTY SSH connection. The SSH/agent exit status reaches your shell.
Setup messages go to stderr. Remote ug/harness output is forwarded without filtering.

## Return to the same environment

```bash
ug cloud --profile my-workspace list
ug cloud --profile my-workspace --sandbox <id> claude --continue
ug cloud --profile my-workspace --sandbox <id> codex resume --last
ug cloud --profile my-workspace ssh <id>
ug cloud --profile my-workspace delete <id>
```

Deletion asks for confirmation; add `--yes` to skip it. Existing Sandboxes are never implicitly
deleted. New Sandboxes for headless Claude `-p` / Codex `exec` or `review` runs are deleted
automatically after success, failure, or a handled interrupt, including their files. Use
`--sandbox <id>` to run headlessly in an environment you want to keep. Interactive
Sandboxes are retained. Cleanup failures print the Sandbox ID and deletion command. If creation is interrupted before returning an ID, inspect `ug cloud ... list`.

## Files and limitations

No local project or credentials are uploaded. Use the remote shell to clone a repository or prepare
files, then launch with `--sandbox <id> --directory /absolute/remote/path` before the agent name.
No automatic file sync, artifact download, detached lifecycle, or general run/session inventory
is included. The local last-session shortcut described below supports interactive reuse.
The remote image must also include util-linux `script` for interactive sessions. Terminal size is
set at launch; live resize propagation is not implemented. Native resume/background behavior depends on the installed harness; disconnect survival is not
established by the headless tests. Approval policies remain the harness's; unattended tasks may
need explicit allowed tools. This is an SSH launcher, not local-agent Sandbox MCP routing.

## Terminal and Sandbox wrapper handling

Interactive sessions inherit the client's TERM, COLORTERM, COLORFGBG, TERM_PROGRAM,
and TERM_PROGRAM_VERSION plus initial rows/columns. Only these display variables are
forwarded; credentials and other local environment variables are not copied. Missing
TERM falls back to xterm-256color. Live resizing remains unsupported.

The launcher sets the Sandbox image's `_UCODE_DISPATCH_ACTIVE=1` guard for the remote
ug process, preventing the image's Claude/Codex wrappers from entering ug a second time.
The outer ug still performs Gateway configuration normally.

Claude's first-run onboarding and project trust prompts are not auto-accepted. Terminal
capability forwarding does not itself suppress that onboarding. Choose `/theme` → auto
in Claude to follow your terminal background; reuse the Sandbox to retain its preferences.

Sandbox creation detects the service quota rejection during native CLI retries and stops
with the configured limit and recovery options. Debug output is inspected in memory only;
it is not printed or saved. Creation has a 300-second timeout.

## Interactive resume picker

After an interactive launch, `ug cloud claude` in the same workspace/profile/directory
checks the last Sandbox and offers: resume the conversation, start a new conversation
in that Sandbox, or create a new Sandbox. Explicit `--sandbox` and native resume flags
bypass the picker. Headless jobs never use the picker.

New Claude conversations get a native session UUID; resume targets that exact UUID.
If onboarding ended before a transcript was written, only new-session/recreate choices
are offered. Codex uses its native `resume --last` and labels the option as the latest
conversation. Remote conversation contents stay in the Sandbox; local metadata in
`~/.ucode/cloud-sessions/` contains only Sandbox/session IDs and the last-opened timestamp.
The shortcut is scoped to workspace, authentication profile, agent, and working directory.

Resume/delete commands print before launch and after a normal remote exit. If SSH
breaks, the exit message may not arrive; the saved picker shortcut is available next time.
Deleting/expiring a Sandbox removes its remote files and conversations; ug checks for
its existence before offering resume. Session retention across Sandbox service expiration
is not guaranteed. Previously created Sandboxes can be registered by launching once with
`--sandbox <id>`; this does not import older conversations into an exact-ID shortcut.

## Sandbox authentication

Before agent launch, ug checks that the Sandbox DEFAULT profile has a service-provided
token for the selected workspace. If remote ug has lost its PAT-mode configuration,
the launcher repairs it using `ug configure --profile DEFAULT --agents <agent> --use-pat`.
It rechecks the saved configuration before launching. This uses the credential already
inside the Sandbox; local credentials are not uploaded. Missing/mismatched credentials
stop with an actionable error instead of offering browser login inside the Sandbox.
