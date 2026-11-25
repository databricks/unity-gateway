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
    $script:focusedTestEdit = [pscustomobject]@{ Current = [pscustomobject]@{ Value = '' }; Writes = 0 }
    $script:focusedTestEdit | Add-Member -MemberType ScriptMethod -Name SetValue -Value {
        param($Value)
        $this.Writes++
        $this.Current.Value = $Value
    }
    $script:focusReads = 0
    $focusedEdit = {
        $script:focusReads++
        if ($script:focusReads -eq 1) { return $null }
        return $script:focusedTestEdit
    }
    Set-CoworkFolderInput -ReadFocusedEdit $focusedEdit -Folder '/tmp/CoworkProbe' `
        -Deadline ([DateTime]::UtcNow.AddSeconds(3))
    if ($script:focusReads -ne 2 -or $script:focusedTestEdit.Writes -ne 1 -or $script:focusedTestEdit.Current.Value -cne '/tmp/CoworkProbe') {
        throw 'Folder input was not written once after focus'
    }
    $script:staleTestEdit = [pscustomobject]@{ Current = [pscustomobject]@{ Value = 'stale' }; Writes = 0 }
    $script:staleTestEdit | Add-Member -MemberType ScriptMethod -Name SetValue -Value { param($Value) $this.Writes++ }
    try {
        Set-CoworkFolderInput -ReadFocusedEdit { $script:staleTestEdit } -Folder '/tmp/CoworkProbe' `
            -Deadline ([DateTime]::UtcNow.AddSeconds(1))
        throw 'A stale address value must fail'
    } catch {
        if ($_.Exception.Message -notmatch 'did not retain') { throw }
    }
    if ($script:staleTestEdit.Writes -ne 1) { throw 'A failed address write must not be repeated' }
    $script:pickerReads = 0
    $picker = {
        $script:pickerReads++
        if ($script:pickerReads -eq 1) { return @{ address = 'Address: runneradmin'; button = 'wrong' } }
        return @{ address = 'Address: Documents > CoworkProbe'; button = 'ready' }
    }
    $button = Wait-CoworkFolderReady -ReadPicker $picker -ExpectedLeaf 'CoworkProbe' -Deadline ([DateTime]::UtcNow.AddSeconds(3))
    if ($button -ne 'ready' -or $script:pickerReads -ne 2) { throw 'Folder picker selected before navigation completed' }
    $selected = $false
    try {
        Wait-CoworkFolderReady -ReadPicker { @{ address = 'Address: runneradmin'; button = 'wrong' } } `
            -ExpectedLeaf 'CoworkProbe' -Deadline ([DateTime]::UtcNow) | Out-Null
        $selected = $true
    } catch {
        if ($_.Exception.Message -notmatch 'Address: runneradmin') { throw }
    }
    if ($selected) { throw 'A non-navigating picker must not select a folder' }
    try {
        Wait-CoworkFolderReady -ReadPicker { @{ address = ''; button = 'wrong' } } `
            -ExpectedLeaf 'CoworkProbe' -Deadline ([DateTime]::UtcNow) | Out-Null
        throw 'A missing address must not be accepted'
    } catch {
        if ($_.Exception.Message -notmatch '<not observed>') { throw }
    }
} finally {
    $env:DATABRICKS_BEARER = $savedBearer
    $env:CLAUDE_CODE_OAUTH_TOKEN = $savedOAuthToken
    $env:DATABRICKS_CLIENT_SECRET = $savedClientSecret
}
