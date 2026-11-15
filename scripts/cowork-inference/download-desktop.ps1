$ErrorActionPreference = 'Stop'
if ($env:CLAUDE_DESKTOP_WINDOWS_SHA256 -notmatch '^[a-fA-F0-9]{64}$') {
    throw 'An installer SHA256 is required'
}
$installer = Join-Path $env:RUNNER_TEMP 'Claude.msix'
Invoke-WebRequest -Uri $env:CLAUDE_DESKTOP_WINDOWS_URL -OutFile $installer -TimeoutSec 300
& "$PSScriptRoot/install-desktop.ps1" -InstallerPath $installer `
    -ExpectedSha256 $env:CLAUDE_DESKTOP_WINDOWS_SHA256 `
    -OutputDirectory "$env:RUNNER_TEMP/cowork-inference"
