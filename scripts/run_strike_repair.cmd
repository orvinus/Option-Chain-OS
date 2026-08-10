@echo off
rem Detached one-shot repair: strip the yymmdd date prefix that the buggy tail
rem parser concatenated into option strikes (and tokens) during the first pull.
cd /d "%~dp0.."
(
echo === strike repair started ===
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "UPDATE oi_archive_bars SET token = 'td:'||symbol||':'||substring(strike::text FROM 1 FOR 6)||':'||substring(strike::text FROM 7)||':'||option_type, strike = substring(strike::text FROM 7)::bigint WHERE option_type IN ('CE','PE') AND strike > 1000000;"
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "UPDATE oi_archive_ticks SET token = 'td:'||symbol||':'||substring(strike::text FROM 1 FOR 6)||':'||substring(strike::text FROM 7)||':'||option_type, strike = substring(strike::text FROM 7)::bigint WHERE option_type IN ('CE','PE') AND strike > 1000000;"
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "UPDATE eod_bars SET expiry = to_date(substring(strike::text FROM 1 FOR 6),'YYMMDD'), strike = substring(strike::text FROM 7)::bigint WHERE option_type IN ('CE','PE') AND strike > 1000000;"
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "SELECT 'bars_bad', count(*) FROM oi_archive_bars WHERE option_type IN ('CE','PE') AND strike > 1000000 UNION ALL SELECT 'ticks_bad', count(*) FROM oi_archive_ticks WHERE option_type IN ('CE','PE') AND strike > 1000000 UNION ALL SELECT 'eod_bad', count(*) FROM eod_bars WHERE option_type IN ('CE','PE') AND strike > 1000000;"
echo === strike repair finished ===
) >> logs\backfill\strike_repair.log 2>&1
