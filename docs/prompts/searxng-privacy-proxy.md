# Prompt: SearXNG-Privacy-Proxy an aimonitor anbinden

Diesen Text in die Unterhaltung mit dem Modell kopieren, das den Privacy-Proxy
gebaut hat. Davor die Platzhalter in Abschnitt 0 ersetzen.

**Lies Abschnitt 4 zuerst.** Dieser Proxy existiert, um personenbezogene Daten
aus Suchanfragen zu entfernen. Eine schlecht gebaute Monitoring-Anbindung würde
genau diese Daten in eine Datenbank schreiben und in einem Dashboard anzeigen —
das Gegenteil des Zwecks.

---

## 0. Was du vor dem Kopieren eintragen musst

| Platzhalter | Dein Wert |
|---|---|
| `<AIMON_URL>` | z. B. `http://aimon.lan:8420` |
| `<AIMON_TOKEN>` | Token mit Scope `ingest` |
| `<NODE_NAME>` | Maschine, auf der der Proxy läuft, z. B. `proxy-host-01` |
| `<SERVICE_KEY>` | z. B. `privacy-proxy@proxy-host-01:8888` |

Token anlegen:

```bash
curl -s -X POST "<AIMON_URL>/api/v1/tokens" \
  -H "Authorization: Bearer $AIMON_BOOTSTRAP_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"privacy-proxy","scopes":["ingest"]}'
```

---

## 1. Aufgabe

Baue in den Privacy-Proxy vor SearXNG eine Telemetrie-Anbindung an ein
Monitoring namens **aimonitor** ein. Gemeldet werden sollen:

- alle 10 Sekunden ein Snapshot der **kumulativen Zähler**,
- jede **Eingriffs-Entscheidung** einzeln (geblockt, bereinigt, Fehler),
- **keine** durchgelassenen Anfragen einzeln (dafür sind die Zähler da).

**Der Datenpfad darf dadurch nicht langsamer oder unzuverlässiger werden.**
Das Senden passiert asynchron, nie im Request-Pfad.

## 2. Die Schnittstelle

`POST <AIMON_URL>/api/v1/ingest/privacy`

Header:
```
Content-Type: application/json
Authorization: Bearer <AIMON_TOKEN>
```

```json
{
  "node":    { "name": "<NODE_NAME>" },
  "service": { "key": "<SERVICE_KEY>",
               "kind": "privacy_proxy",
               "name": "SearXNG Privacy Proxy",
               "base_url": "http://<NODE_NAME>:8888",
               "version": "<deine Version>" },
  "ts": 1793707200,

  "counters": {
    "requests_total":        18422,
    "allowed_total":         17980,
    "blocked_total":           388,
    "redacted_total":           54,
    "errors_total":             11,
    "upstream_errors_total":     3,
    "blocked_by_category":  { "person_name": 301, "email": 44, "phone": 21,
                              "address": 15, "id_number": 7 },
    "redacted_by_category": { "email": 38, "phone": 16 },
    "upstream_latency_ms_sum":   412880.0,
    "upstream_latency_ms_count":  18411,
    "query_len_sum":             538120,
    "query_len_count":            18422
  },

  "decisions": [
    {
      "ts": 1793707198.8,
      "action": "blocked",
      "category": "person_name",
      "rule": "ner.person.v2",
      "confidence": 0.93,
      "query_redacted": "wohnort von [NAME]",
      "query_sha256": "9f2b8c…",
      "query_len": 21,
      "matched_spans": [{ "start": 10, "end": 16, "type": "PERSON" }],
      "client_id": "u:3f1a",
      "upstream_latency_ms": null,
      "upstream_status": null
    }
  ]
}
```

### Die Zähler

**Kumulativ und monoton steigend**, seit Prozessstart. Keine Deltas senden —
aimonitor rechnet die Raten selbst und erkennt Neustarts (Wert springt zurück)
von allein. Wer Deltas schickt, bekommt falsche Raten.

