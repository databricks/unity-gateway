# Claude Desktop deployment through MDM on macOS

Run from your MDM script or a terminal:

```sh
/opt/unitygateway/venv/bin/ug configure --agents claude --global \
  --workspace https://workspace.databricks.com
```

Machine writes use the existing elevation behavior: root writes directly, a terminal lets
sudo prompt, and unattended runs use `sudo -n`. No upfront root check is required.

This writes Desktop's managed plist and a root-owned organization plugin. The plist
contains inference settings, connectors, tool search, and optional tracing. The plugin
contains workspace skills/assets, connector guidance, and warming hooks, with no MCP
declarations. Desktop loads the organization plugin itself; no LaunchAgent, timer,
per-user marketplace registration, or `desktop-mdm-sync` command is installed.

Provisioning works before a user opens Cowork. It leaves the provisioning user's
Desktop profile, marketplace, plugin registration, and credentials untouched.

| Configure scope | Desktop destinations |
| --- | --- |
| Default or `--user` | User 3P JSON, user marketplace, and user registration/cache; root files untouched |
| Explicit `--global` | macOS managed plist, root organization plugin, and shared helpers |

`--global` also configures the requested CLI agents under their existing scope rules.
Windows/Linux skip Desktop under `--global`. Saved `global` scope does not change a
later default/user configure. Ordinary `ug revert` attempts both user and MDM cleanup.

## Prerequisites and authentication

Install shared Python/UG, Databricks CLI, Claude CLI, and Desktop before provisioning.
UG does not install shared runtimes. Python must import the installed UG package without
`PYTHONPATH`. Executables, packages, and their parent directories must be root-owned,
accessible to device users, and not writable by group/other users. Administrator-home
installs are rejected. Set PATH to shared, protected directories before running the command.
Obtain packages through your organization's approved distribution process.

Use a dedicated workspace hostname with at least one accessible Claude model. PAT/custom
OAuth and hosts requiring a workspace-ID selector are unsupported for Desktop MDM.
The workspace's managed configuration supplies models, headers/tracing, skills, and MCPs.
Without a managed config, UG discovers Claude models without managed skills/connectors.
An explicit enabled-agent list excluding Claude prevents provisioning.

Provisioning uses the existing `ug configure --global` authentication flow. For unattended
deployment, supply credentials through the same `DATABRICKS_BEARER` or
`DATABRICKS_BEARER_COMMAND` mechanism used by CLI configuration. Those mechanisms bypass
forced browser login; other authentication follows the existing configure behavior.
Desktop adds no service-principal credential handling or login rules. It validates shared
runtimes before using the common token helper for discovery and publication.
Use explicit agents (`--agents claude` / `--agent claude`) for MDM.

Provisioning reuses one token for discovery/download and never stores it in machine
files. Shared helpers discard provisioning credential overrides and use each user's
separate `UG_DESKTOP_<workspace-hash>` OAuth profile. Browser sign-in happens through
Desktop's existing credential helper. Connector consent and subscription relay sign-in
remain per-user. Existing MCP/tracing helpers keep their authentication behavior;
coexistence with a saved user PAT configuration is outside this release.

`--global` rejects `--mcp`, which registers user-level servers. Desktop receives
workspace-managed MCPs through the plist. `UG_SKIP_OS_SETTINGS` is incompatible with
MDM. Desktop provisioning errors warn and leave successful CLI configuration intact,
including unsupported Desktop authentication/workspaces, runtime prerequisites, failed
downloads, and unavailable elevation. Check warnings to confirm Desktop was provisioned. Desktop dry runs report
destinations without publishing files; other agents retain their dry-run behavior.

## Machine files and loading

| Location | Contents |
| --- | --- |
| `/Library/Managed Preferences/com.anthropic.claudefordesktop.plist` | Inference, `managedMcpServers`, `toolSearchEnabled`, optional tracing |
| `/Library/Application Support/Claude/org-plugins/ug` | Required organization plugin: skills/assets, connector guidance, warming hooks, version metadata |
| `/Library/Application Support/UnityGateway/claude-desktop-mdm` | Shared `ug` wrapper and optional tracing helper |

