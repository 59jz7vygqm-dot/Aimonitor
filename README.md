# aimonitor

Zentrales Monitoring für einen selbstgehosteten KI-Stack aus mehreren GPU-VMs
(vLLM, Ollama, ComfyUI), OpenWebUI als Frontend, einem Autoscaling-Proxy für
OpenTerminal und einem Privacy-Proxy vor SearXNG.

Was es kann:

- **GPU und Host** je Maschine: Temperatur, VRAM, Auslastung, Takt, Throttling,
  Leistungsaufnahme, Prozesse auf der Karte, CPU, RAM, Platte, Netz.
- **Echte Inference-Kennzahlen**: Time to First Token und Prompt-Länge **pro
  Request**, gemessen im Datenpfad. Nicht geschätzt, nicht interpoliert — und
  wenn doch, steht es dabei.
- **Verbindungen zwischen allen Diensten**: Dienstgraph und Erreichbarkeitsmatrix
  aus aktiven Checks und beobachtetem Verkehr.
- **OpenTerminal**: Instanzzahl, Sessions, Headroom gegen die Obergrenze,
  Sättigung gegen die laufenden Instanzen, Scale-Historie samt blockierter
  Scale-ups.
- **SearXNG-Privacy-Proxy**: Anfragen, Blockrate, Blockgründe nach Kategorie und
  Regel, Log jeder geblockten Anfrage — in bereinigter Form.
- **Fehlerbericht als kopierbarer Prompt**: beschreibt Aufbau, Messwerte und
  Fehlerbild in einem Textblock, den man einem anderen Modell oder einem anderen
  Menschen vorlegen kann. Auf Wunsch mit anonymisierten Hostnamen.
- **Neue Server anhängen**, ohne zentral etwas zu konfigurieren.

```
GPU-VM ──► aimon-agent ──┐
OpenWebUI ► aimon-tap ───┼──► aimon-server ──► PostgreSQL/TimescaleDB
Proxies ─── HTTP POST ───┘          │
                                    └──► Web-UI
```

Ausführlich: [`docs/architecture.md`](docs/architecture.md).

---

## Schnellstart

### 1. Zentraler Stack

```bash
git clone <repo> && cd aimonitor/deploy
cp .env.example .env

# Pflichtfelder setzen:
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -base64 32)|" .env
sed -i "s|^AIMON_BOOTSTRAP_TOKEN=.*|AIMON_BOOTSTRAP_TOKEN=$(openssl rand -hex 32)|" .env

docker compose up -d
```

Dashboard: `http://<host>:8420` — der Server lauscht per Default auf allen
Interfaces (`AIMON_BIND=0.0.0.0` in `.env`). Für „nur lokal, Reverse-Proxy
davor" auf `127.0.0.1` setzen.

Ohne `sudo` scheitert Docker mit `permission denied ... /var/run/docker.sock`.
Entweder `sudo` davorsetzen oder einmalig:

```bash
sudo usermod -aG docker "$USER" && newgrp docker
```

### 2. Agent-Token anlegen

```bash
export AIMON_URL=http://<host>:8420
export AIMON_BOOTSTRAP_TOKEN=$(grep ^AIMON_BOOTSTRAP_TOKEN deploy/.env | cut -d= -f2)

make token NAME=agent-gpu-vm-01
```

Das Token wird **nur einmal** ausgegeben; gespeichert ist nur sein Hash.

### 3. Agent auf einer GPU-VM

```bash
make agent-all     # baut die Binaries nach web/dist/

# auf der GPU-VM:
sudo AIMON_URL=http://<host>:8420 AIMON_TOKEN=aimon_... \
     ./deploy/agent/install.sh
```

Das Skript installiert die Binary, schreibt `/etc/aimon/agent.yaml`, führt einen
**Selbsttest** aus und startet den systemd-Dienst erst, wenn der durchläuft.
Lokal laufende Dienste findet der Agent selbst (vLLM, Ollama, ComfyUI,
OpenWebUI, SearXNG) — zentral ist nichts einzutragen.

Prüfen:

```bash
aimon-agent check       # Token, GPU, Host, Dienste, Peers
aimon-agent discover    # gefundene Dienste als YAML
journalctl -u aimon-agent -f
```

### 4. Echte TTFT messen (optional, aber der eigentliche Punkt)

vLLM liefert nur Histogramme, Ollama gar nichts. Wer TTFT **pro Request** und
**pro Modell** will, hängt `aimon-tap` zwischen OpenWebUI und das Backend:

