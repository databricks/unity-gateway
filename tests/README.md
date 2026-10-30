# Test suites and user journeys

MCP/Skills browsing has component coverage in `tests/test_catalog_browsing.py`
(repository-relative path): paginated BASIC metadata, streamed rows, missing metadata,
UTC dates, keyboard selection/filtering/cancellation, loader cancellation, and rendered
80×24/40×12 table rendering and detail scrolling. Status coverage distinguishes selection
from configuration/installation, protects read-only managed MCP rows, and checks Skills
attribution against source identity, target directories, and surviving files. These checks
do not establish live workspace discovery or agent invocation coverage.

Integration runs a freshly installed ug wheel/release, exact real Claude/Codex
(and optionally OpenCode) versions, and the existing real e2e workspace. It has no application imports,
mocks, monkeypatching, fake binaries/services, or fabricated ug state.

| Category | Location | What it proves |
| --- | --- | --- |
| Unit/component | Existing `test_*.py` files | Individual behavior; dependencies may be mocked |
| Dedicated-workspace CUJ | `e2e_cuj/test_*.py` | Real user journeys against read-only, preconfigured workspaces |
| Existing e2e | `test_e2e*.py` | Real workspace behavior with some patched setup/internal calls |
| Connector skill behavior | `connector_behavior/test_*.py` | Real Claude decisions with generated guidance and synthetic connector tools |
| Integration CUJs | `integration/test_*.py` | Public configure, TUI, script, command, protocol, and lifecycle journeys |
| Installation | `integration/test_installation.py` | Fresh installed package, CLI, and local helpers without credentials on Linux and advisory native Windows |

`test_budget.py` covers the workspace-authenticated spend-status client in
`src/ucode/budget.py`:
workspace URL/token reuse without account discovery or account CLI authentication,
personal/shared decimal parsing, optional percentages, pagination through empty pages,
invalid/repeated tokens, malformed responses, and discarding partial results after failures.
Budget IDs come from resource names, including when the deprecated ID field is absent or differs.
Future nonempty scope/period strings are preserved, alone or alongside known values.
Decimal checks cover negative and nonfinite values for all three numeric fields,
valid zero/fractional strings such as `"85.25"`, and explicit nulls treated like missing
optional fields. Null regressions cover each numeric field and all three together,
alongside complete and omitted-field statuses without losing valid limits. One representative
opaque API error covers client passthrough; command-specific auth/error cases and
later-page failure coverage remain separate.
Obsolete account-token tests are replaced by workspace-auth assertions. These are
unit/component checks, not live workspace or sign-in coverage; CLI wiring is separate.

Claude mod entry-point coverage lives in `test_claude_mod.py` (generated plugin contents),
`test_claude_smart_routing_v2.py` (launch wiring), and the explicitly selected
`native_claude/test_mod.py` suite. Claude 2.1.290 validates the module and runs its
TypeScript event-forwarding and launch-option tests in CI. The existing Claude
skill-toggle integration journeys check the live recipe payload field (launch recipe at
launch and after off/on, DISABLED after off) and Agent/Task delegation descriptions.
Native request-construction checks verify the
description switches after both native commands and CLI session toggles, including
native toggles after resuming a saved prompt-snapshot fixture. The description includes
instructions to end the current turn while awaiting a
background completion notification, and retains native follow-up guidance and the Agent
schema, including routing-only and missing-marker launches. Omni launches get the
orchestrator description like other eligible launches.
These deliberately unauthenticated requests do not establish
successful inference or delegation quality.
See [the mod guide](../docs/claude-mod.md).

Native mod component tests cover a lone call's decision under its Agent row, parallel calls
drawn as cards above the parent's next reply in call order (replies drawn before routing
never used, calls still routing waiting for a later reply, cards kept across a turn that
ends while a background agent runs and dropped on abort), distinct IDs for identical tasks,
nothing above the prompt, pending row and turn-spinner routing timers, and disabled/failed
routing. They do not establish live
parallel-agent TUI or gateway coverage.

`test_agent_antigravity.py` covers the Antigravity CLI agent: Databricks-to-Antigravity model
names (dotted versions, `-preview` names and their tiers, image models dropped), the `agy models`
selection-id lookup, the gateway-mode environment, skills.json/mcp_config.json merges and the
entry-scoped skills.json revert, provider-service pinning, and launch cleanup (spawn failure,
nonzero exit, Ctrl-C). `test_gateway_proxy_integration.py` runs the Antigravity proxy hooks over
real local HTTP (model path rewrite, `includeThoughts` body fix, provider header, Gemini-only
route). `test_agents_init.py`, `test_mcp.py`, `test_cli.py` (including `ug revert` keeping
skills.json entries added after configure) and `test_agents_commands.py` cover dispatch, the
native installer, `agy mcp list` parsing, `ug antigravity`/`ug agy`, and
`ug agents add antigravity`. These are unit/component checks. Automated live coverage is the
eight journeys in `integration/` (blocking Linux CI lane, Databricks Hosted and the Gemini
Enterprise MPS; see `integration/README.md`).

`test_integration_contract.py` checks that isolated test sessions set Antigravity's
`AGY_CLI_DISABLE_AUTO_UPDATE` to the required literal `true`, overriding an inherited `1`.
This component check does not run the native updater; the integration runner retains its
before/after agent-version check.

`test_ug_headers.py` covers the `UG_HEADERS` parser and the `--header` flag, `test_databricks.py`
ug's own requests carrying them, and `test_agent_codex.py` `UG_HEADERS` reaching a Codex launch's
`-c` headers (plain and smart-routed) without writing its profile. No automated test checks the
header at the gateway.

`test_claude_credential_blanks.py` covers the `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` blanks: always in
the user file (relayed included), written to the managed file by `ug configure` only, a plain launch
making no privileged write after another writer drops them, skip mode, revert, and admin passthrough
not overriding them. `native_claude/test_credentials.py` runs the CI-pinned Claude (2.1.290) against a
fake API with both shell variables set: the blanks make it send the `apiKeyHelper` token, or on the relayed
path its own OAuth token, with no empty header; without them the shell's values win (control cases). It
skips on a machine with Claude managed settings, which would replace the fixture's.

`test_claude_header_mod.py` covers installing the Claude header mod's files and managed entries
(no rewrite when unchanged), keeping an admin's `prependPlugins`, a failed install never enabling
the plugin, revert removing the files and keeping the guard, the sudo allowlist, `header_mod_active`
needing every file, and an unreadable mod folder. `native_claude/test_mod.py` validates the mod and runs its merge tests
(`native_claude/header_mod_tests`). Claude re-reading `ANTHROPIC_CUSTOM_HEADERS` per request was
checked by hand on Claude 2.1.304; no automated test sends a request through the installed mod.

The required Isaac feedback form and background delivery notifications need manual
validation using the released Isaac skill and existing capture/retry CLI. Check
category selection, empty and inline feedback, native retries, the off form's
16-character minimum, cancellation, repeated toggles, and submission success/failure.
Also check that the form closes immediately and submission makes no model request.
Start a new task during capture and confirm the report retains the original task;
toggle routing during upload and confirm its recorded settings stay fixed. Check that
saved evidence stays unchanged, upload permission denials are honored, local
previews stay local, and Other stays text-only. In a session launched with routing,
check `/isaac-feedback` before and after turning routing off. A session launched
without routing should use the existing feedback skill without loading the router
plugin. The routing-toggle journeys do not cover the form, notification timing, or
live evidence uploads.

`test_claude_answer_delivery.py` covers Claude answer selection, composition, ownership,
failed tasks and resumed revisions using transcript inputs, plus real subprocess I/O,
stream-input bypass and capability lifetime. Text, JSON and streaming JSON results
stay on stdout when UG redirects its diagnostics to stderr. Live model/gateway
selection and hook activation remain uncovered.

The **harness-latest** workflow runs every six hours on `main` and currently covers
smart routing only.
It resolves npm's `claude-code@latest`
and `codex@latest` tags to exact versions and records them in its artifacts. Three
isolated runners cover all of CUJ4, both agents' routing-hook cases, and the managed
skill-toggle and first-prompt banner cases. The Claude toggle cases assert the actual
recipe header values across on → off → on. It runs separately from PR checks, so
failures do not block merges. Existing pinned-version lanes remain.
All three suites use the same resolved versions. After success or failure, one
Slack message is sent through `SLACK_WEBHOOK_UG_CODING_AGENTS_POD`, including the
versions and a workflow-run link on success or a failed-job link on failure. Component tests in
`test_integration_contract.py` check the messages, setup failures, job timeouts,
GitHub link fallback, and Slack delivery errors without sending live messages.

