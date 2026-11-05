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
    & $npm ci --prefix $PSScriptRoot --ignore-scripts --no-audit --fund=false `
        --userconfig $config --fetch-retries=0 --fetch-timeout=30000
    if ($LASTEXITCODE -ne 0) { throw 'Playwright installation through JFrog failed' }
} finally {
    Remove-Item -LiteralPath $config -Force -ErrorAction SilentlyContinue
    Remove-Item Env:UG_PROBE_NPM_TOKEN -ErrorAction SilentlyContinue
}
