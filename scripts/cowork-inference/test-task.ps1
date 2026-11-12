param(
    [Parameter(Mandatory)][ValidateSet('file', 'skill', 'mcp')][string]$Capability,
    [Parameter(Mandatory)][string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Use a disposable GitHub runner' }
Add-Type -AssemblyName System.Windows.Forms, System.Drawing, UIAutomationClient, UIAutomationTypes
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class CapabilityWindow {
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")]
    public static extern void mouse_event(uint flags, uint x, uint y, uint data, UIntPtr extra);
}
'@
. "$PSScriptRoot/diagnostics.ps1"
. "$PSScriptRoot/prepare-task.ps1"
. "$PSScriptRoot/model-picker.ps1"
$evidence = $OutputDirectory
New-Item -ItemType Directory -Path $evidence -Force | Out-Null
$report = [ordered]@{ capability = $Capability; passed = $false; submitted = $false }
$taskPath = Join-Path $env:RUNNER_TEMP "cowork-$Capability-task.json"
$python = Join-Path $env:RUNNER_TEMP 'desktop-ug-runtime/Scripts/python.exe'
$script:window = $null
$started = Get-Date

function Find-Control {
    param([string[]]$Names, $Root = $script:window)
    foreach ($name in $Names) {
        $condition = [Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::NameProperty, $name)
        foreach ($control in $Root.FindAll([Windows.Automation.TreeScope]::Descendants, $condition)) {
            if ($control.Current.IsEnabled -and -not $control.Current.IsOffscreen) { return $control }
        }
    }
    return $null
}

function Wait-Control {
    param([string[]]$Names, [int]$Seconds = 60, $Root = $script:window)
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        $control = Find-Control -Names $Names -Root $Root
        if ($control) { return $control }
        Start-Sleep -Milliseconds 500
    } until ([DateTime]::UtcNow -ge $deadline)
    Save-UI 'wait-failed'
    throw "Control not ready: $($Names -join ', ')"
}

function Invoke-Control {
    param($Control)
    $pattern = $null
    if ($Control.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
        $pattern.Invoke()
    } elseif ($Control.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) {
        $pattern.Select()
    } elseif ($Control.Current.Name -eq 'Select Folder' -and $Control.Current.ControlType.ProgrammaticName -eq 'ControlType.Pane') {
        $point = $Control.GetClickablePoint()
        [Windows.Forms.Cursor]::Position = [Drawing.Point]::new([int]$point.X, [int]$point.Y)
        [CapabilityWindow]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
        [CapabilityWindow]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
    } else { throw "No activation pattern for $($Control.Current.Name)" }
}

function Save-UI {
    param([string]$Name, $Root = $script:window)
    if (-not $Root) { return }
    $controls = $Root.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
    $rows = @($controls | ForEach-Object {
        @{ name = $_.Current.Name; type = $_.Current.ControlType.ProgrammaticName; id = $_.Current.AutomationId; enabled = $_.Current.IsEnabled; offscreen = $_.Current.IsOffscreen }
    })
    Protect-DiagnosticText ($rows | ConvertTo-Json -Depth 5) | Set-Content (Join-Path $evidence "$Name.json")
}

function Select-Folder {
    $folderButton = Wait-Control -Names @('Project or folder', 'Work in a folder', 'Choose a folder', 'Select a folder', 'Add folder')
    $folderButtonName = $folderButton.Current.Name
    Invoke-Control $folderButton
    Start-Sleep -Seconds 1
    Save-UI 'folder-menu'
    $choices = $script:window.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
    $folderChoice = $choices | Where-Object {
        $_.Current.IsEnabled -and -not $_.Current.IsOffscreen -and
        $_.Current.Name -ne $folderButtonName -and
        $_.Current.Name -match '(?i)(choose|select|add|open|work in).{0,30}folder' -and
        $_.Current.ControlType.ProgrammaticName -in @('ControlType.MenuItem', 'ControlType.Button')
    } | Select-Object -First 1
    if ($folderChoice) { Invoke-Control $folderChoice }
    $desktop = [Windows.Automation.AutomationElement]::RootElement
    Start-Sleep -Seconds 1
    Save-UI 'folder-dialog' -Root $desktop
    $dialog = Wait-Control -Names @('Add folders to this session') -Seconds 20 -Root $desktop
    if (-not [CapabilityWindow]::SetForegroundWindow([IntPtr]$dialog.Current.NativeWindowHandle)) {
        throw 'Could not activate the native folder picker'
    }
    [Windows.Forms.Clipboard]::SetText($task.folder)
    [Windows.Forms.SendKeys]::SendWait('^l')
    [Windows.Forms.SendKeys]::SendWait('^v')
    [Windows.Forms.SendKeys]::SendWait('{ENTER}')
    Start-Sleep -Seconds 1
    Invoke-Control (Wait-Control -Names @('Select Folder', 'Select folder', 'Open') -Seconds 20 -Root $dialog)
    $permission = Wait-Control -Names @(
        'Allow Claude to change files in “CoworkProbe”?',
        'Allow Claude to access “CoworkProbe”?',
        'Allow Claude to work in “CoworkProbe”?'
    ) -Seconds 20
    Invoke-Control (Wait-Control -Names @('Allow') -Seconds 10 -Root $permission)
    $report.folderSelected = $true
    Save-UI 'folder-selected'
}

try {
    $task = New-CoworkTask -Capability $Capability -EvidenceDirectory $evidence
    $task.startedAfter = $started.ToUniversalTime().ToString('o')
    $task | ConvertTo-Json | Set-Content -LiteralPath $taskPath
    Start-Process -FilePath $env:CLAUDE_DESKTOP_EXECUTABLE -ArgumentList '--force-renderer-accessibility' | Out-Null
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        $process = Get-Process -Name Claude -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
        if ($process) { break }
        Start-Sleep -Seconds 1
    } until ([DateTime]::UtcNow -ge $deadline)
    if (-not $process) { throw 'Claude did not open a window' }
    $script:window = [Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle)
    Invoke-Control (Wait-Control -Names @('Cowork'))
    Save-UI 'cowork-open'
    $newTask = Find-Control -Names @('New', 'New task', 'New Task')
    if ($newTask) { Invoke-Control $newTask }
    $configuration = Get-Content (Join-Path (Split-Path $evidence -Parent) 'configuration.json') -Raw | ConvertFrom-Json
    $report.initialModel = Select-CoworkModel -Root $script:window -Label $configuration.targetModelLabel
    $report.model = $configuration.targetModel
    $editor = Wait-Control -Names @('Write your prompt to Claude')
    Save-UI 'new-task'
    if ($task.capability -eq 'file') { Select-Folder }
    $editor = Wait-Control -Names @('Write your prompt to Claude')
    $valuePattern = $null
    if ($editor.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern, [ref]$valuePattern)) {
        $valuePattern.SetValue($task.prompt)
    } else {
        $editor.SetFocus()
        [Windows.Forms.Clipboard]::SetText($task.prompt)
        [Windows.Forms.SendKeys]::SendWait('^v')
    }
    Invoke-Control (Wait-Control -Names @('Start task'))
    $report.submitted = $true
    $deadline = [DateTime]::UtcNow.AddSeconds(300)
    # Desktop may ask before a connector tool's first use; the prompt label is not pinned.
    $toolPermissionLabels = @('Allow once', 'Allow for this task', 'Allow for this chat', 'Always allow')
    do {
        Start-Sleep -Seconds 3
        if ($task.capability -eq 'mcp') {
            $allow = Find-Control -Names $toolPermissionLabels
            if ($allow) {
                $report.toolPermission = $allow.Current.Name
                Invoke-Control $allow
            }
        }
        $rawEvidence = & $python "$PSScriptRoot/session-evidence.py" --task $taskPath `
            --sessions-root (Join-Path $env:LOCALAPPDATA 'Claude-3p/local-agent-mode-sessions') | Out-String
        if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the native Desktop session' }
        $sessionEvidence = $rawEvidence | ConvertFrom-Json
        if ($sessionEvidence.status -eq 'pending') { continue }
        $report.session = $sessionEvidence
        if ($sessionEvidence.status -ne 'passed') { throw $sessionEvidence.error }
        $report.passed = $true
        break
    } until ([DateTime]::UtcNow -ge $deadline)
    Save-UI 'result-ui'
    if (-not $report.passed) { throw 'No completed native task with successful tool evidence within 300 seconds' }
} catch {
    $report.error = $_.Exception.Message
} finally {
    try {
        Save-UI 'final-ui'
        if ($script:window) {
            $rect = $script:window.Current.BoundingRectangle
            $bitmap = [Drawing.Bitmap]::new([int]$rect.Width, [int]$rect.Height)
            $graphics = [Drawing.Graphics]::FromImage($bitmap)
            try {
                $graphics.CopyFromScreen([Drawing.Point]::new([int]$rect.X, [int]$rect.Y), [Drawing.Point]::Empty, $bitmap.Size)
                $bitmap.Save((Join-Path $evidence 'screen.png'), [Drawing.Imaging.ImageFormat]::Png)
            } finally { $graphics.Dispose(); $bitmap.Dispose() }
        }
    } catch { $report.captureError = $_.Exception.Message }
    $processes = @(Get-Process -Name Claude -ErrorAction SilentlyContinue)
    $processes | Stop-Process -Force -ErrorAction SilentlyContinue
    foreach ($process in $processes) {
        try { [void]$process.WaitForExit(10000) } catch { }
    }
    if ($report.session.transcript) {
        try {
            $text = Get-Content $report.session.transcript -Raw
            Protect-DiagnosticText $text | Set-Content (Join-Path $evidence 'session.jsonl')
        } catch { $report.captureError = $_.Exception.Message }
    }
    if ($Capability -eq 'mcp') {
        # Desktop's own logs show whether it started each connector; MSIX may virtualize AppData.
        try {
            $logRoots = @(
                @(Get-ChildItem $env:APPDATA -Directory -Filter 'Claude*' -ErrorAction SilentlyContinue)
                @(Get-ChildItem $env:LOCALAPPDATA -Directory -Filter 'Claude*' -ErrorAction SilentlyContinue)
                @(Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Packages') -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue)
            )
            $logs = @($logRoots | ForEach-Object {
                Get-ChildItem $_.FullName -Recurse -File -Filter '*.log' -ErrorAction SilentlyContinue
            } | Where-Object { $_.FullName -match '[\\/]logs[\\/]' -and $_.LastWriteTime -ge $started })
            $logDirectory = Join-Path $evidence 'desktop-logs'
            New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
            foreach ($log in $logs) {
                $name = ($log.FullName.Substring($env:USERPROFILE.Length) -replace '[\\/:]', '-').Trim('-')
                $text = (Get-Content -LiteralPath $log.FullName -Tail 2000 -ErrorAction Stop) -join "`n"
                Protect-DiagnosticText $text | Set-Content -LiteralPath (Join-Path $logDirectory $name)
            }
            $report.desktopLogs = $logs.Count
        } catch { $report.logCaptureError = $_.Exception.Message }
    }
    Remove-Item $taskPath -ErrorAction SilentlyContinue
    Protect-DiagnosticText ($report | ConvertTo-Json -Depth 8) | Set-Content (Join-Path $evidence 'result.json')
}
if (-not $report.passed) { throw "$Capability task failed; see result.json and UI snapshots" }
