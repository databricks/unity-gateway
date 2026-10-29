# OS-Managed Agent Settings

## Summary

Claude Code and Codex give OS-managed settings higher precedence than user settings. Previously,
ucode could write only its local configuration while an existing machine-managed file silently
overrode the gateway endpoint, authentication helper, provider headers, or model.

The two stacked PRs make precedence handling deterministic:

1. The Claude PR adds the shared managed-file lifecycle and applies it to Claude Code.
2. The Codex PR reuses that lifecycle for TOML, applies it to Codex, and removes the old optional
   managed-settings path.

`ug configure` and `ug revert` reconcile the agent's OS-managed file whether or not a terminal is
attached, so an interactive and a headless run take the same path. The write settles privilege:
root writes directly, a terminal lets sudo prompt, and otherwise `sudo -n` either succeeds or the
command fails, naming root, a sudo terminal, and `--user`. A non-interactive launch uses local
settings when the managed file is compatible, and repairs conflicting managed settings using
`sudo -n`, which requires existing authorization and never prompts for a password. If that repair is
denied, the agent does not launch.

## Configuration Files

| Agent | Local ucode configuration | OS-managed configuration |
| --- | --- | --- |
| Claude Code | `~/.claude/ucode-settings.json` | Linux: `/etc/claude-code/managed-settings.json`; macOS: `/Library/Application Support/ClaudeCode/managed-settings.json`; Windows: `C:\Program Files\ClaudeCode\managed-settings.json` |
| Codex | `~/.codex/ucode.config.toml` | Linux and macOS: `/etc/codex/managed_config.toml`; Windows: `C:\ProgramData\OpenAI\Codex\requirements.toml` |

The local file is always written. The OS-managed file is additionally reconciled during interactive
configuration, except for Claude subscription relay.

## Per-user Scope

`ug configure --user` (saved as `config_scope` in `~/.ucode/state.json`, default `global`) skips the
OS-managed files like `UG_SKIP_OS_SETTINGS`: no administrator permission, and managed MCP servers
register in user scope. Because OS-managed settings take precedence over user settings,
`ug configure --user` fails before configuring any agent while a selected agent's OS-managed file
exists (including Windows `requirements.toml`), whoever wrote it; `ug revert`, removal, or
`--global` resolves it. Launches don't check: a managed file added later simply takes precedence.
`UG_SKIP_OS_SETTINGS` doesn't check either: ug trusts the OS-managed files present and leaves them
untouched. `ug configure` without `--user` and `ug revert` reset the scope to `global`.
`ug configure --user` also fails when the selected workspace publishes a managed config, since that
config must be installed machine-wide.

## Interactive Detection

Standard input determines whether UG may request administrator permission interactively. Standard
output does not affect the decision. Without a TTY, either agent may repair an existing conflicting
file through one-shot `sudo -n` with stdin disconnected; absent or compatible files remain local-only.
A denied repair blocks launch.

`managed_writes_allowed` centralizes this policy: `ug configure` and `ug revert` always may write;
for launches its default remains interactive-only, while the shared reconciliation/writer permits an
existing-file repair attempt. The writer derives sudo's
prompt mode and stdin handling itself; the predicate does not probe or grant authorization.

Windows has no `sudo`, so the terminal doesn't matter there: an elevated (or SYSTEM) process may
write `C:\Program Files\ClaudeCode\managed-settings.json` directly, which lets device provisioning
run `ug configure` headlessly. Standard users can't write under Program Files, so their
`ug configure` fails with a request to rerun it elevated. For a launch, as for a headless
Linux/macOS launch, an absent or compatible file stays local-only, and a conflicting file stops the
command. A batched setup outside `ug configure` that can't write (such as `ug agents add` without a
terminal) warns that the machine-wide file wasn't updated; launches, which reconfigure on every
run, stay quiet.

Codex has no admin-only config layer on Windows, so the same elevated runs write
`C:\ProgramData\OpenAI\Codex\requirements.toml`, which Codex 0.156.0 and later enforce over the
user's config and `-c`. It takes only requirement keys: UG writes `model_provider` and the
`Databricks` provider entry, but not `model` or `mcp_servers` (managed MCP servers stay in user
scope there). A required provider entry replaces the configured one instead of merging, so the
launch-scoped headers reach Codex through `env_http_headers` and the
`UG_CODEX_MODEL_PROVIDER_SERVICE`, `UG_CODEX_MODEL_SERVICE_PARENT_SCHEMA`,
`UG_CODEX_SMART_ROUTER_RECIPE`, and `UG_CODEX_REQUEST_TAGS` variables that `ug codex` sets at
launch. Standard users can create folders under ProgramData and keep control of the ones they
create, so before each write UG makes Administrators the owner of `OpenAI` and `Codex`, resets
`OpenAI` to its inherited permissions, and lets standard users only read `Codex`, like the
root-owned 0755 folders on Linux and macOS. It changes only those two folders, not files already in
them, and refuses to write through a linked `OpenAI` or `Codex` folder.

