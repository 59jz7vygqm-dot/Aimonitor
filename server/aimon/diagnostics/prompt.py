"""Erzeugt einen kopierbaren Markdown-Prompt: Aufbau des Stacks + Fehlerbild.

Zweck: ein anderes Modell oder ein anderer Mensch soll ohne Zugriff auf dieses
Dashboard verstehen, wie der Stack aussieht und was gerade schiefgeht.

Grundsätze:
- Nur Zahlen, die wirklich gemessen wurden. Fehlende Daten werden als fehlend
  benannt, nicht geschätzt.
- Mit `redact=true` werden Hostnamen, IPs und URLs durch stabile Platzhalter
  ersetzt, damit der Text ohne weitere Prüfung geteilt werden kann.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from ..config import settings
from ..db import pool

MAX_OK_EDGES = 25
MAX_PROMPT_CHARS = 60_000

SEV_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3, "debug": 4}
SEV_MARK = {
    "critical": "KRITISCH", "error": "FEHLER", "warning": "WARNUNG",
    "info": "INFO", "debug": "DEBUG",
}

# Welche Metriken bei welcher Regel als Beleg mitgeschickt werden.
EVIDENCE: dict[str, list[str]] = {
    "gpu.temp_high": ["gpu.temp_c", "gpu.util_pct", "gpu.power_w", "gpu.fan_pct",
                      "gpu.clock_sm_mhz"],
    "gpu.vram_high": ["gpu.mem_used_bytes", "gpu.mem_total_bytes", "gpu.util_pct"],
    "gpu.throttling": ["gpu.temp_c", "gpu.power_w", "gpu.power_limit_w", "gpu.clock_sm_mhz"],
    "gpu.ecc_errors": ["gpu.ecc_errors_total", "gpu.temp_c"],
    "llm.ttft_high": ["llm.requests_running", "llm.requests_waiting",
                      "llm.kv_cache_usage_pct", "llm.preemptions_total",
                      "llm.prefix_cache_hit_pct"],
    "llm.kv_cache_high": ["llm.kv_cache_usage_pct", "llm.requests_running",
                          "llm.preemptions_total"],
    "llm.queue_backlog": ["llm.requests_waiting", "llm.requests_running",
                          "llm.kv_cache_usage_pct"],
    "llm.preemptions": ["llm.preemptions_total", "llm.kv_cache_usage_pct",
                        "llm.requests_running"],
    "llm.errors": ["llm.requests_running", "llm.kv_cache_usage_pct"],
    "host.disk_full": ["host.disk_used_bytes", "host.disk_total_bytes"],
    "openterminal.headroom_low": ["openterminal.sessions_active",
                                  "openterminal.instances_running",
                                  "openterminal.headroom_pct",
                                  "openterminal.saturation_pct"],
    "openterminal.queue": ["openterminal.sessions_queued",
                           "openterminal.instances_running",
                           "openterminal.instances_desired"],
    "openterminal.scale_lag": ["openterminal.instances_running",
                               "openterminal.instances_desired",
                               "openterminal.instances_starting"],
    "privacy.block_rate_high": ["privacy.requests_total", "privacy.blocked_total",
                                "privacy.block_rate_pct"],
    "privacy.upstream_errors": ["privacy.upstream_errors_total",
                                "privacy.upstream_latency_avg_ms"],
    "comfy.queue_backlog": ["comfy.queue_pending", "comfy.queue_running",
                            "comfy.vram_free_bytes"],
    "comfy.vram_low": ["comfy.vram_free_bytes", "comfy.vram_total_bytes"],
    "probe.failing": ["probe.latency_ms", "probe.up"],
    "node.stale": ["host.cpu_pct", "host.load1"],
    "llm.no_metrics": [],
}

KIND_LABEL = {
    "vllm": "vLLM (OpenAI-kompatibler Inference-Server)",
    "ollama": "Ollama",
    "comfyui": "ComfyUI (Bildgenerierung)",
    "openwebui": "OpenWebUI (Chat-Frontend)",
    "searxng": "SearXNG (Metasuchmaschine)",
    "privacy_proxy": "Privacy-Proxy vor SearXNG (filtert personenbezogene Anfragen)",
    "openterminal_proxy": "Autoscaling-Proxy für OpenTerminal",
    "openterminal_instance": "OpenTerminal-Instanz",
    "tap": "aimon-tap (Messproxy im Inference-Pfad)",
    "other": "unbekannter Dienst",
}

IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
URL_HOST_RE = re.compile(r"(https?://)([^/:\s]+)")


class Redactor:
    """Stabile Pseudonyme, damit derselbe Host im Text immer gleich heißt."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._map: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    _ALIAS_RE = re.compile(r"^[a-z_]+-\d+$")

    def _alias(self, value: str, prefix: str) -> str:
        if value in self._map:
            return self._map[value]
        # Schon ersetzte Werte nicht noch einmal ersetzen: sonst wird aus
        # `ip-1` ein `host-1` und die Legende zeigt Alias auf Alias.
        if self._ALIAS_RE.match(value) or value in self._map.values():
            return value
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        alias = f"{prefix}-{self._counters[prefix]}"
        self._map[value] = alias
        return alias

    def node(self, name: str | None) -> str:
        if not name:
            return "unbekannt"
        return self._alias(name, "node") if self.enabled else name

    def service(self, key: str | None) -> str:
        if not key:
            return "unbekannt"
        if not self.enabled:
            return key
        kind = key.split("@", 1)[0] if "@" in key else "svc"
        return self._alias(key, kind)

    def text(self, s: str | None) -> str:
        if s is None:
            return ""
        if not self.enabled:
            return s
        out = s
        for original, alias in sorted(self._map.items(), key=lambda kv: -len(kv[0])):
            out = out.replace(original, alias)
        out = IP_RE.sub(lambda m: self._alias(m.group(0), "ip"), out)
        out = URL_HOST_RE.sub(lambda m: m.group(1) + self._alias(m.group(2), "host"), out)
        return out

    def legend(self) -> list[tuple[str, str]]:
        return sorted(((a, o) for o, a in self._map.items()), key=lambda kv: kv[0])


