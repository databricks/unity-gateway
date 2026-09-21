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

`test_entry_points.py` also runs both installed console scripts (`ug` and `ucode`)
and checks their version output against the `unity-gateway` distribution metadata.
`TestUpgrade` in `test_cli.py` covers both command names before, during, and after
the distribution rename with mocked installer calls, including failure recovery guidance.
Agent configuration tests also verify `ug` auth/MCP helper commands, including
quoted executable paths and replacement of legacy `ucode` routing/web-search helpers.

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
| `test_ug_configure_claude_databricks` | Configure Databricks Hosted; execute the generated auth helper; launch plain `ug claude`, read a file, and open `/model` | Generated helper invokes `ug` with clean token stdout; assistant returns an unpredictable file value; native discovery caches `system.ai` models and the picker shows a discovered model without an opt-in flag; normal exit; reopen with working keyboard input |
| `test_ug_configure_claude_anthropic_mps` | Select Anthropic MPS in the real configure picker; launch Claude | Saved provider in status; completed TUI file task; normal exit |
| `test_ug_configure_codex_databricks` | Configure Databricks Hosted; execute the generated auth helper; open Codex TUI and read a file | Generated helper invokes `ug` with clean token stdout; completed assistant answer contains the file value; normal exit and reopen |
| `test_ug_configure_codex_openai_mps` | Select OpenAI MPS in the real configure picker; launch Codex | Saved provider in status; completed TUI file task; normal exit |
| `test_ug_claude_custom_oauth_cli_boots`, `test_ug_codex_custom_oauth_cli_boots` | Launch with `ENABLE_CUSTOM_OAUTH_FROM_CLI=1`, `--workspace`, and `--client-id databricks-cli` | Real TUI reaches a usable prompt, accepts keyboard input, exits normally, and saves `client_id = databricks-cli` in its generated CLI profile; Claude also reads the OS-managed settings and requires a profile-only `apiKeyHelper` |
| `test_case_13_configured_claude_discovers_system_models`, `test_case_15_fresh_claude_discovers_system_models` | Launch configured/fresh Claude with no discovery flag or source override | Claude caches `system.ai` models, including ug's discovered family defaults, and shows a discovered picker entry |
| `test_case_14_configured_codex_uses_default_models`, `test_case_16_fresh_codex_uses_default_models` | Launch configured/fresh Codex with no source override | Configured default belongs to discovered `system.ai` models; app-server exposes native GPT entries without a generated scoped catalog |
| `test_case_17_*` | Launch configured and fresh Claude with a provider | The cache contains exactly the provider model; the picker shows its row, including native Haiku 4.5 deduplication |
| `test_case_18_*` | Launch configured and fresh Codex with a provider | The provider supplies exactly its model catalog |
| `test_case_19_*` | Launch configured and fresh Claude with a model location | The explicit parent supplies exactly its picker catalog |
| `test_case_20_*` | Launch configured and fresh Codex with a model location | The explicit parent supplies exactly the Claude and Codex Model Services, independent of order |
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
| `test_smart_routing_claude_route_subagent_hook`, `test_smart_routing_codex_route_subagent_hook` | Pipe a real PreToolUse spawn payload to the installed route-subagent hook with subagent-only routing enabled | Allow decision against the live router; requested model replaced by a routed agent definition (Claude) or bundled catalog slug (Codex) from the offered models; one audited decision matching the session and task |
| `test_smart_routing_claude_subagent_only_launch_shows_no_first_prompt_banner`, `test_smart_routing_codex_subagent_only_launch_shows_no_first_prompt_banner` | Configure, then launch the real TUI with both the full and subagent-only routing flags set and submit one file prompt | Subagent-only takes precedence: the prompt completes with no smart-routing banner and no first-prompt routing wrapper (PTY/interposer); Claude's SessionStart canary proves the routing hooks armed; normal exit |
| `test_ug_configure_claude_repeat_and_revert`, `test_ug_configure_codex_repeat_and_revert` | Configure twice over user settings; complete a task; revert twice | Settings preserved; no bearer in ug state; generated config removed; status unconfigured |
| `test_ug_configure_claude_cleans_stale_skills_mcp_on_workspace_switch` | Configure the first workspace, register its skills MCP, switch to a second real workspace, and use Claude | Old registration removed from Claude and the new workspace state; old workspace bucket preserved; repeat configure stays clean; real file task completes on the second workspace |
| `test_ug_configure_claude_rejects_invalid_credentials`, `test_ug_configure_codex_rejects_invalid_credentials` | Configure with a rejected bearer against the real workspace | Authentication failure; no successful saved setup |
| `test_ug_configure_managed_claude`, `test_ug_configure_managed_codex` | Configure against a workspace that publishes a managed CodingAgentConfig | No agent selector; each agent's generated config exposes exactly the admin's static model_services; real gateway prompt on launch |
| `test_case_01_*` | Launch managed Claude after configure and from fresh state | Claude receives the admin MPS header, caches native discovery results, and opens its real model picker |
| `test_case_05_*`, `test_case_07_*` | Pass a provider or model-location override to managed Claude after configure and from fresh state | ug rejects the override before Claude starts and preserves agent-owned state |
| `test_case_02_*` | Launch managed Codex after configure and from fresh state | Codex exposes exactly the admin MPS-scoped catalog |
| `test_case_06_*`, `test_case_08_*` | Pass a provider or model-location override to managed Codex after configure and from fresh state | ug rejects the override before Codex starts and preserves agent-owned state |
| `test_ug_configure_managed_codex_catalog_fallback` | Configure from an injected managed response containing a GPT model absent from Codex's bundled catalog | Actionable metadata warning; conservative catalog entry for the unknown model; real Codex prompt on the valid default model |
| `test_managed_fixture_claude_mps_defaults_accompany_discovery`, `test_managed_fixture_claude_parent_schema_defaults_accompany_discovery` | Launch Claude from injected managed defaults with MPS and Unity Catalog discovery | Both generated settings files retain every admin-authored default alongside the source header; only UC Opus/Sonnet family ids gain `[1m]` |
| `test_managed_fixture_claude_model_lifecycle`, `test_managed_fixture_codex_model_lifecycle` | Configure across no config -> static A -> static B -> MPS -> no config (stub-injected, `null` for no-config; MPS via a real provider service) | Each agent's model files reconcile to each static config (removed models pruned); switching to an MPS and a workspace with no managed config both clear ug's managed model settings so no stale list is enforced |
| `test_ug_installed_wheel_exposes_help_and_version` | Invoke freshly installed console command | Package version matches; public help works |
| `test_ug_status_in_fresh_home_is_unconfigured` | Request status before configure | Unconfigured status |
| `test_ug_auth_without_configuration_explains_how_to_configure` | Request auth before configure | Actionable setup error and nonzero exit |
| `test_ug_and_ucode_auth_helpers_emit_only_the_supplied_bearer` | Run both auth helper commands with the public bearer override, with and without forced refresh | Exact token-only stdout, no warnings or ANSI escapes; no workspace authentication or saved state |
| `test_ug_and_ucode_web_search_helpers_preserve_mcp_stdio` | Initialize and list tools through both web-search helper commands | Exactly the MCP JSON-RPC responses; no text/ANSI contamination; existing server/tool identities preserved; no model request |

