"""Schreib- und Lesepfad gegen eine echte PostgreSQL-Instanz."""
from __future__ import annotations

import datetime as dt

import pytest

from aimon.models import Batch
from aimon.tsdb import query as q
from aimon.tsdb import writer

pytestmark = pytest.mark.asyncio


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def write(db, payload: dict):
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            return await writer.write_batch(conn, Batch.model_validate(payload))


async def test_node_und_service_werden_angelegt(db):
    res = await write(db, {
        "node": {"name": "gpu-1", "hostname": "gpu-1.lan", "agent_version": "0.1.0",
                 "labels": {"site": "a"}},
        "services": [{"key": "vllm@gpu-1:8000", "kind": "vllm", "name": "vLLM"}],
        "samples": [{"metric": "gpu.temp_c", "value": 70, "labels": {"gpu": "0"}}],
    })
    assert res.rejected == 0
    async with db.pool().acquire() as conn:
        n = await conn.fetchrow("SELECT * FROM nodes WHERE name = 'gpu-1'")
        assert n["hostname"] == "gpu-1.lan"
        assert n["labels"] == {"site": "a"}
        s = await conn.fetchrow("SELECT * FROM services WHERE key = 'vllm@gpu-1:8000'")
        assert s["kind"] == "vllm"
        assert s["node_id"] == n["id"]


async def test_serie_ist_pro_node_getrennt(db):
    """Dieselbe Metrik mit denselben Labels auf zwei Maschinen muss zwei
    Serien ergeben - sonst überschreiben sich die Hosts gegenseitig."""
    for host, val in (("a", 10.0), ("b", 20.0)):
        await write(db, {
            "node": {"name": host},
            "samples": [{"metric": "host.disk_used_bytes", "value": val,
                         "labels": {"mount": "/"}}],
        })
    async with db.pool().acquire() as conn:
        rows = await q.latest(conn, "host.disk_used_bytes")
    assert len(rows) == 2
    assert {r["node"]: r["value"] for r in rows} == {"a": 10.0, "b": 20.0}


async def test_serie_ist_pro_service_getrennt(db):
    await write(db, {
        "node": {"name": "n"},
        "services": [{"key": "vllm@n:8000", "kind": "vllm"},
                     {"key": "vllm@n:8001", "kind": "vllm"}],
        "samples": [
            {"metric": "llm.requests_running", "value": 3, "service": "vllm@n:8000"},
            {"metric": "llm.requests_running", "value": 7, "service": "vllm@n:8001"},
        ],
    })
    async with db.pool().acquire() as conn:
        rows = await q.latest(conn, "llm.requests_running")
    assert {r["service"]: r["value"] for r in rows} == {
        "vllm@n:8000": 3.0, "vllm@n:8001": 7.0}


async def test_unbekannter_service_in_requests_wird_abgelehnt_nicht_verschluckt(db):
    res = await write(db, {
        "node": {"name": "n"},
        "requests": [{"service": "gibt-es-nicht", "ttft_ms": 100}],
    })
    assert res.rejected == 1
    assert "gibt-es-nicht" in res.errors[0]


async def test_counter_rate_wird_berechnet(db):
    base = now() - dt.timedelta(seconds=60)
    await write(db, {
        "node": {"name": "n"},
        "samples": [{"metric": "llm.prompt_tokens_total", "value": 1000,
                     "ts": base.timestamp()}],
    })
    await write(db, {
        "node": {"name": "n"},
        "samples": [{"metric": "llm.prompt_tokens_total", "value": 1600,
                     "ts": (base + dt.timedelta(seconds=60)).timestamp()}],
    })
    async with db.pool().acquire() as conn:
        rows = await q.latest(conn, "llm.prompt_tokens_total")
    assert rows[0]["value"] == 1600
    assert rows[0]["rate"] == pytest.approx(10.0, rel=0.02)   # 600 / 60 s


async def test_probe_erzeugt_genau_eine_kante(db):
    """Wiederholte Probes dürfen die Kantentabelle nicht aufblähen."""
    for _ in range(25):
        await write(db, {
            "node": {"name": "n"},
            "services": [{"key": "owui@h:3000", "kind": "openwebui"}],
            "probes": [{"target": "http://h:3000/health",
                        "target_service": "owui@h:3000", "ok": True,
                        "latency_ms": 5}],
        })
    async with db.pool().acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM edges") == 1
        e = await conn.fetchrow("SELECT * FROM edges")
        assert e["samples"] == 25
        assert e["fails"] == 0


async def test_kante_zaehlt_fehler_mit(db):
    for ok in (True, True, False, False, False):
        await write(db, {
            "node": {"name": "n"},
            "services": [{"key": "x@h:1", "kind": "other"}],
            "probes": [{"target": "http://h:1/", "target_service": "x@h:1",
                        "ok": ok, "error": None if ok else "timeout"}],
        })
    async with db.pool().acquire() as conn:
        e = await conn.fetchrow("SELECT * FROM edges")
    assert e["samples"] == 5
    assert e["fails"] == 3
    assert e["ok"] is False
    assert e["last_ok_ts"] is not None and e["last_fail_ts"] is not None


