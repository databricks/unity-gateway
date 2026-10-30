# Integration tests

Native mod component tests in `../native_claude/test_mod.py` cover a lone call's decision under
its Agent row, parallel calls as cards above the parent's next reply in call order, calls
still routing waiting for a later reply, distinct
IDs for identical tasks, pending row and turn-spinner routing timers, and disabled/failed routing. These are not live
parallel-agent TUI or gateway checks.

`../test_claude_launch_settings.py` covers oversized Windows Claude launch
settings with controlled process boundaries: UTF-16 command length, merged
settings, literal prompts, temporary-file lifetime and cleanup, launch failures,
proxy cleanup, and simultaneous launches. Real Python children verify that the
settings file can be read for large catalogs and non-BMP prompts. Short Windows
commands and POSIX argv are preserved. This does not establish a live Windows
Claude/Bedrock pass; `test_ug_claude_headless_allow_all_bedrock_provider` remains
the existing real-provider journey in the full Claude Windows lane.

The spend-status client's workspace authentication, optional percentage handling,
pagination, validation, and failure propagation are covered by component tests in
`../test_budget.py`, importing the client from `src/ucode/budget.py`.
Account-authentication checks are replaced by assertions that
the client reuses the workspace token without starting CLI auth. These checks do not
establish live workspace, sign-in, or CLI output coverage.
Client component tests also preserve unknown nonempty scope/period strings without
losing known limits, and reject malformed enum types and invalid/negative/nonfinite
amounts. Zero and fractional decimal strings remain valid. Explicit nulls in
percent consumed, spend, and threshold are treated like missing optional fields,
individually or together, without discarding valid statuses. Redundant client error
variants are reduced to one opaque passthrough case; later-page failure checks remain.

MCP/Skills browsing has component coverage in `tests/test_catalog_browsing.py`
(repository-relative path): paginated BASIC metadata, streamed rows, missing metadata,
UTC dates, keyboard selection/filtering/cancellation, loader cancellation, and rendered
80×24/40×12 table rendering and detail scrolling. Status coverage distinguishes selection
from configuration/installation, protects read-only managed MCP rows, and checks Skills
attribution against source identity, target directories, and surviving files. These checks
do not establish live workspace discovery or agent invocation coverage.

Claude answer delivery has offline transcript and subprocess coverage in
`../test_claude_answer_delivery.py`, including stdout preservation for text, JSON and
streaming JSON when UG redirects diagnostics to stderr. Live model/gateway selection
and capability-hook activation are not covered here; interactive selection is unsupported.

Claude Desktop has installed-CLI lifecycle and subscription-relay coverage on Linux and
Windows. The lifecycle journey configures twice and reverts against an isolated native profile
path, preserves an unrelated profile and metadata, then runs the exact generated credential
helper and validates its token against the paginated live models API. It requires system.ai
Claude models without an MPS override and uses the profile's credentials and headers for a small
Messages request on `system.ai.claude-haiku-4-5`, requiring HTTP 200, a nonempty response model ID,
and nonempty assistant text without matching its wording. The service may return a provider model
ID rather than its alias; this does not pin or prove its backing model revision. The relay journey selects
the real subscription provider through configure and completes
Messages inference using the subscription token and separate Databricks header returned by its
helper. Non-relay MPS Desktop configuration is asserted in CUJ2's existing MPS configuration
journey; no dedicated Desktop MPS integration test is needed.

Each of these two journeys supplies pre-existing Desktop account directories as inputs and
requires real Claude CLI installation of the Cowork plugin. It registers system.ai.web_search through
`ug mcp add --names system.ai.web_search`, then runs the command from the installed plugin with the
MCP SDK. The check initializes the protocol, discovers web_search, and searches for Databricks'
official website. A successful result must contain a URL on databricks.com or www.databricks.com;
the response wording and search rankings are not asserted. No SQL compute is needed.
Both journeys verify reconfiguration preserves the profile/plugin and revert removes UG's entries
while retaining unrelated profiles and metadata. A separate local personal plugin is installed
through the real Claude CLI before configuration; its registry entry and installed manifest must
survive installation, reconfiguration, and revert unchanged. Revert also removes UG's marketplace
registration and generated source directory.

The existing Linux/Windows **Full journeys · Claude** jobs run both complete journeys against
the same workspace as their model calls. The workspace must expose system.ai.web_search and allow
the test identity to invoke it. No dedicated Desktop MCP test, separate fixture-workspace step,
or extra credentials are required.

These tests do not launch Desktop or establish GUI, skill execution, browser consent,
token renewal, or injected authentication-failure/reconnect recovery. CI supplies the supported
bearer override. See the [native testing plan](../../docs/claude-desktop-integration-testing.md)
for those remaining checks. The standalone test runtime pins the MCP SDK used for protocol calls;
it stays separate from the installed UG runtime and its dependencies.

Codex desktop has managed-fixture coverage in `test_ug_configure_codex_desktop.py`. It
configures twice beside user-owned `~/.codex/config.toml` settings and checks the stable catalog
pointer and an unchanged repeat configure with no restart notice; a second case keeps a user-owned
catalog pointer through configure and revert. Revert of ug's pointer is covered by
`test_ug_codex_managed_model_discovery.py`. It does not cover desktop tracing (its systemd/launchd
service needs a user session), the GUI, or inference.

The separate [dedicated-workspace CUJ](../e2e_cuj/AGENTS.md) lives in `tests/e2e_cuj/`
and runs directly with pytest. It does not use this suite's runner or config fixtures.
It reuses the session, terminal, file-task, and transcript helpers in `utils/`.
Its Claude/Codex evidence helpers keep scenario-specific assertions separate from shared mechanics.
Workspace config/catalog reads use its base class's Databricks SDK client. Configuration
is read-only and checked for changes at teardown; concurrent readers need no reservation.
CUJ2 adds three separately collected cases for exact published MPS/MCP configuration, Codex
inference, and Claude inference. Both inference cases ask for a short greeting and require a
completed parent turn matching the submitted prompt with a nonempty answer, plus the expected
provider/model and paired HTTP 200. Random IDs correlate requests; answers need not repeat them.
File, skill, MCP and subagent tasks retain their expected-value checks.
The config equality also accounts for the workspace's fixture
skill name as data; skill download and invocation are not covered.

The dedicated smart-routing CUJ in `../e2e_cuj/test_cuj4_smart_routing.py` runs the original
managed-default and explicit-model cases, with explicit-model bypass assertions enabled for
both Claude and Codex. Two parameterized tests run all six
`SMART_ROUTER_CONFIG_VERSION` values: one per agent checking first-prompt routing, orchestrator
context in inference input, and a completed explicitly requested native subagent, and a
Claude-only one checking the status band and hint row at launch and across off/on toggles.
This is not automatic orchestrator-delegation coverage. No workspace configuration is modified.
CUJ4 matches both Codex 0.154.0's top-level tools and 0.162.1's Responses Lite
`additional_tools` input items. Encrypted child messages match the routed task name
to the recipient; plaintext prompts still match exactly. Both require tools.
The shared preset CUJ runs both agents for every version and derives routing and
orchestration assertions from each agent's resolved flags. Both must complete a native
child task. `subagent_orch_v0_claude_only` routes Claude's child; Codex uses the managed
default without routing requests, new router decisions, recipe headers, or orchestrator context.

