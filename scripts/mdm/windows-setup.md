# Deploy Unity Gateway to a Windows device with Intune

Follow these steps to install Unity Gateway, Claude Code and Codex on **one enrolled Windows test
device**. Intune runs the installer as SYSTEM; the developer then signs in with their own
Databricks account. Do not run the provisioning script manually before this test.

This walkthrough is awaiting an actual Intune pilot. If a step fails, stop at that step and collect
the error before assigning the app to more devices.

## Before you start

Have these ready:

- A Windows 11 Pro, Enterprise or Education x64/ARM64 test device enrolled in Intune, and
  permission to create Win32 apps and device groups in the Intune admin center. Take a VM snapshot
  if using a VM.
- Your Databricks workspace URL, provisioning service principal client id and client secret.
  The service principal needs workspace access and access to the models, MCP servers and skills
  in the workspace's published coding-agent configuration. Enable both Claude Code and Codex with
  usable models for this walkthrough. Provisioning requests `ai-gateway:read unity-catalog:read`.
- Network access from the Windows device to your workspace, GitHub and its release-download
  hosts, PyPI, `nodejs.org` and `registry.npmjs.org`.

**Credential warning:** this pilot passes the client secret through Intune's install command.
That can expose it to Intune administrators and endpoint process/management logs. Use a dedicated,
short-lived provisioning secret only if your organization permits this handling. Do not put the
secret in the `.ps1` file or `.intunewin` package, paste it into this repository, or include it in
screenshots/logs you share. If your organization prohibits secret arguments, use its approved
runtime credential-delivery mechanism before proceeding; Intune has no secure secret-argument
field.

The current script also sets an unset or `Restricted` LocalMachine execution policy to
`RemoteSigned` and installs all-users PowerShell functions. It does not override Group Policy.
Confirm these changes are permitted on your test device.

## 1. Prepare the package on Windows

On the Windows device you will use to build the package, open Start, search for **Windows
PowerShell**, and choose **Run as administrator** so you can create the folders below. These
packaging commands do not install Unity Gateway; Intune will do that later.

Create three folders:

```powershell
New-Item -ItemType Directory -Force -Path C:\UGPackage, C:\UGTools, C:\UGOutput | Out-Null
```

Download the merged provisioning script and Microsoft's Win32 Content Prep Tool:

```powershell
Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/databricks/unity-gateway/fc1ec310baf083e311a9cf084b8ea25046a4bb02/scripts/mdm/windows-provision.ps1' -OutFile C:\UGPackage\windows-provision.ps1
Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/microsoft/Microsoft-Win32-Content-Prep-Tool/master/IntuneWinAppUtil.exe' -OutFile C:\UGTools\IntuneWinAppUtil.exe
```

If you already have that version of `windows-provision.ps1`, copying it into `C:\UGPackage` is
also sufficient. Use the provisioning script, not an earlier `windows-native-test.ps1` or
`windows-npm-test.ps1` copy. Leave its credential inputs unchanged.

Check the source folder:

```powershell
Get-ChildItem C:\UGPackage
```

**Expected:** only `windows-provision.ps1`. Remove unrelated files from this package source folder;
the packaging tool includes its contents. Keep `UGTools` and `UGOutput` outside it.

## 2. Create the .intunewin package

In the same PowerShell window:

```powershell
& C:\UGTools\IntuneWinAppUtil.exe -c C:\UGPackage -s windows-provision.ps1 -o C:\UGOutput -q
if ($LASTEXITCODE -ne 0) { throw "Packaging failed: $LASTEXITCODE" }
Get-ChildItem C:\UGOutput
```

**Expected:** `C:\UGOutput\windows-provision.intunewin`.

See Microsoft's [Content Prep Tool instructions](https://learn.microsoft.com/en-us/intune/app-management/deployment/create-win32-package)
for packaging requirements.

## 3. Create a group containing only the test device

