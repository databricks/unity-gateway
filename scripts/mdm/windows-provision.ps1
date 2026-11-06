# Windows MDM provisioning script, written for Microsoft Intune: install
# ug, the Databricks CLI, Claude Code and Codex machine-wide and run
# `ug configure --global` as SYSTEM, which writes the machine-wide Claude
# Code and Codex settings. Users then launch without administrator rights:
#
#   $env:UG_SKIP_OS_SETTINGS = '1'; ug claude    (or: ug codex)
#
# Intune scripts take no parameters, so set the three values below before
# uploading. Run it as SYSTEM in 64-bit PowerShell.
#
# The token is used for this run only; developers sign in with their own
# OAuth. Needs access to PyPI, GitHub, astral.sh and downloads.claude.ai.

$Workspace = 'https://<workspace>.cloud.databricks.com'
$ClientId = '<service principal client id>'
$ClientSecret = '<service principal client secret>'

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
# ug must be in a shared path: the machine-wide agent settings run it by
# absolute path for every user. Standard users can't write under Program Files.
$Prefix = Join-Path $env:ProgramFiles 'UnityGateway'
$BinDir = Join-Path $Prefix 'bin'
$CodexDir = Join-Path $Prefix 'codex'
$ClaudeReleases = 'https://downloads.claude.ai/claude-code-releases'
$CodexReleases = 'https://github.com/openai/codex/releases/latest/download'
$CliReleases = 'https://github.com/databricks/cli/releases/download'
$CliVersionUrl = 'https://raw.githubusercontent.com/databricks/setup-cli/main/VERSION'
$GitRelease = 'https://api.github.com/repos/git-for-windows/git/releases/latest'
$UgRepo = 'https://github.com/databricks/unity-gateway'
# Branch, tag or commit of ug to install; empty for the default branch
$UgRef = ''

$env:UV_TOOL_DIR = Join-Path $Prefix 'tools'
$env:UV_TOOL_BIN_DIR = $BinDir
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Prefix 'python'
$env:UV_PYTHON_PREFERENCE = 'only-managed'

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
# Download into a private directory under Program Files, not the shared temp
$Tmp = Join-Path $Prefix ".tmp-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $Tmp | Out-Null

try {
  # Get ug from git
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

  $uv = Join-Path $BinDir 'uv.exe'
  if (-not (Test-Path -LiteralPath $uv)) {
    $env:UV_INSTALL_DIR = $BinDir
    $env:UV_NO_MODIFY_PATH = '1'
    # A child process, so an `exit` in the installer can't end this script
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command `
      'irm https://astral.sh/uv/install.ps1 | iex'
    if ($LASTEXITCODE -ne 0) { throw "uv install failed: $LASTEXITCODE" }
  }
  $ugSource = if ($UgRef) { "git+$UgRepo@$UgRef" } else { "git+$UgRepo" }
  # cryptography has no Windows ARM64 wheels; x64 Python also runs on ARM64
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

  # Install Claude Code
  if (-not (Test-Path -LiteralPath (Join-Path $BinDir 'claude.exe'))) {
    $platform = if ($Arm) { 'win32-arm64' } else { 'win32-x64' }
    $version = Get-Text "$ClaudeReleases/latest"
    $base = "$ClaudeReleases/$version"
    $manifest = Get-Text "$base/manifest.json" | ConvertFrom-Json
    $claude = Join-Path $Tmp 'claude.exe'
    Get-File "$base/$platform/claude.exe" $claude
    Assert-Sha256 $claude $manifest.platforms.$platform.checksum
    Copy-Item $claude $BinDir
  }

  # Install Codex, keeping its package layout (bin, resources, path tools)
  if (-not (Test-Path -LiteralPath (Join-Path $CodexDir 'bin\codex.exe'))) {
    $target = if ($Arm) { 'aarch64-pc-windows-msvc' } else { 'x86_64-pc-windows-msvc' }
    $package = "codex-package-$target.tar.gz"
    Get-File "$CodexReleases/$package" (Join-Path $Tmp $package)
    Get-File "$CodexReleases/codex-package_SHA256SUMS" (Join-Path $Tmp 'codex-sums')
    Assert-Sha256 (Join-Path $Tmp $package) (Get-Sha256Sum (Join-Path $Tmp 'codex-sums') $package)
    New-Item -ItemType Directory -Force -Path $CodexDir | Out-Null
    & "$env:SystemRoot\System32\tar.exe" -xzf (Join-Path $Tmp $package) -C $CodexDir
    if ($LASTEXITCODE -ne 0) { throw "Extracting Codex failed: $LASTEXITCODE" }
  }
  Add-MachinePath (Join-Path $CodexDir 'bin')
} finally {
  Remove-Item -Recurse -Force -LiteralPath $Tmp -ErrorAction SilentlyContinue
}

# Get short lived credential; the client id and secret stay in this process
# instead of on a command line.
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
