# aimonitor Ingest-Contract v1

Stabiles HTTP-Interface für alles, was Daten in aimonitor schieben will: den
`aimon-agent`, den `aimon-tap` und fremde Dienste (OpenTerminal-Autoscaler,
SearXNG-Privacy-Proxy, eigene Skripte).

Dieses Dokument ist die Referenz. Es ist absichtlich so geschrieben, dass es jemand
(oder ein anderes Modell) implementieren kann, ohne den aimonitor-Code zu kennen.

---

## 1. Transport

- Basis-URL: `http(s)://<aimon-server>:8420`
- Alle Ingest-Endpunkte: `POST`, `Content-Type: application/json`
- Auth: `Authorization: Bearer <token>` — Token mit Scope `ingest`
- Optional `Content-Encoding: gzip`
- Antwort `202 Accepted` mit `{"accepted": <n>, "rejected": <n>, "errors": [...]}`
- Zeitstempel: RFC3339 mit Zone (`2026-11-03T12:34:56.789Z`) **oder** Unix-Sekunden
  als Zahl (Float erlaubt). Ohne `ts` setzt der Server die Empfangszeit.
- Idempotenz: optionaler Header `Idempotency-Key`. Gleicher Key innerhalb 10 min →
  der Batch wird verworfen und `{"duplicate": true}` zurückgegeben.
- Limits: 5 MB pro Request, 10 000 Items pro Batch, 429 bei Überschreitung des
  Token-Ratelimits (Default 60 Requests/min pro Token) mit `Retry-After`.

### Verhalten bei Serverausfall

Der Server ist **nicht** kritisch für den Dienst, der pusht. Erwartetes Verhalten
eines Senders:

- Timeout ≤ 5 s, danach aufgeben.
- Bei 5xx/Timeout: In-Memory-Ringpuffer (empfohlen 10 min Daten), Retry mit
  Exponential Backoff (1 s → 60 s, Jitter). Niemals den eigenen Datenpfad blockieren.
- Bei 4xx (außer 429): Batch verwerfen und das lokal loggen. Ein 400 heißt, das Schema
  passt nicht; Retry hilft nicht.

---

## 2. `POST /api/v1/ingest/batch` — der universelle Endpunkt

Ein Batch darf alle Datenarten gleichzeitig enthalten. Alle Felder außer `node` sind
optional.

```json
{
  "node": {
    "name": "gpu-vm-01",
    "hostname": "gpu-vm-01.lan",
    "agent_version": "0.1.0",
    "labels": {"site": "rack-a", "gpu_model": "RTX 4090", "role": "inference"}
  },
  "services": [
    {
      "key": "vllm@gpu-vm-01:8000",
      "kind": "vllm",
      "name": "vllm-qwen3-32b",
      "base_url": "http://10.0.0.11:8000",
      "version": "0.8.5",
      "labels": {"model": "Qwen3-32B-AWQ", "tp": "2"}
    }
  ],
  "samples": [
    {"metric": "gpu.temp_c", "value": 71.0, "ts": 1793707200,
     "labels": {"gpu": "0", "uuid": "GPU-abc123"}},
    {"metric": "gpu.mem_used_bytes", "value": 21474836480, "ts": 1793707200,
     "labels": {"gpu": "0"}},
    {"metric": "llm.requests_running", "value": 3, "service": "vllm@gpu-vm-01:8000"}
  ],
  "requests": [
    {
      "ts": 1793707199.412,
      "service": "vllm@gpu-vm-01:8000",
      "model": "Qwen3-32B-AWQ",
      "ttft_ms": 184.3,
      "total_ms": 4211.0,
      "prompt_tokens": 1842,
      "completion_tokens": 311,
      "status": 200,
      "stream": true,
      "client": "openwebui",
      "route": "/v1/chat/completions",
      "error": null
    }
  ],
  "probes": [
    {"ts": 1793707200, "target": "http://10.0.0.20:3000/health",
     "target_service": "openwebui@host-02:3000",
     "ok": true, "latency_ms": 12.4, "http_status": 200}
  ],
  "events": [
    {
      "ts": 1793707180,
      "service": "privacy-proxy@host-03:8888",
      "severity": "warning",
      "kind": "privacy.block",
      "title": "Anfrage geblockt: Personenname",
      "detail": {"category": "person_name", "rule": "ner.person",
                 "query_redacted": "adresse von [NAME] in berlin",
                 "query_sha256": "9f2b…", "action": "blocked"},
      "dedup_key": "privacy.block:ner.person"
    }
  ]
}
```

