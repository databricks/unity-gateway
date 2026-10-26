# Managed workspace policy

`ug configure` reads the workspace's published `CodingAgentConfig` through
`GET /api/ai-gateway/v2/coding-agent-configs` and installs it at
`/etc/unitygateway/managed-settings.json` on Linux/macOS.

The file contains only:

```json
{
  "workspace": "https://your-workspace",
  "config": {}
}
```

`config` is the raw remote configuration, not normalized agent settings. The empty object
above illustrates the shape only: a workspace without policy does not create a policy file.
The installed directory is root-owned `0755`; the policy is root-owned `0644`, so ordinary
users can read without sudo. Authentication tokens and profiles remain in user-owned state.
Administrator-authored policy fields, including configured headers, are visible to all local users.

**Never put secrets in managed policy headers.** The current schema accepts arbitrary header
strings; ug cannot establish that a value is safe to publish. Do not put passwords, bearer tokens,
API keys, or session cookies in `http_headers` (including `Authorization`, `Cookie`, or secret
custom headers). Use per-user authentication helpers/credential storage instead. Routing headers
such as a workspace ID are appropriate. This requirement applies to the entire world-readable raw
configuration, not just headers; ug preserves raw fields rather than silently redacting policy.

## Configure and launch

- Configure forces a remote check and installs changed policy before using it. A privileged
  installation failure is an actionable configure error.
- Launch replays the policy and outcome that configure saved and makes no remote request for it,
  however old it is; indirect agent checks do the same. An admin's change reaches a machine with
  the next `ug configure`. Only when nothing is saved for the workspace yet (or the installed file
  changed since it was recorded) does a launch read remote policy once and record the result.
- The installed policy's workspace is authoritative for launch and remote refresh. A different
  workspace selected in user state or with `--workspace` causes a launch error before authentication,
  bootstrap, cache reuse, or any remote request. This also applies to dry-run and `--skip-preflight`.
  Without a user-selected workspace, launch uses the installed workspace. Explicit administrator
  configure can replace the machine workspace; launch cannot switch it.
- Unchanged policy does not invoke sudo or touch installed bytes/metadata.
- Non-root launches never install or remove workspace policy, even in a terminal or with cached
  sudo access. They do not run `sudo -n -l`, start a sudo worker, or request a password.
  This avoids elevation attempts on MDM machines; IT/root provisioning owns the update.
- Explicit configure may request sudo in a terminal or use `sudo -n` in scripts.
  Root-run configure/refresh installs directly without sudo.
- A deferred launch update warns: “Your workspace policy changed. Ask your IT team or an
  administrator to apply the update on this machine.”
  Launch continues with the matching installed policy. Without one, the fetched policy applies to
  that run, but only if there is no installed policy for another workspace; a workspace mismatch is an
  error, not unmanaged fallback. The warning is shown only once per distinct pending policy
  change, not at every recheck.
- No-config or feature-disabled removes a matching installed policy during configure or a root
  refresh. A non-root launch warns and retains it.
- `ug configure` sets the machine's workspace: configuring a workspace that publishes no policy
  also removes another workspace's installed policy. A launch never changes the machine's
  workspace; selecting another workspace is refused.
- Fetch/authentication failures retain existing local fallback and record nothing.
- Launch dry-run is local/offline. Configure dry-run may fetch to preview but does not install.

The one file holds one active workspace for the machine. Installing another workspace replaces
that policy for all users. Workspace URLs are canonicalized; a mismatched selection is refused,
not used to bypass machine policy. Agent-specific Claude/Codex OS-managed settings remain separate; their existing
conflict restrictions can still prevent launch even when workspace-policy refresh falls back.

## Refresh metadata

`~/.ucode/refresh_ts.json` is user-owned `0600`, keyed by canonical workspace URL. Each entry has
`outcome` (`published`, `none`, or `feature_disabled`) and the installed-file fingerprint.
There is no timestamp: a record never expires, and an entry written by an older ug with
`last_checked_at` is read the same way. Deferred updates also record `pending_config_hash`, a SHA-256 digest of the canonical
workspace and fetched configuration, excluding `retrieved_time` and insensitive to JSON object
key order. No second policy copy is stored.

The pending hash suppresses repeat change warnings across rechecks, including pending policy
removal. A different pending config warns again. A successful installation/removal or a fetched
config matching the installed policy clears the hash. External MDM updates invalidate the file
fingerprint, so a user's next launch reconciles its own sidecar even if root used a different home.
Failed authentication/API reads preserve the pending hash and the recorded outcome.
Metadata is best-effort; deleting/corrupting the sidecar, failed writes, or concurrent launches can
cause a repeated warning.

An inode or mtime indicates a local write, not the last successful unchanged remote check.
The sidecar lets a launch tell "no managed config" from "feature disabled" without updating the
root-owned file. Its fingerprint (existence, device, inode, size, mtime, ctime) invalidates the
record after local replacement/edit. It is an optimization, not a tamper-proof enforcement mechanism.

The API's `retrieved_time` changes on reads and is excluded from drift comparison. Its stored
value may therefore predate the latest `ug configure`.
All other raw fields participate in comparison. Policy is otherwise preserved when installed.

## Migration and limits

On Linux and macOS, the per-user `~/.ucode/managed-config.json` is no longer read and is never
promoted to a root-owned policy.
Run `ug configure --workspace YOUR_WORKSPACE` to retrieve and install authoritative policy.
`ug revert` removes the installed policy with sudo; without sudo it warns and keeps the file.
No policy is written when the workspace has none. On MDM machines, ask IT to run root provisioning
instead of attempting administrator-only configure yourself.

Launches make no request when a config is saved, which avoids every-launch latency but delays
detection of remote changes until the next `ug configure`.
Warning/fallback prioritizes availability over strict enforcement of the newest policy.
Privileged writes are supported on Linux/macOS only. Real sudo/password and ownership integration
checks require a disposable clean machine, not an existing developer-managed workstation.

Where the machine cannot hold the policy (Windows, `UG_SKIP_OS_SETTINGS=1`, or `ug configure
--user`) and none is installed for the workspace, ug keeps its earlier behavior unchanged: the
fetched policy is cached per user in `~/.ucode/managed-config.json`, a launch replays it, and a
failed `ug configure` fetch falls back to it. Windows support for the installed policy is a
follow-up. A non-root Linux or macOS launch with nothing installed applies the fetched policy to
that run without installing or caching it, so a first launch before any `ug configure` is still
managed. An installed policy still wins: with `UG_SKIP_OS_SETTINGS=1`
a developer's launch reads it read-only, never writes OS-managed files, and builds only user-owned
settings. That is the supported developer mode on an MDM machine whose administrator ran `ug configure`.

There is no refresh-interval setting in the currently supported `CodingAgentConfig` shape, and
launches do not poll; the installed workspace selects the remote source for `ug configure`.

For fleet-managed installations, schedule root/MDM provisioning about every **four hours**
(`14400` seconds) rather than once daily. This bounds the usual stale-policy window without requiring
developer elevation; offline/sleeping machines can still lag. This is separate from a developer's
`ug configure`. The PR's baseline has no MDM scheduler, and current main's
`scripts/mdm-bootstrap.sh` is a one-shot bootstrap with no 24-hour refresh default. The deployment's
actual scheduler must set the four-hour interval; this change does not invent a background daemon.
