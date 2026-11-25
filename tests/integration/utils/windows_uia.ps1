# Drive the app's visible Windows UI Automation controls from one persistent process.
param(
    [Parameter(Mandatory)][ValidateSet('session', 'tree', 'invoke', 'type', 'screenshot')][string]$Command,
    [Parameter(Mandatory)][int]$ProcessId,
    [string]$First = '',
    [string]$Second = ''
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
$processToScriptMs = [Math]::Max(0, ([DateTime]::UtcNow - [Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime()).TotalMilliseconds)
$initClock = [Diagnostics.Stopwatch]::StartNew()
[Console]::Error.WriteLine('uia phase=load-assemblies')
$moduleClock = [Diagnostics.Stopwatch]::StartNew()
Import-Module Microsoft.PowerShell.Utility
$moduleMs = $moduleClock.Elapsed.TotalMilliseconds
$uiaClock = [Diagnostics.Stopwatch]::StartNew()
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$script:probeLoadError = $null
try {
    if ($env:UG_UIA_PROBE_ASSEMBLY) {
        Add-Type -Path $env:UG_UIA_PROBE_ASSEMBLY
    } else {
        $probeReferences = @([Windows.Automation.AutomationElement].Assembly.Location, [Windows.Automation.AutomationProperty].Assembly.Location)
        Add-Type -Path "$PSScriptRoot/windows_uia_probe.cs" -ReferencedAssemblies $probeReferences
    }
} catch { $script:probeLoadError = $_.Exception.GetType().Name }
$uiaMs = $uiaClock.Elapsed.TotalMilliseconds
$formsClock = [Diagnostics.Stopwatch]::StartNew()
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$formsMs = $formsClock.Elapsed.TotalMilliseconds
$rootClock = [Diagnostics.Stopwatch]::StartNew()
$automation = [Windows.Automation.AutomationElement]
$root = $automation::RootElement
$byProcess = [Windows.Automation.PropertyCondition]::new($automation::ProcessIdProperty, $ProcessId)
$rootMs = $rootClock.Elapsed.TotalMilliseconds
$initMetrics = @{
    process_to_script_ms = $processToScriptMs
    module_ms = $moduleMs
    uia_assembly_ms = $uiaMs
    forms_assembly_ms = $formsMs
    root_ms = $rootMs
    init_ms = $initClock.Elapsed.TotalMilliseconds
}
$script:metrics = @{}
$script:diagnostics = @{}
$script:composer = $null
$script:composerClickedAt = 0
$script:pickerWindows = @()
$script:pickerNewWindows = [Collections.Generic.HashSet[int]]::new()

function Get-Windows {
    [Console]::Error.WriteLine('uia phase=find-windows')
    @($root.FindAll([Windows.Automation.TreeScope]::Children, $byProcess))
}

function Observe-PickerWindows($windows) {
    try {
        foreach ($window in $windows) {
            $handle = $window.Current.NativeWindowHandle
            if ($handle -and $handle -notin $script:pickerWindows) { [void]$script:pickerNewWindows.Add($handle) }
        }
    } catch { $script:diagnostics.picker_window_error = $_.Exception.GetType().Name }
}

function Get-Elements($windows) {
    $elements = @(foreach ($window in $windows) {
        $window
        [Console]::Error.WriteLine('uia phase=find-descendants')
        $window.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
    })
    [Console]::Error.WriteLine('uia phase=filter-visible')
    $elements | Where-Object { $_.Current.IsEnabled -and -not $_.Current.IsOffscreen }
}

function Get-Value($element) {
    $pattern = $null
    if ($element.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern, [ref]$pattern)) {
        return $pattern.Current.Value
    }
    ''
}

function Get-ExpandState($target) {
    $pattern = $null
    if ($target.TryGetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$pattern)) {
        return "$($pattern.Current.ExpandCollapseState)"
    }
    return $null
}

