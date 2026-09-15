-- Phase 6: normalized trade rows written from the live checkpoint path.
-- Money is `numeric` in the smallest raw unit (SuiPump bond SUI = 1e9 mis;
-- tokens = raw units; Cetus amounts = raw units). Every write is idempotent
-- by its natural key (tx_digest, event_index) so replay converges (§12).

CREATE TABLE IF NOT EXISTS bonding_trades (
    tx_digest     TEXT NOT NULL,
    event_index   INT  NOT NULL,
    checkpoint    BIGINT NOT NULL,
    ts_ms         BIGINT NOT NULL,
    curve_id      TEXT NOT NULL,
    side          TEXT NOT NULL CHECK (side IN ('buy','sell')),
    wallet        TEXT,
    token         TEXT,                        -- coin type; set by registry once resolved
    amount_sui    NUMERIC NOT NULL,
    amount_token  NUMERIC NOT NULL,
    fees          JSONB NOT NULL DEFAULT '{}'::jsonb,
    snapshot      JSONB NOT NULL DEFAULT '{}'::jsonb,
    fields        JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw           JSONB NOT NULL,
    event_type    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tx_digest, event_index)
);

CREATE INDEX IF NOT EXISTS bonding_trades_curve_ts_idx   ON bonding_trades (curve_id, ts_ms);
CREATE INDEX IF NOT EXISTS bonding_trades_wallet_ts_idx  ON bonding_trades (wallet, ts_ms);
CREATE INDEX IF NOT EXISTS bonding_trades_checkpoint_idx ON bonding_trades (checkpoint);

CREATE TABLE IF NOT EXISTS swaps (
    tx_digest     TEXT NOT NULL,
    event_index   INT  NOT NULL,
    checkpoint    BIGINT NOT NULL,
    ts_ms         BIGINT NOT NULL,
    dex           TEXT NOT NULL,
    pool          TEXT NOT NULL,
    atob          BOOLEAN,
    amount_in     NUMERIC NOT NULL,
    amount_out    NUMERIC NOT NULL,
    fee           NUMERIC,
    fields        JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw           JSONB NOT NULL,
    event_type    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tx_digest, event_index)
);

CREATE INDEX IF NOT EXISTS swaps_pool_ts_idx      ON swaps (pool, ts_ms);
CREATE INDEX IF NOT EXISTS swaps_checkpoint_idx   ON swaps (checkpoint);

-- Anything adapted-but-undecodable lands here, never silently dropped (§24).
CREATE TABLE IF NOT EXISTS dlq (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    checkpoint  BIGINT NOT NULL,
    event_type  TEXT NOT NULL,
    tx_digest   TEXT,
    event_index INT,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason      TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dlq_event_ts_idx ON dlq (event_type, ts);