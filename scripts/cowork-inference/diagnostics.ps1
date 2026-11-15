function Protect-DiagnosticText {
    param([string]$Text)
    foreach ($secret in @($env:DATABRICKS_BEARER, $env:CLAUDE_CODE_OAUTH_TOKEN, $env:DATABRICKS_CLIENT_SECRET)) {
        if ($secret) { $Text = $Text.Replace($secret, '[REDACTED]') }
    }
    $Text = $Text -replace '\bsk-ant-[A-Za-z0-9_-]+', '[REDACTED]'
    $Text = $Text -replace '(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]'
    $Text = $Text -replace '\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[REDACTED]'
    return $Text
}
