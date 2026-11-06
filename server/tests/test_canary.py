"""Der Kanarienvogel muss zwischen "kaputt" und "langsam" unterscheiden.

Getestet wird gegen einen echten HTTP-Server im Testprozess, nicht gegen
einen gemockten Client: die Zeit bis zum ersten Token ist eine Aussage ueber
den Netzweg, und die kann man nicht mocken, ohne genau das wegzulassen, was
gemessen werden soll.
"""
from __future__ import annotations

import http.server
import json
import threading
import time

import pytest

from aimon import canary
from aimon.diagnostics import rules


class FakeLLM(http.server.BaseHTTPRequestHandler):
    """Ein OpenAI-kompatibles Backend, dessen Verhalten der Test vorgibt."""

    modus = "ok"
    verzoegerung_s = 0.0

    def do_POST(self):
        laenge = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(laenge)

        if self.modus == "http500":
            body = b'{"error":"upstream ist weg"}'
            self.send_response(500)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        if self.modus == "leer":
            # HTTP 200, Stream endet ohne Inhalt. Genau der Fall, den ein
            # Port-Check fuer gesund haelt.
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        time.sleep(self.verzoegerung_s)
        for stueck in ("o", "k"):
            self.wfile.write(
                b"data: " + json.dumps(
                    {"choices": [{"delta": {"content": stueck}}]}
                ).encode() + b"\n\n")
            self.wfile.flush()
        self.wfile.write(
            b"data: " + json.dumps(
                {"choices": [], "usage": {"completion_tokens": 2}}
            ).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *a):
        pass


@pytest.fixture
def backend():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeLLM.modus = "ok"
    FakeLLM.verzoegerung_s = 0.0
    yield srv
    srv.shutdown()


def ziel(srv, **kw) -> canary.Target:
    port = srv.server_address[1]
    return canary.Target(
        name=kw.pop("name", "test"),
        url=f"http://127.0.0.1:{port}/v1/chat/completions",
        model="testmodell", **kw)


# ----------------------------------------------------------------- Konfiguration

def test_ohne_konfiguration_keine_ziele():
    assert canary.parse_targets(None) == []
    assert canary.parse_targets("  ") == []


def test_kaputtes_json_schaltet_nur_den_kanarienvogel_ab(caplog):
    """Ein Tippfehler hier darf nicht das ganze Monitoring verhindern."""
    assert canary.parse_targets("{das ist kein json") == []


def test_ziel_ohne_modell_wird_uebersprungen():
    roh = json.dumps([
        {"name": "a", "url": "http://x/v1/chat/completions"},
        {"name": "b", "url": "http://y/v1/chat/completions", "model": "m"},
    ])
    namen = [t.name for t in canary.parse_targets(roh)]
    assert namen == ["b"]


def test_doppelte_namen_werden_abgewiesen():
    roh = json.dumps([
        {"name": "a", "url": "http://x", "model": "m"},
        {"name": "a", "url": "http://y", "model": "m"},
    ])
    assert len(canary.parse_targets(roh)) == 1


def test_einzelnes_objekt_ist_auch_erlaubt():
    roh = json.dumps({"name": "a", "url": "http://x", "model": "m"})
    assert [t.name for t in canary.parse_targets(roh)] == ["a"]


# ------------------------------------------------------------------- Der Check

def test_erfolgreicher_request_misst_ttft(backend):
    FakeLLM.verzoegerung_s = 0.15
    r = canary._probe_blocking(ziel(backend))
    assert r.ok
    assert r.http_status == 200
    assert r.ttft_ms >= 140          # die kuenstliche Verzoegerung
    assert r.total_ms >= r.ttft_ms
    assert r.completion_tokens == 2


def test_fehler_vom_backend_wird_mitsamt_text_gemeldet(backend):
    FakeLLM.modus = "http500"
    r = canary._probe_blocking(ziel(backend))
    assert not r.ok
    assert r.http_status == 500
    assert "upstream ist weg" in r.error


def test_zweihundert_ohne_inhalt_ist_ein_fehler(backend):
    """Der Fall, den ein Port- oder Health-Check nicht sieht."""
    FakeLLM.modus = "leer"
    r = canary._probe_blocking(ziel(backend))
    assert not r.ok
    assert r.http_status == 200
    assert "keinen Text" in r.error


def test_geschlossener_port_ist_kein_absturz():
    t = canary.Target(name="tot", url="http://127.0.0.1:9/v1/chat/completions",
                      model="m", timeout_s=2.0)
    r = canary._probe_blocking(t)
    assert not r.ok
    assert r.error


# --------------------------------------------------------- Weg in die Datenbank

async def test_ergebnis_landet_als_dienst_mit_metriken(db, backend):
    t = ziel(backend, name="litellm")
    r = await canary.check_once(t)
    assert r.ok
    async with db.pool().acquire() as conn:
        kind = await conn.fetchval(
            "SELECT kind FROM services WHERE key = $1", t.service_key)
        metriken = await conn.fetch(
            """
            SELECT s.metric, l.value FROM series s
            JOIN series_last l ON l.series_id = s.id
            JOIN services sv ON sv.id = s.service_id
            WHERE sv.key = $1
            """, t.service_key)
    assert kind == "canary"
    werte = {r["metric"]: r["value"] for r in metriken}
    assert werte["canary.ok"] == 1
    assert werte["canary.ttft_ms"] > 0


async def test_fehlschlag_erzeugt_kritischen_incident(db, backend):
    FakeLLM.modus = "http500"
    t = ziel(backend, name="litellm")
    await canary.check_once(t)
    treffer = [f for f in await rules.evaluate() if f.rule == "canary.failing"]
    assert len(treffer) == 1
    assert treffer[0].severity == "critical"
    assert "upstream ist weg" in treffer[0].context["letzter_fehler"]


async def test_gesunder_kanarienvogel_meldet_nichts(db, backend):
    await canary.check_once(ziel(backend))
    regeln = {f.rule for f in await rules.evaluate()}
    assert "canary.failing" not in regeln
    assert "canary.slow" not in regeln
    assert "canary.flaky" not in regeln


async def test_langsame_antwort_wird_zur_warnung(db, backend, monkeypatch):
    from aimon.config import settings
    monkeypatch.setattr(settings, "canary_ttft_warn_ms", 50.0)
    FakeLLM.verzoegerung_s = 0.2
    await canary.check_once(ziel(backend))
    treffer = [f for f in await rules.evaluate() if f.rule == "canary.slow"]
    assert len(treffer) == 1
    assert treffer[0].severity == "warning"


async def test_zwischendurch_fehlschlagend_ist_eine_eigene_meldung(db, backend):
    """Der letzte Versuch klappt, zwei davor nicht.

    Ohne diese Unterscheidung sieht ein Dienst, der jede dritte Anfrage
    verliert, genauso gesund aus wie einer, der laeuft.
    """
    t = ziel(backend, name="wackelig")
    FakeLLM.modus = "http500"
    await canary.check_once(t)
    await canary.check_once(t)
    FakeLLM.modus = "ok"
    await canary.check_once(t)
    await canary.check_once(t)

    f = {x.rule: x for x in await rules.evaluate()}
    assert "canary.failing" not in f
    assert f["canary.flaky"].context["fehlgeschlagen_1h"] == 2
    assert f["canary.flaky"].context["versuche_1h"] == 4
