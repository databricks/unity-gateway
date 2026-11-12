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
    $fixture = Get-Content -LiteralPath "$PSScriptRoot/task-fixture.json" -Raw | ConvertFrom-Json
    $task = [ordered]@{
        capability = $Capability
        expected = "$Capability-proof-" + [Guid]::NewGuid().ToString('N').Substring(0, 12)
        prompt = $fixture.prompts.$Capability
    }
    $evidence = $EvidenceDirectory
    $previousConfigDirectory = $env:CLAUDE_CONFIG_DIR
    try {
        switch ($Capability) {
        'file' {
            $folder = Join-Path $env:USERPROFILE 'Documents/CoworkProbe'
            New-Item -ItemType Directory -Path $folder -Force | Out-Null
            Set-Content -LiteralPath (Join-Path $folder 'input.txt') -Value $task.expected -NoNewline
            $task.folder = $folder
            $task.expectedFile = Join-Path $folder 'input.txt'
        }
        'skill' {
            $marketplace = Join-Path $env:RUNNER_TEMP $fixture.marketplace.name
            $skill = Join-Path $marketplace $fixture.skillDirectory
            $plugin = Split-Path (Split-Path $skill -Parent) -Parent
            foreach ($directory in @("$marketplace/.claude-plugin", "$plugin/.claude-plugin", "$skill/references")) {
                New-Item -ItemType Directory -Path $directory -Force | Out-Null
            }
            $fixture.marketplace | ConvertTo-Json -Depth 6 | Set-Content "$marketplace/.claude-plugin/marketplace.json"
            $fixture.plugin | ConvertTo-Json -Depth 6 | Set-Content "$plugin/.claude-plugin/plugin.json"
            Set-Content -LiteralPath "$skill/SKILL.md" -Value $fixture.skillInstructions -NoNewline
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
                @('plugin', 'install', $fixture.pluginId, '--cowork', '--json'),
                @('plugin', 'list', '--cowork', '--json')
            )
            foreach ($arguments in $commands) {
                $result = Invoke-CoworkPluginCommand -Executable $claude -Arguments $arguments `
                    -LogPath (Join-Path $evidence 'skill-install.txt')
            }
            $installed = @(Find-CoworkPlugin -Payload $result -PluginId $fixture.pluginId |
                Where-Object { $_.enabled -eq $true -and $_.installPath })
            if ($installed.Count -ne 1) { throw 'Expected one enabled native skill plugin' }
            $task.expectedFile = Join-Path $installed[0].installPath 'skills/probe-summary/references/facts.txt'
            $task.skill = $fixture.skill
        }
        'mcp' {
            $log = Join-Path $evidence 'mcp-add.txt'
            # UG writes its own absolute path into Desktop's connector from PATH, as an installed ug would be.
            $previousPath = $env:PATH
            $env:PATH = "$(Split-Path -Parent $env:UG_COWORK_TEST_BINARY);$previousPath"
            try {
                $output = (& $env:UG_COWORK_TEST_BINARY mcp add --names $fixture.mcpService 2>&1 | Out-String)
                $code = $LASTEXITCODE
            } finally {
                $env:PATH = $previousPath
            }
            Protect-DiagnosticText $output | Set-Content -LiteralPath $log
            if ($code -ne 0) { throw 'ug mcp add failed; see mcp-add.txt' }
            $profilePath = Join-Path $env:LOCALAPPDATA 'Claude-3p/configLibrary/5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6.json'
            $desktopProfile = Get-Content -Raw -LiteralPath $profilePath | ConvertFrom-Json
            $servers = @($desktopProfile.managedMcpServers | Where-Object { $_.name -eq $fixture.mcpServer })
            if ($servers.Count -ne 1) { throw "Desktop profile is missing the $($fixture.mcpServer) connector" }
            $arguments = @($servers[0].args)
            $url = $arguments[[array]::IndexOf($arguments, '--url') + 1]
            $expectedUrl = "$($env:UCODE_TEST_WORKSPACE.TrimEnd('/'))/ai-gateway/mcp-services/$($fixture.mcpService)"
            if ($servers[0].transport -ne 'stdio' -or $url -ne $expectedUrl) {
                throw "The $($fixture.mcpServer) connector does not proxy $($fixture.mcpService)"
            }
            $task.mcpServer = $fixture.mcpServer
            $task.mcpTool = $fixture.mcpTool
        }
        }
    } finally {
        $env:CLAUDE_CONFIG_DIR = $previousConfigDirectory
    }
    return $task
}
