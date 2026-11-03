"""Schreibpfad: einen validierten Batch in die DB bringen.

Ein Batch = eine Transaktion. Lieber einen ganzen Batch verlieren als
halb geschriebene Zustände, die später niemand erklären kann.
"""
from __future__ import annotations

import datetime as dt
import logging

import asyncpg

from ..config import settings
from ..models import Batch, IngestResult
from . import series as series_mod

log = logging.getLogger("aimon.writer")

_STATS = {"items": 0, "rejected": 0, "batches": 0}


def stats() -> dict:
    return dict(_STATS)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def upsert_node(conn: asyncpg.Connection, node) -> int:
    return await conn.fetchval(
        """
        INSERT INTO nodes (name, hostname, agent_version, labels, last_seen)
        VALUES ($1, $2, $3, $4, now())
        ON CONFLICT (name) DO UPDATE SET
            hostname      = COALESCE(EXCLUDED.hostname, nodes.hostname),
            agent_version = COALESCE(EXCLUDED.agent_version, nodes.agent_version),
            labels        = CASE WHEN EXCLUDED.labels = '{}'::jsonb
                                 THEN nodes.labels ELSE EXCLUDED.labels END,
            last_seen     = now()
        RETURNING id
        """,
        node.name,
        node.hostname,
        node.agent_version,
        node.labels,
    )


async def upsert_services(
    conn: asyncpg.Connection, node_id: int, services: list
) -> dict[str, int]:
    if not services:
        return {}
    rows = await conn.fetch(
        """
        INSERT INTO services (key, node_id, kind, name, base_url, version, labels, last_seen)
        SELECT u.*, now() FROM unnest($1::text[], $2::bigint[], $3::text[], $4::text[],
                             $5::text[], $6::text[], $7::jsonb[]) AS u
        ON CONFLICT (key) DO UPDATE SET
            node_id   = COALESCE(EXCLUDED.node_id, services.node_id),
            kind      = CASE WHEN EXCLUDED.kind = 'other' THEN services.kind ELSE EXCLUDED.kind END,
            name      = COALESCE(EXCLUDED.name, services.name),
            base_url  = COALESCE(EXCLUDED.base_url, services.base_url),
            version   = COALESCE(EXCLUDED.version, services.version),
            labels    = CASE WHEN EXCLUDED.labels = '{}'::jsonb
                             THEN services.labels ELSE EXCLUDED.labels END,
            last_seen = now()
        RETURNING id, key
        """,
        [s.key for s in services],
        [node_id] * len(services),
        [s.kind for s in services],
        [s.name for s in services],
        [s.base_url for s in services],
        [s.version for s in services],
        [s.labels for s in services],
    )
    return {r["key"]: r["id"] for r in rows}


async def resolve_service_keys(
    conn: asyncpg.Connection, keys: set[str], known: dict[str, int]
) -> dict[str, int]:
    """Service-Keys auflösen, die im Batch referenziert aber nicht deklariert wurden."""
    need = {k for k in keys if k and k not in known}
    if not need:
        return known
    rows = await conn.fetch("SELECT id, key FROM services WHERE key = ANY($1::text[])", list(need))
    out = dict(known)
    for r in rows:
        out[r["key"]] = r["id"]
    return out