Agent-specific smart-routing preset component checks cover shared base presets, explicit agent mappings,
complete per-agent validation, session precedence, and preserving the preset selector
until managed-default agent selection.
These checks do not establish live agent coverage.

The shared request recorder and native compatibility-retry checks live in
`utils/request_recorder.py`. The CUJ adapter keeps its existing
configuration glue and imports those helpers; request forwarding, header
redaction, and paired response assertions retain their behavior.

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
agent-compatible pickers (including GPT Luna for both agents), schema exclusions, configured
defaults, and real inference, including an explicit Claude headless task on GPT Luna.
Claude's decoy catalog includes both decoys while the scoped picker excludes both.
CI collects it through the shared `dedicated-cuj` job. Its request checks accept the known
Claude thinking-display 400 only with a successful, otherwise identical native retry.

The two Claude Bedrock journeys use `us.anthropic.claude-sonnet-4-6`. The configure
journey selects it from the restricted `main.ucode.ci_bedrock_mason_filtered_models`
service, while the headless journey pins it through
`main.ucode.ci_bedrock_mason_all_models`.

This suite runs the **installed product** through subprocesses, against the same
`UCODE_TEST_WORKSPACE` used by the existing e2e tests. It does not import `ucode`,
patch application functions, substitute agent executables, run a fake gateway,
or construct ug state files. The normal test suite checks these boundaries.

The Claude mod entry point has separate native component checks in
`../native_claude/test_mod.py`. Claude 2.1.290 validates the module and runs its
TypeScript event-forwarding and launch-option tests in a dedicated CI job. The
existing Claude skill-toggle journeys check the live recipe payload field (launch recipe at launch and after off/on, DISABLED after off).
The required Isaac feedback form, evidence submission, and background delivery
notifications need a separate manual Isaac check. The gateway toggle journey does
not cover the form or notification timing. Use the released Isaac skill with the
existing capture/retry CLI to check native category selection and retries,
immediate form dismissal without model requests, and completion during another task.
Verify the original task stays selected when a new prompt precedes capture, and
routing settings stay fixed across subsequent toggles. Check immutable saved
evidence, upload permission denials, local previews, and text-only Other reports.
In a session launched with routing, check that `/isaac-feedback` remains asynchronous
after routing is turned off. A session launched without routing should use the
existing feedback skill without loading the router plugin.

The existing unit tests keep their fixtures. Integration has an independent
pytest configuration and uses `--confcutdir` so those fixtures cannot leak in.
It is not collected by the default `uv run pytest` command.
The Claude subagent skill-toggle journeys open `/tasks` after its final calculation
and waits up to 180 seconds for "No tasks currently running" or a native menu containing only
completed task rows before submitting `/exit`. Claude 2.1.290 retains finished children in a
`Completed (N)` list, so the empty-state message is not required when the list has exactly N
checkmarked `done` rows and no running or scheduled section. The helper observes menu dismissal
after Escape before typing `/exit`. Offline PTY checks reject active/scheduled tasks, partial
lists, and completed-task text outside the native menu.
It neither stops tasks nor confirms an exit dialog. Process exit retains its 30-second
timeout. Offline PTY checks cover this ordering.
The shared wait also runs before CUJ4's Claude delegation sessions exit, after delegated-task
evidence is checked, because Claude can resume a child after its first completed answer.

`../test_startup_imports.py` covers the bare version fast path, deferred imports,
metadata fallback, and other-argument dispatch as component checks. Installation
checks still exercise real console-script help and version output; neither suite
asserts end-to-end agent startup latency. The component suite also checks that
version output alone cannot establish CLI health when startup dependencies are
absent; use `--help` for the startup-import probe.

Agent-specific bundled instruction selection and prompt/compaction hook content
are covered in `../test_skills.py`; those component checks do not run live agents.

Custom OAuth startup with an expired ordinary profile has no automated integration
coverage. Validate Claude's default, model-location, and provider catalogs in Isaac
with the custom OAuth profile.

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

Codex covers `--model VALUE`, `--model=VALUE`, and `-m VALUE` before ug's separator
and within native `exec`, asserting the completed turn's selected model. Codex has
no UG-owned `--model` option: all three spellings are forwarded to Codex in either position.

The `smart_defaults` wire schema, legacy `spend_tiers` cache reads, and recommendation
request gating are covered by unit/component tests listed in `../README.md`. This suite
does not yet assert live `recommendModel` request counts for configs with and without tiers.

## Dedicated-workspace CUJ5

[`../e2e_cuj/test_cuj5_budget_defaults.py`](../e2e_cuj/test_cuj5_budget_defaults.py) runs
four budget cases in the shared `E2E CUJs` job against the fixed 1% tier. The above-tier
cases use `UG_CUJ_SP_CLIENT_ID` / `UG_CUJ_SP_CLIENT_SECRET`; the below-tier case uses
`UG_BUDGET_CUJ_SP_CLIENT_ID` / `UG_BUDGET_CUJ_SP_CLIENT_SECRET`.

Run from a clean POSIX runner with both credential pairs set:

```bash
UG_CUJ_SP_CLIENT_ID=... UG_CUJ_SP_CLIENT_SECRET=... \
UG_BUDGET_CUJ_SP_CLIENT_ID=... UG_BUDGET_CUJ_SP_CLIENT_SECRET=... \
uv run --with pexpect==4.9.0 --with pyte==0.8.2 \
  pytest --confcutdir=tests/e2e_cuj tests/e2e_cuj -v
```

All four cases submit no inference tasks. The above-tier bare-launch case checks
`ug usage --all` against live budget figures, the Luna recommendation, and Codex
startup; it does not assert that Codex applies Luna. The below-tier case verifies
Claude/Sonnet selection and checks the spend percentage when the backend supplies
budget figures.

The explicit `ug claude` case verifies that startup displays the backend's Codex/Luna
recommendation while its generated `ANTHROPIC_MODEL` setting and native header select
Sonnet, then exits. It does not verify Claude's inference model.

The explicit `ug claude --model <Sonnet id>` case runs `ug configure` in a terminal, so it writes the
OS-managed file through the runner's passwordless sudo. It checks that the launch still displays the
Codex/Luna recommendation and that Claude's native header selects Sonnet. It also requires
`~/.claude/ucode-settings.json` and `/etc/claude-code/managed-settings.json` to stay
byte-identical to what `ug configure` wrote, with Sonnet as `ANTHROPIC_MODEL`. A following plain
`ug claude` must start on Sonnet without rewriting either file. The workspace publishes Sonnet as
its only Claude model, so the explicit model equals the admin default: this proves an explicit
`--model` writes no file, not that it switches models. That switch is covered by CUJ 1, without a
budget. A Claude budget pick that differs from the admin default, alone or with smart routing,
has unit coverage in `../test_agent_claude.py` and `../test_cli.py` only: no CUJ workspace
publishes such a tier. This case also covers startup only, not inference.
All four cases leave workspace and budget configuration unchanged.

