function Invoke-CoworkPluginCommand {
    param([string]$Executable, [string[]]$Arguments, [string]$LogPath)
    $stderrPath = [IO.Path]::GetTempFileName()
    try {
        $output = (& $Executable @Arguments 2> $stderrPath | Out-String)
        $code = $LASTEXITCODE
        $stderr = Get-Content -LiteralPath $stderrPath -Raw
        Protect-DiagnosticText "$output`n$stderr" | Add-Content -LiteralPath $LogPath
        if ($code -ne 0) { throw 'Native Cowork plugin command failed; see skill-install.txt' }
        if ($Arguments -contains '--json') {
            $result = $output | ConvertFrom-Json
            if ($result.outcome -and $result.outcome -notin @('ok', 'success')) {
                throw 'Native plugin command reported failure; see skill-install.txt'
            }
            return $result
        }
    } finally {
        Remove-Item -LiteralPath $stderrPath
    }
}

function Find-CoworkPlugin {
    param($Payload, [string]$PluginId)
    if ($Payload -is [array]) {
        foreach ($entry in $Payload) { Find-CoworkPlugin -Payload $entry -PluginId $PluginId }
        return
    }
    if ($Payload -isnot [pscustomobject]) { return }
    $name, $marketplace = $PluginId -split '@', 2
    if ($Payload.id -eq $PluginId -or $Payload.pluginId -eq $PluginId -or
        ($Payload.name -eq $name -and $Payload.marketplace -eq $marketplace)) {
        return $Payload
    }
    foreach ($property in $Payload.PSObject.Properties) {
        Find-CoworkPlugin -Payload $property.Value -PluginId $PluginId
    }
}

function New-CoworkTask {
    param([string]$Capability, [string]$EvidenceDirectory)
    $task = [ordered]@{ capability = $Capability }
    $evidence = $EvidenceDirectory
    $previousConfigDirectory = $env:CLAUDE_CONFIG_DIR
    try {
        switch ($Capability) {
        'file' {
            $folder = Join-Path $env:USERPROFILE 'Documents/CoworkProbe'
            New-Item -ItemType Directory -Path $folder -Force | Out-Null
            $task.expected = 'proof-' + [Guid]::NewGuid().ToString('N').Substring(0, 12)
            Set-Content -LiteralPath (Join-Path $folder 'input.txt') -Value $task.expected -NoNewline
            $task.folder = $folder
            $task.expectedFile = Join-Path $folder 'input.txt'
            $task.prompt = 'Read input.txt in the selected local folder using a file-reading tool. Reply with only its contents. Do not guess the contents.'
        }
        'skill' {
            $marketplace = Join-Path $env:RUNNER_TEMP 'cowork-probe-marketplace'
            $plugin = Join-Path $marketplace 'plugins/cowork-probe'
            $skill = Join-Path $plugin 'skills/probe-summary'
            foreach ($directory in @("$marketplace/.claude-plugin", "$plugin/.claude-plugin", "$skill/references")) {
                New-Item -ItemType Directory -Path $directory -Force | Out-Null
            }
            @{
                name = 'cowork-probe-marketplace'
                owner = @{ name = 'Unity Gateway CI' }
                plugins = @(@{ name = 'cowork-probe'; source = './plugins/cowork-probe'; description = 'Native Cowork skill test fixture' })
            } | ConvertTo-Json -Depth 6 | Set-Content "$marketplace/.claude-plugin/marketplace.json"
            @{ name = 'cowork-probe'; version = '1.0.0'; author = @{ name = 'Unity Gateway CI' }; description = 'Native Cowork skill test fixture' } |
                ConvertTo-Json | Set-Content "$plugin/.claude-plugin/plugin.json"
            @'
---
name: probe-summary
description: Use when asked to produce the capability probe summary.
---
Read references/facts.txt beside this skill. Reply with only its contents, unchanged.
Do not guess the contents and do not use subagents.
'@ | Set-Content "$skill/SKILL.md"
            $task.expected = 'skill-proof-' + [Guid]::NewGuid().ToString('N').Substring(0, 12)
            Set-Content -LiteralPath "$skill/references/facts.txt" -Value $task.expected -NoNewline
            $sessions = Join-Path $env:LOCALAPPDATA 'Claude-3p/local-agent-mode-sessions'
            $roots = @(Get-ChildItem $sessions -Directory | ForEach-Object { Get-ChildItem $_.FullName -Directory } |
                Where-Object { Test-Path (Join-Path $_.FullName 'cowork_account_settings.json') })
            if ($roots.Count -ne 1) { throw 'Expected one native Cowork account directory after baseline inference' }
            $env:CLAUDE_CONFIG_DIR = $roots[0].FullName
            $claude = (Get-Command claude.cmd -ErrorAction Stop).Source
            $commands = @(
                @('plugin', 'validate', $marketplace),
                @('plugin', 'marketplace', 'add', $marketplace, '--cowork', '--json'),
                @('plugin', 'install', 'cowork-probe@cowork-probe-marketplace', '--cowork', '--json'),
                @('plugin', 'list', '--cowork', '--json')
            )
            foreach ($arguments in $commands) {
                $result = Invoke-CoworkPluginCommand -Executable $claude -Arguments $arguments `
                    -LogPath (Join-Path $evidence 'skill-install.txt')
            }
            $installed = @(Find-CoworkPlugin -Payload $result -PluginId 'cowork-probe@cowork-probe-marketplace' |
                Where-Object { $_.enabled -eq $true -and $_.installPath })
            if ($installed.Count -ne 1) { throw 'Expected one enabled native skill plugin' }
            $task.expectedFile = Join-Path $installed[0].installPath 'skills/probe-summary/references/facts.txt'
            $task.skill = 'cowork-probe:probe-summary'
            $task.prompt = 'Use the probe-summary skill from the cowork-probe plugin to produce the capability probe summary. Reply with only its result. Do not use subagents.'
        }
        }
    } finally {
        $env:CLAUDE_CONFIG_DIR = $previousConfigDirectory
    }
    return $task
}
