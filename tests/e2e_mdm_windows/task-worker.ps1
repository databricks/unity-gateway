param([Parameter(Mandatory=$true)][string]$PipeName)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Closing this task's job kills every descendant, including an agent that outlives its launcher.
Add-Type @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class MdmProcessJob {
    [StructLayout(LayoutKind.Sequential)] struct Basic {
        public long ProcessTime, JobTime;
        public uint Flags;
        public UIntPtr MinWorkingSet, MaxWorkingSet;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct Io {
        public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes;
    }
    [StructLayout(LayoutKind.Sequential)] struct Limits {
        public Basic Basic;
        public Io Io;
        public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr a, string n);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetInformationJobObject(IntPtr j, int c, ref Limits l, uint s);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr j, IntPtr p);
    static IntPtr job;
    public static void Attach() {
        job = CreateJobObject(IntPtr.Zero, null);
        var limits = new Limits();
        limits.Basic.Flags = 0x2000;
        if (job == IntPtr.Zero || !SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(limits)) ||
            !AssignProcessToJobObject(job, System.Diagnostics.Process.GetCurrentProcess().Handle))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        // Keep the handle open for the worker lifetime. Windows closes it when the task exits.
    }
}
'@

$pipe = New-Object IO.Pipes.NamedPipeClientStream('.', $PipeName, [IO.Pipes.PipeDirection]::InOut)
$reader = $null
$writer = $null
$secrets = @()
function Protect-Output([string]$Text) {
  foreach ($secret in $secrets) { if ($secret) { $Text = $Text.Replace($secret, '[redacted]') } }
  [regex]::Replace($Text, 'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}', '[redacted]')
}
try {
  $pipe.Connect(30000)
  $reader = New-Object IO.StreamReader($pipe)
  $writer = New-Object IO.StreamWriter($pipe)
  $writer.AutoFlush = $true
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = New-Object Security.Principal.WindowsPrincipal($identity)
  $writer.WriteLine((@{ pid = $PID; sid = $identity.User.Value; admin = $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator); home = $env:USERPROFILE } | ConvertTo-Json -Compress))
  $request = $reader.ReadLine() | ConvertFrom-Json
  $secrets = @($request.secrets)
  [MdmProcessJob]::Attach()

  $start = New-Object Diagnostics.ProcessStartInfo
  $start.FileName = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
  $start.Arguments = $request.arguments
  $start.WorkingDirectory = $request.directory
  $start.UseShellExecute = $false
  $start.CreateNoWindow = $true
  $start.RedirectStandardInput = $true
  $start.RedirectStandardOutput = $true
  $start.RedirectStandardError = $true
  $start.EnvironmentVariables.Clear()
  foreach ($name in @('SystemRoot', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA',
      'LOCALAPPDATA', 'HOMEDRIVE', 'HOMEPATH', 'ProgramData', 'ProgramFiles', 'ProgramW6432',
      'ProgramFiles(x86)', 'PATHEXT', 'OS', 'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS',
      'SystemDrive', 'USERNAME', 'USERDOMAIN', 'COMPUTERNAME', 'ALLUSERSPROFILE', 'PUBLIC',
      'CommonProgramFiles', 'CommonProgramFiles(x86)', 'CommonProgramW6432', 'PSModulePath')) {
    if (Test-Path "Env:$name") { $start.EnvironmentVariables[$name] = (Get-Item "Env:$name").Value }
  }
  $start.EnvironmentVariables['Path'] = [Environment]::GetEnvironmentVariable('Path', 'Machine')
  foreach ($property in $request.environment.PSObject.Properties) {
    $start.EnvironmentVariables[$property.Name] = [string]$property.Value
  }
  $process = New-Object Diagnostics.Process
  $process.StartInfo = $start
  if (-not $process.Start()) { throw 'Child process did not start.' }
  $process.StandardInput.Close()
  $stdout = $process.StandardOutput.ReadToEndAsync()
  $stderr = $process.StandardError.ReadToEndAsync()
  $deadline = [DateTime]::UtcNow.AddSeconds($request.timeout)
  while (-not ($process.HasExited -and $stdout.IsCompleted -and $stderr.IsCompleted)) {
    if ([DateTime]::UtcNow -ge $deadline) { throw 'Child process timed out; terminating task job.' }
    Start-Sleep -Milliseconds 100
  }
  $writer.WriteLine((@{
    code = $process.ExitCode
    stdout = Protect-Output $stdout.Result
    stderr = Protect-Output $stderr.Result
  } | ConvertTo-Json -Compress))
} catch {
  if ($writer) {
    $writer.WriteLine((@{ code = -1; stdout = ''; stderr = Protect-Output $_.Exception.Message } | ConvertTo-Json -Compress))
  }
  exit 1
} finally {
  $secrets = @()
  if ($writer) { $writer.Dispose() }
  if ($reader) { $reader.Dispose() }
  $pipe.Dispose()
}
