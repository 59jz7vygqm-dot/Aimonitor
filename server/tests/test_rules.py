"""Regelwerk: feuert es bei echten Problemen, schweigt es sonst."""
from __future__ import annotations

import datetime as dt

import pytest

from aimon.diagnostics import rules
from aimon.models import Batch
from aimon.tsdb import writer

pytestmark = pytest.mark.asyncio


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def write(db, payload: dict):
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            return await writer.write_batch(conn, Batch.model_validate(payload))


def by_rule(findings):
    out = {}
    for f in findings:
        out.setdefault(f.rule, []).append(f)
    return out


async def test_gesunder_stack_erzeugt_keine_findings(db):
    await write(db, {
        "node": {"name": "n"},
        "services": [{"key": "vllm@n:8000", "kind": "vllm"}],
        "samples": [
            {"metric": "gpu.temp_c", "value": 62, "labels": {"gpu": "0"}},
            {"metric": "gpu.mem_used_bytes", "value": 10e9, "labels": {"gpu": "0"}},
            {"metric": "gpu.mem_total_bytes", "value": 24e9, "labels": {"gpu": "0"}},
            {"metric": "gpu.throttle_thermal", "value": 0, "labels": {"gpu": "0"}},
            {"metric": "host.disk_used_bytes", "value": 100e9, "labels": {"mount": "/"}},
            {"metric": "host.disk_total_bytes", "value": 1e12, "labels": {"mount": "/"}},
            {"metric": "llm.kv_cache_usage_pct", "value": 40, "service": "vllm@n:8000"},
            {"metric": "llm.requests_waiting", "value": 0, "service": "vllm@n:8000"},
        ],
        "requests": [{"service": "vllm@n:8000", "ttft_ms": 180.0, "status": 200}],
    })
    f = by_rule(await rules.evaluate())
    assert "gpu.temp_high" not in f
    assert "gpu.vram_high" not in f
    assert "host.disk_full" not in f
    assert "llm.ttft_high" not in f
    assert "llm.kv_cache_high" not in f
    assert "node.stale" not in f


async def test_heisse_gpu_mit_throttling(db):
    await write(db, {
        "node": {"name": "n"},
        "samples": [
            {"metric": "gpu.temp_c", "value": 90, "labels": {"gpu": "0"}},
            {"metric": "gpu.throttle_thermal", "value": 1, "labels": {"gpu": "0"}},
            {"metric": "gpu.util_pct", "value": 95, "labels": {"gpu": "0"}},
        ],
    })
    f = by_rule(await rules.evaluate())
    assert f["gpu.temp_high"][0].severity == "critical"
    assert f["gpu.temp_high"][0].subject == "n/gpu0"
    assert f["gpu.temp_high"][0].context["temp_c"] == 90
    assert "gpu.throttling" in f
    assert "thermisch" in f["gpu.throttling"][0].title


async def test_vram_fast_voll(db):
    await write(db, {
        "node": {"name": "n"},
        "samples": [
            {"metric": "gpu.mem_used_bytes", "value": 23.5e9, "labels": {"gpu": "0"}},
            {"metric": "gpu.mem_total_bytes", "value": 24e9, "labels": {"gpu": "0"}},
        ],
    })
    f = by_rule(await rules.evaluate())
    assert "gpu.vram_high" in f
    ctx = f["gpu.vram_high"][0].context
    assert ctx["pct"] == pytest.approx(97.9, abs=0.2)
    # Der Kontext muss erklären, warum hohe Belegung bei vLLM normal sein kann.
    assert "hinweis" in ctx


async def test_ttft_regel_nennt_die_quelle(db):
    await write(db, {
        "node": {"name": "n"},
        "services": [{"key": "vllm@n:8000", "kind": "vllm"}],
        "requests": [{"service": "vllm@n:8000", "ttft_ms": 9000.0,
                      "prompt_tokens": 2000, "status": 200} for _ in range(20)],
    })
    f = by_rule(await rules.evaluate())
    inc = f["llm.ttft_high"][0]
    assert inc.severity == "critical"
    assert inc.context["quelle"].startswith("requests")
    assert inc.context["p95_ttft_ms"] >= 8000
    assert len(inc.context["mögliche_ursachen"]) >= 3


