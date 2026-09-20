"""Versioned config store — load, validate, save, restore (§13, §11).

The live document is ``MAX(version)`` in ``algo_config_versions``. A save is
one INSERT of the whole validated document, so §13's atomicity holds by
construction: a crash between confirm and commit leaves the previous version
untouched, never a half-saved zone.

Every save writes the audit trail (§11.3): one ``config_change`` summary row
plus one row per changed leaf (dotted path, old → new), capped so a wholesale
restore doesn't flood the log — beyond the cap the summary row still carries
the full count.

Reads are cached for a few seconds: the orchestrator consults the config every
evaluation and the dashboard polls it, but the document only changes on save.
Saves invalidate the cache immediately (single-process runtime).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import structlog
from sqlalchemy import text

from ..core.db import AsyncSessionLocal
from .audit import audit, config_diff
from .config_models import AlgoConfig, default_config, validate_document

log = structlog.get_logger(__name__)

# Saves invalidate the cache explicitly (see save()), so the TTL only bounds
# cross-process drift. 3 s made the orchestrator's once-a-minute get_live()
# ALWAYS a miss (SQL + full document re-validation per pass) for nothing.
_CACHE_TTL_S = 30.0
_DIFF_AUDIT_CAP = 200


def _session_open_now() -> bool:
    """NSE session open right now (weekday window minus holidays) — the
    Config Lock's trigger condition."""
    from ..core.holidays import is_nse_holiday
    from ..core.time_utils import is_nse_regular_session_open, now_ist

    return is_nse_regular_session_open() and not is_nse_holiday(now_ist().date())


def _broker_configured() -> bool:
    """Runtime fact for §15's broker-connected check (lazy import — the
    broker package must stay optional for config-only tooling)."""
    try:
        from .broker.xts_interactive import get_interactive_client

        return get_interactive_client().configured
    except Exception:
        return False

_SELECT_LIVE_SQL = text(
    """
    SELECT version, config, saved_by, saved_at, note
    FROM algo_config_versions
    ORDER BY version DESC
    LIMIT 1
    """
)
_SELECT_VERSION_SQL = text(
    """
    SELECT version, config, saved_by, saved_at, note
    FROM algo_config_versions
    WHERE version = :version
    """
)
_SELECT_HISTORY_SQL = text(
    """
    SELECT version, saved_by, saved_at, note
    FROM algo_config_versions
    ORDER BY version DESC
    LIMIT :limit
    """
)
_INSERT_VERSION_SQL = text(
    """
    INSERT INTO algo_config_versions (config, saved_by, note)
    VALUES (CAST(:config AS JSONB), :saved_by, :note)
    RETURNING version, saved_at
    """
)


@dataclass(frozen=True)
class ConfigVersion:
    version: int
    config: AlgoConfig
    saved_by: str
    saved_at: str
    note: str


