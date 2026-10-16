# Claude Desktop through Unity Gateway

After Claude Code is successfully configured, `ug configure` also creates and
selects a `Unity Gateway - <CLI profile name>` profile for Claude Desktop on macOS,
Windows, and Linux. This uses
the same workspace, Databricks CLI OAuth profile, model source, and managed HTTP
headers as Claude Code. There is no separate Desktop checkbox or managed field.

```bash
ug configure --profile <your-profile> --agents claude
```

Quit Claude Desktop before configuring, then reopen it normally from your app launcher.
No terminal process needs to stay running. Claude Desktop must already be installed;
UG does not install it or manage the customer's MDM process. On Windows and Linux,
open Desktop once before configuring so it initializes its application-data directory.

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

Profiles use Anthropic's [documented locations](https://claude.com/docs/third-party/claude-desktop/data-storage#where-data-lives):

- macOS: `~/Library/Application Support/Claude-3p/configLibrary`
- Windows: `%LOCALAPPDATA%\Claude-3p\configLibrary`
- Linux: `~/.config/Claude-3p/configLibrary`

Older Windows releases used `%APPDATA%\Claude-3p`; update and open Desktop to let
the app migrate its data before configuring. UG does not move app data.

The profile ID is a UUIDv5 derived from the normalized workspace URL in a UG-specific
namespace. UG manages that UUID: configure creates it if absent and refreshes it if
present, regardless of prior ownership records. Revert baselines are stored separately
in `~/.ucode/claude-desktop.json`.
The same workspace has the same ID across machines; different workspaces have
different IDs. Changing the Databricks CLI profile updates the display name and
helper rather than creating another Desktop profile. Reconfigure also upgrades
profiles at that UUID named just "Unity Gateway".

If the UG profile is deleted in Desktop, running `ug configure` recreates it with
the same ID and selects it again. Old random-ID profiles and their metadata entries
are left untouched; configure does not migrate or remove them.

Running `ug configure` again replaces UG-owned fields, including values edited or
deleted in Desktop. Other profiles and unknown fields are preserved. Previously
managed optional fields retain their last UG value when configure omits them;
optional fields UG has never written remain user-owned. Configure updates the managed
metadata entry and selection even if that entry was renamed or no ownership record exists.

`ug revert` restores UG-owned changes for the current workspace and preserves edits
and profile selections made after the latest configure. Profiles configured for
other workspaces remain untouched.
Repeated configure retains the original profile and metadata-entry baselines for
revert, including when the UUID profile existed before UG recorded it.
After recreating a deleted profile, revert restores the selection that was active
immediately before recreation.
If Desktop revert fails, UG keeps its state so the operation can be retried. Files
are replaced atomically one at a time with best-effort rollback on write failure;
this is not a crash-atomic transaction across the app and ownership files.
Profile configure/revert waits at most five seconds to acquire its file lock.
If another update holds it longer, UG warns without changing profile or ownership
files; finish the other operation and retry. The timeout does not interrupt the
operation already holding the lock.

Desktop failures are warnings: successful Claude Code setup still returns exit 0.
`--dry-run` performs no Desktop discovery or writes. Removing Claude from managed
configuration does not revoke a previously written Desktop profile. Desktop does
not run UG's launch-time managed-agent checks; gateway authorization remains the
enforcement boundary.

## Validation limits

The native profile format comes from a working macOS Claude Desktop/Cowork
2.19675.0 export. Tests cover configuration, ownership, helper behavior, and CLI
orchestration; they do not prove GUI inference on every app version.

- Windows and Linux profile paths follow the documented shared `configLibrary`
  layout. Component tests exercise filesystem configure/refresh/revert using all
  three platform selections; they do not establish native GUI, helper execution,
  browser-login, or inference coverage on Windows or Linux.
- Anthropic's [Linux Desktop beta](https://code.claude.com/docs/en/desktop-linux)
  supports Ubuntu 22.04+ and Debian 12+. Cowork additionally requires QEMU/KVM.
- A profile selection is not proof that a fresh ordinary installation has switched
  to third-party inference mode. Fresh-install activation remains a native test gate.
- UG does not configure Desktop's default-model selection.
  Inheriting a particular Claude Code default needs further native evidence; UG
  does not guess a field or claim that list order selects the default.
- Inspected Desktop code rejects model IDs containing other vendors' model names,
  including `glm` and `kimi`, before inference. Gateway protocol compatibility does
  not remove this client restriction. UG preserves the catalog without renaming
  models to bypass it.
- Anthropic subscription relay, custom OAuth/PAT profiles, Desktop Smart Routing,
  connector/plugin compatibility, and native OTEL are not implemented here.
- Observed inference applies to Cowork. Ordinary Chat is not claimed to use the
  same gateway path.

Before native rollout on Windows and Linux, verify that the app reads the generated
profile after restart, completes a gateway task, refreshes credentials silently,
opens browser login when interactive recovery is needed, and preserves unrelated
settings through repeat configure and revert. Run these checks on the supported
app version and packaging used by your organization.
