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

## Configure and launch

- Configure forces a remote check and installs changed policy before using it. A privileged
  installation failure is an actionable configure error.
- Launch reads installed policy and reuses a successful check for five minutes. Once expired,
  it checks remote policy; indirect agent checks reuse the same timestamp.
- Unchanged policy does not invoke sudo or touch installed bytes/metadata.
- Changed policy can request sudo/password in an interactive terminal. Scripts use `sudo -n`
  and cannot prompt. A root-run installation does not need sudo.
- A failed or declined launch update warns: “Workspace policy changed; run `ug configure`.”
  Launch continues with the matching installed policy. Without one, existing unmanaged fallback
  remains. The attempt is throttled for five minutes before another launch retries.
- No-config or feature-disabled removes a matching installed policy. Failed launch removal
  warns and keeps the old policy; another workspace's file is never removed just for absence.
- Fetch/authentication failures retain existing local fallback and do not advance the timestamp.
- Launch dry-run is local/offline. Configure dry-run may fetch to preview but does not install.

The one file holds one active workspace for the machine. Installing another workspace replaces
that policy for all users. Workspace URLs are canonicalized and foreign-workspace policy is
never applied. Agent-specific Claude/Codex OS-managed settings remain separate; their existing
conflict restrictions can still prevent launch even when workspace-policy refresh falls back.

## Refresh metadata

`~/.ucode/refresh_ts.json` is user-owned `0600`, keyed by canonical workspace URL. Each entry has
`last_checked_at`, `outcome` (`published`, `none`, or `feature_disabled`), and the installed-file
fingerprint. There is no second policy copy.

An inode or mtime indicates a local write, not the last successful unchanged remote check.
The sidecar allows throttling without updating the root-owned file. Its fingerprint (existence,
device, inode, size, mtime, ctime) invalidates a warm check after local replacement/edit.
These timestamps are an optimization, not a tamper-proof enforcement mechanism.

The API's `retrieved_time` changes on reads and is excluded from drift comparison. Its stored
value may therefore predate the latest check; use `last_checked_at` for refresh information.
All other raw fields participate in comparison. Policy is otherwise preserved when installed.

## Migration and limits

The legacy `~/.ucode/managed-config.json` is ignored and never promoted to a root-owned policy.
Run `ug configure --workspace YOUR_WORKSPACE` to retrieve and install authoritative policy.
No policy is written when the workspace has none.

The five-minute TTL avoids every-launch request latency but delays detection of remote changes.
Warning/fallback prioritizes availability over strict enforcement of the newest policy.
Privileged writes are supported on Linux/macOS only. Real sudo/password and ownership integration
checks require a disposable clean machine, not an existing developer-managed workstation.
