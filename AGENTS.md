# Agent Instructions

## Project

Unity Gateway (`ug`) is a Python CLI that configures and launches coding agents through Databricks
AI Gateway. Its internal Python package remains named `ucode` for backward compatibility.

The package code lives in `src/ucode/`.
Tests live in `tests/`.

## Commands

- Run the full test suite with `uv run pytest`.
- Run focused tests with `uv run pytest tests/<file>.py`.
- Run e2e tests with `UCODE_TEST_WORKSPACE=<db_workspace_url> uv run pytest tests/test_e2e.py -v`.
- Run the lint gate (ruff check + ruff format check + ty) with `just lint`; `just fix` autofixes. These are the same checks CI's Lint job runs. Without `just` installed, use `uvx --from rust-just just lint`, or run the underlying `uv run ruff ...` / `uv run ty ...` commands directly.
- Run the CLI from the current checkout with `uv run ug ...`.
- Reinstall the local checkout as the Unity Gateway tool with `uv tool install --reinstall .`.

## Development

- Use Python 3.12+.
- Keep changes scoped to the requested behavior.
- Follow the existing module boundaries: CLI orchestration in `cli.py`, CLI-agent behavior in `agents/<name>.py`, desktop-app adapters in `agents/desktop/` with CLI setup/cleanup orchestration in `agents/desktop/setup.py`, shared agent dispatch in `agents/__init__.py`, Databricks calls in `databricks.py`, skill download (UC fetch client + on-disk writer + download orchestration) in `skills_download.py`, MCP-connection state glue in `mcp.py`, and presentation helpers in `ui.py`. Skill download persists no disk state — it writes files to `--path` (or the home dir) and registers only the schema-less skills MCP connection. `ug configure skills` with no `--location` (or `--mcp` with no `--location`) registers that schema-less connection without downloading anything.
- Prefer existing helpers for config file writes, state persistence, UI messages, and Databricks authentication.
- Use `ucode.os_compatibility.subprocess_cross_os.run` / `ucode.os_compatibility.subprocess_cross_os.popen` for subprocesses; they resolve Windows npm wrappers automatically and default text streams to UTF-8 with replacement for invalid bytes. Keep commands as argument lists. Do not repeat these encoding defaults at callsites; explicit encoding/error overrides and binary mode remain supported. Ruff rejects direct `subprocess.run` / `subprocess.Popen` calls outside `os_compatibility/subprocess_cross_os.py` and tests.
- Use `acquire_exclusive_file_lock` / `release_file_lock` from `ucode.os_compatibility.file_lock_cross_os` instead of calling `fcntl.flock`, `fcntl.lockf`, or `msvcrt.locking` directly. Ruff enforces this outside the compatibility module and tests.
- Use `is_admin`, `windows_program_files_dir`, `windows_program_data_dir`, and `windows_system32_dir` from `ucode.os_compatibility.system_cross_os` instead of calling `IsUserAnAdmin` or reading `ProgramFiles`/`ProgramData`/`SystemRoot` directly.
- Use `launcher.exec_or_spawn` when handing the terminal to an agent. Shell strings and explicit executable overrides keep their existing subprocess semantics.
- Add or update focused tests for behavior changes.
- Never trigger a workflow with `pull_request_target`; `tests/test_integration_contract.py` rejects it. Use `pull_request` for checks that need no secrets, and a member-only `issue_comment` command that checks out the default branch (as in `fork-integration.yml`) for checks that need secrets on fork PRs.
- Do not modify generated or lock files unless the dependency graph intentionally changes.

## Smart-routing configuration

`SMART_ROUTER_CONFIG_VERSION` is the external selector. Version definitions live in
`src/ucode/smart_routing/config.py` under `_VERSIONS`; their managed environment keys
are registered in `SMART_ROUTING_ENV_KEYS` in `src/ucode/constants.py`.
An unset or empty selector preserves legacy environment-flag behavior.
A valid nonempty selector overrides every conflicting legacy value in that registry.
Do not use `setdefault` or preserve inherited values for version-owned parameters.
Resolve and materialize valid selectors at the CLI boundary before argument parsing or callbacks.
Unknown selectors are a no-op: do not apply a preset, mutate the inherited environment, or raise
an error. Existing legacy flags and explicit launch/session controls retain their behavior.
Restore the inherited environment on every exit.
`--disable-smart-routing` and session on/off controls still apply after version expansion.
`--enable-smart-routing` and managed smart routing turn routing on only through
`apply_enabled_version` in `config.py`: keep a known version, otherwise select
`DEFAULT_ENABLED_VERSION` (an unknown selector is replaced for that launch). Neither may rewrite
already-resolved version flags.

