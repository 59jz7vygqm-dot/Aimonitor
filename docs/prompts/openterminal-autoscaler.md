# Prompt: OpenTerminal-Autoscaler an aimonitor anbinden

Diesen Text in die Unterhaltung mit dem Modell kopieren, das den Autoscaler
gebaut hat. Danach die Platzhalter in Abschnitt 0 ersetzen.

---

## 0. Was du vor dem Kopieren eintragen musst

| Platzhalter | Dein Wert |
|---|---|
| `<AIMON_URL>` | z. B. `http://aimon.lan:8420` |
| `<AIMON_TOKEN>` | Token mit Scope `ingest`, siehe unten |
| `<NODE_NAME>` | Name der Maschine, auf der der Proxy läuft, z. B. `proxy-host-01` |
| `<SERVICE_KEY>` | z. B. `openterminal-proxy@proxy-host-01:7681` |

Token anlegen:

```bash
curl -s -X POST "<AIMON_URL>/api/v1/tokens" \
  -H "Authorization: Bearer $AIMON_BOOTSTRAP_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"openterminal-proxy","scopes":["ingest"]}'
```

---

## 1. Aufgabe

Baue in den OpenTerminal-Autoscaling-Proxy eine Telemetrie-Anbindung an ein
Monitoring namens **aimonitor** ein. Der Proxy soll alle 10 Sekunden seinen
Zustand an eine HTTP-Schnittstelle melden und zusätzlich jedes Scale-Ereignis
einzeln.

**Der Datenpfad darf dadurch nicht langsamer oder unzuverlässiger werden.**
Das ist die wichtigste Anforderung. Alles Weitere ist nachrangig.

## 2. Die Schnittstelle

`POST <AIMON_URL>/api/v1/ingest/scaler`

Header:
```
Content-Type: application/json
Authorization: Bearer <AIMON_TOKEN>
```

Body (alle Felder außer `node` und `service` sind optional; lass weg, was du
nicht sicher weißt — **rate nichts**):

```json
{
  "node":    { "name": "<NODE_NAME>" },
  "service": { "key": "<SERVICE_KEY>",
               "kind": "openterminal_proxy",
               "name": "OpenTerminal Autoscaler",
               "base_url": "http://<NODE_NAME>:7681",
               "version": "<deine Version>" },
  "ts": 1793707200,
  "state": {
    "instances_running":  4,
    "instances_desired":  4,
    "instances_min":      1,
    "instances_max":     12,
    "instances_starting": 0,
    "instances_draining": 1,
    "sessions_active":   27,
    "sessions_per_instance_target": 10,
    "sessions_queued":    0,
    "cpu_pct":           46.0,
    "mem_used_bytes":    7516192768,
    "mem_total_bytes":   33285996544,
    "scale_cooldown_s_remaining": 42,
    "last_scale_reason": "sessions_per_instance>8 for 90s"
  },
  "instances": [
    { "id": "ot-7f3a", "state": "running", "sessions": 9,
      "started_at": 1793705000, "cpu_pct": 51.0,
      "mem_used_bytes": 1932735283, "host": "<NODE_NAME>", "port": 7701 }
  ],
  "events": []
}
```

Antwort ist `202 Accepted` mit `{"accepted": n, "rejected": n, "errors": []}`.

### Feldbedeutungen (wichtig, hier wird oft falsch gemappt)

| Feld | Bedeutung |
|---|---|
| `instances_running` | Instanzen, die **jetzt Sessions bedienen können**. Startende und drainende zählen hier **nicht** mit. |
| `instances_desired` | Was deine Scale-Logik gerade will. Weicht das dauerhaft von `running` ab, heißt das: Instanzen starten nicht oder stürzen beim Start ab — das Monitoring macht daraus einen eigenen Incident. |
| `instances_max` | Die harte Obergrenze deiner Konfiguration. Daraus berechnet aimonitor den Headroom. **Ohne dieses Feld gibt es keine Headroom-Anzeige** — es wird dann bewusst gar nichts angezeigt statt einer geratenen Zahl. |
| `sessions_per_instance_target` | Wie viele Sessions du pro Instanz anstrebst. Zusammen mit `instances_max` ergibt das die Gesamtkapazität. |
| `sessions_queued` | Sessions, die auf eine freie Instanz warten. `0`, wenn du nicht queust. |
| `scale_cooldown_s_remaining` | Sekunden, bis wieder skaliert werden darf. `0` wenn kein Cooldown läuft. |
| `instances[].state` | `starting` \| `running` \| `draining` \| `unhealthy` |
| `ts` | Unix-Sekunden oder RFC3339. Weglassen = Empfangszeit des Servers. |

Aus diesen Feldern rechnet aimonitor selbst:

```
capacity_sessions = instances_max * sessions_per_instance_target
headroom_sessions = capacity_sessions - sessions_active
headroom_pct      = headroom_sessions / capacity_sessions * 100
saturation_pct    = sessions_active / (instances_running * sessions_per_instance_target) * 100
```

Rechne das **nicht selbst** und schicke es nicht mit — doppelte Berechnung an
zwei Stellen geht irgendwann auseinander.

## 3. Scale-Ereignisse

