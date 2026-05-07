from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from google_play_scraper import search


CATEGORIES_FILE = Path(__file__).parent / "categories.json"
FAILURES_FILE = Path(__file__).parent / "discovery_failures.json"


def load_categories() -> list[dict]:
    with CATEGORIES_FILE.open() as f:
        return json.load(f)


def get_category(category_id: str) -> dict | None:
    for c in load_categories():
        if c["id"] == category_id:
            return c
    return None


# Backwards-compat for any external callers; the canonical source is categories.json.
DEFAULT_KEYWORDS = list((get_category("SPIRITUAL_RELIGIOUS") or {"keywords": []}).get("keywords", []))


DDL = """
CREATE TABLE IF NOT EXISTS discovered_apps (
  package_name   TEXT PRIMARY KEY,
  title          TEXT,
  developer      TEXT,
  score          REAL,
  installs       TEXT,
  free           INTEGER,
  price          REAL,
  currency       TEXT,
  icon           TEXT,
  summary        TEXT,
  matched_terms  TEXT,
  categories     TEXT,
  country        TEXT,
  language       TEXT,
  discovered_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discovered_score ON discovered_apps(score DESC);

CREATE TABLE IF NOT EXISTS category_runs (
  category_id   TEXT PRIMARY KEY,
  last_run_at   TEXT NOT NULL,
  app_count     INTEGER NOT NULL,
  country       TEXT,
  language      TEXT
);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.executescript(DDL)
    # Defensive: add the categories column if a pre-existing DB doesn't have it.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(discovered_apps)").fetchall()}
    if "categories" not in cols:
        conn.execute("ALTER TABLE discovered_apps ADD COLUMN categories TEXT")
    conn.commit()
    return conn


def _csv_add(existing: str | None, value: str) -> str:
    parts = [p for p in (existing or "").split(",") if p]
    if value not in parts:
        parts.append(value)
    return ",".join(parts)


def upsert(conn: sqlite3.Connection, items: dict[str, dict]) -> int:
    """Upsert; merges matched_terms and categories rather than overwriting."""
    if not items:
        return 0
    cur = conn.cursor()
    for r in items.values():
        existing = cur.execute(
            "SELECT matched_terms, categories FROM discovered_apps WHERE package_name = ?",
            (r["package_name"],),
        ).fetchone()
        if existing:
            old_terms, old_cats = existing
            merged_terms = old_terms or ""
            for t in (r.get("matched_terms") or "").split(","):
                if t:
                    merged_terms = _csv_add(merged_terms, t)
            merged_cats = old_cats or ""
            for c in (r.get("categories") or "").split(","):
                if c:
                    merged_cats = _csv_add(merged_cats, c)
            r["matched_terms"] = merged_terms
            r["categories"] = merged_cats
    sql = """
        INSERT INTO discovered_apps
          (package_name, title, developer, score, installs, free, price, currency,
           icon, summary, matched_terms, categories, country, language, discovered_at)
        VALUES
          (:package_name, :title, :developer, :score, :installs, :free, :price, :currency,
           :icon, :summary, :matched_terms, :categories, :country, :language, :discovered_at)
        ON CONFLICT(package_name) DO UPDATE SET
          title=excluded.title,
          developer=excluded.developer,
          score=excluded.score,
          installs=excluded.installs,
          free=excluded.free,
          price=excluded.price,
          currency=excluded.currency,
          icon=excluded.icon,
          summary=excluded.summary,
          matched_terms=excluded.matched_terms,
          categories=excluded.categories,
          discovered_at=excluded.discovered_at
    """
    conn.executemany(sql, list(items.values()))
    conn.commit()
    return len(items)


def _record_failure(category_id: str, keyword: str, error: str) -> None:
    """Append a failure record to discovery_failures.json so it can be retried later."""
    entry = {
        "category_id": category_id,
        "keyword": keyword,
        "error": error[:300],
        "at": datetime.now(timezone.utc).isoformat(),
    }
    existing = []
    if FAILURES_FILE.exists():
        try:
            existing = json.loads(FAILURES_FILE.read_text())
        except Exception:
            existing = []
    existing.append(entry)
    FAILURES_FILE.write_text(json.dumps(existing, indent=2))


def _clear_failures(category_id: str | None = None, keyword: str | None = None) -> None:
    if not FAILURES_FILE.exists():
        return
    try:
        existing = json.loads(FAILURES_FILE.read_text())
    except Exception:
        return
    remaining = [
        e for e in existing
        if not (
            (category_id is None or e.get("category_id") == category_id)
            and (keyword is None or e.get("keyword") == keyword)
        )
    ]
    FAILURES_FILE.write_text(json.dumps(remaining, indent=2))


def discover_category(
    conn: sqlite3.Connection,
    category_id: str,
    country: str = "in",
    language: str = "en",
    n_hits: int = 30,
    progress: bool = True,
) -> dict:
    """Scrape Play Store for the keywords associated with a category and upsert results."""
    cat = get_category(category_id)
    if not cat:
        raise ValueError(f"unknown category id: {category_id}")
    keywords = cat["keywords"]
    now = datetime.now(timezone.utc).isoformat()

    discovered: dict[str, dict] = {}
    for kw in keywords:
        try:
            results = search(kw, lang=language, country=country, n_hits=n_hits)
        except Exception as e:
            print(f"[{kw}] error: {e}", file=sys.stderr)
            _record_failure(category_id, kw, str(e))
            continue
        if progress:
            print(f"[{category_id}/{kw}] {len(results)} hits")
        for r in results:
            pkg = r.get("appId")
            if not pkg:
                continue
            existing = discovered.get(pkg)
            terms = set((existing.get("matched_terms") or "").split(",")) if existing else set()
            terms.discard("")
            terms.add(kw)
            cats = set((existing.get("categories") or "").split(",")) if existing else set()
            cats.discard("")
            cats.add(category_id)
            discovered[pkg] = {
                "package_name": pkg,
                "title": r.get("title"),
                "developer": r.get("developer"),
                "score": r.get("score"),
                "installs": r.get("installs"),
                "free": 1 if r.get("free") else 0,
                "price": r.get("price"),
                "currency": r.get("currency"),
                "icon": r.get("icon"),
                "summary": r.get("summary") or r.get("description"),
                "matched_terms": ",".join(sorted(terms)),
                "categories": ",".join(sorted(cats)),
                "country": country,
                "language": language,
                "discovered_at": now,
            }

    n = upsert(conn, discovered)
    conn.execute(
        """
        INSERT INTO category_runs (category_id, last_run_at, app_count, country, language)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(category_id) DO UPDATE SET
          last_run_at = excluded.last_run_at,
          app_count   = excluded.app_count,
          country     = excluded.country,
          language    = excluded.language
        """,
        (category_id, now, n, country, language),
    )
    conn.commit()
    return {"category_id": category_id, "found": n, "package_names": list(discovered.keys())}


def migrate_backfill(conn: sqlite3.Connection) -> int:
    """Backfill `categories` for already-discovered apps using categories.json keyword maps,
    and seed category_runs for any category that already has discovered apps."""
    cats = load_categories()
    rows = conn.execute("SELECT package_name, matched_terms, categories FROM discovered_apps").fetchall()
    n_updated = 0
    for pkg, terms_csv, cats_csv in rows:
        terms = {t for t in (terms_csv or "").split(",") if t}
        if not terms:
            continue
        existing_cats = {c for c in (cats_csv or "").split(",") if c}
        new_cats = set(existing_cats)
        for c in cats:
            if terms & set(c["keywords"]):
                new_cats.add(c["id"])
        if new_cats != existing_cats:
            conn.execute(
                "UPDATE discovered_apps SET categories = ? WHERE package_name = ?",
                (",".join(sorted(new_cats)), pkg),
            )
            n_updated += 1
    conn.commit()

    # Seed category_runs so the frontend recognizes which categories are already populated.
    now = datetime.now(timezone.utc).isoformat()
    for c in cats:
        count = conn.execute(
            "SELECT COUNT(*) FROM discovered_apps WHERE (',' || COALESCE(categories,'') || ',') LIKE ?",
            (f"%,{c['id']},%",),
        ).fetchone()[0]
        if count > 0:
            conn.execute(
                """
                INSERT INTO category_runs (category_id, last_run_at, app_count, country, language)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(category_id) DO UPDATE SET
                  app_count = excluded.app_count
                """,
                (c["id"], now, count, "in", "en"),
            )
    conn.commit()
    return n_updated


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover Google Play apps by keyword search, scoped to curated categories")
    parser.add_argument("--db", default="reviews.db", help="SQLite path")
    parser.add_argument("--country", default="in")
    parser.add_argument("--language", default="en")
    parser.add_argument("--n-hits", type=int, default=30, help="Results per keyword (max ~30)")
    parser.add_argument("--category", help="Category id from categories.json (e.g. SPIRITUAL_RELIGIOUS, HEALTH_FITNESS)")
    parser.add_argument("--all", action="store_true", help="Run discovery for every category")
    parser.add_argument("--keywords", nargs="*", help="Override: search arbitrary keywords (no category tag)")
    parser.add_argument("--migrate", action="store_true", help="Backfill the categories CSV column for existing apps")
    parser.add_argument("--list", action="store_true", help="Print available categories and exit")
    parser.add_argument("--retry-failed", action="store_true", help="Re-attempt every (category,keyword) listed in discovery_failures.json")
    parser.add_argument("--no-enrich", action="store_true", help="Skip the post-discovery enrichment step")
    parser.add_argument("--workers", type=int, default=12, help="Workers for the post-discovery enrichment step")
    args = parser.parse_args()

    if args.list:
        for c in load_categories():
            print(f"{c['id']:30} {c['name']} ({len(c['keywords'])} keywords)")
        return 0

    conn = init_db(Path(args.db))

    if args.migrate:
        n = migrate_backfill(conn)
        print(f"backfilled categories on {n} rows")
        conn.close()
        return 0

    if args.retry_failed:
        if not FAILURES_FILE.exists():
            print("no discovery_failures.json — nothing to retry")
            conn.close()
            return 0
        failures = json.loads(FAILURES_FILE.read_text())
        if not failures:
            print("nothing to retry (failures list is empty)")
            conn.close()
            return 0

        # Split into kinds:
        #   - enrich_pkgs: packages that failed during enrichment (category_id == "<enrich>")
        #   - keyword_failures: dict of category_id -> set(keyword) for search-time errors
        enrich_pkgs = set()
        keyword_failures: dict[str, set[str]] = {}
        for f in failures:
            cat = f.get("category_id", "")
            kw = f.get("keyword", "")
            if cat in ("<enrich>",):
                if kw and kw != "<phase>":
                    enrich_pkgs.add(kw)
            elif cat == "<category-level>":
                # whole category failed; treat as discovery retry by remembering its id
                keyword_failures.setdefault(kw or "", set())
            else:
                if cat and kw and kw != "<category-level>":
                    keyword_failures.setdefault(cat, set()).add(kw)

        # Reset the file; new failures get re-recorded.
        FAILURES_FILE.write_text("[]")

        if keyword_failures:
            print(f"retrying {sum(len(v) for v in keyword_failures.values())} keyword(s) across {len(keyword_failures)} categories")
            for cat_id, kws in keyword_failures.items():
                if not cat_id:
                    continue
                try:
                    discover_category(conn, cat_id, args.country, args.language, args.n_hits, progress=False)
                except Exception as e:
                    for kw in (kws or {"<category>"}):
                        _record_failure(cat_id, kw, str(e))
                    print(f"[retry {cat_id}] still failing: {e}", file=sys.stderr)

        if enrich_pkgs:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            from enrich_apps import VIEWS as ENRICH_VIEWS, fetch_one as enrich_fetch_one, upsert_one as enrich_upsert_one
            print(f"retrying enrichment for {len(enrich_pkgs)} package(s)")
            done = ok = 0
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futs = {ex.submit(enrich_fetch_one, p, args.language, args.country): p for p in enrich_pkgs}
                for fut in as_completed(futs):
                    pkg = futs[fut]
                    try:
                        row = fut.result()
                        enrich_upsert_one(conn, row)
                        if not row.get("fetch_error"):
                            ok += 1
                    except Exception as e:
                        _record_failure("<enrich>", pkg, str(e))
                    done += 1
                    if done % 25 == 0:
                        conn.commit()
                        print(f"  {done}/{len(enrich_pkgs)} (ok={ok})", file=sys.stderr)
            conn.commit()
            conn.executescript(ENRICH_VIEWS)
            conn.commit()
            print(f"  enrichment retry done: {ok}/{len(enrich_pkgs)} successful")

        # Final summary
        remaining = json.loads(FAILURES_FILE.read_text())
        print(f"\nretry complete. {len(remaining)} failure(s) still outstanding.")
        conn.close()
        return 0

    if args.all:
        cats = load_categories()
        total = 0
        succeeded, failed_cats = [], []
        for i, c in enumerate(cats, 1):
            print(f"\n=== [{i}/{len(cats)}] {c['id']} ({c['name']}) ===")
            try:
                res = discover_category(conn, c["id"], args.country, args.language, args.n_hits)
                total += res["found"]
                succeeded.append((c["id"], res["found"]))
            except Exception as e:
                print(f"[{c['id']}] FATAL: {e}", file=sys.stderr)
                _record_failure(c["id"], "<category-level>", str(e))
                failed_cats.append((c["id"], str(e)[:200]))

        print(f"\ndiscovery done. {total} apps across {len(succeeded)}/{len(cats)} categories. {len(failed_cats)} category-level failures.")
        if failed_cats:
            print("category-level failures:", failed_cats)

        if not args.no_enrich:
            print("\nrunning enrichment on all discovered apps (this is the long part)...")
            try:
                from enrich_apps import VIEWS as ENRICH_VIEWS, fetch_one as enrich_fetch_one, upsert_one as enrich_upsert_one
                from concurrent.futures import ThreadPoolExecutor, as_completed
                pkgs = [r[0] for r in conn.execute(
                    "SELECT d.package_name FROM discovered_apps d "
                    "LEFT JOIN apps_enriched e ON e.package_name = d.package_name "
                    "WHERE e.package_name IS NULL OR e.fetch_error IS NOT NULL"
                ).fetchall()]
                print(f"  enriching {len(pkgs)} apps with {args.workers} workers")
                done = 0
                with ThreadPoolExecutor(max_workers=args.workers) as ex:
                    futs = {ex.submit(enrich_fetch_one, p, args.language, args.country): p for p in pkgs}
                    for fut in as_completed(futs):
                        try:
                            row = fut.result()
                            enrich_upsert_one(conn, row)
                        except Exception as e:
                            _record_failure("<enrich>", futs[fut], str(e))
                        done += 1
                        if done % 100 == 0:
                            conn.commit()
                            print(f"    enriched {done}/{len(pkgs)}", file=sys.stderr)
                conn.commit()
                conn.executescript(ENRICH_VIEWS)
                conn.commit()
                print(f"  enrichment done")
            except Exception as e:
                print(f"enrichment phase failed: {e}", file=sys.stderr)
                _record_failure("<enrich>", "<phase>", str(e))
        conn.close()
        return 0

    if args.category:
        res = discover_category(conn, args.category, args.country, args.language, args.n_hits)
        print(f"\ndone. {res['found']} apps written for category {res['category_id']}")
        conn.close()
        return 0

    if args.keywords:
        # legacy mode: discover untagged apps
        now = datetime.now(timezone.utc).isoformat()
        discovered: dict[str, dict] = {}
        for kw in args.keywords:
            try:
                results = search(kw, lang=args.language, country=args.country, n_hits=args.n_hits)
            except Exception as e:
                print(f"[{kw}] error: {e}", file=sys.stderr)
                continue
            print(f"[{kw}] {len(results)} hits")
            for r in results:
                pkg = r.get("appId")
                if not pkg:
                    continue
                existing = discovered.get(pkg)
                terms = set((existing.get("matched_terms") or "").split(",")) if existing else set()
                terms.discard("")
                terms.add(kw)
                discovered[pkg] = {
                    "package_name": pkg,
                    "title": r.get("title"),
                    "developer": r.get("developer"),
                    "score": r.get("score"),
                    "installs": r.get("installs"),
                    "free": 1 if r.get("free") else 0,
                    "price": r.get("price"),
                    "currency": r.get("currency"),
                    "icon": r.get("icon"),
                    "summary": r.get("summary") or r.get("description"),
                    "matched_terms": ",".join(sorted(terms)),
                    "categories": "",
                    "country": args.country,
                    "language": args.language,
                    "discovered_at": now,
                }
        n = upsert(conn, discovered)
        print(f"\ndone. {n} unique apps")
        conn.close()
        return 0

    # default: re-run the spiritual category
    res = discover_category(conn, "SPIRITUAL_RELIGIOUS", args.country, args.language, args.n_hits)
    print(f"\ndone. {res['found']} apps")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
