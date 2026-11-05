# Metrik-Referenz

Konvention: `<domäne>.<name>[_<einheit>]`. Counter enden auf `_total` und sind monoton;
alles andere ist ein Gauge. Einheiten immer SI-Basis (`_bytes`, `_s`) außer Latenzen,
die aus Lesbarkeitsgründen `_ms` sind, und Temperaturen in `_c`.

## `gpu.*` — pro GPU (Label `gpu` = Index, `uuid`, `name`)

| Metrik | Einheit | Quelle |
|---|---|---|
| `gpu.util_pct` | % | `nvidia-smi utilization.gpu` |
| `gpu.mem_util_pct` | % | `utilization.memory` |
| `gpu.mem_used_bytes` | Bytes | `memory.used` |
| `gpu.mem_total_bytes` | Bytes | `memory.total` |
| `gpu.mem_reserved_bytes` | Bytes | `memory.reserved` |
| `gpu.mem_unified` | 0/1 | GPU teilt den Speicher mit dem Host |
| `gpu.mem_unavailable` | 0/1 | Treiber meldet keinen Speicher und es ist kein geteiltes System |
| `gpu.temp_c` | °C | `temperature.gpu` |
| `gpu.temp_mem_c` | °C | `temperature.memory` (nur HBM-Karten) |
| `gpu.power_w` | W | `power.draw` |
| `gpu.power_limit_w` | W | `power.limit` |
| `gpu.fan_pct` | % | `fan.speed` |
| `gpu.clock_sm_mhz` | MHz | `clocks.sm` |
| `gpu.clock_mem_mhz` | MHz | `clocks.mem` |
| `gpu.throttle_hw` | 0/1 | `clocks_throttle_reasons.hw_slowdown` |
| `gpu.throttle_thermal` | 0/1 | `…sw_thermal_slowdown` \| `hw_thermal_slowdown` |
| `gpu.throttle_power` | 0/1 | `…sw_power_cap` |
| `gpu.pcie_gen` | – | `pcie.link.gen.current` |
| `gpu.pcie_width` | – | `pcie.link.width.current` |
| `gpu.ecc_errors_total` | Count | `ecc.errors.uncorrected.volatile.total` |
| `gpu.proc_mem_bytes` | Bytes | `--query-compute-apps`, Label `pid`, `pname` |
| `gpu.count` | – | Anzahl sichtbarer GPUs auf dem Node |

### Woher der Speicherwert kommt

Das Label `memory` an `gpu.mem_total_bytes` und `gpu.mem_used_bytes` sagt es:

| `memory` | Bedeutung |
|---|---|
| (kein Label) | Eigener VRAM, direkt von `--query-gpu` |
| `unified` | Aus `nvidia-smi -q -d MEMORY`, weil `--query-gpu` nichts lieferte |
| `host` | Arbeitsspeicher des Hosts – der Treiber meldet gar keinen GPU-Speicher |

Der letzte Fall ist der Normalzustand auf Grace-Blackwell- und
Tegra-Plattformen (DGX Spark, Jetson): CPU und GPU teilen sich denselben
physischen Speicher, es gibt keinen eigenen VRAM-Pool, über den der Treiber
berichten könnte. Gemessen wurde hier `/proc/meminfo` — die Zahl ist echt, nur
beschreibt sie den gemeinsamen Speicher. Wer dort ein Modell lädt, belegt
genau den.

Die Erkennung läuft über das GPU-Modell, `/etc/nv_tegra_release` und den
Device-Tree. Liegt sie daneben, lässt sie sich in `/etc/aimon/agent.yaml`
überschreiben (`unified_memory: true` oder `false`). `aimon-agent gpudump`
zeigt, was erkannt wurde und warum.

Summen über `gpu.mem_total_bytes` müssen nach `memory` trennen: den
Arbeitsspeicher eines Hosts mit mehreren GPUs mitzuzählen ergäbe sonst ein
Vielfaches des tatsächlich vorhandenen Speichers.

## `host.*`

`host.cpu_pct`, `host.cpu_pct_per_core` (Label `core`), `host.load1`, `host.load5`,
`host.load15`, `host.mem_used_bytes`, `host.mem_total_bytes`, `host.mem_available_bytes`,
`host.swap_used_bytes`, `host.disk_used_bytes` + `host.disk_total_bytes` (Label `mount`),
`host.disk_read_bps` / `host.disk_write_bps` (Label `dev`),
`host.net_rx_bps` / `host.net_tx_bps` (Label `iface`),
`host.uptime_s`, `host.cpu_temp_c` (Label `sensor`), `host.procs_running`,
`host.fd_used`, `host.agent_scrape_ms` (Label `collector`),
`host.agent_scrape_errors_total` (Label `collector`).

## `llm.*` — vLLM, Ollama, Tap (Label `service`, `model`)

Aus dem vLLM-`/metrics`-Endpunkt gemappt:

