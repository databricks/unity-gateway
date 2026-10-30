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
   token, and runs `ug configure`, which installs the workspace policy at
   `/etc/unitygateway/managed-settings.json`. Neither the credentials nor the token are written to
   disk.
3. Developers launch with `UG_SKIP_OS_SETTINGS=1 ug claude` (or `ug codex`). The launch reads the
   installed policy, writes only the developer's own settings, and authenticates as the developer.

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
2. On its **Configurations** tab, leave only **Workspace access** enabled. Grant nothing else: the
   policy run only reads the coding-agent config and lists models.
3. On its **Secrets** tab, choose **Generate secret** with a short lifetime, and copy the client id
   and secret. The secret is shown only once. The script requests the `all-apis` OAuth scope, so
   the client needs it.
4. Check that it can see your models. With a token for the service principal:

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
| 4 | Workspace URL, e.g. `https://myws.cloud.databricks.com` |
| 5 | The service principal's client id |
| 6 | The service principal's secret |

## 4. Verify on a test Mac

After the policy runs (check the computer's **History → Policy Logs**), as a user, with no sudo:

```bash
which ug databricks claude codex                 # /usr/local/bin/...
cat /etc/unitygateway/managed-settings.json      # your workspace's policy
UG_SKIP_OS_SETTINGS=1 ug claude
```

The first launch opens a browser so the developer can sign in to Databricks.

## 5. Tell developers how to launch

```bash
UG_SKIP_OS_SETTINGS=1 ug claude
UG_SKIP_OS_SETTINGS=1 ug codex
```

To avoid typing the variable, they can add `export UG_SKIP_OS_SETTINGS=1` to `~/.zshrc`. Without it,
a launch may try to update the machine-wide settings with sudo and stop.

## Rotate the secret

1. Generate a new secret for the service principal.
2. Update parameter 6 in the policy.
3. Delete the old secret in Databricks once the policy has run on your Macs.

## Troubleshooting

| Policy log shows | Cause |
| --- | --- |
| `Token request to … failed: {"error":"invalid_client"…}` | Wrong client id or secret in parameters 5 and 6, or the secret expired. |
| `Token request … failed: … Scopes … are not assigned to the client` | The client lacks the `all-apis` OAuth scope. |
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