| Zähler | Bedeutung |
|---|---|
| `requests_total` | Alle Anfragen, die durch den Proxy gingen |
| `allowed_total` | Unverändert durchgelassen |
| `blocked_total` | Gar nicht an SearXNG weitergegeben |
| `redacted_total` | Weitergegeben, aber mit entfernten/ersetzten Teilen |
| `errors_total` | Fehler **im Proxy selbst** (Filter abgestürzt, Timeout in der Erkennung) |
| `upstream_errors_total` | Fehler **von SearXNG** (5xx, 429, Timeout, Captcha-Wall) |
| `*_by_category` | Aufschlüsselung nach Kategorie. Die Summe über alle Kategorien sollte dem Gesamtzähler entsprechen. |
| `upstream_latency_ms_sum` / `_count` | Summe und Anzahl, nicht der Durchschnitt — daraus bildet aimonitor den gleitenden Mittelwert über jedes Zeitfenster. |
| `query_len_sum` / `_count` | Dasselbe für die Anfragelänge in Zeichen. |

Es gilt: `allowed_total + blocked_total + redacted_total + errors_total ≈ requests_total`.
Wenn das bei dir nicht aufgeht, erkläre mir warum, statt die Zahlen anzupassen.

### Die Entscheidungen

`action` ist eines von `allowed` | `redacted` | `blocked` | `error`.

| Feld | Pflicht | Bedeutung |
|---|---|---|
| `action` | ja | siehe oben |
| `category` | bei blocked/redacted | Was gefunden wurde: `person_name`, `email`, `phone`, `address`, `id_number`, `credit_card`, … Eigene Kategorien sind erlaubt. |
| `rule` | empfohlen | **Welche Regel** gefeuert hat, z. B. `ner.person.v2` oder `regex.email`. Das ist das wichtigste Feld zur Unterscheidung „zu scharfer Filter" von „jemand sucht wirklich nach Personen": ein einzelnes Regel-Label mit fast allen Treffern heißt False Positives. |
| `confidence` | wenn vorhanden | 0…1 bei Modell-basierter Erkennung |
| `query_redacted` | siehe Abschnitt 4 | Die Anfrage **nach** dem Filtern |
| `query_sha256` | empfohlen | Hash der **Original**anfrage. Damit erkenne ich wiederkehrende Anfragen, ohne den Text zu kennen. |
| `query_len` | empfohlen | Länge der Originalanfrage in Zeichen |
| `matched_spans` | optional | Nur Offsets und Typ, **nie den Treffertext** |
| `client_id` | optional | Pseudonym, siehe Abschnitt 4 |
| `upstream_latency_ms`, `upstream_status` | wenn weitergegeben | |

`allowed`-Entscheidungen bitte **nicht** einzeln senden. Bei normaler Last sind
das tausende Einträge pro Stunde und das Log wird unlesbar. Die Zähler decken
das ab.

## 3. Fehler von SearXNG

Wenn SearXNG selbst Probleme macht, soll das als Entscheidung mit
`"action": "error"` kommen, plus `upstream_status`:

```json
{ "ts": 1793707200, "action": "error", "category": "upstream",
  "rule": "upstream.searxng", "upstream_status": 429,
  "upstream_latency_ms": 5000.0 }
```

429 von SearXNG heißt in der Praxis fast immer: eine der Suchmaschinen hat das
Rate-Limit zugemacht oder zeigt ein Captcha. Das will ich im Dashboard sehen,
weil Benutzer dann leere Ergebnisse bekommen und denken, der Proxy sei kaputt.

## 4. Datenschutz — das ist nicht verhandelbar

Dieser Proxy entfernt personenbezogene Daten. Die Telemetrie darf sie nicht
durch die Hintertür wieder einsammeln.

**Regeln:**

1. **`query_raw` wird standardmäßig nicht gesendet.** Baue das Feld ein, aber
   nur hinter einem Schalter, der per Default **aus** ist
   (`AIMON_SEND_RAW_QUERIES=false`). Der Server nimmt es ohnehin nur an, wenn er
   selbst mit `AIMON_ALLOW_RAW_QUERIES=true` läuft; sonst verwirft er es und
   notiert das im Ereignis. Beide Schalter müssen an sein — eine bewusste Hürde.

2. **`query_redacted` ist die Fassung nach dem Filter.** Sie landet im
   Dashboard und muss dort gefahrlos lesbar sein. Konkret: ersetze erkannte
   Treffer durch Marker wie `[NAME]`, `[EMAIL]`, `[PHONE]`, `[ADDRESS]` —
   nicht durch Sterne, die die Länge verraten, und nicht teilweise
   (`Max M***` ist keine Anonymisierung).

   **Wenn du nicht garantieren kannst, dass `query_redacted` frei von
   personenbezogenen Daten ist, lass das Feld weg.** Dann reichen `category`,
   `rule`, `query_sha256` und `query_len`. Eine Anfrage mit zwei Namen, von
   denen die Erkennung nur einen findet, wäre nach dem Ersetzen immer noch
   personenbezogen — in dem Fall ist Weglassen die richtige Antwort.

