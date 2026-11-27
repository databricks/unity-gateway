$ErrorActionPreference = 'Stop'
if ($env:CLAUDE_DESKTOP_WINDOWS_SHA256 -notmatch '^[a-fA-F0-9]{64}$') {
    throw 'An installer SHA256 is required'
}
$installer = Join-Path $env:RUNNER_TEMP 'Claude.msix'
if (Test-Path -LiteralPath $installer) {
    # A bad cache entry must not strand the lane: drop it and fetch the pinned build again.
    if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ne $env:CLAUDE_DESKTOP_WINDOWS_SHA256) {
        Remove-Item -LiteralPath $installer
    }
}
if (-not (Test-Path -LiteralPath $installer)) {
  Invoke-WebRequest -Uri $env:CLAUDE_DESKTOP_WINDOWS_URL -OutFile $installer -TimeoutSec 300
}
& "$PSScriptRoot/install-desktop.ps1" -InstallerPath $installer `
    -ExpectedSha256 $env:CLAUDE_DESKTOP_WINDOWS_SHA256 `
    -OutputDirectory "$env:RUNNER_TEMP/cowork-inference"
