"""Der Ingest-Contract als ausführbare Prüfung."""
from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from aimon.models import Batch, EventIn, PrivacyIn, SampleIn, ScalerIn, ServiceIn


def test_timestamp_formen():
    """RFC3339, Unix-Sekunden und Millisekunden müssen alle gehen."""
    s = SampleIn(metric="x.y", value=1, ts="2026-11-03T12:00:00Z")
    assert s.ts == dt.datetime(2026, 11, 3, 12, 0, tzinfo=dt.timezone.utc)

    s = SampleIn(metric="x.y", value=1, ts=1793707200)
    assert s.ts.year == 2026

    # Millisekunden werden erkannt, sonst stünde das Sample im Jahr 58000
    s = SampleIn(metric="x.y", value=1, ts=1793707200000)
    assert s.ts.year == 2026

    s = SampleIn(metric="x.y", value=1, ts="2026-11-03T13:00:00+01:00")
    assert s.ts.hour == 12 and s.ts.tzinfo == dt.timezone.utc  # auf UTC normalisiert


def test_nan_und_inf_werden_abgelehnt():
    """Ein NaN würde als Zahl in der DB landen und jede Aggregation vergiften."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            SampleIn(metric="x.y", value=bad)


def test_metrikname_wird_geprueft():
    SampleIn(metric="gpu.temp_c", value=1)
    SampleIn(metric="llm.ttft_bucket", value=1)
    # Kleinschreibung ist gewollte Normalisierung, kein Fehler:
    assert SampleIn(metric="GPU.Temp_C", value=1).metric == "gpu.temp_c"
    for bad in ("gpu temp", "", "9gpu", "gpu-temp", "x" * 70, "gpu/temp"):
        with pytest.raises(ValidationError):
            SampleIn(metric=bad, value=1)


def test_unbekannte_service_art_wird_nicht_abgelehnt():
    """Eine neue Dienstart darf den ganzen Batch nicht scheitern lassen."""
    s = ServiceIn(key="k", kind="irgendwas_neues")
    assert s.kind == "other"


def test_unbekannte_felder_werden_gemeldet_nicht_verworfen():
    b = Batch.model_validate({
        "node": {"name": "n", "zukunftsfeld": 1},
        "samples": [{"metric": "a.b", "value": 1}],
    })
    assert b.node.unknown_fields() == ["zukunftsfeld"]
    assert b.node.name == "n"


def test_labels_werden_zu_strings_und_begrenzt():
    s = SampleIn(metric="a.b", value=1, labels={"gpu": 0, "x": None, "lang": "y" * 500})
    assert s.labels["gpu"] == "0"
    assert "x" not in s.labels          # None fliegt raus
    assert len(s.labels["lang"]) == 200  # gekappt

    many = {f"k{i}": i for i in range(50)}
    assert len(SampleIn(metric="a.b", value=1, labels=many).labels) == 32


def test_negative_latenzen_werden_verworfen():
    from aimon.models import RequestIn

    r = RequestIn(service="s", ttft_ms=-5, total_ms=100)
    assert r.ttft_ms is None
    assert r.total_ms == 100


def test_severity_faellt_auf_info_zurueck():
    assert EventIn(kind="k", title="t", severity="PANIK").severity == "info"
    assert EventIn(kind="k", title="t", severity="critical").severity == "critical"


def test_contract_beispiel_aus_der_doku_validiert():
    """Das Beispiel in docs/integration-contract.md muss durchgehen –
    sonst gibt die Doku etwas vor, das der Server ablehnt."""
    b = Batch.model_validate({
        "node": {"name": "gpu-vm-01", "hostname": "gpu-vm-01.lan",
                 "agent_version": "0.1.0",
                 "labels": {"site": "rack-a", "gpu_model": "RTX 4090"}},
        "services": [{"key": "vllm@gpu-vm-01:8000", "kind": "vllm",
                      "name": "vllm-qwen3-32b", "base_url": "http://10.0.0.11:8000",
                      "version": "0.8.5", "labels": {"model": "Qwen3-32B-AWQ"}}],
        "samples": [{"metric": "gpu.temp_c", "value": 71.0, "ts": 1793707200,
                     "labels": {"gpu": "0", "uuid": "GPU-abc123"}}],
        "requests": [{"ts": 1793707199.412, "service": "vllm@gpu-vm-01:8000",
                      "model": "Qwen3-32B-AWQ", "ttft_ms": 184.3, "total_ms": 4211.0,
                      "prompt_tokens": 1842, "completion_tokens": 311,
                      "status": 200, "stream": True, "client": "openwebui",
                      "route": "/v1/chat/completions", "error": None}],
        "probes": [{"ts": 1793707200, "target": "http://10.0.0.20:3000/health",
                    "target_service": "openwebui@host-02:3000", "ok": True,
                    "latency_ms": 12.4, "http_status": 200}],
        "events": [{"ts": 1793707180, "service": "privacy-proxy@proxy-host-01:8888",
                    "severity": "warning", "kind": "privacy.block",
                    "title": "Anfrage geblockt: Personenname",
                    "detail": {"category": "person_name"},
                    "dedup_key": "privacy.block:ner.person"}],
    })
    assert b.item_count() == 5


def test_scaler_beispiel_validiert():
    p = ScalerIn.model_validate({
        "node": {"name": "proxy-host-01"},
        "service": {"key": "openterminal-proxy@proxy-host-01:7681",
                    "kind": "openterminal_proxy"},
        "ts": 1793707200,
        "state": {"instances_running": 4, "instances_max": 12,
                  "sessions_active": 27, "sessions_per_instance_target": 10},
        "instances": [{"id": "ot-7f3a", "state": "running", "sessions": 9,
                       "started_at": 1793705000}],
    })
    assert p.state.instances_running == 4
    assert p.instances[0].started_at.year == 2026


def test_privacy_beispiel_validiert():
    p = PrivacyIn.model_validate({
        "node": {"name": "proxy-host-01"},
        "service": {"key": "privacy-proxy@proxy-host-01:8888", "kind": "privacy_proxy"},
        "counters": {"requests_total": 18422, "blocked_total": 388,
                     "blocked_by_category": {"person_name": 301}},
        "decisions": [{"ts": 1793707198.8, "action": "blocked",
                       "category": "person_name", "rule": "ner.person.v2",
                       "query_redacted": "wohnort von [NAME]", "query_len": 21}],
    })
    assert p.decisions[0].action == "blocked"

    with pytest.raises(ValidationError):
        PrivacyIn.model_validate({
            "node": {"name": "n"}, "service": {"key": "k", "kind": "privacy_proxy"},
            "decisions": [{"action": "ausgedacht"}],
        })
