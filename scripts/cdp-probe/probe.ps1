param([Parameter(Mandatory)][string]$OutputDirectory)

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Use a disposable GitHub Actions runner' }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$report = [ordered]@{ baselineWindowFound = $false; cdpEndpointFound = $false; playwrightAttached = $false }
$endpoint = 'http://127.0.0.1:9222'
$debugArgument = '--remote-debugging-port=9222'
$report.debugArguments = @($debugArgument)

function Stop-Claude {
    $processes = @(Get-Process -Name Claude -ErrorAction SilentlyContinue)
    foreach ($process in $processes) {
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction Stop
            if (-not $process.WaitForExit(10000)) { throw 'Claude did not exit during cleanup' }
        }
    }
}

try {
    $baseline = Start-Process -FilePath $env:CLAUDE_DESKTOP_EXECUTABLE -PassThru `
        -RedirectStandardOutput (Join-Path $OutputDirectory 'baseline-stdout.log') `
        -RedirectStandardError (Join-Path $OutputDirectory 'baseline-stderr.log')
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        $windows = @(Get-Process -Name Claude -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowHandle -ne 0 })
        if ($windows.Count) { $report.baselineWindowFound = $true; break }
        Start-Sleep -Milliseconds 500
    } until ([DateTime]::UtcNow -ge $deadline)
    if (-not $report.baselineWindowFound) { throw 'Normal launch did not open a window; CDP result would be inconclusive' }
    Stop-Claude
    $baseline.Dispose()

    $process = Start-Process -FilePath $env:CLAUDE_DESKTOP_EXECUTABLE -ArgumentList $debugArgument -PassThru `
        -RedirectStandardOutput (Join-Path $OutputDirectory 'cdp-stdout.log') `
        -RedirectStandardError (Join-Path $OutputDirectory 'cdp-stderr.log')
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        try {
            $version = Invoke-RestMethod -Uri "$endpoint/json/version" -TimeoutSec 2
            if ($version.webSocketDebuggerUrl) {
                $report.cdpEndpointFound = $true
                $report.browser = $version.Browser
                break
            }
        } catch {
            $report.lastEndpointError = $_.Exception.Message
        }
        if ($process.HasExited -and -not @(Get-Process -Name Claude -ErrorAction SilentlyContinue).Count) { break }
        Start-Sleep -Milliseconds 500
    } until ([DateTime]::UtcNow -ge $deadline)
    $report.cdpProcessExited = $process.HasExited
    if ($process.HasExited) { $report.cdpExitCode = $process.ExitCode }
    $report.cdpWindowFound = @((Get-Process -Name Claude -ErrorAction SilentlyContinue) |
        Where-Object { $_.MainWindowHandle -ne 0 }).Count -gt 0

    if (-not $report.cdpEndpointFound) {
        $stderr = Get-Content (Join-Path $OutputDirectory 'cdp-stderr.log') -Raw
        $report.debugSwitchRejected = $stderr -match 'refusing to start.*debugging or network-override switch'
        throw 'CDP-only launch exposed no debugging endpoint'
    }
    & node "$PSScriptRoot/attach.mjs" $endpoint $OutputDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Playwright CDP attachment failed; see attachment.json' }
    $report.playwrightAttached = $true
} catch {
    $report.error = $_.Exception.Message
} finally {
    try { Stop-Claude } catch { $report.cleanupError = $_.Exception.Message }
    $report | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $OutputDirectory 'probe.json') -Encoding utf8
}
if ($report.error -or $report.cleanupError) { exit 1 }
