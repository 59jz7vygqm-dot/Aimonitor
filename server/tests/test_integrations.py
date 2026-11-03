"""Die Umsetzer der Spezial-Endpunkte: Scaler und Privacy-Proxy."""
from __future__ import annotations

from aimon.integrations import privacy as privacy_map
from aimon.integrations import scaler as scaler_map
from aimon.models import PrivacyIn, ScalerIn


def samples(batch) -> dict:
    """Metrik -> Wert, für Samples ohne Labels."""
    return {s.metric: s.value for s in batch.samples if not s.labels}


def test_headroom_rechnung():
    b = scaler_map.to_batch(ScalerIn.model_validate({
        "node": {"name": "n"},
        "service": {"key": "ot@n:7681", "kind": "openterminal_proxy"},
        "state": {"instances_running": 4, "instances_max": 12,
                  "sessions_active": 27, "sessions_per_instance_target": 10},
    }))
    s = samples(b)
    assert s["openterminal.capacity_sessions"] == 120      # 12 * 10
    assert s["openterminal.headroom_sessions"] == 93       # 120 - 27
    assert s["openterminal.headroom_pct"] == 77.5
    # Sättigung rechnet gegen die LAUFENDEN Instanzen, nicht gegen max -
    # das ist die Zahl, die ein Scale-up auslöst.
    assert s["openterminal.saturation_pct"] == 67.5        # 27 / 40
    assert s["openterminal.headroom_now_sessions"] == 13   # 40 - 27


def test_kein_headroom_ohne_grundzahlen():
    """Ohne instances_max darf keine Headroom-Zahl erfunden werden."""
    b = scaler_map.to_batch(ScalerIn.model_validate({
        "node": {"name": "n"},
        "service": {"key": "ot@n:7681", "kind": "openterminal_proxy"},
        "state": {"instances_running": 4, "sessions_active": 27,
                  "sessions_per_instance_target": 10},
    }))
    s = samples(b)
    assert "openterminal.headroom_pct" not in s
    assert "openterminal.capacity_sessions" not in s
    # Die Sättigung geht trotzdem, sie braucht max nicht.
    assert s["openterminal.saturation_pct"] == 67.5


def test_instanzen_bekommen_labels():
    b = scaler_map.to_batch(ScalerIn.model_validate({
        "node": {"name": "n"},
        "service": {"key": "ot@n:7681", "kind": "openterminal_proxy"},
        "ts": 1793707200,
        "state": {"sessions_per_instance_target": 10},
        "instances": [
            {"id": "ot-1", "state": "running", "sessions": 9,
             "started_at": 1793703600},
            {"id": "ot-2", "state": "starting", "sessions": 0},
        ],
    }))
    per = {(s.metric, s.labels.get("instance")): s.value
           for s in b.samples if s.labels.get("instance")}
    assert per[("openterminal.instance_sessions", "ot-1")] == 9
    assert per[("openterminal.instance_uptime_s", "ot-1")] == 3600
    assert ("openterminal.instance_uptime_s", "ot-2") not in per  # kein started_at


def test_scale_grund_wird_ereignis():
    b = scaler_map.to_batch(ScalerIn.model_validate({
        "node": {"name": "n"},
        "service": {"key": "ot@n:7681", "kind": "openterminal_proxy"},
        "state": {"last_scale_reason": "sessions_per_instance>8 for 90s"},
    }))
    kinds = [e.kind for e in b.events]
    assert "scale.reason" in kinds
    ev = next(e for e in b.events if e.kind == "scale.reason")
    assert ev.dedup_key  # sonst flutet derselbe Grund alle 10 s das Log


def test_privacy_zaehler_und_abgeleitete_werte():
    b = privacy_map.to_batch(PrivacyIn.model_validate({
        "node": {"name": "n"},
        "service": {"key": "pp@n:8888", "kind": "privacy_proxy"},
        "counters": {
            "requests_total": 1000, "blocked_total": 250, "allowed_total": 700,
            "redacted_total": 50,
            "blocked_by_category": {"person_name": 200, "email": 50},
            "upstream_latency_ms_sum": 250000.0, "upstream_latency_ms_count": 1000,
            "query_len_sum": 30000, "query_len_count": 1000,
        },
    }))
    s = samples(b)
    assert s["privacy.block_rate_pct"] == 25.0
    assert s["privacy.upstream_latency_avg_ms"] == 250.0
    assert s["privacy.query_len_avg_chars"] == 30.0

    by_cat = {sm.labels["category"]: sm.value for sm in b.samples
              if sm.metric == "privacy.blocked_total"}
    assert by_cat["person_name"] == 200
    assert by_cat["email"] == 50
    assert by_cat["_all"] == 250


def test_erlaubte_anfragen_fluten_das_log_nicht():
    """Nur Eingriffe landen im Ereignis-Log, nicht jede durchgelassene Suche."""
    b = privacy_map.to_batch(PrivacyIn.model_validate({
        "node": {"name": "n"},
        "service": {"key": "pp@n:8888", "kind": "privacy_proxy"},
        "decisions": [
            {"action": "allowed", "query_len": 10},
            {"action": "allowed", "query_len": 12},
            {"action": "blocked", "category": "person_name",
             "query_redacted": "wohnort von [NAME]"},
            {"action": "redacted", "category": "email",
             "query_redacted": "kontakt [EMAIL]"},
            {"action": "error", "category": "upstream", "upstream_status": 429},
        ],
    }))
    kinds = sorted(e.kind for e in b.events)
    assert kinds == ["privacy.blocked", "privacy.error", "privacy.redacted"]
    # Die erlaubten tauchen trotzdem in einem Zähler auf
    batch_counts = {sm.labels["action"]: sm.value for sm in b.samples
                    if sm.metric == "privacy.decisions_batch"}
    assert batch_counts["allowed"] == 2


def test_severity_je_aktion():
    b = privacy_map.to_batch(PrivacyIn.model_validate({
        "node": {"name": "n"},
        "service": {"key": "pp@n:8888", "kind": "privacy_proxy"},
        "decisions": [
            {"action": "blocked", "category": "person_name"},
            {"action": "redacted", "category": "email"},
            {"action": "error", "category": "upstream"},
        ],
    }))
    sev = {e.kind: e.severity for e in b.events}
    assert sev["privacy.blocked"] == "warning"
    assert sev["privacy.redacted"] == "info"
    assert sev["privacy.error"] == "error"


def test_rohtext_bleibt_im_detail_bis_der_writer_entscheidet():
    """Der Mapper reicht query_raw durch; ob es gespeichert wird, entscheidet
    der Writer anhand der Server-Konfiguration – an einer Stelle, nicht zwei."""
    b = privacy_map.to_batch(PrivacyIn.model_validate({
        "node": {"name": "n"},
        "service": {"key": "pp@n:8888", "kind": "privacy_proxy"},
        "decisions": [{"action": "blocked", "category": "person_name",
                       "query_raw": "wohnort von Max Mustermann",
                       "query_redacted": "wohnort von [NAME]"}],
    }))
    d = b.events[0].detail
    assert d["query_raw"] == "wohnort von Max Mustermann"
    assert d["query_redacted"] == "wohnort von [NAME]"
