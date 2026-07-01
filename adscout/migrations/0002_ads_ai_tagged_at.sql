-- Marker for AI tag suggestions: set once an ad has been analyzed,
-- regardless of whether the analysis produced suggestions. Prevents
-- `adscout tag suggest` from re-analyzing (and re-paying for) the same
-- ads on every run.

ALTER TABLE ads ADD COLUMN ai_tagged_at TEXT;
