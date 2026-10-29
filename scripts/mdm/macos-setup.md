# Set up Unity Gateway on Macs with an MDM, for developers without admin rights

This guide is for macOS. Its steps use Jamf Pro; other macOS MDMs need the same pieces (a package,
and a script that runs as root with the workspace URL as its fourth argument). Linux and Windows
setups will follow separately.

The setup installs `ug`, the Databricks CLI, Claude Code and Codex on managed Macs, and installs
your workspace's coding-agent policy machine-wide. Developers then run coding agents through Unity
Gateway without sudo, signing in with their own Databricks account.

A service principal is used only while the Jamf policy runs, to download the workspace policy. Its
credentials reach each Mac in a package that installs them readable only by root.

## How it works

1. A Jamf policy installs a credentials package, which puts the service principal's client id and
   secret in `/Library/Application Support/UnityGateway/provision.env` (owner root, mode 600).
2. The same policy runs `macos-provision.sh` as root. It installs the tools under `/usr/local`,
   exchanges the credentials for a short-lived token, and runs `ug configure`, which installs the
   workspace policy at `/etc/unitygateway/managed-settings.json`.
3. Developers launch with `UG_SKIP_OS_SETTINGS=1 ug claude` (or `ug codex`). The launch reads the
   installed policy, writes only the developer's own settings, and authenticates as the developer.

## Before you start

**Databricks workspace**

- Unity Gateway coding agents are configured for the workspace, on its Coding agents page
  (`/ml/ai-gateway/governance/coding-agents`), with Claude Code, Codex or both enabled. Claude Code needs Claude models
  (`system.ai.claude-*`); `ug configure` fails if no enabled agent has models it can use.
- You can create a service principal and an OAuth secret for it (workspace admin).

**Macs**

- macOS 12 or later, enrolled in Jamf Pro.
- Network access to the workspace URL, PyPI (`pypi.org`, `files.pythonhosted.org`), GitHub
  (`github.com`, `raw.githubusercontent.com`), `astral.sh` and `downloads.claude.ai`. Check DNS or
  proxy blocklists for these.
- The Command Line Tools, or permission for the script to install them through `softwareupdate`
  (about 1 GB). `uv` needs `git` to install `ug`.

**Your workstation**

- A Mac, to build the credentials package with `pkgbuild` (no sudo needed).

## 1. Create the service principal

1. In the workspace, go to **Settings → Identity and access → Service principals → Add service
   principal → Add new**, and name it, for example, `ug-jamf-provisioning`.
2. On its **Configurations** tab, leave only **Workspace access** enabled. Grant nothing else: the
   policy run only reads the coding-agent config and lists models.
3. On its **Secrets** tab, choose **Generate secret** with a short lifetime, and copy the client id
   and secret. The secret is shown only once.
4. Check that it can see your models. With a token for the service principal:

   ```bash
   curl -sS -H "Authorization: Bearer $TOKEN" \
     https://<workspace>/ai-gateway/anthropic/v1/models | head -c 300
   ```

   An empty `data` list means `ug configure` won't find Claude models for Claude Code.

## 2. Build the credentials package

On your Mac, in an empty folder. `read -rs` takes the secret without echoing it or saving it to
your shell history:

```bash
umask 077 && mkdir -p creds/root
read -rs "secret?SP secret: "; echo
printf 'UG_CLIENT_ID=%s\nUG_CLIENT_SECRET=%s\n' '<client-id>' "$secret" > creds/root/provision.env
unset secret
chmod 700 creds/root && chmod 600 creds/root/provision.env
pkgbuild --root creds/root --install-location "/Library/Application Support/UnityGateway" \
  --ownership recommended --identifier com.databricks.unitygateway.credentials --version 1 \
  creds/UnityGatewayCredentials-1.pkg
```

Check the permissions recorded in the package:

```bash
pkgutil --expand creds/UnityGatewayCredentials-1.pkg /tmp/ugcreds-x
lsbom /tmp/ugcreds-x/Bom && rm -rf /tmp/ugcreds-x
```

