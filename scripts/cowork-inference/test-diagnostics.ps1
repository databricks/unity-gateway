$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/diagnostics.ps1"
$savedBearer = $env:DATABRICKS_BEARER
$savedOAuthToken = $env:CLAUDE_CODE_OAUTH_TOKEN
$savedClientSecret = $env:DATABRICKS_CLIENT_SECRET
try {
    $env:DATABRICKS_BEARER = 'known-fixture-token'
    $env:CLAUDE_CODE_OAUTH_TOKEN = 'known-oauth-fixture'
    $env:DATABRICKS_CLIENT_SECRET = 'known-client-secret'
    # Built at runtime so the fixture never looks like a committed token.
    $fakePat = 'dapi' + ('0' * 32) + '-2'
    $cases = @(
        @{ input = 'error: known-fixture-token'; expected = 'error: [REDACTED]' },
        @{ input = 'relay known-oauth-fixture'; expected = 'relay [REDACTED]' },
        @{ input = 'secret known-client-secret'; expected = 'secret [REDACTED]' },
        @{ input = 'token sk-ant-oat01-unrelated_token'; expected = 'token [REDACTED]' },
        @{ input = "pat $fakePat end"; expected = 'pat [REDACTED] end' },
        @{ input = 'Authorization: Bearer unrelated-token'; expected = 'Authorization: Bearer [REDACTED]' },
        @{ input = 'token=eyJhbGci.eyJzdWI.signature'; expected = 'token=[REDACTED]' },
        @{ input = 'callback ?code=synthetic-code&state=synthetic-state'; expected = 'callback ?code=[REDACTED]&state=[REDACTED]' },
        @{ input = 'Claude responded: 48213'; expected = 'Claude responded: 48213' },
        @{ input = ''; expected = '' }
    )
    foreach ($case in $cases) {
        if ((Protect-DiagnosticText $case.input) -cne $case.expected) {
            throw 'Diagnostic redaction assertion failed'
        }
    }
    $env:DATABRICKS_BEARER = 'overlap'
    $env:DATABRICKS_CLIENT_SECRET = 'overlap-long"\\secret'
    $value = Protect-DiagnosticText "prefix $($env:DATABRICKS_CLIENT_SECRET) suffix"
    $encoded = @{ value = $value } | ConvertTo-Json
    if ($value -cne 'prefix [REDACTED] suffix' -or $encoded -match 'overlap|secret') {
        throw 'UI values must redact overlapping credentials before JSON serialization'
    }
    $escaped = @{ authorization = $env:DATABRICKS_CLIENT_SECRET } | ConvertTo-Json -Compress
    if ((Protect-DiagnosticText $escaped) -match 'overlap|secret') {
        throw 'JSON-escaped credentials must be redacted from captured transcripts'
    }
    $env:DATABRICKS_BEARER = 'known-fixture-token'
    $responseError = 'Claude responded: API Error: Request rejected (429)'
    if ((Get-CoworkResponseError @('Start task', "$responseError known-fixture-token")) -cne "$responseError [REDACTED]") {
        throw 'Cowork API error must retain the provider status and redact credentials'
    }
    foreach ($name in @('Claude responded: 48213', 'You said: Claude responded: API Error: 429', 'API Error: 429')) {
        if ($null -ne (Get-CoworkResponseError @($name))) {
            throw 'Only an assistant API error may stop inference'
        }
    }
    if ($null -ne (Get-CoworkResponseError @())) {
        throw 'An empty conversation has no API error'
    }
    $vmLog = [IO.Path]::GetTempFileName()
    try {
        Set-Content -LiteralPath $vmLog -Value '[VM:start] Startup complete, total time: 1ms' -Encoding utf8
        $checkpoint = Get-CoworkLogCheckpoint -LogPath $vmLog
        $offset = $checkpoint.offset
        $prefixHash = $checkpoint.prefixHash
        if ($offset -le 0 -or -not $prefixHash) { throw 'Existing VM log checkpoint must preserve its prefix' }
        if (Test-CoworkVmReady -LogPath $vmLog -Offset $offset -PrefixHash $prefixHash) { throw 'An earlier VM startup is not current readiness' }
        Add-Content -LiteralPath $vmLog -Value '[VM:steps] download_and_sdk_prepare started' -Encoding utf8
        if (Test-CoworkVmReady -LogPath $vmLog -Offset $offset -PrefixHash $prefixHash) { throw 'An unfinished VM startup is not ready' }
        Add-Content -LiteralPath $vmLog -Value '[startVM] VM already connected' -Encoding utf8
        if (-not (Test-CoworkVmReady -LogPath $vmLog -Offset $offset -PrefixHash $prefixHash)) { throw 'A fresh VM reconnect must be ready' }
        Add-Content -LiteralPath $vmLog -Value '[VM:start] Startup complete, total time: 100ms' -Encoding utf8
        if (-not (Test-CoworkVmReady -LogPath $vmLog -Offset $offset -PrefixHash $prefixHash)) { throw 'Completed VM startup must be ready' }
        Set-Content -LiteralPath $vmLog -Value '[VM:start] Startup complete' -Encoding utf8
        if (-not (Test-CoworkVmReady -LogPath $vmLog -Offset $offset -PrefixHash $prefixHash)) { throw 'A rotated log must still show readiness' }
        Set-Content -LiteralPath $vmLog -Value ('[VM:start] Startup complete' + ('x' * 200)) -Encoding utf8
        if (-not (Test-CoworkVmReady -LogPath $vmLog -Offset $offset -PrefixHash $prefixHash)) { throw 'A longer replacement must still show readiness' }
    } finally { Remove-Item -LiteralPath $vmLog -Force }
    $sourceLog = [IO.Path]::GetTempFileName()
    $savedLog = [IO.Path]::GetTempFileName()
    try {
        [IO.File]::WriteAllText($sourceLog, "startup known-fixture-token`nready`n")
        Save-CoworkLogTail -LogPath $sourceLog -OutputPath $savedLog
        $captured = [IO.File]::ReadAllText($savedLog)
        if ($captured -notmatch 'startup \[REDACTED\]' -or $captured -notmatch 'ready' -or $captured -match 'known-fixture-token') {
            throw 'VM log evidence must preserve state while redacting credentials'
        }
    } finally { Remove-Item -LiteralPath $sourceLog, $savedLog -Force }
    $proxyRoot = Join-Path ([IO.Path]::GetTempPath()) ('cowork-proxy-' + [Guid]::NewGuid().ToString())
    $proxyDir = Join-Path $proxyRoot 'session'
    New-Item -ItemType Directory -Path $proxyDir -Force | Out-Null
    try {
        foreach ($name in @('backend.log', 'transport.log', 'transport.log.1', 'unrelated.log')) {
            Set-Content -LiteralPath (Join-Path $proxyDir $name) -Value $name
        }
        $names = @((Get-CoworkProxyLogs -Root $proxyRoot | ForEach-Object Name) | Sort-Object)
        if (($names -join ',') -cne 'backend.log,transport.log,transport.log.1') {
            throw 'Proxy evidence must retain backend, current transport and one rotated log only'
        }
    } finally { Remove-Item -LiteralPath $proxyRoot -Recurse -Force }
    $raceLog = [IO.Path]::GetTempFileName()
    function Test-Path {
        param([string]$LiteralPath)
        if ($LiteralPath -eq $raceLog) {
            Remove-Item -LiteralPath $LiteralPath -Force
            return $true
        }
        Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath
    }
    try {
        if (Test-CoworkVmReady -LogPath $raceLog -Offset 0 -PrefixHash '') {
            throw 'A disappearing VM log is not ready'
        }
        [IO.File]::WriteAllText($raceLog, 'old')
        $checkpoint = Get-CoworkLogCheckpoint -LogPath $raceLog
        if ($checkpoint.offset -ne 0 -or $checkpoint.prefixHash -ne '') {
            throw 'A disappearing VM log must use an empty checkpoint'
        }
    } finally {
        Remove-Item Function:\Test-Path
        Remove-Item -LiteralPath $raceLog -Force -ErrorAction SilentlyContinue
    }
    $lockedLog = [IO.Path]::GetTempFileName()
    $exclusive = [IO.File]::Open($lockedLog, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    try {
        if (Test-CoworkVmReady -LogPath $lockedLog -Offset 0 -PrefixHash '') {
            throw 'An exclusively locked VM log is not ready'
        }
    } finally { $exclusive.Dispose(); Remove-Item -LiteralPath $lockedLog -Force }
    foreach ($events in @(
        @('ready', 'permission'),
        @('permission', 'ready')
    )) {
        $script:eventIndex = 0
        $script:readyObserved = $false
        Wait-CoworkVmReady -Deadline ([DateTime]::UtcNow.AddSeconds(5)) -IsReady {
            $event = $events[$script:eventIndex++]
            if ($event -eq 'ready') { $script:readyObserved = $true }
            return $script:readyObserved
        }
        if (-not $script:readyObserved) { throw 'Permission cannot be granted before VM readiness' }
        if ($events[0] -eq 'permission' -and $script:eventIndex -ne 2) {
            throw 'An early permission prompt must not release the readiness wait'
        }
    }
    $expiredReleased = $false
    try {
        Wait-CoworkVmReady -Deadline ([DateTime]::UtcNow.AddSeconds(-1)) -IsReady { $true }
        $expiredReleased = $true
    } catch {
        if ($_.Exception.Message -notmatch 'task deadline') { throw }
    }
    if ($expiredReleased) { throw 'VM readiness cannot release an expired task deadline' }
    $script:approvalCount = 0
    try {
        Invoke-CoworkToolPermission -VmReady $false -Approve { $script:approvalCount++ }
        throw 'Permission approval before readiness must fail'
    } catch {
        if ($_.Exception.Message -notmatch 'VM.*ready') { throw }
    }
    if ($script:approvalCount -ne 0) { throw 'An early prompt must never be approved' }
    $script:readyObserved = $false
    Wait-CoworkVmReady -Deadline ([DateTime]::UtcNow.AddSeconds(3)) -IsReady { $script:readyObserved = $true; return $true }
    Invoke-CoworkToolPermission -VmReady $script:readyObserved -Approve { $script:approvalCount++ }
    if ($script:approvalCount -ne 1) { throw 'A ready VM must permit exactly one approval' }
    $script:classReads = 0
    $controls = @(
        [pscustomobject]@{ Name = 'Project'; AutomationId = ''; NativeWindowHandle = $null; ControlType = [pscustomobject]@{ ProgrammaticName = 'ControlType.Pane' }; IsEnabled = $true; IsOffscreen = $false; HasKeyboardFocus = $false },
        [pscustomobject]@{ Name = 'Empty'; AutomationId = '1152'; NativeWindowHandle = $null; ControlType = [pscustomobject]@{ ProgrammaticName = 'ControlType.Pane' }; IsEnabled = $true; IsOffscreen = $false; HasKeyboardFocus = $false },
        [pscustomobject]@{ Name = 'Folder'; AutomationId = '1152'; NativeWindowHandle = 66102; ControlType = [pscustomobject]@{ ProgrammaticName = 'ControlType.Pane' }; IsEnabled = $true; IsOffscreen = $false; HasKeyboardFocus = $true }
    )
    $rows = @($controls | ForEach-Object {
        Convert-CoworkUiControl -Current $_ -Value '' -GetClass { param($Handle) $script:classReads++; 'Edit' }
    })
    if ($rows.Count -ne 3 -or $rows[0].ContainsKey('nativeHandle') -or $rows[1].ContainsKey('nativeHandle') -or
        $rows[2].nativeHandle -ne 66102 -or $rows[2].handleClass -cne 'Edit' -or $script:classReads -ne 1) {
        throw 'UI snapshots must tolerate null handles and classify only a real folder field'
    }
    $stale = Convert-CoworkUiControl -Current $controls[2] -Value '' -GetClass {
        param($Handle) throw [ComponentModel.Win32Exception]::new(1400)
    }
    if ($stale.nativeHandle -ne 66102 -or $stale.handleClassError -ne 1400) {
        throw 'A stale folder handle must remain visible in the UI snapshot'
    }
    if (-not ('Windows.Automation.ElementNotAvailableException' -as [type])) {
        if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
            Add-Type -AssemblyName UIAutomationClient
        } else {
            Add-Type 'namespace Windows.Automation { public class ElementNotAvailableException : System.Exception {} }'
        }
    }
    $script:uiaReads = 0
    $script:uiaWrites = 0
    $uiaField = Set-CoworkFolderInput -ReadEdit {
        $script:uiaReads++
        if ($script:uiaReads -eq 1) { throw [Windows.Automation.ElementNotAvailableException]::new() }
        return @{ id = '1152'; type = 'ControlType.Pane'; enabled = $true; handle = 123; class = 'Edit' }
    } -WriteAndRead { param($State, $Value, $Deadline) $script:uiaWrites++; return $Value } `
        -Folder '/tmp/CoworkProbe' -Deadline ([DateTime]::UtcNow.AddSeconds(3))
    if ($script:uiaReads -ne 2 -or $script:uiaWrites -ne 1 -or $uiaField.value -ne '/tmp/CoworkProbe') {
        throw 'A stale UI Automation element must be re-observed before writing once'
    }
    $script:staleReads = 0
    $script:staleWrites = 0
    $field = Set-CoworkFolderInput -ReadEdit {
        $script:staleReads++
        if ($script:staleReads -eq 1) { throw [ComponentModel.Win32Exception]::new(1400) }
        return @{ id = '1152'; type = 'ControlType.Pane'; enabled = $true; handle = 123; class = 'Edit' }
    } -WriteAndRead { param($State, $Value, $Deadline) $script:staleWrites++; return $Value } `
        -Folder '/tmp/CoworkProbe' -Deadline ([DateTime]::UtcNow.AddSeconds(3))
    if ($script:staleReads -ne 2 -or $script:staleWrites -ne 1 -or $field.value -ne '/tmp/CoworkProbe') {
        throw 'A destroyed handle must be re-observed before writing the folder once'
    }
    try {
        Set-CoworkFolderInput -ReadEdit { throw [ComponentModel.Win32Exception]::new(5) } `
            -WriteAndRead { param($State, $Value, $Deadline) return $Value } `
            -Folder '/tmp/CoworkProbe' -Deadline ([DateTime]::UtcNow.AddSeconds(3)) | Out-Null
        throw 'Unrelated Win32 errors must not be hidden'
    } catch [ComponentModel.Win32Exception] {
        if ($_.Exception.NativeErrorCode -ne 5) { throw }
    }
    $script:failedWrites = 0
    try {
        Set-CoworkFolderInput -ReadEdit { @{ id = '1152'; type = 'ControlType.Pane'; enabled = $true; handle = 123; class = 'Edit' } } `
            -WriteAndRead { param($State, $Value, $Deadline) $script:failedWrites++; throw [ComponentModel.Win32Exception]::new(1400) } `
            -Folder '/tmp/CoworkProbe' -Deadline ([DateTime]::UtcNow.AddSeconds(3)) | Out-Null
        throw 'A failed write must not be repeated'
    } catch [ComponentModel.Win32Exception] {
        if ($_.Exception.NativeErrorCode -ne 1400) { throw }
    }
    if ($script:failedWrites -ne 1) { throw 'A stale handle during a write must fail after one write' }
    $script:pickerReads = 0
    $script:fieldWrites = 0
    $picker = {
        $script:pickerReads++
        if ($script:pickerReads -eq 1) { return @{ id = '1152'; type = 'ControlType.Pane'; enabled = $false; handle = 0; class = '' } }
        return @{ id = '1152'; type = 'ControlType.Pane'; enabled = $true; handle = 123; class = 'Edit' }
    }
    $writeAndRead = { param($State, $Value, $Deadline) $script:fieldWrites++; return $Value }
    $field = Set-CoworkFolderInput -ReadEdit $picker -WriteAndRead $writeAndRead -Folder '/tmp/CoworkProbe' `
        -Deadline ([DateTime]::UtcNow.AddSeconds(3))
    if ($script:pickerReads -ne 2 -or $script:fieldWrites -ne 1 -or $field.id -cne '1152' -or $field.value -cne '/tmp/CoworkProbe') {
        throw 'Folder field was not written exactly once after becoming ready'
    }
    $script:fieldWrites = 0
    try {
        Set-CoworkFolderInput -ReadEdit { @{ id = '1152'; type = 'ControlType.Pane'; enabled = $true; handle = 123; class = 'Edit' } } `
            -WriteAndRead { param($State, $Value, $Deadline) $script:fieldWrites++; return 'runneradmin' } `
            -Folder '/tmp/CoworkProbe' -Deadline ([DateTime]::UtcNow.AddSeconds(1)) | Out-Null
        throw 'A stale folder field must fail'
    } catch {
        if ($_.Exception.Message -notmatch 'did not retain') { throw }
    }
    if ($script:fieldWrites -ne 1) { throw 'A failed folder-field write must not be repeated' }
    $script:fieldWrites = 0
    try {
        Set-CoworkFolderInput -ReadEdit { @{ id = '1152'; type = 'ControlType.Pane'; enabled = $true; handle = 123; class = 'Button' } } `
            -WriteAndRead $writeAndRead -Folder '/tmp/CoworkProbe' -Deadline ([DateTime]::UtcNow.AddSeconds(1)) | Out-Null
        throw 'A non-Edit handle must fail'
    } catch {
        if ($_.Exception.Message -notmatch 'class Button') { throw }
    }
    if ($script:fieldWrites -ne 0) { throw 'A non-Edit handle must never receive the path' }
    $selected = $false
    try {
        Set-CoworkFolderInput -ReadEdit { @{ id = '1152'; type = 'ControlType.Pane'; enabled = $true; handle = $null; class = '' } } `
            -WriteAndRead $writeAndRead -Folder '/tmp/CoworkProbe' -Deadline ([DateTime]::UtcNow) | Out-Null
        $selected = $true
    } catch {
        if ($_.Exception.Message -notmatch '1152') { throw }
    }
    if ($selected) { throw 'An unavailable folder field must not be selected' }
    $script:buttonReads = 0
    $button = Wait-CoworkFolderSelect -Deadline ([DateTime]::UtcNow.AddSeconds(3)) -ReadPicker {
        $script:buttonReads++
        if ($script:buttonReads -eq 1) { return @{ address = 'Address: runneradmin'; button = $null } }
        return @{ address = 'Address: runneradmin'; button = 'select-folder' }
    }
    if ($button -ne 'select-folder' -or $script:buttonReads -ne 2) { throw 'Select Folder readiness failed' }
    try {
        Wait-CoworkFolderSelect -Deadline ([DateTime]::UtcNow) -ReadPicker {
            @{ address = 'Address: runneradmin'; button = $null }
        } | Out-Null
        throw 'A missing Select Folder button must fail'
    } catch {
        if ($_.Exception.Message -notmatch 'Address: runneradmin') { throw }
    }
} finally {
    $env:DATABRICKS_BEARER = $savedBearer
    $env:CLAUDE_CODE_OAUTH_TOKEN = $savedOAuthToken
    $env:DATABRICKS_CLIENT_SECRET = $savedClientSecret
}
