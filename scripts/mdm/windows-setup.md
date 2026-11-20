# Deploy Unity Gateway with Microsoft Intune

Use this walkthrough to deploy the Windows provisioning script to one enrolled device as an
Intune Win32 app. Prepare the package on a Windows computer, which can be different from the
deployment device. Intune runs the installer as SYSTEM; developers use their own Databricks OAuth.

## Before you start

- Enroll a Windows 11 Pro, Enterprise or Education x64/ARM64 device in Intune. Have permission to
  create Win32 apps and device groups. Keep an administrator account available for recovery;
  removing administrator access from your only account is not part of this procedure.
- Publish a workspace configuration with Claude Code and Codex and models for both. The
  provisioning service principal needs workspace access and access to the configured models,
  MCP servers and skills. Generate a dedicated OAuth secret with the
  `ai-gateway:read unity-catalog:read` scopes.
- Allow the device's SYSTEM account to reach your workspace, GitHub/release-download hosts, PyPI,
  `nodejs.org`, and `registry.npmjs.org`. Configure approved registry mirrors before packaging if
  public PyPI/npm are blocked on your network.

The example uses a secret argument in Intune. It can appear in management/process logs and app
settings; it is not secure secret injection. Proceed only if your organization permits this
handling, restrict access to the app settings, and use a short-lived dedicated secret. Do not put
credentials in the script/package or share populated commands/logs. An approved runtime credential
source can instead supply `DATABRICKS_CLIENT_SECRET` to the installer process; Intune does not
provide that source or set the variable automatically.

The script changes an unset or `Restricted` LocalMachine execution policy to `RemoteSigned` and
adds PowerShell profile functions. Group Policy is not overridden. Confirm these changes are
permitted on the target device.

## 1. Prepare the package

On the package-building Windows computer, open **Windows PowerShell as administrator** and run:

```powershell
New-Item -ItemType Directory -Force -Path C:\UGPackage, C:\UGTools, C:\UGOutput | Out-Null
Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/databricks/unity-gateway/48bacf1473b0cbd3e870ea38013911d29a8ad1ca/scripts/mdm/windows-provision.ps1' -OutFile C:\UGPackage\windows-provision.ps1
Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/microsoft/Microsoft-Win32-Content-Prep-Tool/master/IntuneWinAppUtil.exe' -OutFile C:\UGTools\IntuneWinAppUtil.exe
```

This pins the installer to the revision with the standard-user runtime-permissions fix. Do not
use an older test copy. These commands prepare files; do not run the provisioning script manually
when testing Intune delivery.

If your network requires package mirrors, edit `C:\UGPackage\windows-provision.ps1` before the
next step. Add process settings below `$ErrorActionPreference = 'Stop'`, after the `param(...)`
block, using your organization's approved endpoints:

```powershell
$env:UV_DEFAULT_INDEX = 'https://<approved-pypi-host>/simple'
$env:npm_config_registry = 'https://<approved-npm-host>/'
```

Omit those settings when public registries are reachable. Do not include proxy passwords in the
package. Keep `C:\UGPackage` limited to `windows-provision.ps1`; the preparation tool packages
that folder's contents.

## 2. Build the .intunewin file

```powershell
& C:\UGTools\IntuneWinAppUtil.exe -c C:\UGPackage -s windows-provision.ps1 -o C:\UGOutput -q
if ($LASTEXITCODE -ne 0) { throw "Packaging failed: $LASTEXITCODE" }
Get-Item C:\UGOutput\windows-provision.intunewin
```

The output is `C:\UGOutput\windows-provision.intunewin`. Keep the output and tool folders outside
the package source. If uploading from another computer, transfer this file to that computer.

## 3. Create a one-device group

