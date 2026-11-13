# Run as SYSTEM or administrator in 64-bit PowerShell. Like the Jamf script,
# exchanges provisioning credentials for a token; developers use their own OAuth.
[CmdletBinding()]
param(
  [string]$Workspace = $env:DATABRICKS_HOST,
  [string]$ClientId = $env:DATABRICKS_CLIENT_ID,
  [string]$ClientSecret = $env:DATABRICKS_CLIENT_SECRET,
  [string]$UgRef = '4504ca99115801f033149dc92d936ed80a2246f9',
  [switch]$Uninstall
)

# Check inputs and set shared install locations
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if (-not [Environment]::Is64BitProcess) { throw 'Run this script in 64-bit PowerShell.' }
$principal = New-Object Security.Principal.WindowsPrincipal(
  [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw 'Run this script as administrator or SYSTEM.'
}
if (-not $Uninstall) {
  if (-not $Workspace -or -not $ClientId -or -not $ClientSecret) {
    throw 'Supply Workspace, ClientId and ClientSecret as parameters or DATABRICKS_HOST, DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET environment variables.'
  }
  $uri = $null
  if (-not [Uri]::TryCreate($Workspace, [UriKind]::Absolute, [ref]$uri) -or
      $uri.Scheme -ne 'https' -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or
      $uri.AbsolutePath -ne '/') { throw 'Workspace must be an https:// workspace origin.' }
  if (-not $UgRef) { throw 'Supply a ug release tag or commit with -UgRef.' }
  $Workspace = $Workspace.TrimEnd('/')
}
$nativeArch = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment').PROCESSOR_ARCHITECTURE
if ($nativeArch -notin 'ARM64', 'AMD64') { throw "Unsupported architecture: $nativeArch" }
$Arm = $nativeArch -eq 'ARM64'
$Prefix = Join-Path $env:ProgramW6432 'UnityGateway'
$BinDir = Join-Path $Prefix 'bin'
$NodeDir = Join-Path $Prefix 'node'
$NpmDir = Join-Path $Prefix 'npm'
$NodeReleases = 'https://nodejs.org/dist'
$CliReleases = 'https://github.com/databricks/cli/releases/download'
$CliVersionUrl = 'https://raw.githubusercontent.com/databricks/setup-cli/main/VERSION'
$UgRepo = 'https://github.com/databricks/unity-gateway'
$GitVersion = '2.56.0.windows.4'
$UvVersion = '0.13.7'
$GitHashes = @{
  AMD64 = '4961a4a8cc778d8b6e00a5819e5fe4d078b754555aff7aedc2d77a5d74c2de80'
  ARM64 = 'e7dcc4adbb0eb373064598cd18bc75e60128707d816ead51cd9bd98e356f0075'
}
$UvHashes = @{
  AMD64 = '0cdc5b38fe5a8168ed6a25814f5ba43c21d313dd3187314fccb37b7cec46e021'
  ARM64 = '505d9e65e7692bac3ddf98c66754aa26c5050c409c184af30a881a0a72cc85e3'
}
$env:PYTHONUTF8 = '1'
$env:UV_TOOL_DIR = Join-Path $Prefix 'tools'
$env:UV_TOOL_BIN_DIR = $BinDir
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Prefix 'python'
$env:UV_PYTHON_PREFERENCE = 'only-managed'
$env:npm_config_prefix = $NpmDir

# Download, checksum, PATH and profile helpers
function Get-File($Url, $OutFile) {
  Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $OutFile
}
function Get-Text($Url) {
  $file = Join-Path $Tmp ([guid]::NewGuid())
  Get-File $Url $file
  (Get-Content -Raw -LiteralPath $file).Trim()
}
function Assert-Sha256($Path, $Expected) {
  if ($Expected -notmatch '^[a-fA-F0-9]{64}$' -or
      (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash -ne $Expected) {
    throw "Checksum mismatch or missing SHA256 for $Path"
  }
}
function Get-Sha256Sum($SumsFile, $Name) {
  foreach ($line in Get-Content -LiteralPath $SumsFile) {
    $parts = $line -split '\s+', 2
    if ($parts.Count -eq 2 -and $parts[1].TrimStart('*') -eq $Name) { return $parts[0] }
  }
  throw "No checksum for $Name"
}
function Update-PathEntry($Path, $Dir, [switch]$Remove) {
  if ($Remove) { return (($Path -split ';' | Where-Object { $_ -ne $Dir }) -join ';') }
  if (($Path -split ';') -contains $Dir) { return $Path }
  return ($Path.TrimEnd(';') + ';' + $Dir).TrimStart(';')
}
function Set-MachinePath($Dir, [switch]$Remove) {
  $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey(
    'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', $true)
  try {
    $path = $key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    $kind = $key.GetValueKind('Path')
    $updated = Update-PathEntry $path $Dir -Remove:$Remove
    if ($updated -ne $path) { $key.SetValue('Path', $updated, $kind) }
  } finally { if ($key) { $key.Dispose() } }
  $env:Path = Update-PathEntry $env:Path $Dir -Remove:$Remove
}
function Get-AgentAliases($Ug) {
  $escaped = $Ug.Replace("'", "''")
  @"
# BEGIN unity-gateway
function claude {
  `$previous = `$env:UG_SKIP_OS_SETTINGS
  try { `$env:UG_SKIP_OS_SETTINGS = '1'; & '$escaped' claude @args }
  finally { `$env:UG_SKIP_OS_SETTINGS = `$previous }
}
function codex {
  `$previous = `$env:UG_SKIP_OS_SETTINGS
  try { `$env:UG_SKIP_OS_SETTINGS = '1'; & '$escaped' codex @args }
  finally { `$env:UG_SKIP_OS_SETTINGS = `$previous }
}
# END unity-gateway
"@
}
function Update-AgentProfile($Text, $Block) {
  $text = "$Text" -replace '(?s)# BEGIN unity-gateway.*?# END unity-gateway\r?\n?', ''
  if (-not $Block) { return $text.TrimEnd() }
  ($text.TrimEnd() + "`r`n" + $Block).TrimStart()
}

$profiles = @("$env:SystemRoot\System32\WindowsPowerShell\v1.0\profile.ps1")
$pwsh7 = Join-Path $env:ProgramW6432 'PowerShell\7'
if (Test-Path -LiteralPath $pwsh7) { $profiles += Join-Path $pwsh7 'profile.ps1' }

# Remove this deployment; leave shared Git and execution policy alone
if ($Uninstall) {
  $ug = Join-Path $BinDir 'ug.exe'
  if (-not (Test-Path -LiteralPath $ug)) { throw 'ug is missing; repair the installation before uninstalling.' }
  $previousSkip = $env:UG_SKIP_OS_SETTINGS
  try {
    $env:UG_SKIP_OS_SETTINGS = $null
    $null | & $ug revert
    if ($LASTEXITCODE -ne 0) { throw "ug revert failed: $LASTEXITCODE" }
  } finally { $env:UG_SKIP_OS_SETTINGS = $previousSkip }
  foreach ($path in $profiles) {
    if (Test-Path -LiteralPath $path) {
      $text = Get-Content -Raw -LiteralPath $path
      Set-Content -LiteralPath $path -Encoding UTF8 -Value (Update-AgentProfile $text '')
    }
  }
  foreach ($dir in $BinDir, $NpmDir, $NodeDir) { Set-MachinePath $dir -Remove }
  Remove-Item -Recurse -Force -LiteralPath $Prefix
  Write-Output 'Unity Gateway deployment removed. Shared Git and execution policy were left unchanged.'
  return
}

New-Item -ItemType Directory -Force -Path $BinDir, $NpmDir | Out-Null
Set-MachinePath $BinDir
Set-MachinePath $NpmDir
$Tmp = Join-Path $Prefix ".tmp-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $Tmp | Out-Null
try {
  # Install Git
  if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
    $suffix = if ($Arm) { 'arm64' } else { '64-bit' }
    $name = "Git-2.56.0.4-$suffix.exe"
    $setup = Join-Path $Tmp $name
    Get-File "https://github.com/git-for-windows/git/releases/download/v$GitVersion/$name" $setup
    Assert-Sha256 $setup $GitHashes[$nativeArch]
    $proc = Start-Process -FilePath $setup -Wait -PassThru `
      -ArgumentList '/VERYSILENT', '/ALLUSERS', '/NORESTART', '/SUPPRESSMSGBOXES', '/SP-'
    if ($proc.ExitCode -ne 0) { throw "Git setup failed: $($proc.ExitCode)" }
    Set-MachinePath (Join-Path $env:ProgramW6432 'Git\cmd')
  }

  # Install uv and ug
  $uv = Join-Path $BinDir 'uv.exe'
  if (-not (Test-Path -LiteralPath $uv)) {
    $target = if ($Arm) { 'aarch64-pc-windows-msvc' } else { 'x86_64-pc-windows-msvc' }
    $zip = Join-Path $Tmp 'uv.zip'
    Get-File "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-$target.zip" $zip
    Assert-Sha256 $zip $UvHashes[$nativeArch]
    Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $Tmp 'uv')
    Copy-Item (Join-Path $Tmp 'uv\uv.exe') $BinDir
  }
  & $uv tool install --force --python cpython-3.13-windows-x86_64-none "git+$UgRepo@$UgRef"
  if ($LASTEXITCODE -ne 0) { throw "uv tool install failed: $LASTEXITCODE" }

  # Install the Databricks CLI
  if (-not (Test-Path -LiteralPath (Join-Path $BinDir 'databricks.exe'))) {
    $version = Get-Text $CliVersionUrl
    $arch = if ($Arm) { 'arm64' } else { 'amd64' }
    $zip = "databricks_cli_${version}_windows_$arch.zip"
    $base = "$CliReleases/v$version"
    Get-File "$base/$zip" (Join-Path $Tmp $zip)
    Get-File "$base/databricks_cli_${version}_SHA256SUMS" (Join-Path $Tmp 'cli-sums')
    Assert-Sha256 (Join-Path $Tmp $zip) (Get-Sha256Sum (Join-Path $Tmp 'cli-sums') $zip)
    Expand-Archive -LiteralPath (Join-Path $Tmp $zip) -DestinationPath (Join-Path $Tmp 'cli')
    Copy-Item (Join-Path $Tmp 'cli\databricks.exe') $BinDir
  }

  # Install Node.js for ug's npm agent installer
  if (-not (Test-Path -LiteralPath (Join-Path $NodeDir 'npm.cmd'))) {
    $releases = Invoke-RestMethod -UseBasicParsing -Uri "$NodeReleases/index.json"
    $version = ($releases | Where-Object { $_.lts } | Select-Object -First 1).version
    if ($version -notmatch '^v\d+\.\d+\.\d+$') { throw 'No valid Node.js LTS release found.' }
    $name = "node-$version-win-$(if ($Arm) { 'arm64' } else { 'x64' })"
    Get-File "$NodeReleases/$version/$name.zip" (Join-Path $Tmp "$name.zip")
    Get-File "$NodeReleases/$version/SHASUMS256.txt" (Join-Path $Tmp 'node-sums')
    Assert-Sha256 (Join-Path $Tmp "$name.zip") (Get-Sha256Sum (Join-Path $Tmp 'node-sums') "$name.zip")
    Expand-Archive -LiteralPath (Join-Path $Tmp "$name.zip") -DestinationPath $Tmp
    New-Item -ItemType Directory -Force -Path $NodeDir | Out-Null
    Copy-Item (Join-Path $Tmp "$name\*") $NodeDir -Recurse -Force
  }
  Set-MachinePath $NodeDir
} finally {
  Remove-Item -Recurse -Force -LiteralPath $Tmp -ErrorAction SilentlyContinue
}

# Get a provisioning token and configure agents with ug's npm fallback
$previousBearer = $env:DATABRICKS_BEARER
$previousSkip = $env:UG_SKIP_OS_SETTINGS
try {
  $basic = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${ClientId}:${ClientSecret}"))
  $resp = Invoke-RestMethod -UseBasicParsing -Method Post `
    -Uri "$Workspace/oidc/v1/token" -Headers @{ Authorization = "Basic $basic" } `
    -ContentType 'application/x-www-form-urlencoded' `
    -Body @{ grant_type = 'client_credentials'; scope = 'ai-gateway:read unity-catalog:read' }
  if (-not $resp.access_token) { throw 'Token request returned no access_token.' }
  $env:DATABRICKS_BEARER = $resp.access_token
  $env:UG_SKIP_OS_SETTINGS = $null
  $null | & (Join-Path $BinDir 'ug.exe') configure --global --workspace $Workspace --agents claude,codex
  if ($LASTEXITCODE -ne 0) { throw "ug configure failed: $LASTEXITCODE" }
} finally {
  $env:DATABRICKS_BEARER = $previousBearer
  $env:UG_SKIP_OS_SETTINGS = $previousSkip
  $ClientSecret = $null
  $basic = $null
  $resp = $null
}
foreach ($name in 'claude', 'codex') {
  if (-not (Test-Path -LiteralPath (Join-Path $NpmDir "$name.cmd"))) {
    throw "$name was not installed in $NpmDir. Check ug configure's output."
  }
}

# Add PowerShell aliases for all users
if ((Get-ExecutionPolicy -Scope LocalMachine) -in 'Undefined', 'Restricted') {
  try { Set-ExecutionPolicy -Scope LocalMachine RemoteSigned -Force }
  catch { if ($_.FullyQualifiedErrorId -notlike 'ExecutionPolicyOverride*') { throw } }
}
foreach ($scope in 'MachinePolicy', 'UserPolicy', 'LocalMachine') {
  if ((Get-ExecutionPolicy -Scope $scope) -in 'AllSigned', 'Restricted') {
    Write-Warning "$scope execution policy may prevent the PowerShell aliases from loading."
  }
}
$block = Get-AgentAliases (Join-Path $BinDir 'ug.exe')
foreach ($path in $profiles) {
  $text = if (Test-Path -LiteralPath $path) { Get-Content -Raw -LiteralPath $path } else { '' }
  Set-Content -LiteralPath $path -Encoding UTF8 -Value (Update-AgentProfile $text $block)
}

# Record a completed deployment and keep the script for Intune uninstall
$scriptHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $PSCommandPath).Hash
$installedScript = Join-Path $Prefix 'windows-provision.ps1'
if ([IO.Path]::GetFullPath($PSCommandPath) -ne [IO.Path]::GetFullPath($installedScript)) {
  Copy-Item -LiteralPath $PSCommandPath -Destination $installedScript -Force
}
@{
  workspace = $Workspace
  ugRef = $UgRef
  scriptSha256 = $scriptHash
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Prefix 'provisioned.json') -Encoding UTF8
Write-Output 'Unity Gateway provisioning completed.'
