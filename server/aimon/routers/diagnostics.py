"""Incidents, Events und der Prompt-Generator."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from ..auth import Principal, require_read
from ..db import pool
from ..diagnostics import prompt as prompt_mod
from ..diagnostics import rules as rules_mod

router = APIRouter(prefix="/api/v1", tags=["diagnostics"])


@router.get("/incidents")
async def list_incidents(
    open_only: bool = True,
    limit: int = 200,
    p: Principal = Depends(require_read),
) -> list[dict]:
    clause = "WHERE ended_at IS NULL" if open_only else ""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT *, extract(epoch FROM (now()-started_at))::float8 AS age_s
            FROM incidents {clause}
            ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1
                     WHEN 'warning' THEN 2 ELSE 3 END, started_at DESC
            LIMIT $1
            """,
            min(limit, 1000),
        )
    return [dict(r) for r in rows]


@router.post("/incidents/{incident_id}/ack")
async def ack_incident(incident_id: int, p: Principal = Depends(require_read)) -> dict:
    async with pool().acquire() as conn:
        res = await conn.execute(
            "UPDATE incidents SET acknowledged = true WHERE id = $1", incident_id
        )
    return {"acknowledged": res.endswith("1")}


@router.post("/incidents/evaluate")
async def evaluate_now(p: Principal = Depends(require_read)) -> dict:
    """Regelwerk sofort laufen lassen, statt aufs Intervall zu warten."""
    findings = await rules_mod.evaluate()
    res = await rules_mod.reconcile(findings)
    return {"findings": len(findings), **res}


