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
$evidence = Join-Path $env:RUNNER_TEMP 'cowork-capability'
$task = Get-Content (Join-Path $env:RUNNER_TEMP 'cowork-capability-task.json') -Raw | ConvertFrom-Json
$report = [ordered]@{ capability = $task.capability; passed = $false; submitted = $false }
if ($task.credentialSource) { $report.credentialSource = $task.credentialSource }
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
    } elseif ($Control.TryGetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$pattern)) {
        $pattern.Expand()
    } elseif ($Control.Current.ControlType.ProgrammaticName -in @('ControlType.Button', 'ControlType.MenuItem') -or
        ($Control.Current.Name -eq 'Select Folder' -and $Control.Current.ControlType.ProgrammaticName -eq 'ControlType.Pane')) {
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
    Invoke-Control (Wait-Control -Names @('Project or folder', 'Work in a folder', 'Choose a folder', 'Select a folder', 'Add folder'))
    Start-Sleep -Seconds 1
    Save-UI 'folder-menu'
    $choices = $script:window.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
    $folderChoice = $choices | Where-Object {
        $_.Current.IsEnabled -and -not $_.Current.IsOffscreen -and
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
    $permission = Wait-Control -Names @('Allow Claude to change files in “CoworkProbe”?') -Seconds 20
    Invoke-Control (Wait-Control -Names @('Allow') -Seconds 10 -Root $permission)
    $report.folderSelected = $true
    Save-UI 'folder-selected'
}

function Read-CompletedAnswer {
    $sessions = Join-Path $env:LOCALAPPDATA 'Claude-3p/local-agent-mode-sessions'
    $files = @(Get-ChildItem $sessions -Recurse -Filter '*.jsonl' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $started -and $_.FullName -match '\\projects\\session\\' })
    foreach ($file in $files) {
        $records = @(foreach ($line in (Get-Content $file.FullName -ErrorAction SilentlyContinue)) {
            try { $line | ConvertFrom-Json -ErrorAction Stop } catch { continue }
        })
        $taskPresent = @($records | Where-Object {
            $_.type -eq 'user' -and ($_.message.content | ConvertTo-Json -Depth 12 -Compress).Contains($task.prompt)
        }).Count -gt 0
        if (-not $taskPresent) { continue }
        foreach ($record in $records) {
            if ($record.type -ne 'assistant' -or $record.message.stop_reason -ne 'end_turn') { continue }
            foreach ($block in @($record.message.content)) {
                if ($block.type -eq 'text' -and $block.text) { return $block.text }
            }
        }
    }
    return $null
}

