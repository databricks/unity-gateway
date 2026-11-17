# MDM / JAMF deployment for Unity Gateway

MDM deployment runs `ug configure` as root on each machine.

| Platform | Script | Setup guide |
| --- | --- | --- |
| macOS | `macos-provision.sh` | [macos-setup.md](macos-setup.md) |

Linux and Windows scripts will follow.

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