function Get-TargetDetails($target, $windows) {
    $editCondition = [Windows.Automation.PropertyCondition]::new($automation::ControlTypeProperty, [Windows.Automation.ControlType]::Edit)
    $editCount = 0
    foreach ($window in $windows) { $editCount += $window.FindAll([Windows.Automation.TreeScope]::Subtree, $editCondition).Count }
    return @{
        name = $target.Current.Name
        value = (Get-Value $target)
        is_enabled = $target.Current.IsEnabled
        runtime_id = @($target.GetRuntimeId())
        expand_collapse_state = (Get-ExpandState $target)
        edit_count = $editCount
    }
}

function Find-Target($windows, $role, $name, $matchValue) {
    $typeName = $role -replace '^ControlType\.', ''
    $field = [Windows.Automation.ControlType].GetField($typeName)
    if (-not $field) { throw "Unknown control type $role" }
    $conditions = @(
        [Windows.Automation.PropertyCondition]::new($automation::ControlTypeProperty, $field.GetValue($null))
        [Windows.Automation.PropertyCondition]::new($automation::IsEnabledProperty, $true)
        [Windows.Automation.PropertyCondition]::new($automation::IsOffscreenProperty, $false)
    )
    if ($name -and $name.Contains(' ')) {
        $condition = [Windows.Automation.AndCondition]::new([Windows.Automation.Condition[]]$conditions)
        foreach ($window in $windows) {
            foreach ($candidate in @($window.FindAll([Windows.Automation.TreeScope]::Subtree, $condition))) {
                $candidateName = "$($candidate.Current.Name)" -replace '[\t\r\n]', ' '
                $candidateValue = if ($matchValue) { "$(Get-Value $candidate)" -replace '[\t\r\n]', ' ' } else { '' }
                if ($candidateName -ceq $name -or ($matchValue -and $candidateValue -ceq $name)) { return $candidate }
            }
        }
        return $null
    }
    if ($name) {
        $byName = [Windows.Automation.PropertyCondition]::new($automation::NameProperty, $name)
        if ($matchValue) {
            $byValue = [Windows.Automation.PropertyCondition]::new([Windows.Automation.ValuePattern]::ValueProperty, $name)
            $conditions += [Windows.Automation.OrCondition]::new($byName, $byValue)
        } else {
            $conditions += $byName
        }
    }
    $condition = [Windows.Automation.AndCondition]::new([Windows.Automation.Condition[]]$conditions)
    foreach ($window in $windows) {
        $target = $window.FindFirst([Windows.Automation.TreeScope]::Subtree, $condition)
        if ($target) { return $target }
    }
    return $null
}

