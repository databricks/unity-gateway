# MDM / JAMF deployment for Unity Gateway

Deploying coding agents through the Databricks AI Gateway to a fleet of macs has
two independent layers. Keep them separate.

## Layer 1 — provisioning

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

## Layer 2 — enforcement (config profiles in this directory)

The OS-managed settings are what enforce gateway routing even for **bare**
`claude` / `codex` launches (not just `ug claude`). Both agents read a macOS
managed-preferences domain, so deploy them as JAMF **Configuration Profiles** —
no root file-writes, no `sudo`, and the profile outranks any on-disk file. This
matches how OpenRouter Ori is deployed (`com.openrouter.ori`).

| Agent | Domain | Template | Reads it |
| --- | --- | --- | --- |
| Claude Code | `com.anthropic.claudecode` | `claude-code.mobileconfig` | startup + every 30 min, read-only |
| Codex | `com.openai.codex` | `codex.mobileconfig` (+ `codex-managed_config.toml.template`) | startup, read-only |

Deploy each via JAMF -> Configuration Profiles -> Application & Custom Settings
(or upload the `.mobileconfig`). Each template has a header comment listing the
placeholders to fill (workspace host, model list, UUIDs) before deployment.

References:
- Claude Code managed settings: https://code.claude.com/docs/en/managed-settings
  (and Anthropic's Jamf template: https://github.com/anthropics/claude-code/tree/main/examples/mdm)
- Codex managed configuration: https://developers.openai.com/codex/enterprise/managed-configuration

## Why the split

Claude Code and Codex both treat OS-managed settings as **externally owned and
read-only** — an external tool writes them once and the agent only reads them.
Having `ug` rewrite them via `sudo` on each launch fights that model and prompts
non-admin users. Let MDM own the enforcement layer; let the provisioning script
own installation and the workspace policy.

## Known gap

`ug` cannot yet **emit** these profile payloads for MDM packaging — it only writes
the OS-managed files in place via an interactive `sudo` reconciliation. Until it
can, generate accurate content from a reference machine (run `ug configure` once as
admin, then read `/Library/Application Support/ClaudeCode/managed-settings.json`
and `/etc/codex/managed_config.toml`) and transcribe it into these templates. See
the "ug MDM gaps" note for the requested `ug` changes.