With both agents selected there are **58 live cases** (12 interactive TUI cases),
**4 managed-workspace cases** (marker `managed`, run against a separate workspace that
publishes a CodingAgentConfig), **1 two-workspace case** (marker `workspace_switch`),
**26 managed-fixture cases** (marker `managed_fixture`, with only
the CodingAgentConfig input injected), and **7 installation checks**. The 14 retained numbered scenarios
comprise **24 explicit journeys**: 12 managed configured/fresh executions and 12 unmanaged
executions. Fourteen additional managed-fixture cases cover focused model, MCP, skills,
and lifecycle shapes. Parametrization varies
argument spelling or routing mode, never hides the agent/provider in the test name. Duplicate boot-only cases
are incorporated into the Databricks configuration TUI journeys.
Generated-file cleanup and strict app-server stdout assertions remain enforced.
Unmanaged discovery Cases 13–20 configure, list models, or open the picker without
submitting inference prompts; separate task journeys still perform inference.
They require a real workspace with no CodingAgentConfig; a read-only prerequisite
check reports any published config rather than bypassing it. `UG_ENABLE_MODEL_DISCOVERY`
is not supported on current main. Its duplicate managed Cases 3, 4, and 9–12 and
unmanaged disable Cases 21–24 are removed; retained Cases 1, 2, and 5–8 cover managed
discovery and override rejection. Cases 13–16 now cover automatic/default launches.
Configure-time model locations are also unsupported; Cases 19–20 cover the supported
launch-time `--model-location`. Historical case numbers are retained with these gaps.
Managed Codex state comparisons exclude `.codex/tmp/arg0`, the disposable executable
links recreated by Codex version checks; persistent agent files remain compared.

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
Databricks Hosted configure/TUI, custom OAuth CLI TUI, and headless argument
journeys for both agents, in two parallel jobs. After smoke finishes, the full
suite runs all 58 live cases across two parallel agent jobs: one Claude VM and one
Codex VM, each running its configure, headless, and commands/lifecycle cases
serially. Each agent is installed once for the full suite, and no two full jobs
for the same agent overlap within a run.
CI starts integration alongside unit tests and the existing e2e shards. Integration
does not wait for agent e2e or get skipped when an agent shard fails. These suites
share workspace capacity; overlapping their requests can still encounter rate limits.
The `All integration tests` check requires every selected integration job to pass; full coverage
does not depend on a label or a manual request.

