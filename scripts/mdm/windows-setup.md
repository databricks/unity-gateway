# Set up Unity Gateway on Windows with Intune

Deploy [windows-provision.ps1](windows-provision.ps1) as an Intune **Windows app (Win32)**,
running as SYSTEM in 64-bit Windows PowerShell. This follows the Jamf flow: install shared tools,
exchange service-principal credentials for a short-lived token, and run `ug configure --global`.
Developers sign in with their own Databricks accounts when they launch an agent.

This is a draft deployment procedure. The revised script has helper tests, but its full install,
uninstall, and SYSTEM-to-standard-user flow still need to be tested on Windows and through Intune.

## Before you start

- Windows 11 x64 or ARM64, enrolled in Intune with the Intune Management Extension installed.
- A workspace with Claude Code and Codex enabled in the Unity Gateway coding-agent configuration,
  and models available for both agents.
- A dedicated provisioning service principal with **Workspace access** and access to the models,
  MCP servers and skills in that configuration. Generate an OAuth secret with the shortest lifetime
  that covers your rollout. Use the `ai-gateway` and `unity-catalog` **Read** scopes
  (`ai-gateway:read unity-catalog:read`). See [Create the service principal](macos-setup.md#1-create-the-service-principal).
- Network access to the workspace, GitHub (`github.com`, `raw.githubusercontent.com` and release
  asset download hosts), PyPI (`pypi.org`, `files.pythonhosted.org`), `nodejs.org`, and the npm
  registry (`registry.npmjs.org`). If required, configure your approved PyPI/npm proxies in a
  deployment wrapper using `UV_DEFAULT_INDEX` and `npm_config_registry`.
- Review your execution-policy requirements. The script changes an unset or `Restricted`
  LocalMachine policy to `RemoteSigned` so the PowerShell aliases load. It keeps `AllSigned` and
  does not override Group Policy. Under a policy requiring signatures, your organization must sign
  the resulting profiles or use `ug claude` / `ug codex` directly.

## 1. Supply deployment inputs

The script accepts these named PowerShell parameters. It does not prompt, so missing credentials
fail the deployment rather than hanging an unattended install.

| Parameter | Environment fallback | Purpose |
| --- | --- | --- |
| `-Workspace` | `DATABRICKS_HOST` | Workspace origin, starting with `https://` |
| `-ClientId` | `DATABRICKS_CLIENT_ID` | Provisioning service principal client id |
| `-ClientSecret` | `DATABRICKS_CLIENT_SECRET` | Provisioning service principal secret |
| `-UgRef` | None | Release tag or commit; defaults to the commit pinned in the script |
| `-Uninstall` | None | Revert and remove this deployment; no credentials required |

### Credential handling

Intune's Win32 **Install command** supports arguments, unlike the Platform scripts upload, which
has no parameter field. Named arguments are the closest equivalent to Jamf policy parameters.
They are **not a secure secret channel**: a secret supplied in the command can be visible to
Intune administrators and through endpoint process auditing or management logs. Microsoft
[advises against putting sensitive information in deployed scripts](https://learn.microsoft.com/en-us/intune/device-management/tools/run-powershell-scripts-windows#before-you-begin).

The example install command below uses a `-ClientSecret` argument for Jamf-style parity. Use it
only if your organization's credential-handling policy permits that exposure, restrict access to
the Intune app configuration, and rotate or revoke the secret after the deployment window.
Keep secrets out of the script and the `.intunewin` package. The script does not write its
credentials into the installed copy, marker, or generated profiles, but this does not erase
Intune's records or endpoint logs.

If your organization already has an approved credential-delivery mechanism, use a wrapper that
obtains the secret at runtime, sets `DATABRICKS_CLIENT_SECRET` **for that process only**, and calls
the script. Omit `-ClientSecret` from the Intune command in that case. Intune does not populate this
variable for you. Do not store the secret in machine environment variables or a packaged config
file. This guide does not add a credential broker or assume that Windows devices can access a
secret vault without authentication.

## 2. Test the script manually

Start with a clean VM snapshot. In **64-bit Windows PowerShell as administrator**, from the folder
containing the script, prompt for the credential so it is not saved in the file or a literal
command-line argument:

```powershell
$env:DATABRICKS_HOST = 'https://<workspace>.cloud.databricks.com'
$credential = Get-Credential -UserName '<client-id>' -Message 'Provisioning service principal'
$env:DATABRICKS_CLIENT_ID = $credential.UserName
$env:DATABRICKS_CLIENT_SECRET = $credential.GetNetworkCredential().Password
try {
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows-provision.ps1
  if ($LASTEXITCODE -ne 0) { throw "Provisioning failed: $LASTEXITCODE" }
} finally {
  Remove-Item Env:DATABRICKS_CLIENT_SECRET -ErrorAction SilentlyContinue
  Remove-Item Env:DATABRICKS_CLIENT_ID -ErrorAction SilentlyContinue
  Remove-Item Env:DATABRICKS_HOST -ErrorAction SilentlyContinue
  $credential = $null
}
```

This administrator test is not an Intune/SYSTEM test. Do the pilot in step 5 before expanding the
assignment. Rerun the script to check repeatability, then restart Windows and verify the aliases.

## 3. Package the script

1. Create a source folder containing only `windows-provision.ps1` (and your approved wrapper, if
   using one). Do not include credentials, test output or local configuration.
2. Select the `ug` revision you have validated. The default is pinned in the script; pass a release
   tag or full commit with `-UgRef` to use another one. Do not use a moving branch for a production
   rollout.
3. Use the [Microsoft Win32 Content Prep Tool](https://learn.microsoft.com/en-us/intune/app-management/deployment/create-win32-package):

   ```powershell
   IntuneWinAppUtil.exe -c C:\UGPackage -s windows-provision.ps1 -o C:\UGOutput
   Get-FileHash C:\UGPackage\windows-provision.ps1 -Algorithm SHA256
   ```

   Keep the output folder outside the source folder. Record the script hash for the detection rule.

Git and uv download versions/hashes are pinned in the script. A first install selects the current
Node.js LTS and the Databricks CLI version from `setup-cli`, and `ug` selects the npm agent packages.
The deployment is not fully reproducible unless your organization also pins or mirrors those
inputs. Review the downloaded tools against your software policy before rollout.

## 4. Create the Win32 app

In the Intune admin center, select **Apps > All apps > Create > Windows app (Win32)** and upload the
`.intunewin` package. See Microsoft's [Win32 app instructions](https://learn.microsoft.com/en-us/intune/app-management/deployment/add-win32).

On **Program**, use **Command line** as the installer type:

| Setting | Value |
| --- | --- |
| Install behavior | **System** |
| Device restart behavior | **No specific action** |
| Allow available uninstall | **No** during the pilot |

**Install command** (replace placeholders; apply the credential warning in step 1):

```text
%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\windows-provision.ps1" -Workspace "https://<workspace>.cloud.databricks.com" -ClientId "<client-id>" -ClientSecret "<client-secret>" -UgRef "<approved-tag-or-full-commit>"
```

Do not paste a real secret into a terminal to test this command. Use the prompted test above.
Verify quoting for your actual credential; do not print the populated command to diagnose it.
An approved wrapper can instead provide the credential through the environment fallback.

**Uninstall command**:

```text
%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Program Files\UnityGateway\windows-provision.ps1" -Uninstall
```

The script keeps an uncredentialed copy at that path after a successful install. For a custom
Windows installation drive, adjust the path. `Sysnative` selects the 64-bit host when launched by
the 32-bit Intune management process; running plain `powershell.exe` there can select 32-bit
PowerShell, which the script refuses.

On **Requirements**, target Windows 11 and the architectures you tested (x64 and/or ARM64).
Use separate pilot assignments if the architecture-specific dependencies differ.

On **Detection rules**, choose **Use a custom detection script**, with
**Run script as 32-bit process on 64-bit clients: No**. Use the script below, replacing all three
expected values with the exact script hash, workspace and `UgRef` used in the install command:

```powershell
$ErrorActionPreference = 'Stop'
$expectedHash = '<script-sha256-from-step-3>'
$expectedWorkspace = 'https://<workspace>.cloud.databricks.com'
$expectedUgRef = '<approved-tag-or-full-commit>'
$root = Join-Path $env:ProgramW6432 'UnityGateway'
$marker = Join-Path $root 'provisioned.json'
if (-not (Test-Path -LiteralPath $marker)) { exit 1 }
try { $record = Get-Content -Raw -LiteralPath $marker | ConvertFrom-Json }
catch { exit 1 }
if ($record.scriptSha256 -ne $expectedHash -or
    $record.workspace -ne $expectedWorkspace -or $record.ugRef -ne $expectedUgRef) { exit 1 }
foreach ($relative in 'bin\ug.exe', 'bin\databricks.exe', 'node\node.exe',
                      'npm\claude.cmd', 'npm\codex.cmd', 'windows-provision.ps1') {
  if (-not (Test-Path -LiteralPath (Join-Path $root $relative))) { exit 1 }
}
Write-Output 'Unity Gateway provisioned'
exit 0
```

The marker is written only after configure and profile installation finish. Intune detection needs
both exit code 0 and output on stdout; a missing marker or binary returns 1. File existence alone
does not establish that an agent can authenticate: verify that separately in the pilot.

## 5. Pilot as SYSTEM, then verify as a user

Assign the app as **Required** to a small test-device group. Check installation status in Intune
and the device's logs under `C:\ProgramData\Microsoft\IntuneManagementExtension\Logs`:
`IntuneManagementExtension.log`, `AppWorkload.log`, and `AgentExecutor.log` where applicable.
Treat collected logs as sensitive. Do not enable PowerShell tracing or dump environments containing
credentials.

After installation, sign out and back in (or restart Windows) so new terminals inherit the updated
machine PATH. As a **standard user**, open Windows PowerShell and run:

```powershell
Get-Command ug, claude, codex | Select-Object Name, Source, CommandType
$before = $env:UG_SKIP_OS_SETTINGS
claude
$env:UG_SKIP_OS_SETTINGS -eq $before
codex
$env:UG_SKIP_OS_SETTINGS -eq $before
```

`claude` and `codex` should be functions. Launch each separately, sign in to Databricks as the
developer when prompted, and complete a prompt before exiting. The environment comparisons should
print `True`. Repeat after a Windows restart. Confirm the agent installations are under
`C:\Program Files\UnityGateway\npm`, not SYSTEM's profile.

If the profile is blocked, check `Get-ExecutionPolicy -List` against your organization's policy.
In a shell that doesn't load the profile, launch directly:

```powershell
$env:UG_SKIP_OS_SETTINGS = '1'; ug claude
# Or ug codex
```

Aliases apply to Windows PowerShell and an existing PowerShell 7 installation, not cmd.exe or Git
Bash. If PowerShell 7 is installed later, rerun provisioning to install its profile block.

## Updates, secret rotation and removal

- **Policy refresh:** unlike a recurring Jamf policy, Win32 detection prevents another install
  while the expected deployment is present. Plan a new deployment revision or a separate,
  explicitly scheduled management action when the workspace policy needs to be refreshed.
- **Software updates:** reruns reinstall the requested `ug` revision. They reuse existing Git,
  uv, Node.js and Databricks CLI installations. Agent version handling remains `ug`'s existing
  behavior; this is not a general fleet updater. Test updates in a pilot and distribute a revised
  package/detection rule through Intune supersedence.
- **Secret rotation:** rotate or revoke the provisioning secret after the rollout window. Before
  a later rollout, replace it in your approved credential source or Intune install command. Existing
  devices use each developer's OAuth and do not need the provisioning secret to launch agents.
- **Removal:** remove the Required assignment before assigning Uninstall. Close running agents.
  Uninstall runs `ug revert` as SYSTEM, removes the managed alias blocks and this deployment's
  PATH entries, and deletes `C:\Program Files\UnityGateway` (including its npm agents and marker).
  It leaves Git for Windows, execution policy, and individual users' Databricks credentials alone.
  Review any pre-existing managed settings that `ug revert` restores from backups. If users also
  ran per-user configuration, have them run `ug revert` in their own session where needed.

Do not delete the shared `ug` runtime before reverting managed settings: those settings can reference
its absolute path for authentication. Do not delete all of `C:\ProgramData\UnityGateway`; it is a
separate machine-policy location managed by `ug`, not the install directory.
