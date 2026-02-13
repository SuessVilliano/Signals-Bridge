-- ============================================================
-- Signal Bridge - Database Schema
-- Run this in Supabase SQL Editor or via migrations
-- ============================================================

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_cron";

-- ============================================================
-- 1. PROVIDERS - Signal provider profiles
-- ============================================================
CREATE TABLE providers (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    api_key_hash    TEXT NOT NULL,           -- bcrypt hash of API key
    webhook_secret  TEXT NOT NULL,           -- HMAC signing secret (hashed)
    is_active       BOOLEAN DEFAULT TRUE,
    is_verified     BOOLEAN DEFAULT FALSE,   -- "Verified by Hybrid" badge
    metadata        JSONB DEFAULT '{}',      -- flexible extra fields
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_providers_api_key ON providers (api_key_hash);
CREATE INDEX idx_providers_active ON providers (is_active) WHERE is_active = TRUE;

-- ============================================================
-- 2. CANONICAL_SIGNALS - Normalized trading signals
-- ============================================================
CREATE TYPE signal_direction AS ENUM ('LONG', 'SHORT');
CREATE TYPE signal_status AS ENUM (
    'PENDING',      -- received, not yet active
    'ACTIVE',       -- entry price has been reached
    'TP1_HIT',      -- take profit 1 reached
    'TP2_HIT',      -- take profit 2 reached
    'TP3_HIT',      -- take profit 3 reached (full win)
    'SL_HIT',       -- stop loss hit
    'CLOSED',       -- manually closed or expired
    'INVALID'       -- failed validation
);
CREATE TYPE asset_class AS ENUM ('FUTURES', 'FOREX', 'CRYPTO', 'STOCKS', 'OTHER');

CREATE TABLE canonical_signals (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider_id         UUID NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    external_signal_id  TEXT,                   -- provider's own signal ID
    strategy_name       TEXT,                   -- optional strategy label

    -- Instrument
    symbol              TEXT NOT NULL,           -- normalized: "NQ", "EURUSD", "BTCUSDT"
    asset_class         asset_class NOT NULL DEFAULT 'OTHER',

    -- Trade levels
    direction           signal_direction NOT NULL,
    entry_price         NUMERIC(20, 8) NOT NULL,
    sl                  NUMERIC(20, 8) NOT NULL,
    tp1                 NUMERIC(20, 8) NOT NULL,
    tp2                 NUMERIC(20, 8),          -- optional
    tp3                 NUMERIC(20, 8),          -- optional

    -- Calculated fields
    risk_distance       NUMERIC(20, 8),          -- abs(entry - sl)
    rr_ratio            NUMERIC(10, 4),          -- tp1 distance / risk distance

    -- State
    status              signal_status NOT NULL DEFAULT 'PENDING',
    entry_time          TIMESTAMPTZ NOT NULL,    -- when signal was issued
    activated_at        TIMESTAMPTZ,             -- when entry was filled
    closed_at           TIMESTAMPTZ,             -- when signal resolved
    close_reason        TEXT,                    -- TP3_HIT / SL_HIT / MANUAL / EXPIRED

    -- Outcome (populated on close)
    exit_price          NUMERIC(20, 8),
    r_value             NUMERIC(10, 4),          -- realized R
    pnl_pct             NUMERIC(10, 4),          -- % gain/loss
    max_favorable       NUMERIC(20, 8),          -- best price during trade
    max_adverse         NUMERIC(20, 8),          -- worst price during trade

    -- Price monitoring
    next_poll_at        TIMESTAMPTZ DEFAULT NOW(),  -- smart scheduler
    last_price          NUMERIC(20, 8),             -- last known price
    last_price_at       TIMESTAMPTZ,                -- when last price was fetched

    -- Audit
    raw_payload         JSONB,                   -- original webhook JSON
    validation_errors   JSONB DEFAULT '[]',
    validation_warnings JSONB DEFAULT '[]',

    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Performance-critical indexes
CREATE INDEX idx_signals_provider ON canonical_signals (provider_id);
CREATE INDEX idx_signals_symbol ON canonical_signals (symbol);
CREATE INDEX idx_signals_status ON canonical_signals (status);
CREATE INDEX idx_signals_active ON canonical_signals (status, next_poll_at)
    WHERE status IN ('PENDING', 'ACTIVE', 'TP1_HIT', 'TP2_HIT', 'TP3_HIT');
CREATE INDEX idx_signals_provider_status ON canonical_signals (provider_id, status);
CREATE INDEX idx_signals_created ON canonical_signals (created_at DESC);

-- ============================================================
-- 3. SIGNAL_EVENTS - Lifecycle events (event sourcing)
-- ============================================================
CREATE TYPE event_type AS ENUM (
    'ENTRY_REGISTERED',   -- signal received and stored
    'ENTRY_HIT',          -- entry price reached
    'TP1_HIT',            -- take profit 1 hit
    'TP2_HIT',            -- take profit 2 hit
    'TP3_HIT',            -- take profit 3 hit
    'SL_HIT',             -- stop loss hit
    'PRICE_UPDATE',       -- periodic price snapshot
    'MANUAL_CLOSE',       -- provider manually closed
    'EXPIRED',            -- signal expired without fill
    'VALIDATION_FAILED'   -- signal failed validation
);
CREATE TYPE event_source AS ENUM (
    'TRADINGVIEW',        -- from TradingView webhook
    'PINESCRIPT',         -- from PineScript monitor
    'POLLING',            -- from REST API price poll
    'WEBSOCKET',          -- from Binance/crypto WebSocket
    'MANUAL',             -- manual entry
    'HISTORICAL'          -- from historical backtest
);

CREATE TABLE signal_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    signal_id       UUID NOT NULL REFERENCES canonical_signals(id) ON DELETE CASCADE,
    event_type      event_type NOT NULL,
    price           NUMERIC(20, 8),          -- price at time of event
    source          event_source NOT NULL,
    event_time      TIMESTAMPTZ NOT NULL,    -- when the event actually happened
    metadata        JSONB DEFAULT '{}',      -- extra context
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_events_signal ON signal_events (signal_id);
CREATE INDEX idx_events_type ON signal_events (event_type);
CREATE INDEX idx_events_time ON signal_events (event_time DESC);

-- ============================================================
-- 4. PRICE_SNAPSHOTS - Periodic price records (audit trail)
-- ============================================================
CREATE TABLE price_snapshots (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    symbol          TEXT NOT NULL,
    price           NUMERIC(20, 8) NOT NULL,
    bid             NUMERIC(20, 8),
    ask             NUMERIC(20, 8),
    source          TEXT NOT NULL,           -- "binance_ws", "twelvedata", "yfinance"
    snapshot_time   TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_price_symbol_time ON price_snapshots (symbol, snapshot_time DESC);

-- Partition by month for performance (optional, enable later)
-- CREATE TABLE price_snapshots ... PARTITION BY RANGE (snapshot_time);

-- ============================================================
-- 5. PROVIDER_STATS - Aggregated performance metrics
-- ============================================================
CREATE TABLE provider_stats (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider_id     UUID NOT NULL REFERENCES providers(id) ON DELETE CASCADE,

    -- Counts
    total_signals   INTEGER DEFAULT 0,
    open_signals    INTEGER DEFAULT 0,
    wins            INTEGER DEFAULT 0,
    losses          INTEGER DEFAULT 0,
    partials        INTEGER DEFAULT 0,       -- partial wins (TP1 hit, then SL)

    -- Rates
    win_rate        NUMERIC(6, 4) DEFAULT 0,
    tp1_hit_rate    NUMERIC(6, 4) DEFAULT 0,
    tp2_hit_rate    NUMERIC(6, 4) DEFAULT 0,
    tp3_hit_rate    NUMERIC(6, 4) DEFAULT 0,

    -- R metrics
    avg_r           NUMERIC(10, 4) DEFAULT 0,
    total_r         NUMERIC(10, 4) DEFAULT 0,
    best_r          NUMERIC(10, 4) DEFAULT 0,
    worst_r         NUMERIC(10, 4) DEFAULT 0,
    expectancy      NUMERIC(10, 4) DEFAULT 0,  -- (win_rate * avg_win_r) - (loss_rate * avg_loss_r)

    -- Time
    avg_duration_hours NUMERIC(10, 2) DEFAULT 0,

    -- Period
    period_start    DATE,                    -- NULL = all-time
    period_end      DATE,

    calculated_at   TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (provider_id, period_start, period_end)
);

CREATE INDEX idx_stats_provider ON provider_stats (provider_id);

-- ============================================================
-- 6. WEBHOOK_CONFIGS - Outbound webhook destinations
-- ============================================================
CREATE TABLE webhook_configs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider_id     UUID NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,            -- "HybridJournal", "Discord Bot", etc.
    url             TEXT NOT NULL,            -- destination URL
    event_types     TEXT[] NOT NULL,          -- {"TP1_HIT", "SL_HIT", "ENTRY_HIT"}
    headers         JSONB DEFAULT '{}',      -- custom headers (auth tokens etc.)
    is_active       BOOLEAN DEFAULT TRUE,
    consecutive_failures INTEGER DEFAULT 0,  -- circuit breaker counter
    last_sent_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_webhooks_provider ON webhook_configs (provider_id);
CREATE INDEX idx_webhooks_active ON webhook_configs (is_active) WHERE is_active = TRUE;

-- ============================================================
-- 7. NOTIFICATION_LOG - Delivery history
-- ============================================================
CREATE TABLE notification_log (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    webhook_config_id   UUID NOT NULL REFERENCES webhook_configs(id) ON DELETE CASCADE,
    signal_id           UUID REFERENCES canonical_signals(id) ON DELETE SET NULL,
    event_type          TEXT NOT NULL,
    payload             JSONB NOT NULL,
    http_status         INTEGER,             -- response status code
    response_body       TEXT,                -- truncated response
    error_message       TEXT,
    attempt_number      INTEGER DEFAULT 1,
    sent_at             TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_notifications_webhook ON notification_log (webhook_config_id);
CREATE INDEX idx_notifications_signal ON notification_log (signal_id);
CREATE INDEX idx_notifications_sent ON notification_log (sent_at DESC);

-- ============================================================
-- AUTO-UPDATE TIMESTAMPS
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_providers_updated
    BEFORE UPDATE ON providers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_signals_updated
    BEFORE UPDATE ON canonical_signals
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_webhooks_updated
    BEFORE UPDATE ON webhook_configs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ============================================================
-- ROW LEVEL SECURITY (RLS)
-- ============================================================
-- Enable RLS on all tables
ALTER TABLE providers ENABLE ROW LEVEL SECURITY;
ALTER TABLE canonical_signals ENABLE ROW LEVEL SECURITY;
ALTER TABLE signal_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_log ENABLE ROW LEVEL SECURITY;

-- Service role bypasses RLS (our FastAPI backend uses service_role key)
-- These policies are for the anon/public key if you want to expose a read-only API

-- Public can read provider stats (leaderboard)
CREATE POLICY "Public read provider stats"
    ON provider_stats FOR SELECT
    USING (true);

-- Public can read closed signal summaries (no raw_payload)
CREATE POLICY "Public read closed signals"
    ON canonical_signals FOR SELECT
    USING (status IN ('CLOSED', 'TP3_HIT', 'SL_HIT'));

-- ============================================================
-- HELPFUL VIEWS
-- ============================================================

-- Active signals that need price monitoring
CREATE VIEW active_signals_to_poll AS
SELECT
    cs.id,
    cs.symbol,
    cs.asset_class,
    cs.direction,
    cs.entry_price,
    cs.sl,
    cs.tp1,
    cs.tp2,
    cs.tp3,
    cs.status,
    cs.last_price,
    cs.next_poll_at,
    p.name AS provider_name
FROM canonical_signals cs
JOIN providers p ON cs.provider_id = p.id
WHERE cs.status IN ('PENDING', 'ACTIVE', 'TP1_HIT', 'TP2_HIT', 'TP3_HIT')
  AND cs.next_poll_at <= NOW()
ORDER BY cs.next_poll_at ASC;

-- Provider leaderboard
CREATE VIEW provider_leaderboard AS
SELECT
    p.id,
    p.name,
    p.is_verified,
    ps.total_signals,
    ps.wins,
    ps.losses,
    ps.win_rate,
    ps.avg_r,
    ps.total_r,
    ps.tp1_hit_rate,
    ps.tp2_hit_rate,
    ps.tp3_hit_rate,
    ps.expectancy,
    ps.calculated_at
FROM providers p
JOIN provider_stats ps ON p.id = ps.provider_id
WHERE p.is_active = TRUE
  AND ps.period_start IS NULL  -- all-time stats
ORDER BY ps.total_r DESC;