Plist values use Anthropic's documented string encoding, including JSON strings for
arrays, objects, and booleans. Connector entries keep the existing stdio `ug mcp-proxy`
transport and short aliases. User and machine settings share the same MCP entry renderer.
Tool search defers loading tool definitions into model context. It does not replace MCP
startup, credential refresh, or warming hooks.

UG writes the plist directly; it does not export a mobileconfig. See Anthropic's
[MDM reference](https://claude.com/docs/third-party/claude-desktop/mdm).
Remove competing mobileconfigs through their MDM owner. UG preserves an existing policy
whose credential helper belongs to another deployment. Its generated files and organization
plugin are replaced, including manual edits. Local administrator skills, project skills,
and independent plugins are not distributed.

Desktop loads the organization plugin from its root directory. Its manifest marks
installation as required, and `version.json` matches the content-derived manifest version.
No UG job copies the plugin into each user's marketplace cache. SessionStart and idle
UserPromptSubmit hooks still refresh user credentials and probe upstream connectors;
they do not fetch workspace policy or make Desktop reload skills.

## Updates, migration, and removal

Rerun provisioning through MDM to publish workspace changes, then fully quit and reopen
Desktop and start a new task. Running conversations do not gain newly published skills
or hooks. The organization-plugin loading and update path needs native Mac validation
before fleet rollout; filesystem checks alone do not establish GUI behavior.

Discovery and skill downloads finish before publication. A failed download leaves the
previous installation intact. Publication reuses the bundle replacement and atomic file
writers. There is no cross-file transaction or recovery ledger: after a partial publication
failure, rerun configure. Run provisioning and removal serially.

Before upgrading a machine from the earlier LaunchAgent-based PR build, quit Desktop.
Unload the old job in each active user session:

```sh
launchctl bootout gui/$(id -u)/com.databricks.unity-gateway.claude-desktop
```

Remove its plist and old root marketplace through MDM:

```sh
sudo rm -f /Library/LaunchAgents/com.databricks.unity-gateway.claude-desktop.plist
sudo rm -rf "/Library/Application Support/UnityGateway/claude-cowork-marketplace"
sudo rm -f "/Library/Application Support/UnityGateway/claude-desktop-mdm/deployment.json"
```

Remove earlier UG Cowork marketplace installations through Desktop's plugin settings or
native Claude commands for each account before enabling the organization plugin. This
also applies when migrating from ordinary user configure. UG does not visit user homes
or remove cached user plugins during root provisioning. Keeping an earlier UG plugin
installed can duplicate skills/hooks and, for older versions, connectors.

To revert UG configuration, including this deployment:

```sh
/opt/unitygateway/venv/bin/ug revert
```

This attempts to remove the managed plist, organization plugin, and shared helpers,
alongside the invoking user's UG profiles and plugin registrations and other agent cleanup.
User credentials and unrelated plugins remain. There are no revert scope flags. If MDM
cleanup cannot obtain elevation, UG warns, continues other cleanup, retains state for
retry, and exits with code 1. Run `ug revert` again with sudo available.
Restart Desktop after removal. MDM configuration
profiles can restore competing policies; remove those through their MDM owner.

## Native rollout checks

1. Provision a fresh Mac before its first Cowork account exists. Verify root ownership,
   shared executable paths, plist string encoding, `toolSearchEnabled`, and connector
   definitions only in `managedMcpServers`. Verify no UG Desktop LaunchAgent exists.
2. Open Desktop as a non-admin, sign in, and verify a real read-only connector call,
   a downloaded workspace skill, and both warming hooks. Confirm no duplicate UG tools
   and use `/context` to check that tool search defers connector schemas.
3. Publish a changed skill and connector set through MDM. Restart Desktop, start a new
   task, and verify the new skill, new connectors, and removed entries. Repeat after reboot
   and an MDM check-in to check managed-plist persistence and precedence.
4. Run `ug revert`, approve elevation if needed, restart Desktop, and verify user/machine
   UG configuration removal and preservation of unrelated data and plugins. Check that MDM does not republish a competing policy.

Automated component checks replace external APIs and ownership boundaries. Earlier Mac
tests established shared runtime/authentication and a Gmail call using the marketplace
version; they do not establish this organization-plugin delivery path end to end.
