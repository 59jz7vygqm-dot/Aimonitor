-- Benachrichtigungen. Idempotent.

CREATE TABLE IF NOT EXISTS alert_log (
    id           BIGSERIAL PRIMARY KEY,
    incident_id  BIGINT REFERENCES incidents(id) ON DELETE CASCADE,
    -- 'fired' oder 'resolved'
    kind         TEXT NOT NULL,
    channel      TEXT NOT NULL,
    sent_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ok           BOOLEAN NOT NULL,
    error        TEXT,
    -- Zum Wiedererkennen, auch wenn der Incident gelöscht wurde.
    subject      TEXT,
    rule         TEXT
);

-- Genau eine Benachrichtigung je Incident, Art und Kanal. Das ist die
-- eigentliche Absicherung gegen Benachrichtigungsschleifen: selbst wenn der
-- Regeldurchlauf mehrfach anläuft, kann derselbe Vorfall nicht zweimal
-- gemeldet werden.
CREATE UNIQUE INDEX IF NOT EXISTS alert_log_once_idx
    ON alert_log (incident_id, kind, channel) WHERE ok;

CREATE INDEX IF NOT EXISTS alert_log_time_idx ON alert_log (sent_at DESC);

INSERT INTO schema_version (version) VALUES (3) ON CONFLICT DO NOTHING;
