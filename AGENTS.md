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
- Use `launcher.exec_or_spawn` when handing the terminal to an agent. Shell strings and explicit executable overrides keep their existing subprocess semantics.
- Add or update focused tests for behavior changes.
- Do not modify generated or lock files unless the dependency graph intentionally changes.

## Style

- Keep user-facing CLI errors actionable.
- Use warnings for recoverable setup problems and errors for launch/runtime blockers.
- Preserve existing Rich UI conventions, including `print_warning`, `print_err`, `print_success`, `print_section`, and `spinner`.
- Avoid broad refactors while fixing a narrow bug.

## Fields `ug` manages

This is the proposed allow list: the fields `ug` sets or replaces, and nothing
else. Every field not listed is ignored. For Claude Code the fields live in
`~/.claude/ucode-settings.json` and the OS managed-settings file, which takes
precedence. For Codex they live in `~/.codex/ucode.config.toml` and
`/etc/codex/managed_config.toml`. "Managed config" is the coding agent config
admins publish through the API. Each note says what the field is, plus any part
`ug` leaves alone or the condition under which `ug` writes it.

This describes the intended behavior. Where today's code differs, mainly by
clearing fields it should leave alone, the gap is tracked in Jira, not in this
table.

<details>
<summary>Claude Code</summary>

| Field | Without managed config | With managed config | Notes |
|-------|------------------------|---------------------|-------|
| `apiKeyHelper` | Sets/replaces | Sets/replaces | Gateway auth-token helper |
| `env.ANTHROPIC_BASE_URL` | Sets/replaces | Sets/replaces | Gateway endpoint URL |
| `env.ANTHROPIC_CUSTOM_HEADERS` | Sets/replaces | Sets/replaces | `ug`'s routing and attribution headers; other header lines left alone. Admin headers added under managed config |
| `env.CLAUDE_CODE_API_KEY_HELPER_TTL_MS` | Sets/replaces | Sets/replaces | Auth-helper cache TTL |
| `env.ENABLE_PROMPT_CACHING_1H` | Sets/replaces | Sets/replaces | Prompt-caching flag |
| `env.ENABLE_TOOL_SEARCH` | Sets/replaces | Sets/replaces | Tool-search flag |
| `env.CLAUDE_CODE_USE_GATEWAY` | Sets/replaces | Sets/replaces | Gateway-routing flag |
| `env.ANTHROPIC_MODEL` | Ignores | Sets/replaces | Launch model, from the config's `default_model` or budget recommendation; set at launch, not by `ug configure` |
| `env.ANTHROPIC_DEFAULT_*_MODEL` | Sets/replaces | Sets/replaces | Per-family default (opus, sonnet, haiku, fable); the config's family default wins under managed config |
| Model picker | Ignores | Sets/replaces | `availableModels`, `enforceAvailableModels`, `modelPicker`; only for a managed static model list |
| `permissions.deny` | Sets/replaces | Sets/replaces | `ug`'s own `WebSearch` rule, added when it replaces web search; other rules left alone |
| Tracing | Ignores | Sets/replaces | The seven `CLAUDE_CODE_*`/`OTEL_*` trace keys and `otelHeadersHelper`; only when the config enables tracing |
| `managedMcpServers` | Ignores | Sets/replaces | The config's MCP servers |
| Smart-routing hooks | Sets/replaces | Sets/replaces | `PreToolUse`, `SessionStart`, `SubagentStart`; `ug`'s own marked handlers, other hooks left alone |

</details>

<details>
<summary>Codex</summary>

| Field | Without managed config | With managed config | Notes |
|-------|------------------------|---------------------|-------|
| `model_provider` | Sets/replaces | Sets/replaces | Gateway provider (`Databricks`) |
| `model` | Ignores | Sets/replaces | The config's default model |
| `model_providers.Databricks` | Sets/replaces | Sets/replaces | Provider block: name, gateway base URL, wire API, `ug` auth command; other keys left alone |
| `http_headers` | Sets/replaces | Sets/replaces | In `[model_providers.Databricks]`; `ug`'s routing headers, admin headers added under managed config |
| `model_catalog_json` | Sets/replaces | Sets/replaces | In `~/.codex/config.toml`; `ug`'s own catalog reference, for a static model list |
| `mcp_servers` | Ignores | Sets/replaces | Managed file; the config's MCP servers |

</details>

`ug` also clears some fields today that this list leaves alone: the
`env.ANTHROPIC_DEFAULT_*_MODEL_NAME` companions,
`env.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`,
`env.CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`, and Codex `model_reasoning_effort`.
Those removals are gaps against this allow list and are tracked in Jira. Codex
`[otel]` is never written to a file.
