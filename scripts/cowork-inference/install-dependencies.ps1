param([switch]$SharedRuntime)

$ErrorActionPreference = 'Stop'
$npm = (Get-Command npm.cmd -ErrorAction Stop).Source
$py = (Get-Command python.exe -ErrorAction Stop).Source
$runtime = Join-Path $env:RUNNER_TEMP 'desktop-ug-runtime'
$npmArgs = @()
if ($SharedRuntime) {
    if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Shared test runtimes require a disposable GitHub runner' }
    $programFiles = if ($env:ProgramW6432) { $env:ProgramW6432 } else { $env:ProgramFiles }
    if (-not $programFiles) { throw 'Native Program Files directory is unavailable' }
    $shared = Join-Path $programFiles 'UnityGatewayNativeTest'
    if (Test-Path -LiteralPath $shared) { throw 'Existing shared test runtime must not be overwritten' }
    New-Item -ItemType Directory -Path $shared | Out-Null
    $basePython = (& $py -c 'import sys; print(sys.base_prefix)').Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not locate installed Python' }
    Copy-Item -LiteralPath $basePython -Destination (Join-Path $shared 'python') -Recurse
    $py = Join-Path $shared 'python/python.exe'
    $runtime = Join-Path $shared 'ug'
    $cliRoot = Join-Path $shared 'cli'
    New-Item -ItemType Directory -Path $cliRoot | Out-Null
    Copy-Item -LiteralPath (Get-Command node.exe -ErrorAction Stop).Source -Destination (Join-Path $cliRoot 'node.exe')
    Copy-Item -LiteralPath (Join-Path $env:RUNNER_TEMP '.bin/databricks.exe') -Destination (Join-Path $cliRoot 'databricks.exe')
    $npmArgs = @('--prefix', $cliRoot)
}
& $npm install -g '@anthropic-ai/claude-code@2.1.290' --no-audit --fund=false `
    --registry https://registry.npmjs.org --fetch-retries=0 --fetch-timeout=30000 @npmArgs
if ($LASTEXITCODE -ne 0) { throw 'Claude CLI installation failed' }
$claude = if ($SharedRuntime) { Join-Path $cliRoot 'claude.cmd' } else { (Get-Command claude.cmd -ErrorAction Stop).Source }
& $claude --version
if ($LASTEXITCODE -ne 0) { throw 'Installed Claude CLI did not start' }
& $py -m venv $runtime
if ($LASTEXITCODE -ne 0) { throw 'Could not create UG runtime' }
$python = Join-Path $runtime 'Scripts/python.exe'
$env:PIP_EXTRA_INDEX_URL = ''
& $python -m pip install --disable-pip-version-check --index-url https://pypi.org/simple .
if ($LASTEXITCODE -ne 0) { throw 'UG installation failed' }
if ($SharedRuntime) {
    $icacls = Join-Path $env:SystemRoot 'System32/icacls.exe'
    & $icacls $shared /setowner '*S-1-5-32-544' /T /L | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not set shared runtime owner' }
    & $icacls $shared /reset /T /L | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not reset shared runtime permissions' }
    & $icacls $shared /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' /L | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not protect shared runtime' }
    $protectedPath = @((Join-Path $runtime 'Scripts'), $cliRoot, (Join-Path $env:SystemRoot 'System32'), (Join-Path $programFiles 'Git/cmd')) -join ';'
    "UG_GLOBAL_TEST_PATH=$protectedPath" | Add-Content $env:GITHUB_ENV
    $cliRoot | Add-Content $env:GITHUB_PATH
}
"UG_COWORK_TEST_BINARY=$(Join-Path $runtime 'Scripts/ug.exe')" | Add-Content $env:GITHUB_ENV
