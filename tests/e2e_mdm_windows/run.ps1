# Scenario: SYSTEM provisions a disposable Windows host; a fresh non-admin launches both agents.
# Expected: real inference succeeds without changing machine files or retaining provisioning secrets.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$UgRef)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. "$PSScriptRoot/helpers.ps1"
. "$PSScriptRoot/native.ps1"
$script:Secrets = @($env:UG_MDM_E2E_CLIENT_SECRET)
$machine = $null
$stage = $null
try {
  if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitProcess -or
      $env:UG_MDM_E2E -ne '1') { throw 'Requires UG_MDM_E2E=1 on a disposable 64-bit Windows host.' }
  $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
  if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Needs an elevated controller.' }
  foreach ($name in 'UG_MDM_E2E_WORKSPACE', 'UG_MDM_E2E_CLIENT_ID', 'UG_MDM_E2E_CLIENT_SECRET') {
    if (-not (Get-Item "Env:$name" -ErrorAction SilentlyContinue).Value) { throw "Set $name." }
  }
  if ($UgRef -notmatch '^[0-9a-f]{40}$') { throw 'UgRef must be the full checkout commit SHA.' }
  $workspace = $env:UG_MDM_E2E_WORKSPACE.TrimEnd('/')
  $uri = $null
  if (-not [Uri]::TryCreate($workspace, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -ne 'https' -or
      $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne '/') { throw 'Invalid workspace origin.' }
  $prefix = Join-Path $env:ProgramW6432 'UnityGateway'
  $policyDir = Join-Path $env:ProgramData 'UnityGateway'
  $claudeDir = Join-Path $env:ProgramW6432 'ClaudeCode'
  $codexDir = Join-Path $env:ProgramData 'OpenAI\Codex'
  $policyPath = Join-Path $policyDir 'managed-settings.json'
  $claudePath = Join-Path $claudeDir 'managed-settings.json'
  $codexPath = Join-Path $codexDir 'requirements.toml'
  foreach ($path in $prefix, $policyDir, $claudeDir, $codexDir) {
    if (Test-Path -LiteralPath $path) { throw "Not a clean machine: $path already exists." }
  }
  if (Get-Command claude, codex -ErrorAction SilentlyContinue) { throw 'Agents already installed on controller PATH.' }

  $user = 'ugmdm' + [guid]::NewGuid().ToString('N').Substring(0, 8)
  $password = 'Ug!9' + [guid]::NewGuid().ToString('N')
  $script:Secrets += $password
  $account = New-LocalUser -Name $user -Password (ConvertTo-SecureString $password -AsPlainText -Force) `
    -Description 'Disposable Unity Gateway MDM E2E user' -AccountNeverExpires
  $machine = @{ User = $user; Password = $password; UserSid = $account.SID.Value }
  Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $user
  [MdmWindows]::BatchLogon($machine.UserSid, $true)
  $machine.BatchLogonGranted = $true
  Assert-Mdm (-not (Get-LocalGroupMember -SID 'S-1-5-32-544' | Where-Object SID -eq $account.SID)) 'fresh user is not an administrator'

  # Stage only code in a protected directory; credentials travel over the restricted pipe.
  $stage = Join-Path $env:ProgramData ('UgMdmE2E-' + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $stage | Out-Null
  $acl = New-Object Security.AccessControl.DirectorySecurity
  $acl.SetAccessRuleProtection($true, $false)
  foreach ($sid in 'S-1-5-18', 'S-1-5-32-544', $account.SID.Value) {
    $rights = if ($sid -eq $account.SID.Value) { 'ReadAndExecute' } else { 'FullControl' }
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
      (New-Object Security.Principal.SecurityIdentifier($sid)), $rights, 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
  }
  Set-Acl -LiteralPath $stage -AclObject $acl
  Copy-Item "$PSScriptRoot/task-worker.ps1" $stage
  Copy-Item "$PSScriptRoot/../../scripts/mdm/windows-provision.ps1" $stage
  $machine.Directory = $stage
  $machine.Worker = Join-Path $stage 'task-worker.ps1'
  $scriptPath = (Join-Path $stage 'windows-provision.ps1').Replace("'", "''")

  $identity = Invoke-MdmTask $machine 'USER' 'Write-Output $env:USERPROFILE'
  Assert-Mdm ($identity.code -eq 0) 'standard-user task starts without UAC or a password prompt'
  $userHome = $identity.home
  $timeoutCommand = @'
$child = Start-Process "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -PassThru -ArgumentList '-NoProfile -NonInteractive -Command Start-Sleep -Seconds 120'
Set-Content -LiteralPath (Join-Path $env:USERPROFILE 'mdm-timeout-child.txt') -Value $child.Id
Start-Sleep -Seconds 120
'@
  $timed = Invoke-MdmTask $machine 'USER' $timeoutCommand -Timeout 5
  Assert-Mdm ($timed.code -eq -1 -and $timed.stderr -match 'timed out') 'worker timeouts fail rather than passing'
  $receipt = Join-Path $userHome 'mdm-timeout-child.txt'
  $descendant = Get-Process -Id ([int](Get-Content -LiteralPath $receipt)) -ErrorAction SilentlyContinue
  if ($descendant) { $null = $descendant.WaitForExit(5000) }
  Assert-Mdm (-not $descendant -or $descendant.HasExited) 'timed-out worker leaves no running descendant'
  Remove-Item -LiteralPath $receipt
  foreach ($relative in '.ucode', '.claude', '.claude.json', '.codex', '.databricks', '.databrickscfg') {
    Assert-Mdm (-not (Test-Path -LiteralPath (Join-Path $userHome $relative))) "user home is initially clean: $relative"
  }
  $provisionEnvironment = @{
    UG_MDM_INPUT_WORKSPACE = $workspace
    UG_MDM_INPUT_CLIENT_ID = $env:UG_MDM_E2E_CLIENT_ID
    UG_MDM_INPUT_CLIENT_SECRET = $env:UG_MDM_E2E_CLIENT_SECRET
    PYTHONUTF8 = '1'
  }
  foreach ($name in 'UV_DEFAULT_INDEX', 'NPM_CONFIG_REGISTRY', 'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY',
      'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS') {
    if (Test-Path "Env:$name") { $provisionEnvironment[$name] = (Get-Item "Env:$name").Value }
  }
  $command = @"
`$ErrorActionPreference = 'Stop'
`$workspace = `$env:UG_MDM_INPUT_WORKSPACE
`$clientId = `$env:UG_MDM_INPUT_CLIENT_ID
`$secret = `$env:UG_MDM_INPUT_CLIENT_SECRET
Remove-Item Env:UG_MDM_INPUT_WORKSPACE, Env:UG_MDM_INPUT_CLIENT_ID, Env:UG_MDM_INPUT_CLIENT_SECRET
& '$scriptPath' -Workspace `$workspace -ClientId `$clientId -ClientSecret `$secret -UgRef '$UgRef'
"@
  $result = Invoke-MdmTask $machine 'SYSTEM' $command $provisionEnvironment -Timeout 1800
  Write-Output (Protect-Output ($result.stdout + $result.stderr))
  Assert-Mdm ($result.code -eq 0) 'SYSTEM runs the real provisioning script without interactive input'
  Assert-Mdm (($result.stdout + $result.stderr) -notmatch 'Failed to configure') 'configure does not silently skip failed agents'
  $systemHome = $result.home
  foreach ($relative in '.ucode', '.claude', '.claude.json', '.codex', '.databricks', '.databrickscfg') {
    Assert-Mdm (-not (Test-Path -LiteralPath (Join-Path $userHome $relative))) "SYSTEM provisioning left the user home untouched: $relative"
  }
  $record = Get-Content -Raw (Join-Path $prefix 'provisioned.json') | ConvertFrom-Json
  Assert-Mdm ($record.workspace -eq $workspace -and $record.ugRef -eq $UgRef) 'detection records the workspace and tested commit'
  Assert-Mdm ($record.scriptSha256 -eq (Get-FileHash -Algorithm SHA256 -LiteralPath $scriptPath).Hash) 'detection records the actual provisioning script hash'
  $policy = Get-Content -Raw -LiteralPath $policyPath | ConvertFrom-Json
  Assert-Mdm ($policy.workspace.TrimEnd('/') -eq $workspace) 'machine workspace policy selects the test workspace'
  Assert-Mdm ($policy.mdm_os -eq 'Windows') '--mdm persists the Windows machine marker'
  $enabled = @($policy.config.enabled_agents | ForEach-Object agent)
  Assert-Mdm ('CODING_AGENT_CLAUDE_CODE' -in $enabled -and 'CODING_AGENT_CODEX' -in $enabled) 'workspace policy enables both agents'
  $claude = Get-Content -Raw -LiteralPath $claudePath | ConvertFrom-Json
  $ug = Join-Path $prefix 'bin\ug.exe'
  Assert-Mdm ($claude.apiKeyHelper.Contains($ug) -and $claude.apiKeyHelper.Contains('auth-token') -and
    $claude.apiKeyHelper.Contains($workspace)) 'Claude authentication uses the shared ug and workspace'
  Assert-Mdm ($claude.env.ANTHROPIC_BASE_URL -eq "$workspace/ai-gateway/anthropic") 'Claude uses the workspace gateway'
  Assert-Mdm ([bool]$claude.availableModels) 'Claude has managed models'
  foreach ($relative in '.claude-plugin\marketplace.json', 'ug-headers\.claude-plugin\plugin.json',
      'ug-headers\hooks\hooks.json', 'ug-headers\hooks\register.ts') {
    Assert-Mdm (Test-Path -LiteralPath (Join-Path $claudeDir "ucode-plugins\$relative")) "Claude header mod installed: $relative"
  }
  # Parse the real TOML with the installed Python, not application internals.
  $codexCheck = @'
import json, sys, tomllib
with open(sys.argv[1], 'rb') as f:
    settings = tomllib.load(f)
provider = settings['model_providers'][settings['model_provider']]
assert provider['base_url'] == sys.argv[2] + '/ai-gateway/codex/v1'
assert provider['auth']['command'] == sys.argv[3]
print('codex-settings-ok')
'@
  $python = Join-Path $prefix 'tools\unity-gateway\Scripts\python.exe'
  $checkFile = Join-Path $stage 'check-codex.py'
  Set-Content -LiteralPath $checkFile -Value $codexCheck -Encoding UTF8
  $checkCommand = "& '$python' -I '$checkFile' '$codexPath' '$workspace' '$ug'; exit `$LASTEXITCODE"
  $check = Invoke-MdmTask $machine 'SYSTEM' $checkCommand
  Assert-Mdm ($check.code -eq 0 -and $check.stdout -match 'codex-settings-ok') 'Codex requirements use shared authentication and the workspace gateway'
  foreach ($relative in 'npm\claude.cmd', 'npm\codex.cmd', 'node\npm.cmd', 'bin\databricks.exe') {
    Assert-Mdm (Test-Path -LiteralPath (Join-Path $prefix $relative)) "shared dependency installed: $relative"
  }
  $systemNpm = Join-Path $systemHome 'AppData\Roaming\npm'
  Assert-Mdm (-not (Test-Path -LiteralPath (Join-Path $systemNpm 'claude.cmd')) -and
    -not (Test-Path -LiteralPath (Join-Path $systemNpm 'codex.cmd'))) 'agents are not installed in SYSTEM profile npm'
  $roots = @($prefix, $policyDir, $claudeDir, $codexDir,
    "$env:SystemRoot\System32\WindowsPowerShell\v1.0\profile.ps1")
  $pwsh7 = Join-Path $env:ProgramW6432 'PowerShell\7'
  if (Test-Path -LiteralPath $pwsh7) { $roots += Join-Path $pwsh7 'profile.ps1' }
  Assert-MdmProtected $roots
  $before = Get-MdmSnapshot $roots

  # A developer token substitutes for browser sign-in; the SP secret never reaches the user.
  $basic = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(
    "$($env:UG_MDM_E2E_CLIENT_ID):$($env:UG_MDM_E2E_CLIENT_SECRET)"))
  $script:Secrets += $basic
  $token = Invoke-RestMethod -UseBasicParsing -Method Post -Uri "$workspace/oidc/v1/token" `
    -Headers @{ Authorization = "Basic $basic" } -ContentType 'application/x-www-form-urlencoded' `
    -Body @{ grant_type = 'client_credentials'; scope = 'ai-gateway unity-catalog' }
  if (-not $token.access_token) { throw 'No inference token returned.' }
  $script:Secrets += $token.access_token
  $launchEnvironment = @{ DATABRICKS_BEARER = $token.access_token; PYTHONUTF8 = '1' }
  foreach ($agent in 'claude', 'codex') {
    $launch = if ($agent -eq 'claude') {
      "claude -p 'What is 6 times 7? Reply with only the number.' --max-turns 1"
    } else { "codex exec --skip-git-repo-check 'What is 6 times 7? Reply with only the number.'" }
    $userCommand = @"
`$ErrorActionPreference = 'Stop'
Set-Location `$env:USERPROFILE
if ((Get-Command '$agent').CommandType -ne 'Function') { throw 'PowerShell alias did not load.' }
`$binary = (Get-Command '$agent.cmd').Source
if (`$binary -ne '$prefix\npm\$agent.cmd') { throw 'Agent resolved outside the machine npm prefix.' }
`$previous = `$env:UG_SKIP_OS_SETTINGS
$launch
`$code = `$LASTEXITCODE
if (`$env:UG_SKIP_OS_SETTINGS -cne `$previous) { throw 'Alias leaked UG_SKIP_OS_SETTINGS.' }
exit `$code
"@
    $answer = Invoke-MdmTask $machine 'USER' $userCommand $launchEnvironment -Timeout 300 -LoadProfile
    Write-Output (Protect-Output ($answer.stdout + $answer.stderr))
    Assert-Mdm ($answer.code -eq 0) "$agent alias launches without administrator rights and restores the environment"
    Assert-Mdm ($answer.stdout -match '(?m)^\W*42\W*$') "$agent completes real headless inference with the correct answer on stdout"
    Assert-MdmSnapshot $before (Get-MdmSnapshot $roots)
  }
  foreach ($relative in '.ucode\state.json', '.claude\ucode-settings.json', '.codex\ucode.config.toml') {
    $path = Join-Path $userHome $relative
    Assert-Mdm (Test-Path -LiteralPath $path) "user-owned state created: $relative"
    $owner = (Get-Acl -LiteralPath $path).GetOwner([Security.Principal.SecurityIdentifier]).Value
    Assert-Mdm ($owner -eq $machine.UserSid) "state belongs to the standard user: $relative"
  }
  Assert-Mdm (-not (Test-Path -LiteralPath (Join-Path $userHome '.ucode\managed-config.json'))) 'launch reuses machine policy rather than caching a user policy'
  $scanRoots = @($policyDir, $claudeDir, $codexDir, (Join-Path $prefix 'provisioned.json'),
    (Join-Path $prefix 'windows-provision.ps1'))
  foreach ($relative in '.ucode', '.claude', '.claude.json', '.codex', '.databricks', '.databrickscfg', '.config') {
    $scanRoots += Join-Path $systemHome $relative
  }
  $leak = Find-MdmCredentialLeak $scanRoots
  Assert-Mdm (-not $leak) "no provisioning secret or bearer persists in managed files or SYSTEM state (leak=$leak)"
  Write-Output 'PASS: Windows SYSTEM-to-standard-user provisioning E2E completed (headless, not browser sign-in or Intune delivery)'
} catch {
  Write-Output ('FAIL: ' + (Protect-Output $_.Exception.Message))
  exit 1
} finally {
  $cleanupErrors = @()
  if ($machine) {
    foreach ($relative in 'bin\ug.exe', 'npm\claude.cmd', 'npm\codex.cmd') {
      $path = Join-Path $prefix $relative
      if (Test-Path -LiteralPath $path) {
        try {
          Write-Output "Installed version: $relative"
          $versionCommand = "& '$path' --version; exit `$LASTEXITCODE"
          $version = Invoke-MdmTask $machine 'SYSTEM' $versionCommand -Timeout 30
          Write-Output (Protect-Output ($version.stdout + $version.stderr))
          if ($version.code -ne 0) { throw "Version command failed: $relative" }
        } catch { $cleanupErrors += Protect-Output $_.Exception.Message }
      }
    }
    if ($machine.UserSid) {
      try {
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        do {
          $profile = Get-CimInstance Win32_UserProfile | Where-Object SID -eq $machine.UserSid
          if (-not $profile -or -not $profile.Loaded) { break }
          Start-Sleep -Milliseconds 200
        } while ([DateTime]::UtcNow -lt $deadline)
        if ($profile.Loaded) { throw 'Test user profile remained loaded after task termination.' }
        if ($profile) { $profile | Remove-CimInstance }
      } catch { $cleanupErrors += Protect-Output $_.Exception.Message }
    }
    if ($machine.BatchLogonGranted) {
      try { [MdmWindows]::BatchLogon($machine.UserSid, $false) }
      catch { $cleanupErrors += Protect-Output $_.Exception.Message }
    }
    try { Remove-LocalUser -Name $machine.User }
    catch { $cleanupErrors += Protect-Output $_.Exception.Message }
  }
  if ($stage -and (Test-Path -LiteralPath $stage)) {
    try { Remove-Item -Recurse -Force -LiteralPath $stage }
    catch { $cleanupErrors += Protect-Output $_.Exception.Message }
  }
  $script:Secrets = @()
  if ($cleanupErrors.Count) { Write-Output ('FAIL: cleanup: ' + ($cleanupErrors -join '; ')); exit 1 }
}
