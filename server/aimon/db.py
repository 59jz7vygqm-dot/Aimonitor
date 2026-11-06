"""Datenbankzugriff: Pool, Migrationen, Timescale-Erkennung."""
from __future__ import annotations

import asyncio
import logging
import pathlib
import time

import asyncpg
from urllib.parse import urlparse, urlunparse

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


async def wait_for_db(timeout_s: float = 120.0) -> None:
    """Auf die Datenbank warten, statt sofort zu sterben.

    Ohne das fällt der Server bei jedem DB-Neustart in eine Neustartschleife:
    Container "Running", Port tot, und der Grund steht nur im Log. Der
    Healthcheck der DB in docker-compose deckt den Erstart ab, nicht aber
    einen späteren Neustart der DB.
    """
    delay = 0.5
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            p = await init_pool()
            async with p.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return
        except Exception as exc:  # noqa: BLE001
            await close_pool()
            fatal = _fatal_reason(exc)
            if fatal:
                # Kaputte Zugangsdaten werden durch Warten nicht besser.
                raise RuntimeError(fatal) from exc
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Datenbank nach {timeout_s:.0f}s nicht erreichbar: {exc}\n"
                    f"Läuft der Dienst 'db' und ist er healthy? "
                    f"(docker compose ps)"
                ) from exc
            log.warning("Datenbank noch nicht bereit (%s) – neuer Versuch in %.1fs",
                        exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)


def _fatal_reason(exc: Exception) -> str | None:
    """Fehler, bei denen Warten sinnlos ist, mit einer brauchbaren Erklärung."""
    if isinstance(exc, ValueError):
        return (
            f"Die Datenbank-Adresse ist nicht lesbar: {exc}\n"
            f"Das passiert, wenn AIMON_DATABASE_URL von Hand zusammengesetzt "
            f"wurde und das Passwort Sonderzeichen wie '/' oder '@' enthält. "
            f"Stattdessen AIMON_DB_HOST / AIMON_DB_USER / AIMON_DB_PASSWORD / "
            f"AIMON_DB_NAME setzen – die werden korrekt kodiert."
        )
    if isinstance(exc, asyncpg.InvalidPasswordError):
        return (
            "Falsches Datenbank-Passwort.\n"
            "PostgreSQL behält das Passwort aus der ersten Initialisierung im "
            "Volume. Wurde POSTGRES_PASSWORD nachträglich geändert, passt es "
            "nicht mehr. Altes Passwort eintragen oder – wenn die Messwerte "
            "entbehrlich sind – 'docker compose down -v' und neu starten."
        )
    if isinstance(exc, asyncpg.InvalidCatalogNameError):
        return (
            f"Die Datenbank existiert nicht: {exc}\n"
            f"AIMON_DB_NAME bzw. POSTGRES_DB prüfen."
        )
    if isinstance(exc, asyncpg.InvalidAuthorizationSpecificationError):
        return f"Anmeldung abgelehnt: {exc}\nAIMON_DB_USER prüfen."
    return None


def masked_dsn() -> str:
    """Die Verbindungsadresse ohne Passwort, zum Loggen."""
    try:
        u = urlparse(settings.database_url)
        if u.password:
            netloc = f"{u.username or ''}:***@{u.hostname or ''}"
            if u.port:
                netloc += f":{u.port}"
            return urlunparse(u._replace(netloc=netloc))
        return settings.database_url
    except Exception:  # noqa: BLE001
        return "<nicht lesbar>"


async def migrate() -> None:
    """Schema anlegen. Timescale ist optional und wird erkannt, nicht erzwungen."""
    global _timescale
    # Beim Start protokollieren, womit tatsächlich verbunden wird. Ohne das
    # ist bei einem Startfehler nicht erkennbar, ob überhaupt die erwartete
    # Konfiguration im Container liegt.
    log.info("aimon-server %s (Build %s), Datenbank %s",
             settings.version, settings.build, masked_dsn())
    await wait_for_db()
    p = await init_pool()
    core = (MIGRATIONS / "001_core.sql").read_text()
    alerts = (MIGRATIONS / "003_alerts.sql").read_text()
    async with p.acquire() as conn:
        await conn.execute(core)
        await conn.execute(alerts)

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
