# Integration tests

The separate [dedicated-workspace CUJ](../e2e_cuj/AGENTS.md) lives in `tests/e2e_cuj/`
and runs directly with pytest. It does not use this suite's runner or config fixtures.
It reuses the session, terminal, file-task, and transcript helpers in `utils/`.
Its Claude/Codex evidence helpers keep scenario-specific assertions separate from shared mechanics.
Workspace config/catalog reads use its base class's Databricks SDK client. Configuration
is read-only and checked for changes at teardown; concurrent readers need no reservation.
CUJ2 adds three separately collected cases for exact published MPS/MCP configuration, Codex
inference, and Claude inference. The config equality also accounts for the workspace's fixture
skill name as data; skill download and invocation are not covered.

The dedicated smart-routing CUJ in `../e2e_cuj/test_cuj4_smart_routing.py` leaves the original
managed-default and explicit-model cases unchanged. One additional test runs the five supported
`SMART_ROUTER_CONFIG_VERSION` values, checking both agents' first-prompt routing, orchestrator
context in inference input, and completed explicitly requested routed subagents.
This is not automatic orchestrator-delegation coverage. No workspace configuration is modified.
Agent-specific smart-routing preset component checks cover identical existing mappings,
complete per-agent validation, configure order, bootstrap and managed-default launch
selection, session controls, and restoration after configuration/launch failures.
These checks do not establish live agent coverage.

CUJ3 and CUJ4 require a non-empty HTTP 200 for inference. Claude 2.1.290's known
thinking-display 400 is accepted only when the next inference request with the same system
context and session metadata removes `display`,
preserves every other payload field, and succeeds; adaptive and enabled thinking are covered.
The native retry may also encounter `safeguards: Extra inputs are not permitted`; that
requires the next recorded attempt to remove only `safeguards` and reach a non-empty 200.
Offline regressions check both CUJs and reject changes to the model, prompt, budget, or effort.
Each preset cleans up interactive OS-managed settings with public `ug revert`, even on failure.
Delegated CUJ tasks wait for the native child's hidden file value, independently of the parent's
final turn. Offline checks reject parent-only answers. First-prompt tasks retain parent-turn evidence.
The CUJ terminal waits for stable billing-notice rendering before Enter and observes dismissal;
an offline PTY regression also exercises a later notice during child work.
Claude child HTTP prompts may carry one appended newline after the route-selection checkpoint;
offline cases reject any other prompt changes and retain exact Codex matching.
Offline CUJ cases also cover Codex delegated-turn completion and task-request matching;
see `../e2e_cuj/README.md` for the evidence requirements.

The [catalog discovery journey](../e2e_cuj/README.md) uses the CUJ3 workspace to check
agent-compatible pickers, schema exclusions, configured defaults, and real inference.
CI collects it through the shared `dedicated-cuj` job. Its request checks accept the known
Claude thinking-display 400 only with a successful, otherwise identical native retry.

This suite runs the **installed product** through subprocesses, against the same
`UCODE_TEST_WORKSPACE` used by the existing e2e tests. It does not import `ucode`,
patch application functions, substitute agent executables, run a fake gateway,
or construct ug state files. The normal test suite checks these boundaries.

The Claude mod entry point has separate native component checks in
`../native_claude/test_mod.py`. Claude 2.1.290 validates the module and runs its
TypeScript event-forwarding test in a dedicated CI job. These checks do not submit
inference requests or replace this suite's live gateway/TUI journeys.

The existing unit tests keep their fixtures. Integration has an independent
pytest configuration and uses `--confcutdir` so those fixtures cannot leak in.
It is not collected by the default `uv run pytest` command.
The Claude subagent skill-toggle journey opens `/tasks` after its final calculation
and waits up to 180 seconds for "No tasks currently running" or a native menu containing only
completed task rows before submitting `/exit`. Claude 2.1.290 retains finished children in a
`Completed (N)` list, so the empty-state message is not required when the list has exactly N
checkmarked `done` rows and no running or scheduled section. The helper observes menu dismissal
after Escape before typing `/exit`. Offline PTY checks reject active/scheduled tasks, partial
lists, and completed-task text outside the native menu.
It neither stops tasks nor confirms an exit dialog. Process exit retains its 30-second
timeout. Offline PTY checks cover this ordering.
The shared wait also runs before CUJ4's Claude preset sessions exit, after delegated-task
evidence is checked, because Claude can resume a child after its first completed answer.

Agent-specific bundled instruction selection and prompt/compaction hook content
are covered in `../test_skills.py`; those component checks do not run live agents.

`TestChildStdoutLaunch` in `../test_cli.py` covers clean Claude print-mode and
Codex exec/app-server stdout, early launch errors, and forwarding through ug's `--`.
Token membership before the agent's `--` intentionally also matches option values and prompt
tokens; component tests verify this only moves UG diagnostics, preserving agent arguments/stdout.
`TestBootstrapStdout` in `../test_agents_init.py` exercises real subprocess streams
with substituted installer/upgrader commands, including failures and native handoff.
These component checks do not establish live installer, Isaac, or inference coverage.
`TestInstallDatabricksCli` in `../test_databricks.py` checks that Windows PATH refresh
discards a cached missing CLI and reuses an existing WinGet alias or persisted user PATH
entry across repeated bootstrap. It does not establish a live Windows launch.
The live Claude/Codex headless prompt-argument and stdin journeys require raw stdout to parse
as JSON/JSONL, without discarding non-JSON lines before checking the completed file task.

The Claude explicit-model journey covers both `--model VALUE` and `--model=VALUE`
before and after ug's separator, with no workspace policy. It requires a completed
file task, the requested model with output tokens in JSON `modelUsage`, and no routing
wrapper. Use the runner's `--claude-model` option to reproduce a specific model.

The `smart_defaults` wire schema, legacy `spend_tiers` cache reads, and recommendation
request gating are covered by unit/component tests listed in `../README.md`. This suite
does not yet assert live `recommendModel` request counts for configs with and without tiers.

## Dedicated-workspace CUJ5

[`../e2e_cuj/test_cuj5_budget_defaults.py`](../e2e_cuj/test_cuj5_budget_defaults.py) runs
three budget cases in the shared `E2E CUJs` job against the fixed 1% tier. The above-tier
cases use `UG_CUJ_SP_CLIENT_ID` / `UG_CUJ_SP_CLIENT_SECRET`; the below-tier case uses
`UG_BUDGET_CUJ_SP_CLIENT_ID` / `UG_BUDGET_CUJ_SP_CLIENT_SECRET`.

Run from a clean POSIX runner with both credential pairs set:

