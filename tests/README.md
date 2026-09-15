# Test suites and user journeys

Integration runs a freshly installed ug wheel/release, exact real Claude/Codex
versions, and the existing real e2e workspace. It has no application imports,
mocks, monkeypatching, fake binaries/services, or fabricated ug state.

| Category | Location | What it proves |
| --- | --- | --- |
| Unit/component | Existing `test_*.py` files | Individual behavior; dependencies may be mocked |
| Existing e2e | `test_e2e*.py` | Real workspace behavior with some patched setup/internal calls |
| Integration CUJs | `integration/test_*.py` | Public configure, TUI, script, command, protocol, and lifecycle journeys |
| Installation | `integration/test_installation.py` | Fresh installed package without credentials |

Agent-picker regression coverage in `test_ui.py` and `test_cli.py` drives actual
keyboard selection: nothing is selected by default, selecting Codex installs only
Codex, and submitting an empty selection installs nothing. Rendering checks cover
the selected and empty checkboxes. These are local component checks, not live gateway tests.

## CUJ coverage matrix

These are **implemented assertions**, not a claim that every version passes.
Consult the run's JUnit report and artifacts for results. Each function states
its **Scenario** and **Expected** outcome and shows its configure and launch
commands. Fixtures supply fresh environments and credentials, never configured ug.
All tests live directly in `integration/`; shared mechanics live in `utils/`.

| Test | User action | Expected evidence |
| --- | --- | --- |
| `test_ug_configure_claude_databricks` | Configure Databricks Hosted; open Claude TUI and read a file | Assistant returns an unpredictable file value; normal exit; reopen with working keyboard input |
| `test_ug_configure_claude_anthropic_mps` | Select Anthropic MPS in the real configure picker; launch Claude | Saved provider in status; completed TUI file task; normal exit |
| `test_ug_configure_codex_databricks` | Configure Databricks Hosted; open Codex TUI and read a file | Completed assistant answer contains the file value; normal exit and reopen |
| `test_ug_configure_codex_openai_mps` | Select OpenAI MPS in the real configure picker; launch Codex | Saved provider in status; completed TUI file task; normal exit |
| `test_ug_claude_*model_discovery*` | Launch Claude with Anthropic/Bedrock MPS and parent scopes, before and after configure | Exact IDs in Claude's real gateway cache; scope switches replace stale catalogs; invalid scopes fail before launch |
| `test_ug_codex_*model_discovery*` | Launch Codex app-server with OpenAI/Bedrock MPS and parent scopes, before and after configure | Exact IDs from Codex's real `model/list`; scope switches replace stale catalogs; invalid scopes fail before launch |
| `test_bugbash_case_01_*` through `test_bugbash_case_24_*` | Run every command contract from the bugbash Tests tab | Managed config wins or rejects overrides; saved/default locations and discovery-disable behavior match the table |
| `test_ug_claude_headless_prompt_argument`, `test_ug_claude_headless_prompt_stdin`, `test_ug_claude_headless_prompt_after_separator` | Run Claude from a script using each prompt form | Structured final answer contains the file value; exit zero; no routing |
| `test_ug_codex_headless_prompt_argument`, `test_ug_codex_headless_prompt_stdin`, `test_ug_codex_headless_prompt_after_separator` | Run Codex from a script using each prompt form | Completed turn and final answer contain the file value; exit zero; no routing |
| `test_ug_claude_headless_explicit_model_bypasses_routing` | Pass `--model VALUE` / `--model=VALUE` with routing enabled | Real file task completes; no routing wrapper |
| `test_ug_codex_headless_explicit_model_bypasses_routing` | Pass `--model VALUE` / `--model=VALUE` / `-m VALUE` with routing enabled | Real file task completes; no routing wrapper |
| `test_ug_claude_preserves_caller_settings_and_hook` | Pass a settings path containing spaces | Real SessionStart hook executes; caller file unchanged; file task completes |
| `test_ug_claude_reports_unsupported_short_model_option` | Pass Claude's unsupported `-m` | Actual agent error and exit status preserved |
| `test_ug_claude_auth_help`, `test_ug_claude_mcp_help` | Request subcommand help, routing off/on | Real agent help; no routing wrapper |
| `test_ug_codex_app_help`, `test_ug_codex_app_server_help`, `test_ug_codex_exec_help`, `test_ug_codex_mcp_help` | Request subcommand help, routing off/on | Real agent help; no routing wrapper |
| `test_ug_codex_app_reports_unknown_argument` | Pass an invalid option directly to `ug codex app`, routing off/on | Real Codex parser error and status preserved |
| `test_ug_codex_app_server_client_initializes` | Connect a stdio client, direct/`--` separator, routing off/on | Actual JSON-RPC initialize response; no non-JSON stdout; no routing |
| `test_ug_configure_claude_repeat_and_revert`, `test_ug_configure_codex_repeat_and_revert` | Configure twice over user settings; complete a task; revert twice | Settings preserved; no bearer in ug state; generated config removed; status unconfigured |
| `test_ug_configure_claude_rejects_invalid_credentials`, `test_ug_configure_codex_rejects_invalid_credentials` | Configure with a rejected bearer against the real workspace | Authentication failure; no successful saved setup |
| `test_ug_installed_wheel_exposes_help_and_version` | Invoke freshly installed console command | Package version matches; public help works |
| `test_ug_status_in_fresh_home_is_unconfigured` | Request status before configure | Unconfigured status |
| `test_ug_auth_without_configuration_explains_how_to_configure` | Request auth before configure | Actionable setup error and nonzero exit |

