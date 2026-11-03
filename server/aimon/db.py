"""Datenbankzugriff: Pool, Migrationen, Timescale-Erkennung."""
from __future__ import annotations

import logging
import pathlib

import asyncpg

from .config import settings

log = logging.getLogger("aimon.db")

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations"

_pool: asyncpg.Pool | None = None
_timescale: bool = False


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            command_timeout=60,
            init=_init_conn,
        )
    return _pool


async def _init_conn(conn: asyncpg.Connection) -> None:
    # JSONB als dict rein/raus, ohne dass jeder Call json.dumps machen muss.
    import json

    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB-Pool nicht initialisiert")
    return _pool


def has_timescale() -> bool:
    return _timescale


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def migrate() -> None:
    """Schema anlegen. Timescale ist optional und wird erkannt, nicht erzwungen."""
    global _timescale
    p = await init_pool()
    core = (MIGRATIONS / "001_core.sql").read_text()
    async with p.acquire() as conn:
        await conn.execute(core)

        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            _timescale = True
        except Exception as exc:  # noqa: BLE001 - Extension fehlt oder keine Rechte
            log.info("Timescale nicht verfügbar (%s) – läuft als normales PostgreSQL", exc)
            _timescale = False

        if _timescale:
            ts_sql = (MIGRATIONS / "002_timescale.sql").read_text()
            # Jede Anweisung einzeln: einige sind idempotent-unfreundlich und
            # dürfen einzeln scheitern, ohne das Schema zu blockieren.
            for stmt in [s.strip() for s in ts_sql.split(";") if s.strip()]:
                if stmt.lstrip().startswith("--"):
                    continue
                try:
                    await conn.execute(stmt)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Timescale-Statement übersprungen: %s – %s",
                                stmt.split("\n")[0][:80], exc)

    log.info("Migration fertig (timescale=%s)", _timescale)


async def health() -> dict:
    try:
        p = await init_pool()
        async with p.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"db": "ok", "timescale": _timescale}
    except Exception as exc:  # noqa: BLE001
        return {"db": f"error: {exc}", "timescale": _timescale}