### 2.1 `node` (Pflicht)

| Feld | Typ | Pflicht | Bedeutung |
|---|---|---|---|
| `name` | string | ja | Stabiler, eindeutiger Name. Nicht die IP — die ändert sich. |
| `hostname` | string | nein | FQDN |
| `agent_version` | string | nein | Version des Senders |
| `labels` | object<string,string> | nein | Freie Dimensionen, max. 32 Keys |

Ein bislang unbekannter `name` wird automatisch angelegt. `name` ist der Schlüssel —
derselbe Name überschreibt Hostname/Labels.

Wenn der Sender ein Dienst ohne eigene Maschine ist (z. B. ein Proxy im Container),
nimm als `node.name` den Host, auf dem er läuft, und trag den Dienst unter `services` ein.

### 2.2 `services`

| Feld | Typ | Pflicht | Bedeutung |
|---|---|---|---|
| `key` | string | ja | Stabiler Schlüssel, Konvention `<kind>@<host>:<port>` |
| `kind` | enum | ja | `vllm`, `ollama`, `comfyui`, `openwebui`, `searxng`, `privacy_proxy`, `openterminal_proxy`, `openterminal_instance`, `tap`, `other` |
| `name` | string | nein | Anzeigename |
| `base_url` | string | nein | Für Deep-Links in der UI |
| `version` | string | nein | |
| `labels` | object | nein | |

`kind` steuert, welche Panels die UI anzeigt. Unbekannte Werte landen als `other` und
bekommen nur die generischen Panels — der Batch wird dadurch **nicht** abgelehnt.

### 2.3 `samples`

| Feld | Typ | Pflicht | Bedeutung |
|---|---|---|---|
| `metric` | string | ja | `[a-z0-9_.]{1,64}`, siehe `metrics-reference.md` |
| `value` | number | ja | Endlich. `NaN`/`Inf` → Item wird verworfen |
| `ts` | RFC3339 \| number | nein | Default: Empfangszeit |
| `labels` | object<string,string> | nein | max. 16 Keys, Werte max. 200 Zeichen |
| `service` | string | nein | `key` aus `services` |

Counter (Suffix `_total`) werden als monoton steigend behandelt; der Server rechnet
Raten selbst und erkennt Resets. Gauges sendet man einfach wie sie sind.
**Keine Deltas senden.**

### 2.4 `requests` — ein Satz pro Inference-Request

Das ist die wichtigste Datenart für TTFT und Prompt-Länge.

| Feld | Typ | Pflicht | Bedeutung |
|---|---|---|---|
| `ts` | ts | nein | Startzeit des Requests |
| `service` | string | ja | Welches Backend hat bedient |
| `model` | string | nein | Modellname wie vom Client angefragt |
| `ttft_ms` | number | nein | Zeit bis erstes Token. Nur bei Streaming messbar. |
| `total_ms` | number | nein | Gesamtdauer bis letztes Token |
| `prompt_tokens` | int | nein | Echt, wenn der Dienst sie liefert |
| `prompt_chars` | int | nein | Fallback, wenn keine Tokens bekannt |
| `completion_tokens` | int | nein | |
| `status` | int | nein | HTTP-Status |
| `stream` | bool | nein | |
| `client` | string | nein | `openwebui`, `cli`, … |
| `route` | string | nein | |
| `error` | string | nein | Kurze Fehlerklasse, kein Stacktrace |
| `queue_ms` | number | nein | Wartezeit vor Verarbeitungsbeginn |