This is a new shared ucode distinction. The previous implementation inferred interactivity from
command shape in some flows and did not guard managed-file writes consistently.

## Behavior Matrix

| Invocation | Managed file | Behavior |
| --- | --- | --- |
| Interactive | Absent | Create it from the ucode configuration after recording an absent baseline. |
| Interactive | Unrelated or partially populated | Preserve unrelated values and add or update all ucode-owned values. |
| Interactive | Conflicting | Back up the baseline, replace the conflicting ucode-owned values, and verify. |
| Interactive | Already identical | Continue without a backup, write, or `sudo` invocation. |
| `ug configure`, any terminal | Any | As Interactive. Without a terminal, root writes directly and others use `sudo -n`; a denied write fails the command. |
| Non-interactive launch | Absent | Use the local ucode file. Do not create the managed file. |
| Non-interactive launch | Ucode-owned values absent or equal | Use the local ucode file. Do not modify the managed file. |
| Non-interactive launch | Ucode-owned value conflicts | Repair through `sudo -n`; stop before launching if authorization or verification fails. |
| Any | Invalid, unreadable, or symlinked | Stop without modifying the file because precedence cannot be established safely. |

`ucode configure`, first-time `ucode claude` or `ucode codex`, and later launches all use the same
agent-specific reconciliation path. A first-time launch from an interactive terminal can therefore
request administrator permission. A first-time non-interactive launch uses local settings unless
the agent must repair an existing conflicting managed file.

For Claude, turning off UG tracing preserves the live OS-managed telemetry values unchanged,
including values UG wrote on an earlier run. The same applies when the workspace has no managed
Coding Agent Config, even if stale local state says tracing was enabled. A tracing-disabled write
does not claim ownership of external telemetry merely because it appears in the full-file snapshot.
This does not relax conflicts when a workspace managed config explicitly enables UG tracing.

Claude custom headers are compared by name and value, ignoring order, name casing, and surrounding
whitespace. Differences in `User-Agent` and `Databricks-Smart-Router-Recipe` alone do not block a
local-only launch: these headers attribute traffic, rather than select the gateway or provider.
The OS-managed header string still wins, so UG attribution may be unavailable for those launches.
Required coding-agent-mode, provider, authentication, and other header differences remain blockers.

## Interactive Reconciliation

For each agent, ucode:

1. Strictly parses the existing managed JSON or TOML document.
2. Produces the desired document by applying the same gateway overlay used for the local ucode file.
3. Preserves settings outside the paths owned by ucode.
4. Preserves enterprise Claude permission-deny entries while adding ucode-required entries.
5. Records the original baseline before the first change.
6. Requests administrator permission and performs an atomic privileged replacement.
7. Reads the installed file back and verifies its exact contents.
8. Records the last-applied snapshot, owned paths, and a launch fingerprint.

An existing managed file is reconciled even when it does not currently conflict. This ensures every
ucode-required value exists at the highest-precedence scope and avoids separate behavior for absent,
partial, and conflicting files.

## Admin Settings Passthrough

An admin can put Claude Code settings in the coding agent config under `agent_native_settings`.
ucode copies them as-is into Claude Code's managed file. The server decides which keys are allowed.
Codex isn't supported yet.

- **ucode's own keys win.** A setting that would overwrite something ucode writes (gateway URL,
  auth, models, MCP servers) is skipped with a warning.
- **Shared lists are merged.** `permissions.deny` and hooks keep entries IT added; ucode only adds
  and removes its own.
- **ucode's MCP servers stay allowed.** If the admin sets `allowedMcpServers`, ucode adds the
  servers it registers, so the allowlist doesn't block them.
- **Removed settings are cleaned up.** When the admin drops a setting, or the developer switches to a
  workspace without it, ucode removes it, unless someone edited it by hand. `ucode revert` removes
  everything ucode wrote.
- **Managed file only.** Settings never go into the private ucode file. Where the managed file can't
  be written (unsupported platform, relayed launch), they aren't applied.

## Privileged Write Transaction

The shared writer handles ordinary MDM-installed and root-owned files rather than treating them as
errors:

- refuses symlink destinations;
- creates a root-owned destination directory when it is absent;
- stages the new file in the destination directory for a same-filesystem atomic rename;
- clones an existing file before replacing its contents, preserving ownership, mode, ACLs, and
  extended attributes;
- creates new files as root-owned and world-readable;
- detects, clears, and restores macOS `schg`, `uchg`, `sappnd`, and `uappnd` flags;
- detects, clears, and restores Linux immutable and append-only attributes;
- verifies the resulting contents after replacement;
- retries once only when device management restored the exact pre-write contents;
- preserves a concurrently changed policy instead of overwriting it.

Interactive updates may request administrator permission. Non-interactive conflict repairs use only
`sudo -n` with stdin disconnected.