class ConfigSaveError(Exception):
    """Raised when §15 validation blocks a save. ``errors`` carries the list."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class AlgoConfigStore:
    def __init__(self) -> None:
        self._cached: Optional[ConfigVersion] = None
        self._cached_at: float = 0.0

    # ------------------------------------------------------------------ reads

    async def get_live(self) -> ConfigVersion:
        """The current document; seeds the Appendix-A default as version 1 on a
        virgin database so the engine's out-of-the-box behaviour matches the
        reviewed mockups (§18)."""
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < _CACHE_TTL_S:
            return self._cached
        async with AsyncSessionLocal() as session:
            row = (await session.execute(_SELECT_LIVE_SQL)).mappings().first()
        if row is None:
            seeded = await self._seed_default()
            return seeded
        cv = self._row_to_version(dict(row))
        self._cached, self._cached_at = cv, now
        return cv

    async def get_version(self, version: int) -> Optional[ConfigVersion]:
        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(_SELECT_VERSION_SQL, {"version": version})
            ).mappings().first()
        return self._row_to_version(dict(row)) if row else None

    async def history(self, limit: int = 25) -> list[dict[str, Any]]:
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    _SELECT_HISTORY_SQL, {"limit": max(1, min(limit, 200))}
                )
            ).mappings().all()
        return [
            {
                "version": r["version"],
                "saved_by": r["saved_by"],
                "saved_at": r["saved_at"].isoformat(),
                "note": r["note"],
            }
            for r in rows
        ]

    # ----------------------------------------------------------------- writes

    async def save(
        self,
        new_config: AlgoConfig,
        *,
        saved_by: str,
        note: str = "",
        user_id: Optional[int] = None,
    ) -> tuple[ConfigVersion, list[str]]:
        """Validate → insert one new version → audit the per-field diff.
        Returns (new version, §15 warnings). Raises ConfigSaveError on errors.
        """
        errors, warnings = validate_document(
            new_config, broker_configured=_broker_configured()
        )
        if errors:
            raise ConfigSaveError(errors)

        previous = await self.get_live()
        old_doc = previous.config.model_dump(by_alias=True)
        new_doc = new_config.model_dump(by_alias=True)

        # Compute the diff BEFORE writing — the market-hours lock needs it,
        # and the audit step below reuses it (never diff twice).
        changes = config_diff(old_doc, new_doc)

        # Config Lock: while the PREVIOUS live document has the lock on and
        # the NSE session is open (holidays excluded), refuse every save that
        # touches anything beyond the lock section itself — turning the lock
        # OFF must always remain possible.
        if previous.config.global_.config_lock.lock_market_hours and _session_open_now():
            if not all(p.startswith("global.config_lock.") for p, _, _ in changes):
                raise ConfigSaveError(
                    [
                        "Config Lock: the configuration is locked during market "
                        "hours. Turn the lock off in Security & Backups first, "
                        "or save after 15:30 IST."
                    ]
                )

        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    _INSERT_VERSION_SQL,
                    {
                        "config": json.dumps(new_doc, default=str),
                        "saved_by": saved_by,
                        "note": note,
                    },
                )
            ).mappings().one()
            await session.commit()

        version = int(row["version"])
        cv = ConfigVersion(
            version=version,
            config=new_config,
            saved_by=saved_by,
            saved_at=row["saved_at"].isoformat(),
            note=note,
        )
        self._cached, self._cached_at = cv, time.monotonic()

        await audit(
            "config_change",
            user_id=user_id,
            username=saved_by,
            scope="document",
            detail={
                "version": version,
                "changed_fields": len(changes),
                "note": note,
                "warnings": warnings,
            },
        )
        for path, before, after in changes[:_DIFF_AUDIT_CAP]:
            await audit(
                "config_change",
                user_id=user_id,
                username=saved_by,
                scope=_scope_of(path),
                field=path,
                old_value=before,
                new_value=after,
                detail={"version": version},
            )
        if len(changes) > _DIFF_AUDIT_CAP:
            log.info(
                "algo.config.diff_capped",
                total=len(changes), capped_at=_DIFF_AUDIT_CAP, version=version,
            )
        log.info(
            "algo.config.saved",
            version=version, by=saved_by, changed=len(changes), warnings=len(warnings),
        )
        # Config-change notifications were removed 2026-09-08: Telegram is the
        # only alert channel and it carries trade/kill/summary messages only.
        return cv, warnings

    async def restore(
        self, version: int, *, saved_by: str, user_id: Optional[int] = None
    ) -> tuple[ConfigVersion, list[str]]:
        """Restore = re-save an old document as a NEW version (audit trail never
        rewinds)."""
        old = await self.get_version(version)
        if old is None:
            raise ConfigSaveError([f"version {version} does not exist"])
        return await self.save(
            old.config,
            saved_by=saved_by,
            note=f"restore of v{version}",
            user_id=user_id,
        )

    # ---------------------------------------------------------------- helpers

    async def _seed_default(self) -> ConfigVersion:
        cfg = default_config()
        doc = cfg.model_dump(by_alias=True)
        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    _INSERT_VERSION_SQL,
                    {
                        "config": json.dumps(doc, default=str),
                        "saved_by": "system",
                        "note": "Appendix-A reference defaults (seed)",
                    },
                )
            ).mappings().one()
            await session.commit()
        cv = ConfigVersion(
            version=int(row["version"]),
            config=cfg,
            saved_by="system",
            saved_at=row["saved_at"].isoformat(),
            note="Appendix-A reference defaults (seed)",
        )
        self._cached, self._cached_at = cv, time.monotonic()
        log.info("algo.config.seeded", version=cv.version)
        return cv

    @staticmethod
    def _row_to_version(row: dict[str, Any]) -> ConfigVersion:
        raw = row["config"]
        doc = raw if isinstance(raw, dict) else json.loads(raw)
        return ConfigVersion(
            version=int(row["version"]),
            config=AlgoConfig.model_validate(doc),
            saved_by=row["saved_by"],
            saved_at=row["saved_at"].isoformat(),
            note=row["note"],
        )


def _scope_of(path: str) -> str:
    """'days.monday.zones.Z1.…' → 'monday/Z1'; 'days.monday.…' → 'monday';
    else 'global' (§11.3 wants which zone/day/global scope a change touched)."""
    parts = path.split(".")
    if parts[0] == "days" and len(parts) >= 2:
        if len(parts) >= 4 and parts[2] == "zones":
            return f"{parts[1]}/{parts[3]}"
        return parts[1]
    return "global"


_store: Optional[AlgoConfigStore] = None


def get_config_store() -> AlgoConfigStore:
    global _store
    if _store is None:
        _store = AlgoConfigStore()
    return _store
