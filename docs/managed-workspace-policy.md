# Managed workspace policy

`ug configure` reads the workspace's published `CodingAgentConfig` through
`GET /api/ai-gateway/v2/coding-agent-configs` and installs it machine-wide at
`/etc/unitygateway/managed-settings.json` on Linux and macOS.

The installed file contains only:

```json
{
  "workspace": "https://your-workspace",
  "config": {}
}
```

`config` is the raw remote configuration, not normalized agent settings. The empty object above
only illustrates the shape: a workspace without a published policy does not leave a policy file.
The directory is root-owned `0755` and the file is root-owned `0644`, so ordinary users can read it
without sudo. Authentication tokens and profiles stay in user-owned state.

**Never put secrets in managed policy headers.** The policy is readable by every local user, and
ug does not redact raw fields. Do not put passwords, bearer tokens, API keys, or session cookies in
`http_headers` or anywhere else in the configuration. Routing headers such as a workspace ID are
appropriate.

## Configure

- `ug configure` forces a fresh read of the workspace policy, so it never installs a stale one.
- Changed policy is written with sudo from a terminal. Where sudo cannot prompt (for example in a
  script), ug uses non-interactive sudo and fails if a password would be needed.
- Root-run configure installs directly, without sudo.
- Unchanged policy writes nothing. A re-read that differs only in `retrieved_time` is not a change.
- A workspace that publishes no policy, or has the feature disabled, removes the installed file.
  Configure decides the machine's workspace, so this also removes a policy installed for a different
  workspace.
- If the installation or removal fails, configure stops with an error that says the machine-wide
  file needs sudo. Rerun `ug configure` from a terminal where you can use sudo, or ask your IT team
  or an administrator to run it.
- Windows has no machine-wide policy file. `UG_SKIP_OS_SETTINGS=1` and `ug configure --user` also
  skip it, so no file is written.

Launches never install or remove the machine-wide policy. Per-user caching of the fetched policy
(`~/.ucode/managed-config.json`) is unchanged by this write path.

## Limits

- The policy describes one workspace for the whole machine. Configuring another workspace replaces it
  for all users.
- `ug revert` removes the policy, with sudo. Without sudo it warns and keeps the file.
- Installation needs sudo on Linux and macOS. On MDM machines, ask IT to run the provisioning step
  rather than attempting administrator-only configure yourself.
- Live sudo and ownership checks need a disposable clean machine, not a developer workstation.
