$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/diagnostics.ps1"
. "$PSScriptRoot/prepare-task.ps1"

$logPath = [IO.Path]::GetTempFileName()
$python = (Get-Command python -ErrorAction Stop).Source
try {
    $arguments = @('-c', 'import sys; print("{\"outcome\":\"success\"}"); print("harmless warning", file=sys.stderr)', '--json')
    $result = Invoke-CoworkPluginCommand -Executable $python -Arguments $arguments -LogPath $logPath
    if ($result.outcome -ne 'success') { throw 'Stderr warning prevented parsing successful JSON' }
    if ((Get-Content $logPath -Raw) -notmatch 'harmless warning') { throw 'Stderr missing from diagnostics' }

    foreach ($code in @(
        'import sys; print("{}"); sys.exit(1)',
        'print("{\"outcome\":\"failed\"}")',
        'print("invalid JSON")'
    )) {
        $rejected = $false
        try {
            Invoke-CoworkPluginCommand -Executable $python -Arguments @('-c', $code, '--json') -LogPath $logPath | Out-Null
        } catch { $rejected = $true }
        if (-not $rejected) { throw 'Plugin command failure was accepted' }
    }

    $pluginId = 'cowork-probe@cowork-probe-marketplace'
    foreach ($payload in @(
        '[{"id":"cowork-probe@cowork-probe-marketplace","enabled":true,"installPath":"fixture"}]',
        '{"plugins":[{"pluginId":"cowork-probe@cowork-probe-marketplace","enabled":true,"installPath":"fixture"}]}',
        '{"result":{"plugins":[{"name":"cowork-probe","marketplace":"cowork-probe-marketplace","enabled":true,"installPath":"fixture"}]}}'
    )) {
        $installedPlugins = @(Find-CoworkPlugin -Payload ($payload | ConvertFrom-Json) -PluginId $pluginId)
        if ($installedPlugins.Count -ne 1 -or $installedPlugins[0].installPath -ne 'fixture' -or -not $installedPlugins[0].enabled) {
            throw 'Could not locate plugin installation in native CLI JSON'
        }
    }
    $unrelated = '{"plugins":[{"id":"another-plugin","enabled":true}]}' | ConvertFrom-Json
    if (@(Find-CoworkPlugin -Payload $unrelated -PluginId $pluginId).Count) {
        throw 'Unrelated plugin matched the fixture'
    }
} finally {
    Remove-Item -LiteralPath $logPath
}
