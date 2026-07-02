-- Pulse initial schema.
-- Never change an applied migration; add a new numbered file instead.

-- Feedback channels. One row per configured source; the dashboard and CLI
-- show the Dutch label for each status (actief / wacht op configuratie /
-- gepauzeerd).
CREATE TABLE sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL
                CHECK (type IN ('gmail', 'meta_comments', 'trustpilot', 'bol', 'manual')),
    name        TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL DEFAULT 'awaiting_config'
                CHECK (status IN ('active', 'awaiting_config', 'paused')),
    config_json TEXT,                                  -- JSON object, never secrets
    last_run    TEXT,                                  -- ISO datetime (UTC) of last OK collect
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Competitor watchlist (concept shared with AdScout: seeded from the same
-- competitors.seed.yaml). review_urls_json is filled in by the founders.
CREATE TABLE competitors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL UNIQUE,
    category         TEXT,
    review_urls_json TEXT NOT NULL DEFAULT '[]',       -- JSON array of URLs
    status           TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'paused')),
    notes            TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Mail conversations grouped (Gmail thread). Other sources leave thread_id
-- on items NULL.
CREATE TABLE threads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id     TEXT NOT NULL,                     -- Gmail thread id
    subject         TEXT,
    status          TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'seen')),
    first_seen      TEXT NOT NULL,
    last_message_at TEXT,
    UNIQUE (source_id, external_id)
);

-- One row per piece of feedback, whatever the channel.
-- competitor_id NULL = about Cloudplunge itself.
-- author_hash is a sha256 of the normalized identifier (e-mail address,
-- platform user id); the bare address is never used as a key. `pulse
-- forget` deletes by this hash.
CREATE TABLE items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id        INTEGER NOT NULL REFERENCES sources(id),
    competitor_id    INTEGER REFERENCES competitors(id) ON DELETE SET NULL,
    external_id      TEXT NOT NULL,                    -- source id, or content hash (manual)
    happened_at      TEXT,                             -- when the feedback was written (ISO)
    language         TEXT,
    author_display   TEXT,                             -- optional display name
    author_hash      TEXT,
    text             TEXT NOT NULL,
    url              TEXT,
    thread_id        INTEGER REFERENCES threads(id) ON DELETE SET NULL,
    first_seen       TEXT NOT NULL,                    -- ISO datetime (UTC) we stored it
    raw_json         TEXT,
    pre_health_flag  INTEGER NOT NULL DEFAULT 0,       -- keyword screen before AI analysis
    followed_up_at   TEXT,                             -- urgent view: marked "opgevolgd"
    analysis_attempts INTEGER NOT NULL DEFAULT 0,      -- parse failures are retried, never dropped
    analysis_error   TEXT,
    UNIQUE (source_id, external_id)
);
CREATE INDEX idx_items_source ON items(source_id);
CREATE INDEX idx_items_competitor ON items(competitor_id);
CREATE INDEX idx_items_happened_at ON items(happened_at);
CREATE INDEX idx_items_author_hash ON items(author_hash);

-- AI analysis result, one per item. Items without a row form the analysis
-- queue. Enum values are English internally; the UI shows Dutch labels.
CREATE TABLE analyses (
    item_id        INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    sentiment      TEXT NOT NULL CHECK (sentiment IN ('positive', 'neutral', 'negative')),
    themes_json    TEXT NOT NULL DEFAULT '[]',         -- JSON array of theme slugs
    urgency        TEXT NOT NULL CHECK (urgency IN ('urgent', 'normal', 'low')),
    type           TEXT NOT NULL
                   CHECK (type IN ('complaint', 'question', 'compliment', 'suggestion', 'review')),
    health_flag    INTEGER NOT NULL DEFAULT 0,         -- 1 = possible health signal, always urgent
    competitor_pro TEXT,                               -- competitor items: praised point
    competitor_con TEXT,                               -- competitor items: mentioned complaint
    confidence     REAL,
    model          TEXT,
    analyzed_at    TEXT NOT NULL
);
CREATE INDEX idx_analyses_urgency ON analyses(urgency);
CREATE INDEX idx_analyses_health ON analyses(health_flag);

-- Theme taxonomy, seeded from pulse-taxonomy.seed.yaml and fully
-- user-managed afterwards (Beheer). Slugs are the values the AI returns.
CREATE TABLE themes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL UNIQUE,                  -- e.g. 'werking-inslapen'
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'archived'))
);

-- Generated weekly reports.
CREATE TABLE digests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start TEXT NOT NULL,                        -- ISO date, inclusive
    period_end   TEXT NOT NULL,                        -- ISO date, inclusive
    generated_at TEXT NOT NULL,
    md_path      TEXT,
    html_path    TEXT,
    summary_json TEXT                                  -- counts snapshot for the dashboard
);

-- Audit trail of every collect / analyze / import / cleanup run.
CREATE TABLE runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL
                   CHECK (kind IN ('collect', 'analyze', 'import', 'cleanup')),
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    ok             INTEGER,                            -- 1 = no errors, 0 = errors
    items_seen     INTEGER NOT NULL DEFAULT 0,         -- fetched, incl. already-known
    items_new      INTEGER NOT NULL DEFAULT 0,
    items_analyzed INTEGER NOT NULL DEFAULT 0,
    items_deleted  INTEGER NOT NULL DEFAULT 0,         -- retention cleanup
    errors         TEXT,                               -- JSON array of error strings
    detail         TEXT                                -- JSON per-source results
);

-- Small user-editable settings (Beheer), e.g. retention_months.
CREATE TABLE app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
