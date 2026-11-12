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
                (& $Predicate $control)) {
                return $control
            }
        }
        Start-Sleep -Milliseconds 500
    } until ([DateTime]::UtcNow -ge $deadline)
    throw "Timed out after $TimeoutSeconds seconds waiting for enabled control: $Description"
}

$script:ModelLabelSuffixes = @('', ' Extended', ' Low', ' Medium', ' High', ' Max')
$script:ModelOptionTypes = @('ControlType.RadioButton', 'ControlType.MenuItem', 'ControlType.ListItem')

function Test-SelectedModel {
    param([string]$Name, [string]$Label)
    foreach ($suffix in $script:ModelLabelSuffixes) {
        if ($Name -ceq "Model: $Label$suffix") { return $true }
    }
    return $false
}

function Select-CoworkModel {
    param([Windows.Automation.AutomationElement]$Root, [string]$Label)
    $picker = Wait-ModelPickerControl -Root $Root -Description 'Model: *' -Predicate {
        param($control) $control.Current.Name.StartsWith('Model: ', [StringComparison]::Ordinal)
    }
    $initial = $picker.Current.Name
    # Desktop starts on its own default model, which is not the target for every provider service.
    if (-not (Test-SelectedModel -Name $initial -Label $Label)) {
        Invoke-ModelPickerControl $picker
        # Options are named "<label> <description>", e.g. "Claude Haiku 4.5 Fastest for quick answers".
        $option = Wait-ModelPickerControl -Root $Root -Description "model option $Label" -Predicate {
            param($control)
            $name = $control.Current.Name
            ($script:ModelOptionTypes -contains $control.Current.ControlType.ProgrammaticName) -and
                ($name -ceq $Label -or $name.StartsWith("$Label ", [StringComparison]::Ordinal))
        }
        Invoke-ModelPickerControl $option
    }
    $null = Wait-ModelPickerControl -Root $Root -Description "Model: $Label" -Predicate {
        param($control) Test-SelectedModel -Name $control.Current.Name -Label $Label
    }
    return $initial
}
