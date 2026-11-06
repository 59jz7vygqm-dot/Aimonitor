"""aimon-server: Einstiegspunkt."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import settings
from .diagnostics import rules
from .routers import diagnostics, ingest, inventory, overview, query, topology
from .tsdb import rollup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("aimon")

WEB_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "web"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Die Task-Liste gehört in den Lifespan, nicht ins Modul: sonst sammelt
    # sie bei mehreren Starts im selben Prozess Tasks aus toten Event-Loops an.
    tasks: list[asyncio.Task] = []
    await db.migrate()
    for coro, name in (
        (rollup.rollup_loop(), "rollup"),
        (rollup.retention_loop(), "retention"),
        (rules.rules_loop(), "rules"),
    ):
        tasks.append(asyncio.create_task(coro, name=name))
    log.info("aimon-server %s gestartet", settings.version)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await db.close_pool()
        log.info("aimon-server gestoppt")


app = FastAPI(
    title="aimonitor",
    version=settings.version,
    description="Zentrales Monitoring für einen selbstgehosteten KI-Stack. "
                "Ingest-Contract: siehe docs/integration-contract.md",
    lifespan=lifespan,
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(ingest.router)
app.include_router(inventory.router)
app.include_router(query.router)
app.include_router(topology.router)
app.include_router(overview.router)
app.include_router(diagnostics.router)


@app.get("/api/v1/health")
async def health() -> JSONResponse:
    h = await db.health()
    ok = h["db"] == "ok"
    return JSONResponse(
        {"status": "ok" if ok else "degraded", "version": settings.version,
         "build": settings.build, **h},
        status_code=200 if ok else 503,
    )


@app.get("/api/v1/contract")
async def contract() -> JSONResponse:
    """Den Ingest-Contract direkt aus dem Server ausliefern.

    Damit kann ein Integrator die Spezifikation ohne Repo-Zugriff abrufen —
    genau die Fassung, die dieser Server umsetzt.
    """
    p = pathlib.Path(__file__).resolve().parent.parent.parent / "docs" / "integration-contract.md"
    if not p.exists():
        return JSONResponse({"error": "Contract-Dokument nicht gefunden"}, status_code=404)
    return JSONResponse({"version": "v1", "markdown": p.read_text()})


if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(WEB_DIR / "index.html"))

    @app.get("/favicon.svg")
    async def favicon() -> FileResponse:
        return FileResponse(str(WEB_DIR / "favicon.svg"))
else:  # pragma: no cover
    log.warning("Kein web/-Verzeichnis gefunden (%s) – UI nicht verfügbar", WEB_DIR)
