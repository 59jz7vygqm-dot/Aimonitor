-- aimonitor core schema. Idempotent: darf mehrfach laufen.

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- Auth
CREATE TABLE IF NOT EXISTS api_tokens (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    token_hash  TEXT NOT NULL UNIQUE,
    scopes      TEXT[] NOT NULL DEFAULT ARRAY['ingest'],
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used   TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS api_tokens_hash_idx ON api_tokens (token_hash) WHERE revoked_at IS NULL;

-- ---------------------------------------------------------------- Inventar
CREATE TABLE IF NOT EXISTS nodes (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    hostname      TEXT,
    agent_version TEXT,
    labels        JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Mit welchem Token diese Maschine zuletzt gemeldet hat. Dient allein der
-- Erkennung von Tokens, die über mehrere Maschinen kopiert wurden - das macht
-- das Widerrufen einzelner Maschinen wirkungslos.
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS token_name TEXT;

CREATE TABLE IF NOT EXISTS services (
    id          BIGSERIAL PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    node_id     BIGINT REFERENCES nodes(id) ON DELETE SET NULL,
    kind        TEXT NOT NULL DEFAULT 'other',
    name        TEXT,
    base_url    TEXT,
    version     TEXT,
    labels      JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS services_node_idx ON services (node_id);
CREATE INDEX IF NOT EXISTS services_kind_idx ON services (kind);

-- ---------------------------------------------------------------- Zeitreihen
-- Series-Dictionary: neue Metriken brauchen keine Migration.
CREATE TABLE IF NOT EXISTS series (
    id          BIGSERIAL PRIMARY KEY,
    metric      TEXT NOT NULL,
    labels_hash TEXT NOT NULL,
    labels      JSONB NOT NULL DEFAULT '{}'::jsonb,
    node_id     BIGINT REFERENCES nodes(id) ON DELETE CASCADE,
    service_id  BIGINT REFERENCES services(id) ON DELETE CASCADE,
    is_counter  BOOLEAN NOT NULL DEFAULT false,
    unit        TEXT,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (metric, labels_hash)
);
CREATE INDEX IF NOT EXISTS series_metric_idx  ON series (metric);
CREATE INDEX IF NOT EXISTS series_node_idx    ON series (node_id);
CREATE INDEX IF NOT EXISTS series_service_idx ON series (service_id);
CREATE INDEX IF NOT EXISTS series_labels_idx  ON series USING gin (labels);

CREATE TABLE IF NOT EXISTS samples (
    ts         TIMESTAMPTZ NOT NULL,
    series_id  BIGINT NOT NULL,
    value      DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_series_ts_idx ON samples (series_id, ts DESC);
CREATE INDEX IF NOT EXISTS samples_ts_idx        ON samples USING brin (ts);

-- Rollups als echte Tabelle (nicht Continuous Aggregate), damit der Code auch
-- auf nacktem PostgreSQL funktioniert.
CREATE TABLE IF NOT EXISTS samples_rollup (
    bucket_s   INTEGER NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,
    series_id  BIGINT NOT NULL,
    n          INTEGER NOT NULL,
    sum        DOUBLE PRECISION NOT NULL,
    min        DOUBLE PRECISION NOT NULL,
    max        DOUBLE PRECISION NOT NULL,
    last       DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (bucket_s, series_id, ts)
);
CREATE INDEX IF NOT EXISTS samples_rollup_lookup_idx
    ON samples_rollup (bucket_s, series_id, ts DESC);

-- Wie weit der Rollup-Worker pro Bucket-Größe gekommen ist.
CREATE TABLE IF NOT EXISTS rollup_state (
    bucket_s      INTEGER PRIMARY KEY,
    last_bucket   TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Letzter Wert pro Serie. Macht die Overview-Abfrage O(Serien) statt O(Samples).
CREATE TABLE IF NOT EXISTS series_last (
    series_id  BIGINT PRIMARY KEY REFERENCES series(id) ON DELETE CASCADE,
    ts         TIMESTAMPTZ NOT NULL,
    value      DOUBLE PRECISION NOT NULL,
    prev_ts    TIMESTAMPTZ,
    prev_value DOUBLE PRECISION
);

-- ---------------------------------------------------------------- Requests
CREATE TABLE IF NOT EXISTS requests (
    ts                TIMESTAMPTZ NOT NULL,
    service_id        BIGINT REFERENCES services(id) ON DELETE CASCADE,
    model             TEXT,
    ttft_ms           DOUBLE PRECISION,
    total_ms          DOUBLE PRECISION,
    queue_ms          DOUBLE PRECISION,
    prompt_tokens     INTEGER,
    prompt_chars      INTEGER,
    completion_tokens INTEGER,
    status            INTEGER,
    stream            BOOLEAN,
    client            TEXT,
    route             TEXT,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS requests_ts_idx      ON requests USING brin (ts);
CREATE INDEX IF NOT EXISTS requests_svc_ts_idx  ON requests (service_id, ts DESC);
CREATE INDEX IF NOT EXISTS requests_model_idx   ON requests (model, ts DESC);

-- ---------------------------------------------------------------- Events
CREATE TABLE IF NOT EXISTS events (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    node_id     BIGINT REFERENCES nodes(id) ON DELETE SET NULL,
    service_id  BIGINT REFERENCES services(id) ON DELETE SET NULL,
    severity    TEXT NOT NULL DEFAULT 'info',
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
    dedup_key   TEXT,
    count       INTEGER NOT NULL DEFAULT 1,
    last_ts     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS events_ts_idx       ON events (ts DESC);
CREATE INDEX IF NOT EXISTS events_kind_ts_idx  ON events (kind, ts DESC);
CREATE INDEX IF NOT EXISTS events_sev_ts_idx   ON events (severity, ts DESC);
CREATE INDEX IF NOT EXISTS events_dedup_idx    ON events (dedup_key, ts DESC) WHERE dedup_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_detail_idx   ON events USING gin (detail);

-- Rohtext geblockter Queries, getrennt gespeichert und mit eigener Retention.
-- Nur beschrieben, wenn AIMON_ALLOW_RAW_QUERIES=true.
CREATE TABLE IF NOT EXISTS raw_queries (
    id        BIGSERIAL PRIMARY KEY,
    event_id  BIGINT REFERENCES events(id) ON DELETE CASCADE,
    ts        TIMESTAMPTZ NOT NULL,
    query_raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS raw_queries_ts_idx ON raw_queries (ts);

-- ---------------------------------------------------------------- Probes / Graph
CREATE TABLE IF NOT EXISTS probes (
    ts             TIMESTAMPTZ NOT NULL,
    from_node_id   BIGINT REFERENCES nodes(id) ON DELETE CASCADE,
    target         TEXT NOT NULL,
    target_svc_id  BIGINT REFERENCES services(id) ON DELETE SET NULL,
    ok             BOOLEAN NOT NULL,
    latency_ms     DOUBLE PRECISION,
    http_status    INTEGER,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS probes_ts_idx ON probes USING brin (ts);
CREATE INDEX IF NOT EXISTS probes_edge_idx ON probes (from_node_id, target, ts DESC);

-- Aktueller Kantenzustand, vom Ingest fortgeschrieben.
CREATE TABLE IF NOT EXISTS edges (
    id             BIGSERIAL PRIMARY KEY,
    src_node_id    BIGINT REFERENCES nodes(id) ON DELETE CASCADE,
    src_svc_id     BIGINT REFERENCES services(id) ON DELETE CASCADE,
    dst_svc_id     BIGINT REFERENCES services(id) ON DELETE CASCADE,
    dst_target     TEXT,
    kind           TEXT NOT NULL DEFAULT 'probe',   -- probe | traffic
    ok             BOOLEAN,
    latency_ms     DOUBLE PRECISION,
    last_ok_ts     TIMESTAMPTZ,
    last_fail_ts   TIMESTAMPTZ,
    last_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    samples        BIGINT NOT NULL DEFAULT 0,
    fails          BIGINT NOT NULL DEFAULT 0,
    -- NULLS NOT DISTINCT ist hier zwingend: src_svc_id und dst_svc_id sind oft NULL,
    -- und mit der Standard-Semantik (NULL != NULL) würde jeder Upsert eine neue
    -- Zeile anlegen statt die bestehende Kante fortzuschreiben. Braucht PG >= 15.
    UNIQUE NULLS NOT DISTINCT (src_node_id, src_svc_id, dst_svc_id, dst_target, kind)
);
CREATE INDEX IF NOT EXISTS edges_dst_idx ON edges (dst_svc_id);

-- ---------------------------------------------------------------- Incidents
CREATE TABLE IF NOT EXISTS incidents (
    id            BIGSERIAL PRIMARY KEY,
    rule          TEXT NOT NULL,
    severity      TEXT NOT NULL,
    subject_kind  TEXT NOT NULL,       -- node | service | gpu | global
    subject       TEXT NOT NULL,
    title         TEXT NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at      TIMESTAMPTZ,
    acknowledged  BOOLEAN NOT NULL DEFAULT false,
    context       JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (rule, subject, started_at)
);
CREATE INDEX IF NOT EXISTS incidents_open_idx ON incidents (ended_at, severity, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS incidents_rule_idx ON incidents (rule, subject) WHERE ended_at IS NULL;

-- ---------------------------------------------------------------- Idempotenz
CREATE TABLE IF NOT EXISTS ingest_keys (
    key        TEXT PRIMARY KEY,
    seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ingest_keys_seen_idx ON ingest_keys (seen_at);

-- Bestehende Installationen: alten UNIQUE-Index auf edges ersetzen.
DO $$
DECLARE
    con_name TEXT;
BEGIN
    -- Die NULLS-Semantik steht in pg_index.indnullsnotdistinct (ab PG 15).
    SELECT c.conname INTO con_name
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    JOIN pg_index i ON i.indexrelid = c.conindid
    WHERE t.relname = 'edges' AND c.contype = 'u' AND NOT i.indnullsnotdistinct
    LIMIT 1;

    IF con_name IS NOT NULL THEN
        -- Duplikate zusammenführen: pro Kantenidentität die jüngste Zeile behalten.
        DELETE FROM edges e USING edges k
        WHERE e.id < k.id
          AND e.src_node_id IS NOT DISTINCT FROM k.src_node_id
          AND e.src_svc_id  IS NOT DISTINCT FROM k.src_svc_id
          AND e.dst_svc_id  IS NOT DISTINCT FROM k.dst_svc_id
          AND e.dst_target  IS NOT DISTINCT FROM k.dst_target
          AND e.kind = k.kind;
        EXECUTE format('ALTER TABLE edges DROP CONSTRAINT %I', con_name);
        ALTER TABLE edges ADD CONSTRAINT edges_identity_key
            UNIQUE NULLS NOT DISTINCT (src_node_id, src_svc_id, dst_svc_id, dst_target, kind);
    END IF;
END $$;

INSERT INTO schema_version (version) VALUES (1) ON CONFLICT DO NOTHING;