```bash
UG_CUJ_SP_CLIENT_ID=... UG_CUJ_SP_CLIENT_SECRET=... \
UG_BUDGET_CUJ_SP_CLIENT_ID=... UG_BUDGET_CUJ_SP_CLIENT_SECRET=... \
uv run --with pexpect==4.9.0 --with pyte==0.8.2 \
  pytest --confcutdir=tests/e2e_cuj tests/e2e_cuj -v
```

All three cases submit no inference tasks. The above-tier bare-launch case checks
`ug usage`, the Luna recommendation, and Codex startup; it does not assert that Codex
applies Luna. The below-tier case verifies Claude/Sonnet selection and checks the
spend percentage when the backend supplies budget figures.

The explicit `ug claude` case verifies that startup displays the backend's Codex/Luna
recommendation while its generated `ANTHROPIC_MODEL` setting and native header select
Sonnet, then exits. It does not verify Claude's inference model. All three cases leave
workspace and budget configuration unchanged.

Shared subprocess command resolution is covered by `../test_subprocess_cross_os.py` and
enforced by Ruff. These component checks do not establish native Windows coverage
for every agent; the Windows journeys below validate their explicitly selected paths.

Shared UTF-8 text defaults, decoding-error handling, explicit overrides, and unchanged
binary output are covered in `../test_subprocess_cross_os.py`. Consumer regressions in
`../test_databricks.py` and `../test_codex_catalog.py` force a cp1252 default at the
subprocess dependency. These are component checks, not a separate live integration journey.

The provider component checks in `../test_claude_search_provider.py` verify that
copied marked helpers remain available in user aliases, project config, and strict
caller config. Only the verified launch override suppresses its catalog; these
local JSON-RPC checks make no model request and are not native integration coverage.
The same component suite checks ownership retention across an empty search-model
catalog and subsequent external handoff or standalone refresh. That recovery path
does not yet have a live integration journey.

Fresh Claude-only search discovery and registration are covered by component
tests in `../test_claude_search_discovery.py`, including legacy catalog fallback,
an explicit model override, and no available GPT model. Live search remains
outside this integration suite.

The sudo-session regression checks live in `../test_managed_files.py` and `../test_cli.py`.
They cover shared-worker invocation counts, shutdown/cancellation, and temporary-file failure
handling without sudo.
This integration suite does not yet assert password-prompt counts with sudo credential caching
disabled; that requires a disposable workstation/VM with an explicit sudo policy.

Caller permission-deny preservation is covered by `TestBuildClaudeArgv` in
`../test_agent_claude.py`, including direct, relayed, and routing configuration output.
Native parent/child enforcement and the distinction between technical search exclusion
and a semantic search deny are not yet covered by this integration suite.

OpenCode `--model` / `-m` selection is covered by unit/component tests in
`test_cli.py` and `test_agent_opencode.py`: the shared CLI passes raw model values,
and the OpenCode launcher validates and converts them before starting the native
process. The CLI still requires an available default model before launch, even
when an explicit model is supplied. The only live OpenCode journey is the headless
prompt case in `test_ug_opencode_headless.py`; there is no dedicated live OpenCode
model-selection journey.

## Shared agent helpers

`utils/agents/claude.py` and `utils/agents/codex.py` own the existing native answer
parsing, session paths, and child-session detection. `utils/evidence.py` dispatches
through them without changing existing journey commands or completion assertions.

## Run a specific combination

Prerequisites: Python 3.12+, uv, and Node/npm. Live runs also require Databricks
CLI 1.17.0. Full live/TUI runs require a POSIX host; Windows supports the explicit
headless subset described below. The runner installs the requested agents into a new
npm prefix and ug into a new virtualenv. Pytest and, for live runs, the PTY/screen
libraries (pexpect and pyte) live in a different virtualenv, so they cannot accidentally
supply a missing application dependency. No packages are installed into your
existing agent installations or checkout's `.venv`.
CI pins Databricks CLI 1.17.0 in the live and managed integration lanes. The
runner's isolated `PATH` exposes that selected CLI, so skills journeys meet ug's
CLI minimum without falling back to another version installed on the machine.
Native live runs refuse existing machine-wide Claude/Codex configuration, which
could override the selected workspace even with a fresh home. Use a clean VM
in that case; the runner never edits or bypasses those managed settings.

Use the existing e2e workspace and its `DATABRICKS_BEARER` credential. Locally,
`--profile YOUR_PROFILE` can mint a bearer for an explicitly selected profile.
No profile or workspace is selected automatically. The default test selection is `live` (all live CUJs).

```bash
export UCODE_TEST_WORKSPACE=https://your-existing-e2e-workspace

python3.12 scripts/run_integration.py \
  --ug-version checkout \
  --claude-version 2.1.268 \
  --codex-version 0.154.0 \
  --profile YOUR_PROFILE
```

`checkout` builds a wheel and installs it with fresh consumer dependency
resolution. **It does not use `uv.lock`.** This exercises the install path that
caught the tomlkit discrepancy in #496. To reproduce a user's release, pass its
exact distribution version instead, e.g. `--ug-version 0.1.0+f7b4b97`; this resolves
`unity-gateway==VERSION`. Use `--ug-wheel` with an archived wheel to reproduce a
legacy `ucode` distribution. Use
`--default-index` for the Python index that contains that release and `--npm-registry`
for an npm mirror if public npm is unavailable. Older releases that only
provide the `ucode` command require `--entry-point ucode`.

Select one agent by providing only its version. Exact agent versions are
required; floating `latest`, caret, and tilde versions are rejected. Hosted provider CUJs use the workspace's configuration and need no model input. Cases that
exercise explicit model arguments use a real `system.ai` model already discovered
by `ug configure`, recorded in that case's `model.json`. Optional `--claude-model`
`--codex-model` and `--opencode-model` overrides reproduce a particular model-related failure.
OpenCode is opt-in: pass `--opencode-version` (for example `1.18.31`) to install and select it.

```bash
# Constrain the suspected dependency while keeping the real CLI and gateway.
python3.12 scripts/run_integration.py \
  --ug-version checkout --claude-version 2.1.268 --codex-version 0.154.0 \
  --claude-model YOUR_CLAUDE_MODEL --codex-model YOUR_CODEX_MODEL \
  --opencode-version 1.18.31 --opencode-model YOUR_OPENCODE_MODEL \
  --profile YOUR_PROFILE --dependency tomlkit==0.14.0 \
  -- -k 'app_server or app_help'
```

Repeat with `--dependency tomlkit==0.15.1`, or run without constraints to test
what a new consumer gets today. Several `--dependency` options can be supplied.
Constraints incompatible with the selected ug release fail installation.

For package-only validation without credentials:

```bash
python3.12 scripts/run_integration.py \
  --ug-version checkout --claude-version 2.1.268 --installation-only
```

