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
- **Zugang zu den Maschinen**: der Agent meldet seine Adressen, das Dashboard
  zeigt den fertigen `ssh`-Befehl zum Kopieren — auch neben dem Fehlerbericht.
- **Benachrichtigung** per Webhook oder E-Mail, bewusst zurückhaltend:
  nur bei kritischen Incidents, die eine Weile bestehen, einmal pro Vorfall.
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

> **Wo läuft was?** `make`, `git` und `docker compose` laufen **nur auf dem
> Monitoring-Host**, im Wurzelverzeichnis des Repos (`/opt/Aimonitor`). Auf den
> GPU-Maschinen gibt es kein Repo und kein `make` — dort wird nur die fertige
> Agent-Binary installiert.

### 2. Agent-Binaries bauen und ausliefern

Auf dem **Monitoring-Host**, im Wurzelverzeichnis des Repos (nicht in
`deploy/`):

```bash
cd /opt/Aimonitor

make agent-docker      # baut im Container, kein Go auf dem Host nötig
# oder, wenn Go >= 1.22 installiert ist:
make agent-all

cd deploy && sudo docker compose restart server
```

Die Binaries landen in `web/dist/` und werden vom Server unter
`/static/dist/` ausgeliefert. Prüfen:

```bash
curl -sI http://localhost:8420/static/dist/install.sh | head -1   # 200 OK
```

### 3. Token für den Agent anlegen

Ebenfalls im Wurzelverzeichnis:

```bash
cd /opt/Aimonitor
export AIMON_URL=http://localhost:8420
export AIMON_BOOTSTRAP_TOKEN=$(grep ^AIMON_BOOTSTRAP_TOKEN deploy/.env | cut -d= -f2-)

make token NAME=agent-gpu-vm-01
```

Ausgabe:

```json
{"id": 1, "name": "agent-gpu-vm-01", "scopes": ["ingest"],
 "token": "aimon_NGvHHy4CWsGcuf0N8xxCaQEhLV3zRHWhPwLpAFihBJ8", ...}
```

Das Token wird **nur hier einmal ausgegeben**; gespeichert ist nur sein Hash.
Pro Maschine ein eigenes Token anlegen — dann lässt sich eines widerrufen,
ohne die anderen zu treffen (`DELETE /api/v1/tokens/<id>`).

### 4. Agent auf der GPU-VM installieren

Auf der **GPU-VM**, nicht auf dem Monitoring-Host:

```bash
curl -fsSL http://<monitoring-host>:8420/static/dist/install.sh | \
  sudo AIMON_URL=http://<monitoring-host>:8420 \
       AIMON_TOKEN=aimon_NGvHHy4... \
       AIMON_LABELS=site=rack-a,role=inference \
       sh
```

Erreicht die GPU-VM den Monitoring-Host nicht per HTTP, Binary und Skript
kopieren:

```bash
scp web/dist/aimon-agent-linux-amd64 deploy/agent/install.sh gpu-vm-01:/tmp/
ssh gpu-vm-01 'cd /tmp && mv aimon-agent-linux-amd64 aimon-agent && \
  sudo AIMON_URL=http://<host>:8420 AIMON_TOKEN=aimon_... sh install.sh'
```

Das Skript installiert die Binary nach `/usr/local/bin/aimon-agent`, schreibt
`/etc/aimon/agent.yaml` (Modus 0600, enthält das Token), führt einen
**Selbsttest** aus und startet den systemd-Dienst erst, wenn der durchläuft.

Lokal laufende Dienste findet der Agent selbst: vLLM (8000/8001/8080), Ollama
(11434), ComfyUI (8188), OpenWebUI (3000/8080), SearXNG (8888/8080). Jeweils
mit Fingerabdruck auf dem Endpunkt — ein offener Port allein zählt nicht.
Zentral ist nichts einzutragen.

Prüfen:

```bash
aimon-agent check              # Token, GPU, Host, Dienste, Peers
aimon-agent discover           # gefundene Dienste als YAML
systemctl status aimon-agent
journalctl -u aimon-agent -f
curl -s localhost:9100/healthz # Zähler des Agents
```

Nach ein bis zwei Minuten muss die Maschine im Dashboard unter „Explorer"
auftauchen.

Dienste auf anderen Ports, Labels oder Peer-Probes gehören in
`/etc/aimon/agent.yaml` — die Vorlage mit allen Optionen steht in
[`deploy/agent/agent.example.yaml`](deploy/agent/agent.example.yaml). Nach
Änderungen: `sudo systemctl restart aimon-agent`.

