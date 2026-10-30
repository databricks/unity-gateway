# Claude Desktop deployment through MDM on macOS

Run `ug configure --agents claude --mdm-desktop` as root from your MDM script.
Provide a workspace URL or an administrator's Databricks CLI profile:

```sh
/opt/unitygateway/venv/bin/ug configure \
  --agents claude --mdm-desktop --workspace https://workspace.databricks.com
```

This configures Desktop only. It publishes a machine policy and a root marketplace
without needing an initialized Desktop account. A LaunchAgent installs and refreshes
the marketplace through each user's normal Cowork plugin registration. It does not use
the organization plugin loader, which behaved differently for connector discovery in
the earlier pilot. Windows and Linux reject `--mdm-desktop`.

## Prerequisites

Deploy Claude Desktop and a shared runtime before running configure. This command
does not install Python, UG, the Databricks CLI, or the Claude CLI. All four must be
root-owned, readable/executable by device users, and not writable by group or other
users. Their parent directories must also be protected and traversable. Installs in
`/Users`, `/var/root`, or an administrator's private virtual environment are rejected.
Use a standalone Claude CLI binary that supports `plugin ... --cowork --json`.
Keep shared runtime updates under MDM control.

For example, an organization can package Python under `/opt/unitygateway/python`,
the standalone Claude and Databricks CLIs under `/opt/unitygateway/bin`, and UG with
its dependencies as wheels under `/opt/unitygateway/wheels`. From the MDM script,
create the environment using that staged package:

```sh
umask 022
/opt/unitygateway/python/bin/python3 -m venv /opt/unitygateway/venv
/opt/unitygateway/venv/bin/python -m pip install --no-index \
  --find-links /opt/unitygateway/wheels 'unity-gateway==<approved-version>'
```

Replace `<approved-version>` with the version in your wheel set. Obtain packages
through your organization's approved distribution process; this example downloads
nothing. The environment must contain the installed UG package, not a checkout
reachable only through `PYTHONPATH`. Provision with `/opt/unitygateway/bin` on `PATH`
so UG binds to those shared CLI binaries. Do not copy an administrator's OAuth cache
or private home directory into this package.

The workspace needs at least one accessible Claude model. Its coding-agent config
supplies the Claude model source, headers, tracing, MCP services, and downloadable
skills. Without a managed config, the deployment uses discovered Claude models and
no managed skills/connectors. `--mcp system.ai.slack,system.ai.github` can additionally
select named MCP services. The first version requires a dedicated workspace hostname;
account URLs requiring a workspace-ID selector are rejected.

If ordinary global `ug configure` or a pilot already owns the root marketplace,
revert that installation through its original owner before deploying MDM. Likewise,
remove a previous third-party mobileconfig through its MDM owner. UG refuses to
overwrite foreign or externally edited machine files. Remove any previous
`/Library/Application Support/Claude/org-plugins/ug` pilot before refreshing plugins.

## Provisioning and authentication

```sh
export PATH="/opt/unitygateway/bin:/usr/bin:/bin:/usr/sbin:/sbin"
/opt/unitygateway/venv/bin/ug configure --agents claude --mdm-desktop \
  --workspace https://workspace.databricks.com
```

UG obtains one provisioning credential and reuses it to read the workspace policy,
discover models/connectors, and download skill bundles. Unattended scripts can supply
the existing `DATABRICKS_BEARER` or `DATABRICKS_BEARER_COMMAND` mechanism using their
secret broker. An interactive administrator can instead supply `--profile <name>`;
normal Databricks CLI authentication applies. A headless MDM process needs a usable
credential before this command; it cannot complete a browser login by itself.

No provisioning token or administrator profile is written into the machine policy
or plugin. The runtime wrapper clears provisioning credential overrides and binds
to the shared Python and CLIs. Each user's Desktop helper uses a separate
`UG_DESKTOP_<workspace-hash>` OAuth profile in that user's own Databricks configuration.
The existing helper permits browser sign-in only on an interactive Desktop auth request.
Background refresh never initiates authentication. Connector consent, subscription
relay login when configured, and subsequent token refresh remain per-user operations.

`--user`, `--use-pat`, custom OAuth options, model-source overrides, and Databricks AI
Tools installation flags cannot be combined with this mode. `--dry-run` reports the
machine destinations without authentication, discovery, or writes. Normal configure
and revert behavior is unchanged when `--mdm-desktop` is absent.

## Files and startup

| Location | Contents |
| --- | --- |
| `/Library/Managed Preferences/com.anthropic.claudefordesktop.plist` | Machine third-party inference and optional tracing policy |
| `/Library/Application Support/UnityGateway/claude-cowork-marketplace` | Marketplace manifest and `plugins/ug`: plugin manifest, `.mcp.json`, hooks, skills, and supporting assets |
| `/Library/Application Support/UnityGateway/claude-desktop-mdm` | Shared command wrapper, optional tracing helper, readable deployment record, private ownership state and lock |
| `/Library/LaunchAgents/com.databricks.unity-gateway.claude-desktop.plist` | Per-user registration/refresh job |
| `~/.ucode/claude-desktop-mdm.json` | User's installed version/account tracking and pending cleanup |
| `~/Library/Application Support/Claude-3p/local-agent-mode-sessions/<account>/<org>/cowork_plugins` | Native user registration and installed plugin cache |

