"""Ein Call für die Startseite. Alles, was oben im Dashboard steht."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends

from ..auth import Principal, require_read
from ..config import settings
from ..db import pool
from ..tsdb import query as q

router = APIRouter(prefix="/api/v1", tags=["overview"])


def _services_by_node(rows) -> dict[str, list[dict]]:
    """Dienste nach Maschine, damit die Übersicht sie an der Karte zeigen kann."""
    out: dict[str, list[dict]] = {}
    for r in rows:
        node = r["node"] or "unbekannt"
        out.setdefault(node, []).append({
            "key": r["key"],
            "kind": r["kind"],
            "name": r["name"],
            "version": r["version"],
            "base_url": r["base_url"],
            # Das Modell ist die Information, die man bei vLLM wirklich sucht.
            "model": (r["labels"] or {}).get("model"),
            "status": ("online" if r["age_s"] <= settings.stale_after_s
                       else "stale" if r["age_s"] <= settings.stale_after_s * 10
                       else "offline"),
            "age_s": r["age_s"],
        })
    return out


def _notify_enabled() -> bool:
    from ..diagnostics import notify

    return notify.enabled()


def _notify_channels() -> list[str]:
    from ..diagnostics import notify

    return notify.channels()


async def _host_rows(conn) -> dict[str, dict]:
    """Kennzahlen des Betriebssystems je Maschine.

    Bei Systemen mit gemeinsamem Speicher ist das nicht nur Beiwerk, sondern
    die eigentliche Speichergrenze für Modelle - deshalb gehört es auf die
    Startseite und nicht in eine Unterseite.
    """
    rows = await conn.fetch(
        """
        SELECT n.name AS node, s.metric, s.labels, l.value, l.ts
        FROM series s
        JOIN series_last l ON l.series_id = s.id
        LEFT JOIN nodes n ON n.id = s.node_id
        WHERE s.metric IN ('host.cpu_pct','host.mem_used_bytes',
                           'host.mem_total_bytes','host.swap_used_bytes',
                           'host.load1','host.uptime_s',
                           'host.disk_used_bytes','host.disk_total_bytes')
          AND n.name IS NOT NULL
        """
    )
    now = dt.datetime.now(dt.timezone.utc)
    out: dict[str, dict] = {}
    # Platten getrennt sammeln: pro Mount ein Paar, am Ende die vollste nehmen.
    disks: dict[str, dict[str, dict]] = {}

    for r in rows:
        node = r["node"]
        d = out.setdefault(node, {"node": node})
        age = (now - r["ts"]).total_seconds() if r["ts"] else None
        d["age_s"] = min(age, d["age_s"]) if d.get("age_s") is not None else age

        if r["metric"] in ("host.disk_used_bytes", "host.disk_total_bytes"):
            mount = (r["labels"] or {}).get("mount") or "/"
            disks.setdefault(node, {}).setdefault(mount, {})[r["metric"]] = r["value"]
            continue
        d[r["metric"].replace("host.", "")] = r["value"]

    for node, mounts in disks.items():
        worst = None
        for mount, vals in mounts.items():
            total = vals.get("host.disk_total_bytes")
            used = vals.get("host.disk_used_bytes")
            if not total or used is None:
                continue
            pct = used / total * 100.0
            if worst is None or pct > worst["disk_pct"]:
                worst = {"disk_mount": mount, "disk_used": used,
                         "disk_total": total, "disk_pct": pct}
        if worst:
            out.setdefault(node, {"node": node}).update(worst)

    for d in out.values():
        total = d.get("mem_total_bytes")
        used = d.get("mem_used_bytes")
        d["mem_pct"] = (used / total * 100.0) if total and used is not None else None
    return out


async def _gpu_rows(conn) -> list[dict]:
    rows = await conn.fetch(
        """
        WITH g AS (
            SELECT s.id, s.metric, s.labels, n.name AS node, l.value, l.ts
            FROM series s
            JOIN series_last l ON l.series_id = s.id
            LEFT JOIN nodes n ON n.id = s.node_id
            WHERE s.metric LIKE 'gpu.%'
        )
        SELECT node, labels->>'gpu' AS gpu,
               max(labels->>'name') AS name,
               max(labels->>'uuid') AS uuid,
               max(ts) AS ts,
               max(value) FILTER (WHERE metric='gpu.temp_c')          AS temp_c,
               max(value) FILTER (WHERE metric='gpu.temp_mem_c')      AS temp_mem_c,
               max(value) FILTER (WHERE metric='gpu.util_pct')        AS util_pct,
               max(value) FILTER (WHERE metric='gpu.mem_used_bytes')  AS mem_used,
               max(value) FILTER (WHERE metric='gpu.mem_total_bytes') AS mem_total,
               max(value) FILTER (WHERE metric='gpu.power_w')         AS power_w,
               max(value) FILTER (WHERE metric='gpu.power_limit_w')   AS power_limit_w,
               max(value) FILTER (WHERE metric='gpu.fan_pct')         AS fan_pct,
               max(value) FILTER (WHERE metric='gpu.clock_sm_mhz')    AS clock_sm_mhz,
               max(value) FILTER (WHERE metric='gpu.throttle_thermal') AS throttle_thermal,
               max(value) FILTER (WHERE metric='gpu.throttle_power')   AS throttle_power,
               max(value) FILTER (WHERE metric='gpu.throttle_hw')      AS throttle_hw,
               max(value) FILTER (WHERE metric='gpu.mem_unified')      AS mem_unified,
               max(value) FILTER (WHERE metric='gpu.mem_unavailable')  AS mem_unavailable,
               max(labels->>'memory') FILTER (WHERE metric='gpu.mem_total_bytes')
                   AS mem_source
        FROM g WHERE labels ? 'gpu'
        GROUP BY node, labels->>'gpu'
        ORDER BY node, (labels->>'gpu')::int
        """
    )
    out = []
    now = dt.datetime.now(dt.timezone.utc)
    for r in rows:
        d = dict(r)
        if d["mem_total"] and d["mem_total"] > 0 and d["mem_used"] is not None:
            d["mem_pct"] = d["mem_used"] / d["mem_total"] * 100.0
        else:
            d["mem_pct"] = None
        d["age_s"] = (now - r["ts"]).total_seconds() if r["ts"] else None
        d["fresh"] = (d["age_s"] is not None and d["age_s"] <= settings.stale_after_s)
        # Warum fehlt der Speicher: gemeinsamer Speicher oder gar keine Angabe.
        # Woher der Speicherwert stammt. Das steht im Label der Serie, weil
        # "90 % belegt" bei eigenem VRAM etwas anderes heißt als beim
        # Arbeitsspeicher eines Hosts, den sich CPU und GPU teilen.
        src = (r["mem_source"] or "")
        d["memory_kind"] = (
            "host" if src == "host"
            else "unified" if (r["mem_unified"] or 0) > 0
            else "unavailable" if (r["mem_unavailable"] or 0) > 0
            else "dedicated"
        )
        out.append(d)
    return out


@router.get("/overview")
async def overview(p: Principal = Depends(require_read)) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    h1 = now - dt.timedelta(hours=1)
    m5 = now - dt.timedelta(minutes=5)

    async with pool().acquire() as conn:
        node_rows = await conn.fetch(
            """
            SELECT n.name, n.hostname, n.agent_version, n.labels, n.last_seen,
                   extract(epoch FROM (now() - n.last_seen))::float8 AS age_s,
                   (SELECT count(*) FROM services s WHERE s.node_id = n.id)
                       AS service_count
            FROM nodes n ORDER BY n.name
            """
        )
        svc_rows = await conn.fetch(
            """
            SELECT kind, count(*) AS n,
                   count(*) FILTER (
                       WHERE last_seen > now() - ($1::int * INTERVAL '1 second')) AS online
            FROM services GROUP BY kind ORDER BY kind
            """,
            settings.stale_after_s,
        )
        svc_detail = await conn.fetch(
            """
            SELECT s.key, s.kind, s.name, s.version, s.base_url, s.labels,
                   n.name AS node,
                   extract(epoch FROM (now() - s.last_seen))::float8 AS age_s
            FROM services s LEFT JOIN nodes n ON n.id = s.node_id
            ORDER BY n.name, s.kind, s.key
            """
        )
        gpus = await _gpu_rows(conn)
        hosts = await _host_rows(conn)

        llm = await conn.fetch(
            """
            SELECT sv.key AS service, sv.kind, count(*) AS requests,
                   avg(r.ttft_ms)::float8 AS avg_ttft_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY r.ttft_ms)::float8 AS p95_ttft_ms,
                   avg(r.prompt_tokens)::float8 AS avg_prompt_tokens,
                   avg(r.completion_tokens)::float8 AS avg_completion_tokens,
                   sum(r.completion_tokens)::float8 AS tokens_out,
                   count(*) FILTER (WHERE r.status >= 400 OR r.error IS NOT NULL) AS errors
            FROM requests r JOIN services sv ON sv.id = r.service_id
            WHERE r.ts > $1 GROUP BY sv.key, sv.kind ORDER BY requests DESC
            """,
            h1,
        )

        running = await q.latest(conn, "llm.requests_running")
        waiting = await q.latest(conn, "llm.requests_waiting")
        kv = await q.latest(conn, "llm.kv_cache_usage_pct")

        ot = await conn.fetch(
            """
            SELECT sv.key AS service, s.metric, l.value, l.ts
            FROM series s JOIN series_last l ON l.series_id = s.id
            JOIN services sv ON sv.id = s.service_id
            WHERE s.metric LIKE 'openterminal.%' AND s.labels = '{}'::jsonb
            """
        )
        pv = await conn.fetch(
            """
            SELECT sv.key AS service, s.metric, s.labels, l.value, l.ts
            FROM series s JOIN series_last l ON l.series_id = s.id
            JOIN services sv ON sv.id = s.service_id
            WHERE s.metric LIKE 'privacy.%'
            """
        )
        pv_recent = await conn.fetch(
            """
            SELECT kind, count(*) AS n, sum(count) AS weighted
            FROM events
            WHERE ts > $1 AND kind LIKE 'privacy.%'
            GROUP BY kind
            """,
            h1,
        )
        comfy = await conn.fetch(
            """
            SELECT sv.key AS service, s.metric, l.value, l.ts
            FROM series s JOIN series_last l ON l.series_id = s.id
            JOIN services sv ON sv.id = s.service_id
            WHERE s.metric LIKE 'comfy.%'
            """
        )
        inc = await conn.fetch(
            """
            SELECT severity, count(*) AS n FROM incidents
            WHERE ended_at IS NULL GROUP BY severity
            """
        )
        ev = await conn.fetch(
            """
            SELECT severity, count(*) AS n FROM events
            WHERE ts > $1 GROUP BY severity
            """,
            h1,
        )
        alerts_hour = await conn.fetchval(
            "SELECT count(*) FROM alert_log WHERE ok "
            "AND sent_at > now() - INTERVAL '1 hour'"
        )
        probe_health = await conn.fetchrow(
            """
            SELECT count(*) AS n, count(*) FILTER (WHERE ok) AS ok
            FROM probes WHERE ts > $1
            """,
            m5,
        )

    def pack(rows, key="service") -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in rows:
            out.setdefault(r[key], {})[r["metric"]] = r["value"]
        return out

    stale = [r["name"] for r in node_rows if r["age_s"] > settings.stale_after_s]

    return {
        "generated_at": now,
        "server_version": settings.version,
        "nodes": {
            "total": len(node_rows),
            "online": len(node_rows) - len(stale),
            "stale": stale,
            "detail": [dict(r) for r in node_rows],
        },
        "services": [dict(r) for r in svc_rows],
        "services_by_node": _services_by_node(svc_detail),
        "hosts": hosts,
        "gpus": gpus,
        "gpu_summary": {
            "count": len(gpus),
            "max_temp_c": max((g["temp_c"] for g in gpus if g["temp_c"] is not None), default=None),
            "total_vram_bytes": sum(g["mem_total"] or 0 for g in gpus) or None,
            "used_vram_bytes": sum(g["mem_used"] or 0 for g in gpus) or None,
            "throttling": [
                f"{g['node']}/gpu{g['gpu']}" for g in gpus
                if (g["throttle_thermal"] or 0) > 0 or (g["throttle_power"] or 0) > 0
                or (g["throttle_hw"] or 0) > 0
            ],
            "stale_gpus": [f"{g['node']}/gpu{g['gpu']}" for g in gpus if not g["fresh"]],
        },
        "llm": {
            "window": "1h",
            "per_service": [dict(r) for r in llm],
            "requests_running": running,
            "requests_waiting": waiting,
            "kv_cache_usage_pct": kv,
            "note": "per_service kommt aus aimon-tap-Requests; ohne Tap bleibt die Liste "
                    "leer und die vLLM-Zähler unter /api/v1/latency liefern die Werte.",
        },
        "openterminal": pack(ot),
        "privacy": {
            "latest": [
                {"service": r["service"], "metric": r["metric"],
                 "labels": r["labels"], "value": r["value"]}
                for r in pv
            ],
            "events_1h": {r["kind"]: {"events": r["n"], "occurrences": int(r["weighted"] or 0)}
                          for r in pv_recent},
        },
        "comfyui": pack(comfy),
        "incidents_open": {r["severity"]: r["n"] for r in inc},
        "events_1h": {r["severity"]: r["n"] for r in ev},
        "alerts": {
            "enabled": _notify_enabled(),
            "channels": _notify_channels(),
            "min_severity": settings.alert_min_severity,
            "sent_last_hour": alerts_hour or 0,
        },
        "probes_5m": {
            "total": probe_health["n"] if probe_health else 0,
            "ok": probe_health["ok"] if probe_health else 0,
            "success_pct": (
                probe_health["ok"] / probe_health["n"] * 100.0
                if probe_health and probe_health["n"] else None
            ),
        },
    }


@router.get("/stats")
async def self_stats(p: Principal = Depends(require_read)) -> dict:
    """Selbstüberwachung: Wie geht es aimonitor selbst."""
    from ..tsdb import rollup, series, writer

    async with pool().acquire() as conn:
        counts = await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM series)         AS series,
                   (SELECT count(*) FROM nodes)          AS nodes,
                   (SELECT count(*) FROM services)       AS services,
                   (SELECT count(*) FROM samples_rollup) AS rollup_rows,
                   (SELECT count(*) FROM events)         AS events,
                   (SELECT count(*) FROM incidents WHERE ended_at IS NULL) AS incidents_open
            """
        )
        # reltuples ist -1, solange die Tabelle nie analysiert wurde. Das als
        # Zahl anzuzeigen wäre Unsinn, deshalb dann None statt -1.
        sample_est = await conn.fetchval(
            "SELECT reltuples::bigint FROM pg_class WHERE relname = 'samples'"
        )
        if sample_est is not None and sample_est < 0:
            sample_est = None
        db_size = await conn.fetchval("SELECT pg_database_size(current_database())")
        rs = await conn.fetch("SELECT bucket_s, last_bucket, updated_at FROM rollup_state")

    from ..db import has_timescale

    return {
        "version": settings.version,
        "timescale": has_timescale(),
        "counts": dict(counts) if counts else {},
        "samples_estimate": (int(sample_est) if sample_est is not None else None),
        "db_size_bytes": db_size,
        "series_cache": series.cache_size(),
        "ingest": writer.stats(),
        "rollup": rollup.stats(),
        "rollup_state": [dict(r) for r in rs],
        "retention_days": {
            "samples": settings.raw_retention_days,
            "rollup_1m": settings.rollup_1m_days,
            "rollup_1h": settings.rollup_1h_days,
            "requests": settings.requests_retention_days,
            "events": settings.events_retention_days,
            "raw_queries": settings.raw_query_retention_days,
        },
        "allow_raw_queries": settings.allow_raw_queries,
    }
