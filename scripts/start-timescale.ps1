# =====================================================================
# Start TimescaleDB on Windows via Docker Desktop.
#
# Run once.  Re-running is safe (it just reports "already running").
# Requires:
#   * Docker Desktop installed and running
# =====================================================================

$ErrorActionPreference = "Stop"

$ContainerName = "timescaledb"
$Image         = "timescale/timescaledb:latest-pg16"
$DbName        = "oi"
$Password      = "postgres"
# Host port 5434 avoids clashing with a local PostgreSQL install on Windows (often :5432).
$Port          = 5434
$Volume        = "timescale_data"

function Test-DockerRunning {
    try {
        docker info | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

if (-not (Test-DockerRunning)) {
    Write-Host "Docker Desktop is not running. Please launch it first." -ForegroundColor Red
    exit 1
}

$existing = docker ps -a --filter "name=^$ContainerName$" --format "{{.Names}}|{{.Status}}"
if ($existing) {
    $parts = $existing -split "\|"
    if ($parts[1] -like "Up*") {
        $published = docker port $ContainerName 5432 2>$null
        if ($published -and ($published -notmatch ":$Port")) {
            Write-Host "TimescaleDB is running but published on a different host port than $Port." -ForegroundColor Yellow
            Write-Host "Remove and recreate so DB matches .env (example): docker rm -f $ContainerName" -ForegroundColor Yellow
            Write-Host "Then re-run this script. Data volume '$Volume' is kept." -ForegroundColor Yellow
        }
        Write-Host "TimescaleDB is already running ($($parts[1]))." -ForegroundColor Green
        exit 0
    }
    Write-Host "TimescaleDB container exists but is stopped. Starting..." -ForegroundColor Yellow
    docker start $ContainerName | Out-Null
}
else {
    Write-Host "Pulling image $Image (first run may take a minute)..." -ForegroundColor Cyan
    docker pull $Image | Out-Null
    Write-Host "Creating container $ContainerName..." -ForegroundColor Cyan
    docker run -d `
        --name $ContainerName `
        -p "${Port}:5432" `
        -e POSTGRES_PASSWORD=$Password `
        -e POSTGRES_DB=$DbName `
        -v "${Volume}:/var/lib/postgresql/data" `
        $Image | Out-Null
}

Write-Host "Waiting for Postgres to accept connections..." -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    $check = docker exec $ContainerName pg_isready -U postgres 2>$null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
}
if (-not $ready) {
    Write-Host "Database did not become ready in time. Check 'docker logs $ContainerName'." -ForegroundColor Red
    exit 1
}

Write-Host "TimescaleDB is up at localhost:$Port (db=$DbName, user=postgres)." -ForegroundColor Green
Write-Host "DB_URL=postgresql+psycopg://postgres:$Password@localhost:$Port/$DbName" -ForegroundColor Gray
