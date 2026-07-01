-- AdScout initial schema.
-- Never change an applied migration; add a new numbered file instead.

CREATE TABLE advertisers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    category    TEXT,
    countries   TEXT NOT NULL DEFAULT 'NL',          -- comma-separated ISO codes
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'paused')),
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE advertiser_pages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    advertiser_id INTEGER NOT NULL REFERENCES advertisers(id) ON DELETE CASCADE,
    page_id       TEXT NOT NULL,
    page_name     TEXT,
    added_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (advertiser_id, page_id)
);

CREATE TABLE ads (
    ad_archive_id     TEXT PRIMARY KEY,               -- Meta's Ad Library ID
    advertiser_id     INTEGER NOT NULL REFERENCES advertisers(id),
    page_id           TEXT,
    first_seen        TEXT NOT NULL,                  -- ISO date (UTC) we first saw it
    last_seen         TEXT NOT NULL,                  -- ISO date (UTC) we last saw it
    ad_creation_time  TEXT,
    ad_delivery_start TEXT,
    ad_delivery_stop  TEXT,
    status            TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'inactive')),
    format            TEXT NOT NULL DEFAULT 'unknown'
                      CHECK (format IN ('image', 'video', 'carousel', 'unknown')),
    snapshot_url      TEXT,
    landing_url       TEXT,
    platforms         TEXT,                           -- JSON array
    languages         TEXT,                           -- JSON array
    countries         TEXT,                           -- JSON array: where we saw it
    eu_reach_latest   INTEGER,
    raw_json_latest   TEXT
);
CREATE INDEX idx_ads_advertiser ON ads(advertiser_id);
CREATE INDEX idx_ads_status ON ads(status);
CREATE INDEX idx_ads_first_seen ON ads(first_seen);

-- ad_creative_bodies can hold multiple text variants; store them all.
CREATE TABLE ad_texts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id         TEXT NOT NULL REFERENCES ads(ad_archive_id) ON DELETE CASCADE,
    variant_index INTEGER NOT NULL,
    body          TEXT,
    title         TEXT,
    caption       TEXT,
    description   TEXT,
    UNIQUE (ad_id, variant_index)
);

-- One row per ad per day: the history the Ad Library itself forgets.
CREATE TABLE ad_snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id     TEXT NOT NULL REFERENCES ads(ad_archive_id) ON DELETE CASCADE,
    seen_at   TEXT NOT NULL,                          -- ISO date (UTC)
    status    TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
    eu_reach  INTEGER,
    raw_json  TEXT,
    UNIQUE (ad_id, seen_at)
);
CREATE INDEX idx_snapshots_seen_at ON ad_snapshots(seen_at);

-- Downloaded media. Meta media URLs expire, so we fetch on first sight.
-- sha256 deduplicates: several ads sharing one creative is a signal.
CREATE TABLE creatives (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id         TEXT NOT NULL REFERENCES ads(ad_archive_id) ON DELETE CASCADE,
    media_type    TEXT NOT NULL CHECK (media_type IN ('image', 'video', 'video_thumbnail')),
    source_url    TEXT,
    local_path    TEXT,
    sha256        TEXT,
    downloaded_at TEXT,
    UNIQUE (ad_id, source_url)
);
CREATE INDEX idx_creatives_sha256 ON creatives(sha256);

-- Both advertiser categories and ad tags, fully user-managed.
CREATE TABLE categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('advertiser_category', 'ad_tag')),
    tag_group   TEXT,                                 -- hook/offer/format/angle/audience
    description TEXT,
    UNIQUE (name, type)
);

CREATE TABLE ad_tags (
    ad_id       TEXT NOT NULL REFERENCES ads(ad_archive_id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    source      TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'ai')),
    confidence  REAL,
    status      TEXT NOT NULL DEFAULT 'accepted'
                CHECK (status IN ('accepted', 'suggested', 'rejected')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (ad_id, category_id)
);

-- Audit trail of every collect run.
CREATE TABLE runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,                              -- 1 = no errors, 0 = errors
    ads_fetched INTEGER NOT NULL DEFAULT 0,
    new_ads     INTEGER NOT NULL DEFAULT 0,
    stopped_ads INTEGER NOT NULL DEFAULT 0,
    errors      TEXT,                                 -- JSON array of error strings
    detail      TEXT                                  -- JSON per-advertiser results
);