Jeden Scale-Vorgang **zusätzlich** als Ereignis melden, im selben Request unter
`events` oder einzeln an `POST <AIMON_URL>/api/v1/ingest/batch`:

```json
{
  "ts": 1793707200,
  "kind": "scale.up",
  "severity": "info",
  "title": "Scale up: 3 -> 4 Instanzen",
  "service": "<SERVICE_KEY>",
  "detail": { "from": 3, "to": 4,
              "reason": "sessions_per_instance=9.3 for 90s",
              "trigger_metric": "sessions_per_instance",
              "trigger_value": 9.3 }
}
```

`kind` ist eines von:

| kind | wann |
|---|---|
| `scale.up` | Instanz(en) gestartet |
| `scale.down` | Instanz(en) gestoppt |
| `scale.blocked` | Du wolltest hochskalieren, **durftest aber nicht** (Obergrenze erreicht, Cooldown aktiv, kein Kapazität auf dem Host). `severity: "warning"`. **Das ist das wichtigste Ereignis überhaupt** — es erklärt, warum Benutzer warten, obwohl der Scaler „läuft". |
| `scale.failed` | Start einer Instanz ist fehlgeschlagen. `severity: "error"`, `detail.error` mit der Fehlerklasse (kein Stacktrace). |
| `instance.unhealthy` | Eine laufende Instanz antwortet nicht mehr. |

Bei `scale.blocked` zusätzlich `"dedup_key": "scale.blocked:max"` setzen, sonst
füllt sich das Log bei anhaltender Überlast mit identischen Zeilen. Der Server
zählt dann innerhalb von 60 Sekunden nur hoch.

## 4. Verhalten, das ich erwarte

1. **Nie blockieren.** Das Senden läuft in einer eigenen Goroutine / einem
   eigenen Task / Thread. Die Scale-Logik wartet nie auf die HTTP-Antwort.
2. **Timeout 5 Sekunden**, danach aufgeben.
3. **Puffer**, wenn aimonitor nicht erreichbar ist: Ringpuffer für ungefähr
   10 Minuten Daten, die ältesten fallen raus. Speicherverbrauch muss nach oben
   begrenzt sein.
4. **Backoff**: nach einem Fehlschlag 1 s, dann 2, 4, 8 … bis maximal 60 s, mit
   etwas Jitter.
5. **`4xx` nicht wiederholen** (außer `429`). Ein `400` heißt, das Schema passt
   nicht — dann den Batch verwerfen und das lokal **einmal** loggen, nicht bei
   jedem Versuch.
6. **`429`** respektieren: `Retry-After` auswerten.
7. **Ein Fehler im Monitoring darf den Proxy nie zum Absturz bringen.** Alles in
   try/catch bzw. mit Fehlerrückgabe, niemals panic/unhandled rejection.
8. **Schaltbar**: eine Umgebungsvariable `AIMON_URL`; ist sie leer, ist die
   ganze Telemetrie aus und es wird nichts zusätzlich ausgeführt.

## 5. Selbsttest, den ich sehen will

Baue einen Modus ein, der einen Beispiel-Payload einmalig sendet und die
Antwort ausgibt — damit ich die Anbindung prüfen kann, ohne auf echte Last zu
warten:

```bash
<dein-proxy> --aimon-test
```

Erwartete Ausgabe bei Erfolg:
```
POST <AIMON_URL>/api/v1/ingest/scaler -> 202 {"accepted": 17, "rejected": 0, "errors": []}
```

Prüfen kann ich es außerdem so (ohne Daten zu schreiben):

```bash
# Token gültig?
curl -s "<AIMON_URL>/api/v1/ingest/echo" -H "Authorization: Bearer <AIMON_TOKEN>"

# Payload validieren, ohne zu speichern:
curl -s -X POST "<AIMON_URL>/api/v1/ingest/scaler?dry_run=1" \
  -H "Authorization: Bearer <AIMON_TOKEN>" -H 'Content-Type: application/json' \
  -d @payload.json
```

`dry_run=1` meldet jeden Validierungsfehler mit JSON-Pointer zurück.

## 6. Was ich nicht will

- Keine zusätzliche Abhängigkeit nur fürs Senden. Der HTTP-Client der
  Standardbibliothek reicht.
- Keine Metriken, die du nicht wirklich misst. Lieber ein Feld weglassen als
  eine plausible Zahl erfinden — aimonitor zeigt fehlende Werte als fehlend an,
  und das ist genau richtig so.
- Keine Session-Inhalte, keine Benutzernamen, keine Kommandozeilen. `instances[].id`
  ist eine technische ID, kein Benutzerbezug.
- Kein eigenes Retry-Thread-Geflecht mit unbegrenztem Speicherwachstum.

## 7. Was du mir am Ende zeigen sollst

1. Die Diff/die neuen Dateien.
2. Wo genau im Code der Snapshot gebaut wird, und aus welchen internen
   Variablen jedes Feld kommt — besonders `instances_running` vs.
   `instances_desired` und `instances_max`.
3. Welche Felder du **nicht** füllen kannst und warum.
4. Die Ausgabe des Selbsttests aus Abschnitt 5.
