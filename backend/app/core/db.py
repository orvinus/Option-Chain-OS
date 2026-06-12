"""Database engines and session factories.

We expose:
- async engine + ``AsyncSessionLocal`` used by FastAPI dependencies and the
  realtime ingestion path (non-blocking inserts).
- sync engine + ``SyncSessionLocal`` used by background bootstrap code that
  runs before the asyncio loop is fully wired (e.g. reading the latest auth
  session at startup).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from .config import settings


def _to_async(url: str) -> str:
    """psycopg has both sync and async drivers under the same dialect.

    SQLAlchemy expects ``postgresql+psycopg`` for sync and ``postgresql+psycopg_async``
    for async if using psycopg's async features. However ``psycopg`` 3 supports
    async natively under the ``psycopg`` dialect, so we leave the URL alone but
    wire it through ``create_async_engine``.
    """
    return url


async_engine = create_async_engine(
    _to_async(settings.db_url),
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    future=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine, expire_on_commit=False, class_=AsyncSession
)

sync_engine = create_engine(
    settings.db_url_sync,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    future=True,
)
SyncSessionLocal = sessionmaker(bind=sync_engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Async context manager yielding a session with commit/rollback semantics."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with AsyncSessionLocal() as session:
        yield session
