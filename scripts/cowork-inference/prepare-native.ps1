param(
    [Parameter(Mandatory)][ValidateSet('cli-enterprise', 'code-enterprise', 'cowork-enterprise', 'cowork-org')][string]$Capability,
    [Parameter(Mandatory)][string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Use a disposable GitHub runner' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Native machine-skill tests require elevation'
}
if (@(Get-Process -Name Claude -ErrorAction SilentlyContinue).Count) { throw 'Stop Desktop before preparing native inputs' }
$programFiles = if ($env:ProgramW6432) { $env:ProgramW6432 } else { $env:ProgramFiles }
if (-not $programFiles) { throw 'Native Program Files directory is unavailable' }
$enterpriseRoot = Join-Path $programFiles 'ClaudeCode/.claude/skills'
$orgRoot = Join-Path $programFiles 'Claude/org-plugins'
foreach ($path in @($enterpriseRoot, $orgRoot)) {
    if (Test-Path -LiteralPath $path) { throw "Existing machine content: $path" }
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$root = Join-Path $env:RUNNER_TEMP 'native-contract'
New-Item -ItemType Directory -Path $root | Out-Null
$events = Join-Path $root 'events'
New-Item -ItemType Directory -Path $events | Out-Null
$folder = Join-Path $env:USERPROFILE 'Documents/NativeCodeProbe'
New-Item -ItemType Directory -Path $folder | Out-Null
$task = [ordered]@{
    capability = 'skill'
    nativeCase = $Capability
    expected = 'proof-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
    skill = 'probe-summary'
    folder = $folder
    eventsDirectory = $events
    startedAfter = [DateTime]::UtcNow.ToString('o')
    requireHooks = $Capability -eq 'cowork-org'
}
$task.prompt = 'Case ' + [Guid]::NewGuid().ToString('N').Substring(0, 8) + ': Invoke the probe-summary skill using the Skill tool, then follow its instructions. Reply only with its result. Do not use subagents.'
$skill = Join-Path $enterpriseRoot 'probe-summary'
if ($Capability -eq 'cowork-org') {
    $plugin = Join-Path $orgRoot 'ug-native-contract'
    $skill = Join-Path $plugin 'skills/probe-summary'
    foreach ($directory in @("$plugin/.claude-plugin", "$plugin/hooks")) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    @{
        name = 'ug-native-contract'
        version = '1.0.0'
        installationPreference = 'required'
        author = @{ name = 'Unity Gateway CI' }
        description = 'Administrator-authored native loading contract fixture'
    } | ConvertTo-Json | Set-Content -LiteralPath "$plugin/.claude-plugin/plugin.json" -Encoding utf8
    @{ version = '1.0.0' } | ConvertTo-Json | Set-Content -LiteralPath "$plugin/version.json" -Encoding utf8
    $hook = Join-Path $plugin 'hooks/record.py'
    @'
import json
import sys
import uuid
from pathlib import Path

payload = json.load(sys.stdin)
record = {key: payload.get(key) for key in ("session_id", "hook_event_name")}
Path(sys.argv[1], uuid.uuid4().hex + ".json").write_text(json.dumps(record), encoding="utf-8")
'@ | Set-Content -LiteralPath $hook -Encoding utf8
    $python = Join-Path (Split-Path $env:UG_COWORK_TEST_BINARY -Parent) 'python.exe'
    $command = '"' + $python + '" -I "' + $hook + '" "' + $events + '"'
    @{
        hooks = @{
            SessionStart = @(@{ hooks = @(@{ type = 'command'; command = $command; timeout = 10 }) })
            UserPromptSubmit = @(@{ hooks = @(@{ type = 'command'; command = $command; timeout = 10 }) })
        }
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath "$plugin/hooks/hooks.json" -Encoding utf8
    $task.skill = 'ug-native-contract:probe-summary'
    $task.prompt = $task.prompt.Replace('the probe-summary skill', 'the ug-native-contract:probe-summary skill')
    $task.allowPluginCache = $true
}
New-Item -ItemType Directory -Path "$skill/references" -Force | Out-Null
@'
---
name: probe-summary
description: Read the native loading contract reference when asked for the probe summary.
---
Use the Read tool to read references/facts.txt beside this SKILL.md.
Reply only with its exact contents. Do not guess the contents or use subagents.
'@ | Set-Content -LiteralPath "$skill/SKILL.md" -Encoding utf8
Set-Content -LiteralPath "$skill/references/facts.txt" -Value $task.expected -NoNewline -Encoding utf8
$task.expectedFile = Join-Path $skill 'references/facts.txt'
$task | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'task.json') -Encoding utf8
@{ nativeCase = $Capability; skill = $task.skill; source = $skill; fixture = 'administrator-authored input; not a managed workspace bundle' } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OutputDirectory 'input.json') -Encoding utf8

if ($Capability -in @('cli-enterprise', 'code-enterprise')) {
    & $env:UG_COWORK_TEST_BINARY configure --profile desktop-ci --agents claude --global `
        --disable-databricks-ai-tools --skip-upgrade
    if ($LASTEXITCODE -ne 0) { throw 'Native CLI configuration failed' }
    if (-not (Test-Path -LiteralPath (Join-Path $programFiles 'ClaudeCode/managed-settings.json'))) {
        throw 'UG did not publish native CLI settings'
    }
}
