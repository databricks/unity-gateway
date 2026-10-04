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
| `env.ANTHROPIC_MODEL` | Ignore | Create/replace | Launch model; written at launch, not by `ug configure`. Set from the budget recommendation when spend tiers are set. A plain `default_model` (no recommendation) is no longer force-pinned for a plain gateway or Unity Catalog source (it resolves through `ANTHROPIC_DEFAULT_MODEL`), so it adds no duplicate `/model` row. Still force-pinned for a static `model_services` list (which sets `enforceAvailableModels`, under which Claude Code ignores `ANTHROPIC_DEFAULT_MODEL`) and for a Model Provider Service (foreign-namespace id) |
| `env.ANTHROPIC_DEFAULT_MODEL` | Ignore | Create/replace | The `/model` "Default" row (session start model), from the config's `default_model`; written by `ug configure` for a plain gateway or Unity Catalog source (both route gateway ids) so a launch starts on it without a forced `ANTHROPIC_MODEL`, and the user can still switch via `/model`. Not written for a static `model_services` list (ignored under `enforceAvailableModels`) or a Model Provider Service (provider-namespace ids) |
| `env.ANTHROPIC_DEFAULT_*_MODEL` | Create/replace | Create/replace | Per-family default (opus, sonnet, haiku, fable), persisted at `ug configure` from the config's authored family slots for gateway, static-list, and Unity Catalog sources (a Model Provider Service pins from its own targets). Only families the config authors are written; unauthored families keep their discovered/preserved values (`default_model` sets the Default row, not the family aliases) |
| Model picker | Ignore | Create/replace | `availableModels`, `enforceAvailableModels`, `modelPicker`; only for a managed static model list |
| `permissions.deny` | Merge | Merge | Add `ug`'s own `WebSearch` rule when it replaces web search; other rules left alone |
| Tracing | Ignore | Create/replace | The seven `CLAUDE_CODE_*`/`OTEL_*` trace keys and `otelHeadersHelper`; only when the config enables tracing |
| `managedMcpServers` | Ignore | Merge | Add/update the config's MCP server entries; other entries left alone |
| Smart-routing hooks | Merge | Merge | `PreToolUse`, `SessionStart`, `SubagentStart`; only `ug`'s own marked handlers, other hooks left alone |

The three Claude model env vars resolve differently per scenario. Each table below lists the value in
the generated settings file(s); "(launch process env)" marks a value set into the launch process
environment rather than written to a file. Unless noted, the private `~/.claude/ucode-settings.json`
and the OS-managed file match. Managed tables assume the config authors an overall `default_model`
and family slots; `*_MODEL` means the four `ANTHROPIC_DEFAULT_<FAMILY>_MODEL` keys.

Managed config, by model source:

<details>
<summary>model_services (static allow-list)</summary>

| env var | `ug configure` | `ug claude` (no rec) | `ug claude` (+ recommendation) |
|---------|----------------|----------------------|--------------------------------|
| `ANTHROPIC_MODEL` | not written | `default_model` (forced) | recommended model (forced) |
| `ANTHROPIC_DEFAULT_MODEL` | not written | not written | not written |
| `ANTHROPIC_DEFAULT_*_MODEL` | authored slots (opus/sonnet `[1m]`) | authored slots | authored slots |

A static list sets `enforceAvailableModels`, under which Claude Code ignores `ANTHROPIC_DEFAULT_MODEL`,
so the admin default stays a forced `ANTHROPIC_MODEL` pin. The OS-managed file drops a family slot
whose model is outside the list; the private file keeps it. At launch the private file carries only
`ANTHROPIC_MODEL` (the OS-managed file also keeps the in-list family keys).
</details>

<details>
<summary>unity_catalog_location (parent-schema discovery)</summary>

| env var | `ug configure` | `ug claude` (no rec) | `ug claude` (+ recommendation) |
|---------|----------------|----------------------|--------------------------------|
| `ANTHROPIC_MODEL` | not written | not written | recommended model (forced) |
| `ANTHROPIC_DEFAULT_MODEL` | `default_model` | `default_model` | `default_model` |
| `ANTHROPIC_DEFAULT_*_MODEL` | authored slots (opus/sonnet `[1m]`) | authored slots | authored slots |

