"""End-to-End durch die HTTP-API: Auth, Ingest, Query, Diagnose."""
from __future__ import annotations

import datetime as dt

import httpx
import pytest
from asgi_lifespan import LifespanManager

pytestmark = pytest.mark.asyncio

TOKEN = "test-token-fuer-pytest"


@pytest.fixture
async def client(db, monkeypatch):
    from aimon.config import settings

    monkeypatch.setattr(settings, "bootstrap_token", TOKEN)
    monkeypatch.setattr(settings, "read_token", None)
    monkeypatch.setattr(settings, "rate_limit_per_min", 0)  # im Test nicht drosseln

    from aimon.main import app

    # Die DB ist durch die db-Fixture schon migriert; der Lifespan startet nur
    # noch die Hintergrundarbeiter.
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as c:
            c.headers["Authorization"] = f"Bearer {TOKEN}"
            yield c


def ts(seconds_ago: float = 0) -> float:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=seconds_ago)).timestamp()


async def test_health_ohne_token(client):
    c = httpx.AsyncClient(transport=client._transport, base_url="http://test")
    r = await c.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    await c.aclose()


async def test_ingest_braucht_token(client):
    c = httpx.AsyncClient(transport=client._transport, base_url="http://test")
    r = await c.post("/api/v1/ingest/batch", json={"node": {"name": "n"}})
    assert r.status_code == 401
    r = await c.post("/api/v1/ingest/batch", json={"node": {"name": "n"}},
                     headers={"Authorization": "Bearer falsch"})
    assert r.status_code == 401
    await c.aclose()


async def test_echo_zeigt_scopes(client):
    r = await client.get("/api/v1/ingest/echo")
    assert r.status_code == 200
    assert "ingest" in r.json()["scopes"]


async def test_batch_schreiben_und_lesen(client):
    r = await client.post("/api/v1/ingest/batch", json={
        "node": {"name": "gpu-1", "agent_version": "0.1.0"},
        "services": [{"key": "vllm@gpu-1:8000", "kind": "vllm",
                      "labels": {"model": "Qwen3-32B"}}],
        "samples": [{"metric": "gpu.temp_c", "value": 71.5, "ts": ts(),
                     "labels": {"gpu": "0"}}],
    })
    assert r.status_code == 202, r.text
    assert r.json()["rejected"] == 0

    r = await client.get("/api/v1/latest", params={"metric": "gpu.temp_c"})
    assert r.json()[0]["value"] == 71.5
    assert r.json()[0]["node"] == "gpu-1"

    r = await client.get("/api/v1/nodes")
    assert r.json()[0]["status"] == "online"

    r = await client.get("/api/v1/services", params={"kind": "vllm"})
    assert r.json()[0]["labels"]["model"] == "Qwen3-32B"


async def test_kaputtes_schema_gibt_400_mit_pointer(client):
    r = await client.post("/api/v1/ingest/batch", json={
        "node": {"name": "n"},
        "samples": [{"metric": "NICHT GÜLTIG!", "value": 1}],
    })
    assert r.status_code == 400
    errs = r.json()["detail"]["errors"]
    assert any("/samples/0/metric" in e for e in errs)


async def test_dry_run_schreibt_nicht(client):
    r = await client.post("/api/v1/ingest/batch?dry_run=1", json={
        "node": {"name": "trocken"},
        "samples": [{"metric": "a.b", "value": 1}],
    })
    assert r.status_code == 202
    assert r.json()["accepted"] == 0
    assert "dry_run" in r.json()["warnings"][0]

    r = await client.get("/api/v1/nodes")
    assert all(n["name"] != "trocken" for n in r.json())


async def test_idempotency_key(client):
    body = {"node": {"name": "n"}, "samples": [{"metric": "a.b", "value": 1}]}
    h = {"Idempotency-Key": "abc-123"}
    r1 = await client.post("/api/v1/ingest/batch", json=body, headers=h)
    r2 = await client.post("/api/v1/ingest/batch", json=body, headers=h)
    assert r1.json()["duplicate"] is False
    assert r2.json()["duplicate"] is True


async def test_gzip_wird_akzeptiert(client):
    import gzip
    import json

    body = json.dumps({"node": {"name": "n"},
                       "samples": [{"metric": "a.b", "value": 1}]}).encode()
    r = await client.post("/api/v1/ingest/batch", content=gzip.compress(body),
                          headers={"Content-Encoding": "gzip",
                                   "Content-Type": "application/json"})
    assert r.status_code == 202, r.text
    assert r.json()["accepted"] >= 1


