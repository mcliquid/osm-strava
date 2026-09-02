#Requires -Version 5.1
# Start the WSL OSM update script from any working directory.
#
#   .\update-osm.ps1 mallorca
#   .\update-osm.ps1 bodenseekreis
#   .\update-osm.ps1 mallorca --fresh
#   .\update-osm.ps1 mallorca --fresh --bootstrap
#   .\update-osm.ps1 --list
#   .\update-osm.ps1 --show-config mallorca
#
$ErrorActionPreference = "Stop"

$repoRoot = $PSScriptRoot
$windowsScript = Join-Path $repoRoot "update-osm.sh"

if (-not (Test-Path -LiteralPath $windowsScript)) {
    Write-Error "update-osm.sh not found: $windowsScript"
    exit 1
}

function ConvertTo-WslPath {
    param([Parameter(Mandatory = $true)][string]$WindowsPath)
    $full = [System.IO.Path]::GetFullPath($WindowsPath)
    if ($full -match "^([A-Za-z]):\\(.*)$") {
        $drive = $Matches[1].ToLowerInvariant()
        $rest = ($Matches[2] -replace "\\", "/")
        return "/mnt/$drive/$rest"
    }
    throw "Cannot convert Windows path to WSL: $WindowsPath"
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    Write-Error "wsl.exe not found. Install Windows Subsystem for Linux."
    exit 1
}

$wslScript = $null
try {
    $wslScript = (& wsl.exe wslpath -a -- "$windowsScript" 2>$null | Select-Object -Last 1)
    if ($wslScript) {
        $wslScript = $wslScript.Trim()
    }
} catch {
    $wslScript = $null
}

if ([string]::IsNullOrWhiteSpace($wslScript)) {
    $wslScript = ConvertTo-WslPath -WindowsPath $windowsScript
}

& wsl.exe -e bash -- "$wslScript" @args
exit $LASTEXITCODE
