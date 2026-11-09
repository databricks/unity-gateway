$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Use a disposable GitHub runner' }
Add-Type -AssemblyName System.Windows.Forms, System.Drawing, UIAutomationClient, UIAutomationTypes
. "$PSScriptRoot/diagnostics.ps1"
$evidence = Join-Path $env:RUNNER_TEMP 'cowork-capability'
$task = Get-Content (Join-Path $env:RUNNER_TEMP 'cowork-capability-task.json') -Raw | ConvertFrom-Json
$report = [ordered]@{ capability = $task.capability; passed = $false; submitted = $false }
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
    } else { throw "No activation pattern for $($Control.Current.Name)" }
}

function Save-UI {
    param([string]$Name)
    if (-not $script:window) { return }
    $controls = $script:window.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
    $rows = @($controls | ForEach-Object {
        @{ name = $_.Current.Name; type = $_.Current.ControlType.ProgrammaticName; id = $_.Current.AutomationId; enabled = $_.Current.IsEnabled; offscreen = $_.Current.IsOffscreen }
    })
    Protect-DiagnosticText ($rows | ConvertTo-Json -Depth 5) | Set-Content (Join-Path $evidence "$Name.json")
}

function Select-Folder {
    Invoke-Control (Wait-Control -Names @('Work in a folder', 'Choose a folder', 'Select a folder', 'Add folder'))
    $desktop = [Windows.Automation.AutomationElement]::RootElement
    $dialog = Wait-Control -Names @('Select Folder', 'Select folder', 'Choose a folder', 'Open', 'Browse For Folder') -Seconds 20 -Root $desktop
    $dialog.SetFocus()
    [Windows.Forms.Clipboard]::SetText($task.folder)
    [Windows.Forms.SendKeys]::SendWait('^l')
    [Windows.Forms.SendKeys]::SendWait('^v')
    [Windows.Forms.SendKeys]::SendWait('{ENTER}')
    Invoke-Control (Wait-Control -Names @('Select Folder', 'Select folder', 'Open') -Seconds 20 -Root $dialog)
    $report.folderSelected = $true
    Save-UI 'folder-selected'
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
    $newTask = Find-Control -Names @('New task', 'New Task')
    if ($newTask) { Invoke-Control $newTask }
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
        $runtimeError = @($names | Where-Object { $_ -match '(?i)(virtualization.{0,80}(disabled|unavailable|enable)|virtual machine.{0,80}(failed|unable)|Cowork.{0,60}(not available|requires))' })
        if ($runtimeError.Count) { throw ($runtimeError -join '; ') }
    } until ([DateTime]::UtcNow -ge $deadline)
    Save-UI 'result-ui'
    if (-not $report.answerVerified) { throw 'No completed assistant answer with the expected result within 300 seconds' }
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