This explicitly selects only the installation checks; it does not claim a live
integration pass. Requested live checks fail when credentials, binaries, models,
or capabilities are missing. There are no capability-based skips or retries of
failed model tasks. A failing historical version should remain a failing result.
The installation-only path also runs natively on Windows, using the installed
`ug.exe` and `ucode.exe` entry points. Windows also supports `--headless-only`:
it selects the existing prompt-argument journey for each requested agent, with
real configure, launch, and a completed file-reading task through the gateway.
Supply the existing e2e workspace and bearer (or an explicitly selected profile),
just as for a POSIX live run. This is not TUI coverage. Windows live PTY/TUI
journeys, managed settings, and signal behavior remain outside this subset.

```powershell
python scripts/run_integration.py --ug-version checkout --claude-version 2.1.268 --headless-only
```

Use `--codex-version 0.154.0` instead of or alongside the Claude version to test
Codex. Headless selection uses exact test node IDs, so unrelated POSIX-only
modules are not collected. Missing prerequisites and failed tasks still fail.

Installation checks also invoke both `ug` and `ucode` auth helpers using the public
bearer override and drive their real local web-search MCP handshake/tool listing.
These assert protocol stdout without stripping ANSI escapes and make no workspace
requests. The live Hosted configure journeys additionally execute the actual `ug`
auth helper written into each agent's configuration before completing a real TUI task.
The Claude journey also opens `/model` after a plain `ug claude` launch, requires
its native gateway cache to contain `system.ai` models, and checks that a discovered
model appears in the picker. No managed config, provider, model location, discovery
flag, or inherited discovery environment variable enables this path. This runs with
the pinned Claude version (currently 2.1.280 in CI).

## Test layout and format

All user journeys are top-level tests. There is no separate regressions category:

```text
test_ug_configure_claude.py             # Databricks Hosted and Anthropic MPS
test_ug_configure_codex.py              # Databricks Hosted and OpenAI MPS
test_ug_claude_custom_oauth.py           # CLI custom-OAuth launch, profile, and managed helper
test_ug_codex_custom_oauth.py            # CLI custom-OAuth launch and profile
test_ug_claude_headless.py              # script prompts, models, caller settings
test_ug_claude_relayed.py               # relayed session: subscription + Databricks-hosted models
test_ug_claude_tracing.py               # Claude OTLP export reaches the configured trace table
test_ug_codex_headless.py               # script prompts and model arguments
test_ug_codex_tracing.py                # Codex OTLP export reaches the configured trace table
test_ug_claude_commands.py              # command help forwarding
test_ug_codex_commands.py               # command help and parser error forwarding
test_ug_codex_app_server.py             # actual client/server initialize exchange
test_ug_smart_routing_hooks.py           # live hook contract plus skill-driven subagent toggles
test_ug_configure_claude_lifecycle.py   # repeat setup, revert, rejected credentials
test_ug_configure_claude_workspace_switch.py # real skills MCP cleanup across two workspaces
test_ug_configure_codex_lifecycle.py    # repeat setup, revert, rejected credentials
test_ug_claude_managed_model_discovery.py # Claude MPS policy cases (claude_mps.json)
test_ug_codex_managed_model_discovery.py  # Codex MPS policy cases (codex_mps.json)
test_ug_claude_model_discovery.py       # unmanaged scenarios 7, 9, 11, 13
test_ug_codex_model_discovery.py        # unmanaged scenarios 8, 10, 12, 14
test_ug_configure_managed.py            # stubbed static model list/catalog pointer, no agent selector, cache TTL
test_ug_configure_managed_models.py     # stubbed model sources, smart-routing banner, Codex fallback metadata
test_ug_configure_managed_mcp.py        # stubbed managed MCP list
test_ug_configure_managed_skills.py     # stubbed managed skills: download, coexist, reconcile away
test_ug_configure_managed_lifecycle.py  # none -> A -> B -> MPS -> none: reconcile, clear on MPS/no-config
test_installation.py                   # fresh installed package
utils/                                # process/terminal/evidence helpers and Docker files
```

`conftest.py`, `pytest.ini`, and this README stay at the suite root for pytest
discovery and run instructions.

Each test has a `Scenario:` / `Expected:` docstring and shows its own public
configure command, launch, user action, and assertions. Shared code only handles
process/terminal mechanics, evidence, and cleanup. Fixtures supply an isolated
session and credentials; none manufacture or configure application state.

ug no longer runs a post-configure agent probe, so no CUJ validates; each
journey still requires its own completed interactive task or command assertions
(the deprecated `--skip-validate` flag is accepted as a no-op where older
journeys pass it). Tests disable optional Databricks AI Tools and retain
`--skip-upgrade` as a deprecated no-op for compatibility. UG only upgrades
agents below its required minimum; before/after version checks still enforce the
selected versions. Fable, subset selection, and required-update policy are covered
by unit/component tests, not dedicated live journeys. Tests use real onboarding
and trust choices, without seeded acceptance or disabled agent sandboxing. If a
routed child asks to locate the random fixture beneath the disposable project,
the terminal driver accepts that exact read-only command through Claude's real
permission dialog; any broader permission request fails immediately.

A fixture file contains an unpredictable value absent from the prompt. Success
requires an assistant answer in the real agent transcript containing that value,
plus normal TUI exit. Codex evidence requires its task-complete event.
Interactive first-prompt routing is covered by the managed_fixture smart-routing
banner journeys below for both agents. The route-subagent hook journeys drive the real
installed hook commands with a harness-shaped payload against the live router. The
subagent-only TUI journeys additionally spawn three native children in one real session,
invoke the installed Smart Router skill to turn routing off and back on, and require the
subagent banner plus a correlated live decision only while routing is enabled. Plugin-refresh
survival, native daemon/background dispatch, interactive explicit-model bypass, and dedicated
smart-routing CI shards remain deferred. Plugin generation and launch arguments are covered
by component tests in `../test_claude_smart_routing_v2.py`, not by a live registration journey.

PATH conflicts for the Smart Router skill have subprocess/component coverage in
`../test_smart_router.py`: the skill uses the launching interpreter despite a different
`ug` first in PATH. The live journeys above do not inject a second installation or
establish PowerShell command execution.

The toggle journeys retain the legacy routing-only and orchestration cases and add each of
the three subagent-only `SMART_ROUTER_CONFIG_VERSION` presets. They require only `smart-router`
for routing-only cases, and both `smart-router-orchestrator` and `smart-router` when orchestration
is enabled. They verify the saved session controls, a new CLI confirmation in the native
tool-result records, and a new
assistant answer after each skill invocation.
Collapsed terminal output is allowed; the answer need not repeat the CLI's exact wording.
Each following child still verifies whether a routing decision occurred.
With orchestration enabled through legacy flags or a preset, both routed children
must show `[orchestrator on]` in their own subagent banner.
Their off-phase child is an explicit user-requested delegation;
these journeys do not establish automatic orchestration behavior. Root-only
activation, compaction, retained skills in ineligible sessions,
role-contract preservation, and isolation from legacy preference files
lack dedicated regression coverage. Codex's native hook merging, project trust,
and execution of pre-existing hooks are not exercised by this integration suite.