Every new `smart_routing` or `smart_router` environment variable must be registered in
`SMART_ROUTING_ENV_KEYS` in the same change, including orchestration controls.
Pass Claude's resolved launch baseline through native `pluginConfigs` options in the existing
launch settings. Keep the TypeScript static; toggles must not write configuration files. An explicit toggle
may delegate the session-control write to UG's CLI, which validates and replaces that file
atomically; the mod itself still writes no file.

### Adding a parameter

1. Define its environment-variable constant in `src/ucode/constants.py` and add it to
   `SMART_ROUTING_ENV_KEYS`.
2. Set an explicit value for it in **every** `_VERSIONS` entry, including existing versions.
   Choose values that preserve existing versions' behavior. Current parameters accept only
   the strings `"0"` and `"1"`; do not use booleans, empty strings, or omitted keys.
3. Add its consumer in the appropriate routing module. Use `resolve_environment` for
   config-aware reads, or the legacy flags materialized by `apply_config` at launch.
   Keep launch-scoped changes restorable and preserve legacy behavior without a selector.
4. `SMART_ROUTING_ENV_KEYS` controls environment snapshots, restoration, and launch/session
   off overrides. Keep routing activation limited to the V2 and subagent-only flags:
   orchestration alone must not enable routing. Add regression tests for the new parameter's
   controls. Session overrides must apply after version resolution.
   Update the native Claude mod's literal setters and registry coverage test in the same change.

`_validate_versions` runs at module import and rejects missing keys, unknown keys, and
invalid values. Do not weaken the complete-key check or infer required keys from `_VERSIONS`.
If a new parameter needs nonbinary values, add parameter-specific validation and tests.

### Adding a config type or revision

1. Add a complete mapping to `_VERSIONS` with an explicit suffix, such as `new_mode_v0`.
   For a changed existing mode, add `existing_mode_v1` rather than changing its `_v0` behavior.
   Do not add unsuffixed names or version aliases.
2. Define every key in `SMART_ROUTING_ENV_KEYS`; never rely on the caller's
   inherited environment to fill missing values.
3. Update the version table and examples in `README.md` and any affected bundled-skill docs.
4. Extend `tests/test_smart_routing_config.py` for the new mode, precedence over legacy flags,
   environment restoration, validation failures, and applicable session/launch behavior.
   Update unknown-version tests when a previously rejected version becomes supported.
   Follow `tests/AGENTS.md` and update its coverage READMEs.

Run `uv run pytest tests/test_smart_routing_config.py` plus relevant routing/CLI tests,
then `just lint`. These component checks do not establish live agent or gateway coverage.

## Style

- Keep user-facing CLI errors actionable.
- Use warnings for recoverable setup problems and errors for launch/runtime blockers.
- Preserve existing Rich UI conventions, including `print_warning`, `print_err`, `print_success`, `print_section`, and `spinner`.
- Avoid broad refactors while fixing a narrow bug.

## Fields `ug` manages

This is the allow list of fields `ug` owns. Every field not listed is left
alone. For each listed field, `ug` does one of:

- **Create/replace**: `ug` owns the whole value and writes it.
- **Merge**: `ug` writes only its own entries within a shared value and leaves the rest alone.
- **Ignore**: `ug` does not touch the field.

