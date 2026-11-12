$ErrorActionPreference = 'Stop'
# Protected runners reach packages only through internal proxies.
$npmRegistry = if ($env:UG_COWORK_NPM_REGISTRY) { $env:UG_COWORK_NPM_REGISTRY } else { 'https://registry.npmjs.org' }
$packageIndex = if ($env:UG_COWORK_PACKAGE_INDEX) { $env:UG_COWORK_PACKAGE_INDEX } else { 'https://pypi.org/simple' }
$npm = (Get-Command npm.cmd -ErrorAction Stop).Source
& $npm install -g '@anthropic-ai/claude-code@2.1.290' --no-audit --fund=false `
    --registry $npmRegistry --fetch-retries=0 --fetch-timeout=30000
if ($LASTEXITCODE -ne 0) { throw 'Claude CLI installation failed' }
$claude = (Get-Command claude.cmd -ErrorAction Stop).Source
& $claude --version
if ($LASTEXITCODE -ne 0) { throw 'Installed Claude CLI did not start' }
$py = (Get-Command python.exe -ErrorAction Stop).Source
$runtime = Join-Path $env:RUNNER_TEMP 'desktop-ug-runtime'
& $py -m venv $runtime
if ($LASTEXITCODE -ne 0) { throw 'Could not create UG runtime' }
$python = Join-Path $runtime 'Scripts/python.exe'
$env:PIP_EXTRA_INDEX_URL = ''
& $python -m pip install --disable-pip-version-check --index-url $packageIndex .
if ($LASTEXITCODE -ne 0) { throw 'UG installation failed' }
"UG_COWORK_TEST_BINARY=$(Join-Path $runtime 'Scripts/ug.exe')" | Add-Content $env:GITHUB_ENV
# UG writes its own path into Desktop's MCP connectors from PATH, as for an installed ug.
Join-Path $runtime 'Scripts' | Add-Content $env:GITHUB_PATH