async def test_fehlerrate_wird_erst_ab_schwelle_gemeldet(db):
    """Ein einzelner 500er bei 100 Requests ist kein Incident."""
    await write(db, {
        "node": {"name": "n"},
        "services": [{"key": "vllm@n:8000", "kind": "vllm"}],
        "requests": [{"service": "vllm@n:8000", "ttft_ms": 100.0,
                      "status": 500 if i == 0 else 200} for i in range(100)],
    })
    assert "llm.errors" not in by_rule(await rules.evaluate())


async def test_fehlerrate_wird_ab_schwelle_gemeldet(db):
    await write(db, {
        "node": {"name": "n"},
        "services": [{"key": "vllm@n:8000", "kind": "vllm"}],
        "requests": [{"service": "vllm@n:8000", "ttft_ms": 100.0,
                      "status": 500 if i < 10 else 200} for i in range(100)],
    })
    f = by_rule(await rules.evaluate())
    assert f["llm.errors"][0].context["error_rate_pct"] == 10.0


async def test_fehlgeschlagene_probe_wird_incident(db):
    for _ in range(6):
        await write(db, {
            "node": {"name": "n"},
            "services": [{"key": "pp@h:8888", "kind": "privacy_proxy"}],
            "probes": [{"target": "http://h:8888/healthz",
                        "target_service": "pp@h:8888", "ok": False,
                        "error": "dial tcp: i/o timeout"}],
        })
    f = by_rule(await rules.evaluate())
    inc = f["probe.failing"][0]
    assert inc.severity == "critical"
    assert inc.context["last_error"] == "dial tcp: i/o timeout"
    assert inc.context["failed"] == 6


async def test_headroom_regel(db):
    await write(db, {
        "node": {"name": "n"},
        "services": [{"key": "ot@n:7681", "kind": "openterminal_proxy"}],
        "samples": [
            {"metric": "openterminal.headroom_pct", "value": 3, "service": "ot@n:7681"},
            {"metric": "openterminal.instances_running", "value": 12,
             "service": "ot@n:7681"},
            {"metric": "openterminal.instances_max", "value": 12, "service": "ot@n:7681"},
            {"metric": "openterminal.sessions_active", "value": 116,
             "service": "ot@n:7681"},
        ],
    })
    f = by_rule(await rules.evaluate())
    assert f["openterminal.headroom_low"][0].severity == "critical"
    assert f["openterminal.headroom_low"][0].context["instances_max"] == 12


async def test_blockrate_regel_liefert_kategorien(db):
    await write(db, {
        "node": {"name": "n"},
        "services": [{"key": "pp@n:8888", "kind": "privacy_proxy"}],
        "samples": [
            {"metric": "privacy.block_rate_pct", "value": 46, "service": "pp@n:8888"},
            {"metric": "privacy.requests_total", "value": 1000, "service": "pp@n:8888"},
            {"metric": "privacy.blocked_total", "value": 460, "service": "pp@n:8888",
             "labels": {"category": "person_name"}},
            {"metric": "privacy.blocked_total", "value": 460, "service": "pp@n:8888",
             "labels": {"category": "_all"}},
        ],
    })
    f = by_rule(await rules.evaluate())
    ctx = f["privacy.block_rate_high"][0].context
    assert ctx["blocked_by_category"]["person_name"] == 460
    # Die Regel muss sagen, wie man False Positives von echten Treffern trennt.
    assert "deutung" in ctx


async def test_incidents_werden_geoeffnet_und_geschlossen(db):
    await write(db, {
        "node": {"name": "n"},
        "samples": [{"metric": "gpu.temp_c", "value": 92, "labels": {"gpu": "0"}}],
    })
    r1 = await rules.reconcile(await rules.evaluate())
    assert r1["opened"] >= 1

    # Zweiter Lauf ohne Änderung: nichts Neues, nur Aktualisierung
    r2 = await rules.reconcile(await rules.evaluate())
    assert r2["opened"] == 0
    assert r2["updated"] >= 1

    # Abkühlung -> der Incident muss sich schließen
    await write(db, {
        "node": {"name": "n"},
        "samples": [{"metric": "gpu.temp_c", "value": 60, "labels": {"gpu": "0"}}],
    })
    r3 = await rules.reconcile(await rules.evaluate())
    assert r3["closed"] >= 1

    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM incidents WHERE rule = 'gpu.temp_high'")
    assert row["ended_at"] is not None
    assert row["started_at"] < row["ended_at"]


