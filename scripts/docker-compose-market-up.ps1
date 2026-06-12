# Bring up the docker stack before NSE session (e.g. Windows Task Scheduler @ 09:05 IST).
# Adjust $RepoRoot if you cloned elsewhere.
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
docker compose --env-file .env -f docker/docker-compose.yml up -d
