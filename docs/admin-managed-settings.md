# Deploy published agent settings to managed devices

Use this guide to apply a workspace's published coding-agent configuration to Claude Code and
Codex on devices managed by IT. `ug configure --global` builds the agents' machine-wide files from
that configuration. Run it from an MDM policy, a provisioning tool, or an elevated terminal. A
developer then runs `ug claude` or `ug codex` to sign in with their own Databricks account.

This guide covers the Claude Code and Codex CLIs on macOS, Linux, and Windows. [Claude Desktop
deployment through MDM](claude-desktop-mdm.md) has separate macOS requirements. For the files,
fields, and exit codes, see the [admin-managed settings reference](admin-managed-settings-reference.md).

## Prepare the workspace and devices

1. [Publish a coding-agent configuration](https://docs.databricks.com/aws/en/ai-gateway/coding-agent-configure-govern)
   in the workspace with Claude Code, Codex, or both enabled.
   Set gateway fields such as `models`, `default_models`, `http_headers`, `tracing`, and `mcp_servers`
   there. Use `agent_native_settings` or `agent_native_config.claude_code.managed_settings` for
   Claude Code keys. Use `agent_native_config.codex.requirements` for enforced Codex rules and
   `agent_native_config.codex.config` for Codex defaults. See the [native field
   examples](admin-managed-settings-reference.md#native-fields) for the JSON shape.
2. Install `ug`, its Python runtime, and the agents in locations that every device user can run.
   Keep the shared installation outside a user's home directory. Codex 0.156.0 or newer is needed
   for the gateway provider lock. The [MDM script index](../scripts/mdm/README.md) lists macOS and
   Windows scripts. The [macOS Jamf guide](../scripts/mdm/macos-setup.md) covers service-principal
   setup.
3. Give the provisioning identity read access to the published configuration and any MCP servers
   it names. Use an unattended credential source for a fleet job. The provisioning credential
   reads the policy; it does not become a developer's sign-in.
4. Choose one writer for each agent's machine-wide files. Each managed run rebuilds those files,
   including the admin's native settings, and replaces earlier hand edits.

Claude Code enforces native settings in its managed file. Codex enforces only the keys its
`requirements.toml` supports; `config.toml` supplies defaults that developers can override.

On Windows, Codex has a machine-wide `requirements.toml` but no machine-wide `config.toml` or
model-catalog defaults layer. If the published configuration sets an effective
`agent_native_config.codex.config` value or `default_models.default_model`, the run exits 2 before
writing files. A static model list reaches users who launch with `ug codex`, but cannot restrict
bare `codex`. Put Codex defaults in each user's configuration if you need them on Windows.

## Preview the files on a test device

Use the same `ug` version and operating system that the fleet uses. To preview the published
configuration, run:

```sh
ug configure --global --workspace https://<workspace> --agents claude,codex --dry-run
```

To inspect a proposed configuration before publishing it, save one JSON configuration object in
the format produced by `ug export --file draft.json`, then run:

```sh
ug configure --global --workspace https://<workspace> --agents claude,codex \
  --dry-run --from-file draft.json
```

The preview lists each file it would create, replace, remove, or leave unchanged. It prints the
contents of files it would create or replace, and a per-agent report. Review `[skipped]` keys and
warnings: a skipped native key either belongs to Unity Gateway or conflicts with a feature. The
[key classes and ownership tables](admin-managed-settings-reference.md#key-classes) give the
reason for each key. A preview writes no machine-wide files. When resolving
workspace MCP servers, the run still needs access to those servers. The preview composes the files;
the test device launch confirms that the installed agent accepts the native values.

## Apply the configuration

Run the command from the fleet job on every device and repeat it on the job's regular schedule:

```sh
ug configure --global --workspace https://<workspace> --agents claude,codex
```

Run the job as root on macOS or Linux, or as an elevated administrator or SYSTEM on Windows. An
interactive macOS or Linux run can prompt for `sudo`; an unattended job needs root or working
`sudo -n`. `--agents` limits which agent files the run rebuilds. When the workspace changes, the
run rebuilds both agents so the other agent cannot keep pointing at the old workspace.

On macOS, a run that selects Claude Code also attempts [Claude Desktop MDM
provisioning](claude-desktop-mdm.md). Desktop provisioning failures warn without failing the CLI
agent setup. Check the job's warnings if Desktop is part of your deployment.

Check the job's exit code and file-action report. Exit 0 includes files already current and files
removed because an agent is no longer enabled. Exit 5 means a device that previously used a
published configuration could not find one now; its existing files remain in place. The
[exit-code table](admin-managed-settings-reference.md#exit-codes) covers other failures. After a
successful run, other `ug` commands use the saved configuration and leave the machine-wide files
alone. A changed admin setting reaches a device on its next managed run.

## Verify a developer session

On a test device, sign in as a standard user and run `ug claude` or `ug codex`. The first launch
saves that user's own settings and signs them in to Databricks. Later launches do not rewrite the
machine-wide files. Run `ug status` to confirm that the machine-wide settings come from the
published configuration. Check an agent launch against an allowed model.

After that first `ug` launch, plain `claude` or `codex` also reads the machine-wide gateway and
native settings. Use `ug claude` or `ug codex` for models that need per-user discovery, tracing,
and smart routing. On macOS and Linux, an explicit static Codex model list is also installed as a
machine-wide catalog for plain `codex`. On Windows, that list applies only through `ug codex`.

## Update or remove settings

Publish a changed configuration and rerun the fleet job to replace the files. If you remove an
agent from `enabled_agents`, the next run removes that agent's files. If you unpublish the entire
configuration, a device that already used it keeps its files and the job exits 5. To remove the
published configuration from a device, run `ug revert` as root on macOS or Linux, or from an
elevated terminal on Windows. An ordinary developer's `ug revert` removes only their own settings.

The [design explanation](admin-managed-settings-explained.md) describes why the files are rebuilt
and which values stay per user.
