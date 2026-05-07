CREATE TABLE IF NOT EXISTS apps (
  package_name TEXT PRIMARY KEY,
  title        TEXT,
  last_fetched TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
  review_id    TEXT PRIMARY KEY,
  package_name TEXT NOT NULL REFERENCES apps(package_name),
  user_name    TEXT,
  rating       INTEGER,
  text         TEXT,
  posted_at    TEXT,
  thumbs_up    INTEGER,
  reply_text   TEXT,
  reply_at     TEXT,
  app_version  TEXT,
  country      TEXT,
  language     TEXT,
  fetched_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reviews_pkg_posted ON reviews(package_name, posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_reviews_rating     ON reviews(package_name, rating);
