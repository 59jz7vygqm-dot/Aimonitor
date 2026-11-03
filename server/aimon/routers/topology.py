"""Dienstgraph: Knoten = Nodes/Services, Kanten = Probes und beobachteter Traffic."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends

from ..auth import Principal, require_read
from ..config import settings
from ..db import pool

router = APIRouter(prefix="/api/v1", tags=["topology"])

# Reihenfolge für das Layout: links Eingang, rechts Backend.
LAYER = {
    "openwebui": 0,
    "openterminal_proxy": 0,
    "privacy_proxy": 1,
    "tap": 1,
    "searxng": 2,
    "vllm": 2,
    "ollama": 2,
    "comfyui": 2,
    "openterminal_instance": 1,
    "other": 3,
}


@router.get("/topology")
async def topology(
    window_s: int = 300, p: Principal = Depends(require_read)
) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(seconds=max(60, window_s))

    async with pool().acquire() as conn:
        nodes = await conn.fetch("SELECT id, name, hostname, labels, last_seen FROM nodes")
        services = await conn.fetch(
            """
            SELECT s.id, s.key, s.kind, s.name, s.base_url, s.version, s.labels,
                   s.last_seen, n.name AS node
            FROM services s LEFT JOIN nodes n ON n.id = s.node_id
            """
        )
        edges = await conn.fetch(
            """
            SELECT e.id, e.kind, e.ok, e.latency_ms, e.last_ok_ts, e.last_fail_ts,
                   e.last_seen, e.samples, e.fails,
                   sn.name AS src_node, ss.key AS src_service, ds.key AS dst_service,
                   e.dst_target
            FROM edges e
            LEFT JOIN nodes    sn ON sn.id = e.src_node_id
            LEFT JOIN services ss ON ss.id = e.src_svc_id
            LEFT JOIN services ds ON ds.id = e.dst_svc_id
            WHERE e.last_seen > $1
            ORDER BY e.last_seen DESC
            """,
            cutoff,
        )
        # Fehlerrate der letzten Fensterbreite aus den Rohproben, nicht aus dem
        # Allzeit-Zähler in `edges`.
        recent = await conn.fetch(
            """
            SELECT n.name AS src_node, pr.target,
                   count(*) AS n,
                   count(*) FILTER (WHERE NOT pr.ok) AS fails,
                   avg(pr.latency_ms)::float8 AS avg_latency_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY pr.latency_ms)::float8
                       AS p95_latency_ms
            FROM probes pr LEFT JOIN nodes n ON n.id = pr.from_node_id
            WHERE pr.ts > $1
            GROUP BY n.name, pr.target
            """,
            cutoff,
        )
        traffic = await conn.fetch(
            """
            SELECT sv.key AS dst_service, r.client,
                   count(*) AS n,
                   avg(r.ttft_ms)::float8 AS avg_ttft_ms,
                   count(*) FILTER (WHERE r.status >= 400 OR r.error IS NOT NULL) AS errors
            FROM requests r LEFT JOIN services sv ON sv.id = r.service_id
            WHERE r.ts > $1
            GROUP BY sv.key, r.client
            """,
            cutoff,
        )

    rstat = {(r["src_node"], r["target"]): dict(r) for r in recent}

    def age(ts: dt.datetime | None) -> float | None:
        return None if ts is None else (now - ts).total_seconds()

    gnodes = [
        {
            "id": f"node:{r['name']}",
            "type": "node",
            "label": r["name"],
            "hostname": r["hostname"],
            "labels": r["labels"],
            "status": (
                "online" if age(r["last_seen"]) <= settings.stale_after_s
                else "stale" if age(r["last_seen"]) <= settings.stale_after_s * 10
                else "offline"
            ),
            "last_seen_age_s": age(r["last_seen"]),
        }
        for r in nodes
    ]
    gnodes += [
        {
            "id": f"svc:{r['key']}",
            "type": "service",
            "kind": r["kind"],
            "label": r["name"] or r["key"],
            "key": r["key"],
            "node": r["node"],
            "parent": f"node:{r['node']}" if r["node"] else None,
            "base_url": r["base_url"],
            "version": r["version"],
            "labels": r["labels"],
            "layer": LAYER.get(r["kind"], 3),
            "status": (
                "online" if age(r["last_seen"]) <= settings.stale_after_s
                else "stale" if age(r["last_seen"]) <= settings.stale_after_s * 10
                else "offline"
            ),
            "last_seen_age_s": age(r["last_seen"]),
        }
        for r in services
    ]

    gedges = []
    for r in edges:
        src = (
            f"svc:{r['src_service']}" if r["src_service"]
            else f"node:{r['src_node']}" if r["src_node"] else None
        )
        dst = f"svc:{r['dst_service']}" if r["dst_service"] else None
        if dst is None:
            # Ziel ist kein registrierter Dienst: als externer Endpunkt zeigen,
            # statt die Kante zu verschlucken.
            dst = f"ext:{r['dst_target']}"
            gnodes.append(
                {
                    "id": dst, "type": "external", "label": r["dst_target"],
                    "kind": "other", "layer": 4,
                    "status": "online" if r["ok"] else "offline",
                }
            )
        if src is None:
            continue
        st = rstat.get((r["src_node"], r["dst_target"]))
        gedges.append(
            {
                "source": src,
                "target": dst,
                "kind": r["kind"],
                "ok": r["ok"],
                "latency_ms": r["latency_ms"],
                "window_probes": st["n"] if st else None,
                "window_fails": st["fails"] if st else None,
                "window_avg_latency_ms": st["avg_latency_ms"] if st else None,
                "window_p95_latency_ms": st["p95_latency_ms"] if st else None,
                "last_ok_age_s": age(r["last_ok_ts"]),
                "last_fail_age_s": age(r["last_fail_ts"]),
                "total_probes": r["samples"],
                "total_fails": r["fails"],
            }
        )

    for r in traffic:
        if not r["dst_service"]:
            continue
        client = r["client"] or "unbekannt"
        src = f"client:{client}"
        if not any(n["id"] == src for n in gnodes):
            gnodes.append(
                {"id": src, "type": "client", "label": client, "kind": "other",
                 "layer": -1, "status": "online"}
            )
        gedges.append(
            {
                "source": src,
                "target": f"svc:{r['dst_service']}",
                "kind": "traffic",
                "ok": r["errors"] == 0,
                "requests": r["n"],
                "errors": r["errors"],
                "latency_ms": r["avg_ttft_ms"],
            }
        )

    # Duplikate zusammenfassen (externe Endpunkte können mehrfach entstehen).
    seen: set[str] = set()
    uniq = []
    for n in gnodes:
        if n["id"] in seen:
            continue
        seen.add(n["id"])
        uniq.append(n)

    return {
        "generated_at": now,
        "window_s": window_s,
        "nodes": uniq,
        "edges": gedges,
        "note": "kind=probe sind aktive Checks der Agents, kind=traffic ist echter "
                "beobachteter Inference-Verkehr aus aimon-tap.",
    }


@router.get("/connectivity-matrix")
async def connectivity_matrix(
    window_s: int = 900, p: Principal = Depends(require_read)
) -> dict:
    """Wer erreicht wen. Zeilen = Quell-Node, Spalten = Ziel."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=max(60, window_s))
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT COALESCE(n.name,'?') AS src,
                   COALESCE(sv.key, pr.target) AS dst,
                   count(*) AS n,
                   count(*) FILTER (WHERE pr.ok) AS ok,
                   avg(pr.latency_ms)::float8 AS avg_ms,
                   max(pr.ts) AS last_ts,
                   (array_agg(pr.error ORDER BY pr.ts DESC) FILTER (WHERE pr.error IS NOT NULL))[1]
                       AS last_error
            FROM probes pr
            LEFT JOIN nodes n ON n.id = pr.from_node_id
            LEFT JOIN services sv ON sv.id = pr.target_svc_id
            WHERE pr.ts > $1
            GROUP BY src, dst ORDER BY src, dst
            """,
            cutoff,
        )
    cells = []
    for r in rows:
        cells.append(
            {
                "src": r["src"], "dst": r["dst"], "probes": r["n"], "ok": r["ok"],
                "success_pct": (r["ok"] / r["n"] * 100.0) if r["n"] else None,
                "avg_latency_ms": r["avg_ms"], "last_ts": r["last_ts"],
                "last_error": r["last_error"],
            }
        )
    return {
        "window_s": window_s,
        "sources": sorted({c["src"] for c in cells}),
        "targets": sorted({c["dst"] for c in cells}),
        "cells": cells,
    }
