$ErrorActionPreference = 'Stop'
if (-not $env:UCODE_TEST_WORKSPACE -or -not $env:DATABRICKS_BEARER) {
    throw 'Existing CI workspace and bearer are required'
}
$config = Join-Path $env:USERPROFILE '.databrickscfg'
@('[desktop-ci]', "host = $env:UCODE_TEST_WORKSPACE", 'auth_type = databricks-cli') |
    Set-Content -LiteralPath $config
$root = Join-Path $env:LOCALAPPDATA 'Claude-3p'
$rootExisted = Test-Path $root
if (-not $env:CLAUDE_DESKTOP_EXECUTABLE -or
    -not (Test-Path -LiteralPath $env:CLAUDE_DESKTOP_EXECUTABLE -PathType Leaf)) {
    throw 'Verified Claude Desktop installation is required before preparing application data'
}
if (@(Get-Process -Name Claude -ErrorAction SilentlyContinue).Count) {
    throw 'Desktop must not be running before the first configured launch'
}
$report = [ordered]@{
    claude3pRootExistedAfterInstall = $rootExisted
    profile = 'desktop-ci'
}
$report | ConvertTo-Json |
    Set-Content "$env:RUNNER_TEMP/cowork-inference/configuration.json"
& $env:UG_COWORK_TEST_BINARY configure --profile desktop-ci `
    --agents claude --user --disable-databricks-ai-tools --skip-validate --skip-upgrade
if ($LASTEXITCODE -ne 0) { throw 'UG configure failed' }
$profilePath = Join-Path $env:LOCALAPPDATA 'Claude-3p/configLibrary/5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6.json'
if (-not (Test-Path $profilePath)) { throw 'UG did not create its Desktop profile' }
$desktopProfile = Get-Content -Raw $profilePath | ConvertFrom-Json
if ($desktopProfile.inferenceProvider -ne 'gateway') { throw 'Desktop profile is not using gateway inference' }
if ($desktopProfile.inferenceCustomHeaders.'Databricks-Model-Provider-Service') {
    throw 'This test requires system.ai model services, not a model provider service'
}
$parentSchema = $desktopProfile.inferenceCustomHeaders.'Databricks-Model-Service-Parent-Schema'
if ($parentSchema -and $parentSchema -ne 'system.ai') {
    throw 'This test requires the system.ai model-service schema'
}
$targetModel = 'system.ai.claude-haiku-4-5'
$models = @($desktopProfile.inferenceModels | Where-Object { $_.name -eq $targetModel })
if ($models.Count -ne 1) { throw 'Expected system.ai Claude model is missing from the configured profile' }
$label = $models[0].labelOverride
if (-not $label) { $label = $targetModel }
$matchingLabels = @($desktopProfile.inferenceModels | Where-Object { $_.labelOverride -eq $label -or $_.name -eq $label })
if ($matchingLabels.Count -ne 1) { throw 'Configured target model label is ambiguous' }
$report.configured = $true
$report.provider = $desktopProfile.inferenceProvider
$report.targetModel = $targetModel
$report.targetModelLabel = $label
$report | ConvertTo-Json |
    Set-Content "$env:RUNNER_TEMP/cowork-inference/configuration.json"
