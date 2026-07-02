-- Compass initial schema.
-- Never change an applied migration; add a new numbered file instead.
--
-- Conventions:
--   * All money amounts are integer cents (EUR). Column names end in _cents.
--   * All "day" columns are ISO dates (YYYY-MM-DD) in Europe/Amsterdam
--     calendar time — the business day the owners see in Shopify/bol/Meta.
--   * Percentages/rates are stored as fractions (0.09 = 9%).
--   * raw_json keeps the untouched source payload: never lose source data.
--
-- Multi-SKU note: Cloudplunge sells one SKU today. `orders.units` covers
-- that; when a second SKU arrives, add an `order_lines` table in a new
-- migration and keep `orders` as the order header.

CREATE TABLE orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    extern_id        TEXT NOT NULL,                    -- the source's own order id
    channel          TEXT NOT NULL CHECK (channel IN ('shopify', 'bol')),
    order_day        TEXT NOT NULL,                    -- Amsterdam calendar day
    ordered_at       TEXT,                             -- full ISO timestamp from the source
    gross_cents      INTEGER NOT NULL,                 -- incl. VAT, after discounts
    net_cents        INTEGER,                          -- excl. VAT when the source provides it
    vat_cents        INTEGER,                          -- VAT amount when the source provides it
    units            INTEGER NOT NULL DEFAULT 1,
    customer_hash    TEXT,                             -- sha256, never raw PII (AVG-dataminimalisatie)
    is_new_customer  INTEGER,                          -- source's own flag: 1/0, NULL = unknown
    payment_method   TEXT,
    status           TEXT NOT NULL DEFAULT 'paid'
                     CHECK (status IN ('paid', 'refunded')),
    refunded_cents   INTEGER NOT NULL DEFAULT 0,       -- partial refunds, incl. VAT
    raw_json         TEXT,
    first_seen       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (channel, extern_id)
);
CREATE INDEX idx_orders_day ON orders(order_day);
CREATE INDEX idx_orders_channel_day ON orders(channel, order_day);
CREATE INDEX idx_orders_customer ON orders(customer_hash);

-- One row per campaign per day, from our own Meta ad account Insights.
CREATE TABLE ad_spend_daily (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    day                       TEXT NOT NULL,
    campaign_id               TEXT NOT NULL,
    campaign_name             TEXT,
    spend_cents               INTEGER NOT NULL DEFAULT 0,
    impressions               INTEGER,
    clicks                    INTEGER,
    meta_purchases            INTEGER,                 -- purchases *according to Meta*
    meta_purchase_value_cents INTEGER,                 -- value *according to Meta*
    raw_json                  TEXT,
    updated_at                TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (day, campaign_id)
);
CREATE INDEX idx_spend_day ON ad_spend_daily(day);

-- Cost model with version history: margins are always computed with the
-- version that was valid on the order day. Rows are never deleted; a new
-- row with a later valid_from supersedes older ones from that day on.
CREATE TABLE cost_model (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    valid_from                TEXT NOT NULL UNIQUE,    -- ISO date
    cogs_per_unit_cents       INTEGER NOT NULL,
    shipping_per_order_cents  INTEGER NOT NULL,
    fee_pct                   REAL NOT NULL DEFAULT 0, -- default payment fee, fraction of gross
    fee_fixed_cents           INTEGER NOT NULL DEFAULT 0,
    payment_fees_json         TEXT NOT NULL DEFAULT '{}',  -- per payment_method override: {"ideal": {"pct": 0, "fixed_cents": 29}}
    bol_commission_pct        REAL NOT NULL DEFAULT 0, -- fraction of gross, bol orders only
    vat_rate                  REAL NOT NULL DEFAULT 0.09,
    fixed_month_cents         INTEGER NOT NULL DEFAULT 0,  -- fixed monthly overhead
    lead_time_days            INTEGER NOT NULL DEFAULT 30, -- supplier lead time
    safety_factor             REAL NOT NULL DEFAULT 1.3,   -- reorder-point safety multiplier
    note                      TEXT,
    created_at                TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE inventory_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day        TEXT NOT NULL,
    units      INTEGER NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('shopify', 'manual', 'fixture')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (day, source)
);
CREATE INDEX idx_inventory_day ON inventory_snapshots(day);

-- Advisory signals. The tool advises; it never changes anything itself.
CREATE TABLE signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    type         TEXT NOT NULL,                        -- scale_up / scale_down / reorder / spend_anomaly
    day          TEXT NOT NULL,                        -- the day the rule fired
    message      TEXT NOT NULL,                        -- one-line advice, plain Dutch
    explanation  TEXT,                                 -- the numbers behind it, plain Dutch
    status       TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'seen')),
    details_json TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (type, day)
);
CREATE INDEX idx_signals_status ON signals(status);

-- Materialized per-day rollup so the dashboard never scans raw orders.
-- Rebuilt by the collector after every run; derived, safe to rebuild.
CREATE TABLE daily_metrics (
    day                       TEXT PRIMARY KEY,
    orders_count              INTEGER NOT NULL DEFAULT 0,
    units                     INTEGER NOT NULL DEFAULT 0,
    revenue_shopify_cents     INTEGER NOT NULL DEFAULT 0,  -- incl. VAT, net of refunds
    revenue_bol_cents         INTEGER NOT NULL DEFAULT 0,  -- incl. VAT, net of refunds
    revenue_excl_cents        INTEGER NOT NULL DEFAULT 0,  -- excl. VAT, net of refunds
    margin_cents              INTEGER NOT NULL DEFAULT 0,  -- contribution margin
    new_customers             INTEGER NOT NULL DEFAULT 0,
    spend_cents               INTEGER NOT NULL DEFAULT 0,
    meta_purchases            INTEGER NOT NULL DEFAULT 0,
    meta_purchase_value_cents INTEGER NOT NULL DEFAULT 0,
    mer                       REAL,                        -- this day's omzet excl. btw / spend
    cac_cents                 INTEGER,                     -- this day's spend / new customers
    updated_at                TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Audit trail of every collect/backfill/import run.
CREATE TABLE runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    kind                TEXT NOT NULL DEFAULT 'collect'
                        CHECK (kind IN ('collect', 'backfill', 'import', 'demo')),
    ok                  INTEGER,                       -- 1 = no errors, 0 = errors
    orders_upserted     INTEGER NOT NULL DEFAULT 0,
    spend_rows_upserted INTEGER NOT NULL DEFAULT 0,
    errors              TEXT,                          -- JSON array of error strings
    detail              TEXT                           -- JSON per-source results
);

-- Dashboard-editable settings (signal thresholds etc.). Defaults live in
-- code (signals.THRESHOLD_DEFAULTS); a row here overrides the default.
CREATE TABLE app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
