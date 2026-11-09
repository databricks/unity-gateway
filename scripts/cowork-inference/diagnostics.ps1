function Protect-DiagnosticText {
    param([string]$Text)
    if ($env:DATABRICKS_BEARER) { $Text = $Text.Replace($env:DATABRICKS_BEARER, '[REDACTED]') }
    $Text = $Text -replace '(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]'
    $Text = $Text -replace '\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[REDACTED]'
    return $Text
}