In the [Intune admin center](https://intune.microsoft.com):

1. Open **Devices > All devices** and note the target device's name. `hostname` in its Windows
   terminal also shows the computer name.
2. Open **Groups > All groups > New group** in the main navigation.
3. Set **Group type: Security**, **Group name: Unity Gateway Windows pilot**, and
   **Membership type: Assigned**.
4. Add only the target **device**, not the user account, as a member. Create the group.

## 4. Upload the Win32 app

1. Open **Apps > All apps > Create**. Choose **Windows app (Win32)** and click **Select**.
2. Click **Select app package file**, upload `windows-provision.intunewin`, and click **OK**.
3. Set **Name: Unity Gateway Windows pilot**, **Description: Unity Gateway Windows pilot**,
   and **Publisher: Databricks**. Click **Next**.

## 5. Set the Program fields

Choose **Command line** as the installer type if the selector appears. In **Install command**,
replace `<workspace-url>`, `<client-id>`, and `<client-secret>`. Keep the quoted `-UgRef` value:

```text
C:\Windows\Sysnative\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\windows-provision.ps1" -Workspace "<workspace-url>" -ClientId "<client-id>" -ClientSecret "<client-secret>" -UgRef "48bacf1473b0cbd3e870ea38013911d29a8ad1ca"
```

The workspace URL is the complete `https://` origin without a trailing path. Enter the populated
command only in Intune. Do not omit `-UgRef`: the installer's older default predates the managed-agent
installation fix. The supplied commit includes that fix and the runtime-permissions repair.

For **Uninstall command**, use:

```text
C:\Windows\Sysnative\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Program Files\UnityGateway\windows-provision.ps1" -Uninstall
```

Both commands assume the Windows drive is `C:`. The uninstall field does not expand environment
variables. `Sysnative` selects 64-bit PowerShell when launched by the 32-bit Intune management
process.

| Setting | Value |
| --- | --- |
| Install behavior | **System** |
| Installation time required | **60 minutes** |
| Device restart behavior | **No specific action** |
| Allow available uninstall | **No**, if shown, for the pilot |
| Return codes | Leave defaults |

Click **Next**. On **Requirements**, select the architecture for the test device (x64 or ARM64,
where listed) and a Windows 11 minimum version that it meets. Leave optional resource rules unset.

## 6. Add detection and assign

On **Detection rules**, select **Manually configure detection rules > Add > File**:

| Field | Value |
| --- | --- |
| Path | `C:\Program Files\UnityGateway` |
| File or folder | `provisioned.json` |
| Detection method | **File or folder exists** |
| Associated with a 32-bit app on 64-bit clients | **No** |

The marker is written after configure, permission repair and profile setup finish. If it already
exists, Intune can consider the app installed without running the new package. This initial-pilot
rule does not detect software versions or schedule configuration refreshes.

1. Save the rule. Add nothing on **Dependencies** or **Supersedence** for this pilot.
2. On **Assignments**, under **Required**, add **Unity Gateway Windows pilot**.
3. Leave **Available** and **Uninstall** empty. Do not select **All users** or **All devices**.
4. Review the summary and click **Create**.

## 7. Sync and check installation

1. Open **Devices > All devices > your device > Sync**. Keep the device online.
2. Open **Apps > All apps > Unity Gateway Windows pilot > Device install status**.
3. Wait for **Installed**. The device Sync pane is not the app's installation status, and a sync
   request does not guarantee an immediate installation.

On the target device:

```powershell
Test-Path 'C:\Program Files\UnityGateway\provisioned.json'
Get-Content 'C:\Program Files\UnityGateway\provisioned.json'
```

Expect `True`, your workspace, and the requested `ugRef` in the marker. If Intune reports failure,
stop and inspect logs instead of installing manually over the failed attempt.

## 8. Restart and verify as a developer

Restart Windows or sign out and back in. Opening a new tab in an existing terminal can retain a
stale PATH and prevent Codex's npm launcher from finding `node`.

Open normal, non-elevated **Windows PowerShell** (not `-NoProfile`):

```powershell
Get-Command ug, node, claude, codex | Select-Object Name, Source, CommandType
```

Expect `ug` and `node` under `C:\Program Files\UnityGateway`, and `claude`/`codex` as **Function**.
Run `claude`, sign in to Databricks as the developer if prompted, and complete a prompt. Exit and
test `codex` separately. The aliases apply to Windows PowerShell and PowerShell 7 when installed
during provisioning, not cmd.exe or Git Bash.

Verify the configured models, MCP servers and skills before expanding the device group. Revoke or
rotate the provisioning secret after the installation window; the developer's sessions use their
own OAuth. Keep a separate recovery administrator; do not demote your only administrator to test
ordinary-user launches.

## Troubleshooting

Intune logs are under `C:\ProgramData\Microsoft\IntuneManagementExtension\Logs`.
Start with `AppWorkload.log` and `AppActionProcessor.log`. Redact credentials, populated commands,
Authorization headers and tokens before sharing excerpts. Those logs may show only an installer
exit code, not PowerShell stderr.

| Symptom | Next check |
| --- | --- |
| `0x80070001` with `lpExitCode 1` | PowerShell failed; this is not a specific PyPI diagnosis. Check which shared tools exist and collect the script error before retrying. Do not map code 1 to success. |
| uv exists but ug does not | Check Git/PyPI reachability as SYSTEM and approved mirrors. Edit the package source and rebuild if settings need to change. |
| `PermissionError` reading `ucode/managed_replace.sh` | Check the packaged script includes `Set-SharedReadAccess` from the pinned revision. Standard users need RX, not write access; re-running that installer reapplies the permissions. |
| Codex reports missing bundled models and its `.cmd` says Node is unrecognized | Sign out/restart, then verify `Get-Command node` resolves the shared runtime. |
| The app shows Installed but never ran | Check whether the marker already existed before assignment. |
| Profile scripts are blocked | Check `Get-ExecutionPolicy -List`. Do not override organization policy; direct launch is `$env:UG_SKIP_OS_SETTINGS = '1'; ug claude` or `ug codex`. |

To retry with a changed script, rebuild the package on the packaging computer and upload the new
`.intunewin` in the app's **Properties > App information > Edit > Select file**, then save and sync
the target. Check the new attempt in **Device install status**; do not assume sync alone retried it.
An already-present marker can prevent reinstalling an updated package.

## Updates and removal

For fleet updates, pilot a revised package, version-aware detection and supersedence. Existing
Git/uv/Node/CLI installations are reused; npm agent compatibility handling is owned by `ug`.
The script is not a general dependency updater. An administrator installing or upgrading shared
packages outside this script can introduce private permissions again; rerun provisioning to reapply
shared-runtime RX.

Remove the Required assignment before assigning Uninstall, and close running agents. The script
runs `ug revert` before removing its profiles, PATH entries and shared runtime. It leaves Git,
execution policy and individual users' Databricks credentials alone. Do not delete the runtime
before reverting managed settings, which can reference its absolute path.

References: [Win32 app settings](https://learn.microsoft.com/en-us/intune/app-management/deployment/add-win32)
and [Content Prep Tool](https://learn.microsoft.com/en-us/intune/app-management/deployment/create-win32-package).
