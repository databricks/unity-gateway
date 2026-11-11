param([switch]$SharedRuntime)

$ErrorActionPreference = 'Stop'
$npm = (Get-Command npm.cmd -ErrorAction Stop).Source
& $npm install -g '@anthropic-ai/claude-code@2.1.290' --no-audit --fund=false `
    --registry https://registry.npmjs.org --fetch-retries=0 --fetch-timeout=30000
if ($LASTEXITCODE -ne 0) { throw 'Claude CLI installation failed' }
$claude = (Get-Command claude.cmd -ErrorAction Stop).Source
& $claude --version
if ($LASTEXITCODE -ne 0) { throw 'Installed Claude CLI did not start' }
$py = (Get-Command python.exe -ErrorAction Stop).Source
$runtime = Join-Path $env:RUNNER_TEMP 'desktop-ug-runtime'
if ($SharedRuntime) {
    if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Shared test runtimes require a disposable GitHub runner' }
    $programFiles = if ($env:ProgramW6432) { $env:ProgramW6432 } else { $env:ProgramFiles }
    if (-not $programFiles) { throw 'Native Program Files directory is unavailable' }
    $runtime = Join-Path $programFiles 'UnityGatewayNativeTest/ug'
    if (Test-Path -LiteralPath $runtime) { throw 'Existing shared test runtime must not be overwritten' }
}
& $py -m venv $runtime
if ($LASTEXITCODE -ne 0) { throw 'Could not create UG runtime' }
$python = Join-Path $runtime 'Scripts/python.exe'
$env:PIP_EXTRA_INDEX_URL = ''
& $python -m pip install --disable-pip-version-check --index-url https://pypi.org/simple .
if ($LASTEXITCODE -ne 0) { throw 'UG installation failed' }
"UG_COWORK_TEST_BINARY=$(Join-Path $runtime 'Scripts/ug.exe')" | Add-Content $env:GITHUB_ENV
