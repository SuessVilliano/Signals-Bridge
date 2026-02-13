-- ============================================================
-- Signal Bridge - PASTE THIS INTO SUPABASE SQL EDITOR
-- URL: https://supabase.com/dashboard/project/szcnpugeztcawwjcopob/sql/new
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. PROVIDERS
CREATE TABLE IF NOT EXISTS providers (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    api_key_hash    TEXT NOT NULL,
    webhook_secret  TEXT NOT NULL,
    is_active       BOOLEAN DEFAULT TRUE,
    is_verified     BOOLEAN DEFAULT FALSE,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_providers_api_key ON providers (api_key_hash);
CREATE INDEX IF NOT EXISTS idx_providers_active ON providers (is_active) WHERE is_active = TRUE;

-- 2. ENUMS + CANONICAL_SIGNALS
DO $$ BEGIN
    CREATE TYPE signal_direction AS ENUM ('LONG', 'SHORT');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE signal_status AS ENUM ('PENDING','ACTIVE','TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT','CLOSED','INVALID');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE asset_class AS ENUM ('FUTURES','FOREX','CRYPTO','STOCKS','OTHER');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS canonical_signals (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider_id         UUID NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    external_signal_id  TEXT,
    strategy_name       TEXT,
    symbol              TEXT NOT NULL,
    asset_class         asset_class NOT NULL DEFAULT 'OTHER',
    direction           signal_direction NOT NULL,
    entry_price         NUMERIC(20, 8) NOT NULL,
    sl                  NUMERIC(20, 8) NOT NULL,
    tp1                 NUMERIC(20, 8) NOT NULL,
    tp2                 NUMERIC(20, 8),
    tp3                 NUMERIC(20, 8),
    risk_distance       NUMERIC(20, 8),
    rr_ratio            NUMERIC(10, 4),
    status              signal_status NOT NULL DEFAULT 'PENDING',
    entry_time          TIMESTAMPTZ NOT NULL,
    activated_at        TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    close_reason        TEXT,
    exit_price          NUMERIC(20, 8),
    r_value             NUMERIC(10, 4),
    pnl_pct             NUMERIC(10, 4),
    max_favorable       NUMERIC(20, 8),
    max_adverse         NUMERIC(20, 8),
    next_poll_at        TIMESTAMPTZ DEFAULT NOW(),
    last_price          NUMERIC(20, 8),
    last_price_at       TIMESTAMPTZ,
    raw_payload         JSONB,
    validation_errors   JSONB DEFAULT '[]',
    validation_warnings JSONB DEFAULT '[]',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signals_provider ON canonical_signals (provider_id);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON canonical_signals (symbol);
CREATE INDEX IF NOT EXISTS idx_signals_status ON canonical_signals (status);
CREATE INDEX IF NOT EXISTS idx_signals_active ON canonical_signals (status, next_poll_at)
    WHERE status IN ('PENDING', 'ACTIVE', 'TP1_HIT', 'TP2_HIT', 'TP3_HIT');
CREATE INDEX IF NOT EXISTS idx_signals_provider_status ON canonical_signals (provider_id, status);
CREATE INDEX IF NOT EXISTS idx_signals_created ON canonical_signals (created_at DESC);

-- 3. SIGNAL_EVENTS
DO $$ BEGIN
    CREATE TYPE event_type AS ENUM ('ENTRY_REGISTERED','ENTRY_HIT','TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT','PRICE_UPDATE','MANUAL_CLOSE','EXPIRED','VALIDATION_FAILED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE event_source AS ENUM ('TRADINGVIEW','PINESCRIPT','POLLING','WEBSOCKET','MANUAL','HISTORICAL');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS signal_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    signal_id       UUID NOT NULL REFERENCES canonical_signals(id) ON DELETE CASCADE,
    event_type      event_type NOT NULL,
    price           NUMERIC(20, 8),
    source          event_source NOT NULL,
    event_time      TIMESTAMPTZ NOT NULL,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_events_signal ON signal_events (signal_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON signal_events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_time ON signal_events (event_time DESC);

-- 4. PRICE_SNAPSHOTS
CREATE TABLE IF NOT EXISTS price_snapshots (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    symbol          TEXT NOT NULL,
    price           NUMERIC(20, 8) NOT NULL,
    bid             NUMERIC(20, 8),
    ask             NUMERIC(20, 8),
    source          TEXT NOT NULL,
    snapshot_time   TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_price_symbol_time ON price_snapshots (symbol, snapshot_time DESC);

-- 5. PROVIDER_STATS
CREATE TABLE IF NOT EXISTS provider_stats (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider_id     UUID NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    total_signals   INTEGER DEFAULT 0,
    open_signals    INTEGER DEFAULT 0,
    wins            INTEGER DEFAULT 0,
    losses          INTEGER DEFAULT 0,
    partials        INTEGER DEFAULT 0,
    win_rate        NUMERIC(6, 4) DEFAULT 0,
    tp1_hit_rate    NUMERIC(6, 4) DEFAULT 0,
    tp2_hit_rate    NUMERIC(6, 4) DEFAULT 0,
    tp3_hit_rate    NUMERIC(6, 4) DEFAULT 0,
    avg_r           NUMERIC(10, 4) DEFAULT 0,
    total_r         NUMERIC(10, 4) DEFAULT 0,
    best_r          NUMERIC(10, 4) DEFAULT 0,
    worst_r         NUMERIC(10, 4) DEFAULT 0,
    expectancy      NUMERIC(10, 4) DEFAULT 0,
    avg_duration_hours NUMERIC(10, 2) DEFAULT 0,
    period_start    DATE,
    period_end      DATE,
    calculated_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (provider_id, period_start, period_end)
);
CREATE INDEX IF NOT EXISTS idx_stats_provider ON provider_stats (provider_id);

-- 6. WEBHOOK_CONFIGS
CREATE TABLE IF NOT EXISTS webhook_configs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider_id     UUID NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    event_types     TEXT[] NOT NULL,
    headers         JSONB DEFAULT '{}',
    is_active       BOOLEAN DEFAULT TRUE,
    consecutive_failures INTEGER DEFAULT 0,
    last_sent_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_webhooks_provider ON webhook_configs (provider_id);
CREATE INDEX IF NOT EXISTS idx_webhooks_active ON webhook_configs (is_active) WHERE is_active = TRUE;

-- 7. NOTIFICATION_LOG
CREATE TABLE IF NOT EXISTS notification_log (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    webhook_config_id   UUID NOT NULL REFERENCES webhook_configs(id) ON DELETE CASCADE,
    signal_id           UUID REFERENCES canonical_signals(id) ON DELETE SET NULL,
    event_type          TEXT NOT NULL,
    payload             JSONB NOT NULL,
    http_status         INTEGER,
    response_body       TEXT,
    error_message       TEXT,
    attempt_number      INTEGER DEFAULT 1,
    sent_at             TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_notifications_webhook ON notification_log (webhook_config_id);
CREATE INDEX IF NOT EXISTS idx_notifications_signal ON notification_log (signal_id);
CREATE INDEX IF NOT EXISTS idx_notifications_sent ON notification_log (sent_at DESC);

-- AUTO-UPDATE TIMESTAMPS
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_providers_updated ON providers;
CREATE TRIGGER trg_providers_updated
    BEFORE UPDATE ON providers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

DROP TRIGGER IF EXISTS trg_signals_updated ON canonical_signals;
CREATE TRIGGER trg_signals_updated
    BEFORE UPDATE ON canonical_signals
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

DROP TRIGGER IF EXISTS trg_webhooks_updated ON webhook_configs;
CREATE TRIGGER trg_webhooks_updated
    BEFORE UPDATE ON webhook_configs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ROW LEVEL SECURITY
ALTER TABLE providers ENABLE ROW LEVEL SECURITY;
ALTER TABLE canonical_signals ENABLE ROW LEVEL SECURITY;
ALTER TABLE signal_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public read provider stats" ON provider_stats FOR SELECT USING (true);
CREATE POLICY "Public read closed signals" ON canonical_signals FOR SELECT USING (status IN ('CLOSED', 'TP3_HIT', 'SL_HIT'));

-- VIEWS
CREATE OR REPLACE VIEW active_signals_to_poll AS
SELECT cs.id, cs.symbol, cs.asset_class, cs.direction, cs.entry_price, cs.sl,
       cs.tp1, cs.tp2, cs.tp3, cs.status, cs.last_price, cs.next_poll_at,
       p.name AS provider_name
FROM canonical_signals cs
JOIN providers p ON cs.provider_id = p.id
WHERE cs.status IN ('PENDING', 'ACTIVE', 'TP1_HIT', 'TP2_HIT', 'TP3_HIT')
  AND cs.next_poll_at <= NOW()
ORDER BY cs.next_poll_at ASC;

CREATE OR REPLACE VIEW provider_leaderboard AS
SELECT p.id, p.name, p.is_verified, ps.total_signals, ps.wins, ps.losses,
       ps.win_rate, ps.avg_r, ps.total_r, ps.tp1_hit_rate, ps.tp2_hit_rate,
       ps.tp3_hit_rate, ps.expectancy, ps.calculated_at
FROM providers p
JOIN provider_stats ps ON p.id = ps.provider_id
WHERE p.is_active = TRUE AND ps.period_start IS NULL
ORDER BY ps.total_r DESC;

-- DONE! You should see "Success. No rows returned" if everything worked.