async def test_scaler_endpunkt_rechnet_headroom(client):
    r = await client.post("/api/v1/ingest/scaler", json={
        "node": {"name": "proxy"},
        "service": {"key": "ot@proxy:7681", "kind": "openterminal_proxy"},
        "ts": ts(),
        "state": {"instances_running": 4, "instances_max": 12,
                  "sessions_active": 27, "sessions_per_instance_target": 10},
    })
    assert r.status_code == 202, r.text

    r = await client.get("/api/v1/latest",
                         params={"metric": "openterminal.headroom_pct"})
    assert r.json()[0]["value"] == 77.5

    r = await client.get("/api/v1/openterminal/summary")
    cur = r.json()["proxies"]["ot@proxy:7681"]["current"]
    assert cur["openterminal.capacity_sessions"] == 120


async def test_privacy_endpunkt_und_log(client):
    r = await client.post("/api/v1/ingest/privacy", json={
        "node": {"name": "proxy"},
        "service": {"key": "pp@proxy:8888", "kind": "privacy_proxy"},
        "ts": ts(),
        "counters": {"requests_total": 100, "blocked_total": 10},
        "decisions": [
            {"action": "blocked", "category": "person_name", "rule": "ner.person",
             "query_redacted": "wohnort von [NAME]", "query_len": 18,
             "query_sha256": "abc123"},
            {"action": "allowed", "query_len": 12},
        ],
    })
    assert r.status_code == 202, r.text

    r = await client.get("/api/v1/privacy/blocked", params={"since": "-1h"})
    data = r.json()
    assert len(data["log"]) == 1   # nur der Eingriff, nicht die erlaubte Anfrage
    entry = data["log"][0]
    assert entry["category"] == "person_name"
    assert entry["query_redacted"] == "wohnort von [NAME]"
    assert entry["query_raw"] is None


async def test_rohtext_wird_abgewiesen_und_gemeldet(client, monkeypatch):
    from aimon.config import settings

    monkeypatch.setattr(settings, "allow_raw_queries", False)
    r = await client.post("/api/v1/ingest/privacy", json={
        "node": {"name": "proxy"},
        "service": {"key": "pp@proxy:8888", "kind": "privacy_proxy"},
        "decisions": [{"action": "blocked", "category": "person_name",
                       "query_raw": "wohnort von Max Mustermann"}],
    })
    assert r.status_code == 202
    # Der Sender muss erfahren, dass sein Feld verworfen wurde.
    assert any("query_raw" in w for w in r.json()["warnings"])

    r = await client.get("/api/v1/privacy/blocked", params={"since": "-1h"})
    assert r.json()["log"][0]["query_raw"] is None


async def test_query_und_zeitraum_parsing(client):
    base = ts(600)
    samples = [{"metric": "gpu.temp_c", "value": 60 + i % 5,
                "ts": base + i * 10, "labels": {"gpu": "0"}} for i in range(60)]
    await client.post("/api/v1/ingest/batch",
                      json={"node": {"name": "n"}, "samples": samples})

    r = await client.get("/api/v1/query",
                         params={"metric": "gpu.temp_c", "start": "-1h"})
    d = r.json()
    assert d["source"] == "raw"
    assert len(d["series"]) == 1
    assert len(d["series"][0]["points"]) > 5

    r = await client.get("/api/v1/query",
                         params={"metric": "gibt.es.nicht", "start": "-1h"})
    assert r.json()["series"] == []
    assert "keine Serie" in r.json()["note"]

    r = await client.get("/api/v1/query",
                         params={"metric": "gpu.temp_c", "start": "-unsinn"})
    assert r.status_code == 400


async def test_latency_sagt_woher_die_zahl_kommt(client):
    await client.post("/api/v1/ingest/batch", json={
        "node": {"name": "n"},
        "services": [{"key": "vllm@n:8000", "kind": "vllm"}],
        "requests": [{"service": "vllm@n:8000", "ttft_ms": float(100 + i),
                      "ts": ts(60)} for i in range(50)],
    })
    r = await client.get("/api/v1/latency", params={"what": "ttft", "start": "-1h"})
    d = r.json()
    assert d["source"] == "requests"
    assert d["n"] == 50
    assert 100 <= d["quantiles"]["0.5"] <= 150


async def test_latency_ohne_daten_erfindet_nichts(client):
    r = await client.get("/api/v1/latency", params={"what": "ttft", "start": "-1h"})
    d = r.json()
    assert d["source"] is None
    assert d["n"] == 0
    assert "keine Daten" in d["note"]