`../test_usage.py`, `../test_budget.py`, and `../test_cli.py` cover top-five selection
by usage percentage, `--all` without an opt-in, short IDs on every entry,
resource-name ID extraction, unavailable usage, and errors.
CUJ5 compares a known personal limit's displayed spend, threshold, and percentage
with live values before and after `ug usage --all`.

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

CLI token memoization and capability-gated `--no-browser` re-auth are covered by
component tests in `../test_databricks.py` and `../test_mcp_web_search.py`, including
wall/monotonic expiry across suspend or clock rollback, forced refresh, concurrent
fetches, login invalidation, confirmed credential-error caching, and transient-failure
recovery. Connection-login component tests also check invalidation on every login
attempt and fresh tokens on async proxy retries. This suite
does not assert live token-fetch counts, stale-profile recovery, or launch-time
savings; credential/profile routing is unchanged.

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

Copilot CLI has two live headless journeys in `test_ug_copilot_headless.py`, one per
wire API (Anthropic Messages for Claude, Responses for GPT). Both go through the
session's loopback proxy and check that no file under the session home holds the
Databricks credential. `test_ug_copilot_lifecycle.py` signs in through
`DATABRICKS_BEARER_COMMAND` (`utils/pat_bearer.py` mints 15-minute PATs) and has the agent
revoke them mid-task: once to require a refresh, once with sign-out to require the
sign-in-expired message. It also covers configure/revert, including upgrading from ug v0.2.0, which saved the bearer. Refresh near natural expiry is
covered by the hermetic proxy tests in `tests/test_gateway_proxy_integration.py`.
`test_ug_copilot_models.py` covers the per-session model registry: one setup runs a
Claude task on a model older than its family's newest (only the full catalog registers it)
and then a GPT task chosen with Copilot's `dbx-responses/<model>` form, an unknown model
fails before Copilot starts,
and the TUI switches from Claude to GPT with `/model` mid-session. Model evidence comes
from Copilot's own `~/.copilot/session-state/*/events.jsonl` (`utils/agents/copilot.py`).
`test_ug_copilot_model_location.py` launches Copilot from fresh state with `--model-location` on the
model-discovery schema and completes a file task on its Claude-compatible model service.
`test_ug_copilot_providers.py` runs Copilot through the MPS fixtures below: a file task on the
Anthropic service's model, on the OpenAI, Azure OpenAI and Microsoft Foundry services' `gpt-5-nano`, and
on a named model of the Bedrock allow-all service; launching on the relayed service fails before Copilot starts.

Antigravity CLI has five live headless journeys in `test_ug_antigravity_headless.py` and three in
`test_ug_configure_antigravity.py`. Headless runs use `agy -p --output-format stream-json`; the
file task requires a finished `read_file` step on the fixture and the value in the `result`
event, every logged model request on ug's loopback proxy (`/ai-gateway/gemini/v1beta/models/`)
with gateway authentication, and no Databricks credential on disk. The explicit-model case picks
a second discovered `system.ai.gemini-*` model and requires every request to name its
Antigravity name (`gemini-3.8-flash`), launched through the `ug agy` alias; that is the agent's
request, not the gateway's destination. Two journeys use the Gemini Enterprise MPS below: one
launches `--provider` with `--model` for each listed target and requires that launch's requests to
name it; the other picks External Models in `ug configure`, answers the start-model prompt an
allow-all service gets (it declares no targets), and completes a bare launch on that model. The MCP case registers `system.ai.dbsql` with `ug mcp add` and requires a finished
`call_mcp_tool` step and the query's `md5` in the answer. The skill case places a user skill in
`~/.agents/skills` and requires its code word. Headless Antigravity cannot answer permission
prompts, so those two cases start from the user's own `permissions.allow` rules
(`mcp(system-ai-dbsql/*)`, `read_file(*)`), which ug must leave unchanged. The TUI journey picks
Antigravity in `ug configure`'s agent picker, answers first-run color-scheme, terms, and folder
trust screens through visible choices, and reads the answer from a finished agent-response step
(type 15 on 1.4.9) in Antigravity's SQLite conversation store (`utils/agents/antigravity.py`); the
revert journey checks the user's own skills.json (including an entry added after configure) and
MCP server survive `ug revert`. Not covered: UC skill download into the declared directory, a
TUI `/model` switch, and Windows (no Antigravity gateway build).

The managed saved-config journey installs `/etc/unitygateway/managed-settings.json` through real
configure, checks root ownership and readable modes, and checks the user-owned
`~/.ucode/refresh_ts.json` record has a `published` outcome and no timestamp. Two launches then
replay the saved config without changing that record or the installed policy bytes, inode, mtime,
or ctime. Unit/component tests
cover configure-only elevation, non-root read-only launches, IT guidance, removal, and
metadata-only drift. Root/non-root packaged-writer checks in a disposable Linux container are
component evidence, not live MDM provisioning or Claude/Codex launch evidence. Running the full MDM
bootstrap and agent journey still requires explicitly selected workspace provisioning credentials.
The foreign-workspace journey configures through the public CLI, then verifies that bare ug,
Claude, and Codex reject a different `--workspace` with dry-run/skip-preflight without changing
machine policy, user state, or refresh metadata. It uses the `managed_workspace_default` fixture and is implemented but not locally run; unit
regressions cover workspace-authoritative refresh and pending-hash warning suppression.
The credential-free disposable-container harness passes real installed-CLI mismatch rejection
as UID 10001; it does not replace the unrun real-workspace configure journey.

## Shared agent helpers

`utils/agents/claude.py` and `utils/agents/codex.py` own the existing native answer
parsing, session paths, and child-session detection. `utils/evidence.py` dispatches
through them without changing existing journey commands or completion assertions.
Claude child answers include sidechain `SubagentHandback` messages, which newer
Claude versions use instead of final text blocks; parent handbacks remain excluded.

## Run a specific combination

