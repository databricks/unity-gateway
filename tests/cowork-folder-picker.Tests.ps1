$ErrorActionPreference = 'Stop'
$errors = $null
$script = Join-Path $PSScriptRoot '../scripts/cowork-inference/test-task.ps1'
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path $script), [ref]$null, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
# Load the pure readiness predicate without starting Desktop or creating a task.
foreach ($node in $ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $true)) {
    if ($node.Name -eq 'Test-FolderAddress') { Invoke-Expression $node.Extent.Text }
}
$expected = 'C:\Users\runneradmin\Documents\CoworkProbe'
$cases = @(
    @{ name = "Address: $expected"; value = ''; ready = $true },
    @{ name = 'Address: c:/users/runneradmin/documents/CoworkProbe/'; value = ''; ready = $true },
    @{ name = 'Address: CoworkProbe'; value = $expected; ready = $true },
    @{ name = 'Address: runneradmin'; value = ''; ready = $false },
    @{ name = 'Address: CoworkProbe'; value = ''; ready = $false },
    @{ name = 'Address: C:\Other\CoworkProbe'; value = ''; ready = $false },
    @{ name = "Address: ${expected}Old"; value = ''; ready = $false },
    @{ name = 'Address'; value = $expected; ready = $false },
    @{ name = 'Search Box'; value = $expected; ready = $false }
)
foreach ($case in $cases) {
    $actual = Test-FolderAddress -Name $case.name -Value $case.value -ExpectedPath $expected
    if ($actual -ne $case.ready) { throw "Unexpected folder readiness for $($case.name): $actual" }
}
Write-Output 'PASS: folder readiness requires a completed navigation to the exact target path'
