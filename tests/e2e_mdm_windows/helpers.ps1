function Assert-Mdm($Condition, [string]$Message) {
  if (-not $Condition) { throw $Message }
  Write-Output "PASS: $Message"
}
function Protect-Output([string]$Text) {
  foreach ($secret in $script:Secrets) {
    if ($secret) { $Text = $Text.Replace($secret, '[redacted]') }
  }
  [regex]::Replace($Text, 'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}', '[redacted]')
}
function Read-PipeLine($Reader, [int]$Seconds) {
  $pending = $Reader.ReadLineAsync()
  if (-not $pending.Wait($Seconds * 1000)) { throw 'Scheduled task communication timed out.' }
  if ($null -eq $pending.Result) { throw 'Scheduled task closed its pipe without a result.' }
  $pending.Result | ConvertFrom-Json
}
function Stop-MdmWorker([int]$ProcessId) {
  if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return }
  $start = New-Object Diagnostics.ProcessStartInfo
  $start.FileName = "$env:SystemRoot\System32\taskkill.exe"
  $start.Arguments = "/PID $ProcessId /T /F"
  $start.UseShellExecute = $false
  $start.CreateNoWindow = $true
  $start.RedirectStandardOutput = $true
  $start.RedirectStandardError = $true
  $killer = New-Object Diagnostics.Process
  $killer.StartInfo = $start
  try {
    $null = $killer.Start()
    $stdout = $killer.StandardOutput.ReadToEndAsync()
    $stderr = $killer.StandardError.ReadToEndAsync()
    if (-not $killer.WaitForExit(10000)) { $killer.Kill(); throw 'taskkill timed out.' }
    # A completed task can exit between the initial process lookup and taskkill.
    if ($killer.ExitCode -ne 0 -and (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
      throw "Could not terminate worker $ProcessId."
    }
  } finally { $killer.Dispose() }
}
function Invoke-MdmTask($Machine, [string]$Identity, [string]$Command,
    [hashtable]$Environment = @{}, [int]$Timeout = 120, [switch]$LoadProfile) {
  $name = 'ug-mdm-' + [guid]::NewGuid().ToString('N')
  $security = New-Object IO.Pipes.PipeSecurity
  $allowed = @('S-1-5-18', 'S-1-5-32-544')
  if ($Identity -ne 'SYSTEM') { $allowed += $Machine.UserSid }
  foreach ($sid in $allowed) {
    $identitySid = New-Object Security.Principal.SecurityIdentifier($sid)
    $rights = if ($sid -eq $Machine.UserSid) { 'ReadWrite, Synchronize' } else { 'FullControl' }
    $security.AddAccessRule((New-Object IO.Pipes.PipeAccessRule($identitySid, $rights, 'Allow')))
  }
  $pipe = New-Object IO.Pipes.NamedPipeServerStream($name, 'InOut', 1, 'Byte', 'Asynchronous', 65536, 65536, $security)
  $reader = $null
  $writer = $null
  $workerPid = $null
  try {
    $exe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $action = New-ScheduledTaskAction -Execute $exe -Argument (
      '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $Machine.Worker + '" -PipeName ' + $name)
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds ($Timeout + 90))
    if ($Identity -eq 'SYSTEM') {
      $principal = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -LogonType ServiceAccount -RunLevel Highest
      Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Settings $settings | Out-Null
    } else {
      Register-ScheduledTask -TaskName $name -Action $action -Settings $settings `
        -User "$env:COMPUTERNAME\$($Machine.User)" -Password $Machine.Password | Out-Null
    }
    $connection = $pipe.WaitForConnectionAsync()
    Start-ScheduledTask -TaskName $name
    if (-not $connection.Wait(60000)) { throw 'Scheduled task never connected to its restricted pipe.' }
    $reader = New-Object IO.StreamReader($pipe)
    $writer = New-Object IO.StreamWriter($pipe)
    $writer.AutoFlush = $true
    $hello = Read-PipeLine $reader 30
    $workerPid = [int][MdmWindows]::PipeClient($pipe.SafePipeHandle)
    if ($workerPid -ne [int]$hello.pid) { throw 'Pipe client PID does not match its handshake.' }
    $owner = Invoke-CimMethod -InputObject (Get-CimInstance Win32_Process -Filter "ProcessId=$workerPid") -MethodName GetOwnerSid
    if ($owner.ReturnValue -ne 0 -or $owner.Sid -ne $hello.sid) { throw 'Pipe client identity could not be verified.' }
    if ($Identity -eq 'SYSTEM') {
      if ($owner.Sid -ne 'S-1-5-18') { throw 'Provisioning task did not run as SYSTEM.' }
    } elseif ($owner.Sid -ne $Machine.UserSid -or $hello.admin) {
      throw 'User task is not the expected standard user.'
    }
    $arguments = '-NonInteractive -EncodedCommand ' + [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command))
    if (-not $LoadProfile) { $arguments = '-NoProfile -ExecutionPolicy Bypass ' + $arguments }
    $taskSecrets = @($Environment.UG_MDM_INPUT_CLIENT_SECRET, $Environment.DATABRICKS_BEARER)
    if ($Environment.UG_MDM_INPUT_CLIENT_SECRET) {
      $taskSecrets += [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(
        "$($Environment.UG_MDM_INPUT_CLIENT_ID):$($Environment.UG_MDM_INPUT_CLIENT_SECRET)"))
    }
    $writer.WriteLine((@{
      arguments = $arguments; environment = $Environment; directory = $Machine.Directory
      timeout = $Timeout; secrets = $taskSecrets
    } | ConvertTo-Json -Depth 5 -Compress))
    $result = Read-PipeLine $reader ($Timeout + 30)
    $result | Add-Member -NotePropertyName home -NotePropertyValue $hello.home
    return $result
  } finally {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    try { if ($workerPid) { Stop-MdmWorker $workerPid } }
    finally {
      Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
      if ($writer) { $writer.Dispose() }
      if ($reader) { $reader.Dispose() }
      $pipe.Dispose()
    }
  }
}
function Get-MdmEntries([string]$Root) {
  $pending = New-Object 'Collections.Generic.Stack[string]'
  $pending.Push($Root)
  while ($pending.Count) {
    $item = Get-Item -LiteralPath $pending.Pop() -Force
    $item
    if ($item.PSIsContainer -and -not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      foreach ($child in Get-ChildItem -LiteralPath $item.FullName -Force) { $pending.Push($child.FullName) }
    }
  }
}
function Get-MdmSnapshot([string[]]$Roots) {
  $snapshot = @{}
  foreach ($root in $Roots) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    foreach ($item in Get-MdmEntries $root) {
      $row = @($item.Attributes.ToString(), $item.LastWriteTimeUtc.Ticks, (Get-Acl -LiteralPath $item.FullName).Sddl)
      if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        $row += @($item.Target) -join ';'
      } elseif (-not $item.PSIsContainer) {
        $row += @($item.Length, (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash)
      }
      $snapshot[$item.FullName] = $row -join '|'
    }
  }
  return $snapshot
}
function Assert-MdmSnapshot($Before, $After) {
  $changed = @($Before.Keys | Where-Object { -not $After.ContainsKey($_) -or $Before[$_] -cne $After[$_] })
  $added = @($After.Keys | Where-Object { -not $Before.ContainsKey($_) })
  Assert-Mdm ($changed.Count -eq 0 -and $added.Count -eq 0) (
    "machine files, hashes and ACLs are unchanged (changed=$($changed.Count), added=$($added.Count))")
}
function Assert-MdmProtected([string[]]$Roots) {
  $untrusted = @('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545', 'S-1-5-32-546')
  $write = [Security.AccessControl.FileSystemRights]'Write,Delete,ChangePermissions,TakeOwnership,DeleteSubdirectoriesAndFiles'
  foreach ($root in $Roots) {
    if (-not (Test-Path -LiteralPath $root)) { throw "Missing machine path: $root" }
    foreach ($item in Get-MdmEntries $root) {
      if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        foreach ($target in $item.Target) {
          $resolved = if ([IO.Path]::IsPathRooted($target)) { [IO.Path]::GetFullPath($target) }
            else { [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $item.FullName) $target)) }
          if (-not $resolved.StartsWith($root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Machine junction escapes its protected root: $($item.FullName)"
          }
        }
      }
      $acl = Get-Acl -LiteralPath $item.FullName
      if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin 'S-1-5-18', 'S-1-5-32-544') {
        throw "Machine path is not owned by SYSTEM/Administrators: $($item.FullName)"
      }
      foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.IdentityReference.Value -in $untrusted -and $rule.AccessControlType -eq 'Allow' -and
            -not ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -and
            ($rule.FileSystemRights -band $write)) { throw "Users can change machine path: $($item.FullName)" }
      }
    }
  }
  Write-Output 'PASS: machine-wide paths have protected owners and ACLs'
}
function Find-MdmCredentialLeak([string[]]$Roots) {
  foreach ($root in $Roots) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    $files = @(Get-MdmEntries $root | Where-Object {
      -not $_.PSIsContainer -and -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint)
    })
    foreach ($file in $files) {
      $text = [IO.File]::ReadAllText($file.FullName)
      foreach ($secret in $script:Secrets) {
        if ($secret -and $text.Contains($secret)) { return $file.FullName }
      }
      if ($text -match 'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}') { return $file.FullName }
    }
  }
}