Preset parameterization augments only routing hooks, skill toggles, and first-prompt routing;
their original legacy-env or managed-default cases remain. Explicit-model selection, command
forwarding, app-server initialization, and catalog fallback retain their original legacy flags
and on/off coverage; preset-specific behavior there is not covered.

The unit/component `../test_smart_routing_config.py` includes a 162-case Cartesian oracle over all
three legacy routing flags (`None`, `0`, `1`) and six selector forms (`None` plus the five
supported presets, including the customer first-prompt-and-subagent mode). It independently
hardcodes preset values and asserts exact
`resolve_environment` and `apply_config` settings, true-unset omission, unrelated-key and
input preservation, valid-selector consumption, and environment restoration.
Absent/unknown selectors are no-ops; CLI checks cover auth, revert, and launch dispatch.
These component checks do not establish live agent, hook, or gateway behavior.
Three component cases additionally compare the equivalent legacy flags and presets for
`subagent_only_v1`, `subagent_orch_v1`, and `first_prompt_and_subagent_no_orch_v0`;
they verify routing activation, first-prompt behavior, orchestration, and router-name preservation.

The portable `../test_claude_windows_smart_routing.py` checks the Windows
subagent-only fallback without Unix imports. Native Windows TUI and hook execution
remain outside this integration suite.

The relayed CUJ launches Claude through a relayed (subscription-relay) MPS and
completes a file task on two models: a bare Anthropic id the subscription serves
directly (`route=relay`) and a Databricks-hosted `system.ai` id the loopback proxy
re-routes to gateway auth (`route=databricks`) — one relayed session reaching both.
It needs a subscription OAuth token (see below). Interactive model-picker selection
remains uncovered.

MPS CUJs select the existing services already used by e2e:

- Claude: `main.ucode.ci_e2e_anthropic_nonrelay_mps`.
- Claude (relayed hybrid): `main.ucode.ci_e2e_anthropic_relay_mps`, which also
  requires `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) so the relayed
  launch runs headless instead of opening a browser login.
- Codex: `main.ucode.ci_openai_mps`, using its allowed `gpt-5-nano` model.

Use `--claude-provider` / `--claude-relayed-provider` / `--codex-provider` to
reproduce another existing service. Use `--claude-provider-model` /
`--codex-provider-model` when it allows a different model. Those choices are recorded in `versions.json`.
No service is created or modified. A missing service, permission, or OAuth token
fails the selected CUJ, rather than skipping it.

The tracing journeys are part of their respective Full agent lanes and use the existing
e2e workspace and bearer. Because that workspace deliberately has no published managed
configuration, each journey injects only a tracing-enabled CodingAgentConfig input (the
`claude_tracing.json` / `codex_tracing.json` fixtures) through the suite's managed-config stub; the agents, inference, OTLP export, and table
verification remain real. Each adds the prompt's UUID as a trace-safe
`ug_integration_marker` attribute, resolves the destination table from the workspace tracing
configuration, waits 30 seconds, and queries that table through an existing SQL warehouse.
The tests assert that a span with the marker arrived and identifies the requested model.

Scoped discovery additionally requires Model Services
`main.ucode.ci_e2e_claude` and `main.ucode.ci_e2e_codex`. Override them with
`--parent-schema`, `--claude-parent-model`, or `--codex-parent-model`. The tests
consume but never create or modify them.

These unmanaged journeys require a workspace that publishes no CodingAgentConfig.
Before any of them configures or launches an agent, a session-scoped, read-only
List request checks that prerequisite. A published config fails with its resource
name; the suite does not delete it, inject a null config, or bypass admin policy.
A code/collection pass does not establish that the live workspace meets this
prerequisite; the live check must pass on each run.

Current main enables discovery automatically; it has no `UG_ENABLE_MODEL_DISCOVERY`
switch or configure-time `--model-location`. Cases 7/9 cover configured/fresh
Claude default discovery, including its real gateway cache and picker. Claude's
recognized `anthropic-aigw-<8-hex-digits>-` aliases are unwrapped for `system.ai`
membership and discovered-family checks; malformed aliases, non-system models,
and duplicate raw IDs still fail. Cases 8/10 require ug's discovered `system.ai`
models while leaving Codex's model and reasoning preferences unset, and expose
Codex's native catalog without a scoped file.
Cases 11–14 retain the exact provider/parent catalog assertions for supported launch
overrides. Cases 11/13 also require explicit Claude provider/model-location launches to replace
built-in picker rows when no managed config exists. They also check that the remaining
Default row names the model in the scoped fixture catalog, selected through the launch-only
`ANTHROPIC_DEFAULT_MODEL` environment variable. Unit tests cover the existing Opus, Sonnet,
then Haiku preference for catalogs with multiple families. Plain Claude launches retain native defaults.
Obsolete disable-flag scenarios and duplicate managed variants are
removed, not skipped; managed discovery and rejection remain covered by Cases
1–6. Repository scenario numbers run consecutively from 01 to 14, with
configured/fresh variants sharing a number. External design-document numbering
remains unchanged and is independent of these repository IDs.

The parent-schema catalog is API-specific: Claude's cache must contain exactly
the Claude service. Codex's app-server catalog must exactly match a separate read-only
Codex model-list request with the parent-schema header, including the dedicated Codex
service and no out-of-schema models. A Claude service is included only if that API
advertises it as compatible. Extra, missing, or duplicate app-server entries fail.
Claude provider discovery still requires the exact `--claude-provider-model` ID
in its cache. For the default `claude-haiku-4-5-20251001` fixture, Claude can
deduplicate it into the native Haiku picker row (Haiku 4.5), while an explicit
provider or parent replacement picker can render the raw gateway ID/display name;
the assertion accepts either numbered-row form. Custom Model Services must
still appear by their gateway IDs or display names in a numbered picker row;
startup banners and footer text cannot satisfy discovery assertions. Cases 7–14 send no inference prompts;
they only configure, list models, and open/close the picker. Other live CUJs perform
real model tasks.

There are **80 live cases** (including 14 marked TUI journeys) and **7 installation
checks** with Claude and Codex; selecting OpenCode adds one live headless case. One
**`workspace_switch` case** uses two real workspaces and checks skills MCP cleanup and a completed
Claude task. A further **43 `managed_fixture` cases** (ten of them also `live`) run on the
managed workspace with a checked-in JSON CodingAgentConfig from `tests/fixtures/managed_config/`
injected through `UCODE_MANAGED_CONFIG_STUB`; there are no cases that read a published config.
Twelve explicit configured/fresh Claude and Codex discovery and source-override journeys use the
`claude_mps.json` / `codex_mps.json` fixtures, which set each agent's source to its dedicated MPS.
The other cases cover focused model, MCP, skills, cache-TTL, and lifecycle shapes, including
per-agent model reconciliation and managed skill cleanup. Of the two Claude default-model cases,
the MPS case stubs a `main.default.ci_e2e_anthropic_mps` source and the `system.ai` parent-schema
case stubs a Unity Catalog source; both verify the generated settings
files retain all admin-authored family defaults. Their replacement pickers contain those mapped defaults plus
the independently fetched MPS or UC schema catalog. MPS family shortcut rows remain separate
from catalog rows for the same target; UC model IDs are deduplicated and catalog labels are retained.
Direct renderer tests cover default/catalog composition, while focused CLI regressions verify UC
catalog discovery with overall defaults, family defaults, or both, along with explicit model
selection and preservation of static model lists. Both cases run on the managed workspace's own
bearer; no second workspace or extra secret is involved.
The 14 retained numbered scenarios comprise 24 explicit journeys: 12 managed and 12 unmanaged
executions; the complete integration suite collects 121 executions. See the named coverage and gaps matrix in
[../README.md](../README.md).

```bash
# Append one of these selections to the runner command:
-- -m live         # default: all live user journeys
-- -m smoke        # six Hosted, custom OAuth CLI TUI, and headless journeys
-- -m 'live and tui'  # twelve interactive live configuration/model-discovery journeys
-- -m 'live and claude' -k trace  # installed Claude -> gateway -> configured trace table
-- -m 'live and codex' -k trace  # installed Codex -> gateway -> configured trace table
-- -k test_ug_codex_app_server_client_initializes  # one named journey and its variants
# Use --installation-only before -- for package checks without credentials.
```

The old focused checks are now descriptive CUJs with setup and outcomes visible
in each test. Duplicate boot-only checks are incorporated into the Databricks
configuration TUI journeys. Real failures, including generated
config left after revert and banners on app-server stdout, remain assertions.
Live MCP/skills functionality, the broad configure-option matrix, and other
agents are outside this focused revision.
Custom OAuth search dispatch and refresh are covered by component tests in
`../test_mcp_web_search.py`; no live search request, delegated search, or classifier
permission decision is asserted by those tests or this integration suite.
`../test_mcp_web_search_concurrency.py` separately covers stdio search concurrency,
the four-worker limit, cancellation, worker errors, EOF draining, and interrupted
input cleanup with controlled auth/HTTP boundaries. It does not establish live
gateway concurrency or latency.
External-provider ownership has local configuration/subprocess coverage in
`../test_claude_search_provider.py`. Live Isaac provider discovery and search,
including both parent and child catalogs, remain separate acceptance work.

The workspace-switch CUJ is an exception to that deferred multi-workspace scope:
it configures the first workspace and registers its skills MCP through `ug skills`,
switches to a second real host using that host's bearer, and verifies the stale
registration is removed from Claude and the target workspace state. It preserves
the first workspace's saved bucket, repeats configure, and requires a completed
Claude file task on the second workspace. This exercises real commands, state,
and agents without injected configuration. Deterministic duplicate-attempt and
timeout/missing-executable regressions remain in `../test_mcp.py` and `../test_cli.py`;
the CUJ does not force an agent failure.

Run this case with both agents installed if either workspace's managed config
enables both. Supply the second workspace and its bearer explicitly:

```bash
# DATABRICKS_SECOND_BEARER must already contain a token for SECOND_WORKSPACE_URL.
python3.12 scripts/run_integration.py \
  --ug-version checkout --claude-version 2.1.268 --codex-version 0.154.0 \
  --workspace FIRST_WORKSPACE_URL --profile FIRST_WORKSPACE_PROFILE \
  --second-workspace SECOND_WORKSPACE_URL -- -m workspace_switch