Only inference/tracing settings go in the managed plist. Values use Anthropic's
documented string encoding, including JSON strings for arrays, objects, and booleans.
This writes the documented machine plist directly; it does not export a `.mobileconfig`.
Managed inference settings override the local third-party profile. A separate per-user
managed plist can override machine keys, so check for competing policies when testing.
See Anthropic's [MDM reference](https://claude.com/docs/third-party/claude-desktop/mdm).

The LaunchAgent runs at login and every 60 seconds. Configure also attempts to start
it for the current console user. With no initialized Cowork account, refresh does
nothing and retries later. Once Desktop creates an account/org directory, refresh
uses the Claude CLI's native marketplace/install/update commands. Multiple ambiguous
account directories produce an error instead of guessing. Other users' files are
never written by root.

There is one `ug@unity-gateway-cowork` registration in the selected account. Refresh
can migrate an existing registration from UG's standard user marketplace; that source
copy can remain on disk without being registered twice. Unrelated plugins are preserved.
All connector commands and hooks use the machine wrapper with per-user authentication.
The generated connector guidance and hook behavior are shared with ordinary configure.
Only workspace-managed downloadable skills are copied; administrator-local skills,
project skills, and independent third-party plugins are not distributed.

On a fresh laptop, open Cowork, complete its sign-in flow, allow the refresh to finish,
then start a new task. The first task may precede plugin installation. To refresh
immediately as the logged-in user, without sudo:

```sh
"/Library/Application Support/UnityGateway/claude-desktop-mdm/ug" desktop-mdm-sync
```

## Updates and removal

Rerun the provisioning command to fetch current workspace configuration and publish
new contents. The plugin version is derived from its contents. Each user's refresh
updates the native installed cache; changing the root source alone is insufficient.
Start a new task after a plugin update, and restart Desktop after policy changes.
There is no automatic workspace-policy fetch from the login job: MDM controls publication.

Publication validates ownership, stages the source, and rolls back files and source
on write failures. Incomplete skill discovery/download fails the deployment instead
of publishing a partial set. A failed rollback reports retained recovery files.
Do not treat these transactions as power-loss recovery guarantees.

Remove the deployment as root:

```sh
/opt/unitygateway/venv/bin/ug revert --mdm-desktop
```

This removes the owned machine policy and marketplace and disables the deployment.
It retains the wrapper, LaunchAgent, and deployment/ownership records so users who
were logged out can clean their registrations at their next login. Per-user cleanup
removes tracked MDM installations, including interrupted refreshes, while preserving
registrations that switched to another source or version. It leaves user credentials
and unrelated plugins intact. Keep the shared runtime until cleanup has run for every
affected user; your MDM uninstaller can then remove the retained agent and MDM directory.

Ordinary `ug revert` does not disable MDM: the refresh job can reinstall the managed
plugin. Use `ug revert --mdm-desktop` for administrator removal. Local 3P profiles were
not changed by MDM and can become active again after its policy is removed.

## Native validation checklist

The existing marketplace pilot verified skills and connector calls on macOS Claude
Desktop 4.4212.0, including source updates and one registration without duplicates.
Those results do not validate this new launchd/provisioning path. Component tests
replace workspace APIs, root ownership, launchd, and native Claude commands; they
verify files, auth separation, cache refresh, rollback, and cleanup only.

Before rollout, test this build on a Mac:

1. Use a test account without an existing UG/mobileconfig/org-plugin installation.
   Stage the shared runtime, then provision as root while Desktop is closed.
2. Check the managed plist with `plutil -lint` and confirm generated `.mcp.json` and
   hooks reference the machine wrapper, with no administrator home or credentials.
3. Log in as a fresh standard user, open Cowork, complete user authentication, and
   wait for refresh (or run the wrapper's `desktop-mdm-sync` command as that user).
4. Start a new task. Invoke an installed workspace skill, make a real connector call,
   and verify exactly one UG plugin in settings. File checks alone do not prove this.
5. Change a managed skill's verification text, rerun root provisioning, and verify
   the new text through skill invocation in a new task after refresh. Also test removal
   of a skill/connector from workspace policy.
6. Repeat with a second standard user. Neither user should inherit administrator or
   other-user authentication. Verify logout/login refresh and expired-auth recovery.
7. Revert as root while one user is logged out. Confirm policy removal immediately,
   plugin removal for the active user, and deferred cleanup when the other user logs in.

Native launchd startup, first-user timing, Desktop policy precedence, browser consent,
and live tool/skill execution remain required rollout checks.
