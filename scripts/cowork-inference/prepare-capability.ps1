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
        'mcp' {
            Invoke-Ug -Arguments @('mcp', 'add', '--agents', 'claude', '--workspace', $env:UCODE_TEST_WORKSPACE, '--names', 'system.ai.web_search') -LogName 'mcp-setup.txt'
            $profile = Get-Content (Join-Path $env:LOCALAPPDATA 'Claude-3p/configLibrary/5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6.json') -Raw | ConvertFrom-Json
            $servers = @($profile.managedMcpServers | Where-Object { ($_.args -join ' ') -match 'system.ai.web_search' })
            if ($servers.Count -ne 1) { throw 'Expected one configured web_search connector' }
            $task.server = $servers[0].name
            $task.prompt = "Use the $($task.server) MCP connector's web_search tool once to search for Databricks official website. Do not use built-in web search. Then reply with the official website URL returned by that tool."
            $task.expected = 'databricks.com'
        }
        'skill' {
            Invoke-Ug -Arguments @('skills', 'add', '--names', 'ug_e2e.skills.fixture-summary') -LogName 'skill-download.txt'
            Invoke-Ug -Arguments @('configure', '--profile', 'desktop-ci', '--agents', 'claude', '--user', '--disable-databricks-ai-tools', '--skip-upgrade') -LogName 'skill-configure.txt'
            $references = @(Get-ChildItem (Join-Path $env:USERPROFILE '.ucode/claude-cowork-marketplace') -Recurse -Filter 'fixture-facts.md')
            if (-not $references.Count) { throw 'Downloaded fixture skill is absent from the generated Cowork plugin' }
            $match = [regex]::Match((Get-Content $references[0].FullName -Raw), 'ugsum-[a-f0-9]+')
            if (-not $match.Success) { throw 'Expected skill reference marker not found' }
            $task.expected = $match.Value
            $task.prompt = 'Use the fixture-summary skill from the ug plugin and reply with only the one-line summary it produces. Read its reference file as instructed. Do not use subagents.'
            $task.skillReference = $references[0].FullName
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
