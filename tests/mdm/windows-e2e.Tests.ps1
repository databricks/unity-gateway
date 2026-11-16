$ErrorActionPreference = 'Stop'
$root = Join-Path $PSScriptRoot '../e2e_mdm_windows'
foreach ($file in Get-ChildItem -LiteralPath $root -Filter '*.ps1') {
  $errors = $null
  [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$null, [ref]$errors) | Out-Null
  if ($errors.Count) { throw ($errors | Out-String) }
}
. "$root/helpers.ps1"
function Expect-Error($Action, $Message) {
  $caught = $false
  try { & $Action | Out-Null }
  catch {
    if ($_.Exception.Message -notlike "*$Message*") { throw }
    $caught = $true
  }
  if (-not $caught) { throw "Expected failure: $Message" }
}

$script:Secrets = @('fixture' + '-secret')
if ((Protect-Output ('prefix fixture' + '-secret suffix')) -ne 'prefix [redacted] suffix') { throw 'Secret was not scrubbed.' }
$token = 'eyJ' + ('x' * 12) + '.' + ('y' * 15) + '.' + ('z' * 15)
if ((Protect-Output $token) -ne '[redacted]') { throw 'JWT was not scrubbed.' }
Write-Output 'PASS: secret and JWT scrubbing'

Assert-MdmSnapshot @{ a = 'one'; b = 'two' } @{ a = 'one'; b = 'two' }
Expect-Error { Assert-MdmSnapshot @{ a = 'one' } @{ a = 'changed' } } 'machine files'
Expect-Error { Assert-MdmSnapshot @{ a = 'one' } @{} } 'machine files'
Expect-Error { Assert-MdmSnapshot @{} @{ a = 'new' } } 'machine files'
Write-Output 'PASS: snapshot comparisons reject changed, deleted and added paths'
Stop-MdmWorker ([int]::MaxValue)
Write-Output 'PASS: worker cleanup tolerates an already-exited process'

$dir = Join-Path ([IO.Path]::GetTempPath()) ('ug-e2e-helpers-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $dir | Out-Null
try {
  $file = Join-Path $dir 'state.txt'
  Set-Content -LiteralPath $file -Value 'safe content'
  if (Find-MdmCredentialLeak @($dir)) { throw 'Clean file reported as credential leak.' }
  Set-Content -LiteralPath $file -Value $script:Secrets[0]
  if ((Find-MdmCredentialLeak @($dir)) -ne $file) { throw 'Secret leak not detected.' }
  Set-Content -LiteralPath $file -Value $token
  if ((Find-MdmCredentialLeak @($dir)) -ne $file) { throw 'Token leak not detected.' }
  Write-Output 'PASS: credential scan accepts clean content and rejects secrets/tokens'

  $target = Join-Path $dir 'target'
  New-Item -ItemType Directory -Path $target | Out-Null
  Set-Content -LiteralPath (Join-Path $target 'child') -Value 'data'
  $link = Join-Path $dir 'link'
  $linkType = if ($env:OS -eq 'Windows_NT') { 'Junction' } else { 'SymbolicLink' }
  New-Item -ItemType $linkType -Path $link -Target $target | Out-Null
  $entries = @(Get-MdmEntries $dir)
  if ($link -notin $entries.FullName -or (Join-Path $link 'child') -in $entries.FullName) {
    throw 'Snapshot traversal follows a directory reparse point.'
  }
  Write-Output 'PASS: snapshot traversal records links without following them'

  $memory = New-Object IO.MemoryStream
  $bytes = [Text.Encoding]::UTF8.GetBytes('{"code":0}' + "`n")
  $memory.Write($bytes, 0, $bytes.Length)
  $memory.Position = 0
  $reader = New-Object IO.StreamReader($memory)
  if ((Read-PipeLine $reader 1).code -ne 0) { throw 'Failed to read pipe JSON.' }
  Expect-Error { Read-PipeLine $reader 1 } 'closed its pipe'
  $reader.Dispose()
  Write-Output 'PASS: pipe JSON and premature EOF handling'

  # Compile the worker's native job declarations without calling Windows APIs.
  foreach ($name in 'task-worker.ps1', 'native.ps1') {
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
      (Join-Path $root $name), [ref]$null, [ref]$null)
    $addType = $ast.Find({ param($node)
      $node -is [System.Management.Automation.Language.CommandAst] -and $node.GetCommandName() -eq 'Add-Type'
    }, $true)
    Add-Type $addType.CommandElements[1].Value
  }
  Write-Output 'PASS: native job-object, pipe-client and batch-logon declarations compile'
} finally {
  Remove-Item -Recurse -Force -LiteralPath $dir
  $script:Secrets = @()
}
