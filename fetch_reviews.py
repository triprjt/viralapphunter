from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from google_play_scraper import Sort, app as gp_app, reviews as gp_reviews


SORT_MAP = {
    "newest": Sort.NEWEST,
    "rating": Sort.RATING,
    "helpful": Sort.MOST_RELEVANT,
}


def load_config(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


REVIEWS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
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
CREATE INDEX IF NOT EXISTS idx_{table}_pkg_posted ON {table}(package_name, posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_{table}_rating     ON {table}(package_name, rating);
"""


def safe_table_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c == "_" else "_" for c in name.lower())
    if not cleaned or not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = "t_" + cleaned
    return cleaned


def init_db(db_path: Path, table: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript((Path(__file__).parent / "schema.sql").read_text())
    conn.executescript(REVIEWS_TABLE_DDL.format(table=table))
    conn.commit()
    return conn


def to_iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def normalize(item: dict, package_name: str, filters: dict) -> dict:
    return {
        "review_id": item.get("reviewId"),
        "package_name": package_name,
        "user_name": item.get("userName"),
        "rating": item.get("score"),
        "text": item.get("content"),
        "posted_at": to_iso(item.get("at")),
        "thumbs_up": item.get("thumbsUpCount") or 0,
        "reply_text": item.get("replyContent"),
        "reply_at": to_iso(item.get("repliedAt")),
        "app_version": item.get("reviewCreatedVersion"),
        "country": filters.get("country"),
        "language": filters.get("language"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def upsert_reviews(conn: sqlite3.Connection, table: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    sql = f"""
        INSERT INTO {table} (review_id, package_name, user_name, rating, text,
                             posted_at, thumbs_up, reply_text, reply_at,
                             app_version, country, language, fetched_at)
        VALUES (:review_id, :package_name, :user_name, :rating, :text,
                :posted_at, :thumbs_up, :reply_text, :reply_at,
                :app_version, :country, :language, :fetched_at)
        ON CONFLICT(review_id) DO UPDATE SET
            rating=excluded.rating,
            text=excluded.text,
            thumbs_up=excluded.thumbs_up,
            reply_text=excluded.reply_text,
            reply_at=excluded.reply_at,
            fetched_at=excluded.fetched_at
    """
    valid = [r for r in rows if r["review_id"]]
    conn.executemany(sql, valid)
    conn.commit()
    return len(valid)


def upsert_app(conn: sqlite3.Connection, package_name: str, title: str | None) -> None:
    conn.execute(
        """
        INSERT INTO apps (package_name, title, last_fetched)
        VALUES (?, ?, ?)
        ON CONFLICT(package_name) DO UPDATE SET
            title=COALESCE(excluded.title, apps.title),
            last_fetched=excluded.last_fetched
        """,
        (package_name, title, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def fetch_app_title(package_name: str, country: str, language: str) -> str | None:
    try:
        info = gp_app(package_name, lang=language, country=country)
        return info.get("title")
    except Exception as e:
        print(f"[{package_name}] title fetch failed: {e}", file=sys.stderr)
        return None


def fetch_reviews(package_name: str, filters: dict) -> list[dict]:
    country = filters.get("country", "us")
    language = filters.get("language", "en")
    sort = SORT_MAP.get(filters.get("sort", "newest"), Sort.NEWEST)
    max_reviews = int(filters.get("max_reviews_per_app", 1000))
    min_rating = filters.get("min_rating")
    max_rating = filters.get("max_rating")
    since = filters.get("since")
    since_dt = None
    if since:
        since_dt = datetime.fromisoformat(str(since))
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)

    collected: list[dict] = []
    token = None
    batch_size = 200

    while len(collected) < max_reviews:
        remaining = max_reviews - len(collected)
        count = min(batch_size, remaining)
        try:
            result, token = gp_reviews(
                package_name,
                lang=language,
                country=country,
                sort=sort,
                count=count,
                continuation_token=token,
            )
        except Exception as e:
            print(f"[{package_name}] fetch error: {e}", file=sys.stderr)
            break

        if not result:
            break

        stop = False
        for item in result:
            rating = item.get("score")
            if min_rating and rating is not None and rating < min_rating:
                continue
            if max_rating and rating is not None and rating > max_rating:
                continue
            posted = item.get("at")
            if since_dt and posted:
                posted_dt = posted if posted.tzinfo else posted.replace(tzinfo=timezone.utc)
                if posted_dt < since_dt:
                    if sort == Sort.NEWEST:
                        stop = True
                        break
                    continue
            collected.append(item)
            if len(collected) >= max_reviews:
                break

        if stop or token is None:
            break

    return collected


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Google Play reviews into SQLite")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config")
    parser.add_argument("--country", help="Override country code from config (e.g. in, us)")
    parser.add_argument("--language", help="Override language code from config")
    parser.add_argument("--table", help="Reviews table name (default: reviews_<country>)")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        cfg_path = Path("config.example.yaml")
    cfg = load_config(cfg_path)

    filters = cfg.get("filters", {})
    if args.country:
        filters["country"] = args.country
    if args.language:
        filters["language"] = args.language
    country = filters.get("country", "us")
    table = args.table or safe_table_name(f"reviews_{country}")
    db_path = Path(cfg.get("output", {}).get("db_path", "reviews.db"))
    conn = init_db(db_path, table)

    total = 0
    for package_name in cfg.get("apps", []):
        print(f"[{package_name}] fetching...")
        title = fetch_app_title(
            package_name, filters.get("country", "us"), filters.get("language", "en")
        )
        raw = fetch_reviews(package_name, filters)
        rows = [normalize(item, package_name, filters) for item in raw]
        upsert_app(conn, package_name, title)
        n = upsert_reviews(conn, table, rows)
        total += n
        print(f"[{package_name}] stored {n} reviews")

    conn.close()
    print(f"done. {total} reviews into {db_path} (table: {table})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
