# =============================================================================
# NIFTY OI Platform - FULL bootstrap for a FRESH Windows machine.
#
# Unzip the product anywhere, double-click "Setup-and-Run.bat", and this script:
#   1. Self-elevates to Administrator (needed to install Docker / WSL2).
#   2. Installs Docker Desktop + WSL2 via winget if they are missing.
#   3. Starts the Docker daemon and waits for it to be ready.
#   4. Verifies the bundled .env (your XTS credentials travel with the zip).
#   5. Brings up the whole stack (TimescaleDB + backend + frontend) and opens
#      the dashboard in your browser.
#
# Safe to run repeatedly: every step is idempotent. If a reboot is required to
# finish installing WSL2/Docker, the script registers itself to resume
# automatically after the next sign-in.
# =============================================================================

$ErrorActionPreference = "Stop"

# ----------------------------------------------------------------------------
# 0) Self-elevate to Administrator (keep window open so Ctrl+C can stop later)
# ----------------------------------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "Requesting Administrator rights..." -ForegroundColor Yellow
    $psArgs = @('-NoExit', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $psArgs
    exit 0
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Write-Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "OK  $m"  -ForegroundColor Green }
function Write-Warn2($m){ Write-Host "!!  $m"  -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host "ERR $m"  -ForegroundColor Red }

Write-Host "============================================================" -ForegroundColor Magenta
Write-Host " NIFTY OI Platform - one-click setup" -ForegroundColor Magenta
Write-Host " Repo: $RepoRoot" -ForegroundColor DarkGray
Write-Host "============================================================" -ForegroundColor Magenta

# Helper: schedule this script to resume after the next sign-in (one time).
function Register-ResumeAfterReboot {
    $runOnce = "HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce"
    $cmd = "powershell.exe -NoExit -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    New-ItemProperty -Path $runOnce -Name "NiftyOiBootstrap" -Value $cmd -PropertyType String -Force | Out-Null
    Write-Warn2 "Registered to resume automatically after you sign back in."
}

function Test-DockerDaemon {
    try { docker info *> $null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

# ----------------------------------------------------------------------------
# 1) winget availability (ships with Win10 1809+/Win11)
# ----------------------------------------------------------------------------
$haveWinget = $false
try { winget --version *> $null; if ($LASTEXITCODE -eq 0) { $haveWinget = $true } } catch {}

# ----------------------------------------------------------------------------
# 2) Ensure Docker Desktop is installed
# ----------------------------------------------------------------------------
$DockerExe = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
$dockerInstalled = Test-Path $DockerExe

if (-not $dockerInstalled) {
    Write-Step "Docker Desktop not found - installing..."

    if (-not $haveWinget) {
        Write-Err "winget is not available on this machine."
        Write-Host "Install 'App Installer' from the Microsoft Store, or download Docker Desktop" -ForegroundColor Yellow
        Write-Host "manually from https://www.docker.com/products/docker-desktop/ then re-run." -ForegroundColor Yellow
        Read-Host "Press Enter to exit"
        exit 1
    }

    # WSL2 is Docker's default backend; install the kernel/feature if absent.
    Write-Step "Ensuring WSL2 is installed..."
    try { wsl --install --no-distribution *> $null } catch {}

    Write-Step "Installing Docker Desktop via winget (this can take several minutes)..."
    winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        Write-Err "winget reported a problem installing Docker Desktop (exit $LASTEXITCODE)."
        Write-Host "You can install it manually from https://www.docker.com/products/docker-desktop/ and re-run." -ForegroundColor Yellow
        Read-Host "Press Enter to exit"
        exit 1
    }
    Write-Ok "Docker Desktop installed."
    $dockerInstalled = Test-Path $DockerExe

    # A fresh WSL2 + Docker install almost always needs one reboot to work.
    Write-Warn2 "A restart is required to finish setting up WSL2 + Docker."
    Register-ResumeAfterReboot
    $ans = Read-Host "Restart now? Setup will resume automatically after sign-in. (Y/N)"
    if ($ans -match '^(y|Y)') {
        Restart-Computer -Force
        exit 0
    } else {
        Write-Warn2 "Please restart manually, then double-click Setup-and-Run.bat again."
        Read-Host "Press Enter to exit"
        exit 0
    }
}
Write-Ok "Docker Desktop is installed."

# ----------------------------------------------------------------------------
# 3) Start the Docker daemon and wait for it
# ----------------------------------------------------------------------------
Write-Step "Checking Docker daemon..."
if (-not (Test-DockerDaemon)) {
    Write-Warn2 "Daemon not responding. Launching Docker Desktop..."
    Start-Process -FilePath $DockerExe | Out-Null
    Write-Host "Waiting for Docker to be ready (up to 180s)..." -NoNewline
    $ready = $false
    for ($i = 0; $i -lt 180; $i++) {
        Start-Sleep -Seconds 1
        if ($i % 2 -eq 0) { Write-Host "." -NoNewline }
        if (Test-DockerDaemon) { $ready = $true; break }
    }
    Write-Host ""
    if (-not $ready) {
        Write-Err "Docker did not become ready in time."
        Write-Host "If this is right after installing, a reboot usually fixes it." -ForegroundColor Yellow
        Register-ResumeAfterReboot
        Read-Host "Press Enter to exit (then reboot and it resumes automatically)"
        exit 1
    }
}
Write-Ok "Docker daemon is ready."

# ----------------------------------------------------------------------------
# 4) Verify the bundled .env (ships inside the zip)
# ----------------------------------------------------------------------------
Write-Step "Checking .env..."
$envPath     = Join-Path $RepoRoot ".env"
$envTemplate = Join-Path $RepoRoot ".env.example"
if (-not (Test-Path $envPath)) {
    if (Test-Path $envTemplate) {
        Copy-Item $envTemplate $envPath
        Write-Warn2 ".env was missing - copied from .env.example."
        Write-Err "Open .env and fill XTS_MD_APP_KEY / XTS_MD_SECRET_KEY, then re-run."
        Start-Process notepad.exe $envPath
        Read-Host "Press Enter after saving .env to continue"
    } else {
        Write-Err ".env and .env.example are both missing - cannot continue."
        Read-Host "Press Enter to exit"
        exit 1
    }
}
Write-Ok ".env found."

# ----------------------------------------------------------------------------
# 5) Browser auto-open (waits for backend health), then bring the stack up
# ----------------------------------------------------------------------------
Write-Step "Spawning browser-opener (waits for backend health)..."
$browserJob = Start-Job -ScriptBlock {
    $deadline = (Get-Date).AddSeconds(300)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "http://localhost:8000/api/health" -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { Start-Process "http://localhost"; return }
        } catch {}
        Start-Sleep -Seconds 2
    }
}

Write-Step "Starting the stack (docker compose up --build). Press Ctrl+C to stop everything."
Write-Host ""
$composeFile = Join-Path $RepoRoot "docker\docker-compose.yml"
try {
    docker compose --env-file $envPath -f $composeFile up --build
} finally {
    Write-Host ""
    Write-Step "Tearing down containers..."
    docker compose --env-file $envPath -f $composeFile down
    if ($browserJob) {
        Stop-Job $browserJob -ErrorAction SilentlyContinue
        Remove-Job $browserJob -ErrorAction SilentlyContinue
    }
    Write-Ok "Stopped. Double-click Setup-and-Run.bat to launch again."
    Read-Host "Press Enter to close"
}
