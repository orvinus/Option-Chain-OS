# One-button launcher for the NIFTY OI trading stack.
# Brings up TimescaleDB, FastAPI backend and frontend in a single window with
# colored, service-prefixed interleaved logs. Press Ctrl+C to stop everything.

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Write-Step($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "OK  $msg"  -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "!!  $msg"  -ForegroundColor Yellow }
function Write-Err($msg)   { Write-Host "ERR $msg"  -ForegroundColor Red }

# ----------------------------------------------------------------------------
# 1) Docker Desktop
# ----------------------------------------------------------------------------
Write-Step "Checking Docker Desktop..."
$dockerOk = $false
try { docker info *> $null; if ($LASTEXITCODE -eq 0) { $dockerOk = $true } } catch {}

if (-not $dockerOk) {
    Write-Warn2 "Docker daemon not responding. Trying to start Docker Desktop..."
    $dd = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dd) {
        Start-Process -FilePath $dd | Out-Null
    } else {
        Write-Err "Docker Desktop.exe not found at '$dd'. Install Docker Desktop and try again."
        exit 1
    }
    Write-Host "Waiting for Docker daemon (up to 60s)..." -NoNewline
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        Write-Host "." -NoNewline
        try { docker info *> $null; if ($LASTEXITCODE -eq 0) { $dockerOk = $true; break } } catch {}
    }
    Write-Host ""
    if (-not $dockerOk) {
        Write-Err "Docker daemon did not come up in 60s. Start Docker Desktop manually and re-run."
        exit 1
    }
}
Write-Ok "Docker is ready."

# ----------------------------------------------------------------------------
# 2) .env file
# ----------------------------------------------------------------------------
Write-Step "Checking .env..."
$envPath     = Join-Path $RepoRoot ".env"
$envTemplate = Join-Path $RepoRoot ".env.example"
if (-not (Test-Path $envPath)) {
    if (Test-Path $envTemplate) {
        Copy-Item $envTemplate $envPath
        Write-Warn2 ".env was missing. Copied .env.example -> .env."
        Write-Err "Open .env, fill in your XTS credentials (XTS_MD_APP_KEY, XTS_MD_SECRET_KEY), then re-run start.bat."
        exit 1
    } else {
        Write-Err ".env not found and no .env.example to copy from. Cannot continue."
        exit 1
    }
}

$envMap = @{}
foreach ($line in Get-Content $envPath) {
    if ($line -match '^\s*#') { continue }
    if ($line -match '^\s*$') { continue }
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
        $envMap[$matches[1]] = $matches[2].Trim('"').Trim("'")
    }
}

$required = @('XTS_MD_APP_KEY','XTS_MD_SECRET_KEY')
$missing  = @()
foreach ($k in $required) {
    if (-not $envMap.ContainsKey($k) -or [string]::IsNullOrWhiteSpace($envMap[$k]) -or $envMap[$k] -like 'your_*') {
        $missing += $k
    }
}
if ($missing.Count -gt 0) {
    Write-Err "These required .env keys are blank or still set to placeholder values:"
    $missing | ForEach-Object { Write-Host "    - $_" -ForegroundColor Red }
    Write-Host "Edit .env at: $envPath" -ForegroundColor Yellow
    exit 1
}
Write-Ok ".env present and required keys filled."

# ----------------------------------------------------------------------------
# 3) Browser auto-open background job
# ----------------------------------------------------------------------------
Write-Step "Spawning background browser-opener (waits for backend health)..."
$browserJob = Start-Job -ScriptBlock {
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "http://localhost:8000/api/health" -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) {
                Start-Process "http://localhost"
                return "opened"
            }
        } catch {}
        Start-Sleep -Seconds 2
    }
    return "timeout"
}

# ----------------------------------------------------------------------------
# 4) Foreground docker compose up
# ----------------------------------------------------------------------------
Write-Step "Starting stack (docker compose up). Press Ctrl+C to stop everything."
Write-Host ""

$composeFile = Join-Path $RepoRoot "docker\docker-compose.yml"

# ALWAYS layer the local override when it exists. It pins RUN_MODE=replay, which
# keeps this PC from logging in to the broker. The XTS market-data API allows only
# ONE session per app-key, so a dev machine running live silently invalidates the
# VPS's token and corrupts production's feed. Compose `environment:` beats
# `env_file:`, so this wins even if .env still says RUN_MODE=live.
$composeArgs = @("-f", $composeFile)
$overrideFile = Join-Path $RepoRoot "docker\docker-compose.override.yml"
if (Test-Path $overrideFile) {
    $composeArgs += @("-f", $overrideFile)
    Write-Host "    local override active -> RUN_MODE=replay (broker login disabled)" -ForegroundColor DarkGray
} else {
    Write-Host "    WARNING: docker\docker-compose.override.yml not found." -ForegroundColor Yellow
    Write-Host "             If .env has RUN_MODE=live this PC will fight the VPS for the" -ForegroundColor Yellow
    Write-Host "             single XTS session. Set RUN_MODE=replay in .env before continuing." -ForegroundColor Yellow
}

try {
    docker compose --env-file $envPath @composeArgs up --build
} finally {
    Write-Host ""
    Write-Step "Tearing down containers..."
    docker compose --env-file $envPath @composeArgs down
    if ($browserJob) {
        Stop-Job   $browserJob -ErrorAction SilentlyContinue
        Remove-Job $browserJob -ErrorAction SilentlyContinue
    }
    Write-Ok "Stopped. Re-run start.bat to launch again."
}