Root access cannot sustainably override an actively enforced policy. If an MDM process immediately
restores the original file twice, ucode stops with an error instead of repeatedly fighting the
management agent. A different concurrent update is also preserved and reported.

## Backup Model

Backups live under `~/.ucode/managed-backups/` with directory mode `0700` and file mode `0600`.
The manifest records, per agent:

- whether the managed file originally existed;
- its absolute path;
- the baseline snapshot and SHA-256 digest;
- the last ucode-applied snapshot and SHA-256 digest;
- the setting paths owned by ucode.

The baseline is created before the first managed change and is not replaced by subsequent
configurations. Later configurations update only the last-applied snapshot. Snapshot filenames are
validated, digests are checked before use, and symlinked backup directories or manifests are
rejected.

If ucode cannot complete a write or revert, the backup remains available for a later retry.

## `ucode revert`

`ucode revert` extends the existing local-file restoration with managed-file restoration:

- If the current file exactly matches ucode's last-applied snapshot, restore the exact baseline.
- If ucode created the file, delete it.
- If an administrator or MDM changed the file later, perform a three-way revert.
- Restore original values only where the current value still matches ucode's last-applied value.
- Remove only list entries added by ucode while preserving externally added entries.
- Preserve externally changed values rather than replacing them with stale baseline values.
- Refuse an unsafe or unparsable merge and retain the backup.

A revert that needs to change an OS-managed file must run interactively. A successful revert removes
that agent's backup record. The existing local configuration and ucode state cleanup still occur as
part of the command.

## Cached Launches

After a successful managed reconciliation or compatibility check, ucode stores:

- the managed path;
- the verification scope;
- device and inode numbers;
- size;
- nanosecond modification and change times.

A cached launch performs one `stat()` call and compares this fingerprint. When it matches, ucode
does not read, parse, back up, write, or invoke `sudo`. When it changes, the normal reconciliation or
compatibility path runs again. This catches later MDM replacement without adding meaningful latency
to unchanged launches.

The verification scopes distinguish:

- a managed file reconciled interactively or through an authorized non-prompting repair;
- a managed file verified as compatible with local settings;
- a managed file verified as compatible with Claude relay.

A compatibility fingerprint created non-interactively cannot suppress the next interactive
reconciliation.

## Claude Subscription Relay

Claude relay is intentionally local-only. It routes through a loopback refresh proxy whose address
and lifetime belong to one `ucode claude` session. Persisting that address in OS-managed settings
would break bare `claude` launches and future sessions.

Relay therefore never creates or updates the managed file. It allows an absent file or unrelated
enterprise settings, but blocks these higher-precedence conflicts:

- `apiKeyHelper`;
- `env.ANTHROPIC_BASE_URL`;
- `env.ANTHROPIC_CUSTOM_HEADERS`.

If ucode wrote those values during an earlier standard configuration, the user runs `ucode revert`
interactively before switching to relay. External conflicting values require administrator action or
standard Databricks authentication.

## Status and Messages

`ucode status` reports, for Claude and Codex:

- managed settings path;
- state: not configured, current, compatible local settings, compatible relay settings, drifted,
  invalid, unreadable, missing, or unsupported;
- whether a managed baseline backup is available.

Interactive updates announce the backup location, administrator-permission request, and verified
result. An identical file produces no elevation message.

Representative blockers are:

```text
Claude Code cannot start because ucode could not update <path>: sudo: a password is required.
Run the ucode command from an interactive terminal and approve the administrator prompt, or contact
your administrator.
```

```text
Claude Code managed settings at <path> were updated but immediately restored by device management.
Contact your administrator.
```

```text
Cannot safely update Codex managed settings at <path>: <parse error>. ucode did not modify the file.
Repair it or contact your administrator.
```

Write, verification, parse, symlink, and managed-conflict failures block the agent launch. Recovery
information always identifies the file and recommends either an interactive configure/revert or
administrator help.

## PR Boundaries

### PR 1: Claude Code

- Add shared strict reads, fingerprints, compatibility checks, secure backups, atomic privileged
  writes, immutable-flag handling, verification, status, and three-way revert helpers.
- Make interactive Claude configuration reconcile OS-managed JSON by default.
- Add non-interactive local fallback with conflict detection.
- Add Claude relay-specific safety checks.
- Add Claude managed status and revert output.
- Remove Claude's old managed-settings scope choice.

### PR 2: Codex

- Stack on the Claude PR and reuse the shared lifecycle with strict TOML parsing and serialization.
- Make interactive Codex configuration reconcile OS-managed TOML by default.
- Add non-interactive local fallback with conflict detection.
- Add Codex fingerprinted cached launches, status, and revert behavior.
- Keep opt-in smart-routing hooks in the local Codex config rather than adding them by default to
  machine-managed policy.
- Remove the remaining managed-settings scope schema, resolution, setup prompt, summary, and tests.