### 5. Echte TTFT messen (optional, aber der eigentliche Punkt)

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

### 6. Die beiden KI-gebauten Proxies anbinden

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

## Benachrichtigungen

Standardmäßig aus. Eingeschaltet meldet sich aimonitor **nur** bei Incidents ab
einer Mindestschwere (Default `critical`), die länger als eine Mindestdauer
bestehen (Default 5 Minuten), genau einmal je Incident plus einer Entwarnung,
und höchstens zehnmal pro Stunde über alles.

Das ist Absicht. Eine Benachrichtigung, die zu oft kommt, wird nach einer Woche
weggefiltert — und fehlt dann an dem Tag, an dem sie wichtig gewesen wäre.
Mit `AIMON_ALERT_MIN_SEVERITY=warning` bekommst du bei einem normalen Stack
mehrere Meldungen am Tag; das ist selten gewollt.

**Webhook** ist der bequemere Weg — kein SMTP-Passwort, kommt direkt aufs Handy.
Funktioniert mit ntfy, Gotify, Discord, Slack, Home Assistant:

```bash
# in deploy/.env
AIMON_ALERT_WEBHOOK_URL=https://ntfy.sh/dein-geheimes-topic
AIMON_PUBLIC_URL=http://aimon.lan:8420
```

**E-Mail** über SMTP:

```bash
AIMON_ALERT_SMTP_HOST=smtp.example.net
AIMON_ALERT_SMTP_PORT=587
AIMON_ALERT_SMTP_USER=aimon@example.net
AIMON_ALERT_SMTP_PASSWORD=...
AIMON_ALERT_MAIL_TO=du@example.net
```

Testen, ohne auf einen echten Vorfall zu warten:

```bash
curl -s -X POST localhost:8420/api/v1/alerts/test   -H "Authorization: Bearer $AIMON_BOOTSTRAP_TOKEN" | python3 -m json.tool
```

Was eingerichtet ist und was zuletzt verschickt wurde, steht unter
`GET /api/v1/alerts` und oben auf der Diagnose-Seite.

## Fehlersuche

### Container läuft, Port antwortet nicht

`docker compose ps` zeigt „Running", aber nichts antwortet auf 8420:

```bash
sudo docker compose ps                  # Status und Health beider Container
sudo docker compose logs --tail=50 server
```

Häufigste Ursachen:

| Logzeile | Bedeutung |
|---|---|
| `Datenbank noch nicht bereit …` | normal beim Start, der Server wartet bis zu 120 s |
| `Datenbank nach 120s nicht erreichbar` | Der Dienst `db` läuft nicht — `docker compose ps` |
| `Falsches Datenbank-Passwort` | `POSTGRES_PASSWORD` wurde nach der ersten Initialisierung geändert. PostgreSQL behält das alte Passwort im Volume. |
| `Die Datenbank-Adresse ist nicht lesbar` | Eine selbst gesetzte `AIMON_DATABASE_URL` mit Sonderzeichen im Passwort. Stattdessen `AIMON_DB_*` benutzen. |
| `Address already in use` | Port 8420 ist belegt, `AIMON_PORT` ändern |
| Container `Restarting` | Startfehler, siehe Logzeilen darüber |

**Läuft im Container überhaupt der Code, den du erwartest?** Die erste
Logzeile des Servers nennt Version und Datenbank-Adresse (ohne Passwort):

```
aimon-server 0.1.0, Datenbank postgresql://aimon:***@db:5432/aimon
```

Fehlt diese Zeile und der Fehler sieht aus wie vorher, ist das Image alt. Das
passiert vor allem, wenn `git pull` nicht durchlief — etwa weil Git das
Verzeichnis einem anderen Benutzer zuordnet:

```bash
git config --global --add safe.directory /opt/Aimonitor
git -C /opt/Aimonitor pull
git -C /opt/Aimonitor log --oneline -1      # sollte den erwarteten Commit zeigen
cd /opt/Aimonitor/deploy && sudo docker compose up -d --build --force-recreate
```

Zeigt der Build überall `CACHED`, hat sich am Quelltext nichts geändert.

Hat Compose beim Hochfahren nur „Running" statt „Recreated"/„Started" gemeldet,
läuft noch der alte Container — dann neu erzeugen:

```bash
sudo docker compose up -d --build --force-recreate
```

