# Creates a clickable Desktop shortcut that launches the whole NIFTY OI stack
# (TimescaleDB + FastAPI backend + frontend) by running start.bat.
#
# Run once:  powershell -ExecutionPolicy Bypass -File scripts\create-desktop-shortcut.ps1

$ErrorActionPreference = "Stop"

$RepoRoot   = Split-Path -Parent $PSScriptRoot
$StartBat   = Join-Path $RepoRoot "start.bat"
$Desktop    = [Environment]::GetFolderPath("Desktop")
$LinkPath   = Join-Path $Desktop "Start NIFTY OI Platform.lnk"

if (-not (Test-Path $StartBat)) {
    throw "start.bat not found at $StartBat"
}

# Pick an icon: prefer Docker Desktop's, else a Windows shell icon.
$DockerExe = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
if (Test-Path $DockerExe) {
    $IconLocation = "$DockerExe,0"
} else {
    $IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
}

$shell    = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($LinkPath)
$shortcut.TargetPath       = $StartBat
$shortcut.WorkingDirectory = $RepoRoot
$shortcut.IconLocation     = $IconLocation
$shortcut.Description       = "Start the NIFTY OI Analytics stack (DB + backend + frontend) and open the dashboard"
$shortcut.WindowStyle       = 1
$shortcut.Save()

Write-Host "Created shortcut: $LinkPath" -ForegroundColor Green
Write-Host "Double-click it to launch the whole system." -ForegroundColor Cyan
