$ErrorActionPreference = 'Stop'
if (-not $env:UCODE_TEST_WORKSPACE -or -not $env:DATABRICKS_BEARER) {
    throw 'Existing CI workspace and bearer are required'
}
$config = Join-Path $env:USERPROFILE '.databrickscfg'
@('[desktop-ci]', "host = $env:UCODE_TEST_WORKSPACE", 'auth_type = databricks-cli') |
    Set-Content -LiteralPath $config
$root = Join-Path $env:LOCALAPPDATA 'Claude-3p'
@{ claude3pRootExists = (Test-Path $root); profile = 'desktop-ci' } | ConvertTo-Json |
    Set-Content "$env:RUNNER_TEMP/claude-desktop-probe/configuration.json"
& $env:UG_DESKTOP_PROBE_BINARY configure --profile desktop-ci `
    --agents claude --user --disable-databricks-ai-tools --skip-validate --skip-upgrade
if ($LASTEXITCODE -ne 0) { throw 'UG configure failed' }
$profilePath = Join-Path $env:LOCALAPPDATA 'Claude-3p/configLibrary/5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6.json'
if (-not (Test-Path $profilePath)) { throw 'UG did not create its Desktop profile' }
$profile = Get-Content -Raw $profilePath | ConvertFrom-Json
if ($profile.inferenceProvider -ne 'gateway') { throw 'Desktop profile is not using gateway inference' }
@{ configured = $true; provider = $profile.inferenceProvider } | ConvertTo-Json |
    Set-Content "$env:RUNNER_TEMP/claude-desktop-probe/configuration.json"
