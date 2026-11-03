"""Query-API: Zeitreihen, letzte Werte, Latenz-Statistiken."""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..auth import Principal, require_read
from ..db import pool
from ..tsdb import query as q

router = APIRouter(prefix="/api/v1", tags=["query"])

DUR_RE = re.compile(r"^(\d+(?:\.\d+)?)([smhdw])$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_range(
    start: str | None, end: str | None, default_s: int = 3600
) -> tuple[dt.datetime, dt.datetime]:
    """`start`/`end` als RFC3339, Unix-Sekunden oder relativ (`-6h`, `now`)."""
    now = dt.datetime.now(dt.timezone.utc)

    def one(v: str | None, fallback: dt.datetime) -> dt.datetime:
        if not v or v == "now":
            return fallback
        if v.startswith("-"):
            m = DUR_RE.match(v[1:])
            if not m:
                raise HTTPException(400, f"ungültige Dauer: {v}")
            return now - dt.timedelta(seconds=float(m.group(1)) * _UNIT[m.group(2)])
        try:
            return dt.datetime.fromtimestamp(float(v), tz=dt.timezone.utc)
        except ValueError:
            pass
        s = v[:-1] + "+00:00" if v.endswith("Z") else v
        try:
            d = dt.datetime.fromisoformat(s)
        except ValueError as exc:
            raise HTTPException(400, f"ungültiger Zeitpunkt: {v}") from exc
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)

    e = one(end, now)
    s = one(start, e - dt.timedelta(seconds=default_s))
    if s >= e:
        raise HTTPException(400, "start muss vor end liegen")
    return s, e


@router.get("/query")
async def query_range(
    metric: str,
    start: str | None = None,
    end: str | None = None,
    agg: str = "avg",
    node: str | None = None,
    service: str | None = None,
    labels: str | None = Query(default=None, description='JSON-Objekt, z.B. {"gpu":"0"}'),
    max_points: int = 400,
    p: Principal = Depends(require_read),
) -> dict:
    if agg not in ("avg", "min", "max", "last", "sum", "rate"):
        raise HTTPException(400, f"unbekanntes agg: {agg}")
    lbl: dict[str, str] | None = None
    if labels:
        import json

        try:
            lbl = {str(k): str(v) for k, v in json.loads(labels).items()}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"labels ist kein JSON-Objekt: {exc}") from exc

    s, e = parse_range(start, end)
    async with pool().acquire() as conn:
        metas = await q.find_series(conn, metric, lbl, node, service)
        if not metas:
            return {"metric": metric, "series": [], "source": None,
                    "start": s, "end": e,
                    "note": "keine Serie gefunden – Metrikname oder Filter prüfen"}
        data, source, bucket = await q.range_query(
            conn, [m.id for m in metas], s, e, agg, min(max(max_points, 10), 5000)  # type: ignore[arg-type]
        )
    return {
        "metric": metric,
        "agg": agg,
        "source": source,
        "bucket_s": bucket,
        "start": s,
        "end": e,
        "series": [
            {
                "series_id": m.id,
                "labels": m.labels,
                "node": m.node,
                "service": m.service,
                "is_counter": m.is_counter,
                "points": [
                    [int(t.timestamp() * 1000), v] for t, v in data.get(m.id, [])
                ],
            }
            for m in metas
        ],
    }


@router.post("/query/multi")
async def query_multi(
    body: dict[str, Any] = Body(...), p: Principal = Depends(require_read)
) -> dict:
    """Mehrere Metriken in einem Roundtrip. Das UI holt ein Panel in einem Call."""
    reqs = body.get("queries")
    if not isinstance(reqs, list) or not reqs:
        raise HTTPException(400, "queries muss eine nicht-leere Liste sein")
    if len(reqs) > 40:
        raise HTTPException(400, "maximal 40 Queries pro Call")
    s, e = parse_range(body.get("start"), body.get("end"))
    max_points = min(max(int(body.get("max_points") or 400), 10), 5000)

    out = []
    async with pool().acquire() as conn:
        for r in reqs:
            metric = r.get("metric")
            if not metric:
                out.append({"error": "metric fehlt"})
                continue
            agg = r.get("agg") or "avg"
            metas = await q.find_series(
                conn, metric, r.get("labels"), r.get("node"), r.get("service")
            )
            if not metas:
                out.append({"metric": metric, "series": [], "source": None})
                continue
            data, source, bucket = await q.range_query(
                conn, [m.id for m in metas], s, e, agg, max_points  # type: ignore[arg-type]
            )
            out.append(
                {
                    "metric": metric,
                    "agg": agg,
                    "source": source,
                    "bucket_s": bucket,
                    "series": [
                        {
                            "series_id": m.id, "labels": m.labels, "node": m.node,
                            "service": m.service,
                            "points": [
                                [int(t.timestamp() * 1000), v] for t, v in data.get(m.id, [])
                            ],
                        }
                        for m in metas
                    ],
                }
            )
    return {"start": s, "end": e, "results": out}