@router.get("/events")
async def list_events(
    since: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
    service: str | None = None,
    search: str | None = None,
    limit: int = 200,
    offset: int = 0,
    p: Principal = Depends(require_read),
) -> dict:
    from .query import parse_range

    start, end = parse_range(since, None, default_s=3600)
    where = ["e.ts >= $1", "e.ts < $2"]
    args: list = [start, end]
    if kind:
        args.append(kind if "%" in kind else f"{kind}%")
        where.append(f"e.kind LIKE ${len(args)}")
    if severity:
        args.append([s.strip() for s in severity.split(",")])
        where.append(f"e.severity = ANY(${len(args)}::text[])")
    if service:
        args.append(service)
        where.append(f"sv.key = ${len(args)}")
    if search:
        args.append(f"%{search}%")
        where.append(f"(e.title ILIKE ${len(args)} OR e.detail::text ILIKE ${len(args)})")
    args.append(min(limit, 2000))
    args.append(max(offset, 0))

    async with pool().acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT e.id, e.ts, e.severity, e.kind, e.title, e.detail, e.count,
                   sv.key AS service, n.name AS node
            FROM events e
            LEFT JOIN services sv ON sv.id = e.service_id
            LEFT JOIN nodes n ON n.id = e.node_id
            WHERE {' AND '.join(where)}
            ORDER BY e.ts DESC LIMIT ${len(args)-1} OFFSET ${len(args)}
            """,
            *args,
        )
        total = await conn.fetchval(
            f"""
            SELECT count(*) FROM events e
            LEFT JOIN services sv ON sv.id = e.service_id
            WHERE {' AND '.join(where)}
            """,
            *args[:-2],
        )
    return {"total": total, "start": start, "end": end, "events": [dict(r) for r in rows]}


@router.get("/diagnostics/prompt")
async def diagnostic_prompt(
    window_s: int = Query(default=3600, ge=60, le=604800),
    scope: str | None = Query(default=None, pattern="^(incident|service|node)$"),
    subject: str | None = None,
    redact: bool = False,
    include_evidence: bool = True,
    note: str | None = None,
    p: Principal = Depends(require_read),
) -> dict:
    """Kopierbarer Fehlerbericht als Markdown.

    `scope=incident&subject=<id>` beschränkt auf einen Incident,
    `redact=true` ersetzt Hostnamen/IPs durch Platzhalter,
    `note` ist die eigene Beobachtung, die oben im Text zitiert wird.
    """
    if scope and not subject:
        raise HTTPException(400, "scope ohne subject")
    return await prompt_mod.generate(
        window_s=window_s, scope=scope, subject=subject, redact=redact,
        include_evidence=include_evidence, user_note=note,
    )


@router.post("/diagnostics/prompt", response_class=PlainTextResponse)
async def diagnostic_prompt_text(
    body: dict = Body(default_factory=dict), p: Principal = Depends(require_read)
) -> str:
    """Gleicher Inhalt, aber als reiner Text — direkt in die Zwischenablage."""
    res = await prompt_mod.generate(
        window_s=int(body.get("window_s") or 3600),
        scope=body.get("scope"),
        subject=body.get("subject"),
        redact=bool(body.get("redact")),
        include_evidence=bool(body.get("include_evidence", True)),
        user_note=body.get("note"),
    )
    return res["prompt"]


@router.get("/privacy/blocked")
async def privacy_blocked(
    since: str | None = None,
    category: str | None = None,
    action: str = "blocked",
    limit: int = 200,
    p: Principal = Depends(require_read),
) -> dict:
    """Log der geblockten/bereinigten Suchanfragen, plus Verteilung je Kategorie."""
    from .query import parse_range

    start, end = parse_range(since, None, default_s=86400)
    actions = [a.strip() for a in action.split(",") if a.strip()]
    kinds = [f"privacy.{a}" for a in actions]

    async with pool().acquire() as conn:
        args: list = [start, end, kinds]
        extra = ""
        if category:
            args.append(category)
            extra = f"AND e.detail->>'category' = ${len(args)}"
        args.append(min(limit, 2000))
        rows = await conn.fetch(
            f"""
            SELECT e.id, e.ts, e.kind, e.severity, e.title, e.count,
                   e.detail->>'category'        AS category,
                   e.detail->>'rule'            AS rule,
                   e.detail->>'query_redacted'  AS query_redacted,
                   e.detail->>'query_sha256'    AS query_sha256,
                   (e.detail->>'query_len')::int    AS query_len,
                   (e.detail->>'confidence')::float AS confidence,
                   e.detail->>'client_id'       AS client_id,
                   e.detail->'matched_spans'    AS matched_spans,
                   (e.detail->>'query_raw_dropped')::bool AS raw_dropped,
                   sv.key AS service,
                   (SELECT q.query_raw FROM raw_queries q WHERE q.event_id = e.id) AS query_raw
            FROM events e LEFT JOIN services sv ON sv.id = e.service_id
            WHERE e.ts >= $1 AND e.ts < $2 AND e.kind = ANY($3::text[]) {extra}
            ORDER BY e.ts DESC LIMIT ${len(args)}
            """,
            *args,
        )
        by_cat = await conn.fetch(
            """
            SELECT e.kind, e.detail->>'category' AS category,
                   e.detail->>'rule' AS rule,
                   count(*) AS events, sum(e.count) AS occurrences
            FROM events e
            WHERE e.ts >= $1 AND e.ts < $2 AND e.kind LIKE 'privacy.%'
            GROUP BY e.kind, category, rule ORDER BY occurrences DESC
            """,
            start, end,
        )
        totals = await conn.fetch(
            """
            SELECT s.metric, s.labels->>'category' AS category, sv.key AS service,
                   l.value
            FROM series s JOIN series_last l ON l.series_id = s.id
            JOIN services sv ON sv.id = s.service_id
            WHERE s.metric IN ('privacy.requests_total','privacy.blocked_total',
                               'privacy.redacted_total','privacy.allowed_total',
                               'privacy.block_rate_pct','privacy.errors_total')
            """
        )
    return {
        "start": start,
        "end": end,
        "log": [dict(r) for r in rows],
        "by_category": [dict(r) for r in by_cat],
        "counters": [dict(r) for r in totals],
        "hinweis": "query_redacted ist die bereinigte Fassung nach dem Filter. "
                   "query_raw erscheint nur, wenn der Server mit "
                   "AIMON_ALLOW_RAW_QUERIES=true läuft und der Proxy es sendet.",
    }


@router.get("/openterminal/summary")
async def openterminal_summary(
    window_s: int = 3600, p: Principal = Depends(require_read)
) -> dict:
    """Instanzzahl, Sessions, Headroom und die Scale-Historie."""
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=max(300, window_s))
    async with pool().acquire() as conn:
        cur = await conn.fetch(
            """
            SELECT sv.key AS service, s.metric, l.value, l.ts
            FROM series s JOIN series_last l ON l.series_id = s.id
            JOIN services sv ON sv.id = s.service_id
            WHERE s.metric LIKE 'openterminal.%' AND s.labels = '{}'::jsonb
            """
        )
        inst = await conn.fetch(
            """
            SELECT sv.key AS service, s.labels->>'instance' AS instance,
                   s.labels->>'state' AS state, s.metric, l.value, l.ts
            FROM series s JOIN series_last l ON l.series_id = s.id
            JOIN services sv ON sv.id = s.service_id
            WHERE s.metric LIKE 'openterminal.instance%'
              AND l.ts > now() - INTERVAL '5 minutes'
            """
        )
        scale_events = await conn.fetch(
            """
            SELECT e.ts, e.kind, e.title, e.detail, sv.key AS service
            FROM events e LEFT JOIN services sv ON sv.id = e.service_id
            WHERE e.ts > $1 AND e.kind LIKE 'scale.%'
            ORDER BY e.ts DESC LIMIT 100
            """,
            start,
        )
        peak = await conn.fetch(
            """
            SELECT sv.key AS service, s.metric,
                   max(sm.value)::float8 AS peak, avg(sm.value)::float8 AS avg,
                   min(sm.value)::float8 AS low
            FROM samples sm JOIN series s ON s.id = sm.series_id
            JOIN services sv ON sv.id = s.service_id
            WHERE sm.ts > $1 AND s.metric IN
                ('openterminal.sessions_active','openterminal.instances_running',
                 'openterminal.headroom_pct','openterminal.saturation_pct',
                 'openterminal.sessions_queued')
            GROUP BY sv.key, s.metric
            """,
            start,
        )

    per: dict[str, dict] = {}
    for r in cur:
        per.setdefault(r["service"], {"current": {}, "window": {}, "instances": []})
        per[r["service"]]["current"][r["metric"]] = r["value"]
    for r in peak:
        per.setdefault(r["service"], {"current": {}, "window": {}, "instances": []})
        per[r["service"]]["window"][r["metric"]] = {
            "peak": r["peak"], "avg": r["avg"], "low": r["low"]
        }
    inst_map: dict[tuple[str, str], dict] = {}
    for r in inst:
        key = (r["service"], r["instance"])
        d = inst_map.setdefault(key, {"instance": r["instance"], "state": r["state"]})
        d[r["metric"].replace("openterminal.instance_", "")] = r["value"]
    for (svc, _i), d in inst_map.items():
        per.setdefault(svc, {"current": {}, "window": {}, "instances": []})
        per[svc]["instances"].append(d)

    return {
        "window_s": window_s,
        "proxies": per,
        "scale_events": [dict(r) for r in scale_events],
        "hinweis": "headroom_pct rechnet gegen instances_max × "
                   "sessions_per_instance_target. saturation_pct rechnet gegen die "
                   "aktuell laufenden Instanzen — das ist die Zahl, die sagt, ob "
                   "jetzt skaliert werden muss.",
    }
