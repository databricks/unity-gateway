# Agent Instructions

## Project

Unity Gateway (`ug`) is a Python CLI that configures and launches coding agents through a
Databricks workspace. Its internal Python package remains named `ucode` for backward compatibility.

The package code lives in `src/ucode/`.
Tests live in `tests/`.

## Commands

- Run the full test suite with `uv run pytest`.
- Run focused tests with `uv run pytest tests/<file>.py`.
- Run e2e tests with `UCODE_TEST_WORKSPACE=<db_workspace_url> uv run pytest tests/test_e2e.py -v`.
- Run the lint gate (ruff check + ruff format check + ty) with `just lint`; `just fix` autofixes. These are the same checks CI's `Checks` job runs. Without `just` installed, use `uvx --from rust-just just lint`, or run the underlying `uv run ruff ...` / `uv run ty ...` commands directly.
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
- Resolve a workspace URL from the user (`--workspace`, `UG_WORKSPACE`, a `--profile`'s host, the interactive picker) with `workspace.resolve_workspace` before using it as a `state.json` key or comparing it with saved state. It keeps the workspace id on a host that serves several workspaces and returns the key earlier versions saved on any other host. Build a key with `workspace_key` (or `state_workspace_key` from a loaded state) and split one with `split_workspace_key`; never concatenate `?w=` by hand.
- On a host that serves several workspaces, every request to it must carry the selected workspace:
  - ug's own HTTP clients merge `workspace_headers(url)` into their headers (it returns nothing for other hosts and single-workspace hosts).
  - A ug helper that an agent or an OS hook starts (`ug mcp-proxy`, `ug otel-*`, router and Cowork hooks, etc.) gets `*workspace_id_args(host)` on its argv, and its command takes a hidden `--workspace-id` and calls `workspace.pin(host, workspace_id)` before any request.
  - Agent configs and launch headers are built from the saved `workspace_id` (`selector_headers`).
  - Add a test beside `tests/test_workspace_selection.py::TestHelperArgv` for each new helper or client.
- Do not pass an `ssl` context or `verify=` to an HTTP client. `tls.apply()` selects the trust store (`UG_TLS_TRUST_STORE`, then `tls_trust_store` in `state.json`, default `os`) for the whole process; call it first in the `main()` of any new module started with `python -m`. Build a `network error:` reason for a failed request with `databricks._network_error_reason`, which adds the certificate hint, and give a `uv` child that installs packages `env=tls.uv_environment()`.
- Use `launcher.exec_or_spawn` when handing the terminal to an agent. Shell strings and explicit executable overrides keep their existing subprocess semantics.
- Every child process inherits `UG_HEADERS`, including an agent started from another agent's session. The gateway records `Databricks-Custom-Coding-Agent` ahead of `User-Agent`, so the root callback drops an inherited one when it launches an agent (bare `ug` or `_AGENT_LAUNCH_COMMANDS` in `cli.py`); helper subcommands such as `mcp-proxy` keep it, since they run under the agent their parent launched. Add a new agent command outside `TOOL_ALIASES` to `_AGENT_LAUNCH_COMMANDS`, or it reports the parent's agent.
- Add or update focused tests for behavior changes.
- In live agent tests, assert completed outcomes and correlated request/response semantics.
  Do not require an exact number or order of model, tool, subagent, or routing calls merely
  because a prompt requests it. Native recovery and agent planning can produce extra calls.
  Require relevant evidence and validate every matching call; use exact counts only when
  cardinality is the contract being tested, such as routing disabled or one selection per response.
- Never trigger a workflow with `pull_request_target`; `tests/test_integration_contract.py` rejects it. Use `pull_request` for checks that need no secrets. For checks that need secrets on fork PRs, use the default-branch `issue_comment` workflows. Only members/owners may request a run that executes PR code with CI secrets (as in `fork-integration.yml`), after reviewing the changes. Review commands that run no PR code (`ug-review.yml`) may also let GitHub-classified `CONTRIBUTOR`s request their own PR; first-time contributors need a member.
- Do not modify generated or lock files unless the dependency graph intentionally changes.

## CLI performance metrics

Use `from ucode import metrics` for instrumentation. The recorder and helpers live in
`src/ucode/metrics/__init__.py`; `metrics/exporter.py` aggregates observations with the
OTel SDK and uploads OTLP to `/api/2.0/ai-gateway/ug-cli/otel/v1/metrics` in a detached process.
Recording is enabled only by `UG_ENABLE_METRICS_EXPORT=1` and defaults off.

- Keep `begin`/`finish` at the CLI lifecycle boundary and `start_agent`/`agent_handoff`
  around agent startup. The Typer callback uses `set_command()` to attribute observations
  to the parsed command. Individual operations must not create or replace the recorder.
- Call `metrics.bind(workspace, token)` where an operation obtains, reuses, or refreshes
  its workspace token. Reuse those credentials; never authenticate solely for telemetry.
  Use the selected workspace key, including its workspace selector. Observations recorded
  before the first binding belong to that first workspace; pass `workspace=` explicitly
  when an operation targets a different workspace.
- Use metric-name constants from `metrics`, such as
  `@metrics.timed(metrics.AGENT_CONFIGURATION_DURATION_MS)`, for a whole synchronous
  operation including its retries. The decorator binds existing `workspace`/`token`
  arguments and derives the error operation by removing `_duration_ms` from the name.
  For a smaller block, use `metrics.measure(metrics.DEPENDENCY_INSTALL_DURATION_MS,
  operation=metrics.DEPENDENCY_INSTALL_OPERATION, dependency=tool, action="install")`.
  `metrics.annotate(...)` adds labels discovered during that measurement; `metrics.record(...)`
  handles counters, byte sizes, or durations measured across callbacks. To attribute a
  measurement to an awaited operation's own duration — a timed caller that reuses a token
  another thread fetched untimed (the launch's speculative Databricks token fetch) reports
  that fetch once as the launch's cache miss — call `metrics.record_awaited_duration(name, ms)`
  inside the `measure` block, where `name` is the measured metric (it walks up to that scope). The value rides the measure scope outside the label set, so
  `record` can never export it as a sample attribute.
- Report handled errors with `metrics.failure(operation, error, ...)`; set
  `continued_with_fallback=True` when execution recovers. Use the existing `phase` label
  (`startup`, `command`, or `agent_session`) to distinguish errors. Preserve the original
  result or exception; telemetry must never rerun an operation or change its exit status.
- Define new metric-name constants in `metrics/__init__.py` and register them in
  `DURATION_METRICS` or `COUNTER_METRICS`; add labels to the typed
  label allowlists. String values must be bounded identifiers; never record tokens, URLs,
  prompts, response bodies, or raw exception messages. A new non-duration histogram also
  needs an entry in `METRICS` and its unit and bucket configuration in `exporter.py`.
- Keep OTel SDK imports and uploads in the exporter. A daemon starts the detached exporter
  when recording is enabled; command threads hand off observations through an unbuffered,
  nonblocking pipe. The exporter holds at most 1,000 observations and uploads every 60 seconds,
  on a flush signal, or after EOF when the CLI exits or execs an agent. Recording and shutdown
  must never wait for locks, process startup, pipe capacity, or networking. A bounded startup
  backlog handles helper startup; contention and backpressure may still drop telemetry.
- Test new instrumentation with the `metrics_recorder` fixture and the relevant operation
  tests. Run `tests/test_metrics.py`, `tests/test_metrics_isolation.py`, and `just lint` for
  recorder/exporter changes; preserve disabled-mode and stalled-export isolation coverage.

Startup instrumentation uses these constants (all in `metrics`):

| Constant | Where and how to use it |
| --- | --- |
| `UG_STARTUP_DURATION_MS` | Emitted by `agent_handoff()` or `finish()` from the entrypoint start time; operation code must not emit another startup sample. |
| `CLI_IMPORT_DURATION_MS` | `record()` in the entrypoint around importing the selected CLI implementation. |
| `DEPENDENCY_SETUP_DURATION_MS` | `timed()` around the complete bootstrap dependency check, with `agent` and `cli_version_check_skipped`; prompts mark `interactive_wait`. |
| `DEPENDENCY_INSTALL_DURATION_MS` | `timed()` around one dependency installer, or `measure()` around an install/update/downgrade subprocess with `dependency` and `action`. |
| `DEPENDENCY_INSTALL_OPERATION` | The matching error operation for a dependency-install `measure()` block; the duration constant is derived from it. |
| `AGENT_CONFIGURATION_DURATION_MS` | `timed()` around `configure_tool()`, with the selected `agent` and `config_scope`. |
| `SKILLS_REFRESH_DURATION_MS` | `measure()` around checks and updates for existing downloaded skills, after no-work returns; annotate `deadline_reached` after the bounded refresh. |
| `SKILLS_REFRESH_OPERATION` | `failure()` for a handled refresh error that lets launch continue; the duration constant is derived from it. |
| `COMMAND_OPERATION` | `failure()` in CLI exception handlers resolves this to the parsed command name and ignores cancellation and propagated agent exit codes. |
| `UG_CLI_ERROR` | Emitted by `failure()` with phase and sanitized error labels; call that helper rather than incrementing the counter directly. |


Authentication and API instrumentation uses these constants (all in `metrics`):

| Constant | Where and how to use it |
| --- | --- |
| `DATABRICKS_TOKEN_FETCH_DURATION_MS` | `timed()` around a token fetch, including refresh or login, with `token_source` (`environment`, `command`, `cli`, or `sdk`) and `in_process_cache_hit`; SDK imports happen before timing. |
| `DATABRICKS_TOKEN_FETCH_OPERATION` | The error operation for a token-fetch `measure()` block; the duration name is derived from it. |
| `DATABRICKS_LOGIN_DURATION_MS` | `timed()` or `measure()` around browser login, with `login_method` (`cli` or `sdk`); call `mark_login()` when login starts so startup samples include `interactive_login`. |
| `DATABRICKS_LOGIN_OPERATION` | The error operation for login measurements and `failure()` on a nonzero login-process exit; report the exit code without changing the caller's retry behavior. |
| `CODING_AGENT_CONFIG_FETCH_DURATION_MS` | `timed(reason_result=True, continued_with_fallback=True)` around fetching the workspace's managed coding-agent configuration. |
| `MODEL_SERVICE_DISCOVERY_DURATION_MS` | `timed(reason_result=True, continued_with_fallback=True)` around listing and classifying model services, with `cache_hit` for the in-process catalog cache. |
| `MODEL_SERVICE_ACCESS_CHECK_DURATION_MS` | `timed()` around the model-services access check, including all pages it reads. |
| `HTTP_REQUEST_DURATION_MS` | `record()` once per HTTP attempt, including failures, with `api_operation`, `http_method`, `attempt`, and `http_status_code` when a response exists; excludes retry backoff. |
| `HTTP_RETRY_COUNT` | `record(..., 1)` before each retry, with `api_operation` and `http_method`; never count the first attempt or count an outer retry twice. |

HTTP helpers annotate the enclosing operation with its final response status. Pass
`http_status_code=None` after a network failure with no response to clear an earlier
attempt's status. Use `http_attempt()` to number retries managed by an outer API helper;
`http_result` reports only the final failure at that boundary.

Usage and routing instrumentation uses these constants (all in `metrics`):

| Constant | Where and how to use it |
| --- | --- |
| `UG_USAGE_DURATION_MS` | `timed()` around `usage_cmd()`, from Databricks CLI setup through workspace/token lookup, budget retrieval, and report rendering, with `show_all` reflecting `--all`. |
| `ROUTING_DECISION_DURATION_MS` | `timed(reason_result=True, continued_with_fallback=True)` around one `select_route()` call, including request construction, the HTTP request, response decoding, and selection validation, with `routing_trigger` set to `first_prompt` or `subagent` and `http_status_code` when a response is available. |
| `ROUTE_VALIDATION_DURATION_MS` | `timed(reason_result=True, continued_with_fallback=True)` around the startup and `/smart-router on` `routes:validate` calls, including the HTTP request and its 10 s deadline. An HTTP 404 leaves routing on; any other failure, a missed deadline, or an unsupported list turns it off at startup and on `/smart-router on`. Its HTTP attempt uses `api_operation="routes_validate"`. |

All three durations are recorded on success and failure. Use `subagent_routing()` around
subagent route selection so its trigger is scoped to that decision. Each routing HTTP
attempt also uses `HTTP_REQUEST_DURATION_MS` with `api_operation="routes_select"`,
`http_method="POST"`, `attempt=1`, and `http_status_code` when a response is available.

Proxy instrumentation uses these constants (all in `metrics`):

| Constant | Where and how to use it |
| --- | --- |
| `PROXY_REQUEST_DURATION_MS` | `record()` once when the proxy handler finishes forwarding or stops on an error/disconnect, measured from handler entry and including body reads, token lookup, the auth retry, and response streaming; label `client_disconnected`. |
| `PROXY_UPSTREAM_HEADERS_DURATION_MS` | `record()` when each upstream response's headers arrive, measured from the start of forwarding, with `attempt=1` or `2`; the second sample includes the first attempt and token refresh. |
| `PROXY_FIRST_BYTE_DURATION_MS` | `record()` on the first nonempty chunk of the response selected for forwarding, measured from the start of forwarding before writing that chunk; includes an auth retry and is absent for empty responses. |
| `PROXY_RESPONSE_BYTES` | `record()` once per handled request with the response body bytes written and flushed to the client, including partial responses; excludes headers and the rejected auth response consumed before retry. |
| `PROXY_AUTH_RETRY_COUNT` | `record(..., 1)` immediately before retrying after an upstream 401/403; do not count the initial attempt. |
| `PROXY_REQUEST_OPERATION` | `failure()` once for a terminal proxy failure with `phase="agent_session"`; a recovered auth rejection or ordinary client disconnect does not emit an error. |

Proxy observations use `workspace=metrics_workspace` and labels `proxy_mode`
(`gateway_auth`, `relay`, or `otel`), `api_operation`, and `http_method`, plus
`http_status_code` once upstream headers arrive. Reuse the proxy's existing token
with `metrics.bind()`; never fetch credentials solely for metrics. Duration names
are registered in `DURATION_METRICS`, the retry counter in `COUNTER_METRICS`, and
`PROXY_RESPONSE_BYTES` in `METRICS` with byte units and `BYTE_BOUNDS` in the exporter.

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

Codex Auto is enabled only by `subagent_auto_v0`. Its native `/model` Auto row
opens a main-model selector through the session interposer. Keep the row and model changes
launch-only; never send the synthetic row ID to inference or save it as a default.
Manual model selection turns session routing off through the existing feedback control.

Claude Auto is enabled by `subagent_auto_v0` or the legacy
`subagent_auto_v0_claude_only` config version.
`ENABLE_SMART_ROUTER_AUTO_MODE` is an internal derived flag: that version sets it to
`"1"` for Claude, and the shared Auto version also sets it for Codex. Other versions set
it to `"0"`. Setting the flag directly,
managed smart routing alone, and an installed static Auto row cannot enable Auto.
Capture the selected version in a scoped Python context before materializing its
flags and consuming the environment selector. Restore that context on every exit;
do not leave the selector in child environments, where it could reapply a preset
over a session's routing overrides. Only an eligible exact-version launch supplies
`autoPicker` to the native mod. Routing off, first-prompt routing and print mode
never enter Auto.
Fresh Auto launches use the managed default model, or the first available model;
explicit model arguments and native resume retain precedence over startup Auto.
Fresh Auto session controls start OFF; native startup initializes the selected state
without treating a manual-model launch as a feedback-gated user disable. Ordinary
launches preserve installed Auto picker metadata without enabling the feature.
Explicit configure removes UG Auto aliases and picker metadata unless the exact Auto
version is selected and routing remains enabled. Revert removes them after restoring
backups, including aliases that a restored backup reintroduces. Cleanup respects user,
global, skip-OS and published-managed scopes, leaves foreign settings untouched, and
resolves a native saved Auto model to its allowed real target before removing its mapping.
An alias-only allowlist remains restrictive; unresolved targets fail before cleanup writes.
Revert retains captured alias targets in the existing UG state until cleanup succeeds, so
a failed native write can retry after source mappings were restored away. Live mappings
override retained targets; the final restored policy still determines allowed targets.
Ordinary launches and session toggles do not perform this cleanup.
Configure provisions Auto aliases only with the exact Auto version and effective routing
enabled. For a UG-owned picker
without managed smart routing, it sets `modelPicker.replaceBuiltInOptions=false`.
When managed Claude `smart_routing_enabled` is true AND the configure-time selector
is `SMART_ROUTER_CONFIG_VERSION=subagent_auto_v0` or the legacy
`subagent_auto_v0_claude_only`,
configure keeps `replaceBuiltInOptions=true` on the UG-owned static catalog and serves
Auto from one row inside it, first among UG's rows (below Claude's native Default row): the default variant's alias with label `Auto`, its
`modelOverrides` mapping, and both aliases and targets in `availableModels`. That mode
is written at configure only — launches preserve whichever installed mode the file
carries, and a launch's own version selection never installs or converts the catalog.
In that mode native always stays on the static row's alias (the launch passes it as
`staticModel` and starts on it), so `/model` checks Auto for every main model; the mod
holds the chosen main model in memory and its `$.store` preference and resolves the
real target from it. Main-model switches never run native `/model`.
Entering static Auto compares the held main model with the current model for cache warnings,
not the static alias's default target. Keeping the same main model skips the native warning.
Saving a different main model while Auto is active asks for confirmation before changing
the saved preference when the session has context. Cancellation keeps the current
main model and reopens the chooser. This check includes cold contexts and does not claim
that a warm cache exists. Other hooks' blocks and explicit confirmation requests still apply.
The main-model chooser opens only after the person confirms the Auto row in `/model` and
the picker closes, or on `/model auto`; never when `/model` opens, on a non-Auto pick, or
on Esc. The chooser presents every allowed main model in one scrollable list with the
current model first. The chooser names the current model and explains that subagents will be smart routed.
Native displays `Auto mode: Subagents smart routed.` before the chooser opens. Saving updates the prompt hint row's
`Auto (<main model>)` without writing a confirmation to the transcript. Native `/effort`
continues to work independently. A fresh launch
without a launch-only model starts on the stored main model; a per-model alias selected
natively (an earlier version's saved selection) still counts as Auto with that model.
Native context window and effort follow the static row (the default variant) for every
main model; the mod's prompt hint row names the held main model, not the row's alias.
The config version controls Auto activation; installed picker metadata does not. Configure
preserves foreign admin
picker/custom option settings. Claude owns model-specific effort capabilities and defaults.
Claude Auto configures immutable per-main-model aliases in `modelOverrides` and
permits only aliases whose real targets are allowed. Configure removes stale UG
aliases and sets `modelPicker.replaceBuiltInOptions` false only for UG-owned
pickers so the native custom option is visible. Preserve admin custom model options
and replacement pickers; skip Auto when they occupy or hide its native custom slot.
Personal configure does not discover or persist a picker for Auto; unrestricted
personal launches and foreign non-replacing pickers build Auto aliases in launch-only
settings when managed settings define neither `modelOverrides` nor `availableModels`.
Managed denials still filter aliases and their real targets. Only the launcher/mod sets `ANTHROPIC_CUSTOM_MODEL_OPTION` and
its display companions in the process environment: putting these in settings env
would reset the pointer whenever native `/model` saves a selection. Keep one Auto
row; aliases must not be separate modelPicker rows. Preserve other overrides and
regular picker rows. `/model auto` selects Auto and opens its main-model selector.
Keep `/auto` hidden as an internal selector command. Native context,
effort, inference target and indicator derive from the selected alias, never another
session's saved preference. The mod stores the per-workspace preference with native
`$.store` and never writes configuration files. An ordinary `/model` selection
disables session routing. Managed configuration changes require explicit configure;
a launch never installs or rewrites machine-wide configuration.

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
- **Launch-only (-c), never written**: `ug` overrides the value for that session without changing the saved file.

The two columns show what `ug` does without and with a [managed config](https://docs.databricks.com/aws/en/ai-gateway/coding-agent-configure-govern), the coding agent config admins publish through the API.

This is the intended behavior; we are still working to make the code match it in every case. In particular, `ug` today also clears some fields that this list leaves alone: the `env.ANTHROPIC_DEFAULT_*_MODEL_NAME` companions, `env.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`, `env.CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`, and Codex `model_reasoning_effort`.

<details>
<summary>Claude Code</summary>

Fields live in `~/.claude/ucode-settings.json` and the OS-managed settings file (`/etc/claude-code/managed-settings.json` on Linux, `/Library/Application Support/ClaudeCode/managed-settings.json` on macOS, `C:\Program Files\ClaudeCode\managed-settings.json` on Windows), which takes precedence.

| Field | Without managed config | With managed config | Notes |
|-------|------------------------|---------------------|-------|
| `apiKeyHelper` | Create/replace | Create/replace | Gateway auth-token helper |
| `env.ANTHROPIC_BASE_URL` | Create/replace | Create/replace | Gateway endpoint URL |
| `env.ANTHROPIC_CUSTOM_HEADERS` | Merge | Merge | Merge `ug`'s routing and attribution headers by name; other header lines left alone. Admin headers added under managed config. On a host that serves several workspaces, `X-Databricks-Org-Id` and `X-Databricks-Workspace-Id` are added last, so an admin header can't change the workspace |
| `env.CLAUDE_CODE_API_KEY_HELPER_TTL_MS` | Create/replace | Create/replace | Auth-helper cache TTL |
| `env.ENABLE_PROMPT_CACHING_1H` | Create/replace | Create/replace | Prompt-caching flag |
| `env.ENABLE_TOOL_SEARCH` | Create/replace | Create/replace | Tool-search flag |
| `env.ANTHROPIC_AUTH_TOKEN`, `env.ANTHROPIC_API_KEY` | Create/replace | Create/replace | Blanked (`""`) so a shell credential can't outrank `apiKeyHelper` or the subscription login (Claude Code ranks both variables above them); a settings `env` value beats the shell's for the Claude process only. Always in `ucode-settings.json`, relayed launches included. Like the header mod, `ug configure` writes them to the managed file, and a plain launch never adds them, so another writer of the file that drops them isn't fought on every launch; present ones are left as they are. Admin `agent_native_settings` can't set them |
| `env.CLAUDE_CODE_USE_GATEWAY` | Create/replace | Create/replace | Gateway-routing flag |
| `env.ANTHROPIC_MODEL` | Ignore | Create/replace | The config's `default_model`, written by `ug configure` and identically by a plain launch (launch never changes it). A budget recommendation that differs, `--model`, or a forwarded `-- --model` reaches only that session through Claude Code's native `--model` flag, which outranks a managed `ANTHROPIC_MODEL`; no file is written |
| `env.ANTHROPIC_DEFAULT_*_MODEL` | Create/replace | Create/replace | Per-family default (opus, sonnet, haiku, fable); the config's family default wins under managed config. Without a managed config, left as the OS-managed file has them (model discovery skipped) when that file has a `modelPicker` with family defaults |
| Model picker | Ignore | Create/replace | `availableModels`, `enforceAvailableModels`, `modelPicker`; only for a managed static model list. A static list naming a model service whose primary destination is a subscription-relay MPS makes Claude Code CLI launch relayed for a user already signed in to a Claude subscription (credential swap, no `apiKeyHelper`; others keep gateway auth with a sign-in note) with the list still owning the picker; Claude Desktop keeps gateway auth for it (CLI-only for now) |
| `modelSettings.<Auto alias>.autoCompactWindow` | Launch-only | Launch-only | For an Auto alias without a native profile, uses the catalog input limit up to 1M tokens, or 200k when the limit is unknown. A known limit below Claude’s 100k minimum is left to Claude. Explicit window settings take precedence. |
| UG Auto metadata and saved selection | Merge | Merge | Explicit configure outside eligible Auto and revert remove UG-reserved aliases from `modelOverrides`, `availableModels`, and picker rows, plus an Auto custom-model environment triplet. Saved Auto `model` and Anthropic model environment values resolve to permitted real targets or are removed. Cleanup also covers native `settings.json` within the command's scope; unrelated models, aliases and policy remain unchanged. Launches and toggles do not clean these fields |
| `permissions.deny` | Merge | Merge | Add `ug`'s own `WebSearch` rule when it replaces web search; other rules left alone. `UG_CLAUDE_SERVER_SIDE_WEB_SEARCH=1` makes one launch strip the bare rule and hide `web_search` in its inline `--settings`; a managed, drop-in or user `WebSearch` deny makes it keep ug's MCP. The managed file never gets the deny; `ug configure --enable-server-side-web-search` (saved as `claude_server_side_web_search`) makes an explicit configure remove an existing bare one unless the admin's settings request it |
| Tracing | Ignore | Create/replace | The seven `CLAUDE_CODE_*`/`OTEL_*` trace keys and `otelHeadersHelper`; only when the config enables tracing |
| `managedMcpServers` | Ignore | Merge | Add/update the config's MCP server entries; other entries left alone |
| Smart-routing hooks | Merge | Merge | `PreToolUse`, `SessionStart`, `SubagentStart`; only `ug`'s own marked handlers, other hooks left alone |
| Admin `agent_native_settings` | Ignore | Create/replace | Managed file only; copied as-is. Keys `ug` owns are skipped, `permissions.deny` and hooks are merged, and `ug`'s MCP servers are added to an admin `allowedMcpServers`. Removed when the admin drops them, unless edited by hand |
| Smart Router Orchestrator hooks | Merge | Merge | Launch-only `UserPromptSubmit` and compact `SessionStart` handlers for smart-routed sessions with `ENABLE_SMART_ROUTER_ORCHESTRATOR=1`; read the same session controls as routing |
| `parentSettingsBehavior` | Create/replace | Create/replace | Managed file only: `"merge"`, so Claude Desktop layers its own policy for Cowork and Chat under this file instead of dropping it (and stops warning about it). Like the header mod, only `ug configure` adds it and a plain launch never does; an administrator's own value is kept. Revert removes it |
| Header mod | Merge | Merge | Managed file only, Linux, macOS, and Windows: `extraKnownMarketplaces.ucode-managed`, `enabledPlugins["ug-headers@ucode-managed"]`, and `ug-headers@ucode-managed` first in `prependPlugins` (an unset list becomes the mod plus `sec-default@builtin`). The mod's files live in an administrator-only `ucode-plugins` folder beside the managed file and add `UG_HEADERS` to each request. Installed, upgraded, and enabled only by `ug configure` (a terminal, or in place over an existing managed file through `sudo -n`; on Windows, elevated), never by a launch (even with `UG_HEADERS`, which instead warns when no header mod is enabled: neither ug's, current, nor another tool's enabled `ug-headers@` plugin such as Isaac's), so a launch writes no machine-wide files and another writer of the file that drops them isn't fought on every launch. The entries are written only once the files are in place; revert removes both, keeping `sec-default@builtin` when an admin has extended the list |
| Recipe mod | Merge | Merge | Managed file only, Linux, macOS, and Windows: `enabledPlugins["ug-smart-router-recipe@ucode-managed"]` and that mod beside `ug-headers@ucode-managed` in `prependPlugins` (an unset list becomes both mods plus `sec-default@builtin`). The mod's files ship in the header mod's `ucode-plugins` marketplace folder, and it alone writes the smart-router recipe into `CLAUDE_CODE_EXTRA_BODY`: the session mod never touches that variable, which Claude 2.1.331's guard denies to the user tier, so a machine without this mod simply sends no recipe. It re-reads the routing flags on every turn step, so a `/smart-router` toggle reaches the next request regardless of mod order. Installed and enabled by `ug configure` like the header mod, and by a managed run, which installs only this mod (per-launch headers stay out of one) into `prependPlugins` ahead of `sec-default@builtin` while keeping the administrator's own entries; revert removes its entries with the header mod's |

</details>

A managed run is `ug configure --global` on a workspace that publishes an agent configuration.
It doesn't follow the tables above. It runs as root and rebuilds each supported agent's
machine-wide files from scratch. It reads the agent configuration and the agent's ownership table
in `src/ucode/ownership/<agent>.toml`, and the code lives in `published_agent_config.py`. To
support another agent, add its ownership table, its file builder in `published_agent_config.py`,
and its tests.

- When a change makes ug write, strip, or reserve an agent key, update the matching table entry.
  Then run `uv run python -m ucode.ownership --write docs/admin-managed-settings-reference.md`.
  Otherwise `tests/test_ownership.py` fails.
- The run leaves the marker file `/etc/unitygateway/agents-state.json`. While the marker file
  exists (`managed_files.published_agent_config_used()`), every other run skips the machine-wide
  files, as `UG_SKIP_OS_SETTINGS` does, and reads the agent configuration that the managed run
  saved.
- A managed run also installs and enables the recipe mod (never the header mod: a managed run
  doesn't support per-launch headers), so the smart-router recipe reaches the gateway from the
  policy tier on Claude releases that seat the guard.
- Without the marker file, a workspace with no agent configuration makes `--global` merge as the
  tables describe.

Claude Desktop third-party profiles live under `Claude-3p/configLibrary` in
`~/Library/Application Support` on macOS, `%LOCALAPPDATA%` on current Windows releases,
and `~/.config` on Linux. Windows can initialize the directory before first launch when Claude's registered MSIX package and executable are present. Linux requires Desktop to have opened once.
UG reserves UUID `5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6` for its fully owned profile.
Configure replaces the profile, updates its metadata entry, and selects it, retaining
same-workspace connectors until MCP reconciliation succeeds. Changing workspaces
replaces the same profile without retaining its connectors. Revert deletes that file and entry.
The generated profile enables the manual import wizard with `claudeAiImport.enabled: true`.
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
The reserved Desktop profile enables `toolSearchEnabled` and declares Claude-targeted
MCP servers in `managedMcpServers`, using the existing stdio proxy commands. Connector sync
requires a profile for the same workspace, replaces the connector list, and restores the
previous profile if plugin setup fails. It works before Cowork has started.
After a successful plugin update, legacy-plugin removal failures warn and keep the
updated plugin and native connectors; reconfigure retries the cleanup.
Claude setup also installs a generated Cowork plugin containing the current workspace's
managed and user-downloaded home-level skills, connector guidance, and hooks. It contains
no MCP declarations; reconfigure removes legacy plugin MCP files.
Downloaded skill additions and removals resync the plugin; project-local,
user-authored, and other-workspace skills are excluded. The user-scope source lives at
`~/.ucode/claude-cowork-marketplace`. Default configure and `--user` both write only
this user source, the user 3P profile, and native Cowork registration/cache, even when saved
scope is `global`. They never publish or remove root Desktop files. Explicit `--global`
is detected by the command-local `machine_wide_only` switch and skips ordinary Desktop
profile/plugin setup and instead provisions Desktop through macOS MDM. CLI-agent behavior
is unchanged by these Desktop guards.
Ordinary plugin setup requires Desktop's local account/org directory and uses scoped
`claude plugin ... --cowork --json` with `CLAUDE_CONFIG_DIR`. First configure skips if no
Cowork sessions directory exists. An earlier root-source registration switches to the
user source without deleting root files or creating a duplicate plugin. Content-derived
manifest versions and an explicit native update refresh changed skills/MCPs in the user
cache. A pre-existing org-plugins/ug copy blocks duplicate loading.
`~/.ucode/claude-cowork.json` records the account/org in version-1
state. Account changes clean up the prior account before generation; no configure/revert
lock, pending-cleanup queue, or migration bookkeeping is added. UG replaces the whole
generated user marketplace, including manual edits. Its writer retains staged replacement
and rollback. Native refresh failures retain the source/state for retry/revert. A failed source switch
restores the previous marketplace/plugin through native commands when possible, and
reports a failed recovery with configure retry instructions. Ordinary
revert removes user source/registration and attempts macOS MDM cleanup. Explicit MCP/skill changes resync this source;
launches do not. The privileged bundle writer is used only for MDM publication; its
macOS worker uses the existing on-demand sudo path, validates caller ownership of staged
files, and rejects links/special files before copying into a root-owned destination.
Staged replacement restores the previous bundle if publication fails.
The macOS Desktop MDM mode requires preinstalled, root-owned shared UG/Python,
Databricks CLI, and Claude CLI runtimes. It publishes the managed plist with
`managedMcpServers` and `toolSearchEnabled`, shared helpers, and a required organization
plugin at `/Library/Application Support/Claude/org-plugins/ug`. That plugin contains
skills/assets, connector guidance, and warming hooks, with no MCP declarations.
It reuses the normal generator and content-derived version, with matching `version.json`.
Desktop loads the organization plugin itself; no LaunchAgent, timer, per-user marketplace
sync, deployment descriptor, or `desktop-mdm-sync` command is installed. No administrator-local
profile, skills, or credentials are copied. Provisioning reuses one token and existing
bundle/atomic writers; rerun configure after partial publication failure. Existing auth,
MCP proxy and tracing commands are unchanged; users authenticate with their own OAuth profiles.
A foreign policy is recognized by its credential helper and preserved. Generated root files
belong to UG and are replaced. Desktop provisioning failures warn without failing CLI setup.
Ordinary `ug revert` attempts user and machine cleanup; failed MDM cleanup warns,
continues other cleanup, and retains UG state for retry. Revert has no scope flags. Earlier marketplace/LaunchAgent
builds require manual migration; remove prior UG marketplace registrations to avoid duplicate
skills/hooks. Restart Desktop after updates. Native organization-plugin delivery remains a
validation gap. See the deployment guide before migration or rollout.
Windows MDM and shared-runtime installation remain unsupported. See docs/claude-desktop-mdm.md.
Cowork uses short service-first aliases for generated UC MCP names, with collision qualifiers;
custom registration names and full proxy URLs are preserved. Its name and display name
are `ug`, with plugin ID `ug@unity-gateway-cowork`. Reconfigure removes the old
`unity-gateway@unity-gateway-cowork` registration only after installing/updating `ug`;
revert removes both IDs. Use Desktop's exposed native connector names, not old plugin-prefixed names.
Claude Code also receives `ug@unity-gateway-code` in its regular user plugin scope,
shared with the Desktop Code tab. The generated source lives at
`~/.ucode/claude-code-marketplace`, with its config root recorded in
`~/.ucode/claude-code-plugin.json`; native plugin installation owns only that
marketplace's `extraKnownMarketplaces` entry and its `enabledPlugins` entry.
This plugin contains only the generated `unity-gateway-connectors` skill with Code-specific
authentication guidance and native MCP registration names. Existing MCP registrations and
downloaded skill files stay in their native locations; Cowork hooks are not installed in Code.
Code setup is independent of Cowork account discovery and best-effort; explicit `--global`
skips this user-scope plugin too. Model-configured Claude and
explicit Claude MCP clients are eligible. Managed configure refreshes Code after successful
MCP reconciliation even if skill downloads fail. Model-configured Claude also syncs native
Desktop connectors on skill failure, while leaving the existing Cowork skill plugin intact.
MCP updates refresh eligible plugins, and revert removes both while preserving
unrelated plugins.
Code and Cowork skill templates live in `src/ucode/agents/desktop/connector_skills/`;
the generator inserts connector names and bounds each description.
See `docs/claude-desktop.md` for native validation limits and revert behavior.

<details>
<summary>Codex</summary>

Fields live in `~/.codex/ucode.config.toml` and `/etc/codex/managed_config.toml`. On Windows the managed file is `C:\ProgramData\OpenAI\Codex\requirements.toml`, which omits `model` and `mcp_servers` and adds `env_http_headers` for the launch-scoped headers.

| Field | Without managed config | With managed config | Notes |
|-------|------------------------|---------------------|-------|
| `model_provider` | Create/replace | Create/replace | Gateway provider (`Databricks`) |
| `model` | Ignore | Create/replace | The config's default model |
| `model_providers.Databricks` | Merge | Merge | Provider block: name, gateway base URL, wire API, `ug` auth command; other keys left alone. A relayed (ChatGPT subscription) provider sets `requires_openai_auth` instead of the auth command and skips the managed file |
| `http_headers` | Merge | Merge | In `[model_providers.Databricks]`; merge `ug`'s routing headers by name, admin headers added under managed config. `UG_HEADERS` names not already present are added to the launch's `-c` flags only, never a file. On a host that serves several workspaces, the workspace selector headers are added last |
| `model_provider`, `model_providers.DatabricksRelay` (app) | Ignore | Ignore | In `~/.codex/config.toml`, only for a relayed (ChatGPT subscription) provider, for Codex desktop, and only by `ug configure` (like Claude Desktop): a loopback relay block with the MPS header served by the login service. Its own name keeps a managed `Databricks` provider from merging into it, and marks it as ug's to replace or remove; a user-defined `model_provider` is left alone |
| `model_catalog_json` | Create/replace | Create/replace | In `~/.codex/config.toml`; `ug`'s own catalog reference, for a static model list. On Linux and macOS, without a provider or UC location, the managed file also names `ug`'s root-owned `/etc/codex/ug-model-catalog.json` |
| `otel.trace_exporter` | Ignore | Create/replace | In `~/.codex/config.toml` when the config enables tracing, for Codex desktop; another exporter and other telemetry settings left alone |
| `mcp_servers` | Ignore | Merge | Managed file; add/update the config's MCP server entries, other entries left alone. A same-name entry in `~/.codex/config.toml` of the other transport (stdio vs HTTP) is removed, since Codex would merge the two into an invalid entry |
| Smart Router and Smart Router Orchestrator hooks | Merge | Merge | Launch-only `PreToolUse`, plus `UserPromptSubmit` and compact `SessionStart` with `ENABLE_SMART_ROUTER_ORCHESTRATOR=1`; Codex combines them with its native hook sources and applies project trust; `features.hooks` is enabled for a smart-routed launch |
| Native v2 collaboration | Launch-only (-c), never written | Launch-only (-c), never written | `features.multi_agent_v2.non_code_mode_only=false` and matching `root_agent_usage_hint_text` / `subagent_usage_hint_text` override saved values for the session; only for smart routing on Codex 0.171.0 or newer. User config files, general developer instructions, and native delegation-mode controls are preserved |
| Smart Router footer | Launch-only (-c), never written | Launch-only (-c), never written | On routed Codex 0.171.0 or newer, enable `features.tui_extensions_statusline`, select `tui.status_line_provider`, and add only `mcp_servers.ug-smart-router-statusline`. The app-server owns the session-local stdio provider; the terminal client receives the feature and selection overrides. Routed launches require stable 0.171.0 or newer through the existing upgrade gate. Saved footer settings and unrelated MCP servers are preserved. The footer relies on that launch gate; do not add a second version probe |
| Smart Router native metadata | Launch-only (-c), never written | Launch-only (-c), never written | `responses_api_metadata.smart_router_recipe_name` records the configured recipe. The app-server interposer updates `smart_router_state` per main turn; spawn hooks stamp child task names. Reserve the state key for the interposer; a static config value would override turn updates |

</details>

The ChatGPT desktop app (Codex desktop) reads these files too. The required **Codex desktop
inference** integration jobs on macOS and Windows open a pinned build after managed `ug configure`
and require a completed system.ai turn; see `tests/integration/README.md` to bump a pin.

<details>
<summary>GitHub Copilot in VS Code</summary>

Fields live in the VS Code user `settings.json`. `ug`'s extension writes them when it starts, from the `ug-config.json` that `ug configure` packed into it, and records what it wrote: once a configure no longer asks for a field, the extension removes the value it wrote, and a value the developer changed since stays. `ug revert` removes them before it uninstalls the extension, from the user `settings.json` and each profile's, except a value the developer changed; for an OTel agent, a changed `otlpEndpoint` keeps its whole group. A `settings.json` with comments is left alone, and revert names the fields to remove.

| Field | Without managed config | With managed config | Notes |
|-------|------------------------|---------------------|-------|
| `github.copilot.chat.otel.enabled`, `.exporterType`, `.protocol`, `.otlpEndpoint` | Ignore | Create/replace | When the config enables tracing for Copilot: the local agent exports protobuf to the extension's loopback receiver, which forwards traces to the gateway |
| `chat.agentHost.otel.enabled`, `.exporterType`, `.otlpEndpoint` | Ignore | Create/replace | When the config enables tracing for Copilot: the Copilot CLI agent exports to the same receiver |
| `chat.defaultModel` | Ignore | Create/replace unless the developer set their own | The config's `default_model` for Copilot, when VS Code's models include it: the model new chat conversations start on |

</details>