`ttft_ms` nur setzen, wenn es **gemessen** wurde. Nicht aus `total_ms` schätzen —
der Server markiert geschätzte Werte nicht und würde sie als echt ausweisen.

### 2.5 `probes` — Konnektivität

Damit baut die UI den Dienstgraphen. Jeder Sender probt die Dienste, die er kennt.

| Feld | Typ | Pflicht |
|---|---|---|
| `target` | string | ja (URL oder `host:port`) |
| `target_service` | string | nein (`key`, macht die Kante im Graph exakt) |
| `ok` | bool | ja |
| `latency_ms` | number | nein |
| `http_status` | int | nein |
| `error` | string | nein |

### 2.6 `events`

| Feld | Typ | Pflicht | Bedeutung |
|---|---|---|---|
| `kind` | string | ja | Punktiert, z. B. `privacy.block`, `scale.up`, `gpu.oom` |
| `severity` | enum | ja | `debug`, `info`, `warning`, `error`, `critical` |
| `title` | string | ja | Eine Zeile, menschenlesbar, ohne PII |
| `ts` | ts | nein | |
| `service` | string | nein | |
| `detail` | object | nein | Beliebiges JSON, ≤ 8 KB |
| `dedup_key` | string | nein | Gleicher Key in 60 s → nur Zähler hoch, kein neues Event |

---

## 3. Spezial-Endpunkte (bequemer, gleiche Wirkung)

Beides sind reine Convenience-Wrapper auf `/ingest/batch`. Wer will, kann
ausschließlich `/ingest/batch` benutzen.

### 3.1 `POST /api/v1/ingest/scaler` — Autoscaler-Status

Für den OpenTerminal-Proxy. Ein Snapshot des aktuellen Zustands, Intervall 10–15 s.

```json
{
  "node": {"name": "proxy-host-01"},
  "service": {"key": "openterminal-proxy@proxy-host-01:7681",
              "kind": "openterminal_proxy", "name": "OpenTerminal Autoscaler"},
  "ts": 1793707200,
  "state": {
    "instances_running": 4,
    "instances_desired": 4,
    "instances_min": 1,
    "instances_max": 12,
    "instances_starting": 0,
    "instances_draining": 1,
    "sessions_active": 27,
    "sessions_per_instance_target": 10,
    "sessions_queued": 0,
    "cpu_pct": 46.0,
    "mem_used_bytes": 7516192768,
    "mem_total_bytes": 33285996544,
    "scale_cooldown_s_remaining": 42,
    "last_scale_reason": "sessions_per_instance>8 for 90s"
  },
  "instances": [
    {"id": "ot-7f3a", "state": "running", "sessions": 9,
     "started_at": 1793705000, "cpu_pct": 51.0, "mem_used_bytes": 1932735283,
     "host": "proxy-host-01", "port": 7701}
  ]
}
```

Der Server leitet daraus ab:

- `openterminal.instances_running`, `…_desired`, `…_min`, `…_max`, `…_starting`, `…_draining`
- `openterminal.sessions_active`, `…_queued`
- `openterminal.capacity_sessions` = `instances_max * sessions_per_instance_target`
- `openterminal.headroom_sessions` = `capacity_sessions - sessions_active`
- `openterminal.headroom_pct` = `headroom_sessions / capacity_sessions * 100`
- `openterminal.saturation_pct` = `sessions_active / (instances_running * sessions_per_instance_target) * 100`

Scale-Vorgänge **zusätzlich** als Event melden, mit `kind` `scale.up` / `scale.down` /
`scale.blocked` und `detail` = `{"from": 3, "to": 4, "reason": "...", "trigger_metric": "..."}`.

### 3.2 `POST /api/v1/ingest/privacy` — Privacy-Proxy

