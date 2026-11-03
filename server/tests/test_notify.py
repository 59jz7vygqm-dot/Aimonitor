"""Benachrichtigungen. Der Kern ist Zurückhaltung – das wird hier geprüft."""
from __future__ import annotations

import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from aimon.diagnostics import notify
from aimon.models import Batch
from aimon.tsdb import writer

pytestmark = pytest.mark.asyncio


class _Collector(BaseHTTPRequestHandler):
    received: list = []
    status = 200

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        type(self).received.append({
            "json": json.loads(body) if body else None,
            "headers": dict(self.headers),
        })
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def webhook(monkeypatch):
    """Ein echter HTTP-Server, der die Benachrichtigungen einsammelt."""
    _Collector.received = []
    _Collector.status = 200
    srv = HTTPServer(("127.0.0.1", 0), _Collector)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    from aimon.config import settings

    monkeypatch.setattr(settings, "alert_webhook_url",
                        f"http://127.0.0.1:{srv.server_port}/hook")
    monkeypatch.setattr(settings, "alert_smtp_host", None)
    monkeypatch.setattr(settings, "alert_min_severity", "critical")
    monkeypatch.setattr(settings, "alert_min_duration_s", 300)
    monkeypatch.setattr(settings, "alert_max_per_hour", 10)
    monkeypatch.setattr(settings, "alert_send_resolved", True)
    yield _Collector
    srv.shutdown()