```

`UCODE_TEST_SECOND_WORKSPACE` is the environment equivalent of `--second-workspace`.
Missing credentials or equal workspace hosts fail the selected test. The runner
records both URLs in `versions.json`, redacts both bearers in evidence, and passes
only the active workspace's bearer to each tested command. CI runs the case in
the existing **Managed config · Claude** lane: the first host uses
`E2E_ADMIN_WORKSPACE` and its service-principal credentials; the second uses the
existing `UCODE_TEST_WORKSPACE` / `DATABRICKS_BEARER` secrets. That lane is
required for full/live runs. Collection or lint success is not a live pass.

The configure terminal helper recognizes `[✓]` / `[ ]` agent checkboxes as well
as legacy markers in older pinned ug releases. It explicitly toggles
the requested agent on and all others off before submitting; the existing live
journeys still require a completed agent task.

## Reproduce a failure

Each run writes a new `.integration-runs/<timestamp>/` directory containing:

- `versions.json`: requested and observed ug/agent versions, Python, Node, uv,
  Databricks CLI, platform, source revision/diff, suite hash, and wheel hash when available.
- `dependencies.txt` and `npm-lock.json`: the resolved Python and npm dependency
  graphs. Replay them with `--constraints` and `--npm-lock`.
- `test-dependencies.txt`: the separately installed pytest/terminal-tool dependencies.
- `junit.xml`: exact test outcomes and parametrized case names.
- `artifacts/`: command arguments, exit codes, timeout status, redacted output,
  app-server protocol diagnostics, and TUI transcripts/rendered screens plus
  keystroke actions and routing logs. No credential files are archived.
- `wheels/`: the tested wheel when built from the checkout; replay it with
  `--ug-wheel`. For release installations, `installed.txt` records the resolution.

Teardown invokes real `ug revert` through a PTY when setup created state, restoring machine-level
configuration through the public CLI. Per-test homes and working directories are
then deleted even on failure.
The working directory is outside the checkout so an agent cannot inherit its
project settings or instruction files by walking parent directories. Virtualenvs,
agent packages, and build caches remain under the results directory for local
inspection; remove that run directory when finished. Agent versions are checked
before and after the suite so an automatic upgrade cannot silently change the
combination being tested. Model requests and subprocesses have deadlines, and
the process group is cleaned up after each command.
Selection after `--` accepts `-k`, `-m`, `-x`, and `--maxfail`; configuration and
report paths cannot be overridden. `--installation-only` always restricts the
selection to installation checks, including when additional filters are used.
`--headless-only` restricts it to the two named prompt-argument journeys (one per
selected agent). The two modes are mutually exclusive.

## Run in GitHub Actions

The **CI** workflow calls **Integration** on pull requests and pushes to `main`,
starting alongside unit tests and the existing agent e2e shards. Integration has
no dependency on agent e2e; a failure there does not prevent integration from running.
The final required `e2e` check waits for both suites and requires both to succeed.
The required installation and live jobs run directly on fresh GitHub Ubuntu VMs,
not inside the optional Docker image. An advisory `windows-server-latest` job runs
the five credential-free checks in `test_installation.py` (installation, CLI,
auth-helper, and local MCP) and
uploads `integration-installation-windows` evidence. It uses `continue-on-error`
and is not part of `All integration tests` until the initial Windows issues are fixed.
The Windows job authenticates to the Databricks JFrog package proxy using
GitHub OIDC, following the organization's SDK CI setup. Its actual OS image is
recorded in `versions.json`; the organization can update the image behind the
runner label. The two POSIX version-floor journeys are outside this Windows subset.
It installs only Claude as the runner prerequisite; the five selected checks
exercise ug and its local helpers, not either agent's inference path.
An advisory **Windows headless journey · Claude** job installs the temporary Windows-pinned
Claude version (currently 2.1.278)
on a native Windows runner using the same authenticated package proxies,
reuses the existing e2e workspace/bearer,
and requires the unpredictable file value in the agent's structured final answer.
It uploads `integration-headless-windows-claude` evidence and remains outside
the required gate while native failures are diagnosed. Codex's Windows CI
journey is deferred while the npm proxy rejects its package metadata; the runner
still supports explicitly selecting it once the requested package is available.
Local native runs use the same runner; Colima/Docker provides a separate Linux
container option. Matching dependency versions does not make those OS environments identical.
Installation jobs need no workspace credentials. For same-repository PRs, the live jobs
reuse the existing `UCODE_TEST_WORKSPACE` and `DATABRICKS_BEARER` secrets; the full
Claude lane also passes `CLAUDE_CODE_OAUTH_TOKEN` (the same secret the e2e workflow
uses) for the relayed hybrid CUJ. Fork PRs run installation checks only because they
cannot receive those secrets.

The workspace check requires the secret to match
`https://eng-ml-inference-team-us-east-1.cloud.databricks.com` (a trailing slash
is accepted). It never changes the secret or switches workspaces. There is no
separate CI model-selection job; the full agent lanes include scoped model
discovery. Real `ug configure` performs its normal workspace discovery inside
each test; only explicit-model scenarios choose and record a discovered
`system.ai` model as a test argument.
Every same-repository PR and push to `main` runs **Smoke journeys**, followed by
**Full journeys** even if smoke fails. Smoke runs the Hosted configure/TUI,
headless argument, and custom OAuth CLI TUI journeys for each agent (six cases,
two agent jobs). Full runs all 80 live cases, including those smoke cases, in two
disjoint agent lanes:

