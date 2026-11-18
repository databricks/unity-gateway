param(
  [string]$ProvisionScript = (Join-Path $PSScriptRoot '../../scripts/mdm/windows-provision.ps1')
)
$ErrorActionPreference = 'Stop'
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path $ProvisionScript), [ref]$null, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }

# Load only helpers; never run the installer or modify machine settings in this test
$helpers = @('Assert-Sha256', 'Get-Sha256Sum', 'Update-PathEntry', 'Get-AgentAliases', 'Update-AgentProfile')
foreach ($node in $ast.FindAll({ param($n)
  $n -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $true)) {
  if ($node.Name -in $helpers) { Invoke-Expression $node.Extent.Text }
}
function Assert-Equal($Actual, $Expected) {
  if ($Actual -cne $Expected) { throw "Expected '$Expected', got '$Actual'" }
}
function Assert-Throws($Action, $Message) {
  $caught = $false
  try { & $Action | Out-Null }
  catch {
    if ($_.Exception.Message -notlike "*$Message*") { throw }
    $caught = $true
  }
  if (-not $caught) { throw "Expected an error containing '$Message'" }
}

$dir = Join-Path ([IO.Path]::GetTempPath()) "ug-mdm-test-$([guid]::NewGuid())"
$oldSkip = $env:UG_SKIP_OS_SETTINGS
New-Item -ItemType Directory -Path $dir | Out-Null
try {
  $file = Join-Path $dir 'artifact'
  Set-Content -LiteralPath $file -Value 'test artifact'
  $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
  Assert-Sha256 $file $hash
  Assert-Throws { Assert-Sha256 $file ('0' * 64) } 'Checksum mismatch'
  Assert-Throws { Assert-Sha256 $file '' } 'missing SHA256'
  $sums = Join-Path $dir 'sums'
  Set-Content -LiteralPath $sums -Value @('', "$hash  other.zip", "$hash *artifact.zip")
  Assert-Equal (Get-Sha256Sum $sums 'artifact.zip') $hash
  Assert-Throws { Get-Sha256Sum $sums 'absent.zip' } 'No checksum'
  Write-Output 'PASS: checksum match, mismatch, missing hash and sums lookup'

  $path = '%SystemRoot%\System32;C:\Tools'
  $updated = Update-PathEntry $path 'C:\Program Files\UnityGateway\npm'
  Assert-Equal $updated ($path + ';C:\Program Files\UnityGateway\npm')
  Assert-Equal (Update-PathEntry $updated 'c:\program files\UnityGateway\npm') $updated
  Assert-Equal (Update-PathEntry '' 'C:\Tools') 'C:\Tools'
  Assert-Equal (Update-PathEntry $updated 'C:\Program Files\UnityGateway\npm' -Remove) $path
  Write-Output 'PASS: PATH retains unexpanded entries and avoids duplicates'

  # A local stand-in verifies wrapper argument forwarding and environment restoration
  $fakeUg = Join-Path $dir "ug's test.ps1"
  Set-Content -LiteralPath $fakeUg -Value @'
if ($args -contains '--fail') { throw 'agent failed' }
"$env:UG_SKIP_OS_SETTINGS|$($args -join '|')"
'@
  $block = Get-AgentAliases $fakeUg
  $existing = "function unrelated { 'keep me' }`r`n"
  $profile = Update-AgentProfile $existing $block
  Assert-Equal (Update-AgentProfile $profile $block) $profile
  Assert-Equal (Update-AgentProfile $profile '') ($existing.TrimEnd())
  Assert-Equal ([regex]::Matches($profile, '# BEGIN unity-gateway').Count) 1
  Invoke-Expression $profile
  Assert-Equal (unrelated) 'keep me'
  foreach ($value in @($null, 'original')) {
    foreach ($agent in 'claude', 'codex') {
      $env:UG_SKIP_OS_SETTINGS = $value
      Assert-Equal (& $agent 'two words' '--version') "1|$agent|two words|--version"
      Assert-Equal $env:UG_SKIP_OS_SETTINGS $value
      Assert-Throws { & $agent '--fail' } 'agent failed'
      Assert-Equal $env:UG_SKIP_OS_SETTINGS $value
    }
  }
  Write-Output 'PASS: profiles are idempotent and preserve existing content'
  Write-Output 'PASS: both wrappers forward arguments and restore unset/set environment on success/failure'
} finally {
  $env:UG_SKIP_OS_SETTINGS = $oldSkip
  Remove-Item -Recurse -Force -LiteralPath $dir
}