async def test_dedup_key_fasst_ereignisse_zusammen(db):
    for _ in range(5):
        await write(db, {
            "node": {"name": "n"},
            "events": [{"kind": "privacy.blocked", "severity": "warning",
                        "title": "geblockt", "dedup_key": "privacy:person"}],
        })
    async with db.pool().acquire() as conn:
        rows = await conn.fetch("SELECT * FROM events")
    assert len(rows) == 1
    assert rows[0]["count"] == 5


async def test_rohtext_wird_ohne_freigabe_verworfen(db, monkeypatch):
    """Der Default muss PII fallen lassen - und das sichtbar vermerken."""
    from aimon.config import settings
    monkeypatch.setattr(settings, "allow_raw_queries", False)
    await write(db, {
        "node": {"name": "n"},
        "events": [{"kind": "privacy.blocked", "severity": "warning",
                    "title": "geblockt",
                    "detail": {"query_raw": "wohnort von Max Mustermann",
                               "category": "person_name"}}],
    })
    async with db.pool().acquire() as conn:
        ev = await conn.fetchrow("SELECT * FROM events")
        assert "query_raw" not in ev["detail"]
        assert ev["detail"]["query_raw_dropped"] is True
        assert await conn.fetchval("SELECT count(*) FROM raw_queries") == 0


async def test_rohtext_mit_freigabe_getrennt_gespeichert(db, monkeypatch):
    from aimon.config import settings
    monkeypatch.setattr(settings, "allow_raw_queries", True)
    await write(db, {
        "node": {"name": "n"},
        "events": [{"kind": "privacy.blocked", "severity": "warning",
                    "title": "geblockt",
                    "detail": {"query_raw": "wohnort von Max Mustermann"}}],
    })
    async with db.pool().acquire() as conn:
        ev = await conn.fetchrow("SELECT * FROM events")
        raw = await conn.fetchrow("SELECT * FROM raw_queries")
    # Auch mit Freigabe nicht im Ereignis selbst, sondern in der Tabelle mit
    # eigener, kürzerer Aufbewahrung.
    assert "query_raw" not in ev["detail"]
    assert raw["query_raw"] == "wohnort von Max Mustermann"
    assert raw["event_id"] == ev["id"]


async def test_range_query_und_rollup(db):
    from aimon.tsdb import rollup

    base = now() - dt.timedelta(minutes=30)
    pts = [{"metric": "gpu.temp_c", "value": 60 + i % 10,
            "ts": (base + dt.timedelta(seconds=10 * i)).timestamp(),
            "labels": {"gpu": "0"}} for i in range(120)]
    await write(db, {"node": {"name": "n"}, "samples": pts})

    async with db.pool().acquire() as conn:
        metas = await q.find_series(conn, "gpu.temp_c")
        assert len(metas) == 1
        data, source, bucket = await q.range_query(
            conn, [metas[0].id], base, now(), "avg", 200)
    assert source == "raw"
    assert len(data[metas[0].id]) > 10

    written = await rollup.rollup_once(60)
    assert written > 0

    # 1 Tag auf 200 Punkte -> ca. 430 s pro Punkt -> 1-Minuten-Rollup.
    async with db.pool().acquire() as conn:
        data, source, bucket = await q.range_query(
            conn, [metas[0].id], now() - dt.timedelta(days=1), now(), "avg", 200)
    assert source == "rollup_60"
    assert bucket == 60
    assert data[metas[0].id], "Rollup-Daten müssen im längeren Fenster erscheinen"


async def test_luecken_bleiben_luecken(db):
    """Fehlende Zeiträume dürfen nicht interpoliert werden."""
    base = now() - dt.timedelta(minutes=20)
    pts = []
    for i in list(range(0, 10)) + list(range(50, 60)):
        pts.append({"metric": "x.y", "value": 1.0,
                    "ts": (base + dt.timedelta(seconds=10 * i)).timestamp()})
    await write(db, {"node": {"name": "n"}, "samples": pts})
    async with db.pool().acquire() as conn:
        metas = await q.find_series(conn, "x.y")
        data, _, _ = await q.range_query(conn, [metas[0].id], base, now(), "avg", 60)
    pts_out = data[metas[0].id]
    # Es darf nur Buckets geben, in denen wirklich Daten lagen.
    assert all(v is not None for _, v in pts_out)
    assert len(pts_out) < 40


async def test_quantile_aus_requests(db):
    await write(db, {
        "node": {"name": "n"},
        "services": [{"key": "vllm@n:8000", "kind": "vllm"}],
        "requests": [{"service": "vllm@n:8000", "model": "m", "ttft_ms": float(v),
                      "prompt_tokens": 100} for v in range(1, 101)],
    })
    async with db.pool().acquire() as conn:
        res = await q.quantiles_from_requests(
            conn, now() - dt.timedelta(hours=1), now() + dt.timedelta(minutes=1))
    assert res["source"] == "requests"
    assert res["n"] == 100
    assert res["quantiles"]["0.5"] == pytest.approx(50.5, abs=1)
    assert res["quantiles"]["0.9"] == pytest.approx(90.1, abs=1.5)


