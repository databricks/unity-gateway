# A UI Automation client for one app: `tree PID` prints its windows' enabled, on-screen elements,
# `invoke PID TYPE NAME` activates the first matching one (any name if NAME is empty), `type PID TEXT [enter]`
# types into the focused control with real keystrokes and optionally presses Enter, and `screenshot PID PATH`
# saves a PNG of its window.
param(
    [Parameter(Mandatory)][ValidateSet('tree', 'invoke', 'type', 'screenshot')][string]$Command,
    [Parameter(Mandatory)][int]$ProcessId,
    [string]$First = '',
    [string]$Second = ''
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::Error.WriteLine('uia phase=load-assemblies')
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms, System.Drawing
$automation = [Windows.Automation.AutomationElement]
$byProcess = [Windows.Automation.PropertyCondition]::new($automation::ProcessIdProperty, $ProcessId)
[Console]::Error.WriteLine('uia phase=find-windows')
$windows = @($automation::RootElement.FindAll([Windows.Automation.TreeScope]::Children, $byProcess))
if (-not $windows.Count) { throw "Process $ProcessId has no window" }

# Only controls a user could act on, so `tree` (what callers wait for) and `invoke` agree.
function Get-Elements {
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

switch ($Command) {
    'tree' {
        foreach ($element in Get-Elements) {
            $fields = $element.Current.ControlType.ProgrammaticName, $element.Current.Name, (Get-Value $element)
            ($fields | ForEach-Object { "$_" -replace '[\t\r\n]', ' ' }) -join "`t"
        }
    }
    'invoke' {
        $target = Get-Elements | Where-Object {
            $_.Current.ControlType.ProgrammaticName -eq $First -and (-not $Second -or $_.Current.Name -eq $Second)
        } | Select-Object -First 1
        if (-not $target) { throw "No enabled $First named '$Second'" }
        $pattern = $null
        if ($target.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
            $pattern.Invoke()
        } elseif ($target.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) {
            $pattern.Select()
        } else {
            $target.SetFocus()
        }
    }
    'type' {
        # SendKeys types into whatever has focus, so fail now rather than lose the keystrokes.
        $focused = $automation::FocusedElement.Current.ProcessId
        if ($focused -ne $ProcessId) { throw "Keyboard focus is in process $focused, not $ProcessId" }
        # SendKeys treats these characters as commands unless braced.
        [Windows.Forms.SendKeys]::SendWait(($First -replace '[+^%~(){}\[\]]', '{$0}'))
        if ($Second -eq 'enter') { [Windows.Forms.SendKeys]::SendWait('{ENTER}') }
    }
    'screenshot' {
        $rect = $windows[0].Current.BoundingRectangle
        $bitmap = [Drawing.Bitmap]::new([int]$rect.Width, [int]$rect.Height)
        $graphics = [Drawing.Graphics]::FromImage($bitmap)
        try {
            $graphics.CopyFromScreen([int]$rect.X, [int]$rect.Y, 0, 0, $bitmap.Size)
            $bitmap.Save($First, [Drawing.Imaging.ImageFormat]::Png)
        } finally {
            $graphics.Dispose()
            $bitmap.Dispose()
        }
    }
}
