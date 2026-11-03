"""Series-Dictionary mit Prozess-Cache.

Eine Serie ist (metric, labels). Die Label-Menge wird kanonisch gehasht, damit
dieselbe Serie unabhängig von der Key-Reihenfolge dieselbe ID bekommt.
"""
from __future__ import annotations

import hashlib
import json

import asyncpg

# (metric, labels_hash) -> series_id
_cache: dict[tuple[str, str], int] = {}
_CACHE_MAX = 200_000

COUNTER_SUFFIXES = ("_total",)


def labels_hash(
    labels: dict[str, str], node_id: int | None = None, service_id: int | None = None
) -> str:
    """Identität einer Serie.

    Node und Service gehören zur Identität, nicht nur die Labels: sonst wäre
    `host.disk_used_bytes{mount="/"}` auf allen Maschinen dieselbe Serie und die
    Hosts würden sich gegenseitig überschreiben.
    """
    canon = json.dumps(
        {"l": labels or {}, "n": node_id, "s": service_id},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.blake2b(canon.encode(), digest_size=16).hexdigest()


def is_counter(metric: str) -> bool:
    return metric.endswith(COUNTER_SUFFIXES)


def cache_size() -> int:
    return len(_cache)


def cache_clear() -> None:
    _cache.clear()


async def resolve_many(
    conn: asyncpg.Connection,
    items: list[tuple[str, dict[str, str], int | None, int | None]],
) -> list[int]:
    """(metric, labels, node_id, service_id) -> series_id, in Eingabereihenfolge.

    Alles Unbekannte wird in einem Rutsch angelegt. Wiederholte Aufrufe treffen
    den Cache und machen keinen DB-Roundtrip.
    """
    keys: list[tuple[str, str]] = []
    for metric, labels, node_id, service_id in items:
        keys.append((metric, labels_hash(labels, node_id, service_id)))

    missing_idx = [i for i, k in enumerate(keys) if k not in _cache]
    if missing_idx:
        # Erst lesen (der Normalfall nach einem Neustart), dann anlegen.
        want = sorted({keys[i] for i in missing_idx})
        rows = await conn.fetch(
            "SELECT id, metric, labels_hash FROM series "
            "WHERE (metric, labels_hash) IN (SELECT * FROM unnest($1::text[], $2::text[]))",
            [m for m, _ in want],
            [h for _, h in want],
        )
        for r in rows:
            _cache[(r["metric"], r["labels_hash"])] = r["id"]

        still = [i for i in missing_idx if keys[i] not in _cache]
        if still:
            seen: set[tuple[str, str]] = set()
            batch: list[tuple] = []
            for i in still:
                k = keys[i]
                if k in seen:
                    continue
                seen.add(k)
                metric, labels, node_id, service_id = items[i]
                batch.append(
                    (metric, k[1], labels, node_id, service_id, is_counter(metric))
                )
            created = await conn.fetch(
                """
                INSERT INTO series (metric, labels_hash, labels, node_id, service_id, is_counter)
                SELECT * FROM unnest(
                    $1::text[], $2::text[], $3::jsonb[], $4::bigint[], $5::bigint[], $6::bool[])
                ON CONFLICT (metric, labels_hash) DO UPDATE
                    SET last_seen = now(),
                        node_id = COALESCE(EXCLUDED.node_id, series.node_id),
                        service_id = COALESCE(EXCLUDED.service_id, series.service_id)
                RETURNING id, metric, labels_hash
                """,
                [b[0] for b in batch],
                [b[1] for b in batch],
                [b[2] for b in batch],
                [b[3] for b in batch],
                [b[4] for b in batch],
                [b[5] for b in batch],
            )
            for r in created:
                _cache[(r["metric"], r["labels_hash"])] = r["id"]

    if len(_cache) > _CACHE_MAX:
        _cache.clear()

    out: list[int] = []
    for i, k in enumerate(keys):
        sid = _cache.get(k)
        if sid is None:
            # Darf nach dem Upsert nicht passieren; defensiv einzeln nachholen.
            metric, labels, node_id, service_id = items[i]
            sid = await conn.fetchval(
                "SELECT id FROM series WHERE metric=$1 AND labels_hash=$2", metric, k[1]
            )
            if sid is None:
                raise RuntimeError(f"Serie konnte nicht angelegt werden: {metric} {labels}")
            _cache[k] = sid
        out.append(sid)
    return out
