function Protect-DiagnosticText {
    param([string]$Text)
    foreach ($secret in @($env:DATABRICKS_BEARER, $env:CLAUDE_CODE_OAUTH_TOKEN, $env:DATABRICKS_CLIENT_SECRET, $env:UG_COWORK_PAT)) {
        if ($secret) { $Text = $Text.Replace($secret, '[REDACTED]') }
    }
    $Text = $Text -replace '\bsk-ant-[A-Za-z0-9_-]+', '[REDACTED]'
    $Text = $Text -replace '\bdapi[0-9a-f]{32}(-\d+)?\b', '[REDACTED]'
    $Text = $Text -replace '(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]'
    $Text = $Text -replace '\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[REDACTED]'
    return $Text
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

function Test-CoworkVmReady {
    param([string]$LogPath, [long]$Offset, [string]$PrefixHash)
    if (-not (Test-Path -LiteralPath $LogPath)) { return $false }
    $stream = [IO.File]::Open($LogPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
    try {
        if ($stream.Length -lt $Offset -or ($Offset -gt 0 -and (Get-CoworkLogPrefixHash $stream $Offset) -ne $PrefixHash)) { $Offset = 0 }
        [void]$stream.Seek($Offset, [IO.SeekOrigin]::Begin)
        $reader = [IO.StreamReader]::new($stream)
        try { return $reader.ReadToEnd().Contains('[VM:start] Startup complete') }
        finally { $reader.Dispose() }
    } finally { $stream.Dispose() }
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
