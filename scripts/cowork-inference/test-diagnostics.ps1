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
        $stream = [IO.File]::OpenRead($vmLog)
        try {
            $offset = $stream.Length
            $prefixHash = Get-CoworkLogPrefixHash $stream $offset
        } finally { $stream.Dispose() }
        if (Test-CoworkVmReady -LogPath $vmLog -Offset $offset -PrefixHash $prefixHash) { throw 'An earlier VM startup is not current readiness' }
        Add-Content -LiteralPath $vmLog -Value '[VM:steps] download_and_sdk_prepare started' -Encoding utf8
        if (Test-CoworkVmReady -LogPath $vmLog -Offset $offset -PrefixHash $prefixHash) { throw 'An unfinished VM startup is not ready' }
        Add-Content -LiteralPath $vmLog -Value '[VM:start] Startup complete, total time: 100ms' -Encoding utf8
        if (-not (Test-CoworkVmReady -LogPath $vmLog -Offset $offset -PrefixHash $prefixHash)) { throw 'Completed VM startup must be ready' }
        Set-Content -LiteralPath $vmLog -Value '[VM:start] Startup complete' -Encoding utf8
        if (-not (Test-CoworkVmReady -LogPath $vmLog -Offset $offset -PrefixHash $prefixHash)) { throw 'A rotated log must still show readiness' }
        Set-Content -LiteralPath $vmLog -Value ('[VM:start] Startup complete' + ('x' * 200)) -Encoding utf8
        if (-not (Test-CoworkVmReady -LogPath $vmLog -Offset $offset -PrefixHash $prefixHash)) { throw 'A longer replacement must still show readiness' }
    } finally { Remove-Item -LiteralPath $vmLog -Force }
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
