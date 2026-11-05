param([Parameter(Mandatory)][string]$OutputPath)

$ErrorActionPreference = 'Stop'
$report = [ordered]@{}

function Measure-Capability([string]$Name, [scriptblock]$Read) {
    try {
        $report[$Name] = & $Read
    } catch {
        $report[$Name] = @{ error = $_.Exception.Message }
    }
}

Measure-Capability 'os' {
    Get-CimInstance Win32_OperatingSystem |
        Select-Object Caption, Version, BuildNumber, OSArchitecture, ProductType
}
Measure-Capability 'machine' {
    Get-CimInstance Win32_ComputerSystem |
        Select-Object Manufacturer, Model, HypervisorPresent, TotalPhysicalMemory
}
Measure-Capability 'processors' {
    @(Get-CimInstance Win32_Processor | Select-Object Name,
        VirtualizationFirmwareEnabled, VMMonitorModeExtensions,
        SecondLevelAddressTranslationExtensions)
}
Measure-Capability 'session' {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    @{
        processSessionId = (Get-Process -Id $PID).SessionId
        userInteractive = [Environment]::UserInteractive
        administrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        apartmentState = [Threading.Thread]::CurrentThread.ApartmentState.ToString()
    }
}
foreach ($feature in @('VirtualMachinePlatform', 'Microsoft-Hyper-V-All', 'Microsoft-Hyper-V', 'HypervisorPlatform')) {
    Measure-Capability "feature:$feature" {
        Get-WindowsOptionalFeature -Online -FeatureName $feature |
            Select-Object FeatureName, @{Name='State'; Expression={$_.State.ToString()}}
    }
}
foreach ($service in @('vmcompute', 'hns')) {
    Measure-Capability "service:$service" {
        Get-Service -Name $service |
            Select-Object Name, @{Name='Status'; Expression={$_.Status.ToString()}},
                @{Name='StartType'; Expression={$_.StartType.ToString()}}
    }
}
Measure-Capability 'claudePackages' {
    @(Get-AppxPackage -Name '*Claude*' | Select-Object Name, Version, Architecture)
}
Measure-Capability 'desktop' {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class DesktopProbe {
    [DllImport("user32.dll", SetLastError = true)]
    public static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern bool GetUserObjectInformation(IntPtr handle, int index,
        StringBuilder info, int length, out int needed);
    [DllImport("user32.dll")]
    public static extern bool CloseDesktop(IntPtr handle);
}
'@
    $desktop = [DesktopProbe]::OpenInputDesktop(0, $false, 1)
    if ($desktop -eq [IntPtr]::Zero) {
        throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }
    try {
        $name = [Text.StringBuilder]::new(512)
        $needed = 0
        if (-not [DesktopProbe]::GetUserObjectInformation($desktop, 2, $name, 1024, [ref]$needed)) {
            throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())
        }
        @{ inputDesktopAccessible = $true; name = $name.ToString() }
    } finally {
        [void][DesktopProbe]::CloseDesktop($desktop)
    }
}
Measure-Capability 'testWindow' {
    Add-Type -AssemblyName System.Windows.Forms
    $form = [Windows.Forms.Form]::new()
    try {
        $form.Text = 'Unity Gateway CI desktop capability probe'
        $form.Width = 480
        $form.Height = 160
        $form.Show()
        [Windows.Forms.Application]::DoEvents()
        @{
            visible = $form.Visible
            handleCreated = $form.IsHandleCreated
            screenCount = [Windows.Forms.Screen]::AllScreens.Count
        }
    } finally {
        $form.Close()
        $form.Dispose()
    }
}
Measure-Capability 'nestedVmBoot' {
    Import-Module Hyper-V -ErrorAction Stop
    $vmName = 'ug-desktop-probe-' + [Guid]::NewGuid().ToString('N')
    $created = $false
    try {
        $vm = New-VM -Name $vmName -Generation 2 -MemoryStartupBytes 256MB -NoVHD
        $created = $true
        Start-VM -VM $vm -ErrorAction Stop
        $state = (Get-VM -Name $vmName).State.ToString()
        @{ started = ($state -eq 'Running'); state = $state; guest = 'UEFI only, no disk or network' }
    } finally {
        if ($created) {
            Stop-VM -Name $vmName -TurnOff -Force -ErrorAction SilentlyContinue
            Remove-VM -Name $vmName -Force -ErrorAction Stop
        }
    }
}

$json = $report | ConvertTo-Json -Depth 8
$json | Set-Content -LiteralPath $OutputPath -Encoding utf8
Write-Output $json
if ($env:GITHUB_STEP_SUMMARY) {
    @('## Windows desktop capabilities', '', '```json', $json, '```', '',
      'Diagnostic collection only. This does not establish Claude Desktop or Cowork compatibility.') |
        Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY
}
