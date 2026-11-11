param([Parameter(Mandatory)][string]$OutputDirectory)

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Use a disposable GitHub runner' }
. "$PSScriptRoot/diagnostics.ps1"
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$taskPath = Join-Path $env:RUNNER_TEMP 'native-contract/task.json'
$task = Get-Content -LiteralPath $taskPath -Raw | ConvertFrom-Json
$python = Join-Path (Split-Path $env:UG_COWORK_TEST_BINARY -Parent) 'python.exe'
$claude = (Get-Command claude.cmd -ErrorAction Stop).Source
$report = [ordered]@{ passed = $false; executable = $claude }
$process = $null
$raw = Join-Path $env:RUNNER_TEMP 'native-contract'
try {
    $report.version = (& $claude --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $report.version -notmatch '^2\.1\.290\b') { throw 'Unexpected CLI version' }
    $configuration = Get-Content (Join-Path (Split-Path $OutputDirectory -Parent) 'configuration.json') -Raw | ConvertFrom-Json
    $arguments = '-NoProfile -File "' + "$PSScriptRoot/invoke-native-cli.ps1" + '" -TaskPath "' + $taskPath + '" -Model "' + $configuration.targetModel + '" -Executable "' + $claude + '"'
    $process = Start-Process -FilePath "$PSHOME/pwsh.exe" -ArgumentList $arguments -WorkingDirectory $task.folder -PassThru `
        -RedirectStandardOutput (Join-Path $raw 'cli-stdout.jsonl') -RedirectStandardError (Join-Path $raw 'cli-stderr.log')
    if (-not $process.WaitForExit(300000)) { throw 'Native CLI task timed out after 300 seconds' }
    $report.exitCode = $process.ExitCode
    if ($process.ExitCode -ne 0) { throw 'Native CLI task failed' }
    $rows = @(Get-Content (Join-Path $raw 'cli-stdout.jsonl') | ForEach-Object { $_ | ConvertFrom-Json })
    $init = @($rows | Where-Object { $_.type -eq 'system' -and $_.subtype -eq 'init' })
    if ($init.Count -ne 1) { throw 'Expected one native session inventory' }
    $report.skills = $init[0].skills
    $report.slashCommands = $init[0].slash_commands
    $inventory = if ($init[0].PSObject.Properties.Name -contains 'skills') { @($init[0].skills) } else { @($init[0].slash_commands) }
    if (@($inventory | Where-Object { $_ -eq $task.skill }).Count -ne 1) { throw 'Enterprise skill is not listed exactly once in native inventory' }
    $result = @($rows | Where-Object { $_.type -eq 'result' })
    if ($result.Count -ne 1 -or $result[0].is_error) { throw 'Native CLI did not complete successfully' }
    $report.session = (& $python "$PSScriptRoot/session-evidence.py" --task $taskPath `
        --sessions-root (Join-Path $env:USERPROFILE '.claude/projects') | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0 -or $report.session.status -ne 'passed') { throw 'Native skill invocation/read evidence did not pass' }
    $report.passed = $true
} catch {
    $report.error = $_.Exception.Message
} finally {
    if ($process -and -not $process.HasExited) {
        & "$env:SystemRoot/System32/taskkill.exe" /PID $process.Id /T /F | Out-Null
        [void]$process.WaitForExit(10000)
    }
    foreach ($name in @('cli-stdout.jsonl', 'cli-stderr.log')) {
        $path = Join-Path $raw $name
        if (Test-Path -LiteralPath $path) {
            Protect-DiagnosticText (Get-Content -LiteralPath $path -Raw) | Set-Content (Join-Path $OutputDirectory $name)
        }
    }
    Protect-DiagnosticText ($report | ConvertTo-Json -Depth 10) | Set-Content (Join-Path $OutputDirectory 'result.json')
}
if (-not $report.passed) { throw 'Native CLI contract failed; see result.json' }
