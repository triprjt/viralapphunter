from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from google_play_scraper import app as gp_app


DDL = """
CREATE TABLE IF NOT EXISTS apps_enriched (
  package_name        TEXT PRIMARY KEY,
  title               TEXT,
  developer           TEXT,
  developer_id        TEXT,
  developer_website   TEXT,
  genre               TEXT,
  genre_id            TEXT,
  content_rating      TEXT,
  released            TEXT,            -- ISO date if parseable, else raw
  released_raw        TEXT,
  updated_at          TEXT,            -- ISO from `updated` epoch
  version             TEXT,
  recent_changes      TEXT,
  installs            TEXT,
  min_installs        INTEGER,         -- lower bound parsed from installs
  real_installs       INTEGER,
  score               REAL,
  ratings             INTEGER,
  reviews             INTEGER,
  histogram_1         INTEGER,
  histogram_2         INTEGER,
  histogram_3         INTEGER,
  histogram_4         INTEGER,
  histogram_5         INTEGER,
  price               REAL,
  free                INTEGER,
  currency            TEXT,
  offers_iap          INTEGER,
  iap_price_range     TEXT,
  ad_supported        INTEGER,
  contains_ads        INTEGER,
  editors_choice      INTEGER,
  size                TEXT,
  android_version     TEXT,
  similar_apps        TEXT,           -- comma-separated
  icon                TEXT,
  header_image        TEXT,
  privacy_policy      TEXT,
  enriched_at         TEXT NOT NULL,
  fetch_error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_apps_enriched_score    ON apps_enriched(score DESC);
CREATE INDEX IF NOT EXISTS idx_apps_enriched_installs ON apps_enriched(min_installs DESC);
CREATE INDEX IF NOT EXISTS idx_apps_enriched_genre    ON apps_enriched(genre_id);
"""


VIEWS = """
DROP VIEW IF EXISTS apps_ranked;
CREATE VIEW apps_ranked AS
SELECT
  e.package_name,
  e.title,
  e.developer,
  e.developer_id,
  e.genre,
  e.score,
  e.ratings,
  e.reviews,
  e.min_installs,
  e.installs,
  e.released,
  e.updated_at,
  e.histogram_1,
  e.histogram_2,
  e.histogram_3,
  e.histogram_4,
  e.histogram_5,
  e.contains_ads,
  e.ad_supported,
  e.offers_iap,
  e.iap_price_range,
  e.editors_choice,
  e.size,
  e.icon,
  d.matched_terms,
  d.categories,
  CASE
    WHEN e.released IS NOT NULL AND e.min_installs IS NOT NULL THEN
      CAST(e.min_installs AS REAL) /
      MAX(1.0, (julianday('now') - julianday(e.released)) / 30.0)
    ELSE NULL
  END AS installs_per_month,
  CASE
    WHEN e.updated_at IS NOT NULL THEN
      CAST(julianday('now') - julianday(e.updated_at) AS INTEGER)
    ELSE NULL
  END AS days_since_update
FROM apps_enriched e
LEFT JOIN discovered_apps d ON d.package_name = e.package_name
WHERE e.fetch_error IS NULL;

DROP VIEW IF EXISTS niche_saturation;
CREATE VIEW niche_saturation AS
WITH RECURSIVE
split(term, rest, package_name) AS (
  SELECT NULL, matched_terms || ',', package_name FROM discovered_apps
  UNION ALL
  SELECT
    substr(rest, 1, instr(rest, ',') - 1),
    substr(rest, instr(rest, ',') + 1),
    package_name
  FROM split WHERE rest != ''
)
SELECT
  s.term,
  COUNT(DISTINCT s.package_name) AS app_count,
  AVG(e.min_installs) AS avg_min_installs,
  MAX(e.min_installs) AS max_min_installs,
  AVG(e.score) AS avg_score,
  AVG(e.ratings) AS avg_ratings
FROM split s
LEFT JOIN apps_enriched e ON e.package_name = s.package_name
WHERE s.term IS NOT NULL AND s.term != ''
GROUP BY s.term
ORDER BY app_count DESC;
"""


def parse_installs(installs: str | None) -> int | None:
    if not installs:
        return None
    try:
        return int(str(installs).replace(",", "").replace("+", "").strip())
    except ValueError:
        return None


def parse_released(value) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    raw = str(value)
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat(), raw
        except ValueError:
            continue
    return None, raw


