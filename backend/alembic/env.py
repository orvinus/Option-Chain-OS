"""Alembic environment.

Loads DB URL from the `.env` so we don't have to keep alembic.ini in sync.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool


def _env_file_path() -> Path:
    """``.env`` beside the exe when bundled; repo root in a normal checkout."""
    try:
        from app.core.config import PROJECT_ROOT

        return PROJECT_ROOT / ".env"
    except Exception:
        return Path(__file__).resolve().parents[2] / ".env"


load_dotenv(_env_file_path())

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

db_url = os.getenv("DB_URL_SYNC") or os.getenv("DB_URL") or config.get_main_option("sqlalchemy.url")
# Alembic uses the sync driver
if db_url and "+asyncpg" in db_url:
    db_url = db_url.replace("+asyncpg", "+psycopg")
config.set_main_option("sqlalchemy.url", db_url)

target_metadata = None  # raw SQL migrations


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
