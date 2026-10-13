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
- Launch reads installed policy and reuses a successful check for five minutes. Once expired,
  it checks remote policy; indirect agent checks reuse the same timestamp.
- Unchanged policy does not invoke sudo or touch installed bytes/metadata.
- Non-root launches never install or remove workspace policy, even in a terminal or with cached
  sudo access. They do not run `sudo -n -l`, start a sudo worker, or request a password.
  This avoids elevation attempts on MDM machines; IT/root provisioning owns the update.
- Explicit configure may request sudo in a terminal or use `sudo -n` in scripts.
  Root-run configure/refresh installs directly without sudo.
- A deferred launch update warns: “Your workspace policy changed. Ask your IT team or an
  administrator to apply the update on this machine.”
  Launch continues with the matching installed policy. Without one, existing unmanaged fallback
  remains. The check is throttled for five minutes before another launch checks again.
- No-config or feature-disabled removes a matching installed policy during configure or a root
  refresh. A non-root launch warns and retains it; another workspace's file is never removed
  just for absence.
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
No policy is written when the workspace has none. On MDM machines, ask IT to run root provisioning
instead of attempting administrator-only configure yourself.

The five-minute TTL avoids every-launch request latency but delays detection of remote changes.
Warning/fallback prioritizes availability over strict enforcement of the newest policy.
Privileged writes are supported on Linux/macOS only. Real sudo/password and ownership integration
checks require a disposable clean machine, not an existing developer-managed workstation.

There is no refresh-interval setting in the currently supported `CodingAgentConfig` shape.
The five-minute default is retained; interpreting the review as requesting a new policy field
requires confirmation of that field's name, units, and semantics first.

## Disposable-container privilege check

`scripts/check_machine_policy_privileges.py` checks the installed wheel's policy writer against
real `/etc` paths inside a disposable Docker container. It refuses host execution or an existing
policy. Root creates/updates/removes policy; UID 10001 reads it, performs unchanged checks, and
cannot install/remove changed policy with or without a TTY. The file fingerprint must stay
unchanged during the non-root checks.

Use the integration image built from `tests/integration/utils/Dockerfile` (it contains Python,
uv, and the UID 10001 integration user). Copy inputs instead of mounting developer homes or `/etc`:

```sh
uv build --wheel --out-dir /tmp/ug-policy-wheels
docker create --rm --name ug-policy-check --user 0 --entrypoint /bin/sh \
  ug-integration:latest -c 'uv pip install --system /wheels/*.whl && python /check_policy.py'
docker cp /tmp/ug-policy-wheels ug-policy-check:/wheels
docker cp scripts/check_machine_policy_privileges.py ug-policy-check:/check_policy.py
docker start -a ug-policy-check
```

Pass an explicit reachable package mirror to `uv build` / `uv pip install` if needed. This is
packaged-writer component evidence, not a successful MDM bootstrap or agent inference journey.
The full `scripts/mdm-bootstrap.sh` on current main requires explicitly supplied workspace,
OAuth client ID, and client secret. It does not currently implement `UG_TARGET_USER`; choose
root with the container's `--user 0`, then exercise developer launches as a non-sudo user.
