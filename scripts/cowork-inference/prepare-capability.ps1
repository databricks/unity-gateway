$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Use a disposable GitHub runner' }
. "$PSScriptRoot/diagnostics.ps1"
$evidence = Join-Path $env:RUNNER_TEMP 'cowork-capability'
New-Item -ItemType Directory -Path $evidence -Force | Out-Null
$task = [ordered]@{ capability = $env:CAPABILITY }

function Invoke-Ug {
    param([string[]]$Arguments, [string]$LogName)
    $text = (& $env:UG_COWORK_TEST_BINARY @Arguments 2>&1 | Out-String)
    $code = $LASTEXITCODE
    Protect-DiagnosticText $text | Set-Content (Join-Path $evidence $LogName)
    if ($code -ne 0) { throw "UG command failed ($code); see $LogName" }
}

try {
    switch ($env:CAPABILITY) {
        'file' {
            $folder = Join-Path $env:USERPROFILE 'Documents/CoworkProbe'
            New-Item -ItemType Directory -Path $folder -Force | Out-Null
            $task.expected = 'proof-' + [Guid]::NewGuid().ToString('N').Substring(0, 12)
            Set-Content -LiteralPath (Join-Path $folder 'input.txt') -Value $task.expected -NoNewline
            $task.folder = $folder
            $task.prompt = 'Read input.txt in the selected local folder using a file-reading tool. Reply with only its contents. Do not guess the contents.'
        }
        { $_ -in @('mcp', 'mcp-pat') } {
            $task.capability = 'mcp'
            $task.credentialSource = 'environment'
            if ($env:CAPABILITY -eq 'mcp-pat') {
                $config = Join-Path $env:USERPROFILE '.databrickscfg'
                @('[desktop-ci]', "host = $env:UCODE_TEST_WORKSPACE", 'auth_type = pat', "token = $env:DATABRICKS_BEARER") |
                    Set-Content -LiteralPath $config
                Invoke-Ug -Arguments @('configure', '--profile', 'desktop-ci', '--use-pat', '--agents', 'claude', '--user', '--disable-databricks-ai-tools', '--skip-validate', '--skip-upgrade') -LogName 'mcp-profile-setup.txt'
                $task.credentialSource = 'temporary Databricks PAT profile using the existing CI bearer'
            }
            Invoke-Ug -Arguments @('mcp', 'add', '--agents', 'claude', '--workspace', $env:UCODE_TEST_WORKSPACE, '--names', 'system.ai.web_search') -LogName 'mcp-setup.txt'
            $profile = Get-Content (Join-Path $env:LOCALAPPDATA 'Claude-3p/configLibrary/5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6.json') -Raw | ConvertFrom-Json
            $servers = @($profile.managedMcpServers | Where-Object { ($_.args -join ' ') -match 'system.ai.web_search' })
            if ($servers.Count -ne 1) { throw 'Expected one configured web_search connector' }
            $task.server = $servers[0].name
            $task.prompt = "Use the $($task.server) MCP connector's web_search tool once to search for Databricks official website. Do not use built-in web search. Then reply with the official website URL returned by that tool."
            $task.expected = 'databricks.com'
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
                plugins = @(@{ name = 'cowork-probe'; source = './plugins/cowork-probe'; description = 'Disposable native skill capability probe' })
            } | ConvertTo-Json -Depth 6 | Set-Content "$marketplace/.claude-plugin/marketplace.json"
            @{ name = 'cowork-probe'; version = '1.0.0'; description = 'Disposable native skill capability probe' } |
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
                $output = (& $claude @arguments 2>&1 | Out-String)
                $code = $LASTEXITCODE
                Protect-DiagnosticText $output | Add-Content (Join-Path $evidence 'skill-install.txt')
                if ($code -ne 0) { throw 'Native Cowork plugin installation failed; see skill-install.txt' }
                if ($arguments -contains '--json') {
                    $result = $output | ConvertFrom-Json
                    if ($result.outcome -and $result.outcome -notin @('ok', 'success')) {
                        throw 'Native plugin command reported failure; see skill-install.txt'
                    }
                }
            }
            Remove-Item Env:CLAUDE_CONFIG_DIR
            $task.prompt = 'Use the probe-summary skill from the cowork-probe plugin to produce the capability probe summary. Reply with only its result. Do not use subagents.'
            $task.skillSource = 'local plugin installed through native Claude plugin commands'
        }
        default { throw 'Unknown capability' }
    }
    $task | ConvertTo-Json | Set-Content (Join-Path $env:RUNNER_TEMP 'cowork-capability-task.json')
    @{ prepared = $true; capability = $env:CAPABILITY } | ConvertTo-Json | Set-Content (Join-Path $evidence 'setup.json')
} catch {
    @{ prepared = $false; capability = $env:CAPABILITY; error = (Protect-DiagnosticText $_.Exception.Message) } |
        ConvertTo-Json | Set-Content (Join-Path $evidence 'setup.json')
    throw
}