try {
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
    if ($task.capability -eq 'mcp') {
        $mainLog = Join-Path $env:LOCALAPPDATA 'Claude-3p/logs/main.log'
        $deadline = [DateTime]::UtcNow.AddSeconds(90)
        do {
            $connected = Get-Content $mainLog -Tail 200 -ErrorAction SilentlyContinue |
                Select-String -SimpleMatch "[custom3p-mcp] connected { name: '$($task.server)'"
            if ($connected) { break }
            Start-Sleep -Seconds 1
        } until ([DateTime]::UtcNow -ge $deadline)
        if (-not $connected) { throw 'Native MCP connection was not ready before task creation' }
        $report.connectorReadyBeforeTask = $true
    }
    $newTask = Find-Control -Names @('New task', 'New Task')
    if ($newTask) { Invoke-Control $newTask }
    $editor = Wait-Control -Names @('Write your prompt to Claude')
    Save-UI 'new-task'
    if ($task.capability -eq 'mcp') {
        Invoke-Control (Wait-Control -Names @('Add files, connectors, and more'))
        Start-Sleep -Seconds 1
        Save-UI 'composer-menu'
        $connectors = Find-Control -Names @('Connectors')
        if ($connectors) {
            Invoke-Control $connectors
            Start-Sleep -Seconds 1
            Save-UI 'connectors-menu'
        }
        $connector = Find-Control -Names @($task.server)
        if ($connector) {
            $toggle = $null
            if ($connector.TryGetCurrentPattern([Windows.Automation.TogglePattern]::Pattern, [ref]$toggle)) {
                if ($toggle.Current.ToggleState -eq [Windows.Automation.ToggleState]::Off) { $toggle.Toggle() }
                $report.connectorEnabledInComposer = $true
            }
        }
        [Windows.Forms.SendKeys]::SendWait('{ESC}')
        [Windows.Forms.SendKeys]::SendWait('{ESC}')
    }
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
    do {
        Start-Sleep -Seconds 3
        $controls = $script:window.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
        $names = @($controls | ForEach-Object { $_.Current.Name })
        $answers = @($names | Where-Object { $_ -like 'Claude responded:*' -and $_.Contains($task.expected) })
        if ($answers.Count -and 'Claude finished the response' -in $names) {
            $report.answerVerified = $true
            $report.completed = $true
            $report.answer = $answers[-1]
            break
        }
        $answer = Read-CompletedAnswer
        if ($answer) {
            $report.completed = $true
            $report.answer = $answer
            $report.answerVerified = $answer.Contains($task.expected)
            $report.completionEvidence = 'native Desktop session transcript with end_turn'
            break
        }
        $runtimeError = @($names | Where-Object { $_ -match '(?i)(virtualization.{0,80}(disabled|unavailable|enable)|virtual machine.{0,80}(failed|unable)|Cowork.{0,60}(not available|requires))' })
        if ($runtimeError.Count) { throw ($runtimeError -join '; ') }
    } until ([DateTime]::UtcNow -ge $deadline)
    Save-UI 'result-ui'
    if (-not $report.answerVerified) { throw 'No completed assistant answer with the expected result; see answer and native session evidence' }
    if ($task.capability -ne 'mcp') { $report.passed = $true }
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
    Get-Process -Name Claude -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    $root = Join-Path $env:LOCALAPPDATA 'Claude-3p'
    $files = @(Get-ChildItem (Join-Path $root 'local-agent-mode-sessions') -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $started -and $_.Extension -in @('.jsonl', '.log') } | Select-Object -First 30)
    $report.transcriptFiles = @($files | ForEach-Object { $_.FullName.Substring($root.Length) })
    $index = 0
    $toolCalls = @{}
    $toolResults = @{}
    foreach ($file in $files) {
        $text = Get-Content $file.FullName -Raw -ErrorAction SilentlyContinue
        if (-not $text) { continue }
        Protect-DiagnosticText $text | Set-Content (Join-Path $evidence "session-$index.txt")
        $index++
        foreach ($line in ($text -split "`n")) {
            try { $record = $line | ConvertFrom-Json -ErrorAction Stop } catch { continue }
            foreach ($block in @($record.message.content)) {
                if ($block.type -eq 'tool_use' -and $block.name -match '^mcp__.*web_search') { $toolCalls[$block.id] = $block.name }
                if ($block.type -eq 'tool_result' -and -not $block.is_error -and $block.tool_use_id) {
                    $toolResults[$block.tool_use_id] = $block.content
                }
            }
        }
    }
    $report.webSearchCalls = @($toolCalls.GetEnumerator() | ForEach-Object { @{ name = $_.Value; succeeded = $toolResults.ContainsKey($_.Key) } })
    if ($task.capability -eq 'mcp' -and $report.answerVerified) {
        $report.passed = @($report.webSearchCalls | Where-Object { $_.succeeded }).Count -gt 0
        if (-not $report.passed) { $report.error = 'Answer matched, but successful MCP execution is not yet verified from native evidence' }
    }
    Protect-DiagnosticText ($report | ConvertTo-Json -Depth 8) | Set-Content (Join-Path $evidence 'result.json')
}
if (-not $report.passed) { throw 'Capability not verified; see result.json and UI snapshots' }