You should see `.  40700  0/0` and `./provision.env  100600  0/0`.

## 3. Upload the package

1. In Jamf Pro, go to **Settings → Computer Management → Packages → New**, upload
   `UnityGatewayCredentials-1.pkg` and save.
2. Delete your local copy: the package holds the secret in plain text.

```bash
rm -rf creds
```

## 4. Add the script

1. Go to **Settings → Computer Management → Scripts → New** and name it `Unity Gateway provision`.
2. On the **Script** tab, paste `macos-provision.sh`. Copy it from the file (for example
   `pbcopy < macos-provision.sh`) rather than from a browser or chat, which can drop text from long
   lines.
3. On the **Options** tab, label **Parameter 4** `Workspace URL`.
4. Save.

## 5. Create the policy

Go to **Computers → Policies → New**:

| Setting | Value |
| --- | --- |
| **General → Trigger** | Recurring Check-in |
| **General → Execution Frequency** | Once every day, to keep the installed policy current |
| **Packages** | `UnityGatewayCredentials-1.pkg`, Action: Install |
| **Scripts** | `Unity Gateway provision`, Priority: After, Parameter 4: your workspace URL, e.g. `https://myws.cloud.databricks.com` |
| **Scope** | Your developer Macs; start with one test Mac |

Jamf installs the package before it runs the script. Don't put the client id or secret in any
policy parameter.

## 6. Verify on a test Mac

After the policy runs (check the computer's **History → Policy Logs**), as a standard user:

```bash
cat "/Library/Application Support/UnityGateway/provision.env"   # Permission denied
which ug databricks claude codex                               # /usr/local/bin/...
cat /etc/unitygateway/managed-settings.json                    # your workspace's policy
UG_SKIP_OS_SETTINGS=1 ug claude
```

The first launch opens a browser so the developer can sign in to Databricks.

## 7. Tell developers how to launch

```bash
UG_SKIP_OS_SETTINGS=1 ug claude
UG_SKIP_OS_SETTINGS=1 ug codex
```

To avoid typing the variable, they can add `export UG_SKIP_OS_SETTINGS=1` to `~/.zshrc`. Without it,
a launch may try to update the machine-wide settings with sudo and stop.

## Rotate the secret

1. Generate a new secret for the service principal.
2. Build the package again with `--version 2` (step 2) and upload it.
3. Replace the package in the policy, then delete the old secret in Databricks once the policy has
   run on your Macs.

## Troubleshooting

| Policy log shows | Cause |
| --- | --- |
| `Missing or unprotected …/provision.env` | The package isn't in the policy, or its file isn't root-owned with mode 600. Check step 2's `lsbom` output. |
| `Token request to … failed: {"error":"invalid_client"…}` | Wrong client id or secret, or the secret expired. Rebuild the package. |
| `Token request … failed` with an HTTP 403 page | The workspace blocks the Mac's network (IP access list or private link). |
| `Connection refused` or a timeout fetching PyPI or GitHub | A network or DNS block on the Mac. See the access list in "Before you start". |
| `None of the coding agents enabled by your workspace configuration are available` | No enabled agent has models the service principal can use, for example Claude Code configured with non-Claude models. |
| `could not read Username for 'https://github.com'` | The `ug` repository URL in the script was cut off when pasted. |

## Security notes

- **Protected from:** developers without admin rights. The credentials file is readable only by
  root, and the script keeps the secret off every command line, so `ps` never shows it.
- **Not protected from:** anyone with sudo on the Mac, Jamf admins who can download the package, and
  anyone with the disk of a Mac without FileVault.
- **Limit the damage of a leak:** give the service principal only workspace access and a
  short-lived secret, and rotate it.
- Don't deliver the credentials as Jamf script parameters (any local user can read them with `ps`
  while the script runs) or in a configuration profile (local users can read
  `/Library/Managed Preferences`).
