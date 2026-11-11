param(
    [Parameter(Mandatory)][string]$TaskPath,
    [Parameter(Mandatory)][string]$Model,
    [Parameter(Mandatory)][string]$Executable
)

$ErrorActionPreference = 'Stop'
$task = Get-Content -LiteralPath $TaskPath -Raw | ConvertFrom-Json
& $Executable -p $task.prompt --model $Model --output-format stream-json --verbose --allowedTools 'Skill,Read'
exit $LASTEXITCODE