Prerequisites: Python 3.12+, uv, and Node/npm. Live runs also require Databricks
CLI 1.17.0. Windows drives TUIs through ConPTY (pywinpty) and supports the explicit
headless subset described below. The runner installs the requested agents into a new
npm prefix and ug into a new virtualenv. Pytest, the recorder's pinned `httpx`, and,
for live runs, the PTY/screen libraries (pexpect and pyte) live in a different
virtualenv, so they cannot accidentally
supply a missing application dependency. No packages are installed into your
existing agent installations or checkout's `.venv`.
CI pins Databricks CLI 1.17.0 in the live and managed integration lanes. The
runner's isolated `PATH` exposes that selected CLI, so skills journeys meet ug's
CLI minimum without falling back to another version installed on the machine.
Native live runs refuse existing machine-wide Unity Gateway/Claude/Codex configuration, which
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
Copilot CLI is opt-in the same way: pass `--copilot-version` (for example `1.0.91`).
Antigravity CLI is not on npm: pass `--antigravity-build` with the exact `<version>-<build id>`
from Google's release manifest (for example `1.4.9-4962164969177088`). The runner downloads that
archive for the host platform into the run directory, records its URL and SHA-512 in
`versions.json`, and sets `AGY_CLI_DISABLE_AUTO_UPDATE=true` in the runner and isolated test
sessions. Antigravity requires the literal `true`; `1` leaves background updates enabled
([upstream documentation](https://antigravity.google/docs/cli/troubleshooting/#resolve-self-updater-locks-and-failures)).
The runner still fails if an agent's version changes during the suite.
`../test_integration_contract.py` checks that isolated sessions use this documented value,
including when the caller supplied `1`; this component check does not run the native updater.

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

Desktop credential-helper coverage remains in `../test_desktop_auth.py`,
`../test_desktop_auth_cli.py`, and `../test_file_lock_cross_os.py`. These component
checks cover token-only stdout, explicit workspace/profile arguments in plain and
ANSI-colored parser errors, browser gating, cancellation, and bounded lock/login
waits. Recovery checks also cover token recheck before browser login, lock release,
the shared deadline, and network/CLI failures at each recovery stage. Invalid token
output stays out of helper errors and streams.
Shared CLI discovery checks pin the newest absolute binary through recovery and
reject a missing CLI before token fetch or browser login.
They replace external authentication and do not establish native Desktop
GUI or browser-login coverage; this integration suite does not launch Desktop.

`../test_claude_desktop.py` exercises real temporary profile, shared metadata,
and previous-selection files. Coverage includes the fixed reserved UUID, complete
replacement without ownership records (including malformed profiles), repeat configure,
workspace/profile switches, deleted-profile recovery, labels, and preservation of
unrelated profiles and metadata. Revert deletes the reserved profile even after edits
or without selection state. Selection tests restore a still-existing previous profile
or preserve a later unrelated selection without leaving a dangling appliedId.

Injected failures at every profile, metadata, and selection-state write boundary verify
exact-byte rollback, including malformed pre-existing UG profiles. Concurrency checks
reject changed snapshots. Desktop-only lock-timeout tests preserve files and close
unacquired handles. Paths use temporary homes across simulated macOS, Windows, and Linux,
including relocated/default Windows Local AppData, missing apps, invalid paths, creation
failures, and parent-metadata preservation.

Rendering tests cover the exact profile fields without `_comment`, complete model IDs,
order/duplicates, literal helper arguments, and copied headers. Omitted optional fields
are removed on replacement. Shared managed-header parsing retains its pre-Desktop behavior;
the extra header-validation tests were removed with that out-of-scope parser change.
The default-model warning assertion was removed because that limitation is documented
instead of returned as a runtime warning.
Obsolete baseline restoration, edit preservation, ownership/migration, and workspace
canonicalization tests are replaced by reserved-file replacement/deletion coverage.
Reconfigure tests remove the legacy `_comment` field while preserving unrelated profiles,
metadata, and previous-selection state, and verify subsequent configure is a no-op.
Existing CLI URL normalization is unchanged. Native Desktop activation,
GUI/helper/browser behavior, and inference remain untested by these component checks;
they do not establish native Windows or Linux support.

`../test_file_lock_cross_os.py` tests the shared timeout mechanism used by Desktop:
real contention, preserving the holder on timeout, and successful acquisition after
release. New lock-wait limits are scoped to Desktop profile updates; existing
non-Desktop callers retain their prior behavior. These are component checks, not
native browser-consent coverage.

`../test_desktop_setup.py` separately exercises configure/refresh/recovery,
successful setup without the default-model warning, recoverable setup failures that still warn,
dry-run, and revert using native path resolution under temporary homes on simulated macOS,
Windows, and Linux, without replacing the path resolver, including
host-native absolute helper paths with spaces and missing-app checks before
authentication/discovery. Revert without a UG profile preserves existing files.
These checks do not launch the Desktop GUI or its credential helper.
They also cover CLI profile labels, workspace switches with a fixed UUID, and
configure/revert warnings that preserve profile and selection-state files on lock timeout.
An existing reserved UUID without records is replaced on configure and deleted on revert;
these remain filesystem component checks.
CLI revert clears workspace state even when Desktop cleanup fails. A component
regression invokes the Typer CLI and checks that a lock failure returns exit code 1,
emits one warning and a failed Desktop summary, and preserves profile and selection
files. Retry restores the previous selection without workspace state and returns 0;
a repeated revert also returns 0. This does not establish native Desktop coverage.

`../test_cowork_plugin.py` covers Cowork-root discovery, generated plugin contents,
scoped Claude plugin commands, install/update selection, PAT/custom-OAuth behavior, and
owned-file cleanup. Alias checks cover short UC names, unchanged proxy routing, custom-name
preservation, order-independent collisions, and repeated replacement of old keys.
Manifest checks require the `ug` name and display name in both manifests.
Legacy-registration component checks cover replacement after successful install/update,
preservation on install failure, scoped cleanup, and unrelated-plugin preservation.
`../test_desktop_configure_cli.py` covers managed orchestration and
cleanup. Both are mocked component coverage; the integration suite does not launch Cowork,
connect a plugin MCP server, discover its deferred tool, or run a task through it.

`../test_mcp_proxy.py` covers persistent OAuth U2M authentication-tool discovery, explicit
reauthentication after success/cancellation, refresh and task retry over in-memory MCP
streams, and renewed discovery-auth failures. Failed metadata lookups remain retryable, and
discovery and unrelated replies continue during interactive login. Metadata/pagination and
upstream tool names are preserved. A fresh listing that removes upstream `authenticate`
restores local recovery, while continuation pages preserve ownership from earlier pages.
PAT/non-U2M discovery, permission errors, and policy URL
elicitation stay unchanged. `../test_databricks.py` returns `False` for unsupported services
and failed requests; later operations look up metadata again. `../test_cowork_plugin.py` also checks generated
sign-in/retry guidance. These component tests replace external login and do not establish
native Cowork browser recovery.
Generated-guidance checks include one sign-in attempt for access/IP denials and stopping
authentication retries if the denial persists. They verify the instructions, not model compliance.
The separately selected `../connector_behavior/` suite tests model compliance using a real Claude
model and synthetic Slack tools: available tools despite a stale notice, tools appearing after
an actual wait, and tools absent through the wait limit. It does not establish native Desktop
sleep/resume, discovery, or proxy reconnection coverage. Run instructions and explicit
workspace/profile/model requirements are in `../README.md`.
Gmail/Calendar component regressions cover service-scoped login URLs in failed tool results
and structured login elicitations: automatic sign-in, one request replay, cancellation,
persistent failures, and unrelated replies during login. Direct URL-matcher tests reject
other services/hosts and malformed URLs, with representative bridge pass-through cases for
both error forms. Success results, unsupported services, failed
metadata lookups, PAT profiles, and policy or mixed challenges stay unchanged.
Discovery login elicitations expose `authenticate` without starting login. No live signed-out
Gmail/Calendar operation or native browser flow was exercised by these component tests.
HTTP 401 component coverage verifies no connector login during initialization, discovery,
notifications, or background requests. Actual tool-call 401s can open connector login;
PAT mode, malformed requests, and other HTTP failures do not open it. Async HTTP-client
cases replace only external network/login boundaries and verify identical request bodies.
They do not establish native logout/relogin behavior.

`../test_mcp_connection_login.py` substitutes browser, HTTP, and clock boundaries to check
workspace sign-in URL construction and polling the selected CLI user's credential. Pending,
FAILED, and 404 states keep waiting; transient errors can recover; denied/malformed states,
workspace-token failures, and timeout fail. A blocked browser retains the manual URL.
Success requires ACTIVE; browser launch alone is insufficient. Proxy coverage combines
this browser flow and replay, preserving the profile and token
memo. These are component tests; live provider sign-in, popup behavior, and native
Desktop/Cowork recovery remain unverified.

`../test_cowork_skills.py` adds component coverage for user-downloaded skills reaching
the current workspace's plugin and refreshing on named, schema, and picker add/update/remove
commands. It preserves managed skills and excludes project-local, other-workspace, and
user-authored skills. These checks substitute skill APIs and the native plugin CLI;
the integration suite does not validate native Cowork skill discovery or invocation.

Model-source tests require Desktop-only, case-insensitive `\bclaude-` filtering
instead of copying every catalog entry. Mixed static/discovered lists exclude GPT/OSS
aliases and lookalikes while preserving Claude IDs, order, duplicates, and source lists.
Empty filtered lists warn and preserve all existing profile, metadata, and selection-state
files, or leave fresh installs untouched. Tests cover modern, Bedrock, Vertex, and custom
Claude-named routes; they do not verify the model behind a name.

Model-label component tests cover catalog display names in `labelOverride`, unchanged
routing IDs, order/duplicates, and ID-only entries for missing/empty names. Static
managed lists use discovery only to label matching IDs; their model selection stays
authoritative. Repeat configure refreshes or removes labels. Source lists/maps remain
unchanged.
These checks use temporary files on simulated platforms, not the native model picker.

Desktop OTEL coverage remains in `../test_claude_desktop.py` and
`../test_desktop_setup.py`: native settings follow Claude's tracing flag for both
model sources on simulated macOS, Windows, and Linux. Component checks cover the
collector base URL, protobuf protocol, refreshing `.sh`/`.cmd` helpers without
persisted tokens, quoting, repeated native helper execution with rotated bearer
overrides, Unicode and trailing-backslash arguments, Windows UTF-8 selection,
JSON-only stdout/failures, repeat configure, workspace changes,
disable/re-enable, helper-byte/permission rollback, and revert preserving unrelated
telemetry. Revert cleanup also covers a removed native library or application-data
root without recreating Desktop directories, plus rollback and retry after cleanup
failure. Byte-writer component cases check explicit permissions independent of
contents, unchanged JSON formatting, helper execute-permission repair, and
atomic-replacement failure cleanup. This suite does not launch
Desktop or establish native export, open-session token refresh, or trace ingestion.
See `../../docs/claude-desktop.md` for native validation steps and auth limits.

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
test_ug_copilot_headless.py             # Copilot via the session proxy: Claude + GPT tasks, no token on disk
test_ug_copilot_lifecycle.py            # Copilot refresh after revocation, sign-in expired, configure/revert, upgrade
test_ug_copilot_models.py               # Copilot model registry: Claude then GPT, unknown model, TUI /model switch
test_ug_copilot_providers.py            # Copilot via Model Provider Services: Anthropic, OpenAI, Azure OpenAI, Foundry, Bedrock allow-all; relayed refused
test_ug_copilot_model_location.py       # Copilot on a Unity Catalog model location: fresh --model-location file task
test_ug_antigravity_headless.py         # Antigravity via ug's proxy: file task, --model (ug agy), Gemini MPS targets, MCP SQL tool, skill dir
test_ug_configure_antigravity.py        # Antigravity: configure picker + TUI task + reopen; External Models picker; revert keeps user files
test_ug_codex_tracing.py                # Codex OTLP export reaches the configured trace table
test_ug_claude_commands.py              # command help forwarding
test_ug_codex_commands.py               # command help and parser error forwarding
test_ug_codex_app_server.py             # actual client/server initialize exchange
test_ug_smart_routing_hooks.py           # live hook contract plus skill-driven subagent toggles
test_ug_configure_claude_lifecycle.py   # repeat setup, revert, rejected credentials
test_ug_configure_claude_desktop.py     # Desktop lifecycle, system.ai/relay inference, installed Cowork MCP; no GUI
test_ug_configure_codex_desktop.py      # stubbed managed Codex desktop catalog pointer, no GUI
test_ug_configure_claude_workspace_switch.py # real skills MCP cleanup across two workspaces
test_ug_configure_codex_lifecycle.py    # repeat setup, revert, rejected credentials
test_ug_claude_managed_model_discovery.py # Claude MPS policy cases (claude_mps.json)
test_ug_codex_managed_model_discovery.py  # Codex MPS policy cases (codex_mps.json)
test_ug_claude_model_discovery.py       # unmanaged scenarios 9, 11, 13
test_ug_codex_model_discovery.py        # unmanaged scenarios 10, 12, 14
test_ug_configure_managed.py            # stubbed static model list/catalog pointer, no agent selector, saved-config replay
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
Interactive first-prompt routing through `first_prompt_and_subagent_no_orch_v0` is covered by
the managed_fixture smart-routing banner journeys below for both agents. The subagent routing command journeys drive the real
installed Claude mod command and Codex hook with native payloads against the live router. The
subagent-only TUI journeys additionally spawn three native children in one real session,
invoke the installed Smart Router skill to turn routing off and back on, and require the
native model/rationale row (Claude) or subagent banner (Codex), plus a correlated
live decision only while routing is enabled. Plugin-refresh survival and native
daemon/background dispatch remain deferred. CUJ4 covers interactive explicit-model
bypass. Plugin generation and launch arguments are covered
by component tests in `../test_claude_smart_routing_v2.py`, not by a live registration journey.

Exact candidate-model and router-name preservation in decision logs has component
coverage in `../test_claude_smart_routing_v2.py`; this suite does not assert those fields.

PATH conflicts for the Smart Router skill have subprocess/component coverage in
`../test_smart_router.py`: the skill uses the launching interpreter despite a different
`ug` first in PATH. The live journeys above do not inject a second installation or
establish PowerShell command execution.

The toggle journeys retain the legacy routing-only and orchestration cases and add each of
the three subagent-only `SMART_ROUTER_CONFIG_VERSION` presets. They require only `smart-router`
for routing-only cases, and both `smart-router-orchestrator` and `smart-router` when orchestration
is enabled. Claude checks each local mod confirmation and the recorded `smart_router_recipe_name`
on the following calculation's real gateway requests. Codex verifies saved session controls,
a new CLI confirmation in native tool-result records, and a new assistant answer after each
skill invocation.
Collapsed terminal output is allowed; the answer need not repeat the CLI's exact wording.
Each following child still verifies whether a routing decision occurred.
With orchestration enabled through legacy flags, a preset, or the managed default, both routed
Codex children must show `[orchestrator on]` in their own subagent banner. Claude shows the selected
model and rationale from each child's audited decision.
Their off-phase child is an explicit user-requested delegation;
these journeys do not establish automatic orchestration behavior. Root-only
activation, compaction, retained skills in ineligible sessions,
role-contract preservation, and isolation from legacy preference files
lack dedicated regression coverage. Codex's native hook merging, project trust,
and execution of pre-existing hooks are not exercised by this integration suite.

Preset parameterization augments only routing hooks, skill toggles, and first-prompt routing;
their original legacy-env cases remain, and the skill toggles add a managed-default case in
which `smart_routing.enabled` alone routes subagents with Smart Router Orchestrator and no
first-prompt wrapper starts. The skill toggles' legacy-env cases load the `_off` copy of the
workspace's routing fixture (`claude_smart_routing_off.json`, `codex_smart_routing_off.json`, or
`codex_smart_routing_ca_off.json`): the same models without `smart_routing`, because managed
smart routing overrides individual routing env vars.
Claude's toggle variants are temporarily non-blocking on both CI platforms; see
[Soft-fail tests](#soft-fail-tests). Their assertions and local
selection are unchanged. Codex toggles and the direct hook journeys remain required.
Explicit-model selection, command forwarding, app-server initialization, and catalog fallback
retain their original legacy flags and on/off coverage; preset-specific behavior there is not
covered.

The Claude route-subagent hook journey checks that both hook notices name the agent
in `updatedInput`, rather than the originally requested type. Native task-header
rendering is outside that hook-contract check.

The unit/component `../test_smart_routing_config.py` includes a 162-case Cartesian oracle over all
three legacy routing flags (`None`, `0`, `1`) and six selector forms (`None` plus the five
supported presets, including the internal first-prompt-and-subagent mode). It independently
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

The relayed CUJ launches Claude from fresh state through a relayed (subscription-relay)
MPS, with no `ug configure` step, and completes a file task on two models: a bare Anthropic id the subscription serves
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
- Antigravity CLI (Gemini Enterprise): `main.ucode.ci-gemini-mps`, using its `gemini-4-argon`
  and `gemini-3.8-flash` targets (`--antigravity-provider`, `--antigravity-provider-models`).
- Codex and Copilot (Azure OpenAI): `main.ucode.ci_azure_openai_mps`, using its `gpt-5-nano` model.
- Codex and Copilot (Microsoft Foundry): `main.ucode.ci_foundry_mps`, using its `gpt-5-nano` model.

Use `--claude-provider` / `--claude-relayed-provider` / `--codex-provider` to
reproduce another existing service. Use `--claude-provider-model` /
`--codex-provider-model` when it allows a different model. Those choices are recorded in `versions.json`.
No service is created or modified. A missing service, permission, or OAuth token
fails the selected CUJ, rather than skipping it.

The fresh `--provider` journeys use these preprovisioned services to check provider selection
and real agent CLI launch, without inference. Codex also checks its app-server model catalog.

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
consume but never create or modify them. Claude and Codex scoped pickers must
match the parent schema's live gateway catalog, which must include the configured parent model.

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
then Haiku preference for catalogs with multiple families. Plain Claude launches replace the
picker with the gateway catalog (saved family defaults first) but keep Claude's own starting model.
Repeated UG-owned picker refresh and preservation of unrecorded or edited admin pickers
have component coverage in `../test_managed_model_lifecycle.py` and `../test_agent_claude.py`;
these discovery journeys do not validate repeated live `/model` launches.
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
they only configure, list models, and open/close the picker. The two dedicated CUJ7 fresh-state
cases instead complete real file tasks through `--workspace` and `--model-location`, using
`ug_e2e.models.claude_haiku` and `ug_e2e.models.gpt_luna`. The two fresh `--provider`
journeys check setup and launch with existing services, without inference.

There are **88 live cases** (including 12 marked TUI journeys) and **7 installation
checks** with Claude and Codex; selecting OpenCode adds one live headless case and two `managed_fixture` cases; selecting Copilot adds seventeen live cases; selecting Antigravity adds eight live cases (one TUI). One
**`workspace_switch` case** uses two real workspaces and checks skills MCP cleanup and a completed
Claude task. A further **44 `managed_fixture` cases** (twelve of them also `live`) run on the
managed workspace with a checked-in JSON CodingAgentConfig from `tests/fixtures/managed_config/`
injected through `UCODE_MANAGED_CONFIG_STUB`; there are no cases that read a published config.
Twelve explicit configured/fresh Claude and Codex discovery and source-override journeys use the
`claude_mps.json` / `codex_mps.json` fixtures, which set each agent's source to its dedicated MPS.
The other cases cover focused model, MCP, skills, saved-config replay, foreign-workspace refusal, and lifecycle shapes, including
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
Across integration and dedicated CUJ7, the 14 numbered scenarios comprise 24 journeys:
12 managed and 12 unmanaged. Integration contains 22 of them (12 managed, 10 unmanaged);
configured unmanaged cases 7 and 8 run in `e2e_cuj/test_cuj7_model_discovery.py`.
The complete integration suite collects 130 executions (127 with Claude and Codex selected).
See the named coverage and gaps matrix in
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
The switch reports one removal from previously configured workspaces; the old
"Dropping stale entry" warning applied when the target inherited the source's MCP selection.

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
- `test-dependencies.txt`: the separately installed pytest, recorder (`httpx`), and
  terminal-tool dependencies.
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
not inside the optional Docker image. An advisory `windows-latest` job runs
the five credential-free checks in `test_installation.py` (installation, CLI,
auth-helper, and local MCP) and
uploads `integration-installation-windows` evidence. It uses `continue-on-error`
and is not part of `All integration tests` until the initial Windows issues are fixed.
It installs from public PyPI and npm, needs no credentials, and also runs on
fork PRs. Its actual OS image is recorded in `versions.json`; GitHub can update
the image behind the runner label. The two POSIX version-floor journeys are outside this Windows subset.
It installs only Claude as the runner prerequisite; the five selected checks
exercise ug and its local helpers, not either agent's inference path.
An advisory **Windows headless journey · Claude** job installs the temporary Windows-pinned
Claude version (currently 2.1.278)
on a native Windows runner from public PyPI and npm,
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

CUJ7's five journeys collect eight cases in `tests/e2e_cuj/test_cuj7_model_discovery.py`: configured
Claude picker and Codex app-server discovery, fresh Claude/Codex model-location file
tasks, and Claude managed-family-default preservation on launch. The class pins `WORKSPACE_URL` to
`https://dbc-14e376e8-6541.cloud.databricks.com`; the required
`E2E CUJs · CUJ 7 · Unmanaged model discovery` shard uses
the shared `UG_CUJ_SP_CLIENT_ID` / `UG_CUJ_SP_CLIENT_SECRET` credentials, not a workspace
secret. The workspace must publish no `CodingAgentConfig`, expose discoverable `system.ai`
models, and retain the existing models `ug_e2e.models.claude_haiku`,
`ug_e2e.models.claude_sonnet`, and `ug_e2e.models.gpt_luna`; the shared service principal
needs read/use privileges on all three.
The task journeys exercise model selection before and after ug's separator, including
Claude's UG-owned `--model` and its native option. All eight cases reuse the CUJ
session/terminal helpers and check that the workspace remains unmanaged; they never
change remote configuration. Function-scoped sessions keep every launch fresh, mint
a bearer per case, and use shared `revert_machine_wide` cleanup.
Both agents correlate the exact user task to requests for the expected UC service and
nonempty HTTP 200 responses through the shared recorder. Scoped launches also require
the `databricks-model-service-parent-schema` header to equal `ug_e2e.models`.
Claude checks structured `modelUsage`; Codex checks its completed native turn's model.
The preservation case seeds distinct Sonnet and other-family defaults only after the
clean-runner preflight, verifies Sonnet inference and unchanged defaults, then reverts
and removes that input. Recording stops before guarded revert; fixture teardown keeps
cleanup failures separate from task failures. An accidental Opus fallback cannot satisfy
the Sonnet assertion.

CUJ8's one journey (`tests/e2e_cuj/test_cuj8_second_managed_settings_writer.py`, shard
`E2E CUJs · CUJ 8 · Second managed-settings writer`) reproduces a launcher's workflow so ug
changes cannot regress launchers that also write managed settings: each of three sessions
applies a second writer's OS-managed
Claude settings (a launcher's full field set with neutral values, its own `modelPicker` and
opus/sonnet/haiku defaults, OTEL pointed at the CUJ workspace, rewritten only on drift through
`sudo mkdir`/`chattr`/`cp`/`chmod`, leaving `apiKeyHelper`, its TTL, and foreign header lines to
ug), then runs `ug claude --workspace <ws> -- <launcher's Claude args>` in a real TUI with the launcher's
launch environment, including its `subagent_orch_v0_claude_only` routing preset, opens
`/model`, and completes a file task. It requires at most two privileged writes in the first
session and none after, no ug password notice after the first session, no ug warnings or
errors, the writer's picker in `/model`, every writer field unchanged except the ones ug owns,
no discovered defaults in either settings file, the orchestrator context on the task request,
recorded inference served on the writer's Haiku default, and `ug revert` restoring the writer's
file exactly. Deviations: prompts are counted as
privileged writes because CI sudo is passwordless; auth uses the CUJ bearer instead of a
custom OAuth app; ug's default router recipe replaces a server-side one; and
`ANTHROPIC_MODEL=haiku` avoids the gateway's rejection of Claude's Opus 5.5 effort field. The
class pins `WORKSPACE_URL` to `https://dbc-6a91dc96-859b.cloud.databricks.com`, which must
publish no `CodingAgentConfig` and serve `system.ai` Claude models; it serves no Fable model,
so the fable ping-pong itself is covered by unit tests.

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
two agent jobs per OS). Full runs every other live case in two disjoint agent lanes;
it excludes the smoke cases, which smoke already ran on the same OS:

