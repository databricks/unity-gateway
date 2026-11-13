$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/diagnostics.ps1"
$savedBearer = $env:DATABRICKS_BEARER
$savedOAuthToken = $env:CLAUDE_CODE_OAUTH_TOKEN
$savedClientSecret = $env:DATABRICKS_CLIENT_SECRET
try {
    $env:DATABRICKS_BEARER = 'known-fixture-token'
    $env:CLAUDE_CODE_OAUTH_TOKEN = 'known-oauth-fixture'
    $env:DATABRICKS_CLIENT_SECRET = 'known-client-secret'
    $cases = @(
        @{ input = 'error: known-fixture-token'; expected = 'error: [REDACTED]' },
        @{ input = 'relay known-oauth-fixture'; expected = 'relay [REDACTED]' },
        @{ input = 'secret known-client-secret'; expected = 'secret [REDACTED]' },
        @{ input = 'token sk-ant-oat01-unrelated_token'; expected = 'token [REDACTED]' },
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
} finally {
    $env:DATABRICKS_BEARER = $savedBearer
    $env:CLAUDE_CODE_OAUTH_TOKEN = $savedOAuthToken
    $env:DATABRICKS_CLIENT_SECRET = $savedClientSecret
}
