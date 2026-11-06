# MDM / JAMF deployment for Unity Gateway

MDM deployment runs `ug configure` as root on each machine.

| Platform | Script | Setup guide |
| --- | --- | --- |
| macOS | `macos-provision.sh` | [macos-setup.md](macos-setup.md) |
| Linux | `linux-provision.sh` | [Linux](#linux) below |

A Windows script will follow.

On macOS, `macos-provision.sh` is an MDM policy script (written for JAMF), run as root on each
machine. It installs `ug` (with its own Python), the Databricks CLI, Claude Code and Codex under
`/usr/local`, where every user can run them, then runs `ug configure` as root, which writes these
root-owned files:

| File | macOS | Linux |
| --- | --- | --- |
| Workspace policy | `/etc/unitygateway/managed-settings.json` | same |
| Claude Code managed settings | `/Library/Application Support/ClaudeCode/managed-settings.json` | `/etc/claude-code/managed-settings.json` |
| Claude Code header mod | `/Library/Application Support/ClaudeCode/ucode-plugins/` | `/etc/claude-code/ucode-plugins/` |
| Codex managed config | `/etc/codex/managed_config.toml` | same |

Developers then launch with `UG_SKIP_OS_SETTINGS=1 ug claude`: no sudo, and inference uses their
own OAuth.

The script authenticates `ug configure` with a short-lived token for a service principal whose
client id and secret come in as policy parameters; neither the credentials nor the token are
written to disk. See [macos-setup.md](macos-setup.md) for the full setup.

## Linux

`linux-provision.sh` does the same as the macOS script for an MDM or configuration management tool
that runs it as root. It takes the same arguments (`$4` workspace URL, `$5` client id, `$6` client
secret; `$1`-`$3` are ignored), installs the same tools under `/usr/local`, and writes the Linux
paths above. It installs any missing `curl`, `git`, `python3`, `tar` or `unzip` with `apt-get` or
`dnf`. For example:

```bash
sudo bash linux-provision.sh _ _ _ https://myws.cloud.databricks.com "$CLIENT_ID" "$CLIENT_SECRET"
```

The client secret is visible to other local users in the process list while the script runs, as
with any tool that passes it as an argument. The service principal setup in
[macos-setup.md](macos-setup.md) applies unchanged.

`tests/e2e_mdm` runs both scripts in CI. It sets `UG_INSTALL_SPEC` to install a checkout instead of
the repository's main branch.
