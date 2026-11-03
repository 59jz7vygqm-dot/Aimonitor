#!/usr/bin/env python3
"""Fake-Backends für vLLM, Ollama und ComfyUI auf localhost.

Damit lässt sich der Agent samt Discovery und der Tap testen, ohne GPUs.

    python3 tools/fake_stack.py            # Ports 8000, 11434, 8188, 3000
    python3 tools/fake_stack.py --slow 2.0 # 2 s Prefill, um TTFT zu sehen
"""
from __future__ import annotations

import argparse
import json
import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

START = time.time()
SLOW = 0.15
COUNTERS = {"prompt": 0.0, "gen": 0.0, "req": 0.0, "pre": 0.0, "ttft_n": 0.0,
            "ttft_sum": 0.0, "pl_sum": 0.0, "pl_n": 0.0}
TTFT_BUCKETS = [0.001, 0.005, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.25,
                0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0]
LOCK = threading.Lock()


def advance() -> None:
    """Zähler so bewegen, wie ein laufender vLLM das täte."""
    with LOCK:
        n = random.uniform(0.5, 3)
        COUNTERS["req"] += n
        COUNTERS["prompt"] += n * random.uniform(800, 2600)
        COUNTERS["gen"] += n * random.uniform(100, 400)
        COUNTERS["ttft_n"] += n
        COUNTERS["ttft_sum"] += n * SLOW
        COUNTERS["pl_n"] += n
        COUNTERS["pl_sum"] += n * random.uniform(900, 2400)
        if random.random() < 0.08:
            COUNTERS["pre"] += 1