| aimonitor | vLLM |
|---|---|
| `llm.requests_running` | `vllm:num_requests_running` |
| `llm.requests_waiting` | `vllm:num_requests_waiting` |
| `llm.requests_swapped` | `vllm:num_requests_swapped` |
| `llm.kv_cache_usage_pct` | `vllm:gpu_cache_usage_perc` × 100 |
| `llm.kv_cache_cpu_usage_pct` | `vllm:cpu_cache_usage_perc` × 100 |
| `llm.prefix_cache_hit_pct` | `vllm:gpu_prefix_cache_hit_rate` × 100 |
| `llm.prompt_tokens_total` | `vllm:prompt_tokens_total` |
| `llm.generation_tokens_total` | `vllm:generation_tokens_total` |
| `llm.requests_total` | `vllm:request_success_total` (Label `finished_reason`) |
| `llm.preemptions_total` | `vllm:num_preemptions_total` |
| `llm.ttft_bucket` | `vllm:time_to_first_token_seconds_bucket` (Label `le`, Sek.) |
| `llm.ttft_sum_s` / `llm.ttft_count` | `…_sum` / `…_count` |
| `llm.e2e_bucket`, `llm.e2e_sum_s`, `llm.e2e_count` | `vllm:e2e_request_latency_seconds_*` |
| `llm.tpot_bucket`, … | `vllm:time_per_output_token_seconds_*` |
| `llm.prompt_len_bucket`, `llm.prompt_len_sum`, `llm.prompt_len_count` | `vllm:request_prompt_tokens_*` |
| `llm.gen_len_bucket`, … | `vllm:request_generation_tokens_*` |
| `llm.queue_time_bucket`, … | `vllm:request_queue_time_seconds_*` |

Abgeleitet vom Server:

- `llm.ttft_p50_ms`, `_p90_ms`, `_p99_ms` — aus `requests` (exakt) oder aus den Buckets
  (interpoliert; die Query-API sagt, welches von beidem).
- `llm.ttft_avg_ms` = `rate(llm.ttft_sum_s) / rate(llm.ttft_count) * 1000`
- `llm.prompt_len_avg_tokens` = `rate(llm.prompt_len_sum) / rate(llm.prompt_len_count)`
- `llm.tokens_out_per_s` = `rate(llm.generation_tokens_total)`
- `llm.throughput_rps` = `rate(llm.requests_total)`

`ollama.*` für Dinge, die vLLM nicht hat:
`ollama.models_loaded`, `ollama.model_vram_bytes` (Label `model`),
`ollama.model_expires_in_s`, `ollama.model_context_len`, `ollama.probe_ttft_ms`
(synthetischer Canary-Request, Label `model`), `ollama.probe_ok`.

> **Zwei Schalter bei vLLM, die Verschiedenes tun.** `--disable-log-stats`
> schaltet `/metrics` ab und damit alles in dieser Tabelle — das Flag gehört
> weggelassen. `--enable-force-include-usage` legt das `usage`-Objekt in den
> gestreamten Antwortstrom; nur damit kann `aimon-tap` Tokens statt Zeichen
> zählen. Fehlt es, misst der Tap TTFT und Dauer weiter exakt, meldet aber
> `prompt_chars` statt `prompt_tokens` und schickt einmalig den Hinweis
> `tap.no_token_counts`.

> Ollama hat keinen Metrik-Endpunkt. Echte TTFT pro Benutzer-Request gibt es nur über
> `aimon-tap`. Ohne Tap liefert der Agent `ollama.probe_ttft_ms` aus einem eigenen
> 1-Token-Request — das ist eine Messung der Bereitschaft des Modells, **nicht** der
> Benutzerlatenz. Die UI markiert diese Serie entsprechend.

## `comfy.*` — ComfyUI

`comfy.queue_pending`, `comfy.queue_running`, `comfy.vram_free_bytes`,
`comfy.vram_total_bytes`, `comfy.torch_vram_free_bytes`, `comfy.exec_last_ms`,
`comfy.exec_bucket` (Label `le`), `comfy.jobs_total` (Label `status`),
`comfy.up`.

## `openwebui.*`

`openwebui.up`, `openwebui.version_info` (Gauge 1, Labels mit Version),
`openwebui.response_ms` (eigene Health-Probe).

## `openterminal.*` — siehe `integration-contract.md` §3.1

`openterminal.instances_running`, `…_desired`, `…_min`, `…_max`, `…_starting`,
`…_draining`, `openterminal.sessions_active`, `…_queued`,
`openterminal.capacity_sessions`, `openterminal.headroom_sessions`,
`openterminal.headroom_pct`, `openterminal.saturation_pct`,
`openterminal.instance_sessions` (Label `instance`),
`openterminal.instance_uptime_s`, `openterminal.scale_cooldown_s`,
`openterminal.scale_events_total` (Label `direction`).

## `privacy.*` — siehe `integration-contract.md` §3.2

`privacy.requests_total`, `privacy.allowed_total`, `privacy.blocked_total`
(Label `category`), `privacy.redacted_total` (Label `category`),
`privacy.errors_total`, `privacy.upstream_errors_total`,
`privacy.block_rate_pct`, `privacy.upstream_latency_avg_ms`,
`privacy.query_len_avg_chars`.

## `probe.*`

`probe.up` (0/1), `probe.latency_ms`, `probe.http_status` —
Labels `from_node`, `target`, `target_service`.

## `aimon.*` — Selbstüberwachung des Servers

`aimon.ingest_items_total` (Label `kind`), `aimon.ingest_rejected_total` (Label `reason`),
`aimon.db_write_ms`, `aimon.series_count`, `aimon.samples_rows`,
`aimon.rollup_lag_s`, `aimon.retention_deleted_total`, `aimon.nodes_stale`.
