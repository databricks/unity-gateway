# MDM deployment for Unity Gateway

MDM deployment runs `ug configure --global` as root (macOS) or SYSTEM (Windows).

| Platform | Script | Setup guide |
| --- | --- | --- |
| macOS | `macos-provision.sh` | [macos-setup.md](macos-setup.md) |
| Windows (draft) | `windows-provision.ps1` | [windows-setup.md](windows-setup.md) |

A Linux script will follow.

On macOS, `macos-provision.sh` is an MDM policy script (written for JAMF), run as root on each
machine. It installs `ug` (with its own Python), the Databricks CLI, Claude Code and Codex under
`/usr/local`, where every user can run them, then runs `ug configure` as root, which installs the
workspace policy at `/etc/unitygateway/managed-settings.json`. It also adds `claude` and `codex`
aliases (for `UG_SKIP_OS_SETTINGS=1 ug claude` / `ug codex`) to `/etc/zshrc` and `/etc/bashrc`, so
developers launch with `claude` or `codex` in a new terminal: no sudo, and inference uses their own
OAuth.

The script authenticates `ug configure` with a short-lived token for a service principal whose
client id and secret come in as policy parameters; neither the credentials nor the token are
written to disk by the script. See [macos-setup.md](macos-setup.md) for the full setup.

On Windows, deploy `windows-provision.ps1` as an Intune Win32 app. It installs shared tools under
Program Files, uses `ug`'s npm agent installer, and adds PowerShell launch functions. Named inputs
replace Jamf's positional parameters. A secret passed in the Intune install command is not secure
secret injection and can appear in management or process logs; review the credential handling,
pilot, detection and removal steps in [windows-setup.md](windows-setup.md).