def parse_updated(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 1e12 else value
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    return None


def normalize(pkg: str, info: dict) -> dict:
    hist = info.get("histogram") or [None] * 5
    if not isinstance(hist, list) or len(hist) < 5:
        hist = (list(hist) + [None] * 5)[:5]
    released_iso, released_raw = parse_released(info.get("released"))
    return {
        "package_name": pkg,
        "title": info.get("title"),
        "developer": info.get("developer"),
        "developer_id": info.get("developerId"),
        "developer_website": info.get("developerWebsite"),
        "genre": info.get("genre"),
        "genre_id": info.get("genreId"),
        "content_rating": info.get("contentRating"),
        "released": released_iso,
        "released_raw": released_raw,
        "updated_at": parse_updated(info.get("updated")),
        "version": info.get("version"),
        "recent_changes": info.get("recentChanges"),
        "installs": info.get("installs"),
        "min_installs": info.get("minInstalls") or parse_installs(info.get("installs")),
        "real_installs": info.get("realInstalls"),
        "score": info.get("score"),
        "ratings": info.get("ratings"),
        "reviews": info.get("reviews"),
        "histogram_1": hist[0],
        "histogram_2": hist[1],
        "histogram_3": hist[2],
        "histogram_4": hist[3],
        "histogram_5": hist[4],
        "price": info.get("price"),
        "free": 1 if info.get("free") else 0,
        "currency": info.get("currency"),
        "offers_iap": 1 if info.get("offersIAP") else 0,
        "iap_price_range": info.get("inAppProductPrice"),
        "ad_supported": 1 if info.get("adSupported") else 0,
        "contains_ads": 1 if info.get("containsAds") else 0,
        "editors_choice": 1 if info.get("editorsChoice") else 0,
        "size": info.get("size"),
        "android_version": info.get("androidVersionText") or info.get("androidVersion"),
        "similar_apps": ",".join(info.get("similarApps") or []) if info.get("similarApps") else None,
        "icon": info.get("icon"),
        "header_image": info.get("headerImage"),
        "privacy_policy": info.get("privacyPolicy"),
        "enriched_at": datetime.now(timezone.utc).isoformat(),
        "fetch_error": None,
    }


UPSERT_SQL = f"""
INSERT INTO apps_enriched ({{cols}}) VALUES ({{placeholders}})
ON CONFLICT(package_name) DO UPDATE SET {{updates}}
"""


def upsert_one(conn: sqlite3.Connection, row: dict) -> None:
    cols = list(row.keys())
    placeholders = ",".join(f":{c}" for c in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "package_name")
    sql = UPSERT_SQL.format(cols=",".join(cols), placeholders=placeholders, updates=updates)
    conn.execute(sql, row)


def fetch_one(pkg: str, lang: str, country: str) -> dict:
    try:
        info = gp_app(pkg, lang=lang, country=country)
        return normalize(pkg, info)
    except Exception as e:
        return {
            "package_name": pkg,
            "enriched_at": datetime.now(timezone.utc).isoformat(),
            "fetch_error": str(e)[:300],
            **{k: None for k in [
                "title","developer","developer_id","developer_website","genre","genre_id",
                "content_rating","released","released_raw","updated_at","version","recent_changes",
                "installs","min_installs","real_installs","score","ratings","reviews",
                "histogram_1","histogram_2","histogram_3","histogram_4","histogram_5",
                "price","free","currency","offers_iap","iap_price_range","ad_supported",
                "contains_ads","editors_choice","size","android_version","similar_apps",
                "icon","header_image","privacy_policy",
            ]},
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich discovered_apps with full Play Store metadata")
    parser.add_argument("--db", default="reviews.db")
    parser.add_argument("--country", default="in")
    parser.add_argument("--language", default="en")
    parser.add_argument("--limit", type=int, default=0, help="Only enrich first N (0=all)")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent fetches")
    parser.add_argument("--skip-fresh", action="store_true", help="Skip apps already enriched")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.executescript(DDL)
    conn.commit()

    cur = conn.cursor()
    if args.skip_fresh:
        cur.execute("""
            SELECT d.package_name FROM discovered_apps d
            LEFT JOIN apps_enriched e ON e.package_name = d.package_name
            WHERE e.package_name IS NULL OR e.fetch_error IS NOT NULL
            ORDER BY d.package_name
        """)
    else:
        cur.execute("SELECT package_name FROM discovered_apps ORDER BY package_name")
    pkgs = [r[0] for r in cur.fetchall()]
    if args.limit:
        pkgs = pkgs[: args.limit]

    if not pkgs:
        print("nothing to enrich")
        return 0

    print(f"enriching {len(pkgs)} apps with {args.workers} workers...")
    done = 0
    errors = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(fetch_one, p, args.language, args.country): p for p in pkgs}
        for fut in as_completed(futures):
            row = fut.result()
            upsert_one(conn, row)
            done += 1
            if row.get("fetch_error"):
                errors += 1
            if done % 50 == 0 or done == len(pkgs):
                conn.commit()
                rate = done / max(1e-3, time.time() - t0)
                print(f"  {done}/{len(pkgs)} ({errors} errors, {rate:.1f}/s)", file=sys.stderr)
    conn.commit()

    print("creating views...")
    conn.executescript(VIEWS)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM apps_enriched WHERE fetch_error IS NULL")
    ok = cur.fetchone()[0]
    print(f"done. {ok} apps enriched ({errors} errors). views: apps_ranked, niche_saturation")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
