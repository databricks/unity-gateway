function Protect-DiagnosticText {
    param([string]$Text)
    $secrets = @($env:DATABRICKS_BEARER, $env:CLAUDE_CODE_OAUTH_TOKEN, $env:DATABRICKS_CLIENT_SECRET, $env:UG_COWORK_PAT) |
        Where-Object { $_ } | ForEach-Object {
            $_
            $json = ConvertTo-Json -InputObject $_ -Compress
            $json.Substring(1, $json.Length - 2)
        } | Sort-Object { $_.Length } -Descending
    foreach ($secret in $secrets) { $Text = $Text.Replace($secret, '[REDACTED]') }
    $Text = $Text -replace '\bsk-ant-[A-Za-z0-9_-]+', '[REDACTED]'
    $Text = $Text -replace '\bdapi[0-9a-f]{32}(-\d+)?\b', '[REDACTED]'
    $Text = $Text -replace '(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]'
    $Text = $Text -replace '\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[REDACTED]'
    $Text = $Text -replace '(?i)([?&](?:code|state)=)[^&#\s"''<>]+', '$1[REDACTED]'
    return $Text
}

function Save-CoworkLogTail {
    param([string]$LogPath, [string]$OutputPath)
    if (-not (Test-Path -LiteralPath $LogPath)) { return }
    $text = (Get-Content -LiteralPath $LogPath -Tail 2000) -join "`n"
    Protect-DiagnosticText $text | Set-Content -LiteralPath $OutputPath -Encoding utf8
}

function Get-CoworkProxyLogs {
    param([string]$Root)
    Get-ChildItem $Root -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in @('backend.log', 'transport.log', 'transport.log.1') }
}

function Get-CoworkLogPrefixHash {
    param([IO.Stream]$Stream, [long]$Length)
    [void]$Stream.Seek(0, [IO.SeekOrigin]::Begin)
    $hash = [Security.Cryptography.IncrementalHash]::CreateHash([Security.Cryptography.HashAlgorithmName]::SHA256)
    try {
        $buffer = [byte[]]::new(65536)
        while ($Length -gt 0) {
            $count = $Stream.Read($buffer, 0, [int][Math]::Min($buffer.Length, $Length))
            if ($count -eq 0) { return $null }
            $hash.AppendData($buffer, 0, $count)
            $Length -= $count
        }
        return [Convert]::ToBase64String($hash.GetHashAndReset())
    } finally { $hash.Dispose() }
}

