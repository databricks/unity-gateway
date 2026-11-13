# Set up Unity Gateway on Macs with an MDM

This is the supported way to deploy Unity Gateway with an MDM. This guide is for macOS systems. Its steps use Jamf Pro; other macOS MDMs need the same
piece: a script that runs as root with the workspace URL, client id and secret as its fourth to
sixth arguments.

The setup installs `ug`, the Databricks CLI, Claude Code and Codex on managed Macs, and installs
your workspace's coding-agent policy machine-wide. Users then run `ug claude` and `ug codex` through
Unity Gateway with no sudo, signing in with their own Databricks account.

A service principal is used only while the Jamf policy runs, to download the workspace policy.

## How it works

1. A Jamf policy runs `macos-provision.sh` as root, with the workspace URL and the service
   principal's client id and secret as policy parameters.
2. The script installs the tools under `/usr/local`, exchanges the credentials for a short-lived
   token, and runs `ug configure --global`, which writes the workspace policy at
   `/etc/unitygateway/managed-settings.json`, Claude Code's managed settings and header mod under
   `/Library/Application Support/ClaudeCode/` (`managed-settings.json`, `ucode-plugins/`), and
   Codex's `/etc/codex/managed_config.toml`. Neither the credentials nor the token are written to
   disk.

   `ug` must be installed in a shared path such as `/usr/local/bin`, not under root's home
   directory: `ug configure` records the absolute path to `ug` in the machine-wide agent settings,
   and every developer's agent runs it from there to authenticate.
3. The script adds `claude` and `codex` aliases for every user's zsh and bash (in `/etc/zshrc` and
   `/etc/bashrc`), so developers launch with `claude` or `codex` in a new terminal. Each alias runs
   `UG_SKIP_OS_SETTINGS=1 ug claude` (or `ug codex`): the launch reads the installed policy, writes
   only the developer's own settings, and authenticates as the developer.

## Before you start

**Databricks workspace**

- Unity Gateway coding agents are configured for the workspace, on its Coding agents page
  (`/ml/ai-gateway/governance/coding-agents`), with Claude Code, Codex or both enabled. Claude Code
  needs Claude models (`system.ai.claude-*`); `ug configure` fails if no enabled agent has models it
  can use.
- You can create a service principal and an OAuth secret for it (workspace admin).

**Macs**

- macOS 12 or later, enrolled in Jamf Pro.
- Network access to the workspace URL, PyPI (`pypi.org`, `files.pythonhosted.org`), GitHub
  (`github.com`, `raw.githubusercontent.com`), `astral.sh` and `downloads.claude.ai`. Check DNS or
  proxy blocklists for these.
- The Command Line Tools, or permission for the script to install them through `softwareupdate`
  (about 1 GB). `uv` needs `git` to install `ug`.

## 1. Create the service principal

1. In the workspace, go to **Settings → Identity and access → Service principals → Add service
   principal → Add new**, and name it, for example, `ug-jamf-provisioning`.
2. On its **Configurations** tab, leave only **Workspace access** enabled. It needs no other
   entitlements: the policy run only reads the coding-agent config and lists models.
3. Grant it access to the models, MCP servers, and skills in the published configuration, including
   the Unity Catalog privileges they need. Without this, `ug configure` finds no models and
   configures no agents.
4. On its **Secrets** tab, choose **Generate secret** with a short lifetime. Under **Scope**, choose
   **Other APIs**, add the `ai-gateway` and `unity-catalog` scopes, and set each to **Read**. Copy
   the client id and secret; you can't view the secret again after you close the dialog.
5. Check that it can see your models. With a token for the service principal:

   ```bash
   curl -sS -H "Authorization: Bearer $TOKEN" \
     https://<workspace>/ai-gateway/anthropic/v1/models | head -c 300
   ```

   An empty `data` list means `ug configure` won't find Claude models for Claude Code.

## 2. Add the script

1. Go to **Settings → Computer Management → Scripts → New** and name it `Unity Gateway provision`.
2. On the **Script** tab, paste `macos-provision.sh`. Copy it from the file (for example
   `pbcopy < macos-provision.sh`) rather than from a browser or chat, which can drop text from long
   lines.
3. On the **Options** tab, label **Parameter 4** `Workspace URL`, **Parameter 5** `Client ID` and
   **Parameter 6** `Client secret`.
