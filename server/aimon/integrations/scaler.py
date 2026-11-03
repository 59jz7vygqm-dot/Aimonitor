"""OpenTerminal-Autoscaler -> Batch. Siehe integration-contract.md §3.1."""
from __future__ import annotations

from ..models import Batch, EventIn, SampleIn, ScalerIn


def _s(metric: str, value, service: str, ts, labels: dict | None = None) -> SampleIn | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return SampleIn(metric=metric, value=v, ts=ts, service=service, labels=labels or {})


def to_batch(p: ScalerIn) -> Batch:
    svc = p.service.key
    ts = p.ts
    st = p.state
    out: list[SampleIn | None] = [
        _s("openterminal.instances_running", st.instances_running, svc, ts),
        _s("openterminal.instances_desired", st.instances_desired, svc, ts),
        _s("openterminal.instances_min", st.instances_min, svc, ts),
        _s("openterminal.instances_max", st.instances_max, svc, ts),
        _s("openterminal.instances_starting", st.instances_starting, svc, ts),
        _s("openterminal.instances_draining", st.instances_draining, svc, ts),
        _s("openterminal.sessions_active", st.sessions_active, svc, ts),
        _s("openterminal.sessions_queued", st.sessions_queued, svc, ts),
        _s("openterminal.sessions_per_instance_target", st.sessions_per_instance_target, svc, ts),
        _s("openterminal.cpu_pct", st.cpu_pct, svc, ts),
        _s("openterminal.mem_used_bytes", st.mem_used_bytes, svc, ts),
        _s("openterminal.mem_total_bytes", st.mem_total_bytes, svc, ts),
        _s("openterminal.scale_cooldown_s", st.scale_cooldown_s_remaining, svc, ts),
    ]

    # Headroom. Nur rechnen, wenn beide Grundzahlen da sind - sonst lieber nichts
    # anzeigen als eine erfundene Prozentzahl.
    per = st.sessions_per_instance_target
    act = st.sessions_active
    if per and per > 0:
        if st.instances_max is not None:
            cap = st.instances_max * per
            out.append(_s("openterminal.capacity_sessions", cap, svc, ts))
            if act is not None:
                out.append(_s("openterminal.headroom_sessions", cap - act, svc, ts))
                if cap > 0:
                    out.append(
                        _s("openterminal.headroom_pct", (cap - act) / cap * 100.0, svc, ts)
                    )
        run = st.instances_running
        if run and run > 0 and act is not None:
            cur_cap = run * per
            out.append(
                _s("openterminal.saturation_pct", act / cur_cap * 100.0, svc, ts)
            )
            out.append(_s("openterminal.capacity_now_sessions", cur_cap, svc, ts))
            out.append(_s("openterminal.headroom_now_sessions", cur_cap - act, svc, ts))

    for inst in p.instances:
        lbl = {"instance": inst.id, "state": inst.state or "unknown"}
        out.append(_s("openterminal.instance_sessions", inst.sessions, svc, ts, lbl))
        out.append(_s("openterminal.instance_cpu_pct", inst.cpu_pct, svc, ts, lbl))
        out.append(_s("openterminal.instance_mem_used_bytes", inst.mem_used_bytes, svc, ts, lbl))
        out.append(_s("openterminal.instance_up", 1.0, svc, ts, lbl))
        if inst.started_at and ts:
            out.append(
                _s("openterminal.instance_uptime_s",
                   (ts - inst.started_at).total_seconds(), svc, ts, lbl)
            )

    events = list(p.events)
    if st.last_scale_reason:
        events.append(
            EventIn(
                kind="scale.reason",
                severity="info",
                title=f"Scale-Entscheidung: {st.last_scale_reason}"[:500],
                ts=ts,
                service=svc,
                detail={
                    "reason": st.last_scale_reason,
                    "instances_running": st.instances_running,
                    "instances_desired": st.instances_desired,
                },
                dedup_key=f"scale.reason:{st.last_scale_reason}"[:200],
            )
        )

    return Batch(
        node=p.node,
        services=[p.service],
        samples=[s for s in out if s is not None],
        events=events,
    )