| Agent lane | Marker | Cases |
| --- | --- | --- |
| Claude | `live and claude` | 38 |
| Codex | `live and codex` | 42 |

A non-blocking **OpenCode** job (`live and opencode`, one case) runs alongside them with
`continue-on-error` and is not part of the required `cujs` gate until it is stable.
A second non-blocking job, **Managed OpenCode self-managed** (`managed_fixture and opencode`, two cases
in `test_ug_agents_self_managed.py`), installs Claude, Codex and OpenCode and runs against the
managed e2e workspace (`E2E_ADMIN_WORKSPACE`) with the injected `managed_workspace_default` config,
which enables Claude/Codex but not OpenCode. It is likewise outside `cujs`.

Each lane installs only the agent CLIs it needs (one, except the managed OpenCode lane's
three), once, and runs all its configure, headless,
commands, lifecycle, and applicable app-server journeys. Cases remain serial
inside each fresh VM because configure/revert can touch machine-level settings;
separate runners isolate those writes as well as the PTYs. Claude and Codex run
in parallel, with at most one full-suite job per agent in a workflow run. This
avoids six serial job startups without overlapping same-agent shards. The two
lanes still share workspace capacity with the concurrently running agent e2e
shards and other PRs; this limit does not guarantee freedom from rate limits.
No test retries or assertion changes
compensate for capacity failures. Both matrices use `fail-fast: false` and upload
uniquely named evidence even when the other agent fails.
The **All integration tests** check requires installation, workspace validation, smoke,
both full lanes, and both **Managed config** lanes to pass for full/live runs. Each tracing
journey is included in its agent's Full lane. The managed lanes do not use `continue-on-error`:
a failure, cancellation, or unexpected skip fails the aggregate check. Manual smoke, TUI,
and installation subsets do not select managed tests and do not require them.
The advisory Windows installation and headless lanes are not yet included in that aggregate check.
The existing required `e2e` context also waits for the complete integration workflow, so integration
cannot still be running when that gate passes. Full coverage on PRs needs no label or opt-in.

### Managed-workspace journeys

