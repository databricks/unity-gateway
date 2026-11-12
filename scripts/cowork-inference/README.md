# Cowork desktop tests

The `cowork-inference-windows` and `cowork-inference-macos` jobs in `.github/workflows/integration.yml` exercise real
Claude Desktop inference, local file reads, and native skill execution through Unity Gateway
on disposable Windows and macOS runners. All three tasks run sequentially in each blocking job.
It runs after lint for same-repository PRs, main pushes, scheduled CI, and manual
CI dispatches. It skips merge-group runs and fork PRs, which lack the required
credentials. Authorized fork-integration runs test the pinned commit.
The job is required by the integration gate, which feeds the required `e2e` check.
Desktop jobs always test the checkout with their pinned dependencies. Manual UG/CLI
version, package-index, entry-point, and dependency overrides apply to CLI jobs only.
Every integration job must succeed; failure, cancellation, or a skip blocks the gate.
A [20-run fresh-Windows soak](https://github.com/databricks/unity-gateway/actions/runs/42240639887)
passed without retries before enabling the inference gate. File/skill tasks have separate
live validation; that soak does not establish their flake rate. Whole-test retries are not used.

The test installs a signed, SHA256-pinned Claude MSIX and drives the real app
through Windows UI Automation. `--force-renderer-accessibility` exposes the app's
accessible controls. Controls are polled until enabled and visible, with a 60-second deadline per
transition. No preliminary launch or virtualization check is needed.

The test installs UG from the checkout, runs `ug configure` with the existing
`UCODE_TEST_WORKSPACE` and `DATABRICKS_BEARER` CI secrets, and checks the generated
gateway profile. UG detects the installed Windows MSIX package and executable,
then creates `%LOCALAPPDATA%/Claude-3p/configLibrary` before the first launch.
The test does not create that directory or write UG's configuration itself.
UG allows 30 seconds for the Windows package query and distinguishes a missing
installation from a query timeout, PowerShell launch failure, or package-query error.
Detection failures still fail the test; the inference assertion is unchanged.

Every step below runs once per Claude model source on each OS:

| Source | `ug configure` | Target model | Windows lane | macOS lane |
|--------|----------------|--------------|--------------|------------|
| Databricks Hosted | no `--provider` | `system.ai.claude-haiku-4-5` | `cowork-inference-windows` | `cowork-inference-macos` |
| Anthropic MPS | `--provider main.ucode.dont_delete_ci_company_anthropic_mps` (staging) | `claude-haiku-4-5-20251001` | `anthropic-mps`, Windows Claude row | — |
| Anthropic MPS (legacy) | `--provider main.ucode.ci_e2e_anthropic_nonrelay_mps` | `claude-haiku-4-5-20251001` | — | `cowork-inference-macos` |
| Anthropic relay MPS | `--provider main.ucode.ci_e2e_anthropic_relay_mps` | `claude-haiku-4-5-20251001` | `cowork-inference-windows` | `cowork-inference-macos` |

The non-relay service lives in the staging workspace that the CLI `anthropic-mps` tests use.
Staging rejects GitHub-hosted runners, so the Windows Desktop tasks run as extra steps of that
lane's Windows Claude row on `databricks-protected-runner-group`. These steps are in the
shared live steps but run only where the job sets `UG_COWORK_DESKTOP`, and they skip
merge-group runs like the other Desktop lanes. That runner reaches packages only through the
internal PyPI and npm proxies; the Claude installer still downloads directly. Desktop's
credential helper authenticates with the lane's `ug-ci-sp` service principal through
`DATABRICKS_BEARER_COMMAND`, which reruns `bearer-token.py` to mint a fresh M2M token on each
fetch, so long tasks don't outlive the one-hour token. No protected macOS runner exists, so
macOS keeps the legacy non-relay service in the e2e workspace. The configuration check
requires the profile's `Databricks-Model-Provider-Service` header to equal the selected
service (absent for Databricks Hosted), and requires the credential helper's `--relay` and
`--claude-binary` arguments exactly when the source is a relay.

The relay entry alone receives the existing `CLAUDE_CODE_OAUTH_TOKEN` secret (a long-lived
`claude setup-token` subscription token) in every step that launches Desktop. Desktop
inherits it and passes it to UG's relay credential helper, which returns it as the inference
token without running `claude auth`, plus the Databricks token in the separate gateway
header. No Claude.ai sign-in or browser is involved. When the token expires, every relay
entry fails until the secret is rotated.

The test accepts gateway onboarding and opens Cowork. Desktop opens on its own default
model, which is the first configured model: that is the target for Databricks Hosted and
the single-model Anthropic MPS, but not for the relay MPS, which lists every Claude model.
When the picker shows another model, the test selects the target label in the model
picker, then requires `Model: <label>`; the opening model is recorded in the evidence.
It submits a number-conversion prompt and requires both `Claude responded: 48213`
in the conversation pane and `Claude finished the response` within four minutes.
The prompt spells the number in words so echoing the input cannot pass.

After inference, three fresh Cowork tasks run through the same native UI:

1. Create `Documents/CoworkProbe/input.txt` with a random value absent from the prompt.
   Select that folder through the native picker and choose **Allow** for this task.
   Require a successful native `Read` call on that exact file, its contents in the
   matching tool result, and an identical completed assistant answer.
2. Install a local test skill through native `claude plugin ... --cowork` commands,
   using the account directory created by the inference task. Its instructions refer
   to a file containing another random value. Require successful `Skill` and `Read`
   calls, the reference file's value in the tool result, and an identical completed answer.
3. The hosted Desktop jobs set `UG_COWORK_MCP`, so the initial `ug configure` also passes
   `--mcp system.ai.web_search` (the service in `task-fixture.json`). The task requires the
   Desktop profile's `web_search` connector to be UG's stdio proxy for that
   service. Ask Cowork to search for Databricks' official website with that connector. Require a
   successful `mcp__*__web_search` call with a nonempty query whose result contains a
   `databricks.com` or `www.databricks.com` URL, and a completed answer repeating one of those
   URLs, ignoring Markdown and trailing slashes. Search ranking and wording are not asserted;
   Desktop's native web search cannot satisfy it. If Desktop asks before the connector's first
   use, the task approves it and records the label. This runs in the hosted Windows and macOS
   Desktop jobs, not the staging `anthropic-mps` lane.
   UG writes the connector's absolute command from the `ug` on `PATH`, so both install scripts
   add the test runtime to `PATH`, as an installed `ug` would be.

`session-evidence.py` reads the native Desktop transcript belonging to the submitted
prompt and requires an assistant `end_turn`. Failed tools, mismatched result IDs,
wrong files, unrelated sessions, and echoed prompts cannot satisfy these assertions.
`tests/test_cowork_session_evidence.py` exercises these rejection cases offline.
Each task selects and checks the target model the same way, has a five-minute response limit, and
stops Desktop before the next task. The skill is an ordinary user-authored plugin;
this does not verify Unity Catalog skill downloads or UG's downloaded-skill propagation.
On macOS, Desktop copies installed plugins into its native runtime directory. The test
identifies the copy by matching both the skill instructions and random reference-file bytes
against the installed fixture, then requires a Read of that exact runtime path. Evidence
stays pending until that copy exists; ambiguous copies fail. Both platforms build prompts and
the skill fixture from `task-fixture.json`. File-path comparisons remain case-sensitive on macOS.

## Dependencies

The official Claude Desktop 2.42304.2 x64 MSIX is pinned by URL and SHA256 once in
`integration.yml`'s workflow-level environment and installed by `download-desktop.ps1` in both
Windows lanes; the installer script also verifies the Anthropic signature. Installer
provenance comes from Microsoft's winget-pkgs catalog:
`manifests/a/Anthropic/Claude/2.42304.2/Anthropic.Claude.installer.yaml`.
Direct official Anthropic installer/runtime downloads were approved for these
disposable CI tests.

Python dependencies come from PyPI and Claude Code CLI 2.1.290 from public npm,
matching the existing GitHub-hosted Windows CLI integration tests. These public
package sources are approved for this CI job. Python 3.12 is selected explicitly;
Claude Code's native binary selection requires its npm postinstall script.
Package installation needs no JFrog credentials or GitHub OIDC permission. The staging lane installs the same
versions through the internal PyPI and npm proxies its CLI tests already use.

Desktop and CLI versions are upgraded deliberately. For Desktop, update the official
URL and SHA256 together, keep signature/version checks, and run the native job on each
affected OS before merging. Desktop has a separate release cadence from Claude Code;
a CLI pin change does not update the app.

## Evidence and limits

Artifacts contain installer provenance, configuration status, UI control names,
window screenshots, native launch logs, and redacted task transcripts/results. Raw process logs stay outside the
artifact directory. Known bearer and subscription-token values, authorization bearer strings, `sk-ant-`
tokens, and JWT-shaped tokens are redacted before writing text artifacts; app profiles and credential files are not archived. Artifacts expire
after three days.

The named `desktop-ci` Databricks profile contains host/auth metadata, not a
token. No Claude.ai login is used. The test verifies the configured provider/model
and desktop response; it does not independently trace gateway HTTP requests.
Code-tab, MCP services other than `system.ai.web_search`, file writes, and Cowork VM readiness are
outside these tests.

## macOS

The `cowork-inference-macos` job runs the same inference, file-read, and skill journeys on a disposable
GitHub-hosted `macos-15-intel` runner. It has the same event guards and
required integration gate as Windows. Apple Silicon hosted runners are not covered:
Cowork was unavailable on the `macos-15` runner in the feasibility probe.
No dedicated Mac is required.

The macOS installer is Claude Desktop 4.23118.0, downloaded from Anthropic's official
universal ZIP. `macos/install-desktop.sh` pins its URL and SHA256, verifies its
code signature and Anthropic team ID, runs Gatekeeper assessment, and checks the
installed version. Dependencies use the same approved public npm/PyPI path as Windows.

`macos/ui.swift` drives the installed app through native macOS Accessibility APIs.
The Python harness runs through Terminal to inherit the hosted image's existing
Accessibility authorization; it does not modify TCC or macOS security settings.
Folder-picker keyboard input uses System Events, as in the existing Codex macOS harness.
Accessibility checks and clicks stay scoped to the active native dialog. A missing permission
fails the job. UG configures the app through its public CLI,
then the test accepts gateway onboarding, opens Cowork, verifies the configured
model, sends the prompt, and requires both the exact answer and completion marker.
Navigation supports the Cowork tab and the task screen exposed under Claude in the
updated UI. It requires Scheduled, the prompt field, and Start task together before
proceeding; a tab name alone does not establish readiness. Start task may remain
disabled until the prompt is entered, and submission still waits for it to be enabled.
JSON snapshots preserve multiline control values. Controls wait for an enabled exact label;
individual Accessibility timeouts retry only within the current wait's remaining budget.
Response assertions wait up to four minutes. The target model must have a unique display
label in the generated profile so another model cannot satisfy the selection check.
There are no whole-test retries.

Terminal receives only the required environment through an owner-only temporary
file, which the child deletes after reading. Raw logs stay in that private directory
with owner-only permissions until redacted. The probe has a 15-minute deadline;
the parent allows another 90 seconds for evidence capture and 30 seconds for startup.
On a parent timeout, it signals the probe first and allows up to 90 seconds for
final diagnostics before terminating the remaining process group. Cleanup errors
fail the run while still attempting to save sanitized logs and remove temporary
credentials. An always-run workflow step also stops Claude and Terminal. Artifacts contain only
sanitized diagnostics, installer provenance, UI snapshots, and screenshots, with
three-day retention. Configuration and credential files are never uploaded.

`tests/test_macos_cowork_harness.py` checks redaction, private log permissions, both
navigation layouts, missing task controls, bounded Accessibility retries, unambiguous
model labels, and rejection of partial, incorrect, or unfinished responses. Component
tests cover launch, cleanup, and evidence-write failures, plus graceful termination of
a real isolated subprocess.
These checks do not replace running the native macOS job.