With both agents selected there are **79 live cases** (16 interactive TUI cases)
and **3 installation checks**. Parametrization varies argument spelling or routing
mode, never hides the agent/provider in the test name. Duplicate boot-only cases
are incorporated into the Databricks configuration TUI journeys.
Generated-file cleanup and strict app-server stdout assertions remain enforced.

ug no longer runs a post-configure agent probe; the deprecated `--skip-validate`
flag is accepted as a no-op where older journeys still pass it. Tests retain
`--skip-upgrade` as a deprecated no-op too; UG only upgrades agents below its
required minimum. Tests disable optional Databricks AI Tools. Help forwarding
does not claim MCP functionality.

Unit/component tests cover automatic Fable discovery and legacy-state cleanup,
available-subset configuration (including a nonzero exit when none are available),
deprecated skip flags, and required-only agent upgrades. They replace the obsolete
Fable opt-in, strict-subset, and optional-update assertions; these options do not
have dedicated live integration coverage.

Fresh consumer dependency resolution covers the install path behind #496, rather
than consuming `uv.lock`. Use `--dependency PACKAGE==VERSION` or replay the archived
dependency graph to reproduce a user's combination. Every relevant same-repository
PR and push to `main` runs both smoke and the full CUJ suite. Smoke covers the
Databricks Hosted configure/TUI and headless argument journeys for both agents,
in two parallel jobs. After smoke finishes, the full suite runs all 79 cases
across two parallel agent jobs: one Claude VM and one Codex VM, each running its
configure, headless, and commands/lifecycle cases serially. Each agent is installed
once for the full suite, and no two full jobs for the same agent overlap within a run.
CI starts integration alongside unit tests and the existing e2e shards. Integration
does not wait for agent e2e or get skipped when an agent shard fails. These suites
share workspace capacity; overlapping their requests can still encounter rate limits.
The `All integration tests` check requires every selected integration job to pass; full coverage
does not depend on a label or a manual request.

The existing e2e workflow runs seven parallel shards: gateway checks plus one for
each of Claude, Codex, Gemini, OpenCode, Copilot, and Pi. Each agent shard installs
its own CLI. Configure-subset checks run in the Claude shard because configuration
invokes the Claude CLI. The Claude shard also runs the existing tracing test file, whose
pre-existing skip remains in place. The `All agent tests` check requires every shard to pass.
Check names describe the coverage: `Unit tests`, `Gateway API tests`,
`Agent launch tests · Claude`, `Smoke journeys · Claude`, and
`Full journeys · Claude` (with the other agents named likewise).
Unit tests still run as one job. Both matrices use `fail-fast: false` so one
failure does not cancel other coverage.

The small `test` and `e2e` compatibility gates retain the exact status contexts
required by the repository's branch rules. `test` requires `Unit tests`; `e2e`
requires both `All agent tests` and the complete integration workflow. A failed
or skipped dependency fails the gate, and a running integration suite keeps it
pending. The descriptive jobs provide the actual coverage and diagnostics.

## Gaps and deferred scope

| Scenario | Status / requirement |
| --- | --- |
| MCP and skills functionality | Deferred at the user's request; existing `mcp --help` dispatch checks only |
| Broad configure flags, tracing, multiple workspaces, OAuth/PAT flows | Deferred while focusing on basic CUJs |
| Relayed/subscription MPS discovery | Not covered by the scoped discovery journeys |
| Workspace-managed discovery/override matrix | Red CUJs are present; they need a separate workspace with Coding Agent Config enabled plus its explicit workspace/token inputs |
| `UG_ENABLE_MODEL_DISCOVERY=0` cases | Red CUJs encode the table's proposed behavior; ug does not implement the variable yet |
| Persisted `--model-location` from `ug configure` | Red CUJs encode the table's proposed behavior; the CLI currently exposes launch-only `--parent` |
| TUI initial prompt supplied on the launch command line | Not yet covered; headless prompt arguments are covered |
| Follow-up turns and conversation resume | Not covered; reopen proves startup, not conversation resume |
| Claude/Codex interactive smart routing | Deferred at the user's request; routing jobs and live journeys removed. Unit/component routing tests remain, but do not establish live routing behavior. |
| Full allow/deny tool-permission matrix | Not covered; onboarding/trust uses actual TUI choices |
| Desktop Codex app, Isaac itself, auto-upgrades | Not covered by command forwarding or pinned-version tests |
| Native macOS/Windows managed settings, resize/signals | Separate platform coverage needed |
| Other agents | Current scope is Claude Code and Codex |

See [integration/README.md](integration/README.md) for commands, CI, artifacts,
and reproduction. Follow [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md) when
adding, modifying, or removing tests. The ordinary suite enforces both the
no-mocking boundary and the Scenario/Expected docstring format.
