"""Lesepfad: Range-Queries, Raten, Quantile.

Grundsatz: die Antwort sagt immer, woher eine Zahl kommt. `source` ist
`raw` | `rollup_60` | `rollup_3600` für Zeitreihen und `requests` | `histogram`
für Quantile. Keine stillen Schätzungen.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

Agg = Literal["avg", "min", "max", "last", "sum", "rate"]


@dataclass
class SeriesMeta:
    id: int
    metric: str
    labels: dict[str, str]
    node: str | None
    service: str | None
    is_counter: bool


def pick_resolution(start: dt.datetime, end: dt.datetime, max_points: int) -> tuple[int, str]:
    """Bucketgröße und Quelle wählen. Lieber groblösig als 100k Punkte ins Browser."""
    span = max(1.0, (end - start).total_seconds())
    ideal = span / max(1, max_points)
    if ideal <= 30:
        return 0, "raw"
    if ideal <= 1800:
        return 60, "rollup_60"
    return 3600, "rollup_3600"


async def find_series(
    conn: asyncpg.Connection,
    metric: str,
    labels: dict[str, str] | None = None,
    node: str | None = None,
    service: str | None = None,
    limit: int = 500,
) -> list[SeriesMeta]:
    sql = [
        """
        SELECT s.id, s.metric, s.labels, s.is_counter, n.name AS node, sv.key AS service
        FROM series s
        LEFT JOIN nodes n ON n.id = s.node_id
        LEFT JOIN services sv ON sv.id = s.service_id
        WHERE s.metric = $1
        """
    ]
    args: list[Any] = [metric]
    if labels:
        args.append(labels)
        sql.append(f"AND s.labels @> ${len(args)}::jsonb")
    if node:
        args.append(node)
        sql.append(f"AND n.name = ${len(args)}")
    if service:
        args.append(service)
        sql.append(f"AND sv.key = ${len(args)}")
    args.append(limit)
    sql.append(f"ORDER BY s.id LIMIT ${len(args)}")

    rows = await conn.fetch("\n".join(sql), *args)
    return [
        SeriesMeta(
            id=r["id"], metric=r["metric"], labels=r["labels"] or {},
            node=r["node"], service=r["service"], is_counter=r["is_counter"],
        )
        for r in rows
    ]


async def range_query(
    conn: asyncpg.Connection,
    series_ids: list[int],
    start: dt.datetime,
    end: dt.datetime,
    agg: Agg = "avg",
    max_points: int = 400,
) -> tuple[dict[int, list[tuple[dt.datetime, float | None]]], str, int]:
    """Zeitreihen je series_id. Rückgabe: (daten, source, bucket_s)."""
    if not series_ids:
        return {}, "raw", 0

    bucket_s, source = pick_resolution(start, end, max_points)
    want_rate = agg == "rate"
    base_agg = "sum" if want_rate else agg

    if bucket_s == 0:
        step = max(1, int((end - start).total_seconds() / max(1, max_points)))
        expr = {
            "avg": "avg(value)", "min": "min(value)", "max": "max(value)",
            "sum": "sum(value)",
            "last": "(array_agg(value ORDER BY ts DESC))[1]",
        }[base_agg if base_agg in ("avg", "min", "max", "sum", "last") else "avg"]
        rows = await conn.fetch(
            f"""
            SELECT series_id,
                   to_timestamp(floor(extract(epoch FROM ts) / $4) * $4) AS bucket,
                   {expr} AS v,
                   max(value) AS vmax, min(value) AS vmin,
                   (array_agg(value ORDER BY ts DESC))[1] AS vlast,
                   (array_agg(value ORDER BY ts ASC))[1] AS vfirst,
                   count(*) AS n
            FROM samples
            WHERE series_id = ANY($1::bigint[]) AND ts >= $2 AND ts < $3
            GROUP BY series_id, bucket
            ORDER BY bucket
            """,
            series_ids, start, end, step,
        )
        bucket_s = step
    else:
        col = {
            "avg": "sum / GREATEST(n,1)", "min": "min", "max": "max",
            "last": "last", "sum": "sum",
        }[base_agg if base_agg in ("avg", "min", "max", "last", "sum") else "avg"]
        rows = await conn.fetch(
            f"""
            SELECT series_id, ts AS bucket, ({col})::float8 AS v,
                   max AS vmax, min AS vmin, last AS vlast, last AS vfirst, n
            FROM samples_rollup
            WHERE bucket_s = $4 AND series_id = ANY($1::bigint[])
              AND ts >= $2 AND ts < $3
            ORDER BY ts
            """,
            series_ids, start, end, bucket_s,
        )

    out: dict[int, list[tuple[dt.datetime, float | None]]] = {sid: [] for sid in series_ids}
    if not want_rate:
        for r in rows:
            out[r["series_id"]].append((r["bucket"], r["v"]))
        return out, source, bucket_s

    # Rate: Differenz des Counters pro Bucket, Reset erkannt (Wert sinkt -> Neustart).
    per: dict[int, list[asyncpg.Record]] = {}
    for r in rows:
        per.setdefault(r["series_id"], []).append(r)
    for sid, rs in per.items():
        prev: float | None = None
        for r in rs:
            cur_last = r["vlast"]
            cur_first = r["vfirst"]
            if prev is None:
                delta = (cur_last - cur_first) if cur_last is not None else None
            elif cur_last is None:
                delta = None
            elif cur_last < prev:
                # Counter-Reset: nur den Anstieg innerhalb des Buckets zählen.
                delta = max(0.0, cur_last - min(cur_first, cur_last))
            else:
                delta = cur_last - prev
            val = (delta / bucket_s) if (delta is not None and bucket_s > 0) else None
            out[sid].append((r["bucket"], val))
            if cur_last is not None:
                prev = cur_last
    return out, source, bucket_s


async def latest(
    conn: asyncpg.Connection, metric: str, labels: dict[str, str] | None = None
) -> list[dict]:
    """Letzter Wert je Serie einer Metrik, plus Rate für Counter."""
    args: list[Any] = [metric]
    extra = ""
    if labels:
        args.append(labels)
        extra = "AND s.labels @> $2::jsonb"
    rows = await conn.fetch(
        f"""
        SELECT s.id, s.metric, s.labels, s.is_counter,
               n.name AS node, sv.key AS service, sv.kind AS service_kind,
               l.ts, l.value, l.prev_ts, l.prev_value
        FROM series s
        JOIN series_last l ON l.series_id = s.id
        LEFT JOIN nodes n ON n.id = s.node_id
        LEFT JOIN services sv ON sv.id = s.service_id
        WHERE s.metric = $1 {extra}
        ORDER BY n.name NULLS LAST, s.labels
        """,
        *args,
    )
    out = []
    for r in rows:
        rate = None
        if r["is_counter"] and r["prev_ts"] is not None:
            dts = (r["ts"] - r["prev_ts"]).total_seconds()
            if dts > 0 and r["value"] >= r["prev_value"]:
                rate = (r["value"] - r["prev_value"]) / dts
        out.append(
            {
                "series_id": r["id"], "metric": r["metric"], "labels": r["labels"] or {},
                "node": r["node"], "service": r["service"], "service_kind": r["service_kind"],
                "ts": r["ts"], "value": r["value"], "rate": rate,
            }
        )
    return out


# ------------------------------------------------------------------- Quantile


def _hist_quantile(buckets: list[tuple[float, float]], q: float) -> float | None:
    """Lineare Interpolation im getroffenen Bucket, Prometheus-Semantik.

    buckets: [(le, kumulative Anzahl)] aufsteigend nach le. Der +Inf-Bucket darf
    fehlen; dann ist der höchste endliche le die Obergrenze.
    """
    bs = sorted((le, c) for le, c in buckets if c is not None)
    if not bs:
        return None
    total = bs[-1][1]
    if total <= 0:
        return None
    target = q * total
    prev_le, prev_c = 0.0, 0.0
    for le, c in bs:
        if c >= target:
            if math.isinf(le):
                return prev_le if prev_le > 0 else None
            if c == prev_c:
                return le
            frac = (target - prev_c) / (c - prev_c)
            return prev_le + frac * (le - prev_le)
        prev_le, prev_c = le, c
    return bs[-1][0]


async def quantiles_from_requests(
    conn: asyncpg.Connection,
    start: dt.datetime,
    end: dt.datetime,
    column: str = "ttft_ms",
    service: str | None = None,
    model: str | None = None,
    qs: tuple[float, ...] = (0.5, 0.9, 0.99),
) -> dict | None:
    if column not in ("ttft_ms", "total_ms", "queue_ms", "prompt_tokens", "completion_tokens"):
        raise ValueError("unerlaubte Spalte")
    args: list[Any] = [start, end]
    where = [f"{column} IS NOT NULL", "ts >= $1", "ts < $2"]
    if service:
        args.append(service)
        where.append(f"sv.key = ${len(args)}")
    if model:
        args.append(model)
        where.append(f"r.model = ${len(args)}")
    args.append(list(qs))
    row = await conn.fetchrow(
        f"""
        SELECT count(*) AS n,
               avg({column})::float8 AS avg,
               min({column})::float8 AS min,
               max({column})::float8 AS max,
               percentile_cont(${len(args)}::float8[])
                   WITHIN GROUP (ORDER BY {column}) AS pct
        FROM requests r LEFT JOIN services sv ON sv.id = r.service_id
        WHERE {' AND '.join(where)}
        """,
        *args,
    )
    if not row or not row["n"]:
        return None
    pct = row["pct"] or []
    return {
        "source": "requests",
        "n": row["n"],
        "avg": row["avg"],
        "min": row["min"],
        "max": row["max"],
        "quantiles": {str(q): (pct[i] if i < len(pct) else None) for i, q in enumerate(qs)},
    }


async def quantiles_from_histogram(
    conn: asyncpg.Connection,
    bucket_metric: str,
    start: dt.datetime,
    end: dt.datetime,
    service: str | None = None,
    qs: tuple[float, ...] = (0.5, 0.9, 0.99),
    scale: float = 1000.0,
) -> dict | None:
    """Quantile aus kumulativen Buckets (vLLM-Histogramme).

    Rechnet mit dem Zuwachs im Fenster, nicht mit dem Absolutwert — sonst wäre
    es das Quantil seit Prozessstart.
    """
    args: list[Any] = [bucket_metric, start, end]
    extra = ""
    if service:
        args.append(service)
        extra = f"AND sv.key = ${len(args)}"
    rows = await conn.fetch(
        f"""
        WITH s AS (
            SELECT sr.id, sr.labels->>'le' AS le
            FROM series sr LEFT JOIN services sv ON sv.id = sr.service_id
            WHERE sr.metric = $1 {extra}
        ),
        w AS (
            SELECT sm.series_id,
                   (array_agg(sm.value ORDER BY sm.ts ASC))[1]  AS first_v,
                   (array_agg(sm.value ORDER BY sm.ts DESC))[1] AS last_v
            FROM samples sm
            WHERE sm.series_id IN (SELECT id FROM s) AND sm.ts >= $2 AND sm.ts < $3
            GROUP BY sm.series_id
        )
        SELECT s.le, sum(GREATEST(w.last_v - w.first_v, 0))::float8 AS delta
        FROM w JOIN s ON s.id = w.series_id
        GROUP BY s.le
        """,
        *args,
    )
    buckets: list[tuple[float, float]] = []
    for r in rows:
        le_s = r["le"]
        if le_s is None:
            continue
        try:
            le = float("inf") if le_s.lower() in ("+inf", "inf") else float(le_s)
        except ValueError:
            continue
        buckets.append((le, float(r["delta"] or 0.0)))
    if not buckets:
        return None
    total = max((c for _, c in buckets), default=0.0)
    if total <= 0:
        return None
    out = {}
    for q in qs:
        v = _hist_quantile(buckets, q)
        out[str(q)] = (v * scale) if v is not None else None
    return {
        "source": "histogram",
        "n": int(total),
        "quantiles": out,
        "note": "interpoliert aus Histogramm-Buckets; Genauigkeit begrenzt durch Bucket-Grenzen",
    }


async def counter_ratio(
    conn: asyncpg.Connection,
    sum_metric: str,
    count_metric: str,
    start: dt.datetime,
    end: dt.datetime,
    service: str | None = None,
    scale: float = 1.0,
) -> dict | None:
    """rate(sum) / rate(count) — z. B. Durchschnitts-TTFT oder Prompt-Länge."""
    async def delta(metric: str) -> float | None:
        args: list[Any] = [metric, start, end]
        extra = ""
        if service:
            args.append(service)
            extra = f"AND sv.key = ${len(args)}"
        row = await conn.fetchrow(
            f"""
            WITH s AS (
                SELECT sr.id FROM series sr LEFT JOIN services sv ON sv.id = sr.service_id
                WHERE sr.metric = $1 {extra}
            ), w AS (
                SELECT sm.series_id,
                       (array_agg(sm.value ORDER BY sm.ts ASC))[1]  AS fv,
                       (array_agg(sm.value ORDER BY sm.ts DESC))[1] AS lv
                FROM samples sm
                WHERE sm.series_id IN (SELECT id FROM s) AND sm.ts >= $2 AND sm.ts < $3
                GROUP BY sm.series_id
            )
            SELECT sum(GREATEST(lv - fv, 0))::float8 AS d FROM w
            """,
            *args,
        )
        return row["d"] if row else None

    ds = await delta(sum_metric)
    dc = await delta(count_metric)
    if ds is None or dc is None or dc <= 0:
        return None
    return {"source": "counter_ratio", "value": (ds / dc) * scale, "n": int(dc)}
