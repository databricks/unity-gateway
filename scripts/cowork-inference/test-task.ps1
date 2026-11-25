param(
    [Parameter(Mandatory)][ValidateSet('file', 'skill', 'mcp')][string]$Capability,
    [Parameter(Mandatory)][string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Use a disposable GitHub runner' }
Add-Type -AssemblyName System.Windows.Forms, System.Drawing, UIAutomationClient, UIAutomationTypes
Add-Type @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
public static class CapabilityWindow {
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")]
    public static extern void mouse_event(uint flags, uint x, uint y, uint data, UIntPtr extra);
    [DllImport("user32.dll", EntryPoint = "RealGetWindowClassW", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    private static extern uint RealGetWindowClass(IntPtr window, StringBuilder name, uint length);
    [DllImport("user32.dll", EntryPoint = "SendMessageTimeoutW", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    private static extern IntPtr SendText(IntPtr window, uint message, IntPtr wParam, string text, uint flags, uint timeout, out IntPtr result);
    [DllImport("user32.dll", EntryPoint = "SendMessageTimeoutW", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    private static extern IntPtr ReadText(IntPtr window, uint message, IntPtr wParam, StringBuilder text, uint flags, uint timeout, out IntPtr result);
    [DllImport("user32.dll", EntryPoint = "SendMessageTimeoutW", ExactSpelling = true, SetLastError = true)]
    private static extern IntPtr ReadLength(IntPtr window, uint message, IntPtr wParam, IntPtr lParam, uint flags, uint timeout, out IntPtr result);
    private static uint Remaining(DateTime deadline) {
        double ms = (deadline.ToUniversalTime() - DateTime.UtcNow).TotalMilliseconds;
        if (ms < 1) throw new TimeoutException("Folder field deadline expired");
        return (uint)Math.Min(ms, UInt32.MaxValue);
    }
    public static string WindowClass(IntPtr window) {
        var name = new StringBuilder(256);
        if (RealGetWindowClass(window, name, (uint)name.Capacity) == 0) throw new Win32Exception(Marshal.GetLastWin32Error());
        return name.ToString();
    }
    public static string SetAndReadFolder(IntPtr window, string value, DateTime deadline) {
        IntPtr result;
        const uint abortIfHung = 2;
        if (SendText(window, 0x000C, IntPtr.Zero, value, abortIfHung, Remaining(deadline), out result) == IntPtr.Zero || result == IntPtr.Zero)
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not set the folder field");
        if (ReadLength(window, 0x000E, IntPtr.Zero, IntPtr.Zero, abortIfHung, Remaining(deadline), out result) == IntPtr.Zero)
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not read the folder field length");
        long length = result.ToInt64();
        if (length < 0 || length > 32767) throw new InvalidOperationException("Invalid folder field length");
        var text = new StringBuilder((int)length + 1);
        if (ReadText(window, 0x000D, (IntPtr)text.Capacity, text, abortIfHung, Remaining(deadline), out result) == IntPtr.Zero)
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not read the folder field");
        return text.ToString();
    }
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
$profilePath = Join-Path $env:LOCALAPPDATA 'Claude-3p/configLibrary/5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6.json'
$policyBackup = Join-Path $env:RUNNER_TEMP ("cowork-desktop-profile-" + [Guid]::NewGuid().ToString('N') + '.backup')
$script:window = $null
$started = Get-Date
$vmLog = Join-Path $env:LOCALAPPDATA 'Claude-3p/logs/cowork_vm_node.log'
$vmCheckpoint = Get-CoworkLogCheckpoint -LogPath $vmLog

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
        try {
            $valuePattern = $null
            $value = if ($_.Current.IsPassword) { '[REDACTED]' }
                elseif ($_.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern, [ref]$valuePattern)) {
                    Protect-DiagnosticText ([string]$valuePattern.Current.Value)
                } else { $null }
            Convert-CoworkUiControl -Current $_.Current -Value $value -GetClass {
                param($Handle)
                [CapabilityWindow]::WindowClass($Handle)
            }
        } catch [Windows.Automation.ElementNotAvailableException] { }
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
    $selectionDeadline = [DateTime]::UtcNow.AddSeconds(20)
    try {
        $field = Set-CoworkFolderInput -Folder $task.folder -Deadline $selectionDeadline -ReadEdit {
            $condition = [Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::AutomationIdProperty, '1152')
            $edit = $dialog.FindFirst([Windows.Automation.TreeScope]::Descendants, $condition)
            if ($edit) {
                $nativeHandle = $edit.Current.NativeWindowHandle
                $handle = if ($null -ne $nativeHandle) { [IntPtr]$nativeHandle } else { [IntPtr]::Zero }
                @{
                    id = $edit.Current.AutomationId
                    type = $edit.Current.ControlType.ProgrammaticName
                    enabled = $edit.Current.IsEnabled -and -not $edit.Current.IsOffscreen
                    handle = $handle
                    class = if ($handle -ne [IntPtr]::Zero) { [CapabilityWindow]::WindowClass($handle) } else { '' }
                }
            }
        } -WriteAndRead {
            param($State, $Folder, $Deadline)
            [CapabilityWindow]::SetAndReadFolder([IntPtr]$State.handle, $Folder, $Deadline)
        }
    } catch {
        $failure = $_
        try { Save-UI 'folder-input-failed' -Root $dialog }
        catch { $report.captureError = $_.Exception.Message }
        throw $failure
    }
    @{
        id = $field.id
        type = $field.type
        class = $field.class
        value = Protect-DiagnosticText $field.value
    } | ConvertTo-Json | Set-Content (Join-Path $evidence 'folder-field.json')
    Save-UI 'folder-input' -Root $dialog
    $button = Wait-CoworkFolderSelect -Deadline $selectionDeadline -ReadPicker {
        $addresses = $dialog.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
        $address = $addresses | Where-Object {
            try {
                $_.Current.ControlType.ProgrammaticName -eq 'ControlType.Pane' -and
                $_.Current.Name -like 'Address:*' -and -not $_.Current.IsOffscreen
            } catch [Windows.Automation.ElementNotAvailableException] { $false }
        } | Select-Object -First 1
        @{
            address = if ($address) { $address.Current.Name } else { '' }
            button = Find-Control -Names @('Select Folder', 'Select folder', 'Open') -Root $dialog
        }
    }
    if ([DateTime]::UtcNow -ge $selectionDeadline) { throw 'Folder selection deadline expired before the click' }
    Invoke-Control $button
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
    if ($Capability -eq 'mcp') {
        & $python "$PSScriptRoot/desktop-policy.py" apply $profilePath $policyBackup
        if ($LASTEXITCODE -ne 0) { throw 'Could not restrict native web search for the MCP task' }
    }
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
    Wait-CoworkVmReady -Deadline $deadline -IsReady {
        Test-CoworkVmReady -LogPath $vmLog -Offset $vmCheckpoint.offset -PrefixHash $vmCheckpoint.prefixHash
    }
    $report.vmReady = $true
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
        $report.session = $sessionEvidence
        if ($sessionEvidence.status -eq 'pending') { continue }
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
    if (Test-Path -LiteralPath $policyBackup) {
        & $python "$PSScriptRoot/desktop-policy.py" restore $profilePath $policyBackup
        if ($LASTEXITCODE -ne 0) {
            $report.policyRestoreError = 'Could not restore the Desktop profile after the MCP task'
            $report.passed = $false
        }
    }
    if ($Capability -eq 'mcp' -and $report.submitted -and -not $report.session.transcript) {
        try {
            $rawEvidence = & $python "$PSScriptRoot/session-evidence.py" --task $taskPath `
                --sessions-root (Join-Path $env:LOCALAPPDATA 'Claude-3p/local-agent-mode-sessions') | Out-String
            if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the native Desktop session' }
            $report.session = $rawEvidence | ConvertFrom-Json
        } catch { $report.captureError = $_.Exception.Message }
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
            # UG's MCP proxy backend explains a connector that Desktop reports as closed.
            $backendRoot = Join-Path $env:USERPROFILE '.ucode/mcp-proxy'
            $backendLogs = @(Get-ChildItem $backendRoot -Recurse -File -Filter 'backend.log' -ErrorAction SilentlyContinue)
            $report.mcpBackendRootExists = Test-Path -LiteralPath $backendRoot
            $report.mcpBackendLogs = $backendLogs.Count
            $logs += $backendLogs
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