The existing e2e workflow runs seven parallel shards: gateway checks plus one for
each of Claude, Codex, Gemini, OpenCode, Copilot, and Pi. Each agent shard installs
its own CLI. Configure-subset checks run in the Claude shard because configuration
invokes the Claude CLI. The `All agent tests` check requires every shard to pass.
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
| Live MCP and skills functionality | Deferred; installation tests cover the local web-search MCP handshake and tool listing, not upstream proxying or a real search request |
| Broad configure flags, tracing, multiple workspaces, and PAT flows | Deferred while focusing on basic CUJs |
| Workspace-switch MCP cleanup | The `workspace_switch` CUJ covers real registration, cleanup, repeat configure, and a completed Claude task. Unit/component tests cover duplicate attempts and injected removal failures; the CUJ does not force an agent timeout. It runs in the existing non-blocking managed CI lane. |
| Relayed/subscription MPS discovery | Not covered by the scoped discovery journeys |
| Fresh provider/parent validation and mixed Bedrock filtering | Not covered after removing the duplicate model-discovery suites |
| TUI initial prompt supplied on the launch command line | Not yet covered; headless prompt arguments are covered |
| Follow-up turns and conversation resume | Not covered; reopen proves startup, not conversation resume |
| Claude/Codex interactive smart routing | First-prompt routing covered by the `managed_fixture` smart-routing banner journeys; subagent routing covered at the hook protocol level by the route-subagent hook journeys, which drive the real installed hook commands with a harness-shaped payload against the live router; the subagent-only launch journeys assert the first-prompt banner and routing wrappers stay silent while the routing hooks arm. The agent's interactive spawn decision, interactive explicit-model bypass, and dedicated routing CI shards remain deferred. Unit/component routing tests do not establish live routing behavior. |
| Full allow/deny tool-permission matrix | Not covered; onboarding/trust uses actual TUI choices |
| Desktop Codex app, Isaac itself, auto-upgrades | Not covered by command forwarding or pinned-version tests |
| Native macOS/Windows managed settings, resize/signals | Separate platform coverage needed |
| Other agents | Current scope is Claude Code and Codex |

See [integration/README.md](integration/README.md) for commands, CI, artifacts,
and reproduction. Follow [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md) when
adding, modifying, or removing tests. The ordinary suite enforces both the
no-mocking boundary and the Scenario/Expected docstring format.