async def _incident(db, *, severity="critical", age_min=10, rule="gpu.temp_high",
                    subject="n/gpu0", ended=False):
    now = dt.datetime.now(dt.timezone.utc)
    started = now - dt.timedelta(minutes=age_min)
    async with db.pool().acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO incidents (rule, severity, subject_kind, subject, title,
                                   started_at, last_seen_at, ended_at, context)
            VALUES ($1,$2,'gpu',$3,$4,$5,now(),$6,$7) RETURNING id
            """,
            rule, severity, subject, f"GPU {subject} zu heiß", started,
            now if ended else None, {"temp_c": 92, "warn_c": 83},
        )


async def test_ohne_kanal_passiert_nichts(db, monkeypatch):
    from aimon.config import settings

    monkeypatch.setattr(settings, "alert_webhook_url", None)
    monkeypatch.setattr(settings, "alert_smtp_host", None)
    assert notify.enabled() is False
    await _incident(db)
    assert (await notify.run_once())["sent"] == 0


async def test_kritischer_incident_wird_gemeldet(db, webhook):
    await _incident(db, severity="critical", age_min=10)
    res = await notify.run_once()
    assert res["sent"] == 1
    assert len(webhook.received) == 1
    p = webhook.received[0]["json"]
    assert p["kind"] == "fired"
    assert p["severity"] == "critical"
    assert p["rule"] == "gpu.temp_high"
    assert p["context"]["temp_c"] == 92
    # Für Dienste wie ntfy, die den Titel im Header erwarten
    assert "KRITISCH" in webhook.received[0]["headers"]["X-Title"]


async def test_junger_incident_wird_nicht_gemeldet(db, webhook):
    """Was von selbst wieder verschwindet, soll niemanden wecken."""
    await _incident(db, severity="critical", age_min=1)
    assert (await notify.run_once())["sent"] == 0
    assert webhook.received == []


async def test_warnung_wird_bei_schwelle_critical_nicht_gemeldet(db, webhook):
    await _incident(db, severity="warning", age_min=30)
    assert (await notify.run_once())["sent"] == 0


async def test_schwelle_kann_gesenkt_werden(db, webhook, monkeypatch):
    from aimon.config import settings

    monkeypatch.setattr(settings, "alert_min_severity", "warning")
    await _incident(db, severity="warning", age_min=30)
    assert (await notify.run_once())["sent"] == 1


async def test_nur_einmal_pro_incident(db, webhook):
    """Auch wenn der Regeldurchlauf alle 30 s läuft."""
    await _incident(db, age_min=10)
    for _ in range(5):
        await notify.run_once()
    assert len(webhook.received) == 1


async def test_entwarnung_nach_alarm(db, webhook):
    inc = await _incident(db, age_min=10)
    await notify.run_once()
    assert len(webhook.received) == 1

    async with db.pool().acquire() as conn:
        await conn.execute("UPDATE incidents SET ended_at = now() WHERE id = $1", inc)
    await notify.run_once()
    assert len(webhook.received) == 2
    assert webhook.received[1]["json"]["kind"] == "resolved"
    assert webhook.received[1]["json"]["ended_at"] is not None

    # Und nicht nochmal
    await notify.run_once()
    assert len(webhook.received) == 2


async def test_keine_entwarnung_ohne_alarm(db, webhook):
    """Ein Incident, der nie gemeldet wurde, braucht keine Entwarnung."""
    await _incident(db, age_min=1, ended=True)
    await notify.run_once()
    assert webhook.received == []


async def test_obergrenze_pro_stunde(db, webhook, monkeypatch):
    """Fällt ein Switch aus, sind sofort zwanzig Dinge kritisch."""
    from aimon.config import settings

    monkeypatch.setattr(settings, "alert_max_per_hour", 3)
    for i in range(8):
        await _incident(db, subject=f"n/gpu{i}", age_min=10)
    await notify.run_once()
    assert len(webhook.received) == 3

    async with db.pool().acquire() as conn:
        unterdrueckt = await conn.fetchval(
            "SELECT count(*) FROM alert_log WHERE NOT ok AND error LIKE 'unterdrückt%'")
    assert unterdrueckt >= 1
    assert notify.stats()["suppressed_rate"] >= 1


async def test_fehlschlag_wird_erneut_versucht(db, webhook):
    """Ein Ausfall des Empfängers darf die Meldung nicht verschlucken."""
    webhook.status = 500
    await _incident(db, age_min=10)
    await notify.run_once()
    assert len(webhook.received) == 1          # Versuch fand statt

    async with db.pool().acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM alert_log ORDER BY id DESC LIMIT 1")
    assert row["ok"] is False
    assert row["error"]

    webhook.status = 200
    await notify.run_once()
    assert len(webhook.received) == 2
    async with db.pool().acquire() as conn:
        ok = await conn.fetchval("SELECT count(*) FROM alert_log WHERE ok")
    assert ok == 1


async def test_bestaetigter_incident_meldet_sich_nicht(db, webhook):
    inc = await _incident(db, age_min=10)
    async with db.pool().acquire() as conn:
        await conn.execute(
            "UPDATE incidents SET acknowledged = true WHERE id = $1", inc)
    assert (await notify.run_once())["sent"] == 0


async def test_testnachricht(db, webhook):
    res = await notify.send_test()
    assert res["ok"] is True
    assert res["results"]["webhook"] == "ok"
    assert webhook.received[0]["json"]["rule"] == "aimon.test"


async def test_mailtext_enthaelt_den_kontext(db):
    now = dt.datetime.now(dt.timezone.utc)
    row = {
        "id": 1, "rule": "llm.ttft_high", "severity": "critical",
        "subject": "vllm@ki:8000", "subject_kind": "service",
        "title": "TTFT p95 bei 9000 ms",
        "started_at": now - dt.timedelta(minutes=12), "ended_at": None,
        "context": {"p95_ttft_ms": 9000,
                    "mögliche_ursachen": ["KV-Cache voll", "GPU throttelt"]},
        "age_s": 720.0,
    }
    text = notify._body_text(row, "fired")
    assert "TTFT p95 bei 9000 ms" in text
    assert "llm.ttft_high" in text
    assert "vllm@ki:8000" in text
    assert "KV-Cache voll" in text
    assert "Besteht seit 12 min" in text
    # Der Empfänger muss wissen, wann sich das System überhaupt meldet -
    # sonst hält er eine ausbleibende Mail für "alles in Ordnung".
    assert "ab Schwere 'critical'" in text
    assert "300 s" in text
