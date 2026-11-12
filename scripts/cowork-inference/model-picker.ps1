function Invoke-ModelPickerControl {
    param([Windows.Automation.AutomationElement]$Control)
    $pattern = $null
    if ($Control.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
        $pattern.Invoke()
    } elseif ($Control.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) {
        $pattern.Select()
    } elseif ($Control.TryGetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$pattern)) {
        $pattern.Expand()
    } else {
        throw "Control '$($Control.Current.Name)' has no supported native activation pattern"
    }
}

function Wait-ModelPickerControl {
    param(
        [Windows.Automation.AutomationElement]$Root,
        [scriptblock]$Predicate,
        [string]$Description,
        [int]$TimeoutSeconds = 60
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $controls = $Root.FindAll([Windows.Automation.TreeScope]::Descendants,
            [Windows.Automation.Condition]::TrueCondition)
        foreach ($control in $controls) {
            if ($control.Current.IsEnabled -and -not $control.Current.IsOffscreen -and
                (& $Predicate $control.Current.Name)) {
                return $control
            }
        }
        Start-Sleep -Milliseconds 500
    } until ([DateTime]::UtcNow -ge $deadline)
    throw "Timed out after $TimeoutSeconds seconds waiting for enabled control: $Description"
}

function Select-CoworkModel {
    param([Windows.Automation.AutomationElement]$Root, [string]$Label)
    $target = "Model: $Label"
    $picker = Wait-ModelPickerControl -Root $Root -Description 'Model: *' -Predicate {
        param($name) $name.StartsWith('Model: ', [StringComparison]::Ordinal)
    }
    $initial = $picker.Current.Name
    # Desktop starts on its own default model, which is not the target for every provider service.
    if ($initial -cne $target) {
        Invoke-ModelPickerControl $picker
        Invoke-ModelPickerControl (Wait-ModelPickerControl -Root $Root -Description $Label -Predicate {
            param($name) $name -ceq $Label
        })
    }
    $null = Wait-ModelPickerControl -Root $Root -Description $target -Predicate {
        param($name) $name -ceq $target
    }
    return $initial
}