```bash
sudo INSTALL_TAP=1 TAP_UPSTREAM=http://127.0.0.1:8000 TAP_LISTEN=0.0.0.0:9500 \
     AIMON_URL=... AIMON_TOKEN=... ./deploy/agent/install.sh
```

Danach in OpenWebUI die Backend-URL von `http://gpu-vm:8000` auf
`http://gpu-vm:9500` ändern. Der Tap reicht alles unverändert durch und streamt
ohne Pufferung — er misst mit, er greift nicht ein.

Ohne Tap funktioniert alles andere weiter; die UI sagt dann ausdrücklich, dass
die TTFT-Werte aus Histogrammen interpoliert sind.

### 5. Die beiden KI-gebauten Proxies anbinden

Für den OpenTerminal-Autoscaler und den SearXNG-Privacy-Proxy liegen fertige
Prompts bereit, die man dem Modell vorlegt, das sie gebaut hat:

- [`docs/prompts/openterminal-autoscaler.md`](docs/prompts/openterminal-autoscaler.md)
- [`docs/prompts/searxng-privacy-proxy.md`](docs/prompts/searxng-privacy-proxy.md)

Platzhalter oben ersetzen, kompletten Text kopieren. Die Prompts enthalten das
JSON-Schema, die Feldbedeutungen, das erwartete Fehlerverhalten und — beim
Privacy-Proxy — die Datenschutzregeln, die nicht verhandelbar sind.

Die zugrundeliegende Spezifikation steht in
[`docs/integration-contract.md`](docs/integration-contract.md) und ist auch
direkt vom Server abrufbar: `GET /api/v1/contract`.

---

## Ohne Hardware ausprobieren

```bash
make up                                     # Stack starten
AIMON_TOKEN=<token> MINUTES=60 PROBLEMS=1 make simulate
```

Das erzeugt einen kompletten Stack aus vier Maschinen mit eingebauten Problemen
(heiße GPU mit Throttling, TTFT-Spitzen, volle Platte, erschöpfter Autoscaler,
zu scharfer Privacy-Filter, unerreichbarer Dienst). Danach im Dashboard unter
„Diagnose" einen Fehlerbericht erzeugen.

Für den Agent gibt es Fake-Backends:

```bash
make fake-stack          # vLLM, Ollama, ComfyUI, OpenWebUI auf localhost
aimon-agent discover
```

---

## Neue Server, neue Dienste, neue Metriken

**Server:** Agent installieren, fertig. Der Server legt Maschine und Dienste
selbst an.

**Dienst auf ungewöhnlichem Port:** in `/etc/aimon/agent.yaml` unter `targets:`
eintragen. Was dort steht, gewinnt gegen die automatische Erkennung.

**Eigener Dienst:** an `POST /api/v1/ingest/batch` schicken, was du hast.
Metriken brauchen **keine Migration** — eine neue Metrik erscheint beim ersten
Eintreffen im Explorer und ist sofort abfragbar. Eine unbekannte Dienstart wird
zu `other` statt den Batch abzulehnen.

**Unbekannte Felder** im JSON werden ignoriert und in `response.warnings`
genannt, nicht als Fehler behandelt — eine neuere Integration bricht nicht an
einem älteren Server.

---

## Was ehrlich gemessen wird und was nicht

Das ist der Teil, auf den bei diesem Projekt am meisten Arbeit entfallen ist.

| Zahl | Herkunft | Verlässlichkeit |
|---|---|---|
| GPU-Werte | `nvidia-smi` | exakt, Felder die der Treiber nicht liefert werden weggelassen statt als 0 gemeldet |
| TTFT mit `aimon-tap` | pro Request im Datenpfad gemessen | exakt, nach Modell und Client aufgelöst |
| TTFT ohne Tap | vLLM-Histogramme, serverseitig interpoliert | begrenzt durch die Bucket-Grenzen, **nicht** nach Modell auflösbar — die API gibt `source: "histogram"` zurück |
| TTFT bei Ollama ohne Tap | synthetischer 1-Token-Request | misst die Bereitschaft des Modells, **nicht** die Benutzerlatenz; die UI schreibt das dazu |
| Prompt-Länge | Tap (exakt) und/oder vLLM-Zähler (alle Requests) | beide werden getrennt ausgewiesen |
| OpenTerminal-Headroom | gerechnet aus den Zahlen des Autoscalers | fehlt `instances_max`, wird **gar nichts** angezeigt statt geraten |
| „Node offline" | `last_seen` am Server | ein stummer Agent erscheint als `stale`, nicht als Host mit 0 % Last |

