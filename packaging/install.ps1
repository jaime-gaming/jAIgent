<#
.SYNOPSIS
    jaigent installer for Windows.

.DESCRIPTION
    Downloads the standalone jaigent.exe for this machine from the latest
    GitHub release, verifies its checksum, installs it under %LOCALAPPDATA%
    and puts it on your PATH. No Python required.

.EXAMPLE
    irm https://raw.githubusercontent.com/jaime-gaming/jaigent/main/packaging/install.ps1 | iex

.PARAMETER Version
    Install a specific tag instead of the latest release.

.PARAMETER InstallDir
    Where to put jaigent.exe. Defaults to %LOCALAPPDATA%\Programs\jaigent.
#>

[CmdletBinding()]
param(
    [string]$Version = $env:JAIGENT_VERSION,
    [string]$InstallDir = $env:JAIGENT_BIN_DIR
)

$ErrorActionPreference = 'Stop'
$Repo = 'jaime-gaming/jaigent'

function Write-Step { param($Message) Write-Host "  $Message" -ForegroundColor DarkGray }
function Write-Ok { param($Message) Write-Host $Message -ForegroundColor Green }
function Write-Fail { param($Message) Write-Host "error: $Message" -ForegroundColor Red; exit 1 }

# TLS 1.2 is not the default on older PowerShell, and GitHub requires it.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not $InstallDir) {
    $InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\jaigent'
}

# ------------------------------------------------------------------ platform
$arch = if ([Environment]::Is64BitOperatingSystem) {
    if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x64' }
} else {
    Write-Fail '32-bit Windows is not supported. Install from source: pip install jaigent'
}

# ------------------------------------------------------------------- version
if (-not $Version) {
    Write-Step 'Looking up the latest release...'
    try {
        $release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest"
        $Version = $release.tag_name
    } catch {
        Write-Fail "could not reach GitHub. Set -Version to install a specific tag."
    }
}

$asset = "jaigent-windows-$arch"
$url = "https://github.com/$Repo/releases/download/$Version/$asset.zip"

Write-Host ''
Write-Step "version   $Version"
Write-Step "platform  windows-$arch"
Write-Step "target    $InstallDir\jaigent.exe"
Write-Host ''

# ------------------------------------------------------------------ download
$temp = Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $temp -Force | Out-Null

try {
    $archive = Join-Path $temp 'jaigent.zip'
    Write-Step "Downloading $asset..."
    try {
        Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
    } catch {
        Write-Fail "download failed: $url"
    }

    # Verify the checksum when the release publishes one. A tampered binary is
    # a far worse outcome than a failed install, so a mismatch is always fatal.
    try {
        $sums = Invoke-WebRequest -Uri `
            "https://github.com/$Repo/releases/download/$Version/checksums.txt" `
            -UseBasicParsing
        $line = ($sums.Content -split "`n" | Where-Object { $_ -match [regex]::Escape("$asset.zip") })
        if ($line) {
            $expected = ($line -split '\s+')[0]
            $actual = (Get-FileHash -Path $archive -Algorithm SHA256).Hash.ToLower()
            if ($actual -ne $expected.ToLower()) {
                Write-Fail 'checksum mismatch — refusing to install.'
            }
            Write-Ok '  checksum verified'
        }
    } catch {
        Write-Step 'no checksum published for this release; skipping verification'
    }

    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    Expand-Archive -Path $archive -DestinationPath $temp -Force

    $binary = Get-ChildItem -Path $temp -Filter 'jaigent.exe' -Recurse | Select-Object -First 1
    if (-not $binary) { Write-Fail 'the archive did not contain jaigent.exe' }

    Copy-Item -Path $binary.FullName -Destination (Join-Path $InstallDir 'jaigent.exe') -Force
    Write-Ok "Installed jaigent $Version to $InstallDir\jaigent.exe"
} finally {
    Remove-Item -Path $temp -Recurse -Force -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------- PATH
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -notlike "*$InstallDir*") {
    [Environment]::SetEnvironmentVariable('Path', "$userPath;$InstallDir", 'User')
    $env:Path = "$env:Path;$InstallDir"
    Write-Step "Added $InstallDir to your PATH (restart your terminal to pick it up)"
}

Write-Host ''
Write-Host 'Next:  ' -NoNewline
Write-Ok 'jaigent init'
