"""SearXNG-Privacy-Proxy -> Batch. Siehe integration-contract.md §3.2.

Datenschutz: `query_raw` wird nur weitergegeben, wenn der Server es erlaubt
(AIMON_ALLOW_RAW_QUERIES). Der Writer verwirft es sonst und notiert das im Event.
"""
from __future__ import annotations

from ..models import Batch, EventIn, PrivacyIn, SampleIn

SEVERITY_BY_ACTION = {
    "blocked": "warning",
    "redacted": "info",
    "error": "error",
    "allowed": "debug",
}


def _s(metric: str, value, service: str, ts, labels: dict | None = None) -> SampleIn | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return SampleIn(metric=metric, value=v, ts=ts, service=service, labels=labels or {})


def to_batch(p: PrivacyIn) -> Batch:
    svc = p.service.key
    ts = p.ts
    c = p.counters
    out: list[SampleIn | None] = [
        _s("privacy.requests_total", c.requests_total, svc, ts),
        _s("privacy.allowed_total", c.allowed_total, svc, ts),
        _s("privacy.blocked_total", c.blocked_total, svc, ts, {"category": "_all"}),
        _s("privacy.redacted_total", c.redacted_total, svc, ts, {"category": "_all"}),
        _s("privacy.errors_total", c.errors_total, svc, ts),
        _s("privacy.upstream_errors_total", c.upstream_errors_total, svc, ts),
        _s("privacy.upstream_latency_ms_sum", c.upstream_latency_ms_sum, svc, ts),
        _s("privacy.upstream_latency_ms_count", c.upstream_latency_ms_count, svc, ts),
        _s("privacy.query_len_sum", c.query_len_sum, svc, ts),
        _s("privacy.query_len_count", c.query_len_count, svc, ts),
    ]
    for cat, n in (c.blocked_by_category or {}).items():
        out.append(_s("privacy.blocked_total", n, svc, ts, {"category": str(cat)[:64]}))
    for cat, n in (c.redacted_by_category or {}).items():
        out.append(_s("privacy.redacted_total", n, svc, ts, {"category": str(cat)[:64]}))

    if c.requests_total and c.requests_total > 0 and c.blocked_total is not None:
        out.append(
            _s("privacy.block_rate_pct", c.blocked_total / c.requests_total * 100.0, svc, ts)
        )
    if c.upstream_latency_ms_count and c.upstream_latency_ms_count > 0 \
            and c.upstream_latency_ms_sum is not None:
        out.append(
            _s("privacy.upstream_latency_avg_ms",
               c.upstream_latency_ms_sum / c.upstream_latency_ms_count, svc, ts)
        )
    if c.query_len_count and c.query_len_count > 0 and c.query_len_sum is not None:
        out.append(
            _s("privacy.query_len_avg_chars", c.query_len_sum / c.query_len_count, svc, ts)
        )

    events: list[EventIn] = []
    lat_sum = 0.0
    lat_n = 0
    for d in p.decisions:
        if d.upstream_latency_ms is not None:
            lat_sum += d.upstream_latency_ms
            lat_n += 1
        if d.action == "allowed":
            # Einzelne erlaubte Anfragen landen nicht im Event-Log; dafür sind
            # die Counter da. Sonst ist das Log in Minuten unlesbar.
            continue
        cat = d.category or "unspecified"
        title = {
            "blocked": f"Anfrage geblockt: {cat}",
            "redacted": f"Anfrage bereinigt: {cat}",
            "error": f"Filter-Fehler: {cat}",
        }.get(d.action, f"{d.action}: {cat}")
        detail = {
            "action": d.action,
            "category": cat,
            "rule": d.rule,
            "confidence": d.confidence,
            "query_redacted": d.query_redacted,
            "query_sha256": d.query_sha256,
            "query_len": d.query_len,
            "matched_spans": d.matched_spans[:20],
            "client_id": d.client_id,
            "upstream_status": d.upstream_status,
            "upstream_latency_ms": d.upstream_latency_ms,
            "engines": d.engines[:20],
        }
        if d.query_raw is not None:
            detail["query_raw"] = d.query_raw
        events.append(
            EventIn(
                kind=f"privacy.{d.action}",
                severity=SEVERITY_BY_ACTION.get(d.action, "info"),
                title=title,
                ts=d.ts or ts,
                service=svc,
                detail={k: v for k, v in detail.items() if v is not None},
            )
        )

    if lat_n:
        out.append(_s("privacy.upstream_latency_batch_avg_ms", lat_sum / lat_n, svc, ts))

    # Entscheidungen auch als Zähler, damit Raten auch ohne `counters` gehen.
    by_action: dict[str, int] = {}
    for d in p.decisions:
        by_action[d.action] = by_action.get(d.action, 0) + 1
    for action, n in by_action.items():
        out.append(
            _s("privacy.decisions_batch", n, svc, ts, {"action": action})
        )

    return Batch(
        node=p.node,
        services=[p.service],
        samples=[s for s in out if s is not None],
        events=events,
    )
