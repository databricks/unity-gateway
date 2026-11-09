$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/diagnostics.ps1"
$savedBearer = $env:DATABRICKS_BEARER
try {
    $env:DATABRICKS_BEARER = 'known-fixture-token'
    $cases = @(
        @{ input = 'error: known-fixture-token'; expected = 'error: [REDACTED]' },
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
}
