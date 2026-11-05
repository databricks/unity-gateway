# Windows Claude Desktop probe

Exploratory CI checks for [AIGTWY-5412](https://databricks.atlassian.net/browse/AIGTWY-5412).
Run only on a disposable Windows runner. The branch workflow exercises real Cowork inference through Unity Gateway.

The existing Windows CI runner provides an accessible interactive desktop.
This inference-only workflow does not check nested virtualization.
The app probe installs a signed,
SHA256-pinned Claude MSIX and drives the real app through Windows UI Automation.
`--force-renderer-accessibility` exposes the app's accessible controls.

The probe installs UG from the checkout, runs `ug configure` with the existing
`UCODE_TEST_WORKSPACE` and `DATABRICKS_BEARER` CI secrets, and checks the generated
gateway profile. Configuration runs before the first Desktop launch. UG detects the installed Windows MSIX package and executable, then creates
`%LOCALAPPDATA%/Claude-3p/configLibrary` itself. The test does not create the app-data
directory or launch Desktop before configuration.
The test rejects model-provider routing and requires the target model service below.
It accepts gateway onboarding, opens Cowork, verifies the selected model maps to
`system.ai.claude-haiku-4-5`, and submits a number-conversion prompt. It asserts
the expected assistant response and the app's response-finished announcement.
The named `desktop-ci` Databricks profile contains host/auth metadata, not a token.
The prompt requests no tools; this test does not establish tool execution or
Cowork VM readiness.

Claude 2.42304.2 rejects the debugging switches used by Playwright's Electron
launcher. Native launch succeeds. The optional `try_playwright` workflow input
retains that diagnostic; it is disabled by default and is not the automation path.

## Dependencies

The user approved direct official Anthropic installer and runtime downloads for
this disposable probe. The workflow downloads the official 2.42304.2 x64 MSIX
and verifies its SHA256 and Anthropic signature before installation. The
versioned URL and SHA256 come from Microsoft's winget-pkgs catalog:
`manifests/a/Anthropic/Claude/2.42304.2/Anthropic.Claude.installer.yaml`.
An internal installer URL and independently pinned SHA256 can also be supplied.

Python dependencies, Claude Code CLI 2.1.290, and optional Playwright Core 1.62.1
come through internal JFrog registries. Claude Code's native binary selection
requires its npm postinstall script. Playwright downloads no browser. The job
exchanges GitHub OIDC identity using the existing `github-actions` JFrog provider
and removes temporary registry credentials after installation.

## Evidence and limits

Artifacts contain installer provenance, configuration status, UI control names,
window screenshots, and native launch logs. Raw process logs stay outside the artifact directory. Known bearer values are
redacted before writing text artifacts; app profiles and credential files are not archived.
Artifacts expire after three days.

The test requires a completed `48213` response in Cowork without a Claude.ai
login. It verifies the configured provider/model and the desktop response; it
does not independently trace the gateway HTTP request.
macOS, Code-tab, MCP, and file-operation coverage are outside this first probe.
