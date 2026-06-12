# =====================================================================
# Start the React + Vite frontend dev server.
# =====================================================================

$ErrorActionPreference = "Stop"

$Root        = Split-Path -Parent $PSScriptRoot
$FrontendDir = Join-Path $Root "frontend"

if (-not (Test-Path (Join-Path $FrontendDir "node_modules"))) {
    Write-Host "Installing npm dependencies..." -ForegroundColor Cyan
    Push-Location $FrontendDir
    try {
        npm install
    }
    finally {
        Pop-Location
    }
}

Write-Host "Starting Vite dev server on http://localhost:5173 ..." -ForegroundColor Green
Push-Location $FrontendDir
try {
    npm run dev
}
finally {
    Pop-Location
}
