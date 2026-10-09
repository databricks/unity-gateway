# Claude Desktop integration testing plan

Ticket: AIGTWY-5020, under epic AIGTWY-5015.

This is an environment and automation plan. It does not provision a runner, install
Claude Desktop, spend on a hosted machine, use live credentials, or contact an
external person. The MVP configures Claude Desktop automatically alongside Claude
Code on macOS. Windows remains an explicit warn-only path until its native profile
path and launch behavior are validated. Independent Desktop configuration, model,
and toggle controls remain P1.

## What is known

- Anthropic documents Desktop third-party inference for Cowork and Code through
  third-party providers in [The full Claude Desktop experience on AWS, Google Cloud,
  and Microsoft Foundry](https://claude.com/resources/articles/the-full-claude-desktop-experience-on-aws-google-cloud-and-microsoft-foundry).
  This establishes the product surface. It does not establish the Databricks
  profile schema or support for every Anthropic-compatible model ID.
- Anthropic's [Enterprise configuration for Claude Desktop](https://support.claude.com/en/articles/12622667-enterprise-configuration-for-claude-desktop)
  documents macOS MDM preferences and Windows Group Policy/Intune. It does not
  publish a user-level third-party profile path or complete profile schema.
- Anthropic's [Windows deployment guide](https://support.claude.com/en/articles/12622703-deploy-claude-desktop-for-windows)
  requires Windows virtualization support for Cowork. The native lane must verify
  <code>VirtualMachinePlatform</code>, restart state, <code>vmcompute</code>, and
  <code>hns</code>. VMs without nested virtualization are unsupported. Windows
  Server support is not established.
- Anthropic documents native Cowork OTLP export in [Monitor Claude Cowork activity
  with OpenTelemetry](https://support.claude.com/en/articles/14477985-monitor-claude-cowork-activity-with-opentelemetry).
  This plan does not make native client OTEL a release gate; gateway correlation is
  the MVP evidence.
- [Claude Code LLM gateway documentation](https://docs.anthropic.com/en/docs/claude-code/llm-gateway)
  documents <code>apiKeyHelper</code>, <code>ANTHROPIC_BASE_URL</code>, auth
  variables, and helper TTL for Claude Code. It is not a Desktop helper contract.
  Desktop helper launch, timeout, Windows quoting, and arbitrary model compatibility
  require native validation. No specific <code>defaultModel</code> field is
  established. The confirmed profile schema includes an
  <code>inferenceCustomHeaders</code> dictionary, an
  <code>alwaysStartWithDefaultModel</code> boolean, and per-row
  <code>labelOverride</code> values.
- Direct observation on macOS Claude Desktop 2.19675.0 confirmed the native store at
  <code>Claude-3p/configLibrary</code>: <code>_meta.json</code> contains
  <code>appliedId</code> and <code>entries</code>, alongside UUID-named profile
  JSON files. Windows profile location and MSIX virtualization remain validation
  gates. Do not cite guessed <code>APPDATA</code> or <code>LOCALAPPDATA</code>
  paths as official behavior.

Do not claim that Chat uses Cowork's inference path. The existing observation proves
only Cowork traffic reached <code>/ai-gateway/anthropic/v1/messages</code>.

## Runner contract

Use a dedicated native runner. Do not extend the existing hosted
<code>windows-server-latest</code> installation/headless lane in
[.github/workflows/integration.yml](../.github/workflows/integration.yml).
The existing [integration runner](../scripts/run_integration.py) remains useful for
package, installation, and headless checks; it is not a Desktop GUI runner.

### macOS

- Use an approved, pinned macOS image and architecture with a logged-in disposable
  runner user and a real WindowServer/GUI session.
- Pin the Claude Desktop installer and app version, the <code>ug</code> wheel or
  commit, the Databricks CLI version, and every agent/package dependency. Resolve
  downloads through the approved internal package proxy. Do not use floating
  versions or a public-registry fallback.
- Use a dedicated Claude test account/org with Cowork entitlement, a fresh Desktop
  profile, a dedicated Databricks profile, and an isolated temporary work folder.
  Never use a developer home, browser profile, or personal account.

### Windows

- Use Windows 11 x64 on an approved physical host or a VM with proven nested
  virtualization. Do not use Windows Server until Anthropic support is established.
- Install the pinned MSIX in the supported machine-wide form required for Cowork;
  verify <code>VirtualMachinePlatform</code>, the required restart, <code>vmcompute</code>,
  and <code>hns</code> before running tests. A per-user install that lacks the
  Cowork virtualization service is a prerequisite failure, not a test skip.
- Run the app as the logged-in runner user in an interactive GUI session. A service-
  only or unattended session cannot prove Desktop behavior.
- Keep the image, MSIX, <code>ug</code> version, Databricks CLI, and dependencies
  pinned and proxy-managed. Do not guess a profile path; capture the path selected
  by the installed app/adapter during preflight.

Automation should use platform-supported accessibility trees and stable roles,
labels, or automation IDs (macOS Accessibility/AX and Windows UI Automation are
candidate APIs). Do not commit to an unverified third-party automation library,
coordinate clicks, or screenshot-only assertions. Select and approve the concrete
adapter during the capability spike.

## Proposed test layout

Add a dedicated runner and native marker in a later implementation PR. Native tests
must fail clearly when prerequisites are missing; they must not skip, xfail, retry,
mock Desktop, replace an executable, fake a gateway, or import <code>ucode</code>
internals. Use the installed public <code>ug</code> entry point and real Desktop
process boundaries.

- <code>tests/integration/test_ug_configure_claude_desktop.py</code>
  - automatic Desktop configuration during the public <code>ug configure</code>
    journey;
  - warn-only Desktop-unavailable path returns exit 0 while reporting the warning;
  - initial profile selection, model-list refresh on a later configure, repeat
    configure without duplicate profiles, preservation of unrelated profiles/keys,
    and public <code>ug revert</code> ownership behavior.
- <code>tests/integration/test_ug_claude_desktop.py</code>
  - launch the real Desktop app and enter Cowork;
  - connect a folder through the UI;
  - ask the agent to read a file containing an unpredictable value absent from the
    prompt;
  - switch to a second configured model through the native model picker and repeat
    the task;
  - correlate each task's marker, model, and successful response with the Databricks
    gateway request. A banner, startup, file picker, or echoed prompt is not success.
- <code>tests/integration/test_ug_claude_desktop_auth.py</code>
  - cached token use and controlled silent expiry/refresh;
  - background helper failure with no browser and an actionable error;
  - a separate interactive U2M browser/SSO lane with an approved test identity and
    manual approval.

The exact module split may change during implementation, but each journey must keep
configure, launch, user action, and assertions visible. Register the native marker
in the dedicated pytest configuration, not the default suite.

## Authentication journeys

Use the actual helper emitted by <code>ug</code>; do not wrap, replace, or fake it.
Capture only redacted command shape, exit code, timing, and error class.

- Warm the helper from a valid dedicated Databricks profile and prove one real Cowork
  request. Let the configured cache lifetime expire using a controlled short-lived
  test identity or an approved test-time setting. If expiry cannot be controlled,
  fail the capability gate rather than weakening the assertion.
- Prove silent refresh in the background lane. Missing or expired credentials must
  not open a browser from a service or noninteractive helper context.
- Prove browser SSO only in the interactive GUI lane with a test identity approved
  for that purpose. A service-principal gateway smoke proves gateway access, not U2M
  browser login.
- Exercise resend after login failure. Record whether model selection persists and
  whether the request reaches the gateway after reauthentication.

## Profile safety journeys

Create personal/default profiles through the real Desktop UI in a disposable profile;
do not hand-write application state to manufacture a scenario.

- First configure creates one Unity Gateway profile and selects it without removing
  existing profiles.
- A second configure updates the recorded profile ID and model list without adding a
  duplicate entry. User-renamed or user-owned fields remain unchanged.
- Change the selected profile in Desktop, rerun configure, and verify the documented
  ownership behavior. Record the result rather than assuming <code>appliedId</code>
  semantics.
- Run public <code>ug revert</code>; verify the Unity Gateway profile/selection changes
  are reverted according to the ownership manifest while unrelated profiles and edits
  survive.
- Confirm the macOS <code>Claude-3p/configLibrary</code> store and assert that
  <code>_meta.json</code>, UUID profiles, and the applied selection remain coherent.
  Windows must discover and validate its own native store before this journey is
  enabled. Never hardcode a path inferred from a community guide.

## Evidence, cleanup, and failure rules

- Store a redacted run manifest: OS/image ID, Desktop/<code>ug</code>/CLI versions,
  selected model names, proxy mode, test marker, exit codes, timestamps, and gateway
  correlation IDs.
- Do not archive tokens, cookies, browser profiles, credential files, complete
  environments, home directories, or unredacted Desktop state. Redact workspace
  identifiers where they identify a private tenant.
- Bound every subprocess and GUI wait. Reap Desktop, helper, browser, and child
  processes on success and failure. Reset the disposable user/profile and VM snapshot
  after each run; do not leave a logged-in session or stale Cowork VM behind.
- Missing virtualization, entitlement, GUI session, app version, proxy access,
  model service, or approved identity is a failed prerequisite with its exact reason.
  Do not hide it with a skip or retry.

## Staged rollout and approval gates

1. **Capability spike:** validate macOS profile discovery, Windows virtualization/MSIX
   behavior, accessibility selectors, Cowork folder/file task, model switch, and
   gateway correlation on disposable hosts. No CI or spend commitment.
2. **macOS lane:** add configure/repeat/preservation/revert and real Cowork task
   journeys at pinned versions. Gate on redacted evidence and clean snapshot reset.
3. **Windows lane:** repeat on the approved Windows 11 native runner. Gate on
   machine-wide Cowork virtualization and an interactive session; hosted headless
   Windows remains a separate lane.
4. **Authentication lane:** add controlled silent-expiry and manually approved U2M
   browser tests. Gate on no-browser background refusal and successful interactive
   recovery.
5. **CI scheduling:** only after platform, security, proxy, account, license, and
   budget owners approve the runner. Schedule the dedicated runner; do not provision
   or spend before that approval.

## Deferred or not executed

This plan does not implement or validate native client OTEL export, subscription
relay, independent Desktop settings/model/toggle controls, connectors/plugins, smart
routing, usage reporting, managed refresh, or unattended background browser login.
It does not provision infrastructure, install software, run live auth, or change the
existing integration workflow.

## Repository references

- [Integration rules](../tests/AGENTS.md)
- [Integration coverage and runner contract](../tests/integration/README.md)
- [Existing integration workflow](../.github/workflows/integration.yml)
- [Existing package/headless runner](../scripts/run_integration.py)
- [Current Desktop profile unit contract](../tests/test_claude_desktop.py)