| Agent lane | Marker |
| --- | --- |
| Claude | `live and claude and not soft_fail and not smoke` |
| Codex | `live and codex and not soft_fail and not smoke` |

A non-blocking **OpenCode** job (`live and opencode`, one case) runs alongside them with
`continue-on-error` and is not part of the required `cujs` gate until it is stable.
A second non-blocking job, **Managed OpenCode self-managed** (`managed_fixture and opencode`, two cases
in `test_ug_agents_self_managed.py`), installs Claude, Codex and OpenCode and runs against the
managed e2e workspace (`E2E_ADMIN_WORKSPACE`) with the injected `managed_workspace_default` config,
which enables Claude/Codex but not OpenCode. It is likewise outside `cujs`.
A non-blocking **Copilot** job (`live and copilot`, sixteen cases) runs on Linux and Windows the
same way and is likewise outside `cujs` until it is stable.
A blocking **Antigravity** job (`live and antigravity`, eight cases) runs on Linux only with
the pinned `ANTIGRAVITY_BUILD`; it is part of the `cujs` gate.

Each lane installs only the agent CLIs it needs (one, except the managed OpenCode lane's
three), once, and runs all its configure, headless,
commands, lifecycle, and applicable app-server journeys. Cases remain serial
inside each fresh VM because configure/revert can touch machine-level settings;
separate runners isolate those writes as well as the PTYs. All four agent × OS
Full jobs run in parallel: cases share no workspace state, only inference capacity.
They share that capacity with each other, the concurrently running agent e2e shards,
and other PRs, so rate limits remain possible.
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

