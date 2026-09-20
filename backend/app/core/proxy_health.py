"""Reachability probe for the SOCKS proxy the TrueData feed depends on.

On the VPS, TrueData is reachable ONLY through Cloudflare WARP's SOCKS5 port
(the vendor's edge drops hosting-ASN source addresses). That makes the proxy a
production dependency of the live feed, and a failure mode the platform has
never had before: the vendor is fine, our credentials are fine, and yet nothing
connects.

Distinguishing "the proxy is down" from "the session is wedged" matters, because
the responses are opposite. A wedge is cured by logoutRequest plus a wait; a dead
proxy is cured by neither, and every recovery attempt against it burns cool-downs
and restart budget for nothing. So the steward PARKS on proxy-down and alerts a
human, rather than escalating.

Cheap by design: a TCP connect to the proxy port, cached for a few seconds. It
deliberately does NOT reach the vendor — a full request would conflate vendor
outages with proxy outages, which is the exact conflation this module exists to
prevent.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

from .config import settings
from .logging import get_logger

log = get_logger("proxy_health")

CACHE_TTL_S = 5.0
CONNECT_TIMEOUT_S = 3.0

_last_check_at: float = 0.0
_last_result: bool = True
_last_error: str = ""


def _target() -> tuple[str, int] | None:
    raw = (settings.truedata_proxy or "").strip()
    if not raw:
        return None            # no proxy configured => nothing to be down
    p = urlparse(raw if "://" in raw else f"socks5://{raw}")
    if not p.hostname:
        return None
    return p.hostname, int(p.port or 1080)


def is_up() -> bool:
    """Last known state. Never blocks — safe on a hot path."""
    return _last_result


def last_error() -> str:
    return _last_error


async def probe(force: bool = False) -> bool:
    """TCP-connect to the proxy, at most once per CACHE_TTL_S."""
    global _last_check_at, _last_result, _last_error

    target = _target()
    if target is None:
        _last_result, _last_error = True, ""
        return True

    now = time.monotonic()
    if not force and (now - _last_check_at) < CACHE_TTL_S:
        return _last_result
    _last_check_at = now

    host, port = target
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT_S
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        if not _last_result:
            log.info("proxy_health.up", host=host, port=port)
        _last_result, _last_error = True, ""
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        if _last_result:
            log.error("proxy_health.down", host=host, port=port, error=msg)
        _last_result, _last_error = False, msg
    return _last_result


async def run_proxy_watch() -> None:
    """Background probe loop; spawned only when a proxy is configured."""
    if _target() is None:
        log.info("proxy_health.disabled", why="TRUEDATA_PROXY is empty")
        return
    while True:
        await probe(force=True)
        await asyncio.sleep(CACHE_TTL_S)


def snapshot() -> dict:
    target = _target()
    return {
        "configured": target is not None,
        "endpoint": f"{target[0]}:{target[1]}" if target else None,
        "up": _last_result,
        "error": _last_error or None,
    }
