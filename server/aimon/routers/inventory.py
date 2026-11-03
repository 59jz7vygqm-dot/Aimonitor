"""Nodes, Services, Metrikkatalog, Token-Verwaltung."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException

from ..auth import Principal, hash_token, new_token, require_admin, require_read
from ..config import settings
from ..db import pool

router = APIRouter(prefix="/api/v1", tags=["inventory"])


def _node_status(last_seen: dt.datetime) -> str:
    age = (dt.datetime.now(dt.timezone.utc) - last_seen).total_seconds()
    if age <= settings.stale_after_s:
        return "online"
    if age <= settings.stale_after_s * 10:
        return "stale"
    return "offline"


@router.get("/nodes")
async def list_nodes(p: Principal = Depends(require_read)) -> list[dict]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT n.*,
                   (SELECT count(*) FROM services s WHERE s.node_id = n.id) AS services,
                   (SELECT count(*) FROM series sr WHERE sr.node_id = n.id) AS series
            FROM nodes n ORDER BY n.name
            """
        )
    out = []
    for r in rows:
        d = dict(r)
        d["status"] = _node_status(r["last_seen"])
        d["last_seen_age_s"] = (
            dt.datetime.now(dt.timezone.utc) - r["last_seen"]
        ).total_seconds()
        out.append(d)
    return out


@router.get("/services")
async def list_services(
    kind: str | None = None, node: str | None = None, p: Principal = Depends(require_read)
) -> list[dict]:
    where, args = [], []
    if kind:
        args.append(kind)
        where.append(f"s.kind = ${len(args)}")
    if node:
        args.append(node)
        where.append(f"n.name = ${len(args)}")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT s.*, n.name AS node_name FROM services s
            LEFT JOIN nodes n ON n.id = s.node_id
            {clause}
            ORDER BY s.kind, s.key
            """,
            *args,
        )
    out = []
    for r in rows:
        d = dict(r)
        d["status"] = _node_status(r["last_seen"])
        out.append(d)
    return out


@router.get("/metrics-catalog")
async def metrics_catalog(p: Principal = Depends(require_read)) -> list[dict]:
    """Welche Metriken existieren, mit welchen Label-Keys. Für das UI-Explorer-Panel."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT metric, count(*) AS series, bool_or(is_counter) AS is_counter,
                   max(last_seen) AS last_seen,
                   (SELECT array_agg(DISTINCT k) FROM series s2,
                        LATERAL jsonb_object_keys(s2.labels) AS k
                    WHERE s2.metric = s.metric) AS label_keys
            FROM series s
            GROUP BY metric ORDER BY metric
            """
        )
    return [dict(r) for r in rows]


@router.get("/series")
async def list_series(
    metric: str, limit: int = 200, p: Principal = Depends(require_read)
) -> list[dict]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.id, s.metric, s.labels, s.is_counter, s.last_seen,
                   n.name AS node, sv.key AS service, sv.kind AS service_kind
            FROM series s
            LEFT JOIN nodes n ON n.id = s.node_id
            LEFT JOIN services sv ON sv.id = s.service_id
            WHERE s.metric = $1 ORDER BY n.name NULLS LAST, s.labels LIMIT $2
            """,
            metric, min(limit, 2000),
        )
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ Tokens


@router.get("/tokens")
async def list_tokens(p: Principal = Depends(require_admin)) -> list[dict]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, scopes, created_at, last_used, revoked_at, note "
            "FROM api_tokens ORDER BY id"
        )
    return [dict(r) for r in rows]


@router.post("/tokens")
async def create_token(
    body: dict, p: Principal = Depends(require_admin)
) -> dict:
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name fehlt")
    scopes = body.get("scopes") or ["ingest"]
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise HTTPException(400, "scopes muss eine Liste von Strings sein")
    bad = set(scopes) - {"ingest", "read", "admin"}
    if bad:
        raise HTTPException(400, f"unbekannte Scopes: {sorted(bad)}")

    tok = new_token()
    async with pool().acquire() as conn:
        try:
            tid = await conn.fetchval(
                "INSERT INTO api_tokens (name, token_hash, scopes, note) "
                "VALUES ($1,$2,$3,$4) RETURNING id",
                name, hash_token(tok), scopes, str(body.get("note") or "")[:500] or None,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(409, f"Token-Name schon vergeben: {exc}") from exc
    # Einzige Stelle, an der das Klartext-Token existiert.
    return {"id": tid, "name": name, "scopes": scopes, "token": tok,
            "hinweis": "Token wird nur hier einmal ausgegeben; es ist nur als Hash gespeichert."}


@router.delete("/tokens/{token_id}")
async def revoke_token(token_id: int, p: Principal = Depends(require_admin)) -> dict:
    async with pool().acquire() as conn:
        n = await conn.execute(
            "UPDATE api_tokens SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL",
            token_id,
        )
    return {"revoked": n.endswith("1")}