def _fmt(v: Any, unit: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "ja" if v else "nein"
    if isinstance(v, (int, float)):
        if unit == "bytes":
            return _bytes(float(v))
        if isinstance(v, float):
            if abs(v) >= 1000:
                return f"{v:,.0f}".replace(",", " ")
            if abs(v) >= 10:
                return f"{v:.1f}"
            return f"{v:.3g}"
        return str(v)
    return str(v)


def _bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1024
    return f"{n:.1f} TiB"


def _is_bytes_metric(metric: str) -> bool:
    return metric.endswith("_bytes")


async def _collect(
    conn, window_s: int, scope: str | None, subject: str | None
) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(seconds=window_s)

    nodes = await conn.fetch(
        """
        SELECT n.name, n.hostname, n.agent_version, n.labels, n.last_seen,
               extract(epoch FROM (now()-n.last_seen))::float8 AS age_s
        FROM nodes n ORDER BY n.name
        """
    )
    services = await conn.fetch(
        """
        SELECT s.key, s.kind, s.name, s.base_url, s.version, s.labels, s.last_seen,
               n.name AS node, extract(epoch FROM (now()-s.last_seen))::float8 AS age_s
        FROM services s LEFT JOIN nodes n ON n.id = s.node_id
        ORDER BY s.kind, s.key
        """
    )
    gpus = await conn.fetch(
        """
        SELECT n.name AS node, s.labels->>'gpu' AS gpu,
               max(s.labels->>'name') AS gpu_name,
               max(l.value) FILTER (WHERE s.metric='gpu.mem_total_bytes') AS vram_total,
               max(l.value) FILTER (WHERE s.metric='gpu.mem_used_bytes')  AS vram_used,
               max(l.value) FILTER (WHERE s.metric='gpu.temp_c')          AS temp_c,
               max(l.value) FILTER (WHERE s.metric='gpu.util_pct')        AS util_pct,
               max(l.value) FILTER (WHERE s.metric='gpu.power_w')         AS power_w,
               max(l.value) FILTER (WHERE s.metric='gpu.power_limit_w')   AS power_limit_w
        FROM series s JOIN series_last l ON l.series_id = s.id
        LEFT JOIN nodes n ON n.id = s.node_id
        WHERE s.metric LIKE 'gpu.%' AND s.labels ? 'gpu'
        GROUP BY n.name, s.labels->>'gpu' ORDER BY n.name, (s.labels->>'gpu')::int
        """
    )

    where = ["ended_at IS NULL"]
    args: list[Any] = []
    if scope == "incident" and subject:
        args.append(int(subject))
        where = [f"id = ${len(args)}"]
    elif scope in ("service", "node") and subject:
        args.append(f"%{subject}%")
        where.append(f"subject LIKE ${len(args)}")
    incidents = await conn.fetch(
        f"SELECT * FROM incidents WHERE {' AND '.join(where)} "
        f"ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1 "
        f"WHEN 'warning' THEN 2 ELSE 3 END, started_at",
        *args,
    )

    resolved = await conn.fetch(
        """
        SELECT rule, subject, title, severity, started_at, ended_at
        FROM incidents WHERE ended_at IS NOT NULL AND ended_at > $1
        ORDER BY ended_at DESC LIMIT 20
        """,
        since,
    )

    events = await conn.fetch(
        """
        SELECT e.ts, e.severity, e.kind, e.title, e.detail, e.count,
               sv.key AS service, n.name AS node
        FROM events e
        LEFT JOIN services sv ON sv.id = e.service_id
        LEFT JOIN nodes n ON n.id = e.node_id
        WHERE e.ts > $1 AND e.severity IN ('warning','error','critical')
        ORDER BY e.ts DESC LIMIT 40
        """,
        since,
    )

    edges = await conn.fetch(
        """
        SELECT COALESCE(n.name,'?') AS src, COALESCE(sv.key, e.dst_target) AS dst,
               e.kind, e.ok, e.latency_ms,
               extract(epoch FROM (now()-e.last_ok_ts))::float8 AS last_ok_age_s
        FROM edges e
        LEFT JOIN nodes n ON n.id = e.src_node_id
        LEFT JOIN services sv ON sv.id = e.dst_svc_id
        WHERE e.last_seen > now() - INTERVAL '15 minutes'
        ORDER BY e.ok, src, dst
        """
    )

    llm = await conn.fetch(
        """
        SELECT sv.key AS service, r.model, count(*) AS n,
               avg(r.ttft_ms)::float8 AS avg_ttft,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY r.ttft_ms)::float8 AS p95_ttft,
               avg(r.prompt_tokens)::float8 AS avg_prompt_tokens,
               avg(r.completion_tokens)::float8 AS avg_completion_tokens,
               count(*) FILTER (WHERE r.status >= 400 OR r.error IS NOT NULL) AS errors
        FROM requests r JOIN services sv ON sv.id = r.service_id
        WHERE r.ts > $1 GROUP BY sv.key, r.model ORDER BY n DESC LIMIT 20
        """,
        since,
    )

    return {
        "now": now, "since": since, "window_s": window_s,
        "nodes": nodes, "services": services, "gpus": gpus,
        "incidents": incidents, "resolved": resolved, "events": events,
        "edges": edges, "llm": llm,
    }


async def _evidence(
    conn, rule: str, subject: str, since: dt.datetime
) -> list[dict]:
    """Zeitreihen-Belege zum Incident: jetzt, Mittel/Max im Fenster."""
    metrics = EVIDENCE.get(rule, [])
    if not metrics:
        return []

    node = gpu = service = None
    if "/" in subject and subject.split("/")[-1].startswith("gpu"):
        node, g = subject.split("/", 1)
        gpu = g.replace("gpu", "")
    elif ":" in subject and "@" not in subject:
        node = subject.split(":", 1)[0]
    elif "@" in subject:
        service = subject
    elif " -> " in subject:
        node = subject.split(" -> ", 1)[0]
    else:
        node = subject

    out = []
    for metric in metrics:
        rows = await conn.fetch(
            """
            SELECT s.metric, s.labels, n.name AS node, sv.key AS service,
                   l.value AS now_value, l.ts,
                   (SELECT avg(sm.value)::float8 FROM samples sm
                      WHERE sm.series_id = s.id AND sm.ts > $2) AS avg_window,
                   (SELECT max(sm.value)::float8 FROM samples sm
                      WHERE sm.series_id = s.id AND sm.ts > $2) AS max_window,
                   (SELECT min(sm.value)::float8 FROM samples sm
                      WHERE sm.series_id = s.id AND sm.ts > $2) AS min_window
            FROM series s JOIN series_last l ON l.series_id = s.id
            LEFT JOIN nodes n ON n.id = s.node_id
            LEFT JOIN services sv ON sv.id = s.service_id
            WHERE s.metric = $1
              AND ($3::text IS NULL OR n.name = $3)
              AND ($4::text IS NULL OR s.labels->>'gpu' = $4)
              AND ($5::text IS NULL OR sv.key = $5)
            LIMIT 8
            """,
            metric, since, node, gpu, service,
        )
        for r in rows:
            out.append(dict(r))
    return out


def _render(data: dict, red: Redactor, user_note: str | None, include_evidence: bool) -> str:
    L: list[str] = []
    now = data["now"]
    win_min = data["window_s"] // 60

    L.append("# Fehlerbericht aus einem selbstgehosteten KI-Stack")
    L.append("")
    L.append(
        "Ich betreibe den unten beschriebenen Stack selbst und habe ein Problem. "
        "Alle Zahlen kommen aus meinem Monitoring (aimonitor), nicht aus Schätzungen. "
        "Wo Daten fehlen, steht das ausdrücklich dabei."
    )
    L.append("")
    L.append(f"- Zeitpunkt des Berichts: `{now.isoformat(timespec='seconds')}`")
    L.append(f"- Betrachtetes Zeitfenster: letzte {win_min} Minuten")
    if red.enabled:
        L.append("- Hostnamen, IPs und URLs sind durch Platzhalter ersetzt.")
    L.append("")

    # ----------------------------------------------------------- 1. Aufbau
    L.append("## 1. Aufbau des Stacks")
    L.append("")
    L.append(f"{len(data['nodes'])} Maschinen, {len(data['services'])} Dienste, "
             f"{len(data['gpus'])} GPUs.")
    L.append("")
    L.append("### Maschinen")
    L.append("")
    L.append("| Maschine | Agent | Status | Labels |")
    L.append("|---|---|---|---|")
    for n in data["nodes"]:
        status = (
            "online" if n["age_s"] <= settings.stale_after_s
            else f"STALE (seit {int(n['age_s'])}s stumm)"
        )
        labels = ", ".join(f"{k}={v}" for k, v in (n["labels"] or {}).items()) or "—"
        L.append(
            f"| `{red.node(n['name'])}` | {n['agent_version'] or '—'} | {status} "
            f"| {red.text(labels)} |"
        )
    L.append("")

    if data["gpus"]:
        L.append("### GPUs")
        L.append("")
        L.append("| Maschine | GPU | Modell | VRAM belegt | Temp | Last | Leistung |")
        L.append("|---|---|---|---|---|---|---|")
        for g in data["gpus"]:
            vram = "—"
            if g["vram_total"]:
                pct = (g["vram_used"] or 0) / g["vram_total"] * 100
                vram = f"{_bytes(g['vram_used'] or 0)} / {_bytes(g['vram_total'])} ({pct:.0f} %)"
            power = "—"
            if g["power_w"] is not None:
                power = f"{g['power_w']:.0f} W"
                if g["power_limit_w"]:
                    power += f" / {g['power_limit_w']:.0f} W"
            L.append(
                f"| `{red.node(g['node'])}` | {g['gpu']} | {g['gpu_name'] or '—'} | {vram} "
                f"| {_fmt(g['temp_c'])} °C | {_fmt(g['util_pct'])} % | {power} |"
            )
        L.append("")

    L.append("### Dienste")
    L.append("")
    L.append("| Dienst | Art | Maschine | Version | Status |")
    L.append("|---|---|---|---|---|")
    for s in data["services"]:
        status = (
            "läuft" if s["age_s"] <= settings.stale_after_s
            else f"keine Daten seit {int(s['age_s'])}s"
        )
        extra = ""
        if s["labels"]:
            interesting = {
                k: v for k, v in s["labels"].items()
                if k in ("model", "tp", "dtype", "quant", "gpu", "role", "max_model_len")
            }
            if interesting:
                extra = " (" + ", ".join(f"{k}={v}" for k, v in interesting.items()) + ")"
        L.append(
            f"| `{red.service(s['key'])}`{extra} | {KIND_LABEL.get(s['kind'], s['kind'])} "
            f"| `{red.node(s['node'])}` | {s['version'] or '—'} | {status} |"
        )
    L.append("")

    if data["edges"]:
        # Kanten zusammenfassen: eine Zeile je (Von, Nach, Art). Fehlerhafte zuerst,
        # funktionierende gedeckelt - 200 Zeilen "ok" helfen beim Debuggen niemandem.
        merged: dict[tuple, dict] = {}
        for e in data["edges"]:
            k = (e["src"], e["dst"], e["kind"])
            cur = merged.get(k)
            if cur is None or (cur["ok"] and not e["ok"]):
                merged[k] = dict(e)
        rows = sorted(merged.values(), key=lambda e: (bool(e["ok"]), e["src"], e["dst"] or ""))
        failing = [e for e in rows if not e["ok"]]
        working = [e for e in rows if e["ok"]]
        shown = failing + working[:MAX_OK_EDGES]

        L.append("### Verbindungen (zuletzt gemessen)")
        L.append("")
        L.append("| Von | Nach | Art | Zustand | Latenz |")
        L.append("|---|---|---|---|---|")
        for e in shown:
            state = "ok" if e["ok"] else "FEHLER"
            if not e["ok"] and e["last_ok_age_s"] is not None:
                state += f" (letzter Erfolg vor {int(e['last_ok_age_s'])}s)"
            elif not e["ok"]:
                state += " (nie erfolgreich)"
            lat = f"{e['latency_ms']:.0f} ms" if e["latency_ms"] is not None else "—"
            dst = e["dst"] or ""
            dst_s = red.service(dst) if "@" in dst else red.text(dst)
            L.append(
                f"| `{red.node(e['src'])}` | `{dst_s}` "
                f"| {e['kind']} | {state} | {lat} |"
            )
        if len(working) > MAX_OK_EDGES:
            L.append(
                f"| … | _{len(working) - MAX_OK_EDGES} weitere funktionierende "
                f"Verbindungen_ | | ok | |"
            )
        L.append("")
        L.append(f"_{len(failing)} von {len(rows)} gemessenen Verbindungen sind gestört._")
        L.append("")

    # --------------------------------------------------------- 2. Inference
    if data["llm"]:
        L.append("## 2. Inference-Kennzahlen im Fenster")
        L.append("")
        L.append("| Dienst | Modell | Requests | TTFT ⌀ | TTFT p95 | Prompt ⌀ | Output ⌀ | Fehler |")
        L.append("|---|---|---|---|---|---|---|---|")
        for r in data["llm"]:
            L.append(
                f"| `{red.service(r['service'])}` | {r['model'] or '—'} | {r['n']} "
                f"| {_fmt(r['avg_ttft'])} ms | {_fmt(r['p95_ttft'])} ms "
                f"| {_fmt(r['avg_prompt_tokens'])} Tok | {_fmt(r['avg_completion_tokens'])} Tok "
                f"| {r['errors']} |"
            )
        L.append("")
        L.append("_Diese Werte sind pro Request gemessen (Messproxy im Datenpfad), "
                 "keine Histogramm-Interpolation._")
        L.append("")
    else:
        L.append("## 2. Inference-Kennzahlen im Fenster")
        L.append("")
        L.append(
            "Keine Request-Daten im Fenster. Das heißt entweder: keine Last, oder der "
            "Messproxy (`aimon-tap`) ist nicht im Datenpfad. TTFT pro Request kann ich "
            "in diesem Zustand nicht belegen."
        )
        L.append("")

    # ---------------------------------------------------------- 3. Problem
    L.append("## 3. Das Problem")
    L.append("")
    if user_note:
        L.append("### Was ich beobachte")
        L.append("")
        for line in user_note.strip().splitlines():
            L.append("> " + line)
        L.append("")

    if not data["incidents"]:
        L.append(
            "Das Monitoring meldet aktuell **keine offenen Incidents**. Wenn ich trotzdem "
            "ein Problem sehe, liegt es außerhalb dessen, was ich messe — genau dazu "
            "brauche ich Hilfe."
        )
        L.append("")
    else:
        L.append(f"{len(data['incidents'])} offene Incidents, nach Schwere sortiert:")
        L.append("")
        for i, inc in enumerate(data["incidents"], 1):
            age = (now - inc["started_at"]).total_seconds()
            L.append(
                f"### 3.{i} [{SEV_MARK.get(inc['severity'], inc['severity'].upper())}] "
                f"{red.text(inc['title'])}"
            )
            L.append("")
            L.append(f"- Regel: `{inc['rule']}`")
            L.append(f"- Betroffen: `{red.text(inc['subject'])}` ({inc['subject_kind']})")
            L.append(f"- Besteht seit: {_duration(age)} "
                     f"(`{inc['started_at'].isoformat(timespec='seconds')}`)")
            ctx = inc["context"] or {}
            if ctx:
                L.append("- Messwerte und Einordnung:")
                for k, v in ctx.items():
                    if isinstance(v, list):
                        L.append(f"  - {k}:")
                        for item in v:
                            L.append(f"    - {red.text(str(item))}")
                    elif isinstance(v, dict):
                        inner = ", ".join(f"{kk}={_fmt(vv)}" for kk, vv in v.items())
                        L.append(f"  - {k}: {red.text(inner)}")
                    else:
                        unit = "bytes" if k.endswith("_bytes") else ""
                        L.append(f"  - {k}: {red.text(_fmt(v, unit))}")
            ev = inc.get("_evidence") or []
            if ev:
                L.append("")
                L.append(f"  Zeitreihen im Fenster (letzte {win_min} min):")
                L.append("")
                L.append("  | Metrik | Labels | jetzt | ⌀ | min | max |")
                L.append("  |---|---|---|---|---|---|")
                for e in ev:
                    unit = "bytes" if _is_bytes_metric(e["metric"]) else ""
                    labels = ", ".join(
                        f"{k}={v}" for k, v in (e["labels"] or {}).items() if k != "le"
                    ) or "—"
                    L.append(
                        f"  | `{e['metric']}` | {red.text(labels)} "
                        f"| {_fmt(e['now_value'], unit)} | {_fmt(e['avg_window'], unit)} "
                        f"| {_fmt(e['min_window'], unit)} | {_fmt(e['max_window'], unit)} |"
                    )
            L.append("")

    if data["events"]:
        L.append("### Ereignisse im Fenster")
        L.append("")
        L.append("| Zeit | Schwere | Art | Dienst | Beschreibung |")
        L.append("|---|---|---|---|---|")
        for e in data["events"][:25]:
            cnt = f" (×{e['count']})" if e["count"] > 1 else ""
            L.append(
                f"| {e['ts'].strftime('%H:%M:%S')} | {e['severity']} | `{e['kind']}` "
                f"| `{red.service(e['service']) if e['service'] else '—'}` "
                f"| {red.text(e['title'])}{cnt} |"
            )
        L.append("")

    if data["resolved"]:
        L.append("### Im Fenster von selbst verschwunden")
        L.append("")
        for r in data["resolved"][:10]:
            dur = (r["ended_at"] - r["started_at"]).total_seconds()
            L.append(
                f"- `{r['rule']}` auf `{red.text(r['subject'])}` — "
                f"{_duration(dur)} lang, endete "
                f"{r['ended_at'].strftime('%H:%M:%S')}"
            )
        L.append("")
        L.append("_Dinge, die kommen und gehen, deuten eher auf Last- oder "
                 "Scheduling-Effekte als auf Defekte._")
        L.append("")

    # ---------------------------------------------------------- 4. Was fehlt
    L.append("## 4. Was ich nicht messe")
    L.append("")
    gaps = _gaps(data)
    if gaps:
        for g in gaps:
            L.append(f"- {g}")
    else:
        L.append("- Nichts Wesentliches bekannt.")
    L.append("")

    # ----------------------------------------------------------- 5. Frage
    L.append("## 5. Meine Frage")
    L.append("")
    L.append(
        "Was ist die wahrscheinlichste Ursache? Bitte antworte in dieser Form:"
    )
    L.append("")
    L.append("1. **Hypothesen**, nach Wahrscheinlichkeit sortiert, jeweils mit der "
             "Begründung aus den Zahlen oben.")
    L.append("2. **Wie ich jede Hypothese prüfe** — konkrete Befehle, Logzeilen oder "
             "Metriken, die sie bestätigen oder ausschließen.")
    L.append("3. **Was ich zuerst ändern soll**, und was das messbar bewirken müsste.")
    L.append("4. **Welche Daten dir fehlen**, damit ich sie nachliefern kann.")
    L.append("")
    L.append("Wenn die Zahlen sich widersprechen, sag das statt dir eine Erklärung "
             "zusammenzureimen.")
    L.append("")

    # Die Platzhalter-Legende gehört NICHT in den Prompt: wer den Block komplett
    # kopiert, würde die Zuordnung mitliefern und die Anonymisierung aufheben.
    # Sie wird getrennt zurückgegeben und in der UI separat angezeigt.
    L.append("---")
    L.append(f"_Erzeugt von aimonitor {settings.version}, "
             f"{now.isoformat(timespec='seconds')}._")
    return "\n".join(L)


def _duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60} s"
    if s < 86400:
        return f"{s // 3600} h {(s % 3600) // 60} min"
    return f"{s // 86400} d {(s % 86400) // 3600} h"