### Soft-fail tests

Use `@pytest.mark.soft_fail` to move a flaky integration test out of required CI without
changing its assertions. Smoke, Full, and Managed config selectors exclude `soft_fail`;
the advisory lane selects `live and <agent> and soft_fail`. The marker is generic, not
Smart Router-specific, and does not change local pytest outcomes: failures still fail.

The advisory matrix currently supports live Claude cases on Linux and Windows using the
existing e2e workspace/bearer. Before marking another agent's cases or tests requiring a
different workspace/setup, extend the advisory lanes to run them. The marker alone does
not provide that setup or affect unit tests, installation checks, or dedicated CUJs.

Only the three Claude Smart Router toggle tests (`test_smart_router_routes_claude_subagent_at_launch`,
`test_smart_router_skill_turns_claude_subagent_routing_off` and
`test_smart_router_skill_turns_claude_subagent_routing_back_on`) and their six variants each carry
`soft_fail`. The **Soft-fail journeys · claude · Linux/Windows (non-blocking)** jobs run those cases
unchanged, with the pinned Claude version and existing e2e workspace/bearer. They run after
Full and Managed config finish, one OS at a time, to avoid adding concurrent inference load.
They use `continue-on-error`, stay outside `All integration tests`, and upload separate
`integration-soft-fail-claude-<os>` artifacts even when tests fail.
No skip, xfail, task retry, weaker assertion, or production routing change is involved.
The separate advisory harness-latest workflow still selects these cases too.

