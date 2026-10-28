# Claude Desktop native integration testing plan

Ticket: AIGTWY-5020, under epic AIGTWY-5015.

This document defines future native macOS and Windows GUI testing. This change does
not provision an interactive runner or install Claude Desktop. Existing Linux and
Windows integration jobs now exercise the installed CLI's profile lifecycle with live
workspace discovery, while component tests cover failure boundaries.

## Existing automated coverage

The component suite uses temporary homes and real profile files while replacing
external authentication and model-discovery boundaries. It covers:

- macOS, Windows, and Linux profile-path resolution;
- the fixed Unity Gateway profile UUID and profile label;
- complete replacement of the reserved profile on configure;
- preservation of unrelated profiles and metadata;
- repeat configure, workspace/profile changes, and deleted-profile recovery;
- Claude-only model filtering and display labels;
- credential-helper arguments and token-only output;
- atomic writes, concurrent-change detection, lock timeouts, and rollback;
- revert deletion and previous-selection handling; and
- missing-app, malformed-file, dry-run, and warn-only failure behavior.

These tests validate UG's file contract. They do not launch Claude Desktop or
prove that a particular app release accepts the profile, runs Cowork, invokes the
helper, or reaches Unity Gateway.

The installed-CLI integration suite seeds an unrelated profile, runs real `ug configure`
twice with an explicit CLI profile, then runs real `ug revert`. On Linux and Windows it
checks the fixed UUID profile, gateway/helper/models, label and metadata, duplicate
prevention, unrelated-profile preservation, deletion, and prior-selection restoration.
It executes the exact generated helper and validates its credential against the live models API.
Both the system.ai and subscription-relay journeys use the generated helper credentials and
profile headers for real Messages inference. Each installs Cowork through the real Claude CLI,
adds system.ai.dbsql through public ug commands, and uses the installed plugin's command to
discover tools and execute a read-only query with a computed receipt. Both check repeat
configure, preservation of unrelated settings, and profile/plugin cleanup on revert, within
the existing Linux/Windows integration lanes and their model workspace.
CUJ1 and CUJ2 assert Desktop configuration for system.ai models and non-relay Anthropic MPS.
CUJ3 verifies that its incompatible scoped model aliases produce a warning and leave existing
Desktop configuration untouched. These tests do not
launch Desktop, exercise its scheduling of the helper, execute connector skills, or run
browser consent.

The existing `windows-latest` integration jobs install and run UG and real coding
agent CLIs on Windows Server. They are headless and do not provide the Windows 11,
interactive desktop session, or Cowork virtualization required for a native
Desktop test.

## Native runner requirements

Use disposable native runners. Pin the OS image, Claude Desktop installer and
version, UG wheel or commit, Databricks CLI, and automation dependencies. Resolve
downloads through approved internal package sources. Never use a developer home,
browser profile, or personal account.

### macOS

- Use a logged-in disposable user with a real WindowServer session.
- Use a dedicated Claude test account with Cowork access and an isolated work
  folder.
- Drive the app through stable macOS Accessibility roles and labels. Do not use
  coordinate clicks or screenshot-only success checks.

### Windows

- Use Windows 11 on an approved physical host or a VM with proven nested
  virtualization. Windows Server support is not established.
- Install Claude Desktop in the supported machine-wide form and verify
  `VirtualMachinePlatform`, the required restart, `vmcompute`, and `hns` before
  running tests.
- Use a logged-in interactive user and stable Windows UI Automation identifiers.
  A service-only session cannot validate Desktop.

## Proposed journeys

Native tests must use the installed public `ug` command and the real Desktop
process. They must not mock Desktop, replace the credential helper, fake Unity
Gateway, import UG internals, or hand-write UG state.

### Configure and revert

1. Create an unrelated Desktop profile through the app.
2. Run `ug configure` with an explicit Databricks CLI profile.
3. Verify Desktop shows and selects `Unity Gateway - <profile name>` using UG's
   fixed reserved UUID.
4. Verify the configured model picker contains the expected Claude-family routes
   and labels.
5. Configure again and verify the same UUID is replaced without duplicate metadata
   entries.
6. Switch workspaces and verify the same reserved profile now contains the new
   gateway, helper arguments, headers, and model list.
7. Verify unrelated profiles and metadata remain unchanged.
8. Run `ug revert`; verify the reserved profile and entry are gone and the prior
   valid selection is restored without changing unrelated profiles.

### Cowork inference

1. Launch the real app and enter Cowork.
2. Connect an isolated folder through the UI.
3. Ask Claude to read a file containing a random value absent from the prompt.
4. Require the value in the final response and correlate the request, selected
   model, and successful response with Unity Gateway evidence.
5. Select a second configured model in Desktop and repeat with a new value.

App launch, a visible profile, an echoed prompt, or a nonempty response alone is
not success.

### Authentication

Use the helper generated by UG without wrapping or replacing it.

- Prove a cached credential supports a real Cowork request.
- With an approved controllable short-lived identity, keep a session open through
  expiry and prove a later request succeeds after silent refresh.
- Prove background refresh failure does not open a browser and produces an
  actionable error.
- Test interactive browser recovery separately with an approved test identity,
  then resend the failed request and verify it reaches Unity Gateway.

If credential expiry cannot be controlled safely, keep that case manual rather
than weakening the assertion.

## Evidence and cleanup

- Record OS/image, Desktop/UG/CLI versions, selected route names, test markers,
  timestamps, exit codes, and gateway correlation IDs.
- Never archive tokens, cookies, browser profiles, credential files, complete home
  directories, or unredacted Desktop state.
- Bound every subprocess and GUI wait. Reap Desktop, helpers, browsers, and child
  processes on success and failure.
- Reset the disposable user or VM snapshot after every run.
- Treat missing GUI, virtualization, entitlement, model service, or approved
  identity as a failed prerequisite with its exact reason. Do not hide failures
  with retries, skips, or xfails.

## Rollout

1. Run a macOS capability spike for app launch, Accessibility selectors, one
   Cowork file task, model switching, and gateway correlation.
2. Add a pinned macOS lane for configure/reconfigure/revert and Cowork inference.
3. Validate Windows 11 installation, virtualization, and UI Automation on the
   intended runner before adding the equivalent Windows lane.
4. Add credential-expiry and interactive browser-recovery lanes only after test
   identities and cleanup are approved.

Native OTEL export and Anthropic subscription relay are separate features. This
plan and its component-test branch do not depend on either implementation.

## References

- [Test rules](../tests/AGENTS.md)
- [Component coverage](../tests/README.md)
- [Integration runner contract](../tests/integration/README.md)
- [Integration workflow](../.github/workflows/integration.yml)
- [Headless integration runner](../scripts/run_integration.py)
- [Desktop profile tests](../tests/test_claude_desktop.py)
