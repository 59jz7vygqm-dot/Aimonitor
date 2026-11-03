#!/usr/bin/env python3
"""Simuliert einen kompletten KI-Stack gegen einen laufenden aimon-server.

Zweck: Dashboard, Regelwerk und Prompt-Generator ohne echte Hardware prüfen.

    python3 tools/simulate.py --url http://localhost:8420 --token <token> \
        --minutes 30 --step 10 --problems
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import time
import urllib.error
import urllib.request

random.seed(42)

GPU_NAMES = ["NVIDIA GeForce RTX 4090", "NVIDIA RTX A6000", "NVIDIA L40S"]
MODELS = ["Qwen3-32B-AWQ", "Llama-3.3-70B-Instruct", "gemma3:27b"]
SEARCH_CATS = ["person_name", "email", "phone", "address", "id_number"]
REDACTED = [
    "wohnort von [NAME]", "telefonnummer [PHONE] rückwärtssuche",
    "kontakt [EMAIL] impressum", "[NAME] arbeitgeber linkedin",
    "adresse [ADDRESS] grundbuch", "[NAME] geburtsdatum",
]


def post(url: str, token: str, path: str, payload: dict) -> dict:
    """Senden mit Backoff bei 429 - der Server ratelimitiert pro Token."""
    data = json.dumps(payload).encode()
    for attempt in range(6):
        req = urllib.request.Request(
            url.rstrip("/") + path, data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = float(e.headers.get("Retry-After") or 2) * (attempt + 1)
                time.sleep(min(wait, 20))
                continue
            body = e.read().decode()[:600]
            raise SystemExit(f"{path} -> HTTP {e.code}: {body}") from e
    raise SystemExit(f"{path}: dauerhaft ratelimitiert. Server-Limit erhöhen "
                     f"(AIMON_RATE_LIMIT_PER_MIN) oder --step vergrößern.")


class Stack:
    """Drei GPU-VMs, ein Proxy-Host, ein Frontend-Host."""

    def __init__(self, problems: bool):
        self.problems = problems
        self.nodes = [
            {"name": "gpu-vm-01", "hostname": "gpu-vm-01.lan", "gpus": 2,
             "labels": {"site": "rack-a", "role": "inference"}},
            {"name": "gpu-vm-02", "hostname": "gpu-vm-02.lan", "gpus": 1,
             "labels": {"site": "rack-a", "role": "inference"}},
            {"name": "gpu-vm-03", "hostname": "gpu-vm-03.lan", "gpus": 1,
             "labels": {"site": "rack-b", "role": "imagegen"}},
            {"name": "proxy-host-01", "hostname": "proxy-01.lan", "gpus": 0,
             "labels": {"site": "rack-b", "role": "edge"}},
        ]
        self.counters: dict[str, float] = {}
        self.ot_instances = 3
        self.ot_sessions = 22

    def c(self, key: str, inc: float) -> float:
        self.counters[key] = self.counters.get(key, 0.0) + inc
        return self.counters[key]

    # ---------------------------------------------------------- GPU-VM-Batch
    def gpu_vm_batch(self, node: dict, t: float, i: int) -> dict:
        samples, services, probes, events = [], [], [], []
        hot = self.problems and node["name"] == "gpu-vm-01"

        for g in range(node["gpus"]):
            # Deterministisch: Pythons hash() ist pro Prozess zufällig, damit
            # bekäme dieselbe GPU bei jedem Lauf einen anderen Namen - und weil
            # der Name Teil der Serien-Identität ist, entstünden Doppel-Serien.
            name_idx = (g + sum(node["name"].encode())) % len(GPU_NAMES)
            lbl = {"gpu": str(g), "uuid": f"GPU-{node['name']}-{g}",
                   "name": GPU_NAMES[name_idx]}
            util = 55 + 35 * math.sin(i / 7 + g) + random.uniform(-8, 8)
            util = max(0, min(100, util))
            base_temp = 86 if (hot and g == 0) else 64
            temp = base_temp + util * 0.12 + random.uniform(-2, 2)
            total = 24 * 1024**3
            used = total * (0.95 if hot and g == 0 else 0.62) + random.uniform(-2e8, 2e8)
            plim = 450.0
            power = min(plim, 120 + util * 3.2 + random.uniform(-15, 15))
            thermal_throttle = 1.0 if temp > 87 else 0.0
            power_throttle = 1.0 if power >= plim - 2 else 0.0

            for metric, val in (
                ("gpu.util_pct", util),
                ("gpu.mem_util_pct", used / total * 100),
                ("gpu.mem_used_bytes", used),
                ("gpu.mem_total_bytes", total),
                ("gpu.temp_c", temp),
                ("gpu.power_w", power),
                ("gpu.power_limit_w", plim),
                ("gpu.fan_pct", min(100, 30 + (temp - 50) * 2)),
                ("gpu.clock_sm_mhz", 2520 - (300 if thermal_throttle else 0)),
                ("gpu.clock_mem_mhz", 10501),
                ("gpu.throttle_thermal", thermal_throttle),
                ("gpu.throttle_power", power_throttle),
                ("gpu.throttle_hw", 0.0),
                ("gpu.pcie_gen", 4),
                ("gpu.pcie_width", 16),
                ("gpu.ecc_errors_total", 0.0),
            ):
                samples.append({"metric": metric, "value": val, "ts": t, "labels": lbl})
            samples.append({"metric": "gpu.count", "value": node["gpus"], "ts": t})

        # Host
        cpu = 20 + 25 * abs(math.sin(i / 11)) + random.uniform(-5, 5)
        memt = 128 * 1024**3
        disk_pct = 0.93 if (self.problems and node["name"] == "gpu-vm-03") else 0.46
        for metric, val, lbl in (
            ("host.cpu_pct", cpu, {}),
            ("host.load1", cpu / 12, {}),
            ("host.load5", cpu / 14, {}),
            ("host.mem_used_bytes", memt * 0.55, {}),
            ("host.mem_total_bytes", memt, {}),
            ("host.mem_available_bytes", memt * 0.42, {}),
            ("host.swap_used_bytes", 0, {}),
            ("host.disk_used_bytes", 2 * 1024**4 * disk_pct, {"mount": "/"}),
            ("host.disk_total_bytes", 2 * 1024**4, {"mount": "/"}),
            ("host.net_rx_bps", 2.4e7 + random.uniform(-5e6, 5e6), {"iface": "eth0"}),
            ("host.net_tx_bps", 1.1e7 + random.uniform(-3e6, 3e6), {"iface": "eth0"}),
            ("host.uptime_s", 864000 + i * 10, {}),
            ("host.cpu_temp_c", 52 + random.uniform(-3, 3), {"sensor": "package"}),
            ("host.procs_running", random.randint(1, 6), {}),
        ):
            samples.append({"metric": metric, "value": val, "ts": t, "labels": lbl})

        # Dienste je Rolle
        if node["name"] in ("gpu-vm-01", "gpu-vm-02"):
            key = f"vllm@{node['name']}:8000"
            model = MODELS[0] if node["name"] == "gpu-vm-01" else MODELS[1]
            services.append({
                "key": key, "kind": "vllm", "name": f"vLLM {model}",
                "base_url": f"http://{node['name']}:8000", "version": "0.8.5",
                "labels": {"model": model, "tp": "2" if node["gpus"] > 1 else "1",
                           "max_model_len": "32768"},
            })
            running = random.randint(0, 9)
            waiting = random.randint(2, 7) if hot else random.randint(0, 1)
            kv = 94 if hot else random.uniform(30, 70)
            for metric, val in (
                ("llm.requests_running", running),
                ("llm.requests_waiting", waiting),
                ("llm.kv_cache_usage_pct", kv),
                ("llm.prefix_cache_hit_pct", random.uniform(40, 85)),
                ("llm.prompt_tokens_total", self.c(f"{key}:pt", random.uniform(400, 3000))),
                ("llm.generation_tokens_total", self.c(f"{key}:gt", random.uniform(80, 600))),
                ("llm.requests_total", self.c(f"{key}:rt", random.uniform(0.5, 4))),
                ("llm.preemptions_total",
                 self.c(f"{key}:pre", 2 if hot else 0)),
            ):
                samples.append({"metric": metric, "value": val, "ts": t, "service": key})

            # TTFT-Histogramm wie vLLM es liefert (kumulativ)
            ttft_base = 3.2 if hot else 0.22
            cum = 0.0
            n_new = random.uniform(1, 5)
            for le in (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, float("inf")):
                frac = 1 / (1 + math.exp(-(math.log10(max(le, 1e-3)) - math.log10(ttft_base)) * 3))
                cum = self.c(f"{key}:ttft:{le}", n_new * frac)
                samples.append({
                    "metric": "llm.ttft_bucket", "value": cum, "ts": t, "service": key,
                    "labels": {"le": ("+Inf" if math.isinf(le) else str(le))},
                })
            samples.append({"metric": "llm.ttft_sum_s",
                            "value": self.c(f"{key}:ttftsum", n_new * ttft_base),
                            "ts": t, "service": key})
            samples.append({"metric": "llm.ttft_count",
                            "value": self.c(f"{key}:ttftcnt", n_new),
                            "ts": t, "service": key})
            samples.append({"metric": "llm.prompt_len_sum",
                            "value": self.c(f"{key}:plsum", n_new * 1840),
                            "ts": t, "service": key})
            samples.append({"metric": "llm.prompt_len_count",
                            "value": self.c(f"{key}:plcnt", n_new),
                            "ts": t, "service": key})

            probes.append({"target": f"http://{node['name']}:8000/health",
                           "target_service": key, "ok": True,
                           "latency_ms": random.uniform(2, 12), "http_status": 200, "ts": t})

        if node["name"] == "gpu-vm-02":
            key = f"ollama@{node['name']}:11434"
            services.append({"key": key, "kind": "ollama", "name": "Ollama",
                             "base_url": f"http://{node['name']}:11434", "version": "0.5.7"})
            for m in ("gemma3:27b", "nomic-embed-text"):
                samples.append({"metric": "ollama.model_loaded", "value": 1, "ts": t,
                                "service": key, "labels": {"model": m}})
                samples.append({"metric": "ollama.model_vram_bytes",
                                "value": 18e9 if "27b" in m else 3e8,
                                "ts": t, "service": key, "labels": {"model": m}})
            samples.append({"metric": "ollama.models_loaded", "value": 2, "ts": t,
                            "service": key})
            samples.append({"metric": "ollama.probe_ttft_ms",
                            "value": random.uniform(90, 400), "ts": t, "service": key,
                            "labels": {"model": "gemma3:27b"}})
            probes.append({"target": f"http://{node['name']}:11434/api/version",
                           "target_service": key, "ok": True,
                           "latency_ms": random.uniform(1, 6), "http_status": 200, "ts": t})

        if node["name"] == "gpu-vm-03":
            key = f"comfyui@{node['name']}:8188"
            services.append({"key": key, "kind": "comfyui", "name": "ComfyUI",
                             "base_url": f"http://{node['name']}:8188", "version": "0.3.26"})
            pend = random.randint(12, 20) if self.problems else random.randint(0, 3)
            for metric, val in (
                ("comfy.queue_pending", pend),
                ("comfy.queue_running", random.randint(0, 1)),
                ("comfy.vram_free_bytes", 0.6e9 if self.problems else 9e9),
                ("comfy.vram_total_bytes", 24 * 1024**3),
                ("comfy.exec_last_ms", random.uniform(4000, 24000)),
                ("comfy.up", 1),
            ):
                samples.append({"metric": metric, "value": val, "ts": t, "service": key})
            probes.append({"target": f"http://{node['name']}:8188/system_stats",
                           "target_service": key, "ok": True,
                           "latency_ms": random.uniform(3, 20), "http_status": 200, "ts": t})
            if pend > 15 and i % 12 == 0:
                events.append({
                    "ts": t, "service": key, "severity": "warning", "kind": "comfy.oom",
                    "title": "Workflow abgebrochen: CUDA out of memory",
                    "detail": {"workflow": "sdxl_upscale_v3", "node": "KSampler",
                               "vram_free_bytes": 6e8},
                    "dedup_key": "comfy.oom:sdxl_upscale_v3",
                })

        # Jeder Node probt das Frontend und den Server-Kollegen
        for target, svc in (
            ("http://proxy-host-01:3000/health", "openwebui@proxy-host-01:3000"),
            ("http://proxy-host-01:8888/healthz", "privacy-proxy@proxy-host-01:8888"),
        ):
            broken = self.problems and node["name"] == "gpu-vm-03" and "8888" in target
            probes.append({
                "target": target, "target_service": svc, "ok": not broken,
                "latency_ms": None if broken else random.uniform(3, 30),
                "http_status": None if broken else 200,
                "error": "dial tcp: i/o timeout" if broken else None, "ts": t,
            })

        return {
            "node": {"name": node["name"], "hostname": node["hostname"],
                     "agent_version": "0.1.0", "labels": node["labels"]},
            "services": services, "samples": samples, "probes": probes, "events": events,
        }

    # ------------------------------------------------------- Frontend-Host
    def proxy_host_batch(self, t: float, i: int) -> dict:
        key_owui = "openwebui@proxy-host-01:3000"
        key_tap = "tap@proxy-host-01:9500"
        key_sx = "searxng@proxy-host-01:8080"
        services = [
            {"key": key_owui, "kind": "openwebui", "name": "OpenWebUI",
             "base_url": "http://proxy-host-01:3000", "version": "0.5.20"},
            {"key": key_tap, "kind": "tap", "name": "aimon-tap",
             "base_url": "http://proxy-host-01:9500", "version": "0.1.0"},
            {"key": key_sx, "kind": "searxng", "name": "SearXNG",
             "base_url": "http://proxy-host-01:8080", "version": "2025.3.1"},
        ]
        samples = [
            {"metric": "openwebui.up", "value": 1, "ts": t, "service": key_owui},
            {"metric": "openwebui.response_ms", "value": random.uniform(20, 120),
             "ts": t, "service": key_owui},
            {"metric": "host.cpu_pct", "value": random.uniform(10, 40), "ts": t},
            {"metric": "host.mem_used_bytes", "value": 12e9, "ts": t},
            {"metric": "host.mem_total_bytes", "value": 32e9, "ts": t},
            {"metric": "host.disk_used_bytes", "value": 3e11, "ts": t,
             "labels": {"mount": "/"}},
            {"metric": "host.disk_total_bytes", "value": 1e12, "ts": t,
             "labels": {"mount": "/"}},
        ]

        # Echte Requests, wie aimon-tap sie liefert
        requests = []
        for _ in range(random.randint(2, 7)):
            svc = random.choice([
                "vllm@gpu-vm-01:8000", "vllm@gpu-vm-02:8000", "ollama@gpu-vm-02:11434",
            ])
            hot = self.problems and svc == "vllm@gpu-vm-01:8000"
            pt = int(random.lognormvariate(7.2, 0.9))
            ct = int(random.lognormvariate(4.8, 0.8))
            ttft = random.lognormvariate(8.2 if hot else 5.3, 0.5)
            fail = random.random() < (0.06 if hot else 0.004)
            requests.append({
                "ts": t - random.uniform(0, 9),
                "service": svc,
                "model": {"vllm@gpu-vm-01:8000": MODELS[0],
                          "vllm@gpu-vm-02:8000": MODELS[1]}.get(svc, MODELS[2]),
                "ttft_ms": None if fail else round(ttft, 1),
                "total_ms": None if fail else round(ttft + ct * random.uniform(12, 30), 1),
                "queue_ms": round(random.uniform(0, 900 if hot else 30), 1),
                "prompt_tokens": pt, "completion_tokens": None if fail else ct,
                "status": 500 if fail else 200, "stream": True, "client": "openwebui",
                "route": "/v1/chat/completions",
                "error": "upstream closed connection" if fail else None,
            })

        probes = []
        for target, svc in (
            ("http://gpu-vm-01:8000/health", "vllm@gpu-vm-01:8000"),
            ("http://gpu-vm-02:8000/health", "vllm@gpu-vm-02:8000"),
            ("http://gpu-vm-02:11434/api/version", "ollama@gpu-vm-02:11434"),
            ("http://gpu-vm-03:8188/system_stats", "comfyui@gpu-vm-03:8188"),
            ("http://proxy-host-01:8080/healthz", key_sx),
        ):
            probes.append({"target": target, "target_service": svc, "ok": True,
                           "latency_ms": random.uniform(2, 25), "http_status": 200,
                           "ts": t, "source_service": key_tap})

        return {
            "node": {"name": "proxy-host-01", "hostname": "proxy-01.lan",
                     "agent_version": "0.1.0", "labels": {"site": "rack-b", "role": "edge"}},
            "services": services, "samples": samples,
            "requests": requests, "probes": probes,
        }

    # ------------------------------------------------------- Autoscaler
    def scaler_payload(self, t: float, i: int) -> dict:
        target_per = 10
        self.ot_sessions = max(
            0, int(self.ot_sessions + random.gauss(0.6 if self.problems else 0, 2.5))
        )
        if self.problems:
            self.ot_sessions = min(118, max(95, self.ot_sessions))
        want = max(1, math.ceil(self.ot_sessions / 8))
        imax = 12
        want = min(want, imax)
        events = []
        if want != self.ot_instances:
            direction = "up" if want > self.ot_instances else "down"
            events.append({
                "ts": t, "severity": "info", "kind": f"scale.{direction}",
                "service": "openterminal-proxy@proxy-host-01:7681",
                "title": f"Scale {direction}: {self.ot_instances} -> {want}",
                "detail": {"from": self.ot_instances, "to": want,
                           "reason": f"sessions_per_instance={self.ot_sessions / max(1, self.ot_instances):.1f}",
                           "trigger_metric": "sessions_per_instance"},
            })
            self.ot_instances = want
        queued = max(0, self.ot_sessions - self.ot_instances * target_per)
        if queued > 0 and self.ot_instances >= imax:
            events.append({
                "ts": t, "severity": "warning", "kind": "scale.blocked",
                "service": "openterminal-proxy@proxy-host-01:7681",
                "title": f"Scale-up blockiert: instances_max={imax} erreicht",
                "detail": {"instances": self.ot_instances, "max": imax,
                           "sessions_queued": queued},
                "dedup_key": "scale.blocked:max",
            })
        return {
            "node": {"name": "proxy-host-01", "agent_version": "0.1.0"},
            "service": {"key": "openterminal-proxy@proxy-host-01:7681",
                        "kind": "openterminal_proxy", "name": "OpenTerminal Autoscaler",
                        "base_url": "http://proxy-host-01:7681", "version": "1.2.0"},
            "ts": t,
            "state": {
                "instances_running": self.ot_instances,
                "instances_desired": self.ot_instances,
                "instances_min": 1, "instances_max": imax,
                "instances_starting": 0, "instances_draining": 0,
                "sessions_active": self.ot_sessions,
                "sessions_per_instance_target": target_per,
                "sessions_queued": queued,
                "cpu_pct": min(99, 20 + self.ot_sessions * 1.4),
                "mem_used_bytes": 2e9 + self.ot_sessions * 2.4e8,
                "mem_total_bytes": 32e9,
                "scale_cooldown_s_remaining": random.choice([0, 0, 0, 30, 45]),
                "last_scale_reason": f"sessions_per_instance>8 for 90s",
            },
            "instances": [
                {"id": f"ot-{j:04x}", "state": "running",
                 "sessions": self.ot_sessions // max(1, self.ot_instances),
                 "started_at": t - 3600 - j * 120,
                 "cpu_pct": random.uniform(20, 70),
                 "mem_used_bytes": random.uniform(1e9, 3e9),
                 "host": "proxy-host-01", "port": 7700 + j}
                for j in range(self.ot_instances)
            ],
            "events": events,
        }

    # ------------------------------------------------------- Privacy-Proxy
    def privacy_payload(self, t: float, i: int) -> dict:
        n_req = random.randint(8, 25)
        n_block = random.randint(4, 10) if self.problems else random.randint(0, 2)
        n_red = random.randint(0, 3)
        n_err = 1 if (self.problems and i % 15 == 0) else 0
        self.c("pv:req", n_req)
        self.c("pv:allow", n_req - n_block - n_red)
        self.c("pv:block", n_block)
        self.c("pv:red", n_red)
        self.c("pv:err", n_err)
        self.c("pv:uerr", n_err)
        self.c("pv:latsum", n_req * random.uniform(120, 400))
        self.c("pv:latcnt", n_req)
        self.c("pv:qlensum", n_req * random.uniform(18, 60))
        self.c("pv:qlencnt", n_req)

        by_cat: dict[str, float] = {}
        decisions = []
        for _ in range(n_block):
            cat = "person_name" if self.problems and random.random() < 0.8 \
                else random.choice(SEARCH_CATS)
            by_cat[cat] = by_cat.get(cat, 0) + 1
            self.c(f"pv:block:{cat}", 1)
            q = random.choice(REDACTED)
            decisions.append({
                "ts": t - random.uniform(0, 9), "action": "blocked", "category": cat,
                "rule": f"ner.{cat}.v2", "confidence": round(random.uniform(0.72, 0.99), 2),
                "query_redacted": q,
                "query_sha256": hashlib.sha256(f"{q}{random.random()}".encode()).hexdigest(),
                "query_len": len(q) + random.randint(0, 12),
                "matched_spans": [{"start": 8, "end": 14, "type": cat.upper()}],
                "client_id": f"u:{random.randint(0x1000, 0xffff):04x}",
            })
        for _ in range(n_red):
            cat = random.choice(["email", "phone"])
            self.c(f"pv:red:{cat}", 1)
            q = random.choice(REDACTED)
            decisions.append({
                "ts": t - random.uniform(0, 9), "action": "redacted", "category": cat,
                "rule": f"regex.{cat}", "query_redacted": q, "query_len": len(q),
                "query_sha256": hashlib.sha256(q.encode()).hexdigest(),
                "upstream_latency_ms": random.uniform(120, 500), "upstream_status": 200,
            })
        if n_err:
            decisions.append({
                "ts": t, "action": "error", "category": "upstream",
                "rule": "upstream.searxng",
                "upstream_status": 429, "upstream_latency_ms": 5000.0,
            })

        return {
            "node": {"name": "proxy-host-01", "agent_version": "0.1.0"},
            "service": {"key": "privacy-proxy@proxy-host-01:8888",
                        "kind": "privacy_proxy", "name": "SearXNG Privacy Proxy",
                        "base_url": "http://proxy-host-01:8888", "version": "0.4.1"},
            "ts": t,
            "counters": {
                "requests_total": self.counters["pv:req"],
                "allowed_total": self.counters["pv:allow"],
                "blocked_total": self.counters["pv:block"],
                "redacted_total": self.counters["pv:red"],
                "errors_total": self.counters["pv:err"],
                "upstream_errors_total": self.counters["pv:uerr"],
                "blocked_by_category": {
                    k.split(":")[-1]: v for k, v in self.counters.items()
                    if k.startswith("pv:block:")
                },
                "redacted_by_category": {
                    k.split(":")[-1]: v for k, v in self.counters.items()
                    if k.startswith("pv:red:")
                },
                "upstream_latency_ms_sum": self.counters["pv:latsum"],
                "upstream_latency_ms_count": self.counters["pv:latcnt"],
                "query_len_sum": self.counters["pv:qlensum"],
                "query_len_count": self.counters["pv:qlencnt"],
            },
            "decisions": decisions,
        }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8420")
    ap.add_argument("--token", required=True)
    ap.add_argument("--minutes", type=float, default=30, help="Simulierte Historie")
    ap.add_argument("--step", type=int, default=10, help="Scrape-Intervall in Sekunden")
    ap.add_argument("--problems", action="store_true",
                    help="Störungen einbauen (heiße GPU, TTFT-Spitzen, Headroom am Ende, "
                         "Filter zu scharf, Platte voll, Dienst unerreichbar)")
    ap.add_argument("--live", action="store_true",
                    help="Nach der Historie in Echtzeit weiterlaufen")
    a = ap.parse_args()

    stack = Stack(a.problems)
    steps = int(a.minutes * 60 / a.step)
    t0 = time.time() - a.minutes * 60
    totals = {"accepted": 0, "rejected": 0}

    for i in range(steps):
        t = t0 + i * a.step
        for node in stack.nodes:
            if node["gpus"] == 0:
                continue
            r = post(a.url, a.token, "/api/v1/ingest/batch", stack.gpu_vm_batch(node, t, i))
            totals["accepted"] += r.get("accepted", 0)
            totals["rejected"] += r.get("rejected", 0)
        r = post(a.url, a.token, "/api/v1/ingest/batch", stack.proxy_host_batch(t, i))
        totals["accepted"] += r.get("accepted", 0)
        if i % 2 == 0:
            post(a.url, a.token, "/api/v1/ingest/scaler", stack.scaler_payload(t, i))
            post(a.url, a.token, "/api/v1/ingest/privacy", stack.privacy_payload(t, i))
        if i % 20 == 0:
            print(f"  {i}/{steps} Schritte, {totals['accepted']} Items", flush=True)

    print(f"Fertig: {totals['accepted']} Items angenommen, "
          f"{totals['rejected']} abgelehnt.")

    if a.live:
        print("Live-Modus (Strg+C beendet) …")
        i = steps
        while True:
            t = time.time()
            for node in stack.nodes:
                if node["gpus"]:
                    post(a.url, a.token, "/api/v1/ingest/batch", stack.gpu_vm_batch(node, t, i))
            post(a.url, a.token, "/api/v1/ingest/batch", stack.proxy_host_batch(t, i))
            post(a.url, a.token, "/api/v1/ingest/scaler", stack.scaler_payload(t, i))
            post(a.url, a.token, "/api/v1/ingest/privacy", stack.privacy_payload(t, i))
            i += 1
            time.sleep(a.step)


if __name__ == "__main__":
    main()
