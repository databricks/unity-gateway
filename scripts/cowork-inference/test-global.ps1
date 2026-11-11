param([Parameter(Mandatory)][string]$OutputDirectory)

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Use a disposable GitHub runner' }
if (-not $env:UCODE_TEST_WORKSPACE -or -not $env:DATABRICKS_BEARER) { throw 'Existing CI credentials are required' }
$env:PATH = $env:UG_GLOBAL_TEST_PATH
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
. "$PSScriptRoot/diagnostics.ps1"
$programFiles = if ($env:ProgramW6432) { $env:ProgramW6432 } else { $env:ProgramFiles }
$runtime = Join-Path $programFiles 'UnityGateway/claude-desktop-mdm'
$plugin = Join-Path $programFiles 'Claude/org-plugins/ug'
$nativeSettings = Join-Path $programFiles 'ClaudeCode/managed-settings.json'
$keyPath = 'SOFTWARE\Policies\Claude'
$hive = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryView]::Registry64)
$profileRoot = Join-Path $env:LOCALAPPDATA 'Claude-3p/configLibrary'
$report = [ordered]@{ passed = $false; liveConsumerOAuth = $false; liveMcpToolCalls = $false }

function Invoke-Configure {
    param([string]$Fixture, [switch]$ExpectFailure)
    $env:UCODE_MANAGED_CONFIG_STUB = (Resolve-Path "tests/fixtures/managed_config/$Fixture").Path
    $output = & $env:UG_COWORK_TEST_BINARY configure --global --profile desktop-ci --agents claude --disable-databricks-ai-tools --skip-upgrade 2>&1 | Out-String
    $code = $LASTEXITCODE
    Protect-DiagnosticText $output | Add-Content (Join-Path $OutputDirectory 'configure.log')
    if ($ExpectFailure) {
        if ($code -eq 0) { throw 'Expected global configure to fail' }
    } elseif ($code -ne 0) { throw "Global configure failed with exit $code" }
}

function Read-Policy {
    $key = $hive.OpenSubKey($keyPath)
    $values = @{}
    if ($key) {
        try {
            foreach ($name in $key.GetValueNames()) {
                $values[$name] = @{ value = $key.GetValue($name); kind = $key.GetValueKind($name).ToString() }
            }
        } finally { $key.Dispose() }
    }
    return $values
}

function Assert-Preserved {
    $policy = Read-Policy
    if ($policy.organizationAnnouncement.value -ne 'Administrator input: preserve me' -or $policy.disableAutoUpdates.kind -ne 'DWord') {
        throw 'Unrelated registry values or native types changed'
    }
    if ((Get-Content -LiteralPath (Join-Path $profileRoot 'user-input.json') -Raw).Trim() -ne '{"toolSearchEnabled":false}') {
        throw 'Pre-existing user-authored profile changed'
    }
    $child = $hive.OpenSubKey("$keyPath\AdministratorInput")
    if (-not $child) { throw 'Unrelated registry subkey was removed' }
    try { if ($child.GetValue('keep') -ne 'yes') { throw 'Unrelated subkey changed' } } finally { $child.Dispose() }
}