In the [Intune admin center](https://intune.microsoft.com):

1. Open **Devices > All devices**. Find the enrolled Windows device and note its device name.
2. Open **Groups > All groups > New group**.
3. Set **Group type** to **Security** and **Group name** to `Unity Gateway Windows pilot`.
4. Set **Membership type** to **Assigned**.
5. Under **Members**, select the Windows **device** you noted, not your user account.
6. Create the group. Do not add other devices yet.

## 4. Upload a Windows app (Win32)

1. Open **Apps > All apps > Create**.
2. Select **Windows app (Win32)** and click **Select**.
3. Click **Select app package file**, choose
   `C:\UGOutput\windows-provision.intunewin`, and click **OK**.
4. On **App information**, enter:

   | Field | Value |
   | --- | --- |
   | Name | `Unity Gateway Windows pilot` |
   | Description | `Unity Gateway, Claude Code and Codex for the Windows pilot` |
   | Publisher | `Databricks` |

5. Click **Next** to open **Program**.

## 5. Enter the install and uninstall commands

On **Program**, choose **Command line** if an installer-type selector is shown.

Paste this as the **Install command**, replacing only `WORKSPACE`, `CLIENT_ID` and `CLIENT_SECRET`.
Keep the double quotes and the supplied `-UgRef` value:

```text
C:\Windows\Sysnative\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\windows-provision.ps1" -Workspace "https://WORKSPACE.cloud.databricks.com" -ClientId "CLIENT_ID" -ClientSecret "CLIENT_SECRET" -UgRef "fc1ec310baf083e311a9cf084b8ea25046a4bb02"
```

Replace the entire workspace origin with your actual URL if it uses a different domain. Enter the
credential only in Intune; do not run the populated command in a terminal or share a screenshot of
it. The supplied revision includes the merged managed-agent installation support. Do not omit
`-UgRef`: the script's older default does not include that fix.

Paste this as the **Uninstall command**:

```text
C:\Windows\Sysnative\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Program Files\UnityGateway\windows-provision.ps1" -Uninstall
```

These paths assume Windows is installed on `C:\`. Adjust both commands if your device uses a
different drive. The uninstall field does not expand environment variables. `Sysnative` selects
64-bit PowerShell when the 32-bit Intune management process launches it.

Set the remaining fields:

| Field | Value |
| --- | --- |
| Install behavior | **System** |
| Installation time required | **60 minutes** |
| Device restart behavior | **No specific action** |
| Allow available uninstall | **No**, if shown |
| Return codes | Leave the defaults |

Click **Next**.

## 6. Set requirements

On **Requirements**:

1. Select the 64-bit operating-system architecture for your test device. If ARM64 is listed
   separately, select it for a Windows ARM64 device.
2. Set **Minimum operating system** to a Windows 11 version your test device meets.
3. Leave the optional CPU, memory and disk requirement rules unset for this pilot.
4. Click **Next**.

## 7. Add a detection rule

On **Detection rules**:

1. Set **Rules format** to **Manually configure detection rules**.
2. Click **Add**, select **File**, and enter:

   | Field | Value |
   | --- | --- |
   | Path | `C:\Program Files\UnityGateway` |
   | File or folder | `provisioned.json` |
   | Detection method | **File or folder exists** |
   | Associated with a 32-bit app on 64-bit clients | **No** |

3. Click **OK**, then **Next**.

The script writes this marker only after configure and profile setup finish. This is an
**initial-install pilot** rule, not a software-version or policy-refresh rule. A device that already
has this file will be considered installed without running the app. Use a clean enrolled device
for the delivery test; do not delete the entire shared installation just to force detection.

On **Dependencies** and **Supersedence**, add nothing and click **Next**. The script installs its
own dependencies for this pilot.

## 8. Assign and create the app

On **Assignments**:

1. Under **Required**, click **Add group**.
2. Select **Unity Gateway Windows pilot**, the group containing only your test device.
3. Do not assign to **All users** or **All devices**. Leave **Available** and **Uninstall** empty.
4. Click **Next**, review the summary, and click **Create**.

**Expected:** the app exists and has a Required assignment to the pilot group.

## 9. Sync the device and watch installation

1. In Intune, open **Devices > All devices > your Windows device > Sync** and confirm.
2. Keep the device powered on and connected. A sync requests check-in; it does not guarantee
   immediate Win32 installation. Do not run the script yourself while waiting.
3. Open **Apps > All apps > Unity Gateway Windows pilot > Device install status**.
4. Wait for your device to show **Installed**. The status page can lag the device's actual work.

On the Windows device, check whether the completion marker exists:

```powershell
Test-Path 'C:\Program Files\UnityGateway\provisioned.json'
```

**Expected:** `True` after a successful installation. You can also read this non-secret marker:

```powershell
Get-Content 'C:\Program Files\UnityGateway\provisioned.json'
```

It should name your workspace and the `fc1ec310baf083e311a9cf084b8ea25046a4bb02` revision.

If Intune reports **Failed**, stop and use the troubleshooting section below. Do not rotate the
provisioning secret until the intended installation attempts have completed or been cancelled.

## 10. Verify as the developer

After installation, restart Windows or sign out and back in to pick up the machine PATH. Open a
normal **Windows PowerShell** window, not an administrator window and not one started with
`-NoProfile`.

```powershell
Get-Command ug, claude, codex | Select-Object Name, Source, CommandType
```

**Expected:** `ug` resolves under `C:\Program Files\UnityGateway\bin`, while `claude` and `codex`
are **Function** commands.

Run:

```powershell
claude
```

Sign in to Databricks as the developer if prompted, send a prompt, and verify a response. Exit the
agent, then run:

```powershell
codex
```

Complete a prompt there too. These developer sessions use the developer's identity, not the
provisioning service principal.

## If a step fails

Send the step number, the exact error text, and the app's **Device install status** error code.
Redact credentials and tokens before sharing anything. Do not send the populated install command.

On the device, Intune logs are under:

```text
C:\ProgramData\Microsoft\IntuneManagementExtension\Logs
```

Start with `AppWorkload.log` and `IntuneManagementExtension.log`; `AgentExecutor.log` may also
contain script-execution details. Search for the app name, the app id from Intune, or
`windows-provision.ps1`. Do not upload the whole log folder without checking it for secrets.

| Symptom | Check |
| --- | --- |
| Intune says Installed immediately, but nothing ran | Check whether `provisioned.json` already existed before assignment. |
| No attempt appears on the device | Confirm device-group membership, Required assignment and requirement applicability. An MDM sync is not proof the app ran. |
| Download or npm/PyPI connection errors | Check network/proxy rules in SYSTEM context. If proxies must be added to the script, edit the source and repackage before retrying. |
| `claude` or `codex` was not installed | Check configure's output, both enabled agents/model access, and that the install command includes the supplied `-UgRef`. |
| PowerShell profile cannot load | Check `Get-ExecutionPolicy -List`. Group Policy may block unsigned profiles; do not override organization policy. |
| `ug` is not recognized | Restart/sign out, then check `C:\Program Files\UnityGateway\bin\ug.exe`. |

If profiles are blocked, the direct launch command is:

```powershell
$env:UG_SKIP_OS_SETTINGS = '1'; ug claude
# Or: ug codex
```

## After the pilot

- **Credentials:** revoke/rotate the dedicated provisioning secret after the deployment window.
  Existing users sign in with their own OAuth. Keep access to Intune app settings restricted.
- **Wider rollout:** only expand the Required group after successful Intune delivery and both
  standard-user launches. Repeat on each architecture you intend to deploy.
- **Updates:** prepare and pilot a new deployment revision, version-aware detection and
  supersedence. The marker-existence rule above will not refresh a working installation merely
  because the workspace policy changed. Reruns reinstall the selected `ug` revision but reuse
  existing dependencies; this script is not a fleet updater. Node.js, the CLI and npm agents are
  selected at first installation rather than fully pinned by this package.
- **Removal:** remove the Required assignment before assigning Uninstall to the test group. Close
  running agents. Uninstall runs `ug revert`, removes this deployment's PowerShell blocks and PATH
  entries, and deletes `C:\Program Files\UnityGateway`. It leaves shared Git, execution policy
  and developers' Databricks credentials alone. Do not delete the runtime before reverting its
  managed settings, which can reference its absolute path.

Reference: [Microsoft's Win32 app deployment settings](https://learn.microsoft.com/en-us/intune/app-management/deployment/add-win32).
