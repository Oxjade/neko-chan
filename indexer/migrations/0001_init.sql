-- Phase 1: raw checkpoint intake store + stream cursors.
-- Natural keys are unique so every write is idempotent (replay safe by construction).

CREATE TABLE IF NOT EXISTS raw_checkpoints (
    seq                        BIGINT PRIMARY KEY,          -- checkpoint sequence number (height)
    digest                     TEXT NOT NULL,               -- CheckpointSummary digest (hex)
    previous_digest            TEXT,                        -- parent digest (gap detection chain)
    epoch                      BIGINT NOT NULL,
    ts_ms                      BIGINT NOT NULL,             -- checkpoint timestamp (unix ms)
    network_total_transactions BIGINT,                      -- cumulative tx count at this checkpoint
    data                       BYTEA,                       -- protobuf-encoded v2 Checkpoint message (raw audit payload)
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS raw_checkpoints_epoch_idx ON raw_checkpoints (epoch);
CREATE INDEX IF NOT EXISTS raw_checkpoints_ts_idx    ON raw_checkpoints (ts_ms);

-- Single-writer stream cursors (one row per logical stream: 'live', per-backfill lanes later).
CREATE TABLE IF NOT EXISTS checkpoint_progress (
    name       TEXT PRIMARY KEY,
    last_seq   BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO checkpoint_progress (name, last_seq) VALUES ('live', 0)
ON CONFLICT (name) DO NOTHING;