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
        $offset = (Get-Item -LiteralPath $vmLog).Length
        if (Test-CoworkVmReady -LogPath $vmLog -Offset $offset) { throw 'An earlier VM startup is not current readiness' }
        Add-Content -LiteralPath $vmLog -Value '[VM:steps] download_and_sdk_prepare started' -Encoding utf8
        if (Test-CoworkVmReady -LogPath $vmLog -Offset $offset) { throw 'An unfinished VM startup is not ready' }
        Add-Content -LiteralPath $vmLog -Value '[VM:start] Startup complete, total time: 100ms' -Encoding utf8
        if (-not (Test-CoworkVmReady -LogPath $vmLog -Offset $offset)) { throw 'Completed VM startup must be ready' }
        Set-Content -LiteralPath $vmLog -Value '[VM:start] Startup complete' -Encoding utf8
        if (-not (Test-CoworkVmReady -LogPath $vmLog -Offset $offset)) { throw 'A rotated log must still show readiness' }
    } finally { Remove-Item -LiteralPath $vmLog -Force }
} finally {
    $env:DATABRICKS_BEARER = $savedBearer
    $env:CLAUDE_CODE_OAUTH_TOKEN = $savedOAuthToken
    $env:DATABRICKS_CLIENT_SECRET = $savedClientSecret
}