def vllm_metrics() -> str:
    advance()
    t = time.time() - START
    running = max(0, int(4 + 3 * math.sin(t / 20)))
    waiting = max(0, int(2 * math.sin(t / 13)))
    kv = 0.3 + 0.35 * abs(math.sin(t / 30))
    m = 'model_name="Qwen3-32B-AWQ"'
    lines = [
        "# HELP vllm:num_requests_running Number of requests currently running.",
        "# TYPE vllm:num_requests_running gauge",
        f"vllm:num_requests_running{{{m}}} {running}.0",
        "# TYPE vllm:num_requests_waiting gauge",
        f"vllm:num_requests_waiting{{{m}}} {waiting}.0",
        "# TYPE vllm:gpu_cache_usage_perc gauge",
        f"vllm:gpu_cache_usage_perc{{{m}}} {kv:.4f}",
        "# TYPE vllm:gpu_prefix_cache_hit_rate gauge",
        f"vllm:gpu_prefix_cache_hit_rate{{{m}}} {random.uniform(0.4, 0.9):.3f}",
        "# TYPE vllm:prompt_tokens_total counter",
        f"vllm:prompt_tokens_total{{{m}}} {COUNTERS['prompt']:.1f}",
        "# TYPE vllm:generation_tokens_total counter",
        f"vllm:generation_tokens_total{{{m}}} {COUNTERS['gen']:.1f}",
        "# TYPE vllm:num_preemptions_total counter",
        f"vllm:num_preemptions_total{{{m}}} {COUNTERS['pre']:.1f}",
        "# TYPE vllm:request_success_total counter",
        f'vllm:request_success_total{{finished_reason="stop",{m}}} {COUNTERS["req"]*0.95:.1f}',
        f'vllm:request_success_total{{finished_reason="length",{m}}} {COUNTERS["req"]*0.05:.1f}',
        "# TYPE vllm:time_to_first_token_seconds histogram",
    ]
    n = COUNTERS["ttft_n"]
    for le in TTFT_BUCKETS:
        frac = 1 / (1 + math.exp(-(math.log10(max(le, 1e-4)) - math.log10(SLOW)) * 4))
        lines.append(
            f'vllm:time_to_first_token_seconds_bucket{{le="{le}",{m}}} {n*frac:.1f}')
    lines += [
        f'vllm:time_to_first_token_seconds_bucket{{le="+Inf",{m}}} {n:.1f}',
        f"vllm:time_to_first_token_seconds_sum{{{m}}} {COUNTERS['ttft_sum']:.3f}",
        f"vllm:time_to_first_token_seconds_count{{{m}}} {n:.1f}",
        "# TYPE vllm:request_prompt_tokens histogram",
    ]
    pn = COUNTERS["pl_n"]
    for le in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000):
        frac = min(1.0, max(0.0, (math.log10(le) - 1.5) / 2.0))
        lines.append(f'vllm:request_prompt_tokens_bucket{{le="{le}",{m}}} {pn*frac:.1f}')
    lines += [
        f'vllm:request_prompt_tokens_bucket{{le="+Inf",{m}}} {pn:.1f}',
        f"vllm:request_prompt_tokens_sum{{{m}}} {COUNTERS['pl_sum']:.1f}",
        f"vllm:request_prompt_tokens_count{{{m}}} {pn:.1f}",
        "",
    ]
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    role = "vllm"

    def log_message(self, *a):  # stumm
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        p = self.path.split("?")[0]
        r = self.role
        if r == "vllm":
            if p == "/metrics":
                return self._send(200, vllm_metrics().encode(), "text/plain; version=0.0.4")
            if p == "/version":
                return self._json({"version": "0.8.5"})
            if p == "/health":
                return self._send(200, b"", "text/plain")
            if p == "/v1/models":
                return self._json({"data": [{"id": "Qwen3-32B-AWQ"}]})
        elif r == "ollama":
            if p == "/api/version":
                return self._json({"version": "0.5.7"})
            if p == "/api/ps":
                return self._json({"models": [{
                    "name": "gemma3:27b", "model": "gemma3:27b",
                    "size": 19_000_000_000, "size_vram": 18_200_000_000,
                    "expires_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 240)),
                    "details": {"parameter_size": "27B", "quantization_level": "Q4_K_M"},
                }]})
            if p == "/api/tags":
                return self._json({"models": [{"name": "gemma3:27b"},
                                              {"name": "nomic-embed-text"}]})
        elif r == "comfyui":
            if p == "/system_stats":
                return self._json({
                    "system": {"comfyui_version": "0.3.26", "python_version": "3.12.1"},
                    "devices": [{"name": "cuda:0 NVIDIA RTX A6000", "type": "cuda",
                                 "vram_total": 51_527_024_640,
                                 "vram_free": random.randint(2, 40) * 10**9,
                                 "torch_vram_total": 40_000_000_000,
                                 "torch_vram_free": random.randint(1, 30) * 10**9}],
                })
            if p == "/queue":
                return self._json({
                    "queue_running": [[0, "abc"]] * random.randint(0, 1),
                    "queue_pending": [[0, "x"]] * random.randint(0, 6),
                })
        elif r == "openwebui":
            if p == "/health":
                return self._json({"status": True, "version": "0.5.20"})
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        stream = bool(body.get("stream"))
        prompt_tokens = max(1, len(json.dumps(body)) // 4)

        if not stream:
            time.sleep(SLOW + random.uniform(0, 0.05))
            return self._json({
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": prompt_tokens,
                          "completion_tokens": 7,
                          "total_tokens": prompt_tokens + 7},
            })

        ollama = self.path.startswith("/api/")
        self.send_response(200)
        self.send_header("Content-Type",
                         "application/x-ndjson" if ollama else "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(data: bytes) -> None:
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()

        time.sleep(SLOW + random.uniform(0, 0.05))  # Prefill
        for i in range(6):
            if ollama:
                chunk(json.dumps({"response": f"t{i}", "done": False}).encode() + b"\n")
            else:
                chunk(b"data: " + json.dumps(
                    {"choices": [{"delta": {"content": f"t{i}"}}]}).encode() + b"\n\n")
            time.sleep(0.02)
        if ollama:
            chunk(json.dumps({"response": "", "done": True,
                              "prompt_eval_count": prompt_tokens,
                              "eval_count": 6}).encode() + b"\n")
        else:
            chunk(b"data: " + json.dumps(
                {"choices": [], "usage": {"prompt_tokens": prompt_tokens,
                                          "completion_tokens": 6}}).encode() + b"\n\n")
            chunk(b"data: [DONE]\n\n")
        chunk(b"")


def serve(port: int, role: str) -> None:
    cls = type(f"H{role}", (Handler,), {"role": role})
    ThreadingHTTPServer(("127.0.0.1", port), cls).serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slow", type=float, default=0.15,
                    help="Prefill-Zeit in Sekunden (wird zur TTFT)")
    a = ap.parse_args()
    global SLOW
    SLOW = a.slow

    for port, role in ((8000, "vllm"), (11434, "ollama"),
                       (8188, "comfyui"), (3000, "openwebui")):
        threading.Thread(target=serve, args=(port, role), daemon=True).start()
        print(f"  {role:10} http://127.0.0.1:{port}")
    print(f"Fake-Stack läuft, Prefill {SLOW}s. Strg+C beendet.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