async def write_batch(conn: asyncpg.Connection, batch: Batch) -> IngestResult:
    res = IngestResult()
    now = _now()

    for name in batch.unknown_fields():
        res.warnings.append(f"unbekanntes Feld ignoriert: {name}")

    node_id = await upsert_node(conn, batch.node)
    svc_ids = await upsert_services(conn, node_id, batch.services)

    referenced = (
        {s.service for s in batch.samples if s.service}
        | {r.service for r in batch.requests}
        | {e.service for e in batch.events if e.service}
        | {p.target_service for p in batch.probes if p.target_service}
        | {p.source_service for p in batch.probes if p.source_service}
    )
    svc_ids = await resolve_service_keys(conn, referenced, svc_ids)

    res.accepted += len(batch.services)

    # ---------------------------------------------------------------- samples
    if batch.samples:
        spec = []
        for s in batch.samples:
            sid = svc_ids.get(s.service) if s.service else None
            spec.append((s.metric, s.labels, node_id, sid))
        series_ids = await series_mod.resolve_many(conn, spec)

        rows = [
            (s.ts or now, sid, s.value)
            for s, sid in zip(batch.samples, series_ids)
        ]
        await conn.copy_records_to_table(
            "samples", records=rows, columns=["ts", "series_id", "value"]
        )
        await _update_last(conn, rows)
        await conn.execute(
            "UPDATE series SET last_seen = now() WHERE id = ANY($1::bigint[])",
            list({r[1] for r in rows}),
        )
        res.accepted += len(rows)

    # --------------------------------------------------------------- requests
    if batch.requests:
        rows = []
        for r in batch.requests:
            sid = svc_ids.get(r.service)
            if sid is None:
                res.rejected += 1
                res.errors.append(f"requests: unbekannter service '{r.service}'")
                continue
            rows.append(
                (
                    r.ts or now, sid, r.model, r.ttft_ms, r.total_ms, r.queue_ms,
                    r.prompt_tokens, r.prompt_chars, r.completion_tokens,
                    r.status, r.stream, r.client, r.route, r.error,
                )
            )
        if rows:
            await conn.copy_records_to_table(
                "requests",
                records=rows,
                columns=[
                    "ts", "service_id", "model", "ttft_ms", "total_ms", "queue_ms",
                    "prompt_tokens", "prompt_chars", "completion_tokens",
                    "status", "stream", "client", "route", "error",
                ],
            )
            res.accepted += len(rows)

    # ----------------------------------------------------------------- probes
    if batch.probes:
        rows = [
            (
                p.ts or now, node_id, p.target,
                svc_ids.get(p.target_service) if p.target_service else None,
                p.ok, p.latency_ms, p.http_status, p.error,
            )
            for p in batch.probes
        ]
        await conn.copy_records_to_table(
            "probes",
            records=rows,
            columns=[
                "ts", "from_node_id", "target", "target_svc_id",
                "ok", "latency_ms", "http_status", "error",
            ],
        )
        await _update_edges(conn, node_id, batch.probes, svc_ids)
        res.accepted += len(rows)

    # ----------------------------------------------------------------- events
    for e in batch.events:
        sid = svc_ids.get(e.service) if e.service else None
        await _write_event(conn, now, node_id, sid, e)
        res.accepted += 1

    _STATS["items"] += res.accepted
    _STATS["rejected"] += res.rejected
    _STATS["batches"] += 1
    return res


async def _update_last(conn: asyncpg.Connection, rows: list[tuple]) -> None:
    """series_last fortschreiben; behält den vorherigen Wert für Ratenrechnung."""
    latest: dict[int, tuple[dt.datetime, float]] = {}
    for ts, sid, val in rows:
        cur = latest.get(sid)
        if cur is None or ts >= cur[0]:
            latest[sid] = (ts, val)
    if not latest:
        return
    await conn.execute(
        """
        INSERT INTO series_last (series_id, ts, value)
        SELECT * FROM unnest($1::bigint[], $2::timestamptz[], $3::float8[])
        ON CONFLICT (series_id) DO UPDATE SET
            prev_ts    = CASE WHEN EXCLUDED.ts > series_last.ts
                              THEN series_last.ts ELSE series_last.prev_ts END,
            prev_value = CASE WHEN EXCLUDED.ts > series_last.ts
                              THEN series_last.value ELSE series_last.prev_value END,
            ts         = GREATEST(EXCLUDED.ts, series_last.ts),
            value      = CASE WHEN EXCLUDED.ts >= series_last.ts
                              THEN EXCLUDED.value ELSE series_last.value END
        """,
        list(latest.keys()),
        [v[0] for v in latest.values()],
        [v[1] for v in latest.values()],
    )


