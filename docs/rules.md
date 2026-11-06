# Welche Probleme aimonitor erkennt

Jede Zeile ist eine Regel. Schlägt sie an, entsteht ein Incident; der steht
auf der Übersicht, taucht im Diagnose-Prompt mit vollem Kontext auf und löst —
sofern eingerichtet — eine Benachrichtigung aus.

Was hier nicht steht, wird nicht erkannt. Das ist Absicht: eine Regel, die
raten müsste, gibt es lieber nicht.

## Funktioniert der Stack überhaupt?

Eine Regel, die als einzige nicht eine Eigenschaft misst, sondern die Funktion.
Nur aktiv, wenn ein Kanarienvogel konfiguriert ist (`AIMON_CANARY_TARGETS`).

| Regel | Schlägt an, wenn | Schwelle |
|---|---|---|
| `canary.failing` | ein echter Chat-Request keine Antwort bekommt | — |
| `canary.flaky` | der letzte Versuch klappte, andere in der Stunde nicht | ab 4 Versuchen |
| `canary.slow` | die Antwort kommt, aber spät | `AIMON_CANARY_TTFT_WARN_MS` 5000 |

Der Unterschied zu `probe.failing` ist der Punkt: ein Port kann offen sein,
während kein Modell geladen ist. Ein vLLM kann saubere Metriken liefern,
während der Proxy davor auf den falschen Upstream zeigt. Beides sieht im
Dashboard gut aus, und beides fällt erst auf, wenn man es benutzt — oder wenn
der Kanarienvogel es tut.

`canary.flaky` ist bewusst von `canary.failing` getrennt: ein Dienst, der jede
dritte Anfrage verliert, sieht ohne diese Unterscheidung genauso gesund aus
wie einer, der läuft.

## Knoten und Host

| Regel | Schlägt an, wenn | Schwelle |
|---|---|---|
| `node.stale` | ein Agent nichts mehr meldet | `AIMON_STALE_AFTER_S` |
| `host.mem_high` | Arbeitsspeicher knapp (gemessen an `MemAvailable`) | `AIMON_MEM_WARN_PCT` 90 / `AIMON_MEM_CRIT_PCT` 96 |
| `host.mem_high` | dasselbe auf Maschinen mit geteiltem Speicher | `AIMON_MEM_WARN_PCT_SHARED` 82 |
| `host.swap_used` | die Auslagerungsdatei spürbar belegt ist | `AIMON_SWAP_WARN_PCT` 25 |
| `host.disk_full` | ein Dateisystem volläuft | `AIMON_DISK_WARN_PCT` 88 / krit. 96 |
| `host.inodes_full` | die Inodes ausgehen, obwohl Platz frei ist | `AIMON_INODES_WARN_PCT` 85 / krit. 95 |
| `host.cpu_hot` | die CPU-Temperatur hoch ist | `AIMON_CPU_TEMP_WARN_C` 90 / `_CRIT_C` 97 |
| `token.shared` | mehrere Maschinen dasselbe Ingest-Token benutzen | — |
| `probe.failing` | ein Dienst von einem anderen Knoten nicht erreichbar ist | — |

Warum `MemAvailable` und nicht `MemFree`: Linux benutzt freien Speicher als
Cache. Gegen `MemFree` gemessen wäre jeder gesunde Server im Daueralarm.

Warum auf geteiltem Speicher früher: auf GB10, Grace und Jetson teilen sich
CPU und GPU denselben physischen Speicher. Wird er knapp, wird nichts
langsamer — der OOM-Killer beendet den größten Prozess, und das ist das
Modell.

## GPU

| Regel | Schlägt an, wenn | Schwelle |
|---|---|---|
| `gpu.temp_high` | die Karte heiß wird | `AIMON_GPU_TEMP_WARN_C` 83 / `_CRIT_C` 88 |
| `gpu.throttling` | der Treiber thermisches oder HW-Throttling meldet | — |
| `gpu.power_cap_idle` | das Power-Limit greift, **obwohl** die Karte kaum rechnet | `AIMON_POWER_CAP_IDLE_UTIL_PCT` 30 |
| `gpu.vram_high` | der VRAM volläuft | `AIMON_VRAM_WARN_PCT` 92 / `_CRIT_PCT` 98 |
| `gpu.ecc_errors` | der Treiber korrigierte Speicherfehler zählt | — |

`gpu.power_cap_idle` meldet bewusst **nicht**, wenn eine ausgelastete Karte am
Power-Limit hängt: das ist der Normalbetrieb einer L4 und war in der ersten
Fassung ein Dauerfehlalarm.

## Inferenz

| Regel | Schlägt an, wenn | Schwelle |
|---|---|---|
| `llm.ttft_high` | die Zeit bis zum ersten Token hoch ist | `AIMON_TTFT_WARN_MS` 2000 / `_CRIT_MS` 8000 |
| `llm.queue_backlog` | Requests warten, statt zu laufen | — |
| `llm.kv_cache_high` | der KV-Cache volläuft | `AIMON_KV_CACHE_WARN_PCT` 90 |
| `llm.preemptions` | vLLM laufende Requests verdrängt | — |
| `llm.errors` | ein Backend Fehler zurückgibt | — |
| `llm.no_metrics` | ein vLLM läuft, aber keine Metriken liefert | — |
| `comfy.queue_backlog` | die ComfyUI-Warteschlange steht | — |
| `comfy.vram_low` | ComfyUI zu wenig VRAM frei meldet | — |

`llm.no_metrics` gilt nur für vLLM. Ollama hat keinen Metrik-Endpunkt — das
dort zu melden wäre eine Dauermeldung über eine Eigenschaft, nicht über einen
Fehler.

## Proxies

| Regel | Schlägt an, wenn | Schwelle |
|---|---|---|
| `openterminal.headroom_low` | kaum noch Instanzen frei sind | `AIMON_HEADROOM_WARN_PCT` 20 / `_CRIT_PCT` 5 |
| `openterminal.queue` | Benutzer auf eine Instanz warten | — |
| `openterminal.scale_lag` | der Autoscaler hochskalieren will, aber nichts passiert | — |
| `privacy.block_rate_high` | auffällig viele Suchen geblockt werden | `AIMON_BLOCK_RATE_WARN_PCT` 25, min. `AIMON_BLOCK_RATE_MIN_REQUESTS` 50 Anfragen |
| `privacy.upstream_errors` | SearXNG hinter dem Filter Fehler liefert | — |

Die Mindestzahl bei der Blockquote ist kein Schönheitsfehler: 1 von 3
geblockten Anfragen sind 33 % und bedeuten nichts.

## Noch nicht erkannt

Ehrlichkeitshalber, Stand heute:

- **Kein Trend.** „Platte zu 88 % voll" wird gemeldet, „Platte in zwei Tagen
  voll" nicht.
- **Kein verschwundener Dienst.** Stirbt ein einzelner Dienst, während der
  Knoten weiterläuft, fällt das nur auf, wenn ihn jemand anprobt.
- **Kein LiteLLM, kein n8n.**
