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
[Console]::Error.WriteLine('uia phase=load-assemblies')
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms, System.Drawing
$automation = [Windows.Automation.AutomationElement]
$byProcess = [Windows.Automation.PropertyCondition]::new($automation::ProcessIdProperty, $ProcessId)

function Get-Windows {
    [Console]::Error.WriteLine('uia phase=find-windows')
    @($automation::RootElement.FindAll([Windows.Automation.TreeScope]::Children, $byProcess))
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
    $field = [Windows.Automation.ControlType].GetField($role -replace '^ControlType\.', '')
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
    $windows = @(Get-Windows)
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
            do {
                $polls++
                $windows = @(Get-Windows)
                $target = Find-Target $windows $first $second ($action -eq 'wait')
                if ($target) { break }
                if ([DateTime]::UtcNow -ge $deadline) { throw "No enabled $first named '$second'" }
                Start-Sleep -Milliseconds 250
            } while ($true)
            if ($action -eq 'wait') { "$polls"; break }
            $pattern = $null
            if ($target.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
                $pattern.Invoke()
            } elseif ($target.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) {
                $pattern.Select()
            } else {
                $target.SetFocus()
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
    while ($null -ne ($line = [Console]::ReadLine())) {
        try {
            $request = $line | ConvertFrom-Json
            $output = @(Invoke-Action $request.command @($request.args)) -join "`n"
            $response = @{ ok = $true; output = $output; error = '' }
        } catch {
            $response = @{ ok = $false; output = ''; error = $_.Exception.Message }
        }
        [Console]::Out.WriteLine(($response | ConvertTo-Json -Compress))
    }
} else {
    Invoke-Action $Command @($First, $Second)
}
