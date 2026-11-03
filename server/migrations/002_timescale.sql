-- Optional: nur wenn die Timescale-Extension verfügbar ist.
-- Der Server führt diese Datei nur aus, wenn CREATE EXTENSION geklappt hat.
-- Alles hier ist nicht-destruktiv und übersprungen, wenn es bereits existiert.

SELECT create_hypertable('samples', 'ts',
    chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE,
    migrate_data => TRUE);
SELECT create_hypertable('requests', 'ts',
    chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE,
    migrate_data => TRUE);
SELECT create_hypertable('probes', 'ts',
    chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE,
    migrate_data => TRUE);

ALTER TABLE samples  SET (timescaledb.compress, timescaledb.compress_segmentby = 'series_id');
ALTER TABLE requests SET (timescaledb.compress, timescaledb.compress_segmentby = 'service_id');

SELECT add_compression_policy('samples',  INTERVAL '2 days', if_not_exists => TRUE);
SELECT add_compression_policy('requests', INTERVAL '3 days', if_not_exists => TRUE);

INSERT INTO schema_version (version) VALUES (2) ON CONFLICT DO NOTHING;
