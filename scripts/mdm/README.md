# MDM / JAMF deployment for Unity Gateway

MDM deployment runs `ug configure` as root on each machine.

| Platform | Script | Setup guide |
| --- | --- | --- |
| macOS | `macos-provision.sh` | [macos-setup.md](macos-setup.md) |

Linux and Windows scripts will follow.

On macOS, `macos-provision.sh` is an MDM policy script (written for JAMF), run as root on each
machine. It installs `ug` (with its own Python), the Databricks CLI, Claude Code and Codex under
`/usr/local`, where every user can run them, then runs `ug configure` as root, which installs the
workspace policy at `/etc/unitygateway/managed-settings.json`. Developers then launch with
`UG_SKIP_OS_SETTINGS=1 ug claude`: no sudo, and inference uses their own OAuth.

The script authenticates `ug configure` with a short-lived token for a service principal whose
client id and secret come in as policy parameters; neither the credentials nor the token are
written to disk. See [macos-setup.md](macos-setup.md) for the full setup.

## Known gap

`macos-provision.sh` requests an `all-apis` token for the service principal, which
is broader than `ug configure` needs: it only reads the workspace's coding-agent
config and lists models. Until the script requests narrower scopes, give the
service principal only workspace access and a short-lived secret.
