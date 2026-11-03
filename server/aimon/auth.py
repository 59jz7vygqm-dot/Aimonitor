"""Token-Auth. Tokens werden nur als SHA-256-Hash gespeichert."""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status

from .config import settings
from .db import pool


def hash_token(tok: str) -> str:
    return hashlib.sha256(tok.strip().encode()).hexdigest()


def new_token() -> str:
    return "aimon_" + secrets.token_urlsafe(32)


@dataclass
class Principal:
    name: str
    scopes: set[str]
    token_id: int | None = None

    def has(self, scope: str) -> bool:
        return "admin" in self.scopes or scope in self.scopes


# Einfaches Token-Bucket pro Token-Name, im Prozessspeicher.
_buckets: dict[str, tuple[float, float]] = {}


def _rate_ok(key: str) -> bool:
    limit = float(settings.rate_limit_per_min)
    if limit <= 0:
        return True
    now = time.monotonic()
    tokens, last = _buckets.get(key, (limit, now))
    tokens = min(limit, tokens + (now - last) * (limit / 60.0))
    if tokens < 1.0:
        _buckets[key] = (tokens, now)
        return False
    _buckets[key] = (tokens - 1.0, now)
    return True


async def _lookup(raw: str) -> Principal | None:
    if settings.bootstrap_token and secrets.compare_digest(raw, settings.bootstrap_token):
        return Principal(name="bootstrap", scopes={"admin", "ingest", "read"})
    if settings.read_token and secrets.compare_digest(raw, settings.read_token):
        return Principal(name="read-token", scopes={"read"})

    h = hash_token(raw)
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, scopes FROM api_tokens "
            "WHERE token_hash = $1 AND revoked_at IS NULL",
            h,
        )
        if row is None:
            return None
        await conn.execute("UPDATE api_tokens SET last_used = now() WHERE id = $1", row["id"])
    return Principal(name=row["name"], scopes=set(row["scopes"] or []), token_id=row["id"])


def _extract(authorization: str | None, x_token: str | None) -> str | None:
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return authorization.strip()
    if x_token:
        return x_token.strip()
    return None


async def principal(
    authorization: str | None = Header(default=None),
    x_aimon_token: str | None = Header(default=None),
) -> Principal | None:
    raw = _extract(authorization, x_aimon_token)
    if not raw:
        return None
    p = await _lookup(raw)
    if p and not _rate_ok(p.name):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Ratelimit überschritten",
            headers={"Retry-After": "10"},
        )
    return p


async def require_ingest(p: Principal | None = Depends(principal)) -> Principal:
    if p is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Bearer-Token fehlt oder unbekannt",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not p.has("ingest"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Scope 'ingest' fehlt")
    return p


async def require_read(p: Principal | None = Depends(principal)) -> Principal:
    """Lesen ist offen, solange AIMON_READ_TOKEN nicht gesetzt ist."""
    if settings.read_token is None and settings.bootstrap_token is None:
        return p or Principal(name="anonymous", scopes={"read"})
    if settings.read_token is None:
        # Nur Bootstrap gesetzt: Lesen bleibt offen, damit die UI ohne Token geht.
        return p or Principal(name="anonymous", scopes={"read"})
    if p is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Lese-Token erforderlich",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not p.has("read"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Scope 'read' fehlt")
    return p


async def require_admin(p: Principal | None = Depends(principal)) -> Principal:
    if p is None or not p.has("admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Scope 'admin' erforderlich")
    return p
