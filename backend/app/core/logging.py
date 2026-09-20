"""Structured logging configuration (structlog over stdlib).

Two renderers, chosen by ``LOG_FORMAT``:
  * ``pretty`` (default) — coloured ConsoleRenderer for dev terminals.
  * ``json`` — machine-parseable one-object-per-line, for production. The VPS
    sentinel and post-mortem grep both depend on this; the coloured renderer
    embeds ANSI escapes that break both.
"""
from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO", fmt: str | None = None) -> None:
    if fmt is None:
        # Lazy import to avoid a config<->logging cycle at module import time.
        from .config import settings

        fmt = settings.log_format
    renderer = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    # SECURITY: httpx logs every request at INFO as "HTTP Request: GET <full url>".
    # Several TrueData hosts take credentials as QUERY PARAMS, so that line put
    # the live vendor password in plaintext into the container logs (observed
    # 2026-08-21). Our own call sites scrub their URLs; httpx's logger does not,
    # and it sits outside structlog entirely. Silencing it also removes the
    # per-contract request spam that buries real feed events during a backfill.
    for _noisy in ("httpx", "httpcore", "websockets.client"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
