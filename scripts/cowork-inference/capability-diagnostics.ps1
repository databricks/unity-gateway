$ErrorActionPreference = 'Continue'
. "$PSScriptRoot/diagnostics.ps1"
$evidence = Join-Path $env:RUNNER_TEMP 'cowork-capability'
New-Item -ItemType Directory -Path $evidence -Force | Out-Null
$hostInfo = @{
    processor = @(Get-CimInstance Win32_Processor | Select-Object Name, VirtualizationFirmwareEnabled, SecondLevelAddressTranslationExtensions, VMMonitorModeExtensions)
    computer = Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer, Model, HypervisorPresent
}
if ($env:CAPABILITY -eq 'mcp') {
    $python = Join-Path $env:RUNNER_TEMP 'desktop-ug-runtime/Scripts/python.exe'
    $text = (& $python "$PSScriptRoot/diagnose-mcp.py" 2>&1 | Out-String)
    Protect-DiagnosticText $text | Set-Content (Join-Path $evidence 'mcp-outside-desktop.txt')
    $index = 0
    foreach ($file in @(Get-ChildItem (Join-Path $env:USERPROFILE '.ucode/mcp-proxy') -Recurse -Filter 'backend.log' -ErrorAction SilentlyContinue)) {
        $text = Get-Content $file.FullName -Tail 150 | Out-String
        Protect-DiagnosticText $text | Set-Content (Join-Path $evidence "mcp-backend-$index.txt")
        $index++
    }
}
$hostInfo | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $evidence 'host.json')
$root = Join-Path $env:LOCALAPPDATA 'Claude-3p'
Get-ChildItem $root -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match 'local-agent-mode|log|cowork' } |
    Select-Object -First 300 @{Name='path'; Expression={$_.FullName.Substring($root.Length)}}, Length |
    ConvertTo-Json | Set-Content (Join-Path $evidence 'runtime-files.json')
$index = 0
foreach ($logs in @((Join-Path $root 'logs'), (Join-Path $env:APPDATA 'Claude/logs'))) {
    foreach ($file in @(Get-ChildItem $logs -File -Filter '*.log' -ErrorAction SilentlyContinue | Select-Object -First 15)) {
        $text = Get-Content $file.FullName -Tail 300 -ErrorAction SilentlyContinue | Out-String
        Protect-DiagnosticText $text | Set-Content (Join-Path $evidence "runtime-$index.log")
        $index++
    }
}