Passwort im Volume und `.env` auseinandergelaufen? Nur zurücksetzen, wenn die
Daten egal sind — **das löscht alle Messwerte**:

```bash
sudo docker compose down -v && sudo docker compose up -d --build
```

### Agent meldet sich nicht

Auf der GPU-VM:

```bash
aimon-agent check              # sagt genau, woran es hängt
journalctl -u aimon-agent -n 50
```

| Befund | Bedeutung |
|---|---|
| `FEHLER: ... connection refused` | Die GPU-VM erreicht den Monitoring-Host nicht. Firewall, `AIMON_URL` in `/etc/aimon/agent.yaml`. |
| `HTTP 401` | Token falsch oder widerrufen. Neues anlegen mit `make token`. |
| `HTTP 403` | Token hat keinen `ingest`-Scope. |
| `0 Samples (nvidia-smi: ...)` | Kein NVIDIA-Treiber oder keine Rechte. `nvidia-smi` von Hand ausführen. |
| `0 Dienste` | Nichts auf den bekannten Ports gefunden. `aimon-agent discover` und ggf. `targets:` in die Konfiguration. |
| `429` im Journal | Ratelimit. `AIMON_RATE_LIMIT_PER_MIN` in `deploy/.env` erhöhen. |

Die Maschine ist im Dashboard da, zeigt aber keine Werte? Dann prüfen, ob der
Agent noch sendet: `curl -s localhost:9100/healthz` auf der GPU-VM. Steigt
`buffered` dauerhaft, kommen die Batches nicht durch.

### GPU ohne Speicherangabe

Auf Systemen mit gemeinsamem Speicher (Grace Blackwell / DGX Spark) liefert
`nvidia-smi --query-gpu=memory.total` ein `N/A`. Der Agent fällt dann auf
`nvidia-smi -q -d MEMORY` zurück und kennzeichnet die Werte als `unified` —
„frei" bedeutet dort etwas anderes als bei einer Karte mit eigenem VRAM.

Bleibt die Anzeige leer, zeigt dieser Befehl auf der betroffenen Maschine
Rohausgabe und Auswertung nebeneinander:

```bash
aimon-agent gpudump
```

Erkennt der Agent dort nichts, ist die Ausgabe das, was ich brauche, um den
Parser anzupassen. Bei gemeinsamem Speicher ist ohnehin der Arbeitsspeicher
des Hosts die eigentliche Grenze — siehe `host.mem_used_bytes` im Explorer.

### „GPU meldet Power-Limit bei nur 0 % Last“

Ein Hinweis, keine Warnung. Eine Karte, die **unter Last** an ihrem
Leistungslimit arbeitet, tut genau das, wofür sie gebaut ist — eine L4 mit 70
von 72 W bei 100 % Auslastung ist der Normalzustand. Gemeldet wird deshalb nur
der umgekehrte Fall: das Limit greift, obwohl die Karte kaum rechnet.

Mögliche Ursachen: ein zu eng gesetztes Limit (`nvidia-smi -q -d POWER`),
schwache oder geteilte Stromversorgung, oder der Treiber meldet das Flag
dauerhaft. Bei Modulen mit gemeinsamem Leistungsbudget ist es im Leerlauf
schlicht normal; dann die Schwelle anheben:

```bash
AIMON_POWER_CAP_IDLE_UTIL_PCT=0   # Hinweis ganz abschalten
```

### `make: *** No rule to make target`

`make` muss im Wurzelverzeichnis des Repos laufen (`/opt/Aimonitor`), nicht in
`deploy/`.

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

### Datenbank-Zugangsdaten

Der Server baut die Verbindungsadresse aus Einzelteilen und kodiert sie selbst:

```
AIMON_DB_HOST=db
AIMON_DB_PORT=5432
AIMON_DB_USER=aimon
AIMON_DB_PASSWORD=<beliebig, auch mit / @ : # %>
AIMON_DB_NAME=aimon
```

Im Compose-Setup werden die aus `POSTGRES_*` in `.env` gefüllt; eintragen muss
man nur `POSTGRES_PASSWORD`.

`AIMON_DATABASE_URL` wird weiterhin akzeptiert und hat Vorrang — dann ist man
selbst für die Kodierung zuständig. **Ein `/` im Passwort zerlegt eine von Hand
gebaute `postgresql://`-URL**, und `openssl rand -base64 32` erzeugt in gut der
Hälfte der Fälle eins. Deshalb ist der Weg über die Einzelteile der Default.

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