async def _update_edges(
    conn: asyncpg.Connection, node_id: int, probes: list, svc_ids: dict[str, int]
) -> None:
    for p in probes:
        dst = svc_ids.get(p.target_service) if p.target_service else None
        src_svc = svc_ids.get(p.source_service) if p.source_service else None
        # Ist der Zieldienst bekannt, identifiziert er die Kante. Sonst die URL.
        # Sonst entstünde pro geprobter URL desselben Dienstes eine eigene Kante.
        dst_target = "" if dst is not None else p.target
        await conn.execute(
            """
            INSERT INTO edges (src_node_id, src_svc_id, dst_svc_id, dst_target, kind,
                               ok, latency_ms, last_ok_ts, last_fail_ts, last_seen,
                               samples, fails)
            VALUES ($1, $2, $3, $4, 'probe', $5, $6,
                    CASE WHEN $5 THEN now() END, CASE WHEN NOT $5 THEN now() END,
                    now(), 1, CASE WHEN $5 THEN 0 ELSE 1 END)
            ON CONFLICT (src_node_id, src_svc_id, dst_svc_id, dst_target, kind) DO UPDATE SET
                ok           = EXCLUDED.ok,
                latency_ms   = EXCLUDED.latency_ms,
                last_ok_ts   = COALESCE(EXCLUDED.last_ok_ts, edges.last_ok_ts),
                last_fail_ts = COALESCE(EXCLUDED.last_fail_ts, edges.last_fail_ts),
                last_seen    = now(),
                samples      = edges.samples + 1,
                fails        = edges.fails + CASE WHEN EXCLUDED.ok THEN 0 ELSE 1 END
            """,
            node_id, src_svc, dst, dst_target, p.ok, p.latency_ms,
        )
        if dst is not None:
            # Beim ersten Scrape eines neuen Nodes war der Zieldienst noch nicht
            # registriert; die damals per URL angelegte Kante ist jetzt redundant.
            await conn.execute(
                "DELETE FROM edges WHERE src_node_id = $1 AND dst_svc_id IS NULL "
                "AND dst_target = $2 AND kind = 'probe'",
                node_id, p.target,
            )


async def record_traffic_edge(
    conn: asyncpg.Connection, src_svc_id: int | None, dst_svc_id: int, latency_ms: float | None
) -> None:
    """Kante aus beobachtetem Traffic (aimon-tap), nicht aus einer Probe."""
    await conn.execute(
        """
        INSERT INTO edges (src_node_id, src_svc_id, dst_svc_id, dst_target, kind,
                           ok, latency_ms, last_ok_ts, last_seen, samples)
        VALUES (NULL, $1, $2, '', 'traffic', true, $3, now(), now(), 1)
        ON CONFLICT (src_node_id, src_svc_id, dst_svc_id, dst_target, kind) DO UPDATE SET
            ok = true, latency_ms = EXCLUDED.latency_ms,
            last_ok_ts = now(), last_seen = now(), samples = edges.samples + 1
        """,
        src_svc_id, dst_svc_id, latency_ms,
    )


async def _write_event(
    conn: asyncpg.Connection,
    now: dt.datetime,
    node_id: int,
    service_id: int | None,
    e,
) -> None:
    ts = e.ts or now
    detail = dict(e.detail or {})

    raw = detail.pop("query_raw", None)
    if raw is not None and not settings.allow_raw_queries:
        detail["query_raw_dropped"] = True
        raw = None

    if e.dedup_key:
        existing = await conn.fetchrow(
            """
            SELECT id FROM events
            WHERE dedup_key = $1 AND ts > now() - INTERVAL '60 seconds'
            ORDER BY ts DESC LIMIT 1
            """,
            e.dedup_key,
        )
        if existing:
            await conn.execute(
                "UPDATE events SET count = count + 1, last_ts = $2 WHERE id = $1",
                existing["id"], ts,
            )
            return

    event_id = await conn.fetchval(
        """
        INSERT INTO events (ts, node_id, service_id, severity, kind, title, detail,
                            dedup_key, last_ts)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$1)
        RETURNING id
        """,
        ts, node_id, service_id, e.severity, e.kind, e.title, detail, e.dedup_key,
    )

    if raw is not None:
        await conn.execute(
            "INSERT INTO raw_queries (event_id, ts, query_raw) VALUES ($1,$2,$3)",
            event_id, ts, str(raw)[:4000],
        )