3. **`query_sha256` über die Originalanfrage**, nicht über die bereinigte.
   Damit erkenne ich, dass dieselbe Anfrage hundertmal kam, ohne sie zu kennen.
   Hinweis zur Ehrlichkeit: ein Hash einer kurzen Suchanfrage ist durch
   Durchprobieren angreifbar. Er ist ein Wiedererkennungsmerkmal, keine
   Anonymisierung. Wenn dir das zu viel ist, lass ihn weg.

4. **`client_id` ist ein Pseudonym**, kein Identifikator: HMAC der Session-ID
   mit einem Schlüssel, der nur im Proxy liegt und beim Neustart neu erzeugt
   wird. **Keine IP-Adresse, kein Benutzername, kein Cookie-Wert.** Wenn du
   keinen solchen Schlüssel hast, lass das Feld weg.

5. **`matched_spans` enthält Offsets und Typ, nie den Treffertext.** `{"start":
   10, "end": 16, "type": "PERSON"}` ist in Ordnung, `{"text": "Müller"}` nicht.

6. **Nichts in die lokalen Logs schreiben, was nicht gesendet wird.** Wenn der
   Rohtext nicht ans Monitoring geht, gehört er auch nicht in die Logdatei des
   Proxys.

## 5. Verhalten, das ich erwarte

1. **Nie blockieren.** Entscheidungen in eine Queue legen, ein eigener Worker
   sendet. Der Request-Pfad wartet nie auf HTTP.
2. **Queue begrenzen** (z. B. 5000 Einträge). Läuft sie über, die ältesten
   verwerfen und einen Zähler `dropped_total` mitzählen — den will ich sehen,
   er sagt mir, dass ich Zahlen verpasse.
3. **Batchen**: Entscheidungen sammeln und alle 5–10 Sekunden gemeinsam
   schicken, zusammen mit dem Zähler-Snapshot. Nicht ein Request pro
   Entscheidung.
4. **Timeout 5 s**, Backoff 1 → 60 s mit Jitter.
5. **`4xx` nicht wiederholen** (außer `429`), Batch verwerfen und einmal loggen.
6. **Ein Fehler in der Telemetrie darf nie eine Suchanfrage kaputtmachen.**
   Im Zweifel: Telemetrie abschalten, Proxy läuft weiter.
7. **Abschaltbar**: `AIMON_URL` leer = Telemetrie komplett aus.

## 6. Selbsttest, den ich sehen will

```bash
<dein-proxy> --aimon-test
```

sendet einen Beispiel-Payload mit erfundenen, offensichtlich künstlichen Daten
(`"wohnort von [NAME]"`) und gibt die Antwort aus.

Prüfen kann ich es auch so:

```bash
curl -s "<AIMON_URL>/api/v1/ingest/echo" -H "Authorization: Bearer <AIMON_TOKEN>"

curl -s -X POST "<AIMON_URL>/api/v1/ingest/privacy?dry_run=1" \
  -H "Authorization: Bearer <AIMON_TOKEN>" -H 'Content-Type: application/json' \
  -d @payload.json
```

Und im Dashboard: Reiter „SearXNG-Proxy" → die Zähler-Kacheln füllen sich, die
geblockten Anfragen erscheinen im Log.

## 7. Was ich nicht will

- Keine zusätzliche Abhängigkeit nur fürs Senden.
- Keine erfundenen Zahlen. Lieber ein Feld weglassen.
- Keine Volltext-Logs der Suchanfragen, weder gesendet noch lokal.
- Kein Senden im Request-Pfad.
- Keine IP-Adressen, nirgendwo.

## 8. Was du mir am Ende zeigen sollst

1. Die Diff/die neuen Dateien.
2. **Wie `query_redacted` entsteht** — die Stelle im Code, die aus der
   Originalanfrage die bereinigte macht, und warum du sicher bist, dass danach
   nichts Personenbezogenes mehr drinsteht. Wenn du nicht sicher bist, sag es
   und lass das Feld weg.
3. Welcher Schalter den Rohtext freigibt und wo sein Default steht.
4. Wie `client_id` berechnet wird, falls du es sendest.
5. Was passiert, wenn aimonitor zehn Minuten nicht erreichbar ist.
6. Die Ausgabe des Selbsttests aus Abschnitt 6.
