"""Hintergrundarbeiter: Rollups verdichten, Alt-Daten löschen.

Bewusst keine Continuous Aggregates — so läuft derselbe Code auf
nacktem PostgreSQL und auf TimescaleDB.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from ..config import settings
from ..db import has_timescale, pool

log = logging.getLogger("aimon.rollup")

BUCKETS = (60, 3600)
_stats = {"rollup_runs": 0, "rows_written": 0, "deleted": 0, "lag_s": 0.0}


def stats() -> dict:
    return dict(_stats)


def _floor(ts: dt.datetime, bucket_s: int) -> dt.datetime:
    epoch = int(ts.timestamp())
    return dt.datetime.fromtimestamp(epoch - (epoch % bucket_s), tz=dt.timezone.utc)


async def rollup_once(bucket_s: int) -> int:
    """Alle abgeschlossenen Buckets seit dem letzten Lauf verdichten.

    Nur abgeschlossene Buckets, damit nichts halb Gefülltes als endgültig gilt.
    """
    now = dt.datetime.now(dt.timezone.utc)
    newest_closed = _floor(now, bucket_s)

    async with pool().acquire() as conn:
        state = await conn.fetchval(
            "SELECT last_bucket FROM rollup_state WHERE bucket_s = $1", bucket_s
        )
        if state is None:
            first = await conn.fetchval("SELECT min(ts) FROM samples")
            if first is None:
                # Noch keine Daten: den Wasserstand NICHT auf jetzt setzen.
                # Sonst würden alle Samples, die später mit älteren Zeitstempeln
                # eintreffen, nie verdichtet.
                return 0
            state = _floor(first, bucket_s)

        # Nachzügler: ein Agent puffert bei Serverausfall und schickt dann
        # Daten mit alten Zeitstempeln. Deshalb wird ein Stück hinter dem
        # Wasserstand erneut verarbeitet. Der Upsert macht das idempotent.
        start = _floor(
            state - dt.timedelta(seconds=settings.rollup_lookback_s), bucket_s
        )
        if state >= newest_closed:
            _stats["lag_s"] = 0.0
            # Trotzdem das Nachzügler-Fenster neu rechnen, aber nicht öfter als
            # nötig: nur wenn dort überhaupt Samples liegen.
            has_late = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM samples WHERE ts >= $1 AND ts < $2)",
                start, newest_closed,
            )
            if not has_late:
                return 0

        # Höchstens 2000 Buckets pro Lauf, sonst zieht ein Backlog die DB fest.
        end = min(newest_closed, start + dt.timedelta(seconds=bucket_s * 2000))
        if end <= start:
            return 0

        written = await conn.fetchval(
            """
            WITH agg AS (
                SELECT to_timestamp(floor(extract(epoch FROM ts) / $1) * $1) AS b,
                       series_id,
                       count(*)::int AS n,
                       sum(value)::float8 AS s,
                       min(value)::float8 AS mn,
                       max(value)::float8 AS mx,
                       (array_agg(value ORDER BY ts DESC))[1]::float8 AS lv
                FROM samples
                WHERE ts >= $2 AND ts < $3
                GROUP BY b, series_id
            ), ins AS (
                INSERT INTO samples_rollup (bucket_s, ts, series_id, n, sum, min, max, last)
                SELECT $1, b, series_id, n, s, mn, mx, lv FROM agg
                ON CONFLICT (bucket_s, series_id, ts) DO UPDATE SET
                    n = EXCLUDED.n, sum = EXCLUDED.sum, min = EXCLUDED.min,
                    max = EXCLUDED.max, last = EXCLUDED.last
                RETURNING 1
            )
            SELECT count(*) FROM ins
            """,
            bucket_s, start, end,
        )
        await conn.execute(
            "INSERT INTO rollup_state (bucket_s, last_bucket, updated_at) "
            "VALUES ($1,$2,now()) ON CONFLICT (bucket_s) DO UPDATE SET "
            "last_bucket = GREATEST(rollup_state.last_bucket, $2), updated_at = now()",
            bucket_s, end,
        )

    _stats["rollup_runs"] += 1
    _stats["rows_written"] += int(written or 0)
    _stats["lag_s"] = max(0.0, (newest_closed - end).total_seconds())
    return int(written or 0)


async def rollup_loop() -> None:
    while True:
        try:
            for b in BUCKETS:
                n = await rollup_once(b)
                if n:
                    log.debug("Rollup %ss: %d Zeilen", b, n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Rollup fehlgeschlagen")
        await asyncio.sleep(settings.rollup_interval_s)


async def retention_once() -> dict[str, int]:
    """Alt-Daten löschen. Timescale kann Chunks droppen, sonst DELETE in Häppchen."""
    out: dict[str, int] = {}
    plan = [
        ("samples", settings.raw_retention_days, "ts"),
        ("requests", settings.requests_retention_days, "ts"),
        ("probes", settings.probes_retention_days, "ts"),
    ]
    async with pool().acquire() as conn:
        for table, days, col in plan:
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
            if has_timescale():
                try:
                    await conn.execute(
                        f"SELECT drop_chunks('{table}', older_than => $1::timestamptz)", cutoff
                    )
                    out[table] = -1  # Chunks gedroppt, Zeilenzahl unbekannt
                    continue
                except Exception as exc:  # noqa: BLE001
                    log.warning("drop_chunks(%s) fehlgeschlagen, fallback DELETE: %s", table, exc)
            total = 0
            while True:
                n = await conn.fetchval(
                    f"WITH d AS (DELETE FROM {table} WHERE {col} < $1 RETURNING 1) "
                    f"SELECT count(*) FROM d",
                    cutoff,
                )
                total += int(n or 0)
                if not n:
                    break
            out[table] = total

        for bucket_s, days in ((60, settings.rollup_1m_days), (3600, settings.rollup_1h_days)):
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
            n = await conn.fetchval(
                "WITH d AS (DELETE FROM samples_rollup WHERE bucket_s = $1 AND ts < $2 "
                "RETURNING 1) SELECT count(*) FROM d",
                bucket_s, cutoff,
            )
            out[f"samples_rollup_{bucket_s}"] = int(n or 0)

        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=settings.events_retention_days)
        out["events"] = int(
            await conn.fetchval(
                "WITH d AS (DELETE FROM events WHERE ts < $1 RETURNING 1) SELECT count(*) FROM d",
                cutoff,
            ) or 0
        )

        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            days=settings.raw_query_retention_days
        )
        out["raw_queries"] = int(
            await conn.fetchval(
                "WITH d AS (DELETE FROM raw_queries WHERE ts < $1 RETURNING 1) "
                "SELECT count(*) FROM d",
                cutoff,
            ) or 0
        )

        await conn.execute("DELETE FROM ingest_keys WHERE seen_at < now() - INTERVAL '10 minutes'")

        # Serien, die seit zwei Retention-Perioden nichts mehr geliefert haben und
        # keine Samples mehr besitzen, aufräumen - sonst wächst das Dictionary endlos.
        out["series"] = int(
            await conn.fetchval(
                """
                WITH dead AS (
                    SELECT s.id FROM series s
                    WHERE s.last_seen < now() - ($1::int * INTERVAL '2 days')
                      AND NOT EXISTS (SELECT 1 FROM samples sm WHERE sm.series_id = s.id)
                    LIMIT 5000
                ), d AS (DELETE FROM series WHERE id IN (SELECT id FROM dead) RETURNING 1)
                SELECT count(*) FROM d
                """,
                settings.raw_retention_days,
            ) or 0
        )

    _stats["deleted"] += sum(v for v in out.values() if v > 0)
    log.info("Retention: %s", out)
    return out


async def retention_loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await retention_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Retention fehlgeschlagen")
        await asyncio.sleep(settings.retention_interval_s)