Die Query-API nennt bei jeder Antwort ihre Quelle (`raw`, `rollup_60`,
`rollup_3600`, `requests`, `histogram`). Fehlende Daten erscheinen als `null`
mit Begründung, nie als `0`.

---

## Datenschutz

Der Privacy-Proxy entfernt personenbezogene Daten aus Suchanfragen. Ein
schlecht gebautes Monitoring würde genau diese Daten einsammeln und anzeigen.
Deshalb:

- Standardmäßig wird nur die **bereinigte** Anfrage gespeichert
  (`wohnort von [NAME]`), plus Kategorie, Regel, Länge und ein Hash.
- Rohtext wird nur übertragen und gespeichert, wenn **beide** Seiten das
  ausdrücklich erlauben (`AIMON_SEND_RAW_QUERIES` im Proxy **und**
  `AIMON_ALLOW_RAW_QUERIES=true` am Server). Dann landet er in einer eigenen
  Tabelle mit kürzerer Aufbewahrung (Default 2 Tage) und das Dashboard
  markiert ihn.
- Wird Rohtext ohne Freigabe geschickt, verwirft der Server ihn, vermerkt das
  im Ereignis und warnt den Sender in der Antwort.
- `client_id` ist ein Pseudonym, keine IP und kein Benutzername.
- Der **Fehlerbericht** kann Hostnamen, IPs und Dienstnamen durch stabile
  Platzhalter ersetzen. Die Zuordnungstabelle steht **nicht** im kopierbaren
  Text, sondern getrennt daneben.

Die Lese-API ist ohne `AIMON_READ_TOKEN` offen. Das ist eine bewusste
Entscheidung für den Einzelbetrieb — wer sie nicht will, setzt das Token oder
stellt einen Reverse-Proxy mit Authentifizierung davor.

---

## Entwicklung

```bash
make test            # Go (Race-Detector) + Python
make lint            # go vet + gofmt
make dev             # Server lokal mit Reload
make agent           # Agent-Binary bauen
```

Die Python-Tests, die eine Datenbank brauchen, werden ohne
`AIMON_TEST_DATABASE_URL` übersprungen:

```bash
createdb aimon_test
AIMON_TEST_DATABASE_URL=postgresql://localhost/aimon_test make test-py
```

### Aufbau des Repos

```
server/      FastAPI, eigene Zeitreihen-Schicht, Regelwerk, Prompt-Generator
  aimon/tsdb/        Schreibpfad, Lesepfad, Rollups, Retention
  aimon/routers/     HTTP-Endpunkte
  aimon/diagnostics/ Regeln und Fehlerbericht
  aimon/integrations/Umsetzer für Scaler und Privacy-Proxy
  migrations/        SQL, idempotent
  tests/             84 Tests
agent/       Go, statisch, ohne cgo
  internal/collector/ nvidia-smi, /proc, vLLM-/metrics, Ollama, ComfyUI, Probes
  internal/tap/       Reverse-Proxy im Inference-Pfad
  internal/shipper/   Push mit Ringpuffer und Backoff
web/         Vanilla JS, eigener SVG-Chart-Renderer, kein Build-Schritt
deploy/      docker-compose, systemd-Units, install.sh
docs/        Architektur, Ingest-Contract, Metrik-Referenz, Integrations-Prompts
tools/       Simulator und Fake-Backends zum Testen ohne Hardware
```

### Warum kein Prometheus

Pull braucht eine zentrale Target-Liste; jeder neue Server wäre eine
Konfigänderung. Push mit Self-Registration passt besser zu wechselnden GPU-VMs
hinter NAT. Dazu kommen der Dienstgraph, die Headroom-Rechnung und der
Prompt-Generator, die ohnehin eigener Code wären. Die Zeitreihen-Schicht ist
bewusst klein: ein Series-Dictionary, eine Sample-Tabelle, zwei Rollup-Stufen.

Timescale ist **optional**. Ohne die Extension läuft alles auf normalem
PostgreSQL 15+, nur ohne Kompression und Chunk-Drop.

## Nicht-Ziele

- Kein Log-Aggregator. Ereignisse ja, Volltext-Logs nein.
- Kein Alerting-Router mit Eskalationsketten.
- Keine Benutzerverwaltung.
