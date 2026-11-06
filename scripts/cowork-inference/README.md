# Windows Cowork inference test

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
