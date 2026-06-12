# =====================================================================
# Start the FastAPI backend on Windows.
#
#   * Creates a Python virtual env at backend/.venv on first run.
#   * Installs requirements.
#   * Runs Alembic migrations against the configured DB.
#   * Boots uvicorn with --reload for dev.
# =====================================================================

$ErrorActionPreference = "Stop"

$Root        = Split-Path -Parent $PSScriptRoot
$BackendDir  = Join-Path $Root "backend"
$VenvDir     = Join-Path $BackendDir ".venv"
$VenvPython  = Join-Path $VenvDir "Scripts\python.exe"
$VenvUvicorn = Join-Path $VenvDir "Scripts\uvicorn.exe"
$EnvFile     = Join-Path $Root ".env"

if (-not (Test-Path $EnvFile)) {
    Write-Host ".env not found at $EnvFile" -ForegroundColor Red
    Write-Host "Copy .env.example to .env and fill in your Angel One credentials." -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating venv at $VenvDir..." -ForegroundColor Cyan
    py -3.11 -m venv $VenvDir
    if (-not (Test-Path $VenvPython)) {
        # Fallback if py-launcher isn't on PATH
        python -m venv $VenvDir
    }
}

Write-Host "Upgrading pip..." -ForegroundColor Cyan
& $VenvPython -m pip install --upgrade pip wheel | Out-Null

Write-Host "Installing requirements..." -ForegroundColor Cyan
& $VenvPython -m pip install -r (Join-Path $BackendDir "requirements.txt")

Write-Host "Running Alembic migrations..." -ForegroundColor Cyan
Push-Location $BackendDir
try {
    & $VenvPython -m alembic upgrade head
}
finally {
    Pop-Location
}

# Resolve API_PORT from .env (same file pydantic-settings uses at runtime).
$ApiPort = & $VenvPython -c @"
from pathlib import Path
from dotenv import dotenv_values
p = Path(r'$EnvFile')
vals = dotenv_values(p) if p.exists() else {}
raw = (vals.get('API_PORT') or '8000').strip() or '8000'
try:
    print(int(raw))
except ValueError:
    print(8000)
"@

$listeners = Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue
if ($listeners) {
    $pids = $listeners | Select-Object -ExpandProperty OwningProcess -Unique
    Write-Host "Port $ApiPort is already in use (WinError 10048 if you start another server on the same port)." -ForegroundColor Red
    foreach ($opid in $pids) {
        $pn = (Get-Process -Id $opid -ErrorAction SilentlyContinue).ProcessName
        Write-Host "  Listener PID $opid $(if ($pn) { "($pn)" })" -ForegroundColor Yellow
    }
    Write-Host "Stop that process (or close NiftyOI-Analytics.exe), or set API_PORT to a free port in .env and retry." -ForegroundColor Yellow
    exit 1
}

Write-Host "Starting FastAPI on http://localhost:$ApiPort ..." -ForegroundColor Green
Push-Location $BackendDir
try {
    # Use run.py instead of uvicorn directly so Windows SelectorEventLoop
    # is set before uvicorn/psycopg start (fixes ProactorEventLoop error).
    & $VenvPython run.py
}
finally {
    Pop-Location
}