async def test_topologie_und_matrix(client):
    await client.post("/api/v1/ingest/batch", json={
        "node": {"name": "gpu-1"},
        "services": [{"key": "owui@h:3000", "kind": "openwebui"},
                     {"key": "vllm@gpu-1:8000", "kind": "vllm"}],
        "probes": [
            {"target": "http://h:3000/health", "target_service": "owui@h:3000",
             "ok": True, "latency_ms": 5, "ts": ts()},
            {"target": "http://gpu-1:8000/health",
             "target_service": "vllm@gpu-1:8000", "ok": False,
             "error": "timeout", "ts": ts()},
        ],
    })
    r = await client.get("/api/v1/topology")
    d = r.json()
    ids = {n["id"] for n in d["nodes"]}
    assert "node:gpu-1" in ids
    assert "svc:owui@h:3000" in ids
    failing = [e for e in d["edges"] if e["ok"] is False]
    assert len(failing) == 1
    assert failing[0]["target"] == "svc:vllm@gpu-1:8000"

    r = await client.get("/api/v1/connectivity-matrix")
    cells = r.json()["cells"]
    assert any(c["last_error"] == "timeout" for c in cells)


async def test_overview_liefert_alle_bloecke(client):
    await client.post("/api/v1/ingest/batch", json={
        "node": {"name": "n"},
        "samples": [
            {"metric": "gpu.temp_c", "value": 70, "ts": ts(),
             "labels": {"gpu": "0", "name": "RTX 4090"}},
            {"metric": "gpu.mem_used_bytes", "value": 12e9, "ts": ts(),
             "labels": {"gpu": "0"}},
            {"metric": "gpu.mem_total_bytes", "value": 24e9, "ts": ts(),
             "labels": {"gpu": "0"}},
        ],
    })
    d = (await client.get("/api/v1/overview")).json()
    assert d["nodes"]["total"] == 1
    assert d["gpu_summary"]["count"] == 1
    assert d["gpu_summary"]["max_temp_c"] == 70
    g = d["gpus"][0]
    assert g["mem_pct"] == pytest.approx(50.0)
    assert g["fresh"] is True
    for key in ("llm", "openterminal", "privacy", "incidents_open", "probes_5m"):
        assert key in d


async def test_diagnose_prompt_enthaelt_den_aufbau(client):
    await client.post("/api/v1/ingest/batch", json={
        "node": {"name": "gpu-vm-01", "agent_version": "0.1.0"},
        "services": [{"key": "vllm@gpu-vm-01:8000", "kind": "vllm",
                      "version": "0.8.5", "labels": {"model": "Qwen3-32B"}}],
        "samples": [
            {"metric": "gpu.temp_c", "value": 91, "ts": ts(),
             "labels": {"gpu": "0", "name": "RTX 4090"}},
            {"metric": "gpu.mem_total_bytes", "value": 24e9, "ts": ts(),
             "labels": {"gpu": "0"}},
        ],
    })
    await client.post("/api/v1/incidents/evaluate")

    d = (await client.get("/api/v1/diagnostics/prompt",
                          params={"window_s": 3600, "note": "Alles langsam"})).json()
    p = d["prompt"]
    assert "gpu-vm-01" in p
    assert "vllm@gpu-vm-01:8000" in p
    assert "Qwen3-32B" in p
    assert "RTX 4090" in p
    assert "Alles langsam" in p
    assert "91" in p
    assert "Was ich nicht messe" in p      # Blindstellen werden benannt
    assert "Meine Frage" in p
    assert d["legend"] == []               # ohne redact keine Platzhalter


async def test_diagnose_prompt_anonymisiert_vollstaendig(client):
    await client.post("/api/v1/ingest/batch", json={
        "node": {"name": "gpu-vm-01"},
        "services": [{"key": "vllm@gpu-vm-01:8000", "kind": "vllm"}],
        "samples": [{"metric": "gpu.temp_c", "value": 91, "ts": ts(),
                     "labels": {"gpu": "0"}}],
    })
    await client.post("/api/v1/incidents/evaluate")

    d = (await client.get("/api/v1/diagnostics/prompt",
                          params={"window_s": 3600, "redact": "true"})).json()
    # Die Zuordnung darf NICHT im kopierbaren Text stehen, sonst wäre die
    # Anonymisierung wirkungslos.
    assert "gpu-vm-01" not in d["prompt"]
    assert "vllm@gpu-vm-01:8000" not in d["prompt"]
    assert any(e["original"] == "gpu-vm-01" for e in d["legend"])