UC ids are gateway-routable, so the admin default is the `/model` Default row via
`ANTHROPIC_DEFAULT_MODEL` (not force-pinned); a recommendation force-pins `ANTHROPIC_MODEL` on top.
</details>

<details>
<summary>model_provider_service (MPS)</summary>

| env var | `ug configure` | `ug claude` (no rec) | `ug claude` (+ recommendation) |
|---------|----------------|----------------------|--------------------------------|
| `ANTHROPIC_MODEL` | not written | `default_model`, resolved to a provider id (forced) | recommended, resolved (forced) |
| `ANTHROPIC_DEFAULT_MODEL` | not written | not written | not written |
| `ANTHROPIC_DEFAULT_*_MODEL` | provider targets (opus/sonnet/haiku; no fable) | authored slots or targets (verbatim, no `[1m]`) | same |

Provider-namespace ids (Anthropic canonical names, Bedrock slugs), so no routable Default row and the
pin stays forced. Family keys at configure come from the service's live targets; at launch from the
authored slots. An Anthropic `allow_all` service (no declared targets) pins nothing.
</details>

No managed config (`ug claude` with the developer's own setup):

<details>
<summary>plain gateway discovery (no flags)</summary>

| env var | `ug configure` | `ug claude` |
|---------|----------------|-------------|
| `ANTHROPIC_MODEL` | not written | not written |
| `ANTHROPIC_DEFAULT_MODEL` | not written | not written |
| `ANTHROPIC_DEFAULT_*_MODEL` | discovered ids (opus/sonnet `[1m]`) | discovered ids |

A developer's own pre-existing family default or `ANTHROPIC_DEFAULT_MODEL` in either file is preserved
over discovery. Launch also sets `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` in the process env.
</details>

<details>
<summary>--model &lt;id&gt; (per-launch model)</summary>

| env var | `ug configure` (no flag) | `ug claude --model <id>` |
|---------|--------------------------|--------------------------|
| `ANTHROPIC_MODEL` | not written | `<id>` (launch process env + launch-scoped `--settings` and `--model` argv); not persisted |
| `ANTHROPIC_DEFAULT_MODEL` | not written | not written (a stale value from a prior managed launch is pruned) |
| `ANTHROPIC_DEFAULT_*_MODEL` | discovered ids | discovered ids |

`--model` is launch-scoped only; `ug configure` has no `--model` flag (its columns match plain gateway).
</details>

<details>
<summary>--provider &lt;mps&gt; (developer Model Provider Service)</summary>

| env var | `ug configure` (persisted provider) | `ug claude --provider <mps>` |
|---------|-------------------------------------|------------------------------|
| `ANTHROPIC_MODEL` | not written | resolved provider id (forced: `--model`, else the service's tier pick); none for `allow_all` with no `--model` |
| `ANTHROPIC_DEFAULT_MODEL` | not written | the discovered picker's default (launch process env); not written to files |
| `ANTHROPIC_DEFAULT_*_MODEL` | provider target ids (opus/sonnet/haiku; no fable); none for Anthropic `allow_all` | same (preserved from configure) |

A developer's hand-set `ANTHROPIC_DEFAULT_MODEL` in `ucode-settings.json` is **not** preserved under a
provider launch (a stale gateway id would be unroutable there). Relayed Claude-subscription sub-case:
only the private file is written (never the OS-managed file), no `apiKeyHelper`, no `ANTHROPIC_MODEL`
or `ANTHROPIC_DEFAULT_MODEL`; the model is forwarded as a `--model` argv.
</details>

<details>
<summary>--model-location &lt;catalog.schema&gt; (developer parent-schema)</summary>

| env var | `ug configure` (no flag) | `ug claude --model-location <catalog.schema>` |
|---------|--------------------------|-----------------------------------------------|
| `ANTHROPIC_MODEL` | not written | set in the launch process env only when `--model` is also passed; not persisted |
| `ANTHROPIC_DEFAULT_MODEL` | not written | the discovered picker's default (launch process env); not written to files |
| `ANTHROPIC_DEFAULT_*_MODEL` | discovered ids | not written (parent-schema suppresses the family branch; no managed default) |

A `modelPicker` is written to both files from the fetched catalog; `--model-location` is a launch-only
flag (its `ug configure` columns match plain gateway). A developer's pre-existing
`ANTHROPIC_DEFAULT_MODEL` in the private file is preserved.
</details>

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

</details>
