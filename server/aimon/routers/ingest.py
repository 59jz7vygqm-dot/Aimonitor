"""Ingest-Endpunkte. Implementiert docs/integration-contract.md §2 und §3."""
from __future__ import annotations

import gzip
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError

from ..auth import Principal, require_ingest
from ..config import settings
from ..db import pool
from ..integrations import privacy as privacy_map
from ..integrations import scaler as scaler_map
from ..models import Batch, IngestResult, PrivacyIn, ScalerIn
from ..tsdb import writer

log = logging.getLogger("aimon.ingest")
router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])


async def _read_json(req: Request) -> dict:
    raw = await req.body()
    if len(raw) > settings.max_body_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Body > {settings.max_body_bytes} Bytes",
        )
    if req.headers.get("content-encoding", "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"gzip kaputt: {exc}") from exc
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "leerer Body")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"kein gültiges JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body muss ein JSON-Objekt sein")
    return data


def _validation_errors(exc: ValidationError) -> list[str]:
    out = []
    for e in exc.errors()[:50]:
        loc = "/" + "/".join(str(x) for x in e["loc"])
        out.append(f"{loc}: {e['msg']}")
    return out


async def _dedupe(key: str | None) -> bool:
    """True = schon gesehen, Batch verwerfen."""
    if not key:
        return False
    async with pool().acquire() as conn:
        inserted = await conn.fetchval(
            "INSERT INTO ingest_keys (key) VALUES ($1) ON CONFLICT (key) DO NOTHING RETURNING 1",
            key[:200],
        )
    return inserted is None


async def _store(batch: Batch, dry_run: bool) -> IngestResult:
    if batch.item_count() > settings.max_items_per_batch:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{batch.item_count()} Items > Limit {settings.max_items_per_batch}",
        )
    if dry_run:
        return IngestResult(
            accepted=0,
            rejected=0,
            warnings=[f"dry_run: {batch.item_count()} Items wären geschrieben worden"]
            + [f"unbekanntes Feld ignoriert: {f}" for f in batch.unknown_fields()],
        )
    async with pool().acquire() as conn:
        async with conn.transaction():
            return await writer.write_batch(conn, batch)


@router.get("/echo")
async def echo(p: Principal = Depends(require_ingest)) -> dict:
    """Token-Check ohne Schreibzugriff."""
    return {"token": p.name, "scopes": sorted(p.scopes), "server_version": settings.version}


@router.post("/batch", status_code=status.HTTP_202_ACCEPTED)
async def ingest_batch(
    req: Request,
    dry_run: int = 0,
    p: Principal = Depends(require_ingest),
) -> IngestResult:
    data = await _read_json(req)
    try:
        batch = Batch.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail={"errors": _validation_errors(exc)}
        ) from exc

    if await _dedupe(req.headers.get("idempotency-key")):
        return IngestResult(duplicate=True, warnings=["Idempotency-Key bereits gesehen"])

    return await _store(batch, bool(dry_run))


@router.post("/scaler", status_code=status.HTTP_202_ACCEPTED)
async def ingest_scaler(
    req: Request,
    dry_run: int = 0,
    p: Principal = Depends(require_ingest),
) -> IngestResult:
    data = await _read_json(req)
    try:
        payload = ScalerIn.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail={"errors": _validation_errors(exc)}
        ) from exc
    if await _dedupe(req.headers.get("idempotency-key")):
        return IngestResult(duplicate=True)
    return await _store(scaler_map.to_batch(payload), bool(dry_run))


@router.post("/privacy", status_code=status.HTTP_202_ACCEPTED)
async def ingest_privacy(
    req: Request,
    dry_run: int = 0,
    p: Principal = Depends(require_ingest),
) -> IngestResult:
    data = await _read_json(req)
    try:
        payload = PrivacyIn.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail={"errors": _validation_errors(exc)}
        ) from exc
    if await _dedupe(req.headers.get("idempotency-key")):
        return IngestResult(duplicate=True)

    res = await _store(privacy_map.to_batch(payload), bool(dry_run))
    if not settings.allow_raw_queries and any(d.query_raw for d in payload.decisions):
        res.warnings.append(
            "query_raw wurde verworfen (AIMON_ALLOW_RAW_QUERIES=false)"
        )
    return res
