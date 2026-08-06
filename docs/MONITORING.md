# Monitoring & self-healing — the three layers

Written after two whole-session outages (2026-08-04 and 2026-08-06) that were
discovered by *looking at the dashboard*. The design goal is not "100% uptime" —
nobody can promise that — it is: **no single failure loses a session; every
failure is bounded to minutes, recovers without a human, and pages the human
anyway.**

```
┌─ Layer 1 — in-process: SessionSteward ──────────────────────────────┐
│ The ONLY component allowed to rotate the XTS token (single-session   │
│ per appKey: every login kills the previous token's socket).          │
│ Health = "are WS rows landing?" · Ladder: kick → rotate → universe   │
│ rebuild → client rebuild → os._exit(1) (Docker restarts it clean).   │
│ Token schedule: daily 08:35 IST pre-open rotation + >22h backstop.   │
│ REST failover: poller covers the active symbol while the WS is down. │
├─ Layer 2 — VPS: oi-sentinel (systemd timer, 1/min) ─────────────────┤
│ Owns what the process can't fix about itself: frozen event loop,     │
│ dead container, dead docker, disk, cert expiry, backup age, Caddy.   │
│ Probes /api/health/strict; restarts backend only when the body says  │
│ restart_recommended:true, K=5 consecutive fails, container >10 min   │
│ old, capped 2/day. Snapshots logs before every restart.              │
├─ Layer 3 — paging ──────────────────────────────────────────────────┤
│ Telegram from both layers · daily 08:50 heartbeat ("silence = broken │
│ alerting") · optional healthchecks.io dead-man ping (a dead VPS      │
│ pages you through their side).                                       │
└──────────────────────────────────────────────────────────────────────┘
```

## The strict probe contract

`GET /api/health/strict` → `200` when healthy (or NSE holiday, or market
closed), `503` with:

```json
{"status":"degraded","reasons":["stale_data"|"db_down"|"recovery_circuit_open"],
 "restart_recommended":true|false,"detail":"..."}
```

`restart_recommended` is **false** for every cause a backend restart cannot fix
(broker down, rotation circuit parked, DB dead) — the sentinel never restarts
for those, it only pages. While REST failover keeps data fresh with the socket
dead, strict stays `200` with `"degraded":"rest_failover"` (never restart a
successfully-limping process; you get a reminder page every 30 min instead).

## Install (once, on the VPS)

```bash
cd /root/nifty-oi
sudo bash scripts/install-sentinel.sh
# then follow its printed next steps: TELEGRAM_* in .env, test-alert, oi-doctor
```

Create the Telegram bot: message @BotFather → `/newbot` → copy the token into
`.env` as `TELEGRAM_BOT_TOKEN=`. Send your new bot any message, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id` into
`TELEGRAM_CHAT_ID=`. Also set `LOG_FORMAT=json` in the prod `.env`.

**Verify everything with one command: `sudo oi-doctor`** — run it after
install, after every reboot, and any time you're unsure the net is armed.

## When paged — the 5-command triage

```bash
scripts/deploy.sh ps
curl -s 127.0.0.1:8000/api/health | head -c 400; echo; curl -si 127.0.0.1:8000/api/health/strict | head -20
journalctl -u oi-sentinel.service -n 60 --no-pager
scripts/deploy.sh logs --tail 100 backend
df -h /; docker info >/dev/null 2>&1 && echo docker-ok || echo DOCKER-DEAD
```

Manual recovery escalation (in order; stop when healthy):
1. Nothing — the steward is usually mid-ladder; give it 3 minutes.
2. `curl -X POST 127.0.0.1:8000/api/auth/login -H 'Content-Type: application/json' -d '{"force_new_token":true}'` — the human override rotation.
3. `scripts/deploy.sh restart backend` (keeps the container = keeps its logs).
4. `scripts/deploy.sh` (full rebuild — snapshots logs first automatically).

## Holidays

`is_nse_regular_session_open()` doesn't know holidays, so on an NSE holiday
strict would read "stale during session hours" all day. `data/nse_holidays.json`
suppresses that — but it ships with only CERTAIN dates. **Maintain it from the
NSE trading-holiday circular** (nseindia.com → Resources → Trading holidays):
a *missing* date costs you at most 2 capped restarts + clearly-worded pages; a
*wrongly added* date silences real alerting for a whole day. `oi-doctor` warns
when the file looks under-maintained. To pre-silence a known holiday morning
instead: `systemctl stop oi-sentinel.timer` for the day (re-enable after!).

## Drills (run once after install, market closed)

| # | Do | Expect |
|---|---|--------|
| D1 | `docker stop $(scripts/deploy.sh ps -q backend)` | ≤2 min: container Up again + "started (compose up -d)" page |
| D2 | `docker pause <backend>` then run `OI_SENTINEL_FORCE_WINDOW=1 oi-sentinel` 6× | On the 5th+ fail: `pre-restart-*.log` in /var/log/oi-sentinel, restart, page. `docker unpause` if needed; then `oi-sentinel reset-day` |
| D3 | `oi-sentinel test-alert hello` with a broken token in .env | Non-zero exit; the main cycle still ACTS when Telegram is down (alerts fail soft) |
| D4 | `netfilter-persistent save` (confirm SSH from a 2nd session first!) then `reboot` | Everything back unattended: 3 containers Up, timers live (`systemctl list-timers 'oi-*'`), iptables persisted, next 08:50 heartbeat arrives |
| D5 | Next trading morning, watch logs 08:30-09:20 | `steward.daily_rotation_due` at 08:35 → `steward.rotation_verified` (tier-3) → rows at 09:15 → green heartbeat at 08:50 |

After any drill: `oi-sentinel reset-day`.

## Residual SPOFs — what still takes production down after all this

1. **VPS hardware/hypervisor death.** Detected by the dead-man ping (if set) or
   heartbeat absence. Recovery = redeploy + restore from the **off-box** backup
   — which is a placeholder until `OI_BACKUP_REMOTE` is set in `.env`.
   **That is the #1 open item.**
2. **DNS / registrar / domain expiry** — enable registrar auto-renew; only the
   TLS check partially covers this.
3. **Caddy death** — the sentinel's edge check pages (alert-only; Caddy is
   host-managed). Verify once: `systemctl show caddy -p Restart` → `on-abnormal`.
4. **Broker-side XTS outage** — alert-only by design; the circuit parks
   rotations and probes every 5 min; data gaps for the broker's duration.
5. **Telegram outage** — covered only by the dead-man ping / heartbeat absence.
6. **A second client on the appKey** (a local `RUN_MODE=live` stack, an old
   cached dashboard tab): the steward detects the verify-then-die signature,
   parks, and names the suspects in the page. See docs/LOCAL-DEV.md — never
   run a second live stack on the production key.

## Notes

* Every deploy/restart now costs **zero logins** (shutdown no longer logs the
  token out; restore validates and reuses it) — a ~30-60s data gap remains.
* The production repo lives at `/root/nifty-oi` (older docs said /opt — fixed).
* Prefer `FRONTEND_BIND=127.0.0.1:8080` in `.env` over the historical
  skip-worktree'd compose edit. Migration:
  `git update-index --no-skip-worktree docker/docker-compose.yml && git checkout -- docker/docker-compose.yml`,
  add the line to `.env`, `scripts/deploy.sh up -d frontend`.
* `websocket_logs` / `api_logs` tables: nothing writes them (verified); no
  retention needed.
