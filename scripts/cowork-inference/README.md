# Windows Cowork inference test

The separate test branch also includes `windows-native-contracts.yml`. It reuses this
installer, configure flow, and accessibility driver on disposable Windows runners:
bare CLI enterprise skill, Desktop Code enterprise skill, Cowork enterprise skill,
and Cowork organization-plugin skill plus hooks. A combined-source case requires one
native inventory entry when enterprise and organization-plugin copies coexist.
Each task must invoke the skill once,
read its reference file, and return a random value absent from the prompt. The organization
plugin must execute SessionStart and UserPromptSubmit once for that native session.
Standalone CLI inventory/version and Desktop transcript engine versions are recorded
separately from the MSIX version. Unsupported loading, UI failures, and missing hooks fail.

These native prerequisite probes use explicitly administrator-authored skill/plugin
inputs, not simulated workspace bundles or UG-generated output. They do not establish
managed skill download, global Desktop publication, separate-user authentication/ACLs,
MCP deduplication, or the configure/update/revert lifecycle. Those checks must exercise
the Windows publisher once native source selection is established. Test changes remain
on their own branch for a later PR; the existing blocking inference job is unchanged.

Native results from November 11, 2026:

- Bare CLI 2.1.290 passed enterprise skill inventory, invocation, reference Read, and
  hidden-file answer in [run 42938900415](https://github.com/databricks/unity-gateway/actions/runs/42938900415).
- Desktop Code 2.42304.2, embedded engine 2.1.299, passed the same checks in
  [run 42942745530](https://github.com/databricks/unity-gateway/actions/runs/42942745530).
  The native inventory contained only `probe-summary`. The driver confirms trust for
  the test workspace, verifies the exact reference path in the native Read request,
  and clicks `Allow once`. This proves execution with explicit native consent, not a
  permission-free read; persistent permissions remain unchanged.
- Cowork organization-plugin skill execution and both hooks passed on Desktop
  2.42304.2, embedded engine 2.1.299, in
  [run 42937812763](https://github.com/databricks/unity-gateway/actions/runs/42937812763).
- Cowork enterprise-only delivery failed: discovery and Skill invocation worked, but
  the native Read hook rejected the Program Files reference as outside connected folders.
  Installing both copies failed the inventory assertion because `probe-summary` and
  `ug-native-contract:probe-summary` were both visible. These failures occurred on
  Desktop 2.42304.2 (embedded 2.1.299) and reproduced on Desktop 3.6556.2 (embedded
  2.1.308) in [run 42939759711](https://github.com/databricks/unity-gateway/actions/runs/42939759711).

Neither tested Cowork layout satisfies the global plan's requirement for one usable
skill per session. Skills are deferred from the Windows global publisher; the earlier skill results do not validate that publisher.
The parser correlates the unique prompt across native session roots, preserves matching
incomplete transcripts for diagnosis, and keeps those incomplete results pending.

Skill probes require manual dispatch, selecting one native case or all five and either pinned Desktop version. Failed native contracts remain available; narrower runs are explicit subsets.

The `cowork-inference-windows` job in `.github/workflows/integration.yml` exercises real
Claude Desktop inference through Unity Gateway on a disposable Windows runner.
It runs after lint for same-repository PRs, main pushes, scheduled CI, and manual
CI dispatches. It skips merge-group runs and fork PRs, which lack the required
credentials. Authorized fork-integration runs test the pinned commit.
The job is required by the integration gate, which feeds the required `e2e` check.
Runs requesting a published UG release, another Claude CLI
version, or a dependency override skip this checkout-specific desktop test.
The gate accepts that explicit skip only for these unsupported dispatch inputs;
failure, cancellation, or an unexpected skip blocks the gate.
A [20-run fresh-Windows soak](https://github.com/databricks/unity-gateway/actions/runs/42240639887)
passed without retries before enabling the gate. Whole-test retries are not used.

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

The test rejects model-provider routing, accepts gateway onboarding, opens
Cowork, and verifies the selected model maps to `system.ai.claude-haiku-4-5`.
It submits a number-conversion prompt and requires both `Claude responded: 48213`
in the conversation pane and `Claude finished the response` within four minutes.
The prompt spells the number in words so echoing the input cannot pass.

## Dependencies

The official Claude Desktop 2.42304.2 x64 MSIX is pinned by URL and SHA256 in
`integration.yml`; the installer script also verifies the Anthropic signature. Installer
provenance comes from Microsoft's winget-pkgs catalog:
`manifests/a/Anthropic/Claude/2.42304.2/Anthropic.Claude.installer.yaml`.
Direct official Anthropic installer/runtime downloads were approved for these
disposable CI tests.

The branch-only native contracts also pin Desktop 3.6556.2 from the same catalog,
`manifests/a/Anthropic/Claude/3.6556.2/Anthropic.Claude.installer.yaml` at commit
`7f3d27eb0b2cb388c051c3b3f1c7b5981c42f7e1`. SHA256 and Anthropic signature checks
remain required. This candidate does not change the blocking suite's version pin.

Python dependencies come from PyPI and Claude Code CLI 2.1.290 from public npm,
matching the existing GitHub-hosted Windows CLI integration tests. These public
package sources are approved for this CI job. Python 3.12 is selected explicitly;
Claude Code's native binary selection requires its npm postinstall script.
Package installation needs no JFrog credentials or GitHub OIDC permission.

## Evidence and limits

Artifacts contain installer provenance, configuration status, UI control names,
window screenshots, and native launch logs. Raw process logs stay outside the
artifact directory. Known bearer values, authorization bearer strings, and JWT-shaped tokens are
redacted before writing text artifacts; app profiles and credential files are not archived. Artifacts expire
after three days.

The named `desktop-ci` Databricks profile contains host/auth metadata, not a
token. No Claude.ai login is used. The test verifies the configured provider/model
and desktop response; it does not independently trace gateway HTTP requests.
macOS, Code-tab, MCP, file operations, and Cowork VM readiness are outside this
test's inference-only coverage.

## Windows global deployment lifecycle

`windows-global-deployment.yml` runs on the separate native-test branch. It installs
UG, Python, Claude CLI, and Databricks CLI under protected Program Files directories
and runs `test-global.ps1` through the installed public CLI. Administrator config inputs
are checked in under `tests/fixtures/managed_config/windows_desktop_global*.json`.

The test requires native registry values/types, matching CLI/Desktop connector names,
parent merge policy, required plugin manifest/version and shared-runtime hooks. It checks
repeat configure, connector/tracing withdrawal, a real locked-file failure and retry,
and revert while preserving unrelated administrator values/subkeys and a user profile.
A new standard Windows account must read/run the helper, fail to modify it, and fail
authentication without its own OAuth cache despite inherited CI provisioning credentials.

Results explicitly report live consumer OAuth and MCP calls as unverified. This is
machine-deployment coverage; it does not drive global-policy inference through the UI.
A consuming-user Databricks browser login is required for that remaining coverage.
The existing user-scope accessibility inference job remains unchanged.

The [November 11 deployment run](https://github.com/databricks/unity-gateway/actions/runs/42948740241)
passed every deployment assertion above, including standard-user isolation and complete
update/failure/retry/revert checks. Live consumer OAuth, UI inference through global policy,
and MCP calls/deduplication remain unverified. Native testing exposed and fixed subprocess
text-input handling and inherited PowerShell module-path incompatibility in ACL validation.
