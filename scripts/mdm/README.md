# MDM / JAMF deployment for Unity Gateway

MDM deployment runs `ug configure` as root on each machine.

| Platform | Script | Setup guide |
| --- | --- | --- |
| macOS | `macos-provision.sh` | [macos-setup.md](macos-setup.md) |
| Linux | `linux-provision.sh` | [Linux](#linux) below |

A Windows script will follow.

On macOS, `macos-provision.sh` is an MDM policy script (written for JAMF), run as root on each
machine. It installs `ug` (with its own Python), the Databricks CLI and Node.js/npm under
`/usr/local`, where every user can run them, then runs `ug configure --global --mdm` as root.
Unity Gateway's built-in npm installer supplies missing Claude Code and Codex binaries in the same
shared prefix. Configure installs the workspace policy and `mdm_os: Mac` at
`/etc/unitygateway/managed-settings.json`. The script also adds `claude` and `codex`
aliases (for `UG_SKIP_OS_SETTINGS=1 ug claude` / `ug codex`) to `/etc/zshrc` and `/etc/bashrc`, so
developers launch with `claude` or `codex` in a new terminal: no sudo, and inference uses their own
OAuth.

When the workspace publishes an agent configuration, the script's `ug configure --global` run is a
managed run: it rebuilds each supported agent's machine-wide files from the configuration,
including the admin's native settings. Developers then launch their agents through `ug` with no
extra variable. See
[Admin-managed agent settings reference](../../docs/admin-managed-settings-reference.md).

The script authenticates `ug configure` with a short-lived token for a service principal whose
client id and secret come in as policy parameters; neither the credentials nor the token are
written to disk. See [macos-setup.md](macos-setup.md) for the full setup.

## Linux

`linux-provision.sh` does the same as the macOS script for an MDM or configuration management tool
that runs it as root. It takes the same arguments (`$4` workspace URL, `$5` client id, `$6` client
secret; `$1`-`$3` are ignored), installs `ug` and the Databricks CLI under `/usr/local`, and
installs Node.js 22 (checksum-pinned, under `/usr/local/unity-gateway/node`) if `node` or `npm` is
missing. `ug configure --global --mdm` then installs any missing Claude Code or Codex with
`npm install -g` into `/usr/local`, and writes the machine-wide files under `/etc/unitygateway/`,
`/etc/claude-code/` and `/etc/codex/`. Agents already on the `PATH` are reused, not migrated to
npm; remove an earlier native install before rerunning if npm should own it. It adds the same
`claude` and `codex` aliases to whichever of `/etc/bash.bashrc`, `/etc/bashrc`, `/etc/zsh/zshrc`
and `/etc/zshrc` exist. It installs any missing `curl`, `git`,
`python3`, `tar` or `unzip` with `apt-get` or `dnf`. For example:

```bash
sudo bash linux-provision.sh _ _ _ https://myws.cloud.databricks.com "$CLIENT_ID" "$CLIENT_SECRET"
```

The client secret is visible to other local users in the process list while the script runs, as
with any tool that passes it as an argument. The service principal setup in
[macos-setup.md](macos-setup.md) applies unchanged.
