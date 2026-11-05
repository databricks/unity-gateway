# Claude Desktop deployment through MDM on macOS

Run as root from your MDM script:

```sh
/opt/unitygateway/venv/bin/ug configure --agents claude --global \
  --workspace https://workspace.databricks.com
```

This generates Claude Desktop's machine plist and a root marketplace containing
workspace-managed connectors, skills/assets, and hooks. A LaunchAgent registers that
source through each user's normal Cowork plugin loader and refreshes its installed cache.
Provisioning does not need a Cowork account and does not write the administrator's
Desktop profile, user marketplace, or plugin registration.

| Configure scope | Desktop destinations |
| --- | --- |
| Default or `--user` | User 3P JSON, user marketplace, and user registration/cache; root files untouched |
| Explicit `--global` | macOS machine plist, root marketplace, shared helpers, and LaunchAgent |

`--global` also configures the requested CLI agents under their existing scope rules.
Windows/Linux skip Desktop under `--global`. Saved `global` scope does not change the
Desktop behavior of a later default/user configure. Ordinary revert leaves MDM intact.

## Prerequisites and authentication

Install shared Python/UG, Databricks CLI, Claude CLI, and Desktop before provisioning.
UG does not install these shared runtimes. Python must import the installed UG package
without `PYTHONPATH`; the Claude CLI must support `plugin ... --cowork --json`.
Executables, packages, and their parent directories must be root-owned, accessible to
all device users, and not writable by group/other users. Administrator-home installs
are rejected. Set PATH to shared, protected directories before running the command.
Obtain packages through your organization's approved distribution process.

Use a dedicated workspace hostname with at least one accessible Claude model. PAT/custom
OAuth and hosts requiring a workspace-ID selector are unsupported for Desktop MDM.
The workspace's managed configuration supplies models, headers/tracing, skills, and MCPs.
Without a managed config, UG discovers Claude models without managed skills/connectors.
An explicit enabled-agent list excluding Claude prevents provisioning.

Supply an existing provisioning credential through `DATABRICKS_BEARER`,
`DATABRICKS_BEARER_COMMAND`, or cached Databricks OAuth with `--profile <name>` instead
of `--workspace`. With explicit Claude selection (`--agents claude` / `--agent claude`),
`--global` validates shared CLI paths before profile lookup/version probes and does not
force browser login. Omitted-agent configure
retains normal authentication/discovery before policy or the picker selects agents;
Desktop prerequisites apply only if Claude is selected. Use explicit agents for MDM.

Provisioning reuses one token for discovery/download and never stores it in machine
files. Shared helpers discard provisioning credential overrides and use each user's
separate `UG_DESKTOP_<workspace-hash>` OAuth profile. Browser sign-in happens only on
an interactive Desktop credential request. Background plugin refresh never authenticates;
connector consent and subscription relay sign-in remain per-user. Existing MCP/tracing
helpers keep their current authentication behavior; coexistence with a saved user PAT
configuration is outside this MDM release.

`--global` rejects `--mcp`, which registers user-level servers. Desktop receives the
workspace-managed MCPs from policy.
`UG_SKIP_OS_SETTINGS` is incompatible with MDM. Explicit Desktop provisioning
errors fail configure. Desktop dry runs report destinations without publishing files;
other agents retain their own dry-run behavior.

## Machine files and user registration

| Location | Contents |
| --- | --- |
| `/Library/Managed Preferences/com.anthropic.claudefordesktop.plist` | Inference and optional tracing policy |
| `/Library/Application Support/UnityGateway/claude-cowork-marketplace` | Marketplace manifest and `plugins/ug`: MCPs, hooks, skills/assets |
| `/Library/Application Support/UnityGateway/claude-desktop-mdm` | Shared `ug` wrapper, optional tracing helper, runtime information in `deployment.json` |
| `/Library/LaunchAgents/com.databricks.unity-gateway.claude-desktop.plist` | User login/refresh job |

