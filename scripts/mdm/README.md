# MDM / JAMF deployment for Unity Gateway

Deploying coding agents through the Databricks AI Gateway to a fleet of macs has
two independent layers. Keep them separate.

## Layer 1 — provisioning (`../mdm-bootstrap.sh`)

A JAMF policy script, run as root on each machine. It ensures prerequisites
(`uv`, `node`), installs `ug`, writes a PAT-based Databricks profile, runs
`ug configure` **non-interactively**, and probes each enabled agent. Because it
runs non-interactively, `ug` writes only its **local** settings and never touches
the OS-managed files, so there is no `sudo` password prompt. `ug claude` /
`ug codex` work off those local settings.

See `../mdm-bootstrap.sh` for inputs (env vars) and the JAMF wrapper snippet.

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
Having the bootstrap (or `ug` per launch) rewrite them via `sudo` fights that
model and prompts non-admin users. Let MDM own the enforcement layer; let the
bootstrap own provisioning + local settings.

## Troubleshooting (`heal-claude-gateway.sh`)

`heal-claude-gateway.sh` is an interactive, idempotent repair tool for a Mac left
half-configured: a legacy wrapper (`~/.local/bin/ucode-claude-ide`) that hardcodes
`ug claude --provider ...` and trips the "`--provider` not allowed when a managed
config exists" error, or a machine where `ug configure` wrote only its local
settings and never the OS-managed file (so `ug claude` routes but bare `claude` /
VS Code do not).

A developer runs it by **typing** it in Terminal (it refuses a non-TTY run,
because `ug` only writes the root-owned managed settings when stdin is a TTY):

    bash heal-claude-gateway.sh https://<workspace>.cloud.databricks.com

It backs up and removes the legacy wrapper, clears the VS Code
`claudeCode.claudeProcessWrapper` setting, runs `ug configure`, verifies where each
piece landed (`~/.claude/ucode-settings.json` and the OS-managed file), and writes
a redacted `~/ug-heal-report-*.txt` for support. Everything it changes is backed up.

## Known gap

`ug` cannot yet **emit** these profile payloads for MDM packaging — it only writes
the OS-managed files in place via an interactive `sudo` reconciliation. Until it
can, generate accurate content from a reference machine (run the bootstrap once as
admin, then read `/Library/Application Support/ClaudeCode/managed-settings.json`
and `/etc/codex/managed_config.toml`) and transcribe it into these templates. See
the "ug MDM gaps" note for the requested `ug` changes.
