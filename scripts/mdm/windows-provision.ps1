# Intune provisioning script: installs ug machine-wide and runs
# `ug configure --global`. Run as SYSTEM in 64-bit PowerShell after setting
# the three values below.

$Workspace = 'https://<workspace>.cloud.databricks.com'
$ClientId = '<service principal client id>'
$ClientSecret = '<service principal client secret>'

# Check the settings and set install locations
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$env:PYTHONUTF8 = '1'

if (-not [Environment]::Is64BitProcess) {
  throw 'Run this script in 64-bit PowerShell.'
}
if ("$Workspace$ClientId$ClientSecret" -match '<') {
  throw 'Set $Workspace, $ClientId and $ClientSecret at the top of the script.'
}
$Workspace = $Workspace.TrimEnd('/')
$Arm = $env:PROCESSOR_ARCHITECTURE -eq 'ARM64'
$Prefix = Join-Path $env:ProgramFiles 'UnityGateway'
$BinDir = Join-Path $Prefix 'bin'
$CliReleases = 'https://github.com/databricks/cli/releases/download'
$CliVersionUrl = 'https://raw.githubusercontent.com/databricks/setup-cli/main/VERSION'
$GitRelease = 'https://api.github.com/repos/git-for-windows/git/releases/latest'
$UgRepo = 'https://github.com/databricks/unity-gateway'
# ug branch, tag or commit; empty for the default branch
$UgRef = ''

$env:UV_TOOL_DIR = Join-Path $Prefix 'tools'
$env:UV_TOOL_BIN_DIR = $BinDir
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Prefix 'python'
$env:UV_PYTHON_PREFERENCE = 'only-managed'

# Download, checksum and PATH helpers
function Get-File($Url, $OutFile) {
  Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $OutFile
}

function Get-Text($Url) {
  $file = Join-Path $Tmp ([guid]::NewGuid())
  Get-File $Url $file
  (Get-Content -Raw -LiteralPath $file).Trim()
}

function Assert-Sha256($Path, $Expected) {
  $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash
  if ($actual -ne $Expected) {
    throw "Checksum mismatch for $Path"
  }
}

function Get-Sha256Sum($SumsFile, $Name) {
  foreach ($line in Get-Content -LiteralPath $SumsFile) {
    $hash, $file = $line -split '\s+', 2
    if ($file.TrimStart('*') -eq $Name) { return $hash }
  }
  throw "No checksum for $Name"
}

function Add-MachinePath($Dir) {
  $path = [Environment]::GetEnvironmentVariable('Path', 'Machine')
  if (($path -split ';') -notcontains $Dir) {
    [Environment]::SetEnvironmentVariable('Path', "$path;$Dir", 'Machine')
  }
  if (($env:Path -split ';') -notcontains $Dir) {
    $env:Path = "$Dir;$env:Path"
  }
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
Add-MachinePath $BinDir
$Tmp = Join-Path $Prefix ".tmp-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $Tmp | Out-Null

try {
  # Install Git, needed to install ug from GitHub
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    $suffix = if ($Arm) { '-arm64.exe' } else { '-64-bit.exe' }
    $release = Invoke-RestMethod -UseBasicParsing -Uri $GitRelease
    $asset = $release.assets |
      Where-Object { $_.name -like 'Git-*' -and $_.name.EndsWith($suffix) } |
      Select-Object -First 1
    if (-not $asset) { throw "No Git for Windows installer ending in $suffix" }
    $setup = Join-Path $Tmp $asset.name
    Get-File $asset.browser_download_url $setup
    Assert-Sha256 $setup ($asset.digest -replace '^sha256:', '')
    $proc = Start-Process -FilePath $setup -Wait -PassThru `
      -ArgumentList '/VERYSILENT', '/NORESTART', '/SUPPRESSMSGBOXES', '/SP-'
    if ($proc.ExitCode -ne 0) {
      throw "Git setup failed with exit code $($proc.ExitCode)"
    }
    Add-MachinePath (Join-Path $env:ProgramFiles 'Git\cmd')
  }

  # Install uv and ug
  $uv = Join-Path $BinDir 'uv.exe'
  if (-not (Test-Path -LiteralPath $uv)) {
    $env:UV_INSTALL_DIR = $BinDir
    $env:UV_NO_MODIFY_PATH = '1'
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command `
      'irm https://astral.sh/uv/install.ps1 | iex'
    if ($LASTEXITCODE -ne 0) { throw "uv install failed: $LASTEXITCODE" }
  }
  $ugSource = if ($UgRef) { "git+$UgRepo@$UgRef" } else { "git+$UgRepo" }
  & $uv tool install --force --python cpython-3.13-windows-x86_64-none $ugSource
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
} finally {
  Remove-Item -Recurse -Force -LiteralPath $Tmp -ErrorAction SilentlyContinue
}

# Get a service principal token and run ug configure, which installs and
# configures Claude Code and Codex
$basic = [Convert]::ToBase64String(
  [Text.Encoding]::UTF8.GetBytes("${ClientId}:${ClientSecret}"))
try {
  $resp = Invoke-RestMethod -UseBasicParsing -Method Post `
    -Uri "$Workspace/oidc/v1/token" `
    -Headers @{ Authorization = "Basic $basic" } `
    -Body @{ grant_type = 'client_credentials'; scope = 'ai-gateway:read unity-catalog:read' }
} catch {
  throw "Token request to $Workspace failed: $($_.ErrorDetails.Message) $($_.Exception.Message)"
}
$env:DATABRICKS_BEARER = $resp.access_token

$null | & (Join-Path $BinDir 'ug.exe') configure --global --workspace $Workspace `
  --agents claude,codex
if ($LASTEXITCODE -ne 0) { throw "ug configure failed: $LASTEXITCODE" }

# Add `claude` and `codex` aliases for all users
if ((Get-ExecutionPolicy -Scope LocalMachine) -in 'Undefined', 'Restricted') {
  try {
    Set-ExecutionPolicy -Scope LocalMachine RemoteSigned -Force
  } catch {
    if ($_.FullyQualifiedErrorId -notlike 'ExecutionPolicyOverride*') { throw }
  }
}
$ug = Join-Path $BinDir 'ug.exe'
$block = @"
# BEGIN unity-gateway
function claude { `$env:UG_SKIP_OS_SETTINGS = '1'; & '$ug' claude @args }
function codex { `$env:UG_SKIP_OS_SETTINGS = '1'; & '$ug' codex @args }
# END unity-gateway
"@
$profiles = @("$env:SystemRoot\System32\WindowsPowerShell\v1.0\profile.ps1")
$pwsh7 = Join-Path $env:ProgramFiles 'PowerShell\7'
if (Test-Path -LiteralPath $pwsh7) { $profiles += Join-Path $pwsh7 'profile.ps1' }
foreach ($path in $profiles) {
  $text = if (Test-Path -LiteralPath $path) { Get-Content -Raw -LiteralPath $path } else { '' }
  $text = $text -replace '(?s)# BEGIN unity-gateway.*?# END unity-gateway\r?\n?', ''
  Set-Content -LiteralPath $path -Encoding UTF8 -Value ("$text".TrimEnd() + "`r`n" + $block).TrimStart()
}
