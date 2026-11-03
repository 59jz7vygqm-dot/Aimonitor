# aimonitor — Architektur

## Ziel

Ein zentrales Dashboard für einen selbstgehosteten KI-Stack aus mehreren GPU-VMs
(vLLM, Ollama, ComfyUI), OpenWebUI als Frontend, einem Autoscaling-Proxy für
OpenTerminal und einem Privacy-Proxy vor SearXNG.

Harte Anforderungen, die die Architektur bestimmen:

1. **Neue Server ohne zentrale Konfigänderung** → Push-Modell mit Self-Registration.
2. **Echte Inference-Metriken** (Time to First Token, Prompt-Länge) → nicht ratbar aus
   Host-Metriken; braucht entweder vLLM-Histogramme oder einen Request-Tap.
3. **Verbindungen zwischen allen Diensten** → aktive Probes + beobachteter Traffic.
4. **Fehler als kopierbarer Prompt** → Server muss Topologie, Versionen und Anomalien
   in einem Textblock zusammenfassen können.
5. **Fremde Dienste (die zwei KI-gebauten Proxies) anbindbar** → stabiles, dokumentiertes
   Ingest-Contract, das ein anderes Modell ohne Kenntnis dieses Codes implementieren kann.

## Komponenten

```
                 GPU-VM 1            GPU-VM 2            GPU-VM n
             ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
             │ aimon-agent  │    │ aimon-agent  │    │ aimon-agent  │
             │ (Go, systemd)│    │              │    │              │
             │  nvidia-smi  │    │  nvidia-smi  │    │              │
             │  vLLM /metrics    │  Ollama      │    │  ComfyUI     │
             │  probes      │    │  probes      │    │  probes      │
             └──────┬───────┘    └──────┬───────┘    └──────┬───────┘
                    │ HTTPS push (Bearer-Token, gzip, NDJSON-Batch)
                    └───────────────────┼───────────────────┘
                                        v
   OpenWebUI ──► aimon-tap ──► vLLM/Ollama      ┌─────────────────────────┐
                    │ (echte TTFT, Tokens)      │      aimon-server       │
                    └──────────────────────────►│  FastAPI                │
                                                │  /api/v1/ingest/*       │
   OpenTerminal-Autoscaler ───── POST ─────────►│  /api/v1/query          │
   SearXNG-Privacy-Proxy   ───── POST ─────────►│  /api/v1/diagnostics    │
                                                └───────────┬─────────────┘
                                                            │
                                                  ┌─────────▼──────────┐
                                                  │ TimescaleDB /      │
                                                  │ PostgreSQL         │
                                                  │  samples (raw)     │
                                                  │  samples_rollup    │
                                                  │  requests, events  │
                                                  └────────────────────┘
                                                            │
                                                  ┌─────────▼──────────┐
                                                  │ Web-UI (statisch,  │
                                                  │ vom Server serviert)│
                                                  └────────────────────┘
```

| Komponente | Sprache | Deployment | Aufgabe |
|---|---|---|---|
| `aimon-server` | Python 3.12 / FastAPI | Docker Compose (zentraler Host) | Ingest, Speicherung, Query, Rollups, Diagnose, UI |
| `aimon-agent` | Go (statisch, CGO_ENABLED=0) | systemd auf jeder VM | Metriken sammeln und pushen |
| `aimon-tap` | Go (dieselbe Binary, `aimon-agent tap`) | systemd, optional | Reverse-Proxy vor den Inference-Backends, misst echte TTFT |
| `aimon-web` | Vanilla JS + vendored uPlot | wird vom Server ausgeliefert | Dashboard |
| DB | TimescaleDB (PG16) | Docker Compose | Zeitreihen, Events, Requests |

## Warum Push und nicht Pull

Pull (Prometheus-Stil) braucht eine zentrale Liste aller Targets. Jeder neue Server
bedeutet eine Konfigänderung und einen Reload. Push + Self-Registration heißt:
Agent installieren, Token eintragen, fertig — der Server legt Node und Services selbst
an. Dazu funktioniert Push durch NAT und über wechselnde IPs, was bei gemieteten
GPU-VMs der Normalfall ist.