4. Save.

The script installs Claude Code, Codex and the Databricks CLI only when they're missing, and
reinstalls `ug` on each run. Codex comes from its release package, which includes the
`codex-code-mode-host` helper that Code Mode needs: the package goes in
`/usr/local/unity-gateway/codex`, with `codex` and `codex-code-mode-host` linked into
`/usr/local/bin`. A Mac with an older Codex install outside that folder gets the package on the next
run.
The script doesn't update the agents or the CLI after the first install; to keep them current,
modify the script to match how you update software on your Macs.

## 3. Create the policy

Go to **Computers → Policies → New**:

| Setting | Value |
| --- | --- |
| **General → Trigger** | Recurring Check-in |
| **General → Execution Frequency** | Once every day, to keep the installed policy current |
| **Scripts** | `Unity Gateway provision`, Priority: After, with parameters 4-6 set (below) |
| **Scope** | Your developer Macs; start with one test Mac |

| Parameter | Value |
| --- | --- |
| 4 | Workspace URL, starting with `https://` |
| 5 | The service principal's client id |
| 6 | The service principal's secret |

## 4. Verify on a test Mac

After the policy runs (check the computer's **History → Policy Logs**), as a user, with no sudo:

```bash
ls -l /usr/local/bin/{ug,databricks,claude,codex,codex-code-mode-host}
cat /etc/unitygateway/managed-settings.json      # your workspace's policy
cat "/Library/Application Support/ClaudeCode/managed-settings.json" /etc/codex/managed_config.toml
alias claude codex                               # in a new terminal: the UG aliases
claude
```

The first launch opens a browser so the developer can sign in to Databricks.

## 5. Tell developers how to launch

Open a new terminal and run `claude` or `codex`. The script's aliases, in a block between
`# BEGIN unity-gateway` and `# END unity-gateway` in `/etc/zshrc` and `/etc/bashrc`, expand them to:

```bash
alias claude='UG_SKIP_OS_SETTINGS=1 ug claude'
alias codex='UG_SKIP_OS_SETTINGS=1 ug codex'
```

Each policy run rewrites that block, which restores it if a macOS update resets the file. Until
then, `claude` and `codex` start the agents directly, which still reach Unity Gateway through the
machine-wide settings the policy installed; they only miss ug's launch features, such as smart
routing, so no extra trigger is needed.

The aliases apply only to interactive zsh and bash. In scripts and other shells, use
`UG_SKIP_OS_SETTINGS=1 ug claude` (or `ug codex`) directly. Without the variable, a launch may try to
update the machine-wide settings with sudo and stop. To run Claude Code itself without ug, use
`command claude`: `command` is the shell built-in that skips aliases and runs the `claude` binary.

## Rotate the secret

1. Generate a new secret for the service principal.
2. Update parameter 6 in the policy.
3. Delete the old secret in Databricks once the policy has run on your Macs.

## Troubleshooting

| Policy log shows | Cause |
| --- | --- |
| `Token request to … failed: {"error":"invalid_client"…}` | Wrong client id or secret in parameters 5 and 6, or the secret expired. |
| `Token request … failed: … Scopes … are not assigned to the client` | The secret lacks the `ai-gateway:read` and `unity-catalog:read` scopes. |
| `Token request … failed` with an HTTP 403 page | The workspace blocks the Mac's network (IP access list or private link). |
| `Connection refused` or a timeout fetching PyPI or GitHub | A network or DNS block on the Mac. See the access list in "Before you start". |
| `None of the coding agents enabled by your workspace configuration are available` | No enabled agent has models the service principal can use, for example Claude Code configured with non-Claude models. |
| `could not read Username for 'https://github.com'` | The `ug` repository URL in the script was cut off when pasted. |

## Security notes

- Jamf passes policy parameters to the script as command-line arguments, so **any local user can
  read the client id and secret with `ps` while the script runs**. Jamf admins who can view the
  policy can read them too. The script itself keeps the secret off `curl`'s command line and never
  writes the credentials or the token to disk.
- **Limit the damage of a leak:** give the service principal only workspace access and a
  short-lived secret, and rotate it.
- Don't deliver the credentials in a configuration profile: local users can read
  `/Library/Managed Preferences`.