def _gaps(data: dict) -> list[str]:
    """Bekannte Blindstellen benennen, statt sie zu verschweigen."""
    gaps = []
    kinds = {s["kind"] for s in data["services"]}
    keys = {s["key"] for s in data["services"]}

    if not any(s["kind"] == "tap" for s in data["services"]):
        gaps.append(
            "Kein `aimon-tap` im Inference-Pfad: TTFT und Prompt-Längen pro Request "
            "fehlen oder stammen aus vLLM-Histogrammen (interpoliert, nicht nach "
            "Modell aufgelöst)."
        )
    if "ollama" in kinds and not any(s["kind"] == "tap" for s in data["services"]):
        gaps.append(
            "Ollama hat keinen Metrik-Endpunkt. Ohne Tap kenne ich dort nur, welche "
            "Modelle geladen sind und ob ein synthetischer Testrequest durchgeht — "
            "keine echten Benutzerlatenzen."
        )
    if "openwebui" in kinds:
        gaps.append(
            "Von OpenWebUI messe ich nur Erreichbarkeit, nicht was es intern tut "
            "(RAG-Schritte, Websuche, Reranking). Verzögerungen dort erscheinen bei "
            "mir nicht."
        )
    if not any(s["kind"] == "openterminal_proxy" for s in data["services"]):
        gaps.append("Der OpenTerminal-Autoscaler meldet nichts an das Monitoring.")
    if not any(s["kind"] == "privacy_proxy" for s in data["services"]):
        gaps.append("Der SearXNG-Privacy-Proxy meldet nichts an das Monitoring.")
    if not data["gpus"]:
        gaps.append("Keine GPU-Daten vorhanden — nvidia-smi auf den Hosts prüfen.")
    gaps.append(
        "Ich habe keine Applikationslogs im Monitoring, nur Metriken und diskrete "
        "Ereignisse. Logzeilen kann ich auf Anfrage nachliefern."
    )
    return gaps