async def test_quantile_aus_histogramm(db):
    """Nur der Zuwachs im Fenster zählt, nicht der Stand seit Prozessstart."""
    t0 = now() - dt.timedelta(minutes=10)
    t1 = now() - dt.timedelta(minutes=1)
    # Vorbelastung: 1000 Requests vor dem Fenster
    pre = {"0.1": 900.0, "0.5": 1000.0, "1.0": 1000.0, "+Inf": 1000.0}
    # Im Fenster kommen 100 dazu, alle langsam (>0.5 s)
    post = {"0.1": 900.0, "0.5": 1000.0, "1.0": 1100.0, "+Inf": 1100.0}
    for ts, vals in ((t0, pre), (t1, post)):
        await write(db, {
            "node": {"name": "n"},
            "services": [{"key": "vllm@n:8000", "kind": "vllm"}],
            "samples": [{"metric": "llm.ttft_bucket", "value": v,
                         "ts": ts.timestamp(), "service": "vllm@n:8000",
                         "labels": {"le": le}} for le, v in vals.items()],
        })
    async with db.pool().acquire() as conn:
        res = await q.quantiles_from_histogram(
            conn, "llm.ttft_bucket", t0 - dt.timedelta(seconds=1), now())
    assert res["source"] == "histogram"
    assert res["n"] == 100
    # Alle 100 neuen lagen zwischen 0,5 und 1,0 s -> p50 muss dort liegen
    assert 500 <= res["quantiles"]["0.5"] <= 1000


async def test_counter_ratio(db):
    t0 = now() - dt.timedelta(minutes=10)
    t1 = now() - dt.timedelta(minutes=1)
    for ts, s, c in ((t0, 1000.0, 10.0), (t1, 3000.0, 20.0)):
        await write(db, {
            "node": {"name": "n"},
            "services": [{"key": "vllm@n:8000", "kind": "vllm"}],
            "samples": [
                {"metric": "llm.prompt_len_sum", "value": s, "ts": ts.timestamp(),
                 "service": "vllm@n:8000"},
                {"metric": "llm.prompt_len_count", "value": c, "ts": ts.timestamp(),
                 "service": "vllm@n:8000"},
            ],
        })
    async with db.pool().acquire() as conn:
        res = await q.counter_ratio(conn, "llm.prompt_len_sum",
                                    "llm.prompt_len_count",
                                    t0 - dt.timedelta(seconds=1), now())
    # (3000-1000) / (20-10) = 200 Tokens im Schnitt
    assert res["value"] == pytest.approx(200.0)
    assert res["n"] == 10


async def test_rollup_erfasst_nachtraeglich_eingetroffene_daten(db):
    """Ein Agent puffert bei Serverausfall und schickt danach alte Zeitstempel.
    Diese Daten dürfen nicht durch den Rollup fallen."""
    from aimon.tsdb import rollup

    # Erst nur ganz frische Daten -> Wasserstand wandert nach vorn
    recent = now() - dt.timedelta(minutes=2)
    await write(db, {"node": {"name": "n"}, "samples": [
        {"metric": "late.test", "value": 1.0,
         "ts": (recent + dt.timedelta(seconds=10 * i)).timestamp()}
        for i in range(6)]})
    await rollup.rollup_once(60)

    # Jetzt kommt der gepufferte Block mit Zeitstempeln von vor 10 Minuten
    old = now() - dt.timedelta(minutes=10)
    await write(db, {"node": {"name": "n"}, "samples": [
        {"metric": "late.test", "value": 5.0,
         "ts": (old + dt.timedelta(seconds=10 * i)).timestamp()}
        for i in range(12)]})
    await rollup.rollup_once(60)

    async with db.pool().acquire() as conn:
        sid = await conn.fetchval("SELECT id FROM series WHERE metric = 'late.test'")
        rows = await conn.fetch(
            "SELECT ts, n, max FROM samples_rollup WHERE bucket_s = 60 "
            "AND series_id = $1 ORDER BY ts", sid)
    buckets = {r["ts"]: r for r in rows}
    assert any(r["max"] == 5.0 for r in rows), \
        "nachträglich eingetroffene Samples fehlen im Rollup"
    assert sum(r["n"] for r in rows) == 18


async def test_rollup_ohne_daten_setzt_keinen_wasserstand(db):
    """Sonst würden die ersten echten Samples übersprungen."""
    from aimon.tsdb import rollup

    assert await rollup.rollup_once(60) == 0
    async with db.pool().acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM rollup_state WHERE bucket_s = 60") == 0

    base = now() - dt.timedelta(minutes=5)
    await write(db, {"node": {"name": "n"}, "samples": [
        {"metric": "a.b", "value": 2.0,
         "ts": (base + dt.timedelta(seconds=10 * i)).timestamp()} for i in range(12)]})
    assert await rollup.rollup_once(60) > 0

    async with db.pool().acquire() as conn:
        total = await conn.fetchval(
            "SELECT sum(n) FROM samples_rollup WHERE bucket_s = 60")
    assert total == 12
