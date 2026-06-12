# Build a shareable Windows executable (backend + Vite UI on one port).
# Prerequisites: Node.js (npm), Python 3.12+ with backend deps + PyInstaller.
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $RepoRoot

$Dist = Join-Path $RepoRoot "frontend\dist"
if (-not (Test-Path (Join-Path $RepoRoot "frontend\package.json"))) {
    Write-Error "frontend/package.json not found; run this script from the repo."
}

Write-Host "==> npm ci + build (frontend)" -ForegroundColor Cyan
Push-Location (Join-Path $RepoRoot "frontend")
npm ci
if ($LASTEXITCODE -ne 0) { Pop-Location; Write-Error "npm ci failed (exit $LASTEXITCODE)." }
npm run build
if ($LASTEXITCODE -ne 0) { Pop-Location; Write-Error "npm run build failed (exit $LASTEXITCODE)." }
Pop-Location

if (-not (Test-Path (Join-Path $Dist "index.html"))) {
    Write-Error "frontend/dist/index.html missing after build."
}

$Exe = Join-Path $RepoRoot "backend\dist\NiftyOI-Analytics.exe"
if (Test-Path $Exe) {
    Write-Host "==> Replacing existing EXE (stop app if it is running)" -ForegroundColor Cyan
    Get-Process -Name "NiftyOI-Analytics" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    try {
        Remove-Item -LiteralPath $Exe -Force -ErrorAction Stop
    } catch {
        Write-Error @"
Cannot delete or replace: $Exe
Close NiftyOI-Analytics (Task Manager), any Explorer window opened inside dist\, and retry. Antivirus can also lock the file.
Original error: $($_.Exception.Message)
"@
    }
}

Write-Host "==> PyInstaller (onefile EXE)" -ForegroundColor Cyan
Push-Location (Join-Path $RepoRoot "backend")
python -m pip install -q -r requirements.txt -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { Pop-Location; Write-Error "pip install failed (exit $LASTEXITCODE)." }
python -m PyInstaller (Join-Path $RepoRoot "packaging\nifty-oi-windows.spec") --noconfirm --clean
$pyExit = $LASTEXITCODE
Pop-Location
if ($pyExit -ne 0) {
    Write-Error "PyInstaller failed (exit $pyExit). See traceback above. If you saw PermissionError on the .exe, stop the running app and run this script again."
}

if (-not (Test-Path $Exe)) {
    Write-Error "PyInstaller reported success but EXE is missing: $Exe"
}

Write-Host "Built: $Exe" -ForegroundColor Green
Write-Host "Ship this file plus a .env next to it (copy from .env.example at repo root)." -ForegroundColor Yellow