try {
    $existing = $hive.OpenSubKey($keyPath)
    if ($existing) { $existing.Dispose(); throw 'Existing Desktop machine policy' }
    foreach ($path in @($runtime, $plugin, $nativeSettings)) {
        if (Test-Path -LiteralPath $path) { throw "Existing machine configuration: $path" }
    }
    @('[desktop-ci]', "host = $env:UCODE_TEST_WORKSPACE", 'auth_type = databricks-cli') | Set-Content (Join-Path $env:USERPROFILE '.databrickscfg')
    New-Item -ItemType Directory -Path $profileRoot -Force | Out-Null
    '{"toolSearchEnabled":false}' | Set-Content (Join-Path $profileRoot 'user-input.json')
    $key = $hive.CreateSubKey($keyPath)
    try {
        $key.SetValue('organizationAnnouncement', 'Administrator input: preserve me', [Microsoft.Win32.RegistryValueKind]::String)
        $key.SetValue('disableAutoUpdates', 1, [Microsoft.Win32.RegistryValueKind]::DWord)
        $child = $key.CreateSubKey('AdministratorInput')
        try { $child.SetValue('keep', 'yes') } finally { $child.Dispose() }
    } finally { $key.Dispose() }

    Invoke-Configure 'windows_desktop_global.json'
    $policy = Read-Policy
    if ($policy.inferenceProvider.value -ne 'gateway') { throw 'No global gateway policy' }
    foreach ($name in @('inferenceModels', 'managedMcpServers', 'inferenceCredentialHelperArgs', 'otlpTracesEnabled')) {
        if ($policy[$name].kind -ne 'String') { throw "Wrong native registry type for $name" }
    }
    $servers = @($policy.managedMcpServers.value | ConvertFrom-Json)
    if ($servers.Count -ne 1 -or $servers[0].name -ne 'system-ai-github') { throw 'Wrong Desktop connectors' }
    $native = Get-Content -LiteralPath $nativeSettings -Raw | ConvertFrom-Json
    if (-not $native.managedMcpServers.'system-ai-github') { throw 'CLI connector is absent' }
    if ($native.parentSettingsBehavior -ne 'merge') { throw 'Desktop parent policy is not retained' }
    if ($native.apiKeyHelper -match 'desktop-ci') { throw 'CLI helper depends on provisioning profile' }
    if (Test-Path (Join-Path $programFiles 'ClaudeCode/.claude/skills')) { throw 'Deferred enterprise skills were installed' }
    if (Test-Path (Join-Path $plugin 'skills/forkable-meals')) { throw 'Deferred workspace plugin skill was installed' }
    if (Test-Path (Join-Path $plugin '.mcp.json')) { throw 'Plugin duplicates connector declarations' }
    $manifest = Get-Content (Join-Path $plugin '.claude-plugin/plugin.json') -Raw | ConvertFrom-Json
    $version = (Get-Content (Join-Path $plugin 'version.json') -Raw | ConvertFrom-Json).version
    if ($manifest.installationPreference -ne 'required' -or $manifest.version -ne $version) { throw 'Invalid organization-plugin manifest/version' }
    $hooks = Get-Content (Join-Path $plugin 'hooks/hooks.json') -Raw | ConvertFrom-Json
    foreach ($event in @('SessionStart', 'UserPromptSubmit')) {
        if ($hooks.hooks.$event[0].hooks[0].command -notmatch 'runtime.py') { throw "Missing shared $event hook" }
    }
    Assert-Preserved
    $report.initialPublication = $true
    Invoke-Configure 'windows_desktop_global.json'
    if ((Get-Content (Join-Path $plugin 'version.json') -Raw | ConvertFrom-Json).version -ne $version) { throw 'Identical configure changed plugin version' }
    $report.repeatConfigure = $true

    $password = ConvertTo-SecureString ('Ug-' + [Guid]::NewGuid().ToString('N') + '!9a') -AsPlainText -Force
    New-LocalUser -Name 'ug-native-consumer' -Password $password -AccountNeverExpires | Out-Null
    Add-LocalGroupMember -SID 'S-1-5-32-545' -Member 'ug-native-consumer'
    $credential = [PSCredential]::new("$env:COMPUTERNAME\ug-native-consumer", $password)
    $python = Join-Path (Split-Path $env:UG_COWORK_TEST_BINARY -Parent) 'python.exe'
    $helperArgs = @($policy.inferenceCredentialHelperArgs.value | ConvertFrom-Json)
    $consumerProfile = $helperArgs[$helperArgs.IndexOf('--profile') + 1]
    $consumerScript = (Resolve-Path "$PSScriptRoot/test-global-consumer.py").Path
    $arguments = @('-I', $consumerScript, (Join-Path $runtime 'runtime.py'), $env:UCODE_TEST_WORKSPACE, $consumerProfile) | ForEach-Object { '"' + $_ + '"' }
    $process = Start-Process -FilePath $python -ArgumentList ($arguments -join ' ') -Credential $credential -LoadUserProfile -PassThru `
        -RedirectStandardOutput (Join-Path $OutputDirectory 'consumer.json') -RedirectStandardError (Join-Path $env:RUNNER_TEMP 'consumer-error.txt')
    if (-not $process.WaitForExit(120000)) { $process.Kill(); throw 'Consumer helper timed out' }
    $process.Refresh()
    if ($process.ExitCode -ne 0) {
        Protect-DiagnosticText (Get-Content (Join-Path $env:RUNNER_TEMP 'consumer-error.txt') -Raw) | Set-Content (Join-Path $OutputDirectory 'consumer-error.txt')
        throw 'Consumer isolation test failed'
    }
    $report.standardUserIsolation = $true

    Invoke-Configure 'windows_desktop_global_empty.json'
    $policy = Read-Policy
    if (@($policy.managedMcpServers.value | ConvertFrom-Json).Count -ne 0) { throw 'Withdrawn Desktop connector remains' }
    if ($policy.ContainsKey('otlpEndpoint') -or (Test-Path (Join-Path $runtime 'otel-headers.cmd'))) { throw 'Withdrawn tracing remains' }
    if (Test-Path (Join-Path $plugin 'hooks/hooks.json')) { throw 'Withdrawn connector hooks remain' }
    $native = Get-Content -LiteralPath $nativeSettings -Raw | ConvertFrom-Json
    if ($native.managedMcpServers.'system-ai-github') { throw 'Withdrawn CLI connector remains' }
    Assert-Preserved
    $report.removalRefresh = $true

    $locked = [IO.File]::Open((Join-Path $runtime 'runtime.py'), [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
    try { Invoke-Configure 'windows_desktop_global_empty.json' -ExpectFailure } finally { $locked.Dispose() }
    if ((Read-Policy).inferenceProvider.value -ne 'gateway') { throw 'Publication failure removed prior policy' }
    Invoke-Configure 'windows_desktop_global_empty.json'
    $report.failureRetry = $true

    $output = & $env:UG_COWORK_TEST_BINARY revert 2>&1 | Out-String
    Protect-DiagnosticText $output | Set-Content (Join-Path $OutputDirectory 'revert.log')
    if ($LASTEXITCODE -ne 0) { throw 'UG revert failed' }
    Assert-Preserved
    foreach ($path in @($runtime, $plugin)) { if (Test-Path -LiteralPath $path) { throw "Owned deployment remains: $path" } }
    if ((Read-Policy).ContainsKey('inferenceProvider')) { throw 'Owned registry policy remains after revert' }
    $report.revert = $true
    $report.passed = $true
} catch {
    $report.error = $_.Exception.Message
} finally {
    if (Get-LocalUser -Name 'ug-native-consumer' -ErrorAction SilentlyContinue) { Remove-LocalUser -Name 'ug-native-consumer' }
    $hive.Dispose()
    Protect-DiagnosticText ($report | ConvertTo-Json -Depth 5) | Set-Content (Join-Path $OutputDirectory 'result.json')
}
if (-not $report.passed) { throw 'Windows global deployment test failed; see result.json' }