The managed lanes run `managed_fixture` journeys (and `workspace_switch`) on the single managed
workspace (`E2E_ADMIN_WORKSPACE`), in per-agent **Managed config** jobs. Every managed config comes
from a JSON file in `tests/fixtures/managed_config/` in the GET shape of one CodingAgentConfig; a
fixture whose content is JSON `null` stands for a workspace with no managed config. Tests select a
fixture with `use_managed_config_fixture(session, name)`, which points `UCODE_MANAGED_CONFIG_STUB`
at the file.
`ug configure --file` (#866) will replace the environment stub; because the inputs are already
checked-in files, that switch is mechanical.

`test_ug_configure_managed.py` uses `managed_workspace_default.json`. `ug configure` applies the
config with no agent selector, and each agent's generated config exposes exactly its static
`model_services` (Claude's `availableModels`/`modelPicker`, Codex's model catalog). The stub takes
the same cache path as a live read (`published` outcome, `retrieved_at` stamp, TTL), so the
cache-TTL journey holds unchanged. The Codex case also checks stderr guidance to restart the
daemon after publication, that the shared app config points at the stable catalog, and that a
fresh bare Codex app-server returns the expected visible model before the existing TUI
prompt/input assertion. It does not claim GUI rendering or inference coverage.

`test_unmanaged_claude_preserves_preexisting_family_defaults` is a live lifecycle journey against
one real workspace. A read-only check first proves that the workspace publishes no
CodingAgentConfig. The test then seeds `/etc/claude-code/managed-settings.json`, runs public
`ug configure`, and requires every pre-existing family default to survive exactly. It checks
settings reconciliation and makes no model-inference claim.

The expected model ids in `test_ug_configure_managed.py` mirror `managed_workspace_default.json`;
update both together. The fixtures reference real securables on the managed workspace (MPS
`main.default.ci_e2e_anthropic_mps` / `ci_e2e_openai_mps`, MCP service `system.ai.github`, skill
`main.default.forkable-meals`), which must keep existing.

The `managed_fixture` journeys use `UCODE_MANAGED_CONFIG_STUB` to short-circuit only the
managed-config HTTP read. The Claude discovery module uses `claude_mps.json`, whose Claude model
source is `main.default.ci_e2e_anthropic_mps` with no static defaults, across all configured/fresh
scenarios. The Codex module does the same with `codex_mps.json` and
`main.default.ci_e2e_openai_mps`. Separate read-only, provider-scoped model-list requests
establish expected IDs independently of the generated agent files. With no authored defaults,
Claude's native cache and replacement picker must match those IDs, preserve catalog display names,
and show a model in a numbered picker row alongside its admin header.
Codex's scoped and stable catalogs, ug-launched app server, and fresh bare app server must match
its independently fetched IDs. The configured Codex journey subsequently runs real `ug revert`,
verifies that the shared pointer and stable catalog are gone, and checks that a user-owned setting
survives. These requests send no inference prompts. Both agents must reject personal source
overrides. This checks desktop startup configuration and cleanup, not GUI rendering or inference.
Codex state comparisons exclude `.codex/tmp/arg0`, the disposable executable links
recreated by version checks, while continuing to compare persistent agent files.
In addition, `test_ug_configure_managed_codex_catalog_fallback` injects the intentionally nonexistent
`system.ai.gpt-99`, keeping it out of the real workspace while launching Codex through that
workspace on the valid default model `system.ai.gpt-5-6-sol`. With legacy smart routing off and
on, it opens the real Codex `/models` picker and requires that injected custom-catalog model
to be listed. The fixture itself has no managed smart-routing setting.

The smart-routing banner journeys inject static Claude and Codex model lists with
`smart_routing` enabled in the agent config, run `ug configure`, then launch the real TUI and
submit one small file task. Each runs once with the selector unset (managed default) and once
with `first_prompt_and_subagent_no_orch_v0`, asserting the "Using Unity Gateway Smart Router."
banner naming the selected model, a routed answer completing the file task, and normal exit.

That workspace authenticates as a service principal, so CI mints a short-lived token per run from
these same-repository secrets rather than storing a long-lived bearer:

- `E2E_ADMIN_WORKSPACE`: the managed workspace URL.
- `E2E_ADMIN_SP_CLIENT_ID` / `E2E_ADMIN_SP_CLIENT_SECRET`: the service principal's OAuth client
  credentials. The job passes them to the runner as the standard `DATABRICKS_CLIENT_ID` /
  `DATABRICKS_CLIENT_SECRET`, and `run_integration.py` mints the workspace token.

Run it locally the same way, pointing at the managed workspace:

```bash
export UCODE_TEST_WORKSPACE=https://<managed-workspace>
export DATABRICKS_CLIENT_ID=<sp-app-id> DATABRICKS_CLIENT_SECRET=<sp-oauth-secret>
python scripts/run_integration.py --claude-version <v> --codex-version <v> -- -m managed_fixture
```

To run only the two Claude defaults cases locally, select them by name with the same environment:

```bash
python3.12 scripts/run_integration.py \
  --ug-version checkout --claude-version 2.1.268 --codex-version 0.154.0 \
  -- -k 'test_managed_claude_mps_defaults_accompany_discovery or test_managed_claude_parent_schema_defaults_accompany_discovery'
```

Each job uses fresh consumer dependency resolution. There is no default dependency
matrix. Manual dispatch accepts an
optional `dependency` such as `tomlkit==0.14.0`, equivalent to the local runner's
`--dependency` option. Live agent jobs use Ubuntu 22.04; newer Ubuntu runner
policies prevented Codex's bubblewrap tool from reading even the test file in the
first run. The agent sandbox is not disabled or bypassed.
The workflow consumes the stored bearer; it does not mint or refresh credentials.

Pull requests also run the **User Journey Test Required** policy check. A trusted
base-branch script sends a bounded product and `tests/integration/` diff to a
Databricks-hosted LLM judge using the existing `UCODE_TEST_WORKSPACE` and
`DATABRICKS_BEARER` secrets. The judge also receives the trusted base-branch
`tests/AGENTS.md` policy so it can reject mocked, trivial, or otherwise invalid
coverage. If a change adds or materially changes a user journey without meaningful
integration coverage, the check fails with a CTA to add or update the relevant test
under `tests/integration/`. Set the repository variable
`UG_CI_USER_JOURNEY_JUDGE_MODEL` to a chat-capable `system.ai` model and make
`User Journey Test Required` a required branch-protection check. For an exceptional
bypass, only `@rohita5l` or `@lilly-luo` can post the exact PR comment
`/skip-user-journey-test`. A trusted comment workflow mirrors that authorization to the
visible `skip-user-journey-test` label, causing the gate to rerun. Editing or deleting the
comment removes the label and reruns the gate; manually adding the label does not bypass it.

For a manual run, use **Actions → Integration → Run workflow**, select the branch,
and set the ug/agent versions. Manual runs execute the complete integration suite.
From the CLI:

```bash
gh workflow run integration.yml -R databricks/unity-gateway --ref YOUR_BRANCH \
  -f ug_version=checkout \
  -f claude_version=2.1.268 -f codex_version=0.154.0
gh run list -R databricks/unity-gateway --workflow integration.yml
gh run watch RUN_ID -R databricks/unity-gateway --exit-status
```

GitHub enables manual dispatch once the workflow exists on the default branch.
Before this PR merges, CI's pull-request event calls the workflow. Missing
credentials or a workspace mismatch fail the workspace job. Expired or invalid
credentials fail the actual workspace calls. Those failures do not count as live
test passes.

## Reproduce and debug a CI failure locally

Use the same runner and the failing job's artifacts. A new developer machine
needs Python 3.12+, uv, Node/npm, Databricks CLI, and its own authorized login for
the CI workspace. Select that local profile explicitly; CI secrets are not downloaded.

```bash
gh run download RUN_ID -R databricks/unity-gateway \
  -n integration-full-claude -D .integration-runs/from-ci
```

Use `integration-full-AGENT` for a full lane, `integration-smoke-AGENT` for
smoke, `integration-installation` for Linux package failures, or
`integration-installation-windows` for native Windows package failures, or
`integration-headless-windows-claude` for the Windows gateway journey. Older runs used
`integration-full-AGENT-GROUP`, `integration-cujs`, or numbered `integration-live-*`
artifacts; download the name
shown on that run. Read `versions.json` for the
exact agent versions, model overrides, entry point, platform and source revision.
For an explicit-model case without a runner override, read its `model.json` for
the exact model used. Basic boot cases require no model arguments. Use
the archived wheel so a changed checkout cannot alter the reproduction:

```bash
python3.12 scripts/run_integration.py \
  --ug-wheel .integration-runs/from-ci/wheels/EXACT_WHEEL.whl \
  --entry-point ug \
  --claude-version CLAUDE_VERSION_FROM_REPORT \
  --workspace https://eng-ml-inference-team-us-east-1.cloud.databricks.com \
  --profile YOUR_E2E_PROFILE \
  --constraints .integration-runs/from-ci/dependencies.txt \
  --npm-lock .integration-runs/from-ci/npm-lock.json \
  --output .integration-runs/repro-1 \
  -- -k test_ug_configure_claude_databricks
```

For an explicit-model failure, also pass the recorded `--claude-model` or
`--codex-model`. For a release run without an archived wheel, use the reported `--ug-version`.
The example replays a Claude case; for Codex use only `--codex-version` and its
test filter. Match the report's selected agents, `pytest_args`, and suite revision
to replay an entire lane; a `-k` filter can reproduce one case independently.
Match Python and Node versions from the report too. `npm-lock.json` replay must
use the same OS/architecture as the original run; add `--platform linux/amd64`
to both `docker build` and `docker run` on an ARM Mac to match GitHub's Ubuntu runner. Changing platforms or
resolving a fresh npm lock is a new comparison, not an exact dependency replay.

Use `-- -m tui` for the six interactive journeys or
`-- -k test_ug_configure_codex_databricks` to narrow a failure. Each rerun needs a new output directory. Inspect:

- `junit.xml` for the failing case and assertion.
- `artifacts/<case>/command-*.json` for the real argv, exit status, stdout and stderr.
- `artifacts/<case>/first-session.json`, `provider-session.json`, or `reopen.json`
  for rendered terminal screens, raw terminal
  output, keyboard actions, exit status, routing logs, and actual agent-session records.
- `install.log` for resolution/bootstrap failures.

Unknown onboarding screens fail with their actual screen text. Update terminal
selectors only after confirming the agent's intended UI changed; do not seed its
onboarding state or relax the prompt/task assertions. Test homes are deleted after
each case; redacted diagnostics remain. For manual interaction, configure a fresh
home with the same installed binaries and recorded public CLI arguments.
Definitive API errors and the client's exhausted retry limit fail the TUI wait
immediately with the actual screen. A transient 429/503 while the client is still
retrying is not treated as terminal; the suite adds no task retries of its own.

## Colima / Docker

Docker is an optional installation/command-check environment, not a guaranteed
replacement for the native Linux live suite. Default Colima/Docker security
policies can reject Codex's user namespaces; the image also lacks `sudo` needed
by machine-level configure/revert journeys. Do not disable the agent sandbox or
use privileged/unconfined containers to turn these into passing results. Use a
clean Ubuntu 22.04 VM for the complete live run below.

Colima provides the Linux Docker engine on macOS. The optional image pins the
Python, Node, uv, and Databricks toolchain; the same runner selects ug and agent
versions inside it. Build from the repository root:

```bash
colima start
COPYFILE_DISABLE=1 tar --format=ustar --exclude=__pycache__ --exclude=.pytest_cache \
  -cf - scripts/run_integration.py tests/integration | \
  docker build -f tests/integration/utils/Dockerfile -t ug-integration -

# Reuse the same e2e variables. Credentials are passed at runtime, never built
# into the image. The named volume keeps results after the container exits.
docker volume create ug-integration-results
docker run --rm --init \
  -e UCODE_TEST_WORKSPACE -e DATABRICKS_BEARER \
  -v ug-integration-results:/results \
  ug-integration \
  --ug-version YOUR_RELEASE_VERSION \
  --claude-version 2.1.268 --codex-version 0.154.0 \
  --installation-only
```

Use a new results volume for each run, or pass a new `--output /results/NAME`.
To test a checkout, build a wheel on the host (`uv build --wheel`), mount the
wheel directory read-only, and pass `--ug-wheel /wheels/FILE.whl` instead of a
release version. The image deliberately contains no source checkout or host
agent configuration. Record the built image digest when sharing a reproduction;
native runs also depend on the host's OS and toolchain.
The explicit build archive includes only the runner and integration files, even
with legacy Docker builders that ignore per-Dockerfile ignore rules. It also
omits macOS extended attributes that Linux cannot unpack.

### Complete local checkout run on the Databricks network

Run from the checkout root in Bash on a clean Ubuntu 22.04 machine/VM with
Python 3.12, uv 0.9.8, Node 22.19.0/npm, Databricks CLI 1.9.0, `sudo`, and
`bubblewrap`. Install bubblewrap with `sudo apt-get install bubblewrap`. Verify
the normal sandbox before starting:

```bash
bwrap --ro-bind / / --unshare-user --proc /proc --dev /dev /usr/bin/true
```

On macOS, Lima can supply a separate VM with no host mounts:

```bash
limactl start --name ug-integration --plain --cpus 2 --memory 4 --disk 12 \
  --yes template:ubuntu-22.04
limactl shell ug-integration
```

Install the prerequisites and copy/clone the checkout inside that VM. On recent
Apple Silicon, the original Ubuntu 22.04 ARM kernel can crash `cryptography`
with `Illegal instruction` before ug starts. Install Ubuntu's supported
`linux-generic-hwe-22.04` package and restart the VM. Do not work around it by
altering Python dependencies or weakening tests. Linux/ARM is not an exact
replay of GitHub's Linux/AMD64 environment; the run records its actual platform.

Use your authorized login and explicitly selected profile; never download CI
secrets. The runner builds the checkout wheel and isolates the installed agents,
ug, test dependencies, and per-case homes:

```bash
set -euo pipefail
integration_profile=eng-ml-inference-team-us-east-1
integration_workspace=https://eng-ml-inference-team-us-east-1.cloud.databricks.com
integration_index=https://pypi-proxy.cloud.databricks.com/simple
integration_registry=https://npm-proxy.cloud.databricks.com/

# Keep this setting on both login and token retrieval when using plaintext storage.
export DATABRICKS_AUTH_STORAGE=plaintext
databricks auth login --host "$integration_workspace" --profile "$integration_profile"
export DATABRICKS_BEARER
DATABRICKS_BEARER=$(databricks auth token --host "$integration_workspace" \
  --profile "$integration_profile" --output json | jq -er '.access_token | select(length > 0)')
uv run --no-project --python 3.12 python scripts/run_integration.py \
  --python 3.12 --ug-version checkout --workspace "$integration_workspace" \
  --default-index "$integration_index" --npm-registry "$integration_registry" \
  --claude-version 2.1.268 --codex-version 0.154.0 -- -m live
unset DATABRICKS_BEARER
```

This runs all 80 live cases. For the seven installation checks, run the same
runner/version/index arguments with `--installation-only` and omit `-- -m live`;
no bearer or workspace is needed. Results remain under `.integration-runs/`.
Each invocation needs a new output directory; an existing one is rejected.
The runner returns nonzero on installation or test failure.

Do not drop the mirror flags if public registries resolve to `127.0.0.1` or return
`ECONNREFUSED`. The runner deliberately ignores host `.npmrc` and resolver settings.
Outside the Databricks network, use reachable package indexes explicitly instead.