function Invoke-Action($action, $argsList) {
    $first = if ($argsList.Count -gt 0) { [string]$argsList[0] } else { '' }
    $second = if ($argsList.Count -gt 1) { [string]$argsList[1] } else { '' }
    if ($action -ne 'type') { $script:composer = $null }
    $windows = if ($action -in @('click', 'wait')) { @() } else { @(Get-Windows) }
    if (-not $windows.Count -and ($action -notin @('click', 'wait'))) { throw "Process $ProcessId has no window" }
    switch ($action) {
        'tree' {
            foreach ($element in Get-Elements $windows) {
                $fields = $element.Current.ControlType.ProgrammaticName, $element.Current.Name, (Get-Value $element)
                ($fields | ForEach-Object { "$_" -replace '[\t\r\n]', ' ' }) -join "`t"
            }
        }
        { $_ -in 'invoke', 'click', 'wait' } {
            $timeout = if (($action -in @('click', 'wait')) -and $argsList.Count -gt 2) { [double]$argsList[2] } else { 0 }
            $deadline = [DateTime]::UtcNow.AddSeconds($timeout)
            $polls = 0
            $watchPicker = $action -eq 'click' -and $first -eq 'ControlType.MenuItem' -and $script:pickerWindows.Count -gt 0
            $waitClock = [Diagnostics.Stopwatch]::StartNew()
            try {
                do {
                    $polls++
                    $windows = @(Get-Windows)
                    if ($watchPicker) { Observe-PickerWindows $windows }
                    $target = Find-Target $windows $first $second ($action -eq 'wait')
                    if ($target) { break }
                    if ([DateTime]::UtcNow -ge $deadline) { throw "No enabled $first named '$second'" }
                    Start-Sleep -Milliseconds 250
                } while ($true)
            } finally {
                $script:metrics.wait_ms = $waitClock.Elapsed.TotalMilliseconds
                $script:metrics.polls = $polls
                if ($watchPicker) { $script:metrics.picker_new_window_count = $script:pickerNewWindows.Count }
            }
            if ($action -eq 'wait') { "$polls"; break }
            $isPicker = $action -eq 'click' -and $argsList.Count -gt 3 -and $argsList[3] -eq 'picker'
            $observeTarget = $action -eq 'click' -and ($first -eq 'ControlType.Edit' -or $isPicker)
            if ($isPicker) {
                try { $script:pickerWindows = @($windows | ForEach-Object { $_.Current.NativeWindowHandle }) }
                catch { $script:diagnostics.picker_window_error = $_.Exception.GetType().Name; $script:pickerWindows = @() }
                $script:pickerNewWindows.Clear()
                $script:diagnostics.top_level_windows_before = $script:pickerWindows.Count
                $script:diagnostics.activation_succeeded = $false
            }
            if ($observeTarget) {
                $snapshotClock = [Diagnostics.Stopwatch]::StartNew()
                try { $script:diagnostics.target = Get-TargetDetails $target $windows }
                catch { $script:diagnostics.target_error = $_.Exception.GetType().Name }
                $script:metrics.target_snapshot_ms = $snapshotClock.Elapsed.TotalMilliseconds
            }
            $clickClock = [Diagnostics.Stopwatch]::StartNew()
            try {
                $pattern = $null
                if ($target.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
                    if ($isPicker) { $script:diagnostics.activation = 'invoke' }
                    $pattern.Invoke()
                } elseif ($target.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) {
                    if ($isPicker) { $script:diagnostics.activation = 'selection' }
                    $pattern.Select()
                } else {
                    if ($isPicker) { $script:diagnostics.activation = 'focus' }
                    $target.SetFocus()
                }
                if ($isPicker) { $script:diagnostics.activation_succeeded = $true }
            } finally {
                $script:metrics.click_ms = $clickClock.Elapsed.TotalMilliseconds
            }
            if ($isPicker) {
                try { $script:diagnostics.target_after = @{ expand_collapse_state = (Get-ExpandState $target); runtime_id = @($target.GetRuntimeId()) } }
                catch { $script:diagnostics.target_after_error = $_.Exception.GetType().Name }
                try { Observe-PickerWindows @(Get-Windows) }
                catch { $script:diagnostics.picker_window_error = $_.Exception.GetType().Name }
                $script:metrics.picker_new_window_count = $script:pickerNewWindows.Count
            }
            if ($action -eq 'click' -and $first -eq 'ControlType.Edit') {
                $script:composer = $target
                $script:composerClickedAt = [Diagnostics.Stopwatch]::GetTimestamp()
            }
            if ($action -eq 'click') { "$polls" }
        }
        'type' {
            $observedComposer = $script:composer
            $observePickerType = -not $observedComposer -and $script:pickerWindows.Count -gt 0
            if ($observePickerType) { Observe-PickerWindows $windows }
            if ($script:composer) {
                $focusClock = [Diagnostics.Stopwatch]::StartNew()
                $hasFocus = $script:composer.Current.HasKeyboardFocus
                $script:metrics.composer_focus_before_wait = [int]$hasFocus
                try {
                    while (-not $hasFocus) {
                        $sinceClick = ([Diagnostics.Stopwatch]::GetTimestamp() - $script:composerClickedAt) / [Diagnostics.Stopwatch]::Frequency
                        if ($sinceClick -ge 60) { throw 'The composer did not receive keyboard focus' }
                        Start-Sleep -Milliseconds 250
                        $hasFocus = $script:composer.Current.HasKeyboardFocus
                    }
                } finally {
                    $script:metrics.composer_focus_wait_ms = $focusClock.Elapsed.TotalMilliseconds
                }
            }
            $focused = $automation::FocusedElement.Current.ProcessId
            if ($focused -ne $ProcessId) { throw "Keyboard focus is in process $focused, not $ProcessId" }
            $probe = $null
            if ($observedComposer -and $script:probeLoadError) {
                $script:diagnostics.composer_probe_error = $script:probeLoadError
            } elseif ($observedComposer) {
                try {
                    $probe = [WindowsUIA.ComposerProbe]::new()
                    foreach ($window in $windows) { $probe.Observe($window) }
                } catch {
                    $script:diagnostics.composer_probe_error = $_.Exception.GetType().Name
                    if ($probe) { try { $probe.Dispose() } catch { $script:diagnostics.composer_probe_dispose_error = $_.Exception.GetType().Name } }
                    $probe = $null
                }
            }
            try {
                if ($script:composer) {
                    $script:metrics.composer_focus_at_first_key = [int]$script:composer.Current.HasKeyboardFocus
                    $script:metrics.click_to_first_key_ms = 1000 * ([Diagnostics.Stopwatch]::GetTimestamp() - $script:composerClickedAt) / [Diagnostics.Stopwatch]::Frequency
                    if (-not $script:metrics.composer_focus_at_first_key) { throw 'The composer lost keyboard focus before typing' }
                    $script:composer = $null
                }
                [Windows.Forms.SendKeys]::SendWait(($first -replace '[+^%~(){}\[\]]', '{$0}'))
                if ($second -eq 'enter') { [Windows.Forms.SendKeys]::SendWait('{ENTER}') }
            } finally {
                if ($probe) {
                    try { $probe.Dispose() } catch { $script:diagnostics.composer_probe_error = $_.Exception.GetType().Name }
                    $script:diagnostics.composer_samples = @($probe.GetSamples())
                    $script:diagnostics.composer_probe_errors = $probe.ErrorCount
                }
                if ($observePickerType) {
                    Observe-PickerWindows @(Get-Windows)
                    $script:metrics.picker_new_window_count = $script:pickerNewWindows.Count
                }
                if ($observedComposer) {
                    try {
                        $current = Find-Target @(Get-Windows) 'ControlType.Edit' '' $false
                        if ($current) { $script:diagnostics.composer_final = @{ value = (Get-Value $current); runtime_id = @($current.GetRuntimeId()) } }
                    } catch { $script:diagnostics.composer_final_error = $_.Exception.GetType().Name }
                }
            }
        }
        'screenshot' {
            $rect = $windows[0].Current.BoundingRectangle
            $bitmap = [Drawing.Bitmap]::new([int]$rect.Width, [int]$rect.Height)
            $graphics = [Drawing.Graphics]::FromImage($bitmap)
            try {
                $graphics.CopyFromScreen([int]$rect.X, [int]$rect.Y, 0, 0, $bitmap.Size)
                $bitmap.Save($first, [Drawing.Imaging.ImageFormat]::Png)
            } finally {
                $graphics.Dispose()
                $bitmap.Dispose()
            }
        }
        default { throw "Unknown UI Automation action $action" }
    }
}

if ($Command -eq 'session') {
    [Console]::Out.WriteLine((@{ event = 'ready'; metrics = $initMetrics } | ConvertTo-Json -Compress -Depth 4))
    while ($null -ne ($line = [Console]::ReadLine())) {
        $script:metrics = @{} + $initMetrics
        $script:diagnostics = @{}
        $actionClock = [Diagnostics.Stopwatch]::StartNew()
        try {
            $request = $line | ConvertFrom-Json
            $output = @(Invoke-Action $request.command @($request.args)) -join "`n"
            $response = @{ ok = $true; output = $output; error = '' }
        } catch {
            $response = @{ ok = $false; output = ''; error = $_.Exception.Message }
        }
        $script:metrics.action_ms = $actionClock.Elapsed.TotalMilliseconds
        $response.metrics = $script:metrics
        $response.diagnostics = $script:diagnostics
        [Console]::Out.WriteLine(($response | ConvertTo-Json -Compress -Depth 8))
    }
} else {
    Invoke-Action $Command @($First, $Second)
}
