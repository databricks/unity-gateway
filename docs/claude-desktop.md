# Claude Desktop through Unity Gateway

After Claude Code is successfully configured, `ug configure` also creates and
selects a dedicated Unity Gateway profile for Claude Desktop on macOS. This uses
the same workspace, Databricks CLI OAuth profile, model source, and managed HTTP
headers as Claude Code. There is no separate Desktop checkbox or managed field.

```bash
ug configure --profile <your-profile> --agents claude
```

Quit Claude Desktop before configuring, then reopen it normally from Applications
or the Dock. No terminal process needs to stay running. Claude Desktop must already
be installed; UG does not install it or manage the customer's MDM process.

## Models and authentication

An admin's static model list is copied as-is. Otherwise UG obtains the complete
Anthropic-compatible gateway catalog, scoped to the same provider service or
Unity Catalog location when applicable. Model IDs are not filtered to Claude
families: backend-translated OSS models remain in the list. The catalog refreshes
when `ug configure` runs again, not when the app opens or on a timer.

The profile uses a credential helper bound to the Python installation running UG.
It stores only the workspace/profile name and command arguments, never a bearer
token. The helper asks the Databricks CLI for a cached or silently refreshed token.
If authentication is missing, it opens browser login only when Desktop supplies
`CLAUDE_HELPER_CONTEXT=interactive`. Other contexts fail with instructions to start
a new Cowork task. Browser login has a bounded timeout and concurrent sign-ins are
serialized. If the original message failed during login, resend it.

## Existing profiles and rollback

Profiles live in
`~/Library/Application Support/Claude-3p/configLibrary`. UG records its UUID and
ownership separately in `~/.ucode/claude-desktop.json`. It never adopts a profile
just because its name is "Unity Gateway". Repeated configuration reuses the
recorded profile for that workspace and Databricks profile.

Other profiles, metadata, and unknown fields are preserved. Editing UG-owned fields
causes a conflict warning instead of an overwrite. `ug revert` restores UG-owned
changes for the current workspace/profile and preserves subsequent user edits and
profile selections. Profiles configured for other workspaces remain untouched.
If Desktop revert fails, UG keeps its state so the operation can be retried. Files
are replaced atomically one at a time with best-effort rollback on write failure;
this is not a crash-atomic transaction across the app and ownership files.

Desktop failures are warnings: successful Claude Code setup still returns exit 0.
`--dry-run` performs no Desktop discovery or writes. Removing Claude from managed
configuration does not revoke a previously written Desktop profile. Desktop does
not run UG's launch-time managed-agent checks; gateway authorization remains the
enforcement boundary.

## Validation limits

The native profile format comes from a working macOS Claude Desktop/Cowork
2.19675.0 export. Tests cover configuration, ownership, helper behavior, and CLI
orchestration; they do not prove GUI inference on every app version.

- Windows profile location and MSIX behavior need native validation. Windows
  currently reports a warning and does not write Desktop settings; Linux is a no-op.
- A profile selection is not proof that a fresh ordinary installation has switched
  to third-party inference mode. Fresh-install activation remains a native test gate.
- The exported `alwaysStartWithDefaultModel` boolean does not identify a model.
  Inheriting a particular Claude Code default needs further native evidence; UG
  does not guess a field or claim that list order selects the default.
- Anthropic subscription relay, custom OAuth/PAT profiles, Desktop Smart Routing,
  connector/plugin compatibility, and native OTEL are not implemented here.
- Observed inference applies to Cowork. Ordinary Chat is not claimed to use the
  same gateway path.

See [native integration-test plan](claude-desktop-integration-testing.md) for
Windows runner prerequisites, GUI journeys, browser auth, and release gates.