function Get-CoworkLogCheckpoint {
    param([string]$LogPath)
    if (-not (Test-Path -LiteralPath $LogPath)) { return @{ offset = 0; prefixHash = '' } }
    try {
        $stream = [IO.File]::Open($LogPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
    } catch [IO.FileNotFoundException], [IO.DirectoryNotFoundException] {
        return @{ offset = 0; prefixHash = '' }
    }
    try {
        $offset = $stream.Length
        return @{ offset = $offset; prefixHash = Get-CoworkLogPrefixHash $stream $offset }
    }
    finally { $stream.Dispose() }
}

function Test-CoworkVmReady {
    param([string]$LogPath, [long]$Offset, [string]$PrefixHash)
    if (-not (Test-Path -LiteralPath $LogPath)) { return $false }
    try {
        $stream = [IO.File]::Open($LogPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
    } catch [IO.FileNotFoundException], [IO.DirectoryNotFoundException] {
        return $false
    } catch [IO.IOException] {
        # EAGAIN and Windows sharing/lock violations mean another process temporarily owns the file.
        if ($_.Exception.HResult -in @(11, -2147024864, -2147024863)) { return $false }
        throw
    }
    try {
        if ($stream.Length -lt $Offset -or ($Offset -gt 0 -and (Get-CoworkLogPrefixHash $stream $Offset) -ne $PrefixHash)) { $Offset = 0 }
        [void]$stream.Seek($Offset, [IO.SeekOrigin]::Begin)
        $reader = [IO.StreamReader]::new($stream)
        try {
            $text = $reader.ReadToEnd()
            return $text.Contains('[VM:start] Startup complete') -or $text.Contains('[startVM] VM already connected')
        }
        finally { $reader.Dispose() }
    } finally { $stream.Dispose() }
}

function Wait-CoworkVmReady {
    param([DateTime]$Deadline, [scriptblock]$IsReady)
    do {
        if ([DateTime]::UtcNow -ge $Deadline) { throw 'Cowork VM did not become ready within the task deadline' }
        if (& $IsReady) { return }
        Start-Sleep -Milliseconds 500
    } while ($true)
}

function Invoke-CoworkToolPermission {
    param([bool]$VmReady, [scriptblock]$Approve)
    if (-not $VmReady) { throw 'Cowork VM must be ready before granting tool permission' }
    & $Approve
}

function Convert-CoworkUiControl {
    param($Current, $Value, [scriptblock]$GetClass)
    $id = [string]$Current.AutomationId
    $row = @{
        name = Protect-DiagnosticText ([string]$Current.Name)
        type = $Current.ControlType.ProgrammaticName
        id = Protect-DiagnosticText $id
        enabled = $Current.IsEnabled
        offscreen = $Current.IsOffscreen
        focused = $Current.HasKeyboardFocus
        value = if ($null -ne $Value) { Protect-DiagnosticText ([string]$Value) } else { $null }
    }
    if ($id -eq '1152' -and $null -ne $Current.NativeWindowHandle) {
        $handle = [IntPtr]$Current.NativeWindowHandle
        if ($handle -ne [IntPtr]::Zero) {
            $row.nativeHandle = $handle.ToInt64()
            try { $row.handleClass = & $GetClass $handle }
            catch [ComponentModel.Win32Exception] { $row.handleClassError = $_.Exception.NativeErrorCode }
        }
    }
    return $row
}

function Set-CoworkFolderInput {
    param([scriptblock]$ReadEdit, [scriptblock]$WriteAndRead, [string]$Folder, [DateTime]$Deadline)
    do {
        try { $state = & $ReadEdit }
        catch [System.Windows.Automation.ElementNotAvailableException] { $state = $null }
        catch [ComponentModel.Win32Exception] {
            if ($_.Exception.NativeErrorCode -ne 1400) { throw }
            $state = $null
        }
        if ($state -and $state.enabled -and $null -ne $state.handle -and [IntPtr]$state.handle -ne [IntPtr]::Zero -and [DateTime]::UtcNow -lt $Deadline) {
            if ($state.class -cne 'Edit') { throw "Folder field 1152 has class $($state.class), not Edit" }
            $actual = & $WriteAndRead $state $Folder $Deadline
            if ($actual -cne $Folder) {
                throw "Folder field did not retain the requested path; observed: $(Protect-DiagnosticText $actual)"
            }
            $state.value = $actual
            return $state
        }
        if ([DateTime]::UtcNow -ge $Deadline) {
            throw "Folder field 1152 not ready; last type: $($state.type); enabled: $($state.enabled); handle: $($state.handle)"
        }
        Start-Sleep -Milliseconds 500
    } while ($true)
}

function Wait-CoworkFolderSelect {
    param([scriptblock]$ReadPicker, [DateTime]$Deadline)
    do {
        $state = & $ReadPicker
        if ($state.button -and [DateTime]::UtcNow -lt $Deadline) { return $state.button }
        if ([DateTime]::UtcNow -ge $Deadline) {
            $observed = if ($state.address) { $state.address } else { '<not observed>' }
            throw "Select Folder was not ready; last address: $observed"
        }
        Start-Sleep -Milliseconds 500
    } while ($true)
}

function Get-CoworkResponseError {
    param([string[]]$Names)
    foreach ($name in $Names) {
        if ($name.StartsWith('Claude responded: API Error:')) {
            return Protect-DiagnosticText $name
        }
    }
    return $null
}
