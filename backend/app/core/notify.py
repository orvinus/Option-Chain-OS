"""Telegram alerting — fire-and-forget, rate-limited, and impossible to break the app.

Production failed twice with nobody watching: the only "alerting" was the operator
looking at the dashboard. This module gives every recovery layer a mouth. Design
constraints, in order of importance:

1.  **Never raises, never blocks.** An alert is a side effect of recovery, not a
    step in it. Telegram being down must not slow a reconnect by one millisecond —
    the send runs as a detached task with a 5s timeout and a bare except.
2.  **Inert without configuration.** No ``TELEGRAM_BOT_TOKEN``/``TELEGRAM_CHAT_ID``
    in the environment → every call is a no-op. Dev boxes stay silent.
3.  **Rate-limited per key.** A watchdog that checks every 30s must not page every
    30s. Repeats of the same ``key`` within ``ALERT_MIN_INTERVAL_S`` are dropped;
    distinct keys are independent, so "unhealthy" and "recovered" both get through.

Usage:  notify("steward_unhealthy", "Feed unhealthy 3 min — escalating")
"""
from __future__ import annotations

import asyncio
import time

import httpx

from .config import settings
from .logging import get_logger

log = get_logger("notify")

_SEND_TIMEOUT_S = 5.0
# key -> monotonic time of the last message actually sent for it.
_last_sent: dict[str, float] = {}


_PROBE_TTL_S = 300.0
_probe_cache: dict[str, object] = {}


def is_configured() -> bool:
    return bool((settings.telegram_bot_token or "").strip()) and bool(
        (settings.telegram_chat_id or "").strip()
    )


def _mask_chat(chat_id: str) -> str:
    c = chat_id.strip()
    return ("…" + c[-4:]) if len(c) > 4 else "…"


async def probe(force: bool = False) -> dict:
    """Is the bot reachable? ``getMe`` on the token, cached for 5 minutes.
    Never returns the token. ``ok`` is None while unconfigured."""
    if not is_configured():
        return {
            "configured": False, "ok": None, "bot_username": None,
            "chat_id_masked": None, "checked_at": None, "error": None,
        }
    now = time.monotonic()
    cached = _probe_cache.get("result")
    at = _probe_cache.get("at")
    if cached is not None and not force and isinstance(at, float) and now - at < _PROBE_TTL_S:
        return dict(cached)  # type: ignore[arg-type]
    token = settings.telegram_bot_token.strip()
    from datetime import datetime, timezone

    result: dict = {
        "configured": True, "ok": False, "bot_username": None,
        "chat_id_masked": _mask_chat(settings.telegram_chat_id),
        "checked_at": datetime.now(timezone.utc).isoformat(), "error": None,
    }
    try:
        async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_S) as client:
            resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        if resp.status_code == 200 and resp.json().get("ok"):
            result["ok"] = True
            result["bot_username"] = resp.json().get("result", {}).get("username")
        elif resp.status_code == 401:
            result["error"] = "bot token rejected (401)"
        else:
            result["error"] = f"getMe HTTP {resp.status_code}"
    except Exception as e:  # noqa: BLE001
        result["error"] = f"unreachable: {type(e).__name__}"
    _probe_cache["result"] = dict(result)
    _probe_cache["at"] = now
    return result


async def send_now(text: str) -> tuple[bool, str]:
    """Send immediately, bypassing the per-key throttle — the admin's
    'Send test message' button only."""
    if not is_configured():
        return False, "not configured"
    return await _send(
        settings.telegram_bot_token.strip(), settings.telegram_chat_id.strip(), text
    )


def notify(key: str, message: str) -> None:
    """Queue a Telegram message, deduped per key. Safe to call from anywhere."""
    token = (settings.telegram_bot_token or "").strip()
    chat_id = (settings.telegram_chat_id or "").strip()
    if not token or not chat_id:
        return
    now = time.monotonic()
    last = _last_sent.get(key)
    if last is not None and now - last < settings.alert_min_interval_s:
        return
    _last_sent[key] = now
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop (sync context / interpreter teardown) — an alert is not worth
        # spinning one up for.
        return
    loop.create_task(_send(token, chat_id, message), name=f"notify-{key}")


async def _send(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    """(ok, detail). Never raises; the detail never contains the token."""
    try:
        async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_S) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": text[:4000]},
            )
            if resp.status_code != 200:
                detail = ""
                try:
                    detail = str(resp.json().get("description", ""))
                except Exception:  # noqa: BLE001
                    detail = resp.text[:120]
                log.warning("notify.rejected", status=resp.status_code)
                return False, f"HTTP {resp.status_code} {detail}".strip()
            return True, "sent"
    except Exception as e:  # noqa: BLE001 — alerting must never propagate
        log.warning("notify.failed", error=str(e))
        return False, f"unreachable: {type(e).__name__}"
