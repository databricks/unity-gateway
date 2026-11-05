# Windows Claude Desktop probe

Exploratory CI checks for [AIGTWY-5412](https://databricks.atlassian.net/browse/AIGTWY-5412).
Run only on a disposable Windows runner. The branch workflow is not yet a
completed Cowork inference regression test.

The existing Windows CI runner provides an accessible interactive desktop and
successfully starts a nested Hyper-V VM. The app probe installs a signed,
SHA256-pinned Claude MSIX and drives the real app through Windows UI Automation.
`--force-renderer-accessibility` exposes the app's accessible controls.

The probe installs UG from the checkout, runs `ug configure` with the existing
`UCODE_TEST_WORKSPACE` and `DATABRICKS_BEARER` CI secrets, and checks the generated
gateway profile. It then accepts gateway onboarding and navigates toward Cowork.
The named `desktop-ci` Databricks profile contains host/auth metadata, not a token.
Opening Cowork alone does not establish runtime readiness or successful inference.

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
window screenshots, and native launch logs. Known bearer values are redacted
from text artifacts; app profiles and credential files are not archived.
Artifacts expire after three days.

Verified so far: native installation/launch, accessibility interaction, real UG
configuration, and Claude's gateway welcome screen without a Claude.ai login.
Still required: complete Cowork onboarding, select a `system.ai` Claude model,
submit a prompt, and assert the completed response through the desktop UI.
macOS, Code-tab, MCP, and file-operation coverage are outside this first probe.