Evidence: [the original Windows failure](https://github.com/databricks/unity-gateway/actions/runs/39822847395/job/119982066996)
and [a matching Linux failure](https://github.com/databricks/unity-gateway/actions/runs/39736474661/job/119684849649)
completed ordinary child work but never displayed a selected routing model. Child startup
roughly 20 seconds after the Agent call suggests route-selection timeout followed by fallback;
the router's exact error was not retained. This is a shared reliability issue, not an
established Windows-specific defect.

Restore required coverage after route-selection outcome/error diagnostics identify the
failure, the underlying routing/fallback issue is addressed, and repeated first-attempt
advisory runs pass on both platforms. Remove `soft_fail` from the recovered cases; keep
all existing routing and child-completion assertions. Keep the shared marker/selectors
and advisory lanes while other tests still use them.

### Opting into harness-latest

The **harness-latest** workflow runs every six hours on `main` (00:00, 06:00,
12:00, and 18:00 UTC), or manually through Actions. It runs marked tests against the
latest Claude and Codex releases, recording exact versions in its artifacts. It covers
CUJ4, routing hooks, skill toggles (including Claude's recipe-header assertions),
and first-prompt banners. It runs separately from PR checks, so failures do not block merges.
Versions are resolved once for all three suites. A final job sends one success or
failure message using `SLACK_WEBHOOK_UG_CODING_AGENTS_POD`, with both versions and
a workflow-run link on success or a failed-job link on failure (the workflow-run
link if GitHub's jobs API is unavailable).
Setup failures also alert; unresolved versions appear as `unknown`. Slack delivery
errors fail the notification job. `test_integration_contract.py` covers this wiring
and message generation with a substituted HTTP boundary, not live Slack delivery.

Opt in a test or class with `@pytest.mark.harness_latest`, or a whole module with:

```python
pytestmark = pytest.mark.harness_latest
```

Append the marker to an existing `pytestmark` list when present. CI discovers marked
cases under `e2e_cuj/` and `integration/` without workflow edits. Keep existing agent
and workspace markers: `managed_fixture` selects the managed integration runner.
Tests use each runner's existing credentials and installed Claude/Codex CLIs.

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
the same cache path as a live read (`published` outcome, no timestamp), so the
saved-config replay journey holds unchanged. The Codex case also checks stderr guidance to restart the
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

The Claude parent-schema defaults case expects discovered numbered GPT models to carry
`[1m]` in the picker and retain its catalog display label.

The smart-routing banner journeys inject static Claude and Codex model lists with
`smart_routing` enabled in the agent config, run `ug configure`, then launch the real TUI with
`first_prompt_and_subagent_no_orch_v0` and submit one small file task, asserting the
"Using Unity Gateway Smart Router." banner naming the selected model, a routed answer completing
the file task, and normal exit. The managed default alone routes only subagents, with the
orchestrator; the
managed-default skill-toggle cases cover it.

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

The separate member-triggered `/ug-review` workflow runs the pinned Open Code Review
action through the Databricks gateway's Anthropic route. Adding `ug-review` or `code-review` also
requests review on in-repository PRs; fork PRs use the member comment to access
secrets safely. `../test_ug_review_bot.py` covers gateway authentication,
OIDC-authenticated npm-proxy configuration and credential cleanup, and the workflow
contract as component tests. This integration suite does not establish
live review-model access or GitHub comment posting.

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
  -cf - scripts/run_integration.py tests/integration tests/fixtures | \
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
Python 3.12, uv 0.13.5, Node 22.19.0/npm, Databricks CLI 1.9.0, `sudo`, and
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

This runs all 88 live cases. For the seven installation checks, run the same
runner/version/index arguments with `--installation-only` and omit `-- -m live`;
no bearer or workspace is needed. Results remain under `.integration-runs/`.
Each invocation needs a new output directory; an existing one is rejected.
The runner returns nonzero on installation or test failure.

Do not drop the mirror flags if public registries resolve to `127.0.0.1` or return
`ECONNREFUSED`. The runner deliberately ignores host `.npmrc` and resolver settings.
Outside the Databricks network, use reachable package indexes explicitly instead.

Native launch-option component tests (`native_claude/test_mod.py`) check two real Claude
processes with different resolved routing baselines. Python cases cover Claude-specific presets,
unknown selectors, and absent versus empty flags. These checks do not establish gateway/TUI coverage.

CUJ 4's preset journeys check Claude's visible ON/OFF status band and routing scope
before work and after off/on toggles. Checks ignore terminal styling and trailing
UI controls; offline cases reject stale states, wrong scope, and command confirmations.

The plugin test host runs without a process capability, so the mod's session-control call
is covered by the live skill-toggle journeys rather than `claude plugin test`; native
component tests stub the subagent routing process protocol. No component
check asserts that the launch paths withdraw the skills.
The TypeScript launch test also checks event/result forwarding. Separate cases cover
denied-write rollback, invalid request bodies, disabled launches, and body-merge validation;
no simulated subprocess success is used to claim toggle coverage.

`integration/test_ug_smart_routing_hooks.py` checks `smart_router_recipe_name` in its existing
Claude skill-toggle journeys. The recorder forwards to the real workspace, configured
through public `ug configure --workspace <recorder-url>` without hand-writing ug state.
Checkpoints and each calculation's unique marker correlate parent/child inference
requests to the launch, off, and back-on phases. All matched tool-capable inference payloads must carry the
launch recipe, `DISABLED`, then the launch recipe again, omit the recipe header, and reach non-empty HTTP 200
responses, with only the existing verified native compatibility retries accepted.
The same requests must carry the orchestrator's Agent/Task guidance only while
orchestration is active, with the native "When in doubt, don't spawn" advice absent.
Background completion and native follow-up guidance must remain present, including
instructions to end the current turn and wait for a later completion notification.
Native request-construction component checks also compare the Agent schema across
native commands, CLI session toggles, and native toggles after resuming a saved
prompt-snapshot fixture. They also check ineligible launches; their
unauthenticated requests are not live inference coverage.
The expected launch recipe comes from the recorded routing request, so a default
recipe change does not require editing the test.
Local Claude mod confirmation replaces the old saved-control-file/assistant-turn wait;
Codex retains its CLI control-file checks. No extra TUI sessions or tasks are added.
Clear/reload, compaction, and denied-write rollback are not independently checked.

Subagent-routing failures have shared warning coverage in `test_codex_routing.py`;
the live hook journeys retain their routing assertions and include stderr when no
decision is returned. This diagnostic coverage does not prove routing service health.

Smart-routing scenarios use checked-in model menus assigned to the explicit CI
workspace. US `task_v3` uses GPT-6 Sol / Luna; the Canadian managed workspace uses
GPT-5.6 Sol / Terra / Luna, since it has no GPT-6 services. Unknown Codex workspaces
fail until a fixture is assigned. Claude uses Opus 4.8 / Sonnet 4.6 / Haiku 4.5.
Direct hook inputs share the US fixture; managed TUI inputs use their workspace's
fixture. No workspace is auto-selected or modified. All real routing, native notice,
audit correlation, and child completion assertions remain required.

Routing-toggle journeys wait for the child's own persisted assistant answer, in addition
to the parent answer. A parent reporting the expected value while the child is still
running cannot satisfy child completion. The wait is bounded and still fails on timeout.

Codex routing-display unit coverage accepts a generic child label only after the tagged
prompt and before the next prompt, requires the expected orchestration state, and
rejects stale or unrelated panels. Live audit correlation and child answers remain required.

After Codex compaction removes a prompt, display correlation may instead use the
native Spawned row immediately following the routing box. Unit checks reject later
answer text, intervening prompts, and nonadjacent or unrelated spawn rows.

For a managed workspace other than the two CI workspaces, set
`UCODE_TEST_CODEX_ROUTING_FIXTURE` to the basename of a checked-in fixture whose model menu has
been validated in that workspace. The suite never guesses a model menu or silently changes the
workspace.

Browser MCP review regressions check stale ACTIVE credentials wait for a changed credential
or an observed non-ACTIVE state, pending provisioning continues, and missing services fail
before browser launch even with a cached workspace ID. Polling bounds each HTTP call
by the remaining deadline and never reports success from a late response. These component tests control external boundaries
and do not establish live browser sign-in or endpoint availability.

Pre-browser credential reads retry transient HTTP/network failures twice before reporting failure.

Credential validation rejects malformed initial responses before browser launch and non-string
states during polling. The browser subprocess runs Python in isolated mode.

MCP review regressions cover bounded connector descriptions with a complete body list,
malformed upstream tool entries and slow metadata
discovery while unrelated replies continue. Failed metadata discovery retries naturally
on the next operation, without a cache or cooldown. These are component
checks, not native Cowork invocation or live provider sign-in.
