# backup-oi.ps1 - dump the TimescaleDB `oi` database to a dated .dump file
# on this computer, from either the LOCAL docker stack or a remote VPS.
#
#   Local stack:  .\scripts\backup-oi.ps1
#   VPS stack:    .\scripts\backup-oi.ps1 -Mode vps -VpsHost root@203.0.113.10
#
# Schedule daily (16:00, after market close) via Task Scheduler:
#   schtasks /Create /TN "OI-DB-Backup" /SC DAILY /ST 16:00 /TR "powershell -NoProfile -ExecutionPolicy Bypass -File \"D:\trading algo (ayush bhai)\trading algo (ayush bhai)\scripts\backup-oi.ps1\""
# or weekly (Friday 16:00):
#   schtasks /Create /TN "OI-DB-Backup" /SC WEEKLY /D FRI /ST 16:00 /TR "..."
#
# Restore (TimescaleDB needs its pre/post restore hooks):
#   docker cp <file>.dump docker-timescaledb-1:/tmp/r.dump
#   docker exec docker-timescaledb-1 psql -U postgres -d <db> -c "SELECT timescaledb_pre_restore();"
#   docker exec docker-timescaledb-1 pg_restore -U postgres -d <db> --no-owner /tmp/r.dump
#   docker exec docker-timescaledb-1 psql -U postgres -d <db> -c "SELECT timescaledb_post_restore();"
#
# pg_dump runs inside the DB container and the file is copied out afterwards
# (never piped through PowerShell, which corrupts binary streams).

param(
    [ValidateSet("local", "vps")]
    [string]$Mode = "local",
    [string]$OutDir = "D:\oi-backups",
    [string]$Container = "docker-timescaledb-1",   # local mode
    [string]$VpsHost = "",                          # vps mode: user@ip (SSH key auth)
    [string]$VpsPath = "/opt/nifty-oi",             # vps mode: repo path on the server
    [int]$KeepDays = 60
)

$ErrorActionPreference = "Stop"
$stamp = Get-Date -Format "yyyy-MM-dd_HHmm"
$outFile = Join-Path $OutDir "oi_$stamp.dump"
New-Item -ItemType Directory -Force $OutDir | Out-Null

if ($Mode -eq "local") {
    docker exec $Container sh -c "pg_dump -U postgres -Fc oi > /tmp/oi_backup.dump"
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed inside container $Container" }
    docker cp "${Container}:/tmp/oi_backup.dump" $outFile
    if ($LASTEXITCODE -ne 0) { throw "docker cp failed" }
    docker exec $Container rm -f /tmp/oi_backup.dump | Out-Null
}
else {
    if (-not $VpsHost) { throw "-VpsHost user@ip is required for -Mode vps" }
    ssh $VpsHost "cd $VpsPath && docker compose --env-file .env -f docker/docker-compose.yml exec -T timescaledb sh -c 'pg_dump -U postgres -Fc oi > /tmp/oi_backup.dump' && docker compose --env-file .env -f docker/docker-compose.yml cp timescaledb:/tmp/oi_backup.dump /tmp/oi_backup.dump"
    if ($LASTEXITCODE -ne 0) { throw "remote pg_dump failed on $VpsHost" }
    scp "${VpsHost}:/tmp/oi_backup.dump" $outFile
    if ($LASTEXITCODE -ne 0) { throw "scp failed" }
    ssh $VpsHost "rm -f /tmp/oi_backup.dump"
}

$size = (Get-Item $outFile).Length
if ($size -lt 10KB) { throw "backup suspiciously small ($size bytes): $outFile" }
"backup OK: $outFile ($([math]::Round($size / 1MB, 1)) MB)"

# Retention: prune dumps older than KeepDays.
Get-ChildItem $OutDir -Filter "oi_*.dump" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
    ForEach-Object { "pruned: $($_.Name)"; Remove-Item $_.FullName -Force -Confirm:$false }