The two columns show what `ug` does without and with a [managed config](https://docs.databricks.com/aws/en/ai-gateway/coding-agent-configure-govern), the coding agent config admins publish through the API.

This is the intended behavior; we are still working to make the code match it in every case. In particular, `ug` today also clears some fields that this list leaves alone: the `env.ANTHROPIC_DEFAULT_*_MODEL_NAME` companions, `env.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`, `env.CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`, and Codex `model_reasoning_effort`.

<details>
<summary>Claude Code</summary>

Fields live in `~/.claude/ucode-settings.json` and the OS-managed settings file (`/etc/claude-code/managed-settings.json` on Linux, `/Library/Application Support/ClaudeCode/managed-settings.json` on macOS, `C:\Program Files\ClaudeCode\managed-settings.json` on Windows), which takes precedence.

| Field | Without managed config | With managed config | Notes |
|-------|------------------------|---------------------|-------|
| `apiKeyHelper` | Create/replace | Create/replace | Gateway auth-token helper |
| `env.ANTHROPIC_BASE_URL` | Create/replace | Create/replace | Gateway endpoint URL |
| `env.ANTHROPIC_CUSTOM_HEADERS` | Merge | Merge | Merge `ug`'s routing and attribution headers by name; other header lines left alone. Admin headers added under managed config |
| `env.CLAUDE_CODE_API_KEY_HELPER_TTL_MS` | Create/replace | Create/replace | Auth-helper cache TTL |
| `env.ENABLE_PROMPT_CACHING_1H` | Create/replace | Create/replace | Prompt-caching flag |
| `env.ENABLE_TOOL_SEARCH` | Create/replace | Create/replace | Tool-search flag |
| `env.ANTHROPIC_AUTH_TOKEN`, `env.ANTHROPIC_API_KEY` | Create/replace | Create/replace | Blanked (`""`) so a shell credential can't outrank `apiKeyHelper` or the subscription login (Claude Code ranks both variables above them); a settings `env` value beats the shell's for the Claude process only. Always in `ucode-settings.json`, relayed launches included. Like the header mod, `ug configure` writes them to the managed file, and a plain launch never adds them, so another writer of the file that drops them isn't fought on every launch; present ones are left as they are. Admin `agent_native_settings` can't set them |
| `env.CLAUDE_CODE_USE_GATEWAY` | Create/replace | Create/replace | Gateway-routing flag |
| `env.ANTHROPIC_MODEL` | Ignore | Create/replace | The config's `default_model`, written by `ug configure` and identically by a plain launch (launch never changes it). A budget recommendation that differs, `--model`, or a forwarded `-- --model` reaches only that session through Claude Code's native `--model` flag, which outranks a managed `ANTHROPIC_MODEL`; no file is written |
| `env.ANTHROPIC_DEFAULT_*_MODEL` | Create/replace | Create/replace | Per-family default (opus, sonnet, haiku, fable); the config's family default wins under managed config. Without a managed config, left as the OS-managed file has them (model discovery skipped) when that file has a `modelPicker` with family defaults |
| Model picker | Ignore | Create/replace | `availableModels`, `enforceAvailableModels`, `modelPicker`; only for a managed static model list |
| `permissions.deny` | Merge | Merge | Add `ug`'s own `WebSearch` rule when it replaces web search; other rules left alone |
| Tracing | Ignore | Create/replace | The seven `CLAUDE_CODE_*`/`OTEL_*` trace keys and `otelHeadersHelper`; only when the config enables tracing |
| `managedMcpServers` | Ignore | Merge | Add/update the config's MCP server entries; other entries left alone |
| Smart-routing hooks | Merge | Merge | `PreToolUse`, `SessionStart`, `SubagentStart`; only `ug`'s own marked handlers, other hooks left alone |
| Admin `agent_native_settings` | Ignore | Create/replace | Managed file only; copied as-is. Keys `ug` owns are skipped, `permissions.deny` and hooks are merged, and `ug`'s MCP servers are added to an admin `allowedMcpServers`. Removed when the admin drops them, unless edited by hand |
| Smart Router Orchestrator hooks | Merge | Merge | Launch-only `UserPromptSubmit` and compact `SessionStart` handlers for smart-routed sessions with `ENABLE_SMART_ROUTER_ORCHESTRATOR=1`; read the same session controls as routing |
| Header mod | Merge | Merge | Managed file only, Linux and macOS: `extraKnownMarketplaces.ucode-managed`, `enabledPlugins["ug-headers@ucode-managed"]`, and `ug-headers@ucode-managed` first in `prependPlugins` (an unset list becomes the mod plus `sec-default@builtin`). The mod's files live in a root-owned `ucode-plugins` folder beside the managed file and add `UG_HEADERS` to each request. Installed, upgraded, and enabled only by `ug configure`, never by a launch (even with `UG_HEADERS`, which instead warns when the mod is missing or stale), so a launch writes no machine-wide files and another writer of the file that drops them isn't fought on every launch. The entries are written only once the files are in place; revert removes both, keeping `sec-default@builtin` when an admin has extended the list |

</details>

Claude Desktop third-party profiles live under `Claude-3p/configLibrary` in
`~/Library/Application Support` on macOS, `%LOCALAPPDATA%` on current Windows releases,
and `~/.config` on Linux. Windows and Linux require Desktop to have opened once.
UG reserves UUID `5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6` for its fully owned profile.
Configure replaces the whole file, updates its metadata entry, and selects it;
changing workspaces replaces the same profile. Revert deletes that file and entry.
Unrelated profiles and metadata are preserved. Only the previous selection is saved
in `~/.ucode/claude-desktop.json`; profile contents are not restored on revert.
Old random-ID and workspace-derived profiles are left untouched.
Desktop setup is configure-only and best-effort. Writes retain atomic replacement,
failure rollback, and the Desktop-only five-second lock timeout.
Desktop filters both static and discovered model IDs with case-insensitive `\bclaude-`;
shared discovery and Claude Code lists are unchanged. If no IDs match, setup warns without writes.
When `claude_otel_tracing` is enabled, Desktop's reserved profile also owns
`otlpEndpoint`, `otlpProtocol`, `otlpHeadersHelper`, and `otlpTracesEnabled`. A generated
`.sh`/`.cmd` wrapper pins the configuring Python installation, workspace, and profile
for the existing `otel-headers` command. Native sessions rerun it as tokens rotate;
no bearer is persisted. Disable/revert removes the wrapper, with atomic writes and
rollback shared with the profile. Disabled tracing omits these fields on replacement.
Claude setup also installs a generated Cowork plugin containing only the current
workspace's managed and user-downloaded home-level skills and Claude-targeted MCP servers.
Downloaded skill additions and removals resync the plugin; project-local,
user-authored, and other-workspace skills are excluded. The source lives at
`~/.ucode/claude-cowork-marketplace`, and `~/.ucode/claude-cowork.json` records the selected
account/org root for cleanup. Cowork must have started once. Setup and cleanup use scoped
`claude plugin ... --cowork --json` commands with `CLAUDE_CONFIG_DIR`; failures stay best-effort.
Cowork uses short service-first aliases for generated UC MCP names, with collision qualifiers;
custom registration names and full proxy URLs are preserved. Its name and display name
are `ug`, with plugin ID `ug@unity-gateway-cowork`. Reconfigure removes the old
`unity-gateway@unity-gateway-cowork` registration only after installing/updating `ug`;
revert removes both IDs. New sessions use the `mcp__plugin_ug_...` tool namespace.
See `docs/claude-desktop.md` for native validation limits and revert behavior.

<details>
<summary>Codex</summary>

Fields live in `~/.codex/ucode.config.toml` and `/etc/codex/managed_config.toml`. On Windows the managed file is `C:\ProgramData\OpenAI\Codex\requirements.toml`, which omits `model` and `mcp_servers` and adds `env_http_headers` for the launch-scoped headers.

| Field | Without managed config | With managed config | Notes |
|-------|------------------------|---------------------|-------|
| `model_provider` | Create/replace | Create/replace | Gateway provider (`Databricks`) |
| `model` | Ignore | Create/replace | The config's default model |
| `model_providers.Databricks` | Merge | Merge | Provider block: name, gateway base URL, wire API, `ug` auth command; other keys left alone. A relayed (ChatGPT subscription) provider sets `requires_openai_auth` instead of the auth command and skips the managed file |
| `http_headers` | Merge | Merge | In `[model_providers.Databricks]`; merge `ug`'s routing headers by name, admin headers added under managed config. `UG_HEADERS` names not already present are added to the launch's `-c` flags only, never a file |
| `model_provider`, `model_providers.DatabricksRelay` (app) | Ignore | Ignore | In `~/.codex/config.toml`, only for a relayed (ChatGPT subscription) provider, for Codex desktop, and only by `ug configure` (like Claude Desktop): a loopback relay block with the MPS header served by the login service. Its own name keeps a managed `Databricks` provider from merging into it, and marks it as ug's to replace or remove; a user-defined `model_provider` is left alone |
| `model_catalog_json` | Create/replace | Create/replace | In `~/.codex/config.toml`; `ug`'s own catalog reference, for a static model list |
| `otel.trace_exporter` | Ignore | Create/replace | In `~/.codex/config.toml` when the config enables tracing, for Codex desktop; another exporter and other telemetry settings left alone |
| `mcp_servers` | Ignore | Merge | Managed file; add/update the config's MCP server entries, other entries left alone |
| Smart Router and Smart Router Orchestrator hooks | Merge | Merge | Launch-only `PreToolUse`, plus `UserPromptSubmit` and compact `SessionStart` with `ENABLE_SMART_ROUTER_ORCHESTRATOR=1`; Codex combines them with its native hook sources and applies project trust; `features.hooks` is enabled for a smart-routed launch |

</details>