Preis dafür: der Server kann nicht zwischen „Agent down" und „Netz down" unterscheiden.
Deshalb trackt er `last_seen` pro Node und wertet eine Lücke > `stale_after` als
`stale`, was explizit als eigener Zustand (nicht als „GPU auf 0 %") angezeigt wird.

## Datenmodell

Generisches Series-Dictionary, damit neue Metriken **ohne Migration** dazukommen:

- `series(id, metric, labels_hash, labels jsonb, node_id, service_id)` — Dictionary.
- `samples(ts, series_id, value)` — Hypertable, Raw-Daten.
- `samples_rollup(bucket_s, ts, series_id, n, sum, min, max, last)` — 60 s und 3600 s,
  von einem Worker im Server gefüllt (nicht als Continuous Aggregate, damit der Code
  auch auf nacktem PostgreSQL ohne Timescale läuft).
- `requests(ts, service_id, model, ttft_ms, total_ms, prompt_tokens, completion_tokens,
  status, …)` — ein Satz pro Inference-Request, aus `aimon-tap` oder einem instrumentierten
  Dienst. Hieraus kommen TTFT-Perzentile und durchschnittliche Prompt-Länge.
- `events(ts, service_id, severity, kind, title, detail jsonb, dedup_key)` — diskrete
  Vorfälle: geblockte SearXNG-Anfrage, Scale-up/down, CUDA-OOM, Probe-Ausfall.
- `probes(ts, from_node_id, target, ok, latency_ms, http_status)` — Konnektivitätsmatrix.
- `edges(src, dst, kind, …)` — aus `probes` und `requests` abgeleiteter Dienstgraph.
- `incidents(id, rule, severity, subject, started_at, ended_at, context jsonb)` — vom
  Regelwerk erzeugt, Grundlage des Diagnose-Prompts.

Quantile (TTFT p50/p90/p99) werden auf zwei Wegen gewonnen:

1. **Exakt** aus `requests`, wenn `aimon-tap` oder ein instrumentierter Dienst liefert.
2. **Histogramm-Interpolation** aus den vLLM-Buckets
   (`vllm:time_to_first_token_seconds_bucket`), die der Agent als
   `llm.ttft_bucket{le="…"}` mitschickt. Ungenauer, aber ohne Eingriff in den Datenpfad.

Welcher Weg benutzt wurde, steht in der Antwort der Query-API (`source: "requests"` vs.
`source: "histogram"`). Keine stillen Schätzungen.

## Retention

| Stufe | Auflösung | Default |
|---|---|---|
| `samples` | Scrape-Intervall (10 s) | 14 Tage |
| `samples_rollup` 60 s | 1 min | 90 Tage |
| `samples_rollup` 3600 s | 1 h | 730 Tage |
| `requests` | pro Request | 30 Tage |
| `events` | pro Event | 180 Tage |

Konfigurierbar in `deploy/.env`. Wenn die Timescale-Extension vorhanden ist, werden
`samples`, `requests`, `probes` und `events` zu Hypertables mit Kompression; ansonsten
bleiben sie normale Tabellen mit BRIN-Index auf `ts` und der Retention-Worker löscht
per `DELETE`.

## Sicherheit und Datenschutz

- **Ingest** braucht immer ein Bearer-Token (`api_tokens`, nur SHA-256-Hash gespeichert,
  Scopes `ingest` / `read` / `admin`).
- **Der Privacy-Proxy ist der heikle Teil.** „Log welcher Request geblockt wurde" heißt
  per Definition, dass die Daten, die der Proxy gerade entfernen soll, im Monitoring
  landen könnten. Default im Contract ist deshalb:
  `query_redacted` (PII durch `[EMAIL]`, `[PHONE]`, `[NAME]` ersetzt) + `query_sha256`
  zum Wiedererkennen gleicher Anfragen. Der Rohtext (`query_raw`) wird nur übertragen,
  wenn im Proxy **und** im Server explizit `store_raw_queries=true` gesetzt ist, und dann
  mit eigener, kürzerer Retention (`RAW_QUERY_RETENTION_DAYS`, Default 2).
- Die UI selbst hat keine eigene Benutzerverwaltung. Wenn `AIMON_READ_TOKEN` gesetzt ist,
  verlangt jeder Lese-Endpunkt dieses Token; ansonsten ist die UI offen und gehört hinter
  einen Reverse-Proxy mit Auth. Das ist eine bewusste Entscheidung, keine Lücke aus
  Versehen — Single-Admin-Homelab.

## Neuen Server anhängen

```bash
curl -fsSL http://aimon.example/install.sh | AIMON_URL=... AIMON_TOKEN=... sh
```

Das Skript lädt die Binary, schreibt `/etc/aimon/agent.yaml`, aktiviert den
systemd-Service. Der Agent erkennt lokal laufende Dienste selbst
(Port-Scan auf die bekannten Ports + `/metrics`-Fingerprint) und registriert sie.
Zentral ist nichts zu tun.

## Nicht-Ziele

- Kein Log-Aggregator. Events ja, Volltext-Logs nein — dafür Loki/journald.
- Kein Alerting-Router mit Eskalationsketten. Es gibt Incidents + Webhook; wer PagerDuty
  will, hängt es an den Webhook.
- Keine Metrik-Langzeitarchivierung über 2 Jahre.
