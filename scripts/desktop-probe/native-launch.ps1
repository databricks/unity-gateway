param([Parameter(Mandatory)][string]$OutputDirectory)

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Run on a disposable GitHub Actions runner' }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
Add-Type -AssemblyName System.Windows.Forms, System.Drawing, UIAutomationClient, UIAutomationTypes
$report = [ordered]@{}
$process = $null
try {
    $process = Start-Process -FilePath $env:CLAUDE_DESKTOP_EXECUTABLE -PassThru `
        -RedirectStandardOutput (Join-Path $OutputDirectory 'native-stdout.log') `
        -RedirectStandardError (Join-Path $OutputDirectory 'native-stderr.log')
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Seconds 1
        $windows = @(Get-Process -Name Claude -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowHandle -ne 0 })
    } until ($windows.Count -or [DateTime]::UtcNow -ge $deadline)
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
    $bounds = [Windows.Forms.Screen]::PrimaryScreen.Bounds
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
    $report.error = $_.Exception.Message
} finally {
    Get-Process -Name Claude -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    $report | ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath (Join-Path $OutputDirectory 'native-launch.json') -Encoding utf8
}
