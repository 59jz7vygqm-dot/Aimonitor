"""Regelwerk. Erzeugt Incidents, die der Prompt-Generator später erklärt.

Jede Regel liefert Findings mit genug Kontext, dass man sie ohne Rückgriff auf
die DB verstehen kann. Was eine Regel nicht messen kann, behauptet sie nicht.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..db import pool

log = logging.getLogger("aimon.rules")


@dataclass
class Finding:
    rule: str
    severity: str
    subject_kind: str
    subject: str
    title: str
    context: dict[str, Any] = field(default_factory=dict)


def _sev(value: float, warn: float, crit: float) -> str | None:
    if value >= crit:
        return "critical"
    if value >= warn:
        return "warning"
    return None


async def _rule_nodes_stale(conn) -> list[Finding]:
    rows = await conn.fetch(
        """
        SELECT name, last_seen, extract(epoch FROM (now()-last_seen))::float8 AS age
        FROM nodes WHERE last_seen < now() - ($1::int * INTERVAL '1 second')
        """,
        settings.stale_after_s,
    )
    out = []
    for r in rows:
        sev = "critical" if r["age"] > settings.stale_after_s * 10 else "warning"
        out.append(
            Finding(
                "node.stale", sev, "node", r["name"],
                f"Node {r['name']} meldet seit {int(r['age'])}s nichts mehr",
                {
                    "last_seen": r["last_seen"].isoformat(),
                    "age_s": round(r["age"]),
                    "stale_after_s": settings.stale_after_s,
                    "bedeutung": "Agent gestoppt, Host aus oder Netz zum Server weg. "
                                 "Die zuletzt gezeigten Werte sind alt, nicht niedrig.",
                },
            )
        )
    return out


async def _rule_gpu(conn) -> list[Finding]:
    rows = await conn.fetch(
        """
        SELECT n.name AS node, s.labels->>'gpu' AS gpu, s.labels->>'name' AS gpu_name,
               s.metric, l.value, l.ts
        FROM series s JOIN series_last l ON l.series_id = s.id
        LEFT JOIN nodes n ON n.id = s.node_id
        WHERE s.metric IN ('gpu.temp_c','gpu.mem_used_bytes','gpu.mem_total_bytes',
                           'gpu.throttle_thermal','gpu.throttle_power','gpu.throttle_hw',
                           'gpu.util_pct','gpu.power_w','gpu.power_limit_w',
                           'gpu.ecc_errors_total')
          AND s.labels ? 'gpu'
          AND l.ts > now() - ($1::int * INTERVAL '1 second')
        """,
        settings.stale_after_s * 3,
    )
    per: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["node"], r["gpu"])
        d = per.setdefault(key, {"gpu_name": r["gpu_name"]})
        d[r["metric"]] = r["value"]

    out = []
    for (node, gpu), m in per.items():
        subj = f"{node}/gpu{gpu}"
        temp = m.get("gpu.temp_c")
        if temp is not None:
            sev = _sev(temp, settings.gpu_temp_warn_c, settings.gpu_temp_crit_c)
            if sev:
                out.append(
                    Finding(
                        "gpu.temp_high", sev, "gpu", subj,
                        f"GPU {subj} bei {temp:.0f} °C",
                        {
                            "temp_c": temp,
                            "warn_c": settings.gpu_temp_warn_c,
                            "crit_c": settings.gpu_temp_crit_c,
                            "util_pct": m.get("gpu.util_pct"),
                            "power_w": m.get("gpu.power_w"),
                            "power_limit_w": m.get("gpu.power_limit_w"),
                            "gpu_name": m.get("gpu_name"),
                        },
                    )
                )
        used, total = m.get("gpu.mem_used_bytes"), m.get("gpu.mem_total_bytes")
        if used is not None and total:
            pct = used / total * 100.0
            sev = _sev(pct, settings.vram_warn_pct, settings.vram_crit_pct)
            if sev:
                out.append(
                    Finding(
                        "gpu.vram_high", sev, "gpu", subj,
                        f"VRAM auf {subj} zu {pct:.1f} % belegt",
                        {
                            "used_bytes": used, "total_bytes": total, "pct": round(pct, 1),
                            "free_bytes": total - used,
                            "warn_pct": settings.vram_warn_pct,
                            "hinweis": "vLLM reserviert VRAM per gpu_memory_utilization "
                                       "vorab; hohe Belegung ist dort normal. Kritisch wird "
                                       "es erst, wenn ein weiterer Dienst dieselbe GPU nutzt.",
                        },
                    )
                )
        for metric, label in (
            ("gpu.throttle_thermal", "thermisch"),
            ("gpu.throttle_power", "Power-Limit"),
            ("gpu.throttle_hw", "Hardware"),
        ):
            if (m.get(metric) or 0) > 0:
                out.append(
                    Finding(
                        "gpu.throttling", "warning", "gpu", subj,
                        f"GPU {subj} throttelt ({label})",
                        {
                            "reason": label, "temp_c": m.get("gpu.temp_c"),
                            "power_w": m.get("gpu.power_w"),
                            "power_limit_w": m.get("gpu.power_limit_w"),
                            "clock_effect": "Takt wird reduziert; TTFT und Durchsatz sinken "
                                            "ohne dass eine Fehlermeldung auftaucht.",
                        },
                    )
                )
        if (m.get("gpu.ecc_errors_total") or 0) > 0:
            out.append(
                Finding(
                    "gpu.ecc_errors", "critical", "gpu", subj,
                    f"GPU {subj} meldet unkorrigierte ECC-Fehler",
                    {"count": m.get("gpu.ecc_errors_total"),
                     "bedeutung": "Hardware-Defekt oder instabiler Speichertakt. "
                                  "Ergebnisse können still falsch sein."},
                )
            )
    return out


async def _rule_disk(conn) -> list[Finding]:
    rows = await conn.fetch(
        """
        SELECT n.name AS node, s.labels->>'mount' AS mount, s.metric, l.value
        FROM series s JOIN series_last l ON l.series_id = s.id
        LEFT JOIN nodes n ON n.id = s.node_id
        WHERE s.metric IN ('host.disk_used_bytes','host.disk_total_bytes')
          AND l.ts > now() - INTERVAL '10 minutes'
        """
    )
    per: dict[tuple[str, str], dict] = {}
    for r in rows:
        per.setdefault((r["node"], r["mount"]), {})[r["metric"]] = r["value"]
    out = []
    for (node, mount), m in per.items():
        used, total = m.get("host.disk_used_bytes"), m.get("host.disk_total_bytes")
        if used is None or not total:
            continue
        pct = used / total * 100.0
        sev = _sev(pct, settings.disk_warn_pct, 96.0)
        if sev:
            out.append(
                Finding(
                    "host.disk_full", sev, "node", f"{node}:{mount}",
                    f"Platte {mount} auf {node} zu {pct:.1f} % voll",
                    {"used_bytes": used, "total_bytes": total,
                     "free_bytes": total - used, "pct": round(pct, 1),
                     "hinweis": "Modellgewichte und ComfyUI-Outputs sind die üblichen "
                                "Kandidaten."},
                )
            )
    return out


async def _rule_llm(conn) -> list[Finding]:
    out: list[Finding] = []
    now = dt.datetime.now(dt.timezone.utc)
    w = now - dt.timedelta(minutes=15)

    rows = await conn.fetch(
        """
        SELECT sv.key AS service, count(*) AS n,
               avg(r.ttft_ms)::float8 AS avg_ttft,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY r.ttft_ms)::float8 AS p95_ttft,
               avg(r.prompt_tokens)::float8 AS avg_prompt,
               count(*) FILTER (WHERE r.status >= 500 OR r.error IS NOT NULL) AS err5xx,
               count(*) FILTER (WHERE r.status >= 400 AND r.status < 500) AS err4xx
        FROM requests r JOIN services sv ON sv.id = r.service_id
        WHERE r.ts > $1 GROUP BY sv.key
        """,
        w,
    )
    for r in rows:
        if r["p95_ttft"] is not None:
            sev = _sev(r["p95_ttft"], settings.ttft_warn_ms, settings.ttft_crit_ms)
            if sev:
                out.append(
                    Finding(
                        "llm.ttft_high", sev, "service", r["service"],
                        f"TTFT p95 auf {r['service']} bei {r['p95_ttft']:.0f} ms",
                        {
                            "p95_ttft_ms": round(r["p95_ttft"]),
                            "avg_ttft_ms": round(r["avg_ttft"] or 0),
                            "requests_15m": r["n"],
                            "avg_prompt_tokens": round(r["avg_prompt"] or 0),
                            "warn_ms": settings.ttft_warn_ms,
                            "quelle": "requests (aimon-tap, exakt)",
                            "mögliche_ursachen": [
                                "Queue: mehr parallele Requests als max_num_seqs",
                                "lange Prompts ohne Prefix-Cache-Treffer",
                                "KV-Cache voll -> Preemption",
                                "GPU throttelt",
                                "Modell war ausgelagert und musste erst geladen werden",
                            ],
                        },
                    )
                )
        if r["n"] and r["err5xx"]:
            rate = r["err5xx"] / r["n"] * 100.0
            if rate >= 2.0:
                out.append(
                    Finding(
                        "llm.errors", "error" if rate < 20 else "critical",
                        "service", r["service"],
                        f"{rate:.1f} % 5xx/Fehler auf {r['service']} (15 min)",
                        {"errors": r["err5xx"], "requests": r["n"],
                         "error_rate_pct": round(rate, 1), "err4xx": r["err4xx"]},
                    )
                )

    kv = await conn.fetch(
        """
        SELECT sv.key AS service, l.value FROM series s
        JOIN series_last l ON l.series_id = s.id
        JOIN services sv ON sv.id = s.service_id
        WHERE s.metric = 'llm.kv_cache_usage_pct' AND l.ts > now() - INTERVAL '5 minutes'
        """
    )
    for r in kv:
        if r["value"] >= settings.kv_cache_warn_pct:
            out.append(
                Finding(
                    "llm.kv_cache_high", "warning", "service", r["service"],
                    f"KV-Cache auf {r['service']} zu {r['value']:.0f} % belegt",
                    {"kv_cache_usage_pct": round(r["value"], 1),
                     "warn_pct": settings.kv_cache_warn_pct,
                     "folge": "vLLM fängt an zu preempten; TTFT steigt sprunghaft, "
                              "ohne dass die GPU-Auslastung auffällig wird."},
                )
            )

    waiting = await conn.fetch(
        """
        SELECT sv.key AS service, avg(sm.value)::float8 AS avg_wait,
               max(sm.value)::float8 AS max_wait
        FROM samples sm JOIN series s ON s.id = sm.series_id
        JOIN services sv ON sv.id = s.service_id
        WHERE s.metric = 'llm.requests_waiting' AND sm.ts > now() - INTERVAL '10 minutes'
        GROUP BY sv.key HAVING avg(sm.value) >= 1
        """
    )
    for r in waiting:
        out.append(
            Finding(
                "llm.queue_backlog", "warning", "service", r["service"],
                f"{r['service']} hat dauerhaft {r['avg_wait']:.1f} Requests in der Warteschlange",
                {"avg_waiting_10m": round(r["avg_wait"], 1),
                 "max_waiting_10m": round(r["max_wait"], 1),
                 "folge": "Wartezeit geht voll in die TTFT ein.",
                 "stellhebel": "max_num_seqs, max_num_batched_tokens, "
                               "mehr Replicas oder kleineres Modell"},
            )
        )

    pre = await conn.fetch(
        """
        SELECT sv.key AS service,
               (max(sm.value) - min(sm.value))::float8 AS delta
        FROM samples sm JOIN series s ON s.id = sm.series_id
        JOIN services sv ON sv.id = s.service_id
        WHERE s.metric = 'llm.preemptions_total' AND sm.ts > now() - INTERVAL '15 minutes'
        GROUP BY sv.key HAVING (max(sm.value) - min(sm.value)) > 0
        """
    )
    for r in pre:
        out.append(
            Finding(
                "llm.preemptions", "warning", "service", r["service"],
                f"{int(r['delta'])} Preemptions auf {r['service']} in 15 min",
                {"preemptions_15m": int(r["delta"]),
                 "bedeutung": "vLLM musste laufende Sequenzen verdrängen, weil der "
                              "KV-Cache nicht reichte. Direkte Ursache für TTFT-Spitzen."},
            )
        )
    return out


async def _rule_probes(conn) -> list[Finding]:
    rows = await conn.fetch(
        """
        SELECT COALESCE(n.name,'?') AS src, COALESCE(sv.key, pr.target) AS dst,
               pr.target AS raw_target,
               count(*) AS n, count(*) FILTER (WHERE pr.ok) AS ok,
               (array_agg(pr.error ORDER BY pr.ts DESC) FILTER (WHERE pr.error IS NOT NULL))[1]
                   AS last_error,
               (array_agg(pr.http_status ORDER BY pr.ts DESC)
                   FILTER (WHERE pr.http_status IS NOT NULL))[1] AS last_status,
               max(pr.ts) AS last_ts
        FROM probes pr
        LEFT JOIN nodes n ON n.id = pr.from_node_id
        LEFT JOIN services sv ON sv.id = pr.target_svc_id
        WHERE pr.ts > now() - INTERVAL '10 minutes'
        GROUP BY src, dst, pr.target
        HAVING count(*) FILTER (WHERE pr.ok) < count(*)
        """
    )
    out = []
    for r in rows:
        fails = r["n"] - r["ok"]
        down = r["ok"] == 0
        out.append(
            Finding(
                "probe.failing", "critical" if down else "warning",
                "service", f"{r['src']} -> {r['dst']}",
                (f"{r['dst']} von {r['src']} nicht erreichbar"
                 if down else
                 f"{r['dst']} von {r['src']} unzuverlässig ({fails}/{r['n']} Checks fehlgeschlagen)"),
                {
                    "source_node": r["src"], "target": r["dst"],
                    "target_url": r["raw_target"],
                    "checks_10m": r["n"], "failed": fails,
                    "last_error": r["last_error"], "last_http_status": r["last_status"],
                    "last_check": r["last_ts"].isoformat() if r["last_ts"] else None,
                },
            )
        )
    return out


async def _rule_openterminal(conn) -> list[Finding]:
    rows = await conn.fetch(
        """
        SELECT sv.key AS service, s.metric, l.value, l.ts
        FROM series s JOIN series_last l ON l.series_id = s.id
        JOIN services sv ON sv.id = s.service_id
        WHERE s.metric LIKE 'openterminal.%' AND s.labels = '{}'::jsonb
          AND l.ts > now() - INTERVAL '5 minutes'
        """
    )
    per: dict[str, dict] = {}
    for r in rows:
        per.setdefault(r["service"], {})[r["metric"]] = r["value"]

    out = []
    for svc, m in per.items():
        hp = m.get("openterminal.headroom_pct")
        if hp is not None:
            sev = (
                "critical" if hp <= settings.headroom_crit_pct
                else "warning" if hp <= settings.headroom_warn_pct
                else None
            )
            if sev:
                out.append(
                    Finding(
                        "openterminal.headroom_low", sev, "service", svc,
                        f"OpenTerminal-Headroom nur noch {hp:.0f} %",
                        {
                            "headroom_pct": round(hp, 1),
                            "headroom_sessions": m.get("openterminal.headroom_sessions"),
                            "sessions_active": m.get("openterminal.sessions_active"),
                            "capacity_sessions": m.get("openterminal.capacity_sessions"),
                            "instances_running": m.get("openterminal.instances_running"),
                            "instances_max": m.get("openterminal.instances_max"),
                            "bedeutung": "Headroom rechnet gegen instances_max. Bei 0 % "
                                         "kann der Autoscaler nicht mehr hochskalieren, "
                                         "neue Sessions landen in der Queue.",
                        },
                    )
                )
        q = m.get("openterminal.sessions_queued")
        if q and q > 0:
            out.append(
                Finding(
                    "openterminal.queue", "warning", "service", svc,
                    f"{int(q)} OpenTerminal-Sessions warten",
                    {"sessions_queued": int(q),
                     "instances_running": m.get("openterminal.instances_running"),
                     "instances_desired": m.get("openterminal.instances_desired"),
                     "instances_max": m.get("openterminal.instances_max"),
                     "cooldown_s": m.get("openterminal.scale_cooldown_s")},
                )
            )
        run = m.get("openterminal.instances_running")
        des = m.get("openterminal.instances_desired")
        if run is not None and des is not None and abs(run - des) >= 1:
            out.append(
                Finding(
                    "openterminal.scale_lag", "info", "service", svc,
                    f"Soll {int(des)} Instanzen, laufen {int(run)}",
                    {"running": int(run), "desired": int(des),
                     "starting": m.get("openterminal.instances_starting"),
                     "draining": m.get("openterminal.instances_draining"),
                     "hinweis": "Normal während eines Scale-Vorgangs. Dauerhaft heißt: "
                                "Instanzen starten nicht oder crashen beim Start."},
                )
            )
    return out


async def _rule_privacy(conn) -> list[Finding]:
    rows = await conn.fetch(
        """
        SELECT sv.key AS service, s.metric, s.labels, l.value
        FROM series s JOIN series_last l ON l.series_id = s.id
        JOIN services sv ON sv.id = s.service_id
        WHERE s.metric LIKE 'privacy.%' AND l.ts > now() - INTERVAL '10 minutes'
        """
    )
    per: dict[str, dict] = {}
    for r in rows:
        key = r["metric"]
        if r["labels"]:
            cat = r["labels"].get("category")
            if cat and cat != "_all":
                key = f"{r['metric']}#{cat}"
        per.setdefault(r["service"], {})[key] = r["value"]

    out = []
    for svc, m in per.items():
        br = m.get("privacy.block_rate_pct")
        if br is not None and br >= settings.block_rate_warn_pct:
            cats = {
                k.split("#", 1)[1]: v
                for k, v in m.items() if k.startswith("privacy.blocked_total#")
            }
            out.append(
                Finding(
                    "privacy.block_rate_high", "warning", "service", svc,
                    f"Privacy-Proxy blockt {br:.0f} % der Suchanfragen",
                    {
                        "block_rate_pct": round(br, 1),
                        "warn_pct": settings.block_rate_warn_pct,
                        "blocked_by_category": cats,
                        "requests_total": m.get("privacy.requests_total"),
                        "deutung": "Entweder ein Filter ist zu scharf (False Positives) "
                                   "oder jemand sucht tatsächlich systematisch nach "
                                   "Personen. Die Kategorie-Verteilung unterscheidet das: "
                                   "ein einzelnes Regel-Label mit fast allen Treffern "
                                   "spricht für False Positives.",
                    },
                )
            )
        ue = m.get("privacy.upstream_errors_total")
        if ue and ue > 0:
            prev = await conn.fetchval(
                """
                SELECT (max(sm.value) - min(sm.value))::float8 FROM samples sm
                JOIN series s ON s.id = sm.series_id
                JOIN services sv ON sv.id = s.service_id
                WHERE s.metric = 'privacy.upstream_errors_total' AND sv.key = $1
                  AND sm.ts > now() - INTERVAL '15 minutes'
                """,
                svc,
            )
            if prev and prev > 0:
                out.append(
                    Finding(
                        "privacy.upstream_errors", "error", "service", svc,
                        f"{int(prev)} Upstream-Fehler zu SearXNG in 15 min",
                        {"errors_15m": int(prev),
                         "hinweis": "SearXNG selbst prüfen: Rate-Limits der Engines, "
                                    "Captcha-Wall oder SearXNG down."},
                    )
                )
    return out


async def _rule_comfy(conn) -> list[Finding]:
    rows = await conn.fetch(
        """
        SELECT sv.key AS service, s.metric, l.value
        FROM series s JOIN series_last l ON l.series_id = s.id
        JOIN services sv ON sv.id = s.service_id
        WHERE s.metric LIKE 'comfy.%' AND l.ts > now() - INTERVAL '5 minutes'
        """
    )
    per: dict[str, dict] = {}
    for r in rows:
        per.setdefault(r["service"], {})[r["metric"]] = r["value"]
    out = []
    for svc, m in per.items():
        pend = m.get("comfy.queue_pending")
        if pend is not None and pend >= 10:
            out.append(
                Finding(
                    "comfy.queue_backlog", "warning", "service", svc,
                    f"ComfyUI-Queue bei {int(pend)} wartenden Jobs",
                    {"queue_pending": int(pend),
                     "queue_running": m.get("comfy.queue_running"),
                     "vram_free_bytes": m.get("comfy.vram_free_bytes")},
                )
            )
        free = m.get("comfy.vram_free_bytes")
        total = m.get("comfy.vram_total_bytes")
        if free is not None and total and total > 0 and free / total < 0.05:
            out.append(
                Finding(
                    "comfy.vram_low", "warning", "service", svc,
                    f"ComfyUI hat nur noch {free / 1e9:.1f} GB VRAM frei",
                    {"vram_free_bytes": free, "vram_total_bytes": total,
                     "folge": "Nächster großer Workflow läuft in CUDA-OOM. Teilt sich "
                              "die GPU mit vLLM? Dann kämpfen beide um denselben Speicher."},
                )
            )
    return out


async def _rule_no_llm_data(conn) -> list[Finding]:
    """Ein vLLM/Ollama-Dienst ist bekannt, liefert aber keine Inference-Metriken."""
    rows = await conn.fetch(
        """
        SELECT s.key, s.kind FROM services s
        WHERE s.kind IN ('vllm','ollama')
          AND s.last_seen > now() - INTERVAL '10 minutes'
          AND NOT EXISTS (
              SELECT 1 FROM requests r WHERE r.service_id = s.id
                AND r.ts > now() - INTERVAL '1 hour')
          AND NOT EXISTS (
              SELECT 1 FROM series sr JOIN series_last l ON l.series_id = sr.id
              WHERE sr.service_id = s.id AND sr.metric LIKE 'llm.%'
                AND l.ts > now() - INTERVAL '10 minutes')
        """
    )
    return [
        Finding(
            "llm.no_metrics", "info", "service", r["key"],
            f"{r['key']} läuft, liefert aber keine Inference-Metriken",
            {
                "kind": r["kind"],
                "bedeutung": "Der Dienst antwortet auf Health-Checks, aber es kommen "
                             "weder TTFT-Daten noch vLLM-Zähler an.",
                "zu_prüfen": [
                    "vLLM: ist /metrics erreichbar und nicht durch --disable-log-stats aus?",
                    "Ollama: hat keinen Metrik-Endpunkt. TTFT gibt es nur über aimon-tap.",
                    "Läuft überhaupt Last auf dem Dienst?",
                ],
            },
        )
        for r in rows
    ]


RULES = (
    _rule_nodes_stale,
    _rule_gpu,
    _rule_disk,
    _rule_llm,
    _rule_probes,
    _rule_openterminal,
    _rule_privacy,
    _rule_comfy,
    _rule_no_llm_data,
)


async def evaluate() -> list[Finding]:
    findings: list[Finding] = []
    async with pool().acquire() as conn:
        for rule in RULES:
            try:
                findings += await rule(conn)
            except Exception:
                log.exception("Regel %s fehlgeschlagen", rule.__name__)
    return findings


async def reconcile(findings: list[Finding]) -> dict[str, int]:
    """Findings mit der Incident-Tabelle abgleichen: öffnen, verlängern, schließen."""
    now = dt.datetime.now(dt.timezone.utc)
    active = {(f.rule, f.subject) for f in findings}
    opened = closed = updated = 0

    async with pool().acquire() as conn:
        async with conn.transaction():
            for f in findings:
                row = await conn.fetchrow(
                    "SELECT id FROM incidents WHERE rule=$1 AND subject=$2 AND ended_at IS NULL",
                    f.rule, f.subject,
                )
                if row:
                    await conn.execute(
                        "UPDATE incidents SET last_seen_at=$2, severity=$3, title=$4, "
                        "context=$5 WHERE id=$1",
                        row["id"], now, f.severity, f.title, f.context,
                    )
                    updated += 1
                else:
                    await conn.execute(
                        """
                        INSERT INTO incidents (rule, severity, subject_kind, subject, title,
                                               started_at, last_seen_at, context)
                        VALUES ($1,$2,$3,$4,$5,$6,$6,$7)
                        ON CONFLICT DO NOTHING
                        """,
                        f.rule, f.severity, f.subject_kind, f.subject, f.title, now, f.context,
                    )
                    opened += 1

            open_rows = await conn.fetch(
                "SELECT id, rule, subject FROM incidents WHERE ended_at IS NULL"
            )
            stale_ids = [
                r["id"] for r in open_rows if (r["rule"], r["subject"]) not in active
            ]
            if stale_ids:
                await conn.execute(
                    "UPDATE incidents SET ended_at = $2 WHERE id = ANY($1::bigint[])",
                    stale_ids, now,
                )
                closed = len(stale_ids)

    return {"opened": opened, "updated": updated, "closed": closed}


async def rules_loop() -> None:
    await asyncio.sleep(10)
    while True:
        try:
            res = await reconcile(await evaluate())
            if res["opened"] or res["closed"]:
                log.info("Incidents: %s", res)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Regeldurchlauf fehlgeschlagen")
        await asyncio.sleep(settings.rules_interval_s)