async def generate(
    window_s: int = 3600,
    scope: str | None = None,
    subject: str | None = None,
    redact: bool = False,
    include_evidence: bool = True,
    user_note: str | None = None,
) -> dict:
    async with pool().acquire() as conn:
        data = await _collect(conn, window_s, scope, subject)
        if include_evidence:
            enriched = []
            for inc in data["incidents"]:
                d = dict(inc)
                d["_evidence"] = await _evidence(
                    conn, inc["rule"], inc["subject"], data["since"]
                )
                enriched.append(d)
            data["incidents"] = enriched

    red = Redactor(redact)
    # Pseudonyme vorab anlegen, damit freie Textfelder sie ersetzen können.
    if redact:
        for n in data["nodes"]:
            red.node(n["name"])
        for s in data["services"]:
            red.service(s["key"])

    text = _render(data, red, user_note, include_evidence)
    legend = [{"alias": a, "original": o} for a, o in red.legend()] if redact else []
    truncated = False
    if len(text) > MAX_PROMPT_CHARS:
        # Lieber hart abschneiden und das sagen, als ein Modell mit 100 KB
        # Tabellen zu füttern, in denen das Wesentliche untergeht.
        cut = text.rfind("\n", 0, MAX_PROMPT_CHARS)
        text = text[: cut if cut > 0 else MAX_PROMPT_CHARS] + (
            "\n\n---\n_Bericht bei "
            f"{MAX_PROMPT_CHARS} Zeichen abgeschnitten. Für einen kürzeren Bericht "
            "`window_s` verkleinern oder mit `scope=incident&subject=<id>` auf einen "
            "Incident einschränken._\n"
        )
        truncated = True
    return {
        "prompt": text,
        "legend": legend,
        "truncated": truncated,
        "chars": len(text),
        "incidents": len(data["incidents"]),
        "window_s": window_s,
        "redacted": redact,
        "generated_at": data["now"],
    }