async def test_stale_node_wird_als_stumm_gemeldet_nicht_als_null(db):
    """Ein stummer Agent ist kein Host mit 0 % Last - das muss die Regel sagen."""
    await write(db, {"node": {"name": "alt"},
                     "samples": [{"metric": "host.cpu_pct", "value": 20}]})
    async with db.pool().acquire() as conn:
        await conn.execute(
            "UPDATE nodes SET last_seen = now() - INTERVAL '30 minutes' "
            "WHERE name = 'alt'")
    f = by_rule(await rules.evaluate())
    inc = f["node.stale"][0]
    assert inc.subject == "alt"
    assert "alt, nicht niedrig" in inc.context["bedeutung"]


async def test_power_cap_unter_last_ist_keine_warnung(db):
    """Eine L4 mit 70 von 72 W bei 100 % Auslastung arbeitet bestimmungsgemäß.
    Als Warnung gemeldet würde das Dauerfeuer erzeugen."""
    await write(db, {
        "node": {"name": "ki"},
        "samples": [
            {"metric": "gpu.throttle_power", "value": 1, "labels": {"gpu": "0"}},
            {"metric": "gpu.throttle_thermal", "value": 0, "labels": {"gpu": "0"}},
            {"metric": "gpu.util_pct", "value": 100, "labels": {"gpu": "0"}},
            {"metric": "gpu.power_w", "value": 70, "labels": {"gpu": "0"}},
            {"metric": "gpu.power_limit_w", "value": 72, "labels": {"gpu": "0"}},
            {"metric": "gpu.temp_c", "value": 59, "labels": {"gpu": "0"}},
        ],
    })
    f = by_rule(await rules.evaluate())
    assert "gpu.throttling" not in f
    assert "gpu.power_cap_idle" not in f


async def test_power_cap_flag_ohne_last_und_ohne_verbrauch_ist_kein_incident(db):
    """Ein gesetztes Flag bei 3 von 72 W ist auf Modulen mit gemeinsamem
    Leistungsbudget der Normalzustand. Ein Incident, der sich nie auflöst,
    wäre Dauerinventar statt Information – sichtbar bleibt es am Dashboard."""
    await write(db, {
        "node": {"name": "spark01"},
        "samples": [
            {"metric": "gpu.throttle_power", "value": 1, "labels": {"gpu": "0"}},
            {"metric": "gpu.util_pct", "value": 0, "labels": {"gpu": "0"}},
            {"metric": "gpu.power_w", "value": 3, "labels": {"gpu": "0"}},
            {"metric": "gpu.power_limit_w", "value": 72, "labels": {"gpu": "0"}},
        ],
    })
    assert "gpu.power_cap_idle" not in by_rule(await rules.evaluate())


async def test_power_cap_am_limit_ohne_last_ist_warnung(db):
    """Nahe am Limit bei fast keiner Last: da stimmt etwas nicht.
    Das ist der einzige Fall, der gemeldet wird."""
    await write(db, {
        "node": {"name": "n"},
        "samples": [
            {"metric": "gpu.throttle_power", "value": 1, "labels": {"gpu": "0"}},
            {"metric": "gpu.util_pct", "value": 5, "labels": {"gpu": "0"}},
            {"metric": "gpu.power_w", "value": 69, "labels": {"gpu": "0"}},
            {"metric": "gpu.power_limit_w", "value": 72, "labels": {"gpu": "0"}},
        ],
    })
    f = by_rule(await rules.evaluate())
    inc = f["gpu.power_cap_idle"][0]
    assert inc.severity == "warning"
    assert inc.context["util_pct"] == 5
    assert "zu_prüfen" in inc.context


async def test_thermisches_throttling_bleibt_warnung(db):
    """Thermisch heißt: Kühlung reicht nicht. Das bleibt eine Warnung,
    egal bei welcher Last."""
    await write(db, {
        "node": {"name": "n"},
        "samples": [
            {"metric": "gpu.throttle_thermal", "value": 1, "labels": {"gpu": "0"}},
            {"metric": "gpu.throttle_power", "value": 1, "labels": {"gpu": "0"}},
            {"metric": "gpu.util_pct", "value": 100, "labels": {"gpu": "0"}},
            {"metric": "gpu.temp_c", "value": 86, "labels": {"gpu": "0"}},
        ],
    })
    f = by_rule(await rules.evaluate())
    assert f["gpu.throttling"][0].severity == "warning"
    assert "thermisch" in f["gpu.throttling"][0].title
    # Das gleichzeitige Power-Cap unter Volllast taucht NICHT zusätzlich auf.
    assert "gpu.power_cap_idle" not in f