Only inference/tracing settings go in the plist. Values use Anthropic's documented
string encoding, including JSON strings for arrays, objects, and booleans. UG writes
the plist directly; it does not export a mobileconfig. See Anthropic's
[MDM reference](https://claude.com/docs/third-party/claude-desktop/mdm).

Remove competing mobileconfigs through their MDM owner and any old
`/Library/Application Support/Claude/org-plugins/ug` pilot before provisioning.
UG preserves an existing policy whose credential helper belongs to another deployment.
Its own generated files and marketplace are replaced, including manual edits. Local
administrator skills, project skills, and independent plugins are not distributed.

The LaunchAgent runs at login and every 60 seconds; configure also tries to start it
for the current console user. With no Cowork account, it waits for Desktop to create
one. It then uses native Claude marketplace/install/update commands to maintain one
`ug@unity-gateway-cowork` registration and matching cached version in that account's
`cowork_plugins` directory. Unrelated plugins are preserved. There is no per-user MDM
tracking file or cleanup queue. Refresh can switch UG's standard user registration to
the root source; ambiguous or conflicting account/source selections fail without guessing.

After opening Cowork and completing user sign-in, wait for refresh and start a new task.
To refresh immediately, run without sudo:

```sh
"/Library/Application Support/UnityGateway/claude-desktop-mdm/ug" desktop-mdm-sync
```

## Updates and removal

Rerun provisioning to publish workspace changes. Content-derived plugin versions
trigger each user's cache update; root files alone do not update installed plugins.
Start a new task after plugin changes and restart Desktop after policy changes.
The login job does not fetch workspace policy. While enabled, it restores root-source
registration after ordinary configure selects a user source.

Publication reuses existing bundle replacement and atomic file writers. Discovery and
skill downloads finish before publication. There is no cross-file transaction, recovery
backup, or ownership ledger: after a partial publication failure, rerun configure.
Run provisioning and manual user refresh serially.

User plugin removal is manual. Quit Desktop and unload the refresh job in each active
user session:

```sh
launchctl bootout gui/$(id -u)/com.databricks.unity-gateway.claude-desktop
```

Remove the UG plugin and marketplace through Desktop's plugin settings or native
Claude commands for each Cowork account:

```sh
CLAUDE_CONFIG_DIR="<Cowork account/org directory>" /opt/unitygateway/bin/claude \
  plugin uninstall ug@unity-gateway-cowork --cowork
CLAUDE_CONFIG_DIR="<Cowork account/org directory>" /opt/unitygateway/bin/claude \
  plugin marketplace remove unity-gateway-cowork --cowork
```

Then remove machine files as root:

```sh
/opt/unitygateway/venv/bin/ug revert --mdm-desktop
```

This deletes the policy, root marketplace, shared helpers/runtime record, and LaunchAgent
file. It does not visit user homes or remove cached plugins. Logged-out users must
remove their cached UG plugin when they next sign in; no deferred cleanup job remains.
User credentials and unrelated plugins are unchanged. Local 3P profiles can become active
again after the machine policy is removed.

## Native validation

Component tests replace workspace APIs, ownership, launchd, and native Claude commands;
they establish file placement, auth separation, cache refresh, and machine-file removal.
Earlier macOS testing verified skills/connectors and a shared-runtime Gmail call, but
the simplified lifecycle needs fresh native validation before fleet rollout:

1. Provision with no existing UG/mobileconfig/org-plugin installation. Check the plist
   with `plutil -lint` and confirm MCP/hook commands use the shared wrapper.
2. Log in as a fresh standard user, open Cowork, authenticate, and refresh. Invoke a
   workspace skill and connector in a new task; verify exactly one UG plugin.
3. Publish a skill/connector change, refresh, and verify the new contents through actual
   invocation. Repeat with another user and confirm their credentials stay separate.
4. Unload jobs and remove user plugins, then revert as root. Verify machine-file removal
   and manual cleanup for an account that was logged out.
5. Reboot and run an MDM check-in. Confirm the directly written plist persists and Desktop
   reads it; macOS can rebuild Managed Preferences from installed profiles.
