param([switch]$InstallUg)

$ErrorActionPreference = 'Stop'
$registry = 'https://databricks.jfrog.io/artifactory/api/npm/db-npm/'
try {
    $githubToken = (Invoke-RestMethod -Uri $env:ACTIONS_ID_TOKEN_REQUEST_URL `
        -Headers @{ Authorization = "Bearer $env:ACTIONS_ID_TOKEN_REQUEST_TOKEN" } -TimeoutSec 30).value
    if (-not $githubToken) { throw 'Missing GitHub identity token' }
    Write-Output "::add-mask::$githubToken"
    $payload = @{
        grant_type = 'urn:ietf:params:oauth:grant-type:token-exchange'
        subject_token_type = 'urn:ietf:params:oauth:token-type:id_token'
        subject_token = $githubToken
        provider_name = 'github-actions'
    } | ConvertTo-Json
    $token = (Invoke-RestMethod -Uri 'https://databricks.jfrog.io/access/api/v1/oidc/token' `
        -Method Post -ContentType 'application/json' -Body $payload -TimeoutSec 30).access_token
    if (-not $token) { throw 'Missing JFrog access token' }
} catch {
    throw 'Could not obtain JFrog credentials through the existing github-actions OIDC provider'
}
Write-Output "::add-mask::$token"
$config = Join-Path $env:RUNNER_TEMP 'desktop-probe.npmrc'
try {
    $env:UG_PROBE_NPM_TOKEN = $token
    @("registry=$registry", '//databricks.jfrog.io/artifactory/api/npm/db-npm/:_authToken=${UG_PROBE_NPM_TOKEN}') |
        Set-Content -LiteralPath $config -Encoding utf8
    $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
    if (-not $InstallUg) {
        & $npm ci --prefix $PSScriptRoot --ignore-scripts --no-audit --fund=false `
            --userconfig $config --fetch-retries=0 --fetch-timeout=30000
        if ($LASTEXITCODE -ne 0) { throw 'Playwright installation through JFrog failed' }
        return
    }
    & $npm install -g '@anthropic-ai/claude-code@2.1.290' --ignore-scripts --no-audit --fund=false `
        --userconfig $config --fetch-retries=0 --fetch-timeout=30000
    if ($LASTEXITCODE -ne 0) { throw 'Claude CLI installation through JFrog failed' }
    $encoded = $token.Split('.')[1].Replace('-', '+').Replace('_', '/')
    $encoded = $encoded.PadRight([int]([Math]::Ceiling($encoded.Length / 4.0) * 4), '=')
    $claims = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded)) | ConvertFrom-Json
    $username = $claims.sub.Split('/')[-1]
    $env:PIP_INDEX_URL = 'https://' + [Uri]::EscapeDataString($username) + ':' +
        [Uri]::EscapeDataString($token) + '@databricks.jfrog.io/artifactory/api/pypi/db-pypi/simple'
    $env:PIP_EXTRA_INDEX_URL = ''
    $py = (Get-Command py.exe -ErrorAction Stop).Source
    $runtime = Join-Path $env:RUNNER_TEMP 'desktop-ug-runtime'
    & $py -3.12 -m venv $runtime
    if ($LASTEXITCODE -ne 0) { throw 'Could not create UG runtime' }
    $python = Join-Path $runtime 'Scripts/python.exe'
    & $python -m pip install --disable-pip-version-check .
    if ($LASTEXITCODE -ne 0) { throw 'UG installation through JFrog failed' }
    "UG_DESKTOP_PROBE_BINARY=$(Join-Path $runtime 'Scripts/ug.exe')" | Add-Content $env:GITHUB_ENV
} finally {
    Remove-Item -LiteralPath $config -Force -ErrorAction SilentlyContinue
    Remove-Item Env:UG_PROBE_NPM_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:PIP_INDEX_URL -ErrorAction SilentlyContinue
}