@router.get("/latest")
async def latest(
    metric: str, labels: str | None = None, p: Principal = Depends(require_read)
) -> list[dict]:
    lbl = None
    if labels:
        import json

        lbl = json.loads(labels)
    async with pool().acquire() as conn:
        return await q.latest(conn, metric, lbl)


@router.get("/latency")
async def latency_stats(
    what: str = Query(default="ttft", pattern="^(ttft|total|queue|prompt_tokens|completion_tokens)$"),
    start: str | None = None,
    end: str | None = None,
    service: str | None = None,
    model: str | None = None,
    p: Principal = Depends(require_read),
) -> dict:
    """TTFT & Co. Exakt aus `requests`, sonst interpoliert aus den vLLM-Buckets.

    `source` in der Antwort sagt, welcher Weg genommen wurde.
    """
    s, e = parse_range(start, end, default_s=3600)
    col = {
        "ttft": "ttft_ms", "total": "total_ms", "queue": "queue_ms",
        "prompt_tokens": "prompt_tokens", "completion_tokens": "completion_tokens",
    }[what]
    hist = {
        "ttft": ("llm.ttft_bucket", 1000.0),
        "total": ("llm.e2e_bucket", 1000.0),
        "queue": ("llm.queue_time_bucket", 1000.0),
        "prompt_tokens": ("llm.prompt_len_bucket", 1.0),
        "completion_tokens": ("llm.gen_len_bucket", 1.0),
    }[what]

    async with pool().acquire() as conn:
        exact = await q.quantiles_from_requests(conn, s, e, col, service, model)
        if exact:
            exact.update({"what": what, "start": s, "end": e,
                          "service": service, "model": model})
            return exact
        if model:
            # Histogramme kennen keine Modell-Dimension; dann lieber leer melden,
            # als eine Zahl über alle Modelle als modellspezifisch auszugeben.
            return {"what": what, "source": None, "n": 0, "start": s, "end": e,
                    "note": "keine Request-Daten für dieses Modell; Histogramme sind "
                            "nicht nach Modell aufgelöst – aimon-tap liefert das"}
        approx = await q.quantiles_from_histogram(conn, hist[0], s, e, service, scale=hist[1])
        if approx:
            approx.update({"what": what, "start": s, "end": e, "service": service})
            return approx

    return {"what": what, "source": None, "n": 0, "start": s, "end": e,
            "note": "keine Daten: weder aimon-tap-Requests noch vLLM-Histogramme vorhanden"}


@router.get("/prompt-stats")
async def prompt_stats(
    start: str | None = None,
    end: str | None = None,
    service: str | None = None,
    p: Principal = Depends(require_read),
) -> dict:
    """Durchschnittliche Prompt-Länge, aus beiden Quellen, getrennt ausgewiesen."""
    s, e = parse_range(start, end, default_s=3600)
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT sv.key AS service, r.model,
                   count(*) AS n,
                   avg(r.prompt_tokens)::float8 AS avg_prompt_tokens,
                   avg(r.prompt_chars)::float8  AS avg_prompt_chars,
                   avg(r.completion_tokens)::float8 AS avg_completion_tokens,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY r.prompt_tokens)::float8
                       AS p50_prompt_tokens,
                   max(r.prompt_tokens) AS max_prompt_tokens
            FROM requests r LEFT JOIN services sv ON sv.id = r.service_id
            WHERE r.ts >= $1 AND r.ts < $2
              AND ($3::text IS NULL OR sv.key = $3)
            GROUP BY sv.key, r.model
            ORDER BY n DESC LIMIT 50
            """,
            s, e, service,
        )
        ratio = await q.counter_ratio(
            conn, "llm.prompt_len_sum", "llm.prompt_len_count", s, e, service
        )
    return {
        "start": s,
        "end": e,
        "from_requests": [dict(r) for r in rows],
        "from_vllm_counters": ratio,
        "note": "from_requests ist exakt (aimon-tap / instrumentierter Dienst); "
                "from_vllm_counters ist rate(sum)/rate(count) aus vLLM und deckt alle "
                "Requests ab, auch die ohne Tap.",
    }
