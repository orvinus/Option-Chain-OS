@echo off
rem Finish the data-quality repair locally:
rem 1) stamp underlying from IDX rows (exact minute), then NIFTY-I/SENSEX-I
rem    futures close as proxy where the vendor has no index bars
rem 2) dump every affected day (both symbols) + all IDX/FUT rows for shipping
cd /d "%~dp0.."
(
echo === spot stamp started ===
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "UPDATE oi_archive_bars o SET underlying = i.close FROM oi_archive_bars i WHERE i.symbol=o.symbol AND i.option_type='IDX' AND i.ts=o.ts AND o.option_type IN ('CE','PE') AND o.underlying IS NULL AND i.close IS NOT NULL;"
echo === futures-proxy fallback ===
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "UPDATE oi_archive_bars o SET underlying = f.close FROM oi_archive_bars f WHERE f.symbol=o.symbol AND f.option_type='FUT' AND f.ts=o.ts AND o.option_type IN ('CE','PE') AND o.underlying IS NULL AND f.close IS NOT NULL;"
docker exec docker-timescaledb-1 psql -U postgres -d oi -t -c "SELECT count(*) FILTER (WHERE underlying IS NULL) AS still_null, count(*) FROM oi_archive_bars WHERE option_type IN ('CE','PE');"
echo === dumping affected days ===
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "\copy (SELECT * FROM oi_archive_bars WHERE option_type IN ('CE','PE') AND ((ts AT TIME ZONE 'Asia/Kolkata')::date BETWEEN '2026-03-09' AND '2026-03-20' OR (ts AT TIME ZONE 'Asia/Kolkata')::date >= '2026-05-27' OR (ts AT TIME ZONE 'Asia/Kolkata')::date IN ('2026-06-05'))) TO STDOUT WITH (FORMAT csv)" | gzip > logs/backfill/transfer/repair_days.csv.gz
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "\copy (SELECT * FROM oi_archive_bars WHERE option_type IN ('IDX','FUT')) TO STDOUT WITH (FORMAT csv)" | gzip > logs/backfill/transfer/repair_idxfut.csv.gz
dir logs\backfill\transfer\repair_*.gz
echo === SPOT STAMP AND DUMP FINISHED ===
) >> logs\backfill\gapday_repair.log 2>&1
