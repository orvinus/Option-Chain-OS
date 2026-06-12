"""Entry point for the FastAPI backend.

On Windows with Python 3.12+, psycopg async requires SelectorEventLoop.
We use asyncio.run(..., loop_factory=...) — the modern API — instead of
the deprecated set_event_loop_policy(), so this works on Python 3.14 too.

``--migrate`` runs Alembic migrations then exits (same ``DB_URL`` / ``.env`` as the server).
"""
from __future__ import annotations

import argparse
import asyncio
import selectors
import sys
from pathlib import Path

import uvicorn


def _make_selector_loop() -> asyncio.SelectorEventLoop:
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


def _alembic_script_dir() -> Path:
    """Directory containing Alembic ``env.py`` (``backend/alembic`` in dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "bundle" / "alembic"
    return Path(__file__).resolve().parent / "alembic"


def run_migrate() -> None:
    from alembic import command
    from alembic.config import Config

    from app.core.config import settings

    url = settings.db_url_sync
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg")

    script_dir = _alembic_script_dir()
    if not (script_dir / "env.py").is_file():
        raise SystemExit(
            "Alembic migration files are missing inside the EXE bundle.\n"
            f"Expected: {script_dir / 'env.py'}\n"
            "Rebuild with: .\\scripts\\build-windows-exe.ps1"
        )

    if getattr(sys, "frozen", False):
        # One-file EXE: avoid relying on alembic.ini being unpacked to a fixed path.
        cfg = Config()
        cfg.set_main_option("script_location", str(script_dir))
    else:
        ini = Path(__file__).resolve().parent / "alembic.ini"
        if not ini.is_file():
            raise SystemExit(f"Alembic config not found: {ini}")
        cfg = Config(str(ini))

    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    print("Migrations applied (alembic upgrade head).", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="NIFTY OI Analytics backend")
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Run database migrations (Alembic) then exit",
    )
    args, uvicorn_args = parser.parse_known_args()

    if args.migrate:
        run_migrate()
        return

    # Import after migrate path so --migrate does not need uvicorn event loop setup.
    from app.core.config import settings

    config = uvicorn.Config(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
        log_level="info",
    )
    server = uvicorn.Server(config)

    if sys.platform == "win32":
        asyncio.run(server.serve(), loop_factory=_make_selector_loop)
    else:
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
