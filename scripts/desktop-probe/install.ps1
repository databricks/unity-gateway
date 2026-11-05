param(
    [Parameter(Mandatory)][string]$InstallerPath,
    [Parameter(Mandatory)][ValidatePattern('^[a-fA-F0-9]{64}$')][string]$ExpectedSha256,
    [Parameter(Mandatory)][string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Run on a disposable GitHub Actions runner' }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$report = [ordered]@{ installed = $false }
try {
    $hash = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash
    if ($hash -ne $ExpectedSha256) { throw 'Installer SHA256 does not match the pinned digest' }
    $report.sha256 = $hash
    $signature = Get-AuthenticodeSignature -LiteralPath $InstallerPath
    $report.signature = $signature.Status.ToString()
    $report.signer = $signature.SignerCertificate.Subject
    if ($signature.Status -ne 'Valid' -or $report.signer -notmatch '\bO=Anthropic\b') {
        throw 'Expected a valid Anthropic-signed installer'
    }
    if (@(Get-AppxPackage -Name '*Claude*').Count -ne 0) {
        throw 'Expected a clean runner without Claude installed'
    }

    Add-AppxPackage -Path $InstallerPath -ErrorAction Stop
    $packages = @(Get-AppxPackage -Name '*Claude*')
    if ($packages.Count -ne 1) { throw 'Expected exactly one installed Claude package' }
    $package = $packages[0]
    $report.installed = $true
    $report.package = $package.Name
    $report.version = $package.Version.ToString()
    $manifest = Get-AppxPackageManifest -Package $package.PackageFullName
    $applications = @($manifest.Package.Applications.Application |
        Where-Object { $_.Executable -match '(^|[\\/])claude\.exe$' })
    if ($applications.Count -ne 1) { throw 'Cannot identify the Claude application executable' }
    $executable = Join-Path $package.InstallLocation $applications[0].Executable
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw 'Claude executable is missing' }
    "CLAUDE_DESKTOP_EXECUTABLE=$executable" | Add-Content -LiteralPath $env:GITHUB_ENV
} catch {
    $report.error = $_.Exception.Message
    throw
} finally {
    $report | ConvertTo-Json -Depth 5 |
        Set-Content -LiteralPath (Join-Path $OutputDirectory 'installation.json') -Encoding utf8
}