async def test_prompt_als_text(client):
    r = await client.post("/api/v1/diagnostics/prompt", json={"window_s": 900})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text.startswith("# Fehlerbericht")


async def test_events_filter(client):
    await client.post("/api/v1/ingest/batch", json={
        "node": {"name": "n"},
        "events": [
            {"kind": "scale.up", "severity": "info", "title": "hoch"},
            {"kind": "privacy.blocked", "severity": "warning", "title": "geblockt"},
            {"kind": "gpu.oom", "severity": "error", "title": "CUDA OOM"},
        ],
    })
    d = (await client.get("/api/v1/events",
                          params={"since": "-1h", "severity": "error,warning"})).json()
    assert d["total"] == 2

    d = (await client.get("/api/v1/events",
                          params={"since": "-1h", "kind": "privacy."})).json()
    assert d["total"] == 1

    d = (await client.get("/api/v1/events",
                          params={"since": "-1h", "search": "CUDA"})).json()
    assert d["total"] == 1


async def test_metrik_katalog_waechst_ohne_migration(client):
    await client.post("/api/v1/ingest/batch", json={
        "node": {"name": "n"},
        "samples": [{"metric": "voellig.neue_metrik", "value": 1,
                     "labels": {"dimension": "x"}}],
    })
    cat = (await client.get("/api/v1/metrics-catalog")).json()
    entry = next(c for c in cat if c["metric"] == "voellig.neue_metrik")
    assert entry["series"] == 1
    assert "dimension" in entry["label_keys"]


async def test_token_anlegen_und_widerrufen(client):
    r = await client.post("/api/v1/tokens",
                          json={"name": "agent-x", "scopes": ["ingest"]})
    assert r.status_code == 200
    tok = r.json()["token"]
    assert tok.startswith("aimon_")

    c = httpx.AsyncClient(transport=client._transport, base_url="http://test",
                          headers={"Authorization": f"Bearer {tok}"})
    assert (await c.get("/api/v1/ingest/echo")).status_code == 200

    await client.delete(f"/api/v1/tokens/{r.json()['id']}")
    assert (await c.get("/api/v1/ingest/echo")).status_code == 401
    await c.aclose()


async def test_token_wird_nur_als_hash_gespeichert(client, db):
    r = await client.post("/api/v1/tokens", json={"name": "geheim"})
    tok = r.json()["token"]
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM api_tokens WHERE name = 'geheim'")
    assert tok not in str(dict(row))
    assert len(row["token_hash"]) == 64


async def test_bericht_zeigt_ssh_zugang(client):
    """Der Bericht soll sagen, wie man auf die Maschine kommt."""
    await client.post("/api/v1/ingest/batch", json={
        "node": {"name": "ki", "agent_version": "0.1.0",
                 "labels": {"ip": "10.0.0.5", "ssh_user": "kvkh",
                            "ssh_port": "2222", "site": "rack-a"}},
        "samples": [{"metric": "gpu.temp_c", "value": 60, "ts": ts(),
                     "labels": {"gpu": "0"}}],
    })
    d = (await client.get("/api/v1/diagnostics/prompt",
                          params={"window_s": 3600})).json()
    assert "ssh -p 2222 kvkh@10.0.0.5" in d["prompt"]
    assert "Zugang" in d["prompt"]
    # Die Adresse steht in ihrer Spalte, nicht zusätzlich in den Labels.
    assert "ip=10.0.0.5" not in d["prompt"]
    assert "site=rack-a" in d["prompt"]


async def test_bericht_mit_platzhaltern_zeigt_keinen_zugang(client):
    """Ein anonymisierter Bericht darf keinen ssh-Befehl enthalten -
    der würde die Anonymisierung sofort aufheben."""
    await client.post("/api/v1/ingest/batch", json={
        "node": {"name": "ki",
                 "labels": {"ip": "10.0.0.5", "ssh_user": "kvkh"}},
        "samples": [{"metric": "gpu.temp_c", "value": 60, "ts": ts(),
                     "labels": {"gpu": "0"}}],
    })
    d = (await client.get("/api/v1/diagnostics/prompt",
                          params={"window_s": 3600, "redact": "true"})).json()
    assert "10.0.0.5" not in d["prompt"]
    assert "kvkh" not in d["prompt"]
    assert "Zugang" not in d["prompt"]