Opt a test or class in with `@pytest.mark.harness_latest`, or an entire module
with `pytestmark = pytest.mark.harness_latest`. The lane discovers marked cases
under `e2e_cuj/` and `integration/`; no workflow edit is needed. Integration cases
retain their agent and workspace markers, including `managed_fixture` when needed.
See [the harness opt-in guide](integration/README.md#opting-into-harness-latest).

CUJ 4's Claude-only preset band journey (separate from the delegation journeys) checks Claude's ON/OFF status band, routing scope, and
updates after off/on toggles, and that the band is hidden after the launch turn and by
the second turn after each toggle. They also check that the prompt hint row under the
input ends with `smart router ON` from launch, flips to `OFF` and back to `ON` with each
toggle, and keeps that state after the band hides. Checks ignore terminal styling and
trailing UI controls; offline cases reject stale states, wrong routing scope, and command
confirmations, and keep the band, the hint row, and the toggle reply apart.
The mod's own tests (`native_claude/mod_tests`) cover the same rules for both toggles
without a live agent.

CUJ3 expects GPT Luna in both agents' catalogs and pickers, including Claude's gateway alias,
and covers an explicit Claude headless task on that model.
Its Claude decoy catalog includes both Claude and Codex decoys; neither belongs in the scoped picker.
The managed Claude parent-schema journey expects numbered GPT models to carry `[1m]` in the
picker while retaining the catalog's display label.

Dedicated CUJs reuse `integration/utils` session/terminal mechanics and file-task
and transcript readers, not its config stubs or pytest fixtures. Prompt/model
correlation remains CUJ-specific. Concurrent runs may read the same CUJ workspace.
Workspace operations reuse the base class's authenticated Databricks SDK client;
offline tests require GET-only API calls and verify config changes fail without repair.
The live fixture compares configuration before and after the journey, even on failure.
CUJs never republish configuration or create a remote reservation.
CUJ helper tests also verify that unsupported agent names fail rather than defaulting to Codex.
Offline PTY checks verify that the Claude background-task wait observes "No tasks currently
running" or a native task menu containing only completed rows before sending `/exit`, without
stopping tasks or confirming an exit dialog. Running/scheduled sections, incomplete row counts,
and completed-task text outside the native menu cannot satisfy the wait.
The live Claude subagent skill-toggle journeys use this wait after their final calculation.
CUJ4's Claude delegation sessions also use it after verifying delegated file tasks: a completed
answer does not prove that a resumed child has stopped running.
They cover Claude/Codex helper dispatch and rejection of routing decisions without
the agent-specific prompt-submission evidence.
The shared preset CUJ runs both agents for every version and derives routing and
orchestration assertions from each agent's resolved flags. Both must complete a native
child task. `subagent_orch_v0_claude_only` routes Claude's child; Codex uses the managed
default without routing requests, new router decisions, recipe headers, or orchestrator context.

Agent-specific smart-routing preset component checks cover shared base presets, explicit agent mappings,
complete per-agent validation, session precedence, and preserving the preset selector
until managed-default agent selection.
CLI/component checks cover `--enable-smart-routing` and managed routing defaulting to
`subagent_orch_v0_claude_only`, preserving known selectors, overriding legacy flags,
and restoring the inherited environment. Claude routes subagents with orchestration;
Codex stays unrouted unless an explicit known version enables it.
These checks do not establish live agent coverage.

The shared request recorder and native compatibility-retry checks live in
`integration/utils/request_recorder.py`. The CUJ adapter keeps its existing
configuration glue and imports those helpers; request forwarding, header
redaction, and paired response assertions retain their behavior. The integration
runner installs `httpx` in its isolated test environment for these helpers.

CUJ3 and CUJ4 also verify Claude's native recovery from the known thinking-display 400:
the same payload without display must receive a non-empty 200 for adaptive or enabled thinking.
If the next attempt rejects `safeguards`, only its native removal is accepted before the final 200.
Retries match system context and session metadata so concurrent parent traffic is excluded.
Offline regressions reject missing/failed retries and changes to the model, prompt, budget, or effort.
Delegated tasks wait for the native child's answer, independently of the parent's final turn.
Offline checks require the child's hidden file value; a parent-only answer cannot satisfy the wait.
First-prompt tasks retain completed parent-turn evidence. Offline PTY checks cover billing-notice
input readiness, observed dismissal, and a later notice for child work.
Child HTTP matching accepts Claude's single appended transport newline after the routing
checkpoint; additional text, whitespace, and Codex prompt changes remain rejected.
Offline CUJ regressions cover Codex delegated-turn completion and exact task-request matching;
notifications alone, Claude title requests, and parent continuations do not qualify.
Unrelated and pre-checkpoint request bodies are excluded before JSON decoding.
CUJ4 accepts both top-level tool definitions (Codex 0.154.0) and Responses Lite
`additional_tools` input items (0.162.1). Encrypted child messages match the routed
task name to the recipient; plaintext prompts still match exactly. Both require tools.
The original smart-routing CUJ runs four fresh sessions: routed and explicit model for both
Claude and Codex, with explicit-model bypass assertions enabled for both agents.
Per-agent tests cover six `SMART_ROUTER_CONFIG_VERSION` cases each; every case checks
first-prompt routing, orchestrator context in inference input, and a completed explicitly
requested routed subagent. A separate Claude-only test covers the status band and hint row. It does not establish
automatic orchestrator delegation. Routing-disabled coverage is deferred until a separately
preconfigured workspace is assigned.

`test_entry_points.py` also runs both installed console scripts (`ug` and `ucode`)
and checks their version output against the `unity-gateway` distribution metadata.
`test_startup_imports.py` guards the bare `--version` / `-V` fast path and deferred
command-specific imports in fresh interpreters. Component tests cover metadata lookup,
version suffixes, fallback, and unchanged dispatch for other arguments; they do not
claim a live agent launch or end-to-end startup timing. An isolated interpreter
without site packages also proves that intact metadata can produce version output
while `--help` fails on a missing CLI startup dependency.
`TestUpgrade` in `test_cli.py` covers both command names before, during, and after
the distribution rename with mocked installer calls, including `--help` verification
of the full CLI import path and failure recovery guidance.
`test_subprocess_cross_os.py` covers Windows npm shim resolution, native and Node targets,
literal argument preservation, and the shared `subprocess_cross_os.run` / `subprocess_cross_os.popen`
entry points, including UTF-8 text decoding, explicit encoding/error overrides, and unchanged
binary output. `test_launcher.py` covers terminal handoff and exit status.
Ruff rejects direct subprocess launches outside `os_compatibility/subprocess_cross_os.py` and tests.
`test_system_cross_os.py` covers the administrator check on Windows and elsewhere and the
Program Files lookup.
Claude's native resolver tests remain in
`test_agent_claude.py`; installation failures are covered in `test_agents_init.py`.
These are component checks, not live Windows coverage for every agent.

Agent configuration tests also verify `ug` auth/MCP helper commands, including
quoted executable paths and replacement of legacy `ucode` routing/web-search helpers.

`test_desktop_auth.py`, `test_desktop_auth_cli.py`, and `test_file_lock_cross_os.py`
cover Desktop token-only stdout, explicit workspace/profile arguments, interactive-only
browser recovery, bounded login/lock waits, cancellation, and lock contention.
Recovery regressions check token recheck before browser login, lock release after
success/failure, one shared deadline, and network/CLI failures at each recovery stage.
Proxy bridge coverage checks that OAuth U2M discovery retains an authentication action
alongside normal tools, without opening a browser. It covers per-request metadata lookup,
recovery on the next operation after a failed lookup, PAT/non-U2M exclusion, pagination,
upstream name collisions, and result/transport metadata preservation. If upstream removes its authentication tool on a
fresh listing, local discovery and automatic recovery resume; pagination retains upstream
ownership within that listing. Discovery and unrelated replies continue during
interactive login. Metadata-helper cases return `False` for failed lookups and absent
connection kinds; later operations look up metadata again.
A stream scenario covers tool failure, explicit login, tool refresh, task retry, and later
reauthentication; explicit retry also works after cancellation. Discovery auth errors and
service-scoped login-URL challenges expose recovery after earlier success. Gmail/Calendar
tool-error and login-elicitation regressions check automatic sign-in and one request replay,
cancel/persistent-error pass-through, PAT/non-U2M exclusions, and nonblocking discovery.
Direct URL-matcher tests reject other services/hosts and malformed URLs; representative
bridge cases verify pass-through for both error forms. Success results and policy or mixed
elicitations retain their responses. Generated guidance distinguishes authentication from
retrying data tools.
A refresh that still requires authentication returns a failed auth result. These
checks replace external login and do not prove native Cowork recovery.
Invalid token output must not appear in helper errors or stdout/stderr.
Shared CLI discovery selects the newest absolute binary for cached tokens and every
recovery subprocess; a missing CLI fails before token fetch or browser login.
Missing-option checks exercise plain and ANSI-colored stderr and require parser exit
code 2 with empty stdout. These component tests replace external authentication;
they do not establish native Desktop GUI or browser-login coverage.

`test_claude_desktop.py` exercises real temporary profile, shared metadata,
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
The installed-CLI integration suite covers two complete Desktop journeys on Linux and Windows:
system.ai models and subscription-relay Anthropic MPS. Both verify profile configuration,
credential-helper execution, live Messages inference, installed Cowork MCP calls, preservation,
and revert. Inference requires HTTP 200 and nonempty assistant text; MCP calls use web_search
to find Databricks' official website without SQL compute. See
[integration coverage](integration/README.md) for prerequisites and evidence.
CUJ1/CUJ2 compare complete Desktop profile/metadata JSON for system.ai models and non-relay
Anthropic MPS. CUJ3's existing picker journey checks its incompatible scoped aliases preserve
all Desktop profile/metadata bytes after its existing configure call.
These cases do not launch Desktop, execute connector skills, or exercise browser login/renewal.
The [native integration plan](../docs/claude-desktop-integration-testing.md) defines the
separate interactive macOS and Windows coverage needed for those claims.

`test_desktop_setup.py` exercises configure, refresh, deleted-profile recovery,
successful setup without the default-model warning, recoverable setup failures that still warn,
dry-run, and revert with native path resolution under temporary homes on simulated macOS,
Windows, and Linux, without replacing the path resolver. The helper
executable uses an absolute host-native path containing spaces. Missing Windows/Linux
app data fails before external authentication or model discovery, and revert without
a UG profile leaves existing settings untouched. These remain component checks.
They also assert `Unity Gateway - <CLI profile name>` labels, label refresh without
changing the UUID, and configure/revert warnings on profile-lock timeout without
changing profile or selection-state files. An existing reserved UUID without records
is replaced on configure and deleted on revert. Workspace changes reuse the same file.
Subscription-relay component coverage configures the combined credential helper on all
three platforms. Helper tests cover macOS Keychain, Linux and Windows credential files,
`CLAUDE_CONFIG_DIR`, custom macOS Keychain entries, finite required expiry timestamps, silent
refresh before interactive login, shared login budgets, malformed environment tokens, and
credential separation. These component checks do not establish native Desktop invocation
of the helper or inference.
CLI revert clears workspace state even when Desktop cleanup fails. A lock-failure
regression invokes the Typer CLI and checks exit code 1, one warning, and a failed
Desktop summary while profile and previous-selection files survive. Retry returns
exit code 0, deletes the UG profile, and restores the previous selection without
workspace state; a repeated revert also returns 0.

`test_cowork_hooks.py` covers SessionStart guidance naming every connector without reporting
unanswered or timed-out probes, a timestamped sign-in line for connectors whose probe returns a
sign-in error, sign-in error recognition (`-32006`, `-32042`, HTTP 401, nested exception
groups) versus other errors, and skipping probes when the token cannot be minted.
UserPromptSubmit cases cover doing nothing after a short gap (measured from Claude's last
reply, so an already-recorded prompt does not count), probing and flagging stale disconnect
notices and sign-in needs after an idle gap, keeping that guidance without a token, and doing
nothing without a readable transcript or previous reply. Other events are ignored and CLI
stdout holds only the hook JSON. Probes and token minting are faked; native Cowork hook
execution remains unverified.

`test_cowork_plugin.py` covers active Cowork-root discovery, ambiguous-profile refusal,
generated marketplace/plugin/MCP manifests, the generated connector skill naming Claude-only
MCP servers with its two 5-second retries and one sign-in recovery attempt for access/IP
denials with a stop on persistent denial, the generated `SessionStart` hook command,
current-workspace managed-skill selection,
Claude-only MCP filtering, absolute proxy executables, saved PAT mode, custom-OAuth refusal,
install/update selection, idempotent cleanup, and saved-root removal. Alias checks cover
short service-first names, unchanged proxy routing, custom-name preservation,
order-independent collisions, and repeated replacement of old keys. Manifest checks require
the `ug` name and display name in both manifests. Legacy-registration checks cover
replacement after successful install/update, preservation on install failure, scoped
cleanup, and unrelated-plugin preservation. CLI component tests
cover managed setup after successful skill/MCP reconciliation, preservation after transient
reconciliation failures, and cleanup for unmanaged or non-Claude configurations. These tests
mock the Claude plugin CLI and do not establish native Desktop policy, account-feature,
late MCP connection, tool discovery, or inference behavior.

`connector_behavior/test_connector_skill.py` is an explicitly selected live-model suite.
It checks that Claude uses an available Slack tool despite a stale disconnect notice, actually
waits and then uses a tool exposed after resume, and performs the bounded wait before reporting
tools absent. Available/pending scenarios run with both the description alone and a skill loaded
before the break. Assertions require actual tool calls and an answer containing a random value
returned only by the synthetic Slack tool. The missing-tools scenario requires both real
five-second waits and rejects invented reconnect/sign-in instructions. An idle-resume case
shows only the skill description plus the `UserPromptSubmit` hook note and requires Claude to
wait and use Slack instead of trusting the disconnect notice. Model requests are not
retried. Slack tools are synthetic to isolate skill compliance from connector credentials;
these are not native Cowork, Slack, proxy reconnect, or Desktop sleep/resume tests.

The suite is excluded from default collection. Run it with an explicitly chosen workspace,
profile, and exact Claude model-service name; missing inputs fail rather than skip:

```bash
PYTHONPATH=src uv run pytest --confcutdir=tests/connector_behavior tests/connector_behavior \
  --workspace https://<workspace> --profile <profile> --model <catalog.schema.claude-model> -v
```

`test_cowork_skills.py` checks current-workspace managed and user-downloaded skill copies,
project/other-workspace/user-authored exclusions, and add/update/remove CLI flows using
names, schemas, and the picker. It checks generated bundle contents and stale-file cleanup
while preserving managed skills; project-local skills stay excluded during plugin refreshes.
Skill APIs and the native plugin CLI are substituted; native Cowork skill invocation
remains unverified.

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

Desktop OTEL component tests cover native trace settings gated by `claude_otel_tracing`
for static and discovered models on simulated macOS, Windows, and Linux. They verify
the collector base URL, protobuf protocol, refreshing `.sh`/`.cmd` headers helpers
without persisted tokens, literal argument quoting, repeated native helper execution
with rotated bearer overrides, Unicode and trailing-backslash arguments, and
JSON-only stdout/failure output. Windows wrappers select UTF-8 before the command.
Lifecycle checks cover repeat configure, workspace changes, disable/re-enable, rollback of helper bytes
and permissions at each write boundary, and revert preserving unrelated telemetry.
Revert also cleans up the helper and selection state after the Desktop library or
application-data root is removed, without recreating either directory; failed cleanup
restores helper bytes, permissions, and selection state for a later retry.
Byte-writer checks cover explicit permissions independent of contents, unchanged
JSON formatting, helper execute-permission repair, and failed atomic replacement
without leftover temporary files.
These checks do not prove native export, token refresh in open sessions, or ingestion.

`TestChildStdoutLaunch` in `test_cli.py` checks Claude `-p`/`--print` and Codex `exec`
(plain and `--json`) and `app-server` status/error output on stderr, argument forwarding with
and without ug's `--`, and an unchanged stdout descriptor. Detection checks exact tokens before
the agent's `--`, including `e` and tokens after unknown or variadic options, without parsing
Codex's options. An option value or prompt token equal to `exec`, `e`, or `app-server` also
routes UG diagnostics to stderr; arguments and native agent stdout remain unchanged.
Launches without a matching token retain stdout for status messages.
`TestBootstrapStdout` in `test_agents_init.py` substitutes local Python processes
for Databricks and agent installers/upgraders, asserting real stdout/stderr routing
on success and failure, preserved version-output capture, and clean native handoff.
These are component checks, not live installer, Isaac, or inference coverage.
The live Claude/Codex headless prompt-argument and stdin journeys also require raw stdout to
parse as JSON/JSONL without stripping UG messages; those assertions still require a live run.

Codex browser-login component tests cover invalid refresh tokens, token-only stdout,
network errors, and reuse of a concurrent sign-in within the timeout. Codex config
tests cover the helper flag; real browser consent and desktop behavior are not covered.

Claude catalog selection with a custom OAuth CLI profile needs a live Isaac check,
including default discovery, model locations, and providers when the ordinary profile
has expired. The automated suite does not cover this combination.

`test_mcp_web_search.py` and `test_agent_claude.py` cover custom OAuth search
registration, stale registration repair, SDK cache reuse/refresh, CLI profile
selection, and errors without browser consent through the MCP handler. These
component checks replace external auth/network boundaries; they do not establish
live search, parent/child discovery, or classifier permission behavior.

`test_databricks.py` also covers process-local CLI token memoization, expiry
margins on both wall and monotonic clocks (including suspend and clock rollback),
short-lived failure entries only for confirmed invalid/missing credentials,
forced refresh, and login invalidation. Unknown CLI failures, network errors,
and malformed token responses retry on the next call without a stale-profile hint.
Concurrent callers share a per-profile fetch, and an in-flight result cannot
restore a memo invalidated by login.
Fake CLI subprocesses check that re-auth runs only when a successful help probe
advertises `--no-browser`. Search component tests verify token reuse and transient
timeout retries.
`test_mcp_connection_login.py` controls only browser, HTTP, and clock boundaries. It checks
service-name encoding, workspace-ID resolution, the selected profile user's credential,
pending/FAILED/404 states, transient errors, denial, malformed responses, wrong-account
timeout, manual URL fallback, and workspace-token failures. Success requires an ACTIVE
credential; success, denial, and timeout preserve the workspace profile and token memo.
These replace obsolete resource-flag support, CLI resource-login, and token-invalidation
cases because connector browser sign-in no longer runs CLI login or replaces its session.
`test_mcp_proxy.py` checks async post-login retries and recovery when a service returns
the MCP authentication-required error. HTTP 401 regressions cover no browser on
initialization/discovery/background requests, and connector login only for actual tool calls.
Async HTTP-client checks replace the upstream network and verify unchanged tool-call bodies
across retries. The combined browser-flow case verifies connector readiness polling,
profile/token preservation, and replay without refreshing workspace credentials.
PAT and non-401 errors retain their behavior.
These checks do not establish live login or launch-time savings.

`test_mcp_web_search_concurrency.py` drives the real stdio dispatcher with controlled
HTTP and authentication boundaries. It covers concurrent results and catalog requests,
the four-worker limit, active and queued cancellation, isolated worker errors, and
draining pending searches on EOF. Input failure and interruption cancel queued work.
These component checks make no live gateway requests.

`test_claude_search_provider.py` covers external-provider setup and launch using
real temporary config files and local helper JSON-RPC subprocesses. It checks
legacy ownership, copied marked helpers, custom/disabled entry preservation, config conflicts, caller
arguments, routing/direct/relayed paths, and concurrent standalone/custom helper
catalogs. It also checks that an empty search-model catalog preserves registration
ownership for later external handoff or standalone refresh. These are component
checks, not a live Isaac or gateway journey.

`test_claude_search_discovery.py` covers fresh Claude-only setup through model
discovery, saved state, and search registration, with both UC and legacy model
catalogs. It also checks explicit search-model precedence and preservation of an
existing Isaac server when no GPT model is available. These component tests mock
external discovery and the Claude CLI; they do not establish live search coverage.

`test_agent_claude.py`, `test_agent_codex.py`, and `test_managed_files.py` cover headless repair of
conflicting managed settings, compatible/absent no-ops, policy and backup preservation, denied
authorization, and one-shot `sudo -n` with disconnected stdin. These are component regressions with
privileged writes replaced by temporary-file operations, not automated Isaac, real sudo-policy, or
live telemetry-export coverage.

`test_configure_managed_writes.py` covers a headless `ug configure` writing Claude's managed
`apiKeyHelper`, `ANTHROPIC_BASE_URL`, and custom headers (new and existing file) and installing the
header mod, a headless launch still leaving the file alone, a denied write failing configure with
root/sudo/`--user` guidance, the write policy on each OS, root running privileged commands without
sudo, and `ug configure`/`ug revert` requesting explicit writes. `test_agent_codex.py` covers the
same write and failure for Codex. The CUJ `Session.configure` helper runs headless and so now
writes machine-wide settings with the runner's passwordless sudo, which the `cuj` teardown reverts.
No automated test runs a real root configure.

Managed smart defaults are covered by `test_managed_config.py`, `test_cli.py`,
`test_managed_setup.py`, `test_databricks.py`, and `test_managed_budget.py`: parsing the
`smart_defaults` wire field, reading older `spend_tiers` caches, skipping recommendations
without tier rules, applying recommendations when tiers exist, and serializing the current
API field. These are unit/component checks; live request-count coverage is not included.

`test_usage.py`, `test_budget.py`, and `test_cli.py` cover budget usage without an
opt-in, including the five highest usage percentages, stable ties, missing percentages
ordered last, and the full listing with `--all`.
They check workspace authentication, unavailable usage, and lookup errors without
reporting failures as absent budgets.
Presentation checks cover short budget IDs, spacing, usage colors, over-limit values,
output without colors, and the hint when more limits exist.
These are unit/component tests; CUJ5 checks live `ug usage --all` output against budget
figures before and after the command.

`test_codex_smart_routing_v2.py` checks that the remote Codex TUI receives the
gateway provider on Windows while Unix launch arguments stay unchanged and routing
hooks stay with the app-server. This is component coverage, not a live Windows
sign-in or TUI test.

Claude picker composition is checked directly through the catalog and renderer functions in
`test_agent_claude.py`; focused CLI cases cover source selection and launch precedence.
Ownership regressions there and in `test_managed_model_lifecycle.py` keep UG's discovered
picker refreshable across three launch cycles while preserving unrecorded or edited admin
pickers. These are component checks, not repeated live `/model` validation.
Explicit Claude/GPT `--model` regressions in `test_cli.py` exercise the real launcher
without a managed catalog and preserve family defaults; these are component checks.
Managed UC schema regressions in `test_cli.py` retain non-default catalog models with an overall
default, a family default, or both, while preserving startup selection and family mappings.
`TestBuildClaudeArgv` also checks that caller permission denies survive ug's technical
native-search deny in direct, relayed, and routing settings composition. Inline/file
inputs, repeated settings, and empty launch overrides retain restrictions and leave
source files unchanged. `test_claude_launch_settings.py` drives normal and relayed
launches with oversized Windows settings, preserving merged values and literal
prompt arguments while keeping the settings file alive until the child exits.
Success, nonzero exit, interruption, and spawn failures clean up temporary files;
relayed failures also stop the proxy. Separate launches use separate files, and
short Windows commands and POSIX keep their existing argv. Real Python children
read the file for large catalogs and commands containing non-BMP characters.
Windows command limits and child lifecycle are otherwise simulated; the native
Bedrock integration journey still supplies live Windows/Claude coverage.
These are actual argv/configuration assertions, not native classifier acceptance
coverage.
`test_databricks.py` checks bounded Anthropic catalog requests with `limit=1000`, including
scoped routing headers and model display metadata. It also verifies that Windows CLI install
and upgrade use WinGet and report an actionable error when WinGet is unavailable. Windows
PATH refresh regressions prime a cached missing CLI, then verify that existing WinGet aliases
and persisted user PATH entries are discovered without reinstalling, including repeated bootstrap.
These are component checks, not a live Windows launch. Subprocess regression checks force a
cp1252 default at the dependency seam, then verify UTF-8 text decoding and unchanged binary
output. `test_codex_catalog.py` covers the same forced-locale failure at Codex catalog validation.
The managed-default and discovery integration journeys below check the generated settings
and real picker.

Agent-picker regression coverage in `test_ui.py` and `test_cli.py` drives actual
keyboard selection: nothing is selected by default, selecting Codex installs only
Codex, and submitting an empty selection installs nothing. Rendering checks cover
the selected and empty checkboxes. These are local component checks, not live gateway tests.

`test_claude_smart_routing_v2.py` checks plugin-qualified names, exact model
definitions, and temporary plugin loading in both routing modes. The GLM-to-Haiku
notice regression verifies that both hook messages name the routed agent with
orchestration on and off. It mocks the router response, not native Claude rendering.
Existing launch tests cover caller argument preservation and file removal after exit. These checks
do not cover plugin-write/process-start failures, native plugin refresh, or daemon behavior.

`test_claude_smart_routing_v2.py` checks that the decision log retains the exact candidate
models and router name sent in the request. `test_codex_routing.py` checks that
Codex decisions carry the same request metadata. These use mocked HTTP responses.

`test_claude_pty_replay.py` drives `run_claude_pty` against fake TUIs. One models Claude Code
2.1.295 and later, which keep a hook-blocked prompt in the input box: `/model` must run as a
command, and the replay must arrive unchanged after a switch, a switch timeout, or a missing
quiet window. These are component checks; live first-prompt routing on a pinned Claude Code
version is not executed here.

`test_smart_router.py` executes the installed skill's shell commands with another `ug`
first in PATH and a launching interpreter path containing spaces. Both agents' toggles
must update the session controls through the launching installation. Launch tests check
that Claude settings and Codex's shell policy carry the interpreter and session marker.
These are component checks; they do not establish native skill permission matching or
PowerShell execution.

`test_skills.py` checks agent-specific instruction installation, bundle updates,
and matching prompt/compaction hook content for Claude and Codex. It also covers
shared `SKILL.md` bundles. These are component checks, not live agent execution.

`test_smart_routing_config.py` includes a 162-case Cartesian component oracle: all three legacy
routing flags take unset, `0`, and `1`, while the selector takes unset or one of the five
supported presets, including the customer first-prompt-and-subagent mode. It uses the named
version constants but independently hardcodes each preset's settings and asserts exact
`resolve_environment` and `apply_config` settings, true-unset omission, unrelated-key and
input preservation, valid-selector consumption, and environment restoration.
Absent/unknown selectors are no-ops; CLI checks cover auth, revert, and launch dispatch.
These component checks do not establish live agent, hook, or gateway behavior.
Three additional component cases compare the legacy flags with `subagent_only_v1`,
`subagent_orch_v1`, and `first_prompt_and_subagent_no_orch_v0`, including first-prompt,
subagent-routing activation, orchestration, and preservation of the chosen router name.

The toggle integration journeys retain the legacy routing-only and orchestration cases and
add the three subagent-only `SMART_ROUTER_CONFIG_VERSION` presets. They require only
`smart-router` for routing-only cases, and both bundled skills when orchestration is enabled;
Claude checks local mod confirmations and the inference payload's `smart_router_recipe_name`;
Codex checks saved session controls and native tool-result confirmations after each toggle.
The managed-routing variants use the default version for Claude and explicitly select
`subagent_orch_v0` for Codex; CUJ4 checks that managed defaults leave Codex unrouted.
Both explicitly request their children,
including while routing is off.
Claude's six toggle variants are temporarily non-blocking on Linux and Windows; required
Smoke, Full, and Managed config lanes deselect `soft_fail` cases. A separate
Soft-fail journeys lane still runs every assertion and uploads evidence on both platforms.
Codex toggles, direct routing hooks, CUJ4, and routing component tests remain required.
See [the generic soft-fail marker, evidence, and restoration criteria](integration/README.md#soft-fail-tests).
The managed-fixture banner journeys separately cover the selector unset (managed default) and
`first_prompt_and_subagent_no_orch_v0` for real first-prompt tasks.
Preset parameterization augments only routing hooks, skill toggles, and first-prompt routing;
their original legacy-env or managed-default cases remain. Explicit-model selection, command
forwarding, app-server initialization, and catalog fallback retain their original legacy flags
and on/off coverage; preset-specific behavior there is not covered.
`test_integration_evidence.py` checks native tool-result extraction for both agents,
including collapsed-output records, and excludes user echoes and assistant claims.

Dedicated regression coverage is missing for root-only Smart Router Orchestrator activation,
compaction, retained skills in ineligible sessions,
role-contract preservation, and isolation from legacy preference files. Codex's
native hook merging and project trust, automatic delegation, and execution of
pre-existing hooks remain outside the integration suite.

The portable Windows routing test checks native executable forwarding, generated
hooks/plugins, caller arguments, and cleanup without Unix imports. It does not
establish live Windows hook execution or interactive routing.

Pi's token-command tests in `test_agent_pi.py` exercise Windows executable paths
through POSIX parsing, including spaces, apostrophes, profile names, and PAT mode.
They do not launch Pi or Git Bash on Windows.

Shared native transcript parsing is checked by `test_integration_agent_helpers.py` and
`test_integration_evidence.py`. These component checks protect existing journey evidence;
they do not establish live agent coverage.

## CUJ coverage matrix

These are **implemented assertions**, not a claim that every version passes.
Consult the run's JUnit report and artifacts for results. Each function states
its **Scenario** and **Expected** outcome and shows its configure and launch
commands. Fixtures supply fresh environments and credentials, never configured ug.
Integration tests live directly in `integration/`; shared mechanics live in `utils/`.
Dedicated-workspace CUJs live in `e2e_cuj/` and use the shared `cuj` fixture plus
integration utilities; only CUJ-specific evidence correlation stays in a test file.

| Test | User action | Expected evidence |
| --- | --- | --- |
| `test_cuj_configuration`, `test_cuj_codex_inference`, `test_cuj_claude_inference` in `test_cuj2_mps_mcp.py` | Read the permanently preconfigured two-agent MPS/MCP config; run separate configure, Codex TUI, and Claude TUI cases | Configuration verifies both native MPS APIs, generated settings, and sandbox inclusion with web_search excluded; each TUI case records its exact MPS target header and model, paired HTTP 200 response, and a completed parent turn with a nonempty greeting answer. Request IDs correlate traffic without requiring the model to repeat them. Live sandbox execution is deferred because MAS cannot downscope the CI service principal. No workspace config CRUD is performed |
| `test_ug_configure_claude_databricks` | Configure Databricks Hosted; execute the generated auth helper; launch plain `ug claude`, read a file, and open `/model` | Generated helper invokes `ug` with clean token stdout; assistant returns an unpredictable file value; native discovery caches `system.ai` models and the picker shows a discovered model without an opt-in flag; normal exit; reopen with working keyboard input |
| `test_ug_configure_claude_anthropic_mps` | Select Anthropic MPS in the real configure picker; launch Claude | Saved provider in status; completed TUI file task; normal exit |
| `test_ug_configure_claude_bedrock_mps` | Select the restricted Bedrock MPS in the real configure picker; launch Claude without an explicit model | Saved provider in status; Sonnet 4.6 is selected from the Sonnet + Haiku allow-list and completes a TUI file task; normal exit |
| `test_ug_configure_codex_databricks` | Configure Databricks Hosted; execute the generated auth helper; open Codex TUI and read a file | Generated helper invokes `ug` with clean token stdout; completed assistant answer contains the file value; normal exit and reopen |
| `test_ug_configure_codex_openai_mps` | Select OpenAI MPS in the real configure picker; launch Codex | Saved provider in status; completed TUI file task; normal exit |
| `test_ug_claude_custom_oauth_cli_boots`, `test_ug_codex_custom_oauth_cli_boots` | Launch with `ENABLE_CUSTOM_OAUTH_FROM_CLI=1`, `--workspace`, and `--client-id databricks-cli` | Real TUI reaches a usable prompt, accepts keyboard input, exits normally, and saves `client_id = databricks-cli` in its generated CLI profile; Claude also reads the OS-managed settings and requires a profile-only `apiKeyHelper` |
| `test_case_07_configured_claude_discovers_system_models` (CUJ7), `test_case_09_fresh_claude_discovers_system_models` (integration) | Launch configured/fresh Claude with no discovery flag or source override | Claude caches `system.ai` models (including recognized Anthropic gateway aliases), includes ug's discovered family defaults, and shows a discovered picker entry; case 07 lives in `e2e_cuj/test_cuj7_model_discovery.py` |
| `test_case_08_configured_codex_uses_default_models` (CUJ7), `test_case_10_fresh_codex_uses_default_models` (integration) | Launch configured/fresh Codex with no source override | ug discovers `system.ai` models but leaves model/reasoning preferences unset; app-server exposes native GPT entries without a generated scoped catalog; case 08 lives in `e2e_cuj/test_cuj7_model_discovery.py` |
| `test_case_11_*` | Launch configured and fresh Claude with an explicit provider and no managed config | The provider catalog replaces built-in picker rows; the cache contains exactly the provider model, and the picker shows both its row and Default resolving to it |
| `test_case_12_*` | Launch configured and fresh Codex with a provider | The provider supplies exactly its model catalog |
| `test_case_13_*` | Launch configured and fresh Claude with a model location and no managed config | The explicit parent's catalog replaces built-in picker rows and appears in the real picker; Default resolves to the model in the scoped fixture catalog |
| `test_case_14_*` | Launch configured and fresh Codex with a model location | The app-server list exactly matches the independent API-compatible parent catalog, includes the dedicated Codex service, and contains no out-of-schema models |
| `test_ug_claude_headless_prompt_argument`, `test_ug_claude_headless_prompt_stdin`, `test_ug_claude_headless_prompt_after_separator` | Run Claude from a script using each prompt form | Structured final answer contains the file value; exit zero; no routing |
| `test_ug_claude_headless_allow_all_bedrock_provider` | Launch Claude through the Bedrock MPS with `allow_all_targets` and no declared targets, explicitly selecting `us.anthropic.claude-sonnet-4-6` | Structured final answer contains the file value; inference completes through Bedrock; no routing |
| `test_ug_codex_headless_prompt_argument`, `test_ug_codex_headless_prompt_stdin`, `test_ug_codex_headless_prompt_after_separator` | Run Codex from a script using each prompt form | Completed turn and final answer contain the file value; exit zero; no routing |
| `test_ug_copilot_headless_completes_file_task`, `test_ug_copilot_headless_gpt_responses_task` | Run Copilot CLI from a script (`-p ... --output-format json`) on a Claude model (Anthropic Messages) and a GPT model (Responses) | Completed turn whose assistant message contains the file value and was sent as the requested model; the session started on the matching registry provider; no file under the session home holds the Databricks credential; no session registry left (non-blocking CI lane) |
| `test_ug_copilot_one_setup_runs_claude_then_gpt_by_provider_ref` | One configure, then headless tasks on a Claude model older than its family's newest (only the full catalog registers it) and a GPT model chosen as `dbx-responses/<model>` | Each answer contains its file value and was sent as the requested model; sessions started on `dbx-anthropic` and `dbx-responses` (non-blocking CI lane) |
| `test_ug_copilot_unknown_model_lists_available_models` | Interactive launch with a model the workspace doesn't have | Exit 1 before Copilot starts; stderr names the model and lists the available ones, without `claude-opus-4-1`; no session registry left (non-blocking CI lane) |
| `test_ug_copilot_tui_switches_from_claude_to_gpt_with_model_command` | TUI: answer folder trust, Claude task, `/model dbx-responses/<model>`, GPT task, `/exit` | Both answers sent as the Claude then GPT model; the session records the switch; normal exit; no session registry left (non-blocking CI lane) |
| `test_ug_copilot_refreshes_a_revoked_token_mid_session`, `test_ug_copilot_reports_expired_sign_in` | Sign in through `DATABRICKS_BEARER_COMMAND` minting short-lived PATs; mid-task the agent revokes them (or also signs out) | Revoked: a token is minted after the revocation and the task completes without restarting Copilot. Signed out: output shows "Databricks sign-in expired" with `databricks auth login`; the failure is in `~/.ucode/logs/copilot-proxy.log`, not the terminal (non-blocking CI lane) |
| `test_ug_configure_copilot_repeat_and_revert` | Configure Copilot twice over a user-owned `~/.copilot/mcp-config.json`, run a GPT task, revert twice | Task completes; no file holds the bearer; revert removes `~/.copilot/ucode.env`, leaves the user file byte-identical and reports Not Configured. (non-blocking CI lane) |
| `test_ug_copilot_upgrade_scrubs_token_saved_by_older_ug` | Configure Copilot with ug v0.2.0 (pinned commit; saves the bearer in `~/.copilot/ucode.env`), then configure with this ug, run a GPT task, revert | v0.2.0 left the bearer; after this ug's configure no file holds it; task completes; revert leaves no token and no `ucode.env` (non-blocking CI lane) |
| `test_ug_opencode_headless_prompt_argument` | Run OpenCode from a script (`run --format json --auto`) with an argument prompt | Completed Read tool call; final text answer contains the file value; exit zero (non-blocking CI lane) |
| `test_ug_agents_self_managed_opencode_journey` | Under the injected `managed_workspace_default` config (enables Claude/Codex, not OpenCode): bare configure, `ug agents list`, refused OpenCode launch, `ug agents add opencode`, real headless task, `ug agents remove opencode` (marker `managed_fixture and opencode`, non-blocking CI lane) | OpenCode absent from the list and launch fails with "doesn't enable OpenCode" / `ug agents add opencode`; after add it is listed self-managed and the headless Read task returns the fixture value; after remove it is hidden and refused again |
| `test_ug_agents_admin_managed_guardrails` | `ug agents add` / `remove` on an agent the admin config enables | Add is a no-op noting the admin manages it; remove is rejected with nonzero exit |
| `test_ug_claude_headless_fresh_workspace`, `test_ug_codex_headless_fresh_workspace` | From fresh state, launch an agent with `--workspace` against a workspace with no managed config | Claude Haiku 4.5 and Codex GPT-5.4 Nano read an unpredictable file value through the gateway and return it in a structured completed answer; exit zero; no routing |
| `test_ug_claude_fresh_provider_launch`, `test_ug_codex_fresh_provider_launch` | From fresh state, launch each real agent CLI with `--workspace` and an existing provider | Both CLIs report the selected provider and exit successfully without routing or inference; Claude also checks its provider header. Cases 11/12 cover provider discovery |
| `test_ug_claude_headless_fresh_model_location` | From fresh state, launch Claude in the CUJ7 workspace with `--workspace`, `--model-location ug_e2e.models`, and `ug_e2e.models.claude_haiku` | Haiku reads an unpredictable file value through the selected model location and returns it in a structured completed answer; exit zero; no routing |
| `test_ug_codex_headless_fresh_model_location` | From fresh state, launch Codex in the CUJ7 workspace with `--workspace`, `--model-location ug_e2e.models`, and `ug_e2e.models.gpt_luna` | Luna reads an unpredictable file value through the selected model location and returns it in a structured completed answer; exit zero; no routing |
| `test_ug_claude_preserves_preexisting_managed_family_defaults` | Launch fresh Claude with pre-existing OS-managed family defaults and select the Sonnet family | Every default survives unchanged; the selected family completes a file task through the preconfigured Sonnet service |
| `test_ug_claude_exports_trace_to_configured_table`, `test_ug_codex_exports_trace_to_configured_table` | Configure tracing, complete a headless task carrying a unique trace marker, then wait for ingestion | The configured trace table contains an agent span with the same trace-safe marker and requested model |
| `test_ug_claude_headless_explicit_model_bypasses_routing` | Pass `--model VALUE` / `--model=VALUE` before and after ug's separator with smart routing enabled, without workspace policy | Real file task completes; JSON `modelUsage` reports the requested model with output tokens; no routing wrapper |
| `test_ug_codex_headless_explicit_model_bypasses_routing` | Pass `--model VALUE` / `--model=VALUE` / `-m VALUE` before ug's separator and within Codex `exec`, with smart routing enabled | Real file task completes; completed-turn model matches the selection; no routing wrapper |
| `test_ug_claude_preserves_caller_settings_and_hook` | Pass a settings path containing spaces | Real SessionStart hook executes; caller file unchanged; file task completes |
| `test_ug_claude_reports_unsupported_short_model_option` | Pass Claude's unsupported `-m` | Actual agent error and exit status preserved |
| `test_ug_claude_auth_help`, `test_ug_claude_mcp_help` | Request subcommand help with legacy smart routing off/on | Real agent help; no routing wrapper |
| `test_ug_codex_app_help`, `test_ug_codex_app_server_help`, `test_ug_codex_exec_help`, `test_ug_codex_mcp_help` | Request subcommand help with legacy smart routing off/on | Real agent help; no routing wrapper |
| `test_ug_codex_app_reports_unknown_argument` | Pass an invalid option directly to `ug codex app` with legacy smart routing off/on | Real Codex parser error and status preserved |
| `test_ug_codex_app_server_client_initializes` | Connect a stdio client, direct/`--` separator, with legacy smart routing off/on | Actual JSON-RPC initialize response; no non-JSON stdout; no routing |
| `test_smart_routing_claude_route_subagent_hook`, `test_smart_routing_codex_route_subagent_hook` | Pipe a native spawn payload to the installed Claude mod command or Codex PreToolUse hook with legacy subagent routing or each supported routing preset | Claude returns model/rationale/updatedInput JSON naming the routed agent the mod labels; Codex returns an allow-hook decision; requested model replaced by a routed agent definition (Claude) or bundled catalog slug (Codex) from the offered models; one audited decision matching the session and task |
| `test_smart_router_routes_{claude,codex}_subagent_at_launch`, `test_smart_router_skill_turns_{claude,codex}_subagent_routing_off`, `test_smart_router_skill_turns_{claude,codex}_subagent_routing_back_on` | Configure, launch a real subagent-only TUI with legacy routing-only/orchestration flags or the three subagent-only presets, each in its own fresh session: spawn a tagged child at launch; invoke the installed Smart Router skill to turn routing off then spawn a child; or spawn a routed child, turn routing off and spawn an unrouted child, then turn routing back on and spawn a routed child | Routing-only cases install `smart-router`; orchestration cases also install `smart-router-orchestrator`; each tagged native child completes; routed children (launch, before off and after back on) show a native model/rationale row (Claude) or subagent banner (Codex) and produce a live routing decision correlated with the child, while each child after off shows neither and has no decision; Claude's recorded task inference payloads carry `smart_router_recipe_name` as the launch recipe (`at_launch`, `back_on`, where the second child must match the launch child) or DISABLED (after off), omit the recipe header, and receive successful gateway responses; opted-in Codex banners include `[orchestrator on]`; no first-prompt routing wrapper; the customer full-mode preset is intentionally outside this journey; normal exit |
| `test_ug_configure_claude_repeat_and_revert`, `test_ug_configure_codex_repeat_and_revert` | Configure twice over user settings; complete a task; revert twice | Settings preserved; no bearer in ug state; generated config removed; status unconfigured |
| `test_ug_configure_claude_cleans_stale_skills_mcp_on_workspace_switch` | Configure the first workspace, register its skills MCP, switch to a second real workspace, and use Claude | One cross-workspace removal reported; old registration removed from Claude and the new workspace state; old workspace bucket preserved; repeat configure stays clean; real file task completes on the second workspace |
| `test_ug_configure_claude_rejects_invalid_credentials`, `test_ug_configure_codex_rejects_invalid_credentials` | Configure with a rejected bearer against the real workspace | Authentication failure; no successful saved setup |
| `test_ug_configure_managed_claude`, `test_ug_configure_managed_codex` | Configure on the managed workspace with the stubbed `managed_workspace_default.json` CodingAgentConfig | No agent selector; each agent's generated config exposes exactly the admin's static model_services; real gateway prompt on launch. The Codex case also checks the shared catalog pointer, restart guidance, and a fresh bare app-server's visible model list |
| `e2e_cuj/test_cuj5_budget_defaults.py` | Launch bare `ug` with separate low-spend and above-tier principals against the fixed 1% tier (`spending_percentage=0.01`); explicitly launch `ug claude`, and `ug claude --model`, above the tier | Bare launches check Claude/Sonnet below the tier and the Codex/Luna recommendation above it; `ug usage` agrees with backend spend, threshold, and percentage. Explicit `ug claude` displays the backend's Codex/Luna recommendation while its generated model setting and native header select Sonnet. `ug claude --model <Sonnet id>` (the workspace's only Claude model) displays the same recommendation, selects Sonnet in the native header, and leaves the user and OS-managed settings files byte-identical to configure's (Sonnet `ANTHROPIC_MODEL`); a following plain launch selects Sonnet without rewriting them. A differing Claude budget pick, alone or with smart routing, is unit-covered only. No budget writes or inference tasks. Runs on its own `E2E CUJs · CUJ 5 · Budget-driven defaults` runner with both credential pairs documented in `integration/README.md`. |
| `test_case_01_*` | Launch managed Claude without defaults after configure and from fresh state | Claude receives the admin MPS header; its gateway cache and replacement picker match the independently fetched provider model IDs; catalog labels are preserved and a model appears in a numbered picker row |
| `test_case_03_*`, `test_case_05_*` | Pass a provider or model-location override to managed Claude after configure and from fresh state | ug rejects the override before Claude starts and preserves agent-owned state |
| `test_case_02_*` | Launch managed Codex after configure and from fresh state | The scoped and stable catalogs, ug-launched app server, and fresh bare app server match the independently fetched admin MPS model IDs. The configured case uses real `ug revert` to remove ug's shared pointer and stable file while preserving a user setting |
| `test_case_04_*`, `test_case_06_*` | Pass a provider or model-location override to managed Codex after configure and from fresh state | ug rejects the override before Codex starts and preserves agent-owned state |
| `test_ug_configure_managed_codex_catalog_fallback` | Configure from an injected managed response containing a GPT model absent from Codex's bundled catalog, then inspect the picker with legacy smart routing off/on | Actionable metadata warning; conservative catalog entry for the unknown model; real Codex picker lists the custom catalog model |
| `test_managed_fixture_codex_http_headers_in_managed_file` | Interactive PTY configure with injected managed `http_headers` for Codex | The specified header (`x-databricks-workspace`) lands in `model_providers.Databricks.http_headers` in `/etc/codex/managed_config.toml` with the exact admin value |
| `test_managed_claude_mps_defaults_accompany_discovery`, `test_managed_claude_parent_schema_defaults_accompany_discovery` | Configure from a stubbed config and launch Claude with MPS discovery (`main.default.ci_e2e_anthropic_mps`) and with `system.ai` Unity Catalog discovery, respectively, both on the managed workspace | Both generated settings files retain every admin-authored default alongside the source header and every independently fetched catalog model with its label; MPS pickers keep family shortcut rows separate from catalog entries; only UC Opus/Sonnet family ids gain `[1m]` |
| `test_launcher_sessions_prompt_at_most_once` (CUJ8) | Three launcher sessions: a second writer applies a launcher's OS-managed field set on drift, then a real smart-routed `ug claude` TUI with the launcher's args and env opens `/model` and runs a task | At most two privileged writes in the first session and none after; no ug password notice after the first session, warnings, or errors; writer's picker shown; every writer field unchanged except ug-owned ones; no discovered defaults in either settings file; orchestrator on; inference served on the writer's Haiku default; `ug revert` restores the writer's file exactly |
| `test_unmanaged_claude_preserves_preexisting_family_defaults` | Seed Claude's OS-managed family defaults, then configure against one real workspace verified to have no managed config | Every pre-existing Claude family default remains unchanged in the OS-managed settings file |
| `test_managed_fixture_claude_model_lifecycle`, `test_managed_fixture_codex_model_lifecycle` | Configure across no config -> static A -> static B -> MPS -> no config (stub-injected, `null` for no-config; MPS via a real provider service) | Each agent's model files reconcile to each static config (removed models pruned); switching to an MPS and a workspace with no managed config clears ug's static picker/catalog so no stale list is enforced |
| `test_ug_installed_wheel_exposes_help_and_version` | Invoke freshly installed console command | Package version matches; public help works |
| `test_ug_status_in_fresh_home_is_unconfigured` | Request status before configure | Unconfigured status |
| `test_ug_auth_without_configuration_explains_how_to_configure` | Request auth before configure | Actionable setup error and nonzero exit |
| `test_ug_and_ucode_auth_helpers_emit_only_the_supplied_bearer` | Run both auth helper commands with the public bearer override, with and without forced refresh | Exact token-only stdout, no warnings or ANSI escapes; no workspace authentication or saved state |
| `test_ug_and_ucode_web_search_helpers_preserve_mcp_stdio` | Initialize and list tools through both web-search helper commands | Exactly the MCP JSON-RPC responses; no text/ANSI contamination; existing server/tool identities preserved; no model request |

With Claude and Codex selected there are **88 live cases** (12 marked TUI cases),
**1 two-workspace case** (marker `workspace_switch`),
**44 managed-fixture cases** (marker `managed_fixture`, with only
the CodingAgentConfig input injected from a JSON file in `fixtures/managed_config/`), and **7 installation checks**. The 14 retained numbered scenarios
comprise **24 explicit journeys**: 12 managed configured/fresh executions and 12 unmanaged
executions. The remaining managed-fixture cases cover focused model, MCP, skills,
saved-config replay, and lifecycle shapes, including two Claude defaults cases. Parametrization varies
argument spelling or external routing selector, never hides the agent/provider in the test name. Duplicate boot-only cases
are incorporated into the Databricks configuration TUI journeys.
Generated-file cleanup and strict app-server stdout assertions remain enforced.
Unmanaged discovery Cases 7–14 configure, list models, or open the picker without
submitting inference prompts; separate task journeys still perform inference.
They require a real workspace with no CodingAgentConfig; a read-only prerequisite
check reports any published config rather than bypassing it. `UG_ENABLE_MODEL_DISCOVERY`
is not supported on current main. Its duplicate managed variants and obsolete
unmanaged disable scenarios are removed; Cases 1–6 cover managed discovery and
override rejection. Cases 7–10 cover automatic/default launches.
Configure-time model locations are also unsupported; Cases 13–14 cover the supported
launch-time `--model-location`. Repository scenario numbers run consecutively from
01 to 14; configured/fresh variants share a number. External design-document
numbering is unchanged and is not the source of these repository IDs.
Managed Codex state comparisons exclude `.codex/tmp/arg0`, the disposable executable
links recreated by Codex version checks; persistent agent files remain compared.
Claude discovery assertions match numbered picker rows, not startup banners or
footers. Offline regressions cover that distinction, native Haiku/Opus/Sonnet
deduplication, and raw catalog ID/display-name rows for scoped pickers.
Managed discovery expectations come from separate read-only, provider-scoped
model-list requests; they do not rely solely on ug's generated catalog.

The dedicated-workspace CUJ suite adds three independently runnable CUJ2 MPS+MCP cases:
one configuration case and separate Codex and Claude TUI task cases. The workspace is
preconfigured for CUJ2 and is read-only; every case validates it, and the fixture compares
it again during teardown. The TUI cases forward agent requests through the shared per-test
loopback recorder to the real workspace and assert the provider-service header, model,
paired HTTP 200 response, and a nonempty final answer in the completed parent turn matching
the submitted greeting prompt. Request IDs correlate recorded traffic without constraining
answer text. Offline evidence checks reject empty, incomplete, unrelated and child-session
answers while retaining hidden-value requirements for file tasks.
The existing direct Anthropic non-relay MPS e2e check also requires a complete Messages
response: assistant role/type, the pinned model, `end_turn`, and nonempty text.
The configuration case verifies sandbox MCP
selection and generated client listings; live sandbox execution remains deferred because
MAS cannot downscope the CI service principal. CUJ1 and CUJ2 also initialize Claude Desktop's
Linux profile path and verify that its profile uses, respectively, the published Databricks-hosted
Claude models or Anthropic provider service and model. This is installed-CLI coverage of real
published managed configs, not Desktop GUI or helper execution. Failed TUI runs
retain terminal output/actions/screen and best-effort agent transcript snapshots under the
runner artifact directory before the temporary homes are deleted. The suite requires
`UG_CUJ_SP_CLIENT_ID` and `UG_CUJ_SP_CLIENT_SECRET`, with `ug`, `claude`, `codex`, and
`databricks` on PATH; run it with
`uv run --with pexpect==4.9.0 --with pyte==0.8.2 pytest --confcutdir=tests/e2e_cuj tests/e2e_cuj`.
Pytest prints the per-test artifact directory; the fixture forwards only its short-lived
bearer to the isolated session. It reads the published config as an input only and does not
use `UCODE_MANAGED_CONFIG_STUB`.

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
suite runs all 88 live cases across two parallel agent jobs: one Claude VM and one
Codex VM, each running its configure, headless, and commands/lifecycle cases
serially. Each agent is installed once for the full suite, and no two full jobs
for the same agent overlap within a run.
CUJ7's five journeys collect eight cases in the required
`E2E CUJs · CUJ 7 · Unmanaged model discovery` shard: configured Claude picker and
Codex app-server discovery, fresh Claude/Codex model-location file tasks, and Claude
managed-family-default preservation on launch. Task journeys cover model selection before
and after ug's separator. Each case reverts its local setup. The class
pins `WORKSPACE_URL` to `https://dbc-14e376e8-6541.cloud.databricks.com` rather than using
a workspace secret. The workspace must publish no `CodingAgentConfig`, expose discoverable
`system.ai` models, and retain the existing models `ug_e2e.models.claude_haiku`,
`ug_e2e.models.claude_sonnet`, and `ug_e2e.models.gpt_luna`; the shared service principal
needs read/use privileges on all three.
CI starts integration alongside unit tests and the existing e2e shards. Integration
does not wait for agent e2e or get skipped when an agent shard fails. These suites
share workspace capacity; overlapping their requests can still encounter rate limits.
An advisory native Windows lane runs the five fresh-install, CLI, auth-helper,
and local MCP checks in `test_installation.py` without credentials. The two POSIX
version-floor journeys are outside this Windows subset. It uploads separate evidence but remains
non-blocking while initial native Windows issues are diagnosed.
An advisory Windows headless job reuses the Claude prompt-argument journey:
public configure, real launch, and a completed gateway-backed file task.
It installs the shared CI Claude version and uploads independent evidence. Codex's Windows CI
journey is deferred while its npm proxy access is blocked. Native TUI
and managed-settings coverage remain deferred; a green installation check alone
does not establish a successful live task.
The `All integration tests` check requires every selected integration job to pass, including
both managed-config lanes for full/live runs; full coverage does not depend on a label or
a manual request.

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

The existing e2e workflow runs seven parallel shards: gateway checks plus one for
each of Claude, Codex, Gemini, OpenCode, Copilot, and Pi. Each agent shard installs
its own CLI. Configure-subset checks run in the Claude shard because configuration
invokes the Claude CLI. The `All agent tests` check requires every shard to pass.
Copilot's per-model greeting smoke uses Anthropic Messages for Claude, Responses for
GPT-5+, and Chat Completions for other models. Existing
Codex-specific, and Grok exclusions remain. `test_agent_copilot.py` covers API
selection, model-override precedence, persisted configuration, and token refresh
locally; it does not establish live inference or in-session model switching.
Check names describe the coverage: `Unit tests`, `Gateway API tests`,
`Agent launch tests · Claude`, `Smoke journeys · Claude`, and
`Full journeys · Claude` (with the other agents named likewise).
Unit tests still run as one job. `E2E CUJs · CUJ <N> · <topic>` runs one runner per
`tests/e2e_cuj/test_cuj<N>_*.py` file, because CUJs share machine-wide `/etc` agent
settings. All matrices use `fail-fast: false` so one failure does not cancel other coverage.

The small `test` and `e2e` compatibility gates retain the exact status contexts
required by the repository's branch rules. `test` requires `Unit tests`; `e2e`
requires both `All agent tests` and the complete integration workflow. A failed
or skipped dependency fails the gate, and a running integration suite keeps it
pending. The advisory Windows installation and headless lanes are not part of `All integration
tests` yet. The descriptive jobs provide the actual coverage and diagnostics.

Fork PRs get no secrets, so their `pull_request` run skips every credentialed integration
job. After reviewing a PR, an org member comments `/integration-test` to run the Integration
workflow with secrets on the PR's current head commit, or `/integration-test <sha>` to run
only if the head is still the commit they reviewed. It posts a `Fork integration` status,
and works on in-repo PRs too as an on-demand rerun. Each new push needs a new command.
It runs main's workflow definitions and never caches fork builds. `/ug-review` requests
Open Code Review with the trusted UG rubric and inline findings.
It reuses the review service principal through the Databricks gateway's Anthropic route.
Adding either `ug-review` or `code-review` also requests
review on in-repository PRs; fork PRs still require the member comment because
their PR-triggered runs cannot access the gateway secret. Label removal and
ordinary pushes do not rerun reviews. `test_ug_review_bot.py` covers gateway authentication,
OIDC-authenticated npm-proxy configuration and credential cleanup, and the workflow contract,
not live inference or GitHub posting. The old custom
Responses/diff/parser/summary-rendering tests are obsolete because that implementation
is removed. `test_polly_review.py` covers the Polly review workflow contract and its
summary helper. `/user-journey-check` runs the user-journey gate,
which runs automatically only for in-repo PRs.

## Gaps and deferred scope

Custom OAuth lock tests cover release after use, Windows contention retries, and
permanent lock-error propagation. Native Windows browser consent and concurrent
OAuth helpers still need platform validation.

`test_file_lock_cross_os.py` exercises the shared timeout mechanism used by Desktop:
real contention, preserving the holder after timeout, and acquiring after release.
New lock-wait limits are scoped to Desktop profile updates; existing non-Desktop
callers retain their prior behavior.

Gateway proxy tests exercise a busy cached port falling back to a free port with
address reuse disabled, plus the platform-specific reuse policy. Linux runs do
not establish native Windows socket behavior.

`test_managed_files.py` covers a lazy sudo worker shared across multiple managed-file writes,
no elevation for unchanged files, target/symlink rejection, bounded shutdown and cancellation,
and real-shell copy/rename failure handling in temporary directories.
`test_cli.py` covers the workspace configure session boundary.
These are unit/component checks; they do not establish live sudo password-prompt behavior.

Codex desktop OTEL component tests cover the systemd unit, the macOS LaunchAgent, and the
Windows scheduled task (`launchctl` and `schtasks` are simulated), reuse of CLI proxy/auth,
fixed-port collisions, setup failures that only warn, unchanged launches that keep the
service running, and leaving another exporter alone. They do not establish a live
desktop GUI, a real `launchctl` or `schtasks` run, or a login/reboot pass.

`test_managed_config.py` and `test_managed_files.py` cover machine policy versus user-owned
`refresh_ts.json`, launches replaying the saved read without a request, no privileged write for unchanged policy or
retrieval-only drift, configure-only sudo selection, deferred-update warning/fallback, matching
removal, workspace isolation, fingerprint invalidation, and dry-run behavior.
Pending-change regressions cover one warning across rechecks, distinct-change warnings,
retrieval/key-order stability, removal, and clearing after remote/MDM/configure reconciliation.
Workspace-lock regressions in `test_managed_config.py` and `test_cli.py` refuse foreign workspaces
before cache/auth/bootstrap, including bare/agent launches, dry-run, and skip-preflight.
`test_ug_managed_machine_refuses_foreign_workspace_launch` adds the black-box managed journey
(stubbed managed config): configure followed by rejected bare/Claude/Codex workspace overrides, with policy,
refresh metadata, and user state unchanged. This new live case has not been run locally.
`test_ug_managed_config_launch_replays_saved_config` is the live journey for root-owned `0644`
policy, a user-owned `0600` record with a `published` outcome and no timestamp, and unchanged
policy and record bytes (inode/mtime/ctime for the policy) across two launches. The record is
backdated first, so the removed five-minute refresh would have refetched and rewritten it. Its live sudo/password behavior still requires a disposable clean VM.
Non-root writer regressions cover terminal/script launches, initial install and removal, and
verify that neither a sudo capability probe nor a prompting worker is started.

| Scenario | Status / requirement |
| --- | --- |
| Broad live MCP functionality | CUJ2 covers sandbox MCP configuration and generated client listings. Live sandbox execution is deferred because MAS cannot downscope the CI service principal. Installation tests cover the local web-search MCP handshake and tool listing, not upstream proxying or a real search request. Other MCP services, live parent/child search, and permission decisions remain deferred |
| Dedicated CUJ Skill coverage | CUJ6 covers the named managed skill `ug_e2e.skills.fixture-summary`: download to both skill roots, an unchanged repeat configure, and removal from disk and from both agents' `/skills` once the selector is dropped, including after relaunch. Skill fixtures need a schema on customer S3 storage because Default Storage rejects skill uploads. CUJ2 checks that only the named skill downloads, byte-identical to the live fixture, is listed in `/skills` without its siblings, and is invoked by both agents. CUJ3 checks that every skill in `ug_e2e.skills` downloads and is listed, the `ug_e2e.other_skills` decoy stays absent, `ug status` counts the skills, and both agents invoke one |
| Broad configure flags, multiple workspaces, and PAT flows | Deferred while focusing on basic CUJs |
| Workspace-switch MCP cleanup | The `workspace_switch` CUJ covers real registration, cleanup, repeat configure, and a completed Claude task. Unit/component tests cover duplicate attempts and injected removal failures; the CUJ does not force an agent timeout. It runs in the required managed CI lane for full/live runs. |
| Relayed/subscription MPS discovery | Not covered by the scoped discovery journeys |
| Saved Claude picker selection | Unit/component checks in `test_agent_claude.py` cover mapped family aliases, `[1m]`/`[200k]` variants, and unavailable-model fallback without changing saved settings. No dedicated live journey. |
| Fresh provider/parent validation and mixed Bedrock filtering | Not covered after removing the duplicate model-discovery suites |
| TUI initial prompt supplied on the launch command line | Not yet covered; headless prompt arguments are covered |
| Follow-up turns and conversation resume | Not covered; reopen proves startup, not conversation resume |
| Claude/Codex interactive smart routing | First-prompt routing through `first_prompt_and_subagent_no_orch_v0` is covered by the `managed_fixture` banner journeys and CUJ4's preset journeys. CUJ4's managed journeys verify that `smart_routing.enabled` alone leaves both first prompts unrouted and turns on Smart Router Orchestrator only for Claude; the managed-routing skill-toggle cases use Claude's default and an explicit routing version for Codex. `--enable-smart-routing` has component coverage only. The live subagent-only journeys spawn native children and invoke the installed Smart Router skill to verify routing at launch, off, and off -> on in separate Claude/Codex sessions. The scheduled harness-latest workflow repeats CUJ4, routing hooks, skill toggles, and banners with both latest agents. Plugin-refresh survival and native daemon/background dispatch remain deferred. |
| Full allow/deny tool-permission matrix | Not covered; onboarding/trust uses actual TUI choices |
| Desktop Codex app, Isaac itself, auto-upgrades | Not covered by command forwarding or pinned-version tests. Codex desktop's shared-config catalog pointer (repeat configure and a user-owned pointer) is covered by `integration/test_ug_configure_codex_desktop.py`; desktop tracing, GUI, and inference are not |
| Native macOS/Windows live TUI, managed settings, resize/signals | Windows fresh-install, CLI, local helpers, and the Claude headless gateway journey are advisory; Codex Windows CI is blocked on npm proxy access. Live PTY/TUI, managed settings, and signal behavior still need separate platform implementation and coverage |
| Other agents | Live scope is Claude Code and Codex, plus a non-blocking OpenCode headless case |
| OpenCode `--model` / `-m` | Unit/component tests cover raw CLI forwarding, configured model selection, launch arguments, rejection of unknown models before starting the native process, and the existing failure when no default model is available; no dedicated live integration journey |

See [integration/README.md](integration/README.md) for commands, CI, artifacts,
and reproduction. Follow [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md) when
adding, modifying, or removing tests. The ordinary suite enforces both the
no-mocking boundary and the Scenario/Expected docstring format.

Native launch-option component tests (`native_claude/test_mod.py`) check two real Claude
processes with different resolved routing baselines. Python cases cover Claude-specific presets,
unknown selectors, and absent versus empty flags. These checks do not establish gateway/TUI coverage.

The real session-control process call is covered by the live skill-toggle journeys;
native component tests stub the subagent routing process protocol. No component
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
The expected launch recipe comes from the recorded routing request, so a default
recipe change does not require editing the test.
Local Claude mod confirmation replaces the old saved-control-file/assistant-turn wait;
Codex retains its CLI control-file checks. No extra TUI sessions or tasks are added.
Clear/reload, compaction, and denied-write rollback are not independently checked.
A native process check runs the real disabled routing CLI from Claude with inherited
launch flags; it makes no gateway request.

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
