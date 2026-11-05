param([Parameter(Mandatory)][string]$OutputDirectory, [switch]$Configured)

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Run on a disposable GitHub Actions runner' }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
Add-Type -AssemblyName System.Windows.Forms, System.Drawing, UIAutomationClient, UIAutomationTypes
$report = [ordered]@{}
$process = $null
$nativeLogDirectory = Join-Path $env:RUNNER_TEMP ('claude-native-' + [Guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $nativeLogDirectory | Out-Null
try {
    $process = Start-Process -FilePath $env:CLAUDE_DESKTOP_EXECUTABLE -ArgumentList '--force-renderer-accessibility' -PassThru `
        -RedirectStandardOutput (Join-Path $nativeLogDirectory 'native-stdout.log') `
        -RedirectStandardError (Join-Path $nativeLogDirectory 'native-stderr.log')
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Seconds 1
        $windows = @(Get-Process -Name Claude -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowHandle -ne 0 })
    } until ($windows.Count -or [DateTime]::UtcNow -ge $deadline)
    if (-not $windows.Count) { throw 'Claude did not open a window' }
    $window = [Windows.Automation.AutomationElement]::FromHandle($windows[0].MainWindowHandle)
    $condition = [Windows.Automation.PropertyCondition]::new(
        [Windows.Automation.AutomationElement]::NameProperty, 'Get started')
    $deadline = [DateTime]::UtcNow.AddSeconds($(if ($Configured) { 15 } else { 45 }))
    do {
        $startButton = $window.FindFirst([Windows.Automation.TreeScope]::Descendants, $condition)
        if (-not $startButton) { Start-Sleep -Seconds 1 }
    } until ($startButton -or [DateTime]::UtcNow -ge $deadline)
    $report.getStartedFound = $null -ne $startButton
    if ($startButton) {
        $invoke = $startButton.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern)
        $invoke.Invoke()
        $report.getStartedInvoked = $true
        Start-Sleep -Seconds 8
    }
    if ($Configured) {
        $continueCondition = [Windows.Automation.PropertyCondition]::new(
            [Windows.Automation.AutomationElement]::NameProperty, 'Continue')
        $continueButton = $window.FindFirst([Windows.Automation.TreeScope]::Descendants, $continueCondition)
        if (-not $continueButton) { throw 'Configured gateway welcome screen has no Continue button' }
        $continueButton.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
        $report.gatewayContinueInvoked = $true
        Start-Sleep -Seconds 15
        $coworkCondition = [Windows.Automation.PropertyCondition]::new(
            [Windows.Automation.AutomationElement]::NameProperty, 'Cowork')
        $cowork = $window.FindFirst([Windows.Automation.TreeScope]::Descendants, $coworkCondition)
        $report.coworkControlFound = $null -ne $cowork
        if (-not $cowork) { throw 'Cowork control is unavailable' }
        if ($cowork) {
            $pattern = $null
            if ($cowork.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
                $pattern.Invoke()
            } elseif ($cowork.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) {
                $pattern.Select()
            } else {
                throw 'Cowork control has no supported native activation pattern'
            }
            $report.coworkActivated = $true
            Start-Sleep -Seconds 10
        }
        $configuration = Get-Content (Join-Path $OutputDirectory 'configuration.json') -Raw | ConvertFrom-Json
        $modelCondition = [Windows.Automation.PropertyCondition]::new(
            [Windows.Automation.AutomationElement]::NameProperty, "Model: $($configuration.targetModelLabel)")
        if (-not $window.FindFirst([Windows.Automation.TreeScope]::Descendants, $modelCondition)) {
            throw 'Cowork did not select the expected system.ai Claude model'
        }
        $report.model = $configuration.targetModel
        $editorCondition = [Windows.Automation.PropertyCondition]::new(
            [Windows.Automation.AutomationElement]::NameProperty, 'Write your prompt to Claude')
        $editor = $window.FindFirst([Windows.Automation.TreeScope]::Descendants, $editorCondition)
        if (-not $editor) { throw 'Cowork prompt editor is unavailable' }
        $prompt = 'Reply with only the digits for forty-eight thousand two hundred thirteen. Do not use tools.'
        $valuePattern = $null
        if ($editor.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern, [ref]$valuePattern)) {
            $valuePattern.SetValue($prompt)
        } else {
            $editor.SetFocus()
            [Windows.Forms.SendKeys]::SendWait($prompt)
        }
        Start-Sleep -Seconds 1
        $submitCondition = [Windows.Automation.PropertyCondition]::new(
            [Windows.Automation.AutomationElement]::NameProperty, 'Start task')
        $submit = $window.FindFirst([Windows.Automation.TreeScope]::Descendants, $submitCondition)
        if (-not $submit -or -not $submit.Current.IsEnabled) { throw 'Cowork Start task is unavailable or disabled' }
        $submit.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
        $report.promptSubmitted = $true
        $deadline = [DateTime]::UtcNow.AddSeconds(240)
        do {
            Start-Sleep -Seconds 2
            $paneCondition = [Windows.Automation.PropertyCondition]::new(
                [Windows.Automation.AutomationElement]::NameProperty, 'Primary pane')
            $pane = $window.FindFirst([Windows.Automation.TreeScope]::Descendants, $paneCondition)
            if (-not $pane) { continue }
            $responseCondition = [Windows.Automation.PropertyCondition]::new(
                [Windows.Automation.AutomationElement]::NameProperty, 'Claude responded: 48213')
            $finishedCondition = [Windows.Automation.PropertyCondition]::new(
                [Windows.Automation.AutomationElement]::NameProperty, 'Claude finished the response')
            $answer = $pane.FindFirst([Windows.Automation.TreeScope]::Descendants, $responseCondition)
            $finished = $window.FindFirst([Windows.Automation.TreeScope]::Descendants, $finishedCondition)
            if ($answer -and $finished) {
                $report.responseAsserted = $true
                $report.responseFinished = $true
                $report.expectedResponse = '48213'
                break
            }
        } until ([DateTime]::UtcNow -ge $deadline)
        if (-not $report.responseAsserted) { throw 'Cowork did not show the expected response within 240 seconds' }
    }
} catch {
    $report.error = $_.Exception.Message
} finally {
    try {
        $report.processExited = $process.HasExited
        if ($process.HasExited) { $report.exitCode = $process.ExitCode }
        $report.processes = @(Get-Process -Name Claude -ErrorAction SilentlyContinue |
            Select-Object Id, MainWindowTitle, @{Name='hasWindow'; Expression={$_.MainWindowHandle -ne 0}})
        $report.windows = @($windows | ForEach-Object {
            $element = [Windows.Automation.AutomationElement]::FromHandle($_.MainWindowHandle)
            $controls = $element.FindAll([Windows.Automation.TreeScope]::Descendants,
                [Windows.Automation.Condition]::TrueCondition)
            @{
                title = $_.MainWindowTitle
                controls = @($controls | Select-Object -First 150 | ForEach-Object {
                    @{ name = $_.Current.Name; type = $_.Current.ControlType.ProgrammaticName }
                })
            }
        })
        $rect = $window.Current.BoundingRectangle
        $bounds = [Drawing.Rectangle]::new([int]$rect.X, [int]$rect.Y, [int]$rect.Width, [int]$rect.Height)
        $bitmap = [Drawing.Bitmap]::new($bounds.Width, $bounds.Height)
        $graphics = [Drawing.Graphics]::FromImage($bitmap)
        try {
            $graphics.CopyFromScreen($bounds.Location, [Drawing.Point]::Empty, $bounds.Size)
            $bitmap.Save((Join-Path $OutputDirectory 'native-launch.png'), [Drawing.Imaging.ImageFormat]::Png)
        } finally {
            $graphics.Dispose()
            $bitmap.Dispose()
        }
    } catch {
        $report.captureError = $_.Exception.Message
    }
    $claudeProcesses = @(Get-Process -Name Claude -ErrorAction SilentlyContinue)
    $claudeProcesses | Stop-Process -Force -ErrorAction SilentlyContinue
    foreach ($claudeProcess in $claudeProcesses) {
        try { [void]$claudeProcess.WaitForExit(10000) } catch { }
    }
    if ($process) { [void]$process.WaitForExit(10000) }
    foreach ($log in Get-ChildItem -LiteralPath $nativeLogDirectory -Filter '*.log') {
        $text = Get-Content -LiteralPath $log.FullName -Raw
        if ($text) {
            if ($env:DATABRICKS_BEARER) { $text = $text.Replace($env:DATABRICKS_BEARER, '[REDACTED]') }
            $text | Set-Content -LiteralPath (Join-Path $OutputDirectory $log.Name) -Encoding utf8
        }
    }
    $json = $report | ConvertTo-Json -Depth 8
    if ($env:DATABRICKS_BEARER) { $json = $json.Replace($env:DATABRICKS_BEARER, '[REDACTED]') }
    $json | Set-Content -LiteralPath (Join-Path $OutputDirectory 'native-launch.json') -Encoding utf8
}
if ($report.error) { exit 1 }
