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

function Get-Windows {
    [Console]::Error.WriteLine('uia phase=find-windows')
    @($root.FindAll([Windows.Automation.TreeScope]::Children, $byProcess))
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

function Find-Target($windows, $role, $name, $matchValue) {
    $typeName = $role -replace '^ControlType\.', ''
    $field = [Windows.Automation.ControlType].GetField($typeName)
    if (-not $field) { throw "Unknown control type $role" }
    $conditions = @(
        [Windows.Automation.PropertyCondition]::new($automation::ControlTypeProperty, $field.GetValue($null))
        [Windows.Automation.PropertyCondition]::new($automation::IsEnabledProperty, $true)
        [Windows.Automation.PropertyCondition]::new($automation::IsOffscreenProperty, $false)
    )
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
            $waitClock = [Diagnostics.Stopwatch]::StartNew()
            try {
                do {
                    $polls++
                    $windows = @(Get-Windows)
                    $target = Find-Target $windows $first $second ($action -eq 'wait')
                    if ($target) { break }
                    if ([DateTime]::UtcNow -ge $deadline) { throw "No enabled $first named '$second'" }
                    Start-Sleep -Milliseconds 250
                } while ($true)
            } finally {
                $script:metrics.wait_ms = $waitClock.Elapsed.TotalMilliseconds
                $script:metrics.polls = $polls
            }
            if ($action -eq 'wait') { "$polls"; break }
            $clickClock = [Diagnostics.Stopwatch]::StartNew()
            try {
                $pattern = $null
                if ($target.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
                    $pattern.Invoke()
                } elseif ($target.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) {
                    $pattern.Select()
                } else {
                    $target.SetFocus()
                }
            } finally {
                $script:metrics.click_ms = $clickClock.Elapsed.TotalMilliseconds
            }
            if ($action -eq 'click') { "$polls" }
        }
        'type' {
            $focused = $automation::FocusedElement.Current.ProcessId
            if ($focused -ne $ProcessId) { throw "Keyboard focus is in process $focused, not $ProcessId" }
            [Windows.Forms.SendKeys]::SendWait(($first -replace '[+^%~(){}\[\]]', '{$0}'))
            if ($second -eq 'enter') { [Windows.Forms.SendKeys]::SendWait('{ENTER}') }
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
        [Console]::Out.WriteLine(($response | ConvertTo-Json -Compress -Depth 4))
    }
} else {
    Invoke-Action $Command @($First, $Second)
}
