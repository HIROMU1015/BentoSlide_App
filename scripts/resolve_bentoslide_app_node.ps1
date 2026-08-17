Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-BentoSlideAppNodeVersion {
    param([Parameter(Mandatory = $true)][string]$VersionText)

    if ($VersionText -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') { return $false }
    $version = [System.Version]$VersionText
    if ($version.Major -eq 22) { return $version -ge [System.Version]'22.22.2' }
    if ($version.Major -eq 24) { return $version -ge [System.Version]'24.15.0' }
    return $version.Major -ge 26
}

function Invoke-BentoSlideAppNpm {
    param(
        [Parameter(Mandatory = $true)]$NodeResolution,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $nodeDirectory = Split-Path -Parent ([string]$NodeResolution.Node)
    $previousPath = $env:Path
    try {
        $env:Path = $nodeDirectory + [System.IO.Path]::PathSeparator + $previousPath
        & ([string]$NodeResolution.Npm) @Arguments | Out-Host
        return $LASTEXITCODE
    }
    finally {
        $env:Path = $previousPath
    }
}

function Resolve-BentoSlideAppNode {
    param([Parameter(Mandatory = $true)][string]$Repository)

    $systemNode = Get-Command node.exe -ErrorAction SilentlyContinue
    $systemNpm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($null -ne $systemNode -and $null -ne $systemNpm) {
        try {
            $versionText = (& $systemNode.Source --version).Trim().TrimStart('v')
            if (Test-BentoSlideAppNodeVersion -VersionText $versionText) {
                return [pscustomobject]@{ Node = $systemNode.Source; Npm = $systemNpm.Source; Source = 'system' }
            }
        }
        catch { }
    }

    $resolvedRepository = [System.IO.Path]::GetFullPath($Repository)
    $toolsRoot = Join-Path $resolvedRepository 'output\app-tools'
    [System.IO.Directory]::CreateDirectory($toolsRoot) | Out-Null
    $releaseIndex = Invoke-RestMethod -Uri 'https://nodejs.org/dist/index.json' -TimeoutSec 30
    $release = $releaseIndex | Where-Object {
        $_.lts -and
        ($_.files -contains 'win-x64-zip') -and
        (Test-BentoSlideAppNodeVersion -VersionText ([string]$_.version).TrimStart('v'))
    } | Select-Object -First 1
    if ($null -eq $release) { throw 'The official Node.js release index has no compatible Windows x64 LTS package.' }
    $version = [string]$release.version
    if ($version -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+$') { throw "Unexpected Node.js release version: $version" }
    $folderName = "node-$version-win-x64"
    $nodeRoot = Join-Path $toolsRoot $folderName
    $nodePath = Join-Path $nodeRoot 'node.exe'
    $npmPath = Join-Path $nodeRoot 'npm.cmd'
    if ((Test-Path -LiteralPath $nodePath -PathType Leaf) -and (Test-Path -LiteralPath $npmPath -PathType Leaf)) {
        return [pscustomobject]@{ Node = $nodePath; Npm = $npmPath; Source = 'verified-portable' }
    }

    $archiveName = "$folderName.zip"
    $archivePath = Join-Path $toolsRoot $archiveName
    $checksumsPath = Join-Path $toolsRoot "SHASUMS256-$version.txt"
    $baseUrl = "https://nodejs.org/dist/$version"
    Invoke-WebRequest -Uri "$baseUrl/$archiveName" -OutFile $archivePath -TimeoutSec 180
    Invoke-WebRequest -Uri "$baseUrl/SHASUMS256.txt" -OutFile $checksumsPath -TimeoutSec 30
    $checksumLine = Get-Content -LiteralPath $checksumsPath -Encoding ascii | Where-Object { $_ -match ("^[0-9a-f]{64}\s+" + [regex]::Escape($archiveName) + '$') } | Select-Object -First 1
    if (-not $checksumLine) { throw "The official checksum list does not contain $archiveName" }
    $expectedHash = ($checksumLine -split '\s+')[0].ToUpperInvariant()
    $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($actualHash -ne $expectedHash) { throw 'The downloaded Node.js archive failed SHA-256 verification.' }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $toolsRoot -Force
    if (-not (Test-Path -LiteralPath $nodePath -PathType Leaf) -or -not (Test-Path -LiteralPath $npmPath -PathType Leaf)) {
        throw 'The verified Node.js archive did not create the expected executables.'
    }
    return [pscustomobject]@{ Node = $nodePath; Npm = $npmPath; Source = 'verified-portable' }
}