Für den SearXNG-Vorschalt-Proxy. Entweder pro Request (`decisions`) oder als
aggregierter Snapshot (`counters`) oder beides.

```json
{
  "node": {"name": "proxy-host-01"},
  "service": {"key": "privacy-proxy@proxy-host-01:8888",
              "kind": "privacy_proxy", "name": "SearXNG Privacy Proxy"},
  "ts": 1793707200,
  "counters": {
    "requests_total": 18422,
    "allowed_total": 17980,
    "blocked_total": 388,
    "redacted_total": 54,
    "errors_total": 11,
    "upstream_errors_total": 3,
    "blocked_by_category": {"person_name": 301, "email": 44, "phone": 21,
                            "address": 15, "id_number": 7},
    "upstream_latency_ms_sum": 412880.0,
    "upstream_latency_ms_count": 18411
  },
  "decisions": [
    {
      "ts": 1793707198.8,
      "action": "blocked",
      "category": "person_name",
      "rule": "ner.person.v2",
      "confidence": 0.93,
      "query_redacted": "wohnort von [NAME]",
      "query_sha256": "9f2b8c...",
      "query_len": 21,
      "matched_spans": [{"start": 10, "end": 16, "type": "PERSON"}],
      "client_id": "u:3f1a",
      "upstream_latency_ms": null,
      "upstream_status": null
    },
    {
      "ts": 1793707199.1,
      "action": "redacted",
      "category": "email",
      "rule": "regex.email",
      "query_redacted": "kontakt [EMAIL] impressum",
      "query_sha256": "11aa…",
      "query_len": 32,
      "upstream_latency_ms": 221.0,
      "upstream_status": 200
    },
    {"ts": 1793707199.5, "action": "allowed", "query_len": 14,
     "upstream_latency_ms": 180.0, "upstream_status": 200}
  ]
}
```

`action` ∈ `allowed` | `redacted` | `blocked` | `error`.

**Datenschutz — verbindlich:**

- `query_raw` **nicht** senden, außer es ist bewusst konfiguriert. Der Server nimmt es
  nur an, wenn er selbst mit `AIMON_ALLOW_RAW_QUERIES=true` läuft, speichert es getrennt
  und löscht es nach `RAW_QUERY_RETENTION_DAYS` (Default 2).
- `query_redacted` ist die Variante **nach** dem Filtern. Es muss sicher sein, sie in
  einem Dashboard anzuzeigen. Wer das nicht garantieren kann, lässt das Feld weg und
  sendet nur `category` + `query_sha256` + `query_len`.
- `client_id` ist pseudonymisiert (z. B. HMAC der Session-ID mit serverseitigem Salt),
  keine IP, kein Benutzername.
- `matched_spans` enthält nur Offsets und Typ, nie den Treffertext.

`allowed`-Entscheidungen einzeln zu senden ist optional und bei hohem Volumen
unnötig — dann reichen `counters`. `blocked` und `redacted` sollten immer einzeln
kommen, weil genau die im Log stehen sollen.

---

## 4. Health und Selbstkontrolle

- `GET /api/v1/health` → `{"status":"ok","db":"ok","version":"…"}`, kein Token nötig.
- `GET /api/v1/ingest/echo` (Token nötig) → gibt Token-Name und Scopes zurück. Damit
  prüft ein Integrator, ob sein Token funktioniert, ohne Daten zu schreiben.
- `POST /api/v1/ingest/batch?dry_run=1` → validiert und verwirft; die Antwort enthält
  jeden Validierungsfehler mit JSON-Pointer. Zum Entwickeln einer Integration gedacht.

## 5. Versionierung

Pfad-Präfix `/api/v1` ist stabil. Neue optionale Felder können jederzeit dazukommen;
unbekannte Felder im Request werden ignoriert und in `response.warnings` genannt
(nicht abgelehnt). Breaking Changes nur unter `/api/v2`.
