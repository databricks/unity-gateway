$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$source = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PSScriptRoot 'test-task.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$definition = $source.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Wait-Control'
}, $true)
Invoke-Expression $definition.Extent.Text

# UI Automation is the external boundary; execute the real wait and evidence handling.
function Find-Control { param($Names, $Root) return $script:control }
function Save-UI {
    param($Name, $Root)
    if ($Name -ne 'wait-failed' -or -not [object]::ReferenceEquals($Root, $script:expectedRoot)) {
        throw 'Wrong snapshot subtree'
    }
    if ($script:failSnapshot) { throw [InvalidOperationException]::new('Closed UI root') }
    $script:snapshotSaved = $true
}

$script:control = [pscustomobject]@{ Name = 'ready' }
$report = [ordered]@{}
if ((Wait-Control -Names @('ready') -Seconds 0) -ne $script:control -or $report.Contains('waitFailure')) {
    throw 'Ready control did not return without failure evidence'
}
$script:control = $null
$script:expectedRoot = [pscustomobject]@{ Name = 'native folder dialog' }
foreach ($script:failSnapshot in @($false, $true)) {
    $report = [ordered]@{}
    $script:snapshotSaved = $false
    $message = ''
    try { Wait-Control -Names @('missing') -Seconds 0 -Root $script:expectedRoot }
    catch { $message = $_.Exception.Message }
    if ($message -ne 'Control not ready: missing') { throw "Timeout replaced by: $message" }
    $failure = ($report | ConvertTo-Json -Depth 8 | ConvertFrom-Json).waitFailure
    if ($failure.names[0] -ne 'missing' -or $failure.budgetSeconds -ne 0 -or $failure.scans -ne 1 -or
        $failure.elapsedMilliseconds -lt 0 -or $failure.snapshotCompletedMilliseconds -lt $failure.elapsedMilliseconds) {
        throw 'Missing or inconsistent wait evidence'
    }
    if ($script:failSnapshot) {
        if ($failure.snapshotError -ne 'InvalidOperationException') { throw 'Missing snapshot error class' }
    } elseif (-not $script:snapshotSaved) { throw 'Failure snapshot was not saved' }
}
