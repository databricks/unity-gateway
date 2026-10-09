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
- Follow the existing module boundaries: CLI orchestration in `cli.py`, agent-specific behavior in `agents/<name>.py`, shared agent dispatch in `agents/__init__.py`, Databricks calls in `databricks.py`, skill download (UC fetch client + on-disk writer + download orchestration) in `skills_download.py`, MCP-connection state glue in `mcp.py`, and presentation helpers in `ui.py`. Skill download persists no disk state — it writes files to `--path` (or the home dir) and registers only the schema-less skills MCP connection. `ug configure skills` with no `--location` (or `--mcp` with no `--location`) registers that schema-less connection without downloading anything.
- Prefer existing helpers for config file writes, state persistence, UI messages, and Databricks authentication.
- Use `ucode.os_compatibility.subprocess_cross_os.run` / `ucode.os_compatibility.subprocess_cross_os.popen` for subprocesses; they resolve Windows npm wrappers automatically and default text streams to UTF-8 with replacement for invalid bytes. Keep commands as argument lists. Do not repeat these encoding defaults at callsites; explicit encoding/error overrides and binary mode remain supported. Ruff rejects direct `subprocess.run` / `subprocess.Popen` calls outside `os_compatibility/subprocess_cross_os.py` and tests.
- Use `acquire_exclusive_file_lock` / `release_file_lock` from `ucode.os_compatibility.file_lock_cross_os` instead of calling `fcntl.flock`, `fcntl.lockf`, or `msvcrt.locking` directly. Ruff enforces this outside the compatibility module and tests.
- Use `launcher.exec_or_spawn` when handing the terminal to an agent. Shell strings and explicit executable overrides keep their existing subprocess semantics.
- Add or update focused tests for behavior changes.
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
Explicit launch/session on/off controls still apply after version expansion.
Managed routing defaults must not rewrite already-resolved version flags.

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

This is the intended behavior; we are still working to make the code match it in every case. In particular, `ug` today also clears some fields that this list leaves alone: the `env.ANTHROPIC_DEFAULT_*_MODEL_NAME` companions, `env.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`, `env.CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`, and Codex `model_reasoning_effort`. Codex `[otel]` is never written to a file.

<details>
<summary>Claude Code</summary>

Fields live in `~/.claude/ucode-settings.json` and the OS-managed settings file (`/etc/claude-code/managed-settings.json` on Linux, `/Library/Application Support/ClaudeCode/managed-settings.json` on macOS), which takes precedence.

| Field | Without managed config | With managed config | Notes |
|-------|------------------------|---------------------|-------|
| `apiKeyHelper` | Create/replace | Create/replace | Gateway auth-token helper |
| `env.ANTHROPIC_BASE_URL` | Create/replace | Create/replace | Gateway endpoint URL |
| `env.ANTHROPIC_CUSTOM_HEADERS` | Merge | Merge | Merge `ug`'s routing and attribution headers by name; other header lines left alone. Admin headers added under managed config |
| `env.CLAUDE_CODE_API_KEY_HELPER_TTL_MS` | Create/replace | Create/replace | Auth-helper cache TTL |
| `env.ENABLE_PROMPT_CACHING_1H` | Create/replace | Create/replace | Prompt-caching flag |
| `env.ENABLE_TOOL_SEARCH` | Create/replace | Create/replace | Tool-search flag |
| `env.CLAUDE_CODE_USE_GATEWAY` | Create/replace | Create/replace | Gateway-routing flag |
| `env.ANTHROPIC_MODEL` | Ignore | Create/replace | Launch model, from the config's `default_model` or the budget recommendation when spend tiers are set; written at launch, not by `ug configure` |
| `env.ANTHROPIC_DEFAULT_*_MODEL` | Create/replace | Create/replace | Per-family default (opus, sonnet, haiku, fable); the config's family default wins under managed config |
| Model picker | Ignore | Create/replace | `availableModels`, `enforceAvailableModels`, `modelPicker`; only for a managed static model list |
| `permissions.deny` | Merge | Merge | Add `ug`'s own `WebSearch` rule when it replaces web search; other rules left alone |
| Tracing | Ignore | Create/replace | The seven `CLAUDE_CODE_*`/`OTEL_*` trace keys and `otelHeadersHelper`; only when the config enables tracing |
| `managedMcpServers` | Ignore | Merge | Add/update the config's MCP server entries; other entries left alone |
| Smart-routing hooks | Merge | Merge | `PreToolUse`, `SessionStart`, `SubagentStart`; only `ug`'s own marked handlers, other hooks left alone |
| Admin `agent_native_settings` | Ignore | Create/replace | Managed file only; copied as-is. Keys `ug` owns are skipped, `permissions.deny` and hooks are merged, and `ug`'s MCP servers are added to an admin `allowedMcpServers`. Removed when the admin drops them, unless edited by hand |
| Smart Router Orchestrator hooks | Merge | Merge | Launch-only `UserPromptSubmit` and compact `SessionStart` handlers for smart-routed sessions with `ENABLE_SMART_ROUTER_ORCHESTRATOR=1`; read the same session controls as routing |

</details>

<details>
<summary>Codex</summary>

Fields live in `~/.codex/ucode.config.toml` and `/etc/codex/managed_config.toml`.

| Field | Without managed config | With managed config | Notes |
|-------|------------------------|---------------------|-------|
| `model_provider` | Create/replace | Create/replace | Gateway provider (`Databricks`) |
| `model` | Ignore | Create/replace | The config's default model |
| `model_providers.Databricks` | Merge | Merge | Provider block: name, gateway base URL, wire API, `ug` auth command; other keys left alone |
| `http_headers` | Merge | Merge | In `[model_providers.Databricks]`; merge `ug`'s routing headers by name, admin headers added under managed config |
| `model_catalog_json` | Create/replace | Create/replace | In `~/.codex/config.toml`; `ug`'s own catalog reference, for a static model list |
| `mcp_servers` | Ignore | Merge | Managed file; add/update the config's MCP server entries, other entries left alone |
| Smart Router and Smart Router Orchestrator hooks | Merge | Merge | Launch-only `PreToolUse`, plus `UserPromptSubmit` and compact `SessionStart` with `ENABLE_SMART_ROUTER_ORCHESTRATOR=1`; Codex combines them with its native hook sources and applies project trust; `features.hooks` is enabled for a smart-routed launch |

</details>
