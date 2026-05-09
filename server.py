from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google_play_scraper import search as gp_search

# Load .env if present (best-effort)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

import auth as auth_mod
import email_service as email_mod

from discover_apps import (
    DDL as DISCOVER_DDL,
    discover_category as run_discover_category,
    init_db as discover_init_db,
    load_categories,
)
from enrich_apps import VIEWS as ENRICH_VIEWS, DDL as ENRICH_DDL, fetch_one as enrich_fetch_one, upsert_one as enrich_upsert_one
from fetch_reviews import (
    REVIEWS_TABLE_DDL,
    fetch_app_title,
    fetch_reviews,
    init_db,
    normalize,
    safe_table_name,
    upsert_app,
    upsert_reviews,
)


import re

DB_PATH = Path("reviews.db")

# Per-app detail page URL: /app/<pkg>/details (with optional trailing slash and ?show=)
# pkg is a Java package name: lowercase letters/digits/dots/underscores/dashes.
_APP_DETAIL_RE = re.compile(r"^/app/([A-Za-z0-9._\-]+)/details/?$")


def _inject_vfuser(html: str, user: dict) -> str:
    """Inject the vf-user meta tag (and optional dark-theme attr) into an HTML shell.
    Used by /app, /dashboard, /app/<pkg>/details — every auth-gated page."""
    meta = (
        f'<meta name="vf-user" '
        f'data-email="{user["email"]}" '
        f'data-name="{user.get("name") or ""}" '
        f'data-picture="{user.get("picture") or ""}" '
        f'data-admin="{"1" if user.get("is_admin") else "0"}" '
        f'data-plan="{user.get("plan", "free")}" '
        f'data-onboarded="{"1" if user.get("onboarded") else "0"}" '
        f'data-categories="{user.get("picked_categories") or ""}" '
        f'data-goal="{user.get("picked_goal") or ""}" '
        f'data-theme="{user.get("theme") or "light"}" />'
    )
    if user.get("theme") == "dark":
        html = html.replace("<html", '<html data-theme="dark"', 1)
    return html.replace("</head>", meta + "</head>", 1)
DEFAULT_COUNTRY = "in"
DEFAULT_LANGUAGE = "en"
DEFAULT_TABLE = safe_table_name(f"reviews_{DEFAULT_COUNTRY}")
DEFAULT_FILTERS = {
    "country": DEFAULT_COUNTRY,
    "language": DEFAULT_LANGUAGE,
    "sort": "newest",
    "min_rating": 1,
    "max_rating": 5,
    "since": "2025-01-01",
    "max_reviews_per_app": 5000,
}

STATUS_DDL = """
CREATE TABLE IF NOT EXISTS app_review_status (
  package_name TEXT PRIMARY KEY,
  state        TEXT NOT NULL,         -- idle | fetching | done | error
  count        INTEGER DEFAULT 0,
  table_name   TEXT,
  started_at   TEXT,
  completed_at TEXT,
  error        TEXT
);
"""


_state_lock = threading.Lock()
_running: dict[str, threading.Thread] = {}

# ---- Reviews-fetch queue (bounded workers so we don't hammer Play Store) ----
NUM_FETCH_WORKERS = 2
# Items in the queue are (pkg, user_id_or_None). Workers fire first-fetch email if a user is attached.
_fetch_queue: queue.Queue = queue.Queue()
_workers_started = False


def _ensure_fetch_workers() -> None:
    global _workers_started
    with _state_lock:
        if _workers_started:
            return
        _workers_started = True
        for _ in range(NUM_FETCH_WORKERS):
            t = threading.Thread(target=_fetch_worker_loop, daemon=True)
            t.start()


def _fetch_worker_loop() -> None:
    while True:
        item = _fetch_queue.get()
        pkg, user_id = (item if isinstance(item, tuple) else (item, None))
        try:
            _fetch_worker(pkg)
            # On success, fire first-fetch trigger if user attached
            if user_id:
                conn = _conn()
                try:
                    row = conn.execute(
                        "SELECT count, state FROM app_review_status WHERE package_name=?", (pkg,)
                    ).fetchone()
                    user_row = conn.execute(
                        "SELECT id, email, name, picture, plan, is_admin FROM users WHERE id=?",
                        (user_id,),
                    ).fetchone()
                finally:
                    conn.close()
                if row and row[1] == "done":
                    # Refresh sentiment + Q&A for this app — the new reviews will
                    # update the "why loved / hate / verdict" answers next time
                    # someone views the detail page.
                    try:
                        import poller as _poller
                        _poller.refresh_sentiment_and_qa([pkg])
                    except Exception as e:
                        print(f"[fetch_worker] sentiment refresh failed for {pkg}: {e}", flush=True)
                    if user_row:
                        user = {
                            "id": user_row[0], "email": user_row[1], "name": user_row[2],
                            "picture": user_row[3], "plan": user_row[4], "is_admin": bool(user_row[5]),
                        }
                        try:
                            email_mod.fire_first_fetch(user, pkg, int(row[0] or 0))
                        except Exception:
                            pass
        except Exception as e:
            _set_status(pkg, state="error", error=str(e)[:300])
        finally:
            _fetch_queue.task_done()


def _conn() -> sqlite3.Connection:
    # timeout=30 means Python retries internally for up to 30s on lock contention.
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")  # belt + suspenders
    return c


def _ensure_schema() -> None:
    auth_mod.init_schema()  # users, sessions, email_log, ...
    conn = _conn()
    try:
        conn.executescript(STATUS_DDL)
        conn.executescript(REVIEWS_TABLE_DDL.format(table=DEFAULT_TABLE))
        conn.executescript(ENRICH_DDL)
        conn.executescript(DISCOVER_DDL)
        # Defensive ALTER for older DBs missing the categories column.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(discovered_apps)").fetchall()}
        if "categories" not in cols:
            conn.execute("ALTER TABLE discovered_apps ADD COLUMN categories TEXT")
        conn.commit()
    finally:
        conn.close()


def _discovery_progress() -> dict:
    """Snapshot of the long-running `discover_apps.py --all` job.
    Computed from DB state (no IPC with the discover process)."""
    pid_file = Path("/tmp/discover_all.pid")
    pid, alive = None, False
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # raises if process gone
            alive = True
        except (ValueError, OSError, ProcessLookupError):
            alive = False

    cats = load_categories()
    conn = _conn()
    try:
        n_discovered = conn.execute("SELECT COUNT(*) FROM discovered_apps").fetchone()[0]
        n_enriched_ok = conn.execute("SELECT COUNT(*) FROM apps_enriched WHERE fetch_error IS NULL").fetchone()[0]
        n_enriched_err = conn.execute("SELECT COUNT(*) FROM apps_enriched WHERE fetch_error IS NOT NULL").fetchone()[0]
        run_rows = conn.execute(
            "SELECT category_id, app_count, last_run_at FROM category_runs ORDER BY last_run_at"
        ).fetchall()
        # Apps yet to enrich = discovered_apps minus enriched
        n_pending = conn.execute(
            "SELECT COUNT(*) FROM discovered_apps d "
            "LEFT JOIN apps_enriched e ON e.package_name = d.package_name "
            "WHERE e.package_name IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()

    failures = []
    fp = Path("discovery_failures.json")
    if fp.exists():
        try:
            failures = json.loads(fp.read_text())
        except Exception:
            failures = []

    n_enriched_total = n_enriched_ok + n_enriched_err
    n_total_target = n_enriched_total + n_pending

    # Phase inference
    if not alive and run_rows and len(run_rows) >= len(cats) and n_pending == 0:
        phase = "done"
    elif alive and n_pending > 0:
        phase = "enrichment"
    elif alive:
        phase = "discovery"
    elif not alive and n_pending > 0:
        phase = "stalled"
    else:
        phase = "idle"

    return {
        "running": alive,
        "pid": pid,
        "phase": phase,
        "n_discovered": n_discovered,
        "n_enriched_ok": n_enriched_ok,
        "n_enriched_err": n_enriched_err,
        "n_enrichment_pending": n_pending,
        "n_enrichment_target": n_total_target,
        "categories_total": len(cats),
        "categories_done": len(run_rows),
        "category_runs": [
            {"category_id": r[0], "app_count": r[1], "last_run_at": r[2]} for r in run_rows
        ],
        "failures_count": len(failures),
    }


def _list_categories_with_status() -> list[dict]:
    """Returns categories.json contents enriched with cache status from category_runs."""
    cats = load_categories()
    conn = _conn()
    try:
        runs = {
            row[0]: {"last_run_at": row[1], "app_count": row[2]}
            for row in conn.execute("SELECT category_id, last_run_at, app_count FROM category_runs").fetchall()
        }
    finally:
        conn.close()
    out = []
    for c in cats:
        run = runs.get(c["id"])
        out.append({
            "id": c["id"],
            "name": c["name"],
            "genre_ids": c.get("genre_ids", []),
            "keywords": c["keywords"],
            "cached": bool(run),
            "app_count": (run or {}).get("app_count", 0),
            "last_run_at": (run or {}).get("last_run_at"),
        })
    return out


def discover_category_full(cat_id: str) -> dict:
    """Discover apps for a category and enrich them so they appear in apps_ranked."""
    conn = _conn()
    try:
        # discover_category writes new rows + tags; relies on the DDL we already ensured.
        result = run_discover_category(conn, cat_id, DEFAULT_COUNTRY, DEFAULT_LANGUAGE, n_hits=30, progress=False)
        # enrich every package this category touched (cheap when already enriched - upsert).
        # Concurrent fetch (network-bound) + serial DB upsert.
        pkgs = result["package_names"]
        if pkgs:
            with ThreadPoolExecutor(max_workers=12) as ex:
                fetched = list(ex.map(lambda p: enrich_fetch_one(p, DEFAULT_LANGUAGE, DEFAULT_COUNTRY), pkgs))
            for row in fetched:
                enrich_upsert_one(conn, row)
        conn.commit()
        # rebuild views so niche_saturation includes any fresh keywords
        conn.executescript(ENRICH_VIEWS)
        conn.commit()
    finally:
        conn.close()
    return result


_DISCOVER_DDL = """
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
  country        TEXT,
  language       TEXT,
  discovered_at  TEXT NOT NULL
);
"""


def discover_developer(dev_id: str, dev_name: str) -> dict:
    """Search Google Play for the developer's apps, upsert into discovered_apps + enrich.

    Strategy: search by developer name (Play has no public developer API in this lib),
    then filter results to ones whose developerId matches. Captures the prominent
    apps; long-tail apps with low search rank may be missed.
    """
    candidates: dict[str, dict] = {}
    queries = list({dev_name, dev_id.split(":")[-1] if ":" in dev_id else dev_id})
    for q in queries:
        try:
            hits = gp_search(q, lang=DEFAULT_LANGUAGE, country=DEFAULT_COUNTRY, n_hits=50)
        except Exception:
            continue
        for h in hits:
            if h.get("developerId") != dev_id and (h.get("developer") or "").lower() != (dev_name or "").lower():
                continue
            pkg = h.get("appId")
            if not pkg:
                continue
            if pkg not in candidates:
                candidates[pkg] = h

    if not candidates:
        return {"found": 0, "package_names": []}

    now = datetime.now(timezone.utc).isoformat()

    conn = _conn()
    try:
        conn.executescript(_DISCOVER_DDL)
        for pkg, r in candidates.items():
            # Insert with empty matched_terms (developer filter uses developer_id, not matched_terms,
            # so we don't pollute niche_saturation with per-developer pseudo-niches).
            # On conflict, leave existing matched_terms untouched.
            conn.execute(
                """
                INSERT INTO discovered_apps
                  (package_name, title, developer, score, installs, free, price, currency,
                   icon, summary, matched_terms, country, language, discovered_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(package_name) DO UPDATE SET
                  discovered_at = excluded.discovered_at
                """,
                (
                    pkg, r.get("title"), r.get("developer"), r.get("score"),
                    r.get("installs"), 1 if r.get("free") else 0, r.get("price"),
                    r.get("currency"), r.get("icon"), r.get("summary") or r.get("description"),
                    "", DEFAULT_COUNTRY, DEFAULT_LANGUAGE, now,
                ),
            )
        conn.commit()

        # enrich (re-)all candidates so they show up in apps_ranked
        for pkg in candidates:
            row = enrich_fetch_one(pkg, DEFAULT_LANGUAGE, DEFAULT_COUNTRY)
            enrich_upsert_one(conn, row)
        conn.commit()

        # rebuild views so niche_saturation + apps_ranked include any new rows
        conn.executescript(ENRICH_VIEWS)
        conn.commit()
    finally:
        conn.close()

    return {"found": len(candidates), "package_names": list(candidates.keys())}


def _set_status(pkg: str, **fields) -> None:
    conn = _conn()
    try:
        cur = conn.execute("SELECT package_name FROM app_review_status WHERE package_name = ?", (pkg,))
        exists = cur.fetchone() is not None
        if exists:
            cols = ",".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE app_review_status SET {cols} WHERE package_name=?",
                         (*fields.values(), pkg))
        else:
            cols = ["package_name", *fields.keys()]
            placeholders = ",".join("?" * len(cols))
            conn.execute(f"INSERT INTO app_review_status ({','.join(cols)}) VALUES ({placeholders})",
                         (pkg, *fields.values()))
        conn.commit()
    finally:
        conn.close()


def _fetch_worker(pkg: str) -> None:
    started = datetime.now(timezone.utc).isoformat()
    _set_status(pkg, state="fetching", started_at=started, error=None)
    try:
        title = fetch_app_title(pkg, DEFAULT_COUNTRY, DEFAULT_LANGUAGE)
        raw = fetch_reviews(pkg, DEFAULT_FILTERS)
        rows = [normalize(item, pkg, DEFAULT_FILTERS) for item in raw]

        conn = _conn()
        try:
            upsert_app(conn, pkg, title)
            n = upsert_reviews(conn, DEFAULT_TABLE, rows)
        finally:
            conn.close()

        _set_status(
            pkg, state="done", count=n, table_name=DEFAULT_TABLE,
            completed_at=datetime.now(timezone.utc).isoformat(), error=None,
        )
    except Exception as e:
        _set_status(
            pkg, state="error", error=str(e)[:300],
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
    finally:
        with _state_lock:
            _running.pop(pkg, None)


_STALE_FETCH_MINUTES = 5  # rows older than this in queued/fetching state are considered orphaned


def _is_stale_fetch_row(state: str | None, started_at: str | None) -> bool:
    """A queued/fetching row is stale if its started_at is older than _STALE_FETCH_MINUTES,
    or if there's no started_at at all (meaning the worker died before claiming it)."""
    if state not in ("queued", "fetching"):
        return False
    if not started_at:
        # queued but never started → if we don't see it in the in-memory queue, it's stale
        return True
    try:
        t = datetime.fromisoformat(started_at)
        return (datetime.now(timezone.utc) - t) > timedelta(minutes=_STALE_FETCH_MINUTES)
    except Exception:
        return True


def _sweep_stale_fetches() -> int:
    """On server start (and any time we want), mark old in-flight rows as errored
    so the user can re-trigger the fetch. Without this, a server restart leaves
    every in-flight package permanently stuck because _start_fetch sees state='fetching'
    and refuses to re-queue."""
    conn = _conn()
    n = 0
    try:
        rows = conn.execute(
            "SELECT package_name, state, started_at FROM app_review_status WHERE state IN ('queued','fetching')"
        ).fetchall()
        for pkg, state, started_at in rows:
            if _is_stale_fetch_row(state, started_at):
                conn.execute(
                    "UPDATE app_review_status SET state='error', error='Worker died (server restart). Click retry to re-queue.', "
                    "completed_at=? WHERE package_name=?",
                    (datetime.now(timezone.utc).isoformat(), pkg),
                )
                n += 1
        conn.commit()
    finally:
        conn.close()
    if n:
        print(f"[startup] swept {n} stale in-flight fetch row(s)", flush=True)
    return n


def _start_fetch(pkg: str, user_id: int | None = None) -> dict:
    """Enqueue a reviews-fetch job. The bounded worker pool consumes it."""
    _ensure_fetch_workers()
    # Skip if already pending/running — but only if the row is FRESH. Stale rows
    # (server restart, worker died) get treated as not-running and re-queued.
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT state, started_at FROM app_review_status WHERE package_name = ?", (pkg,)
        ).fetchone()
    finally:
        conn.close()
    if row and row[0] in ("queued", "fetching") and not _is_stale_fetch_row(row[0], row[1]):
        return {"ok": True, "state": row[0], "already_running": True, "queue_size": _fetch_queue.qsize()}
    if row and row[0] in ("queued", "fetching"):
        # Stale — fall through to re-queue
        print(f"[fetch] re-queuing stale {pkg} (was {row[0]} since {row[1]})", flush=True)
    _set_status(pkg, state="queued", error=None, started_at=None, completed_at=None)
    _fetch_queue.put((pkg, user_id))
    return {"ok": True, "state": "queued", "queue_size": _fetch_queue.qsize()}


TIPS = [
    "Sort Niches by 'Best opportunity' to find under-served corners.",
    "Star apps in Viral Ranking — they show up on your Home dashboard.",
    "Use Saved Searches to recall a useful Viral Ranking filter.",
    "Newly-released apps with high install velocity are often the leading edge of a trend.",
    "Niches with fewer than 5 apps and any installs are signal — competition is missing.",
    "Read review snippets to learn the exact words your users will use.",
    "Check the weekly category report on Mondays — fresh winners every week.",
    "If three apps in a niche all spiked, the niche itself is rising.",
    "Filter Reviews to 1–2 star to find unmet needs you can build for.",
    "Export a CSV before changing categories — your scope changes the data.",
]


def _tip_for_today() -> str:
    """Deterministic by date — same tip for every user on a given day, no per-user storage."""
    return TIPS[datetime.now(timezone.utc).timetuple().tm_yday % len(TIPS)]


def _user_category_ids(user: dict) -> list[str]:
    raw = (user.get("picked_categories") or "").strip()
    if not raw:
        return []
    return [c for c in raw.split(",") if c]


def _top_viral_for_categories(category_ids: list[str], limit: int = 5) -> list[dict]:
    """Top apps ordered by installs_per_month (the server-side proxy for 'viral')."""
    if not category_ids:
        sql = (
            "SELECT package_name, title, icon, installs_per_month, categories "
            "FROM apps_ranked WHERE installs_per_month IS NOT NULL "
            "ORDER BY installs_per_month DESC LIMIT ?"
        )
        params = (limit,)
    else:
        clauses = " OR ".join("categories LIKE ?" for _ in category_ids)
        params = tuple(f"%{cid}%" for cid in category_ids) + (limit,)
        sql = (
            f"SELECT package_name, title, icon, installs_per_month, categories "
            f"FROM apps_ranked WHERE ({clauses}) AND installs_per_month IS NOT NULL "
            f"ORDER BY installs_per_month DESC LIMIT ?"
        )
    conn = _conn()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [
        {
            "package_name": r[0], "title": r[1], "icon": r[2],
            "installs_per_month": r[3], "categories": r[4],
        }
        for r in rows
    ]


def _niche_of_week_for(category_ids: list[str]) -> dict | None:
    """Best-opportunity niche from saved categories (or globally if none/no categories col)."""
    conn = _conn()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(niche_saturation)").fetchall()}
        if not cols:
            return None
        max_col = "max_min_installs" if "max_min_installs" in cols else "max_installs"
        has_categories = "categories" in cols
        where = f"app_count <= 8 AND {max_col} > 1000"
        params: tuple = ()
        if category_ids and has_categories:
            clauses = " OR ".join("categories LIKE ?" for _ in category_ids)
            where = f"({clauses}) AND " + where
            params = tuple(f"%{cid}%" for cid in category_ids)
        sql = (
            f"SELECT term, app_count, {max_col} FROM niche_saturation "
            f"WHERE {where} ORDER BY app_count ASC, {max_col} DESC LIMIT 1"
        )
        row = conn.execute(sql, params).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    if not row:
        return None
    return {"term": row[0], "app_count": row[1], "max_installs": row[2]}


def _user_fetched_reviews(user_id: int, limit: int = 25) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT context, used_at FROM feature_usage "
            "WHERE user_id=? AND feature='review_fetch' AND context != '' "
            "ORDER BY used_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            pkg = r[0]; used_at = r[1]
            # Try to get the review count from app_review_status
            try:
                status = conn.execute(
                    "SELECT count, state FROM app_review_status WHERE package_name=?", (pkg,)
                ).fetchone()
                review_count = status[0] if status else 0
                state = status[1] if status else "unknown"
            except Exception:
                review_count = 0; state = "unknown"
            out.append({
                "package_name": pkg, "used_at": used_at,
                "review_count": review_count, "state": state,
            })
    finally:
        conn.close()
    return out


def _iso_year_week(d: datetime) -> tuple[int, int]:
    iy, iw, _ = d.isocalendar()
    return iy, iw


def _weekly_reports_for(category_ids: list[str], year: int, week: int) -> list[dict]:
    """Returns {category_id, year, week, payload} for the most recent published week per category."""
    if not category_ids:
        return []
    out = []
    conn = _conn()
    try:
        for cid in category_ids:
            row = conn.execute(
                "SELECT category_id, year, week, generated_at, payload FROM weekly_reports "
                "WHERE category_id=? ORDER BY year DESC, week DESC LIMIT 1",
                (cid,),
            ).fetchone()
            if not row:
                continue
            try: payload = json.loads(row[4])
            except Exception: payload = {}
            out.append({
                "category_id": row[0], "year": row[1], "week": row[2],
                "generated_at": row[3], "payload": payload,
            })
    finally:
        conn.close()
    return out


def _generate_weekly_reports(force: bool = False) -> int:
    """Generate this-week's top-5 viral apps snapshot per category.

    Idempotent per ISO (year, week, category) thanks to the unique index.
    Returns the number of categories newly written.
    """
    now = datetime.now(timezone.utc)
    year, week = _iso_year_week(now)
    cats = load_categories()
    written = 0
    conn = _conn()
    try:
        for cat in cats:
            cid = cat["id"]
            existing = conn.execute(
                "SELECT id FROM weekly_reports WHERE category_id=? AND year=? AND week=?",
                (cid, year, week),
            ).fetchone()
            if existing and not force:
                continue
            top = conn.execute(
                "SELECT package_name, title, icon, installs_per_month "
                "FROM apps_ranked WHERE categories LIKE ? AND installs_per_month IS NOT NULL "
                "ORDER BY installs_per_month DESC LIMIT 5",
                (f"%{cid}%",),
            ).fetchall()
            # Find the previous week's snapshot for delta calc
            prev = conn.execute(
                "SELECT payload FROM weekly_reports WHERE category_id=? AND NOT (year=? AND week=?) "
                "ORDER BY year DESC, week DESC LIMIT 1",
                (cid, year, week),
            ).fetchone()
            prev_map = {}
            if prev:
                try:
                    prev_payload = json.loads(prev[0])
                    for app in prev_payload.get("apps", []):
                        prev_map[app["package_name"]] = app.get("installs_per_month") or 0
                except Exception:
                    pass
            apps = []
            for r in top:
                pkg, title, icon, installs = r
                prev_installs = prev_map.get(pkg)
                if prev_installs and installs:
                    delta_pct = ((installs - prev_installs) / prev_installs) * 100.0
                else:
                    delta_pct = None
                apps.append({
                    "package_name": pkg, "title": title, "icon": icon,
                    "installs_per_month": installs,
                    "delta_pct": delta_pct,
                })
            payload = {
                "category_id": cid, "category_name": cat["name"],
                "year": year, "week": week, "apps": apps,
            }
            if existing and force:
                conn.execute(
                    "UPDATE weekly_reports SET generated_at=?, payload=? WHERE id=?",
                    (now.isoformat(), json.dumps(payload), existing[0]),
                )
            else:
                conn.execute(
                    "INSERT INTO weekly_reports (category_id, year, week, generated_at, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (cid, year, week, now.isoformat(), json.dumps(payload)),
                )
            written += 1
        conn.commit()
    finally:
        conn.close()
    return written


# ---- Composite viral score + monthly_hot_apps (Beads 7, 15-17) ----

import math


def _normalize_log10(x: float | int | None, floor: float = 1000.0, ceil: float = 1e9) -> float:
    """Normalize an installs/month-style number to [0, 1] via log10."""
    if not x or x <= 0:
        return 0.0
    v = math.log10(max(x, floor))
    lo, hi = math.log10(floor), math.log10(ceil)
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def _normalize_clip(x: float | None, lo: float = -50.0, hi: float = 200.0) -> float:
    if x is None:
        return 0.5  # neutral when unknown
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def _recency_score(released_iso: str | None) -> float:
    if not released_iso:
        return 0.3
    try:
        d = datetime.fromisoformat(released_iso[:10]).replace(tzinfo=timezone.utc)
    except Exception:
        return 0.3
    days = max(0.0, (datetime.now(timezone.utc) - d).days)
    if days < 60:
        return 1.0
    if days < 365:
        return max(0.0, 1.0 - (days - 60) / 305.0)
    return 0.0


def _snapshot_low_star_pct(h1, h2, h3, h4, h5) -> float | None:
    h1 = h1 or 0; h2 = h2 or 0
    total = (h1 + h2 + (h3 or 0) + (h4 or 0) + (h5 or 0))
    if total < 10:
        return None
    return min(0.5, (h1 + h2) / total) / 0.5  # normalized to [0, 1]


def compute_momentum_30d(package_name: str) -> float | None:
    """Bead 15: 30-day install momentum from app_history.
    Returns (today_min_installs - 30d_ago_min_installs) / 30d_ago_min_installs * 100,
    clipped to [-50, 200]. Returns None when fewer than 25 days of history (cold-start)."""
    cutoff_old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    cutoff_floor = (datetime.now(timezone.utc) - timedelta(days=25)).isoformat()
    conn = _conn()
    try:
        # Have we been polling this package long enough?
        oldest = conn.execute(
            "SELECT MIN(captured_at) FROM app_history WHERE package_name=?",
            (package_name,),
        ).fetchone()
        if not oldest or not oldest[0] or oldest[0] > cutoff_floor:
            return None
        latest_row = conn.execute(
            "SELECT min_installs FROM app_history WHERE package_name=? "
            "ORDER BY captured_at DESC LIMIT 1",
            (package_name,),
        ).fetchone()
        old_row = conn.execute(
            "SELECT min_installs FROM app_history WHERE package_name=? AND captured_at <= ? "
            "ORDER BY captured_at DESC LIMIT 1",
            (package_name, cutoff_old),
        ).fetchone()
    finally:
        conn.close()
    if not latest_row or not latest_row[0] or not old_row or not old_row[0]:
        return None
    latest, old = latest_row[0], old_row[0]
    if old <= 0:
        return None
    delta_pct = (latest - old) / old * 100.0
    return max(-50.0, min(200.0, delta_pct))


def compute_low_star_30d(package_name: str) -> float | None:
    """Bead 16: (1-star + 2-star) / total reviews over the last 30 days from reviews_daily.
    Returns None when fewer than 50 reviews in the window (cold-start fallback to snapshot)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT SUM(rating_1) + SUM(rating_2), SUM(count) "
            "FROM reviews_daily WHERE package_name=? AND date >= ?",
            (package_name, cutoff),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[1] or row[1] < 50:
        return None
    low, total = (row[0] or 0), row[1]
    return low / total


# ---- Bead R3: install-velocity series for sparklines + hero growth chart ----

def compute_install_series(package_name: str, installs_per_month: float | None,
                            released_iso: str | None, days: int = 30) -> tuple[list[int], bool]:
    """Return (series, is_real). Real series uses app_history; synthetic curve uses
    a smooth ascending interpolation from the launch baseline up to today's installs/mo
    fraction. The synthetic curve is what the user sees in the cold-start period."""
    conn = _conn()
    real_points: list[tuple[str, int]] = []
    try:
        rows = conn.execute(
            "SELECT captured_at, min_installs FROM app_history "
            "WHERE package_name=? AND min_installs IS NOT NULL "
            "ORDER BY captured_at ASC",
            (package_name,),
        ).fetchall()
        real_points = [(r[0], r[1]) for r in rows]
    finally:
        conn.close()

    if len(real_points) >= 7:
        # Real path: project the historical min_installs to a "per-month delta" curve.
        # Use cumulative-min-installs deltas between adjacent samples normalized to monthly.
        return _real_series_from_history(real_points, days), True

    # Synthetic path: a smooth ease-out curve ending at installs_per_month.
    return _synthetic_series(installs_per_month or 0, released_iso, days), False


def _real_series_from_history(points: list[tuple[str, int]], days: int) -> list[int]:
    """Convert sparse (timestamp, cumulative_min_installs) samples into a daily series
    of "installs added per day". Linearly interpolate between samples."""
    from datetime import datetime as _dt
    parsed = []
    for ts, mi in points:
        try:
            t = _dt.fromisoformat(ts)
        except Exception:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        parsed.append((t, mi))
    if len(parsed) < 2:
        return [0] * days
    parsed.sort()
    end = parsed[-1][0]
    start = end - timedelta(days=days)
    series: list[int] = []
    # Walk one day at a time; for each day, interpolate cumulative min_installs at start+i and start+i+1, take diff.
    def interp(at: datetime) -> float:
        if at <= parsed[0][0]:
            return float(parsed[0][1])
        if at >= parsed[-1][0]:
            return float(parsed[-1][1])
        for i in range(len(parsed) - 1):
            t0, m0 = parsed[i]
            t1, m1 = parsed[i + 1]
            if t0 <= at <= t1:
                if t1 == t0:
                    return float(m1)
                ratio = (at - t0).total_seconds() / (t1 - t0).total_seconds()
                return m0 + (m1 - m0) * ratio
        return float(parsed[-1][1])
    prev = interp(start)
    for i in range(days):
        cur = interp(start + timedelta(days=i + 1))
        series.append(max(0, int(round(cur - prev))))
        prev = cur
    return series


def _synthetic_series(installs_per_month: float, released_iso: str | None, days: int) -> list[int]:
    """A smooth ease-out curve totaling roughly installs_per_month, with a small launch ramp
    if released < 90 days. Used until app_history has enough real data."""
    if installs_per_month <= 0:
        return [0] * days
    daily_avg = installs_per_month / 30.0
    # Ease shape: starts at 60% of avg, peaks at 140%
    series: list[int] = []
    for i in range(days):
        t = i / max(1, days - 1)
        # Ease: 0.6 + 0.8 * smoothstep(0..1)
        smooth = t * t * (3 - 2 * t)
        factor = 0.6 + 0.8 * smooth
        series.append(max(0, int(round(daily_avg * factor))))

    # Recency boost: if released < 60 days, taper the early values toward zero
    if released_iso:
        try:
            d = datetime.fromisoformat(released_iso[:10]).replace(tzinfo=timezone.utc)
            days_old = max(0, (datetime.now(timezone.utc) - d).days)
            if days_old < days:
                # Zero out points before launch
                pad = days - days_old
                for j in range(pad):
                    series[j] = 0
        except Exception:
            pass
    return series


# ---- Bead R5: rule-based "why it's rising" tag ----

RISING_TAGS = ("NEW SURGE", "MOMENTUM", "SUSTAINED", "OPPORTUNITY", "COOLING", "NEW RELEASE")


def _rising_tag(components: dict, released_iso: str | None) -> str:
    # Released < 30 days → NEW RELEASE
    if released_iso:
        try:
            d = datetime.fromisoformat(released_iso[:10]).replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - d).days < 30:
                return "NEW RELEASE"
        except Exception:
            pass
    momentum_input = components.get("momentum_input")
    if momentum_input is not None and momentum_input < -10:
        return "COOLING"
    velocity = components.get("velocity") or 0.0
    momentum = components.get("momentum") or 0.0
    recency = components.get("recency") or 0.0
    low_star = components.get("low_star") or 0.0
    # Recency strong + momentum > 0.5 → NEW SURGE
    if recency > 0.7 and momentum >= 0.5:
        return "NEW SURGE"
    if momentum > 0.6:
        return "MOMENTUM"
    if low_star > 0.4 and velocity > 0.3:
        return "OPPORTUNITY"
    if velocity > 0.5 and momentum >= 0.4:
        return "SUSTAINED"
    # Fallback: pick the dominant component
    dom = max(("velocity", velocity), ("momentum", momentum), ("recency", recency), ("low_star", low_star), key=lambda x: x[1])
    return {"velocity": "SUSTAINED", "momentum": "MOMENTUM", "recency": "NEW SURGE", "low_star": "OPPORTUNITY"}[dom[0]]


# ---- Bead R6: Fermi revenue estimate ----
# ARPU per active user per month, hand-tuned by genre family. Real revenue is enterprise-only;
# this is a rough order-of-magnitude estimate the UI labels "est."
_ARPU_BY_GENRE_KEY = {
    # Higher: finance, productivity tools, dating
    "FINANCE": 4.0,
    "BUSINESS": 3.0,
    "PRODUCTIVITY": 1.5,
    "DATING": 5.0,
    "EDUCATION": 1.0,
    # Mid: lifestyle, social, health
    "LIFESTYLE": 0.8,
    "HEALTH_AND_FITNESS": 1.5,
    "SOCIAL": 0.5,
    "COMMUNICATION": 0.4,
    "MEDICAL": 1.0,
    # Music/video/photo
    "MUSIC_AND_AUDIO": 0.6,
    "VIDEO_PLAYERS": 0.3,
    "PHOTOGRAPHY": 0.5,
    "ENTERTAINMENT": 0.4,
    # Tools/utility
    "TOOLS": 0.4,
    "MAPS_AND_NAVIGATION": 0.4,
    "TRAVEL_AND_LOCAL": 0.6,
    "WEATHER": 0.3,
    "BOOKS_AND_REFERENCE": 0.4,
    "NEWS_AND_MAGAZINES": 0.5,
    "FOOD_AND_DRINK": 0.6,
    "SHOPPING": 1.5,
    "SPORTS": 0.6,
    # Spiritual / kids
    "SPIRITUAL_RELIGIOUS": 0.5,
    "PARENTING": 1.2,
    # Games (fall-through default)
    "GAME": 0.5,
    "GAMES": 0.5,
}
_DEFAULT_ARPU = 0.4

# Active-user fraction of installs (rough; gives MAU from cumulative installs).
_ACTIVE_FRACTION = 0.10


def compute_revenue_estimate(genre: str | None, installs_per_month: float | None,
                              contains_ads: int | None = None, offers_iap: int | None = None,
                              min_installs: int | None = None) -> int:
    """Returns USD/mo as an integer. Order-of-magnitude estimate. Always labeled 'est.' in UI."""
    if not min_installs and not installs_per_month:
        return 0
    # Use cumulative installs × active fraction as proxy MAU; fall back to installs_per_month × 3 as monthly active.
    if min_installs:
        mau = min_installs * _ACTIVE_FRACTION
    else:
        mau = (installs_per_month or 0) * 3.0  # active-fraction proxy when min_installs missing

    # ARPU lookup: genre might be a CSV like "PRODUCTIVITY,TOOLS"; use first match.
    arpu = _DEFAULT_ARPU
    if genre:
        for token in (genre or "").upper().replace(" ", "_").split(","):
            token = token.strip()
            if token in _ARPU_BY_GENRE_KEY:
                arpu = _ARPU_BY_GENRE_KEY[token]
                break

    # Monetization multiplier: ads-only = 0.4× the genre ARPU (ad revenue is thinner than IAP/subs)
    if offers_iap:
        mult = 1.0
    elif contains_ads:
        mult = 0.4
    else:
        mult = 0.2  # neither flag set
    return int(round(mau * arpu * mult))


# ---- Bead R11: weekly review count from reviews_daily ----

def compute_weekly_review_count(package_name: str) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM reviews_daily "
            "WHERE package_name=? AND date >= ?",
            (package_name, cutoff),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0) if row else 0


def _viral_score_v1(app: dict, weekly_delta_pct: float | None) -> tuple[float, dict]:
    """Composite score in [0, 10]. Returns (score, components) for debugging."""
    velocity = _normalize_log10(app.get("installs_per_month"))
    # Momentum: prefer real 30-day delta from app_history when available; cold-start
    # fallback is the most recent weekly_reports.delta_pct.
    momentum_real = compute_momentum_30d(app["package_name"])
    momentum_input = momentum_real if momentum_real is not None else weekly_delta_pct
    momentum = _normalize_clip(momentum_input)
    recency = _recency_score(app.get("released"))
    # Low-star: prefer 30-day rate when available; cold-start uses snapshot histogram.
    low_real = compute_low_star_30d(app["package_name"])
    if low_real is not None:
        low_star = min(0.5, low_real) / 0.5
    else:
        snap = _snapshot_low_star_pct(
            app.get("histogram_1"), app.get("histogram_2"),
            app.get("histogram_3"), app.get("histogram_4"), app.get("histogram_5"),
        )
        low_star = snap if snap is not None else 0.0

    # Tweak A: high low-star is only a real "build-a-better-one opportunity"
    # if the underlying app has meaningful traction. A 1000-install broken app
    # with 50% 1-2★ reviews is just a broken app, not a niche. Suppress.
    low_star_pre = low_star
    if velocity < 0.3:                 # ≈ <50K installs/mo
        low_star *= 0.4
    suppressed_low_star = low_star_pre != low_star

    score01 = (0.30 * velocity) + (0.25 * momentum) + (0.25 * recency) + (0.20 * low_star)

    # Tweak B: chronically-broken apps (rating < 3.0) are not opportunities,
    # they're failures. Apply a 40% score penalty so they don't crowd the leaderboard.
    rating_penalty = False
    rating = app.get("score")
    if isinstance(rating, (int, float)) and rating < 3.0:
        score01 *= 0.6
        rating_penalty = True

    return round(score01 * 10.0, 2), {
        "velocity": round(velocity, 3),
        "momentum": round(momentum, 3),
        "momentum_input": momentum_input,
        "recency": round(recency, 3),
        "low_star": round(low_star, 3),
        "low_star_pre": round(low_star_pre, 3),
        "suppressed_low_star": suppressed_low_star,
        "rating_penalty": rating_penalty,
        "used_real_momentum": momentum_real is not None,
        "used_real_low_star": low_real is not None,
    }


def compute_monthly_hot_apps(force: bool = False, top_n: int = 10) -> int:
    """Score every app in every category-in-use, write top-N per category to monthly_hot_apps.

    Idempotent per (category_id, computed_at, rank). Returns total rows written.
    """
    now = datetime.now(timezone.utc)
    computed_at = now.isoformat()

    cats_in_use: set[str] = set()
    conn = _conn()
    try:
        # Union of categories actually saved by users. If no users have any picks yet,
        # fall back to every category in categories.json so the table is never empty.
        rows = conn.execute(
            "SELECT picked_categories FROM users WHERE picked_categories IS NOT NULL AND picked_categories != ''"
        ).fetchall()
        for r in rows:
            for c in (r[0] or "").split(","):
                if c:
                    cats_in_use.add(c)
        if not cats_in_use:
            cats_in_use = {c["id"] for c in load_categories()}

        # Look up the most recent weekly_reports.delta_pct per package, used as cold-start
        # momentum input until app_history (Bead 15) is populated.
        delta_by_pkg: dict[str, float] = {}
        try:
            wrows = conn.execute(
                "SELECT payload FROM weekly_reports ORDER BY year DESC, week DESC LIMIT 200"
            ).fetchall()
            for (raw,) in wrows:
                try:
                    p = json.loads(raw)
                    for a in p.get("apps", []):
                        pkg = a.get("package_name")
                        if pkg and pkg not in delta_by_pkg and isinstance(a.get("delta_pct"), (int, float)):
                            delta_by_pkg[pkg] = float(a["delta_pct"])
                except Exception:
                    continue
        except Exception:
            pass

        cat_name_by_id = {c["id"]: c["name"] for c in load_categories()}

        written = 0
        for cid in sorted(cats_in_use):
            # Pull every enriched app in this category. apps_ranked exposes histogram + released.
            try:
                arows = conn.execute(
                    """
                    SELECT package_name, title, developer, icon, released,
                           installs_per_month, min_installs, score, ratings, reviews,
                           histogram_1, histogram_2, histogram_3, histogram_4, histogram_5,
                           categories, genre, contains_ads, offers_iap
                    FROM apps_ranked
                    WHERE installs_per_month IS NOT NULL
                      AND (',' || COALESCE(categories,'') || ',') LIKE ?
                    """,
                    (f"%,{cid},%",),
                ).fetchall()
            except Exception as e:
                # apps_ranked view may not exist on a fresh DB; skip the category.
                print(f"[monthly_hot_apps] skip {cid}: {e}", flush=True)
                continue

            scored: list[tuple[float, dict, dict]] = []  # (score, app_dict, components)
            for r in arows:
                app = {
                    "package_name": r[0], "title": r[1], "developer": r[2], "icon": r[3],
                    "released": r[4], "installs_per_month": r[5], "min_installs": r[6],
                    "score": r[7], "ratings": r[8], "reviews": r[9],
                    "histogram_1": r[10], "histogram_2": r[11], "histogram_3": r[12],
                    "histogram_4": r[13], "histogram_5": r[14],
                    "categories": r[15],
                    "genre": r[16] if len(r) > 16 else None,
                    "contains_ads": r[17] if len(r) > 17 else None,
                    "offers_iap": r[18] if len(r) > 18 else None,
                }
                vscore, components = _viral_score_v1(app, delta_by_pkg.get(app["package_name"]))
                scored.append((vscore, app, components))

            if not scored:
                continue

            scored.sort(key=lambda x: x[0], reverse=True)
            top = scored[:top_n]

            # Wipe prior ranks at this exact computed_at if forcing same-second rerun.
            if force:
                conn.execute(
                    "DELETE FROM monthly_hot_apps WHERE category_id=? AND computed_at=?",
                    (cid, computed_at),
                )
            for rank, (vscore, app, components) in enumerate(top, start=1):
                low_star_pct = _snapshot_low_star_pct(
                    app.get("histogram_1"), app.get("histogram_2"),
                    app.get("histogram_3"), app.get("histogram_4"), app.get("histogram_5"),
                )
                series_30d, series_real = compute_install_series(
                    app["package_name"], app.get("installs_per_month"), app.get("released"), days=30,
                )
                # Monetization label: real DB-backed signal. Drops the misleading
                # Fermi revenue estimate in favor of facts.
                if app.get("offers_iap"):
                    monetization = "IAP"
                elif app.get("contains_ads"):
                    monetization = "ADS"
                else:
                    monetization = "FREE"
                tag = _rising_tag(components, app.get("released"))
                weekly_reviews = compute_weekly_review_count(app["package_name"])
                payload = {
                    "package_name": app["package_name"],
                    "title": app.get("title"),
                    "developer": app.get("developer"),
                    "icon": app.get("icon"),
                    "released": app.get("released"),
                    "installs_per_month": app.get("installs_per_month"),
                    "min_installs": app.get("min_installs"),
                    "score": app.get("score"),
                    "ratings": app.get("ratings"),
                    "reviews": app.get("reviews"),
                    "delta_pct": delta_by_pkg.get(app["package_name"]),
                    "low_star_pct": (low_star_pct * 0.5) if low_star_pct is not None else None,
                    "viral_score": vscore,
                    "category_name": cat_name_by_id.get(cid, cid),
                    "score_components": components,
                    # New (Beads R3, R5, R11) — revenue dropped: was Fermi-estimated,
                    # replaced by real monetization label + rating below.
                    "series_30d": series_30d,
                    "series_30d_real": series_real,
                    "rising_tag": tag,
                    "monetization": monetization,
                    "weekly_review_count": weekly_reviews,
                    "genre": app.get("genre"),
                }
                conn.execute(
                    "INSERT OR REPLACE INTO monthly_hot_apps "
                    "(category_id, computed_at, rank, package_name, payload) VALUES (?, ?, ?, ?, ?)",
                    (cid, computed_at, rank, app["package_name"], json.dumps(payload)),
                )
                written += 1
        conn.commit()
    finally:
        conn.close()
    print(f"[monthly_hot_apps] wrote {written} rows for {len(cats_in_use)} categories at {computed_at}", flush=True)
    return written


def _app_about(package_name: str) -> dict:
    """Returns {description, qa, qa_updated_at} for the app detail page.
    description = discovered_apps.summary (capped); qa = parsed apps_enriched.qa_json.
    Falls back to a live build if the cache is empty so the user never sees a blank page."""
    conn = _conn()
    try:
        row = conn.execute(
            """SELECT d.summary, e.qa_json, e.qa_updated_at, e.title
               FROM apps_enriched e
               LEFT JOIN discovered_apps d ON d.package_name = e.package_name
               WHERE e.package_name = ?""",
            (package_name,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"package_name": package_name, "description": None, "qa": None, "qa_updated_at": None}
    description, qa_raw, qa_updated_at, _title = row
    qa = None
    if qa_raw:
        try:
            qa = json.loads(qa_raw)
        except Exception:
            qa = None
    # Fallback: build on demand if the cache hasn't been populated yet.
    if qa is None:
        try:
            import app_qa as _qa_mod
            conn2 = _conn()
            try:
                qa = _qa_mod.compute_app_qa(conn2, package_name)
            finally:
                conn2.close()
        except Exception:
            qa = None
    return {
        "package_name": package_name,
        "description": description,
        "qa": qa,
        "qa_updated_at": qa_updated_at,
    }


def _latest_good_review(package_name: str) -> dict:
    """Bead R8: most recent rating>=4 review across all reviews_* tables for this package."""
    conn = _conn()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'reviews_%' AND name != 'reviews_daily'"
        ).fetchall()]
        best = None
        for tbl in tables:
            try:
                row = conn.execute(
                    f"SELECT user_name, rating, text, posted_at FROM {tbl} "
                    f"WHERE package_name=? AND rating >= 4 AND text IS NOT NULL AND length(text) > 8 "
                    f"ORDER BY posted_at DESC LIMIT 1",
                    (package_name,),
                ).fetchone()
            except Exception:
                continue
            if row and (best is None or (row[3] or "") > (best[3] or "")):
                best = row
        if not best:
            return {"package_name": package_name, "review": None}
        return {
            "package_name": package_name,
            "review": {
                "user_name": best[0], "rating": best[1],
                "text": best[2], "posted_at": best[3],
            },
        }
    finally:
        conn.close()


def _dashboard_payload(user: dict, cats: list[str], plan: str) -> dict:
    """Bead 8: minimalist dashboard shape — categories with their top apps from
    monthly_hot_apps, falling back to live apps_ranked when the cache is empty."""
    cat_name_by_id = {c["id"]: c["name"] for c in load_categories()}
    out_cats: list[dict] = []
    latest_overall: str | None = None
    conn = _conn()
    try:
        for cid in cats:
            apps: list[dict] = []
            cat_computed_at: str | None = None
            tracked_count = 0
            # R14: how many apps we track in this category overall
            try:
                cnt = conn.execute(
                    "SELECT COUNT(DISTINCT package_name) FROM apps_ranked "
                    "WHERE installs_per_month IS NOT NULL "
                    "  AND (',' || COALESCE(categories,'') || ',') LIKE ?",
                    (f"%,{cid},%",),
                ).fetchone()
                tracked_count = int(cnt[0] or 0) if cnt else 0
            except Exception:
                tracked_count = 0

            try:
                # Latest computed_at for this category — pull top-10 (was top-5)
                row = conn.execute(
                    "SELECT MAX(computed_at) FROM monthly_hot_apps WHERE category_id=?", (cid,)
                ).fetchone()
                latest_at = row[0] if row else None
                if latest_at:
                    cat_computed_at = latest_at
                    if latest_overall is None or latest_at > latest_overall:
                        latest_overall = latest_at
                    rows = conn.execute(
                        "SELECT rank, payload FROM monthly_hot_apps "
                        "WHERE category_id=? AND computed_at=? ORDER BY rank ASC LIMIT 10",
                        (cid, latest_at),
                    ).fetchall()
                    for _rank, raw in rows:
                        try:
                            apps.append(json.loads(raw))
                        except Exception:
                            continue
            except Exception:
                apps = []

            if not apps:
                # Cold-start fallback: live query against apps_ranked.
                fallback = _top_viral_for_categories([cid], 10)
                for a in fallback:
                    series_30d, series_real = compute_install_series(
                        a.get("package_name"), a.get("installs_per_month"),
                        None, days=30,
                    )
                    apps.append({
                        "package_name": a.get("package_name"),
                        "title": a.get("title"),
                        "icon": a.get("icon"),
                        "installs_per_month": a.get("installs_per_month"),
                        "delta_pct": None,
                        "low_star_pct": None,
                        "viral_score": None,
                        "rising_tag": "NEW RELEASE" if a.get("released") else "SUSTAINED",
                        "monetization": "FREE",
                        "weekly_review_count": 0,
                        "series_30d": series_30d,
                        "series_30d_real": series_real,
                    })

            out_cats.append({
                "id": cid,
                "name": cat_name_by_id.get(cid, cid),
                "apps": apps,
                "tracked_count": tracked_count,
                "computed_at": cat_computed_at,
            })
    finally:
        conn.close()

    # Total categories available globally — drives the "X of N" upsell ribbon.
    total_categories = len(load_categories())
    return {
        "user": {
            "id": user["id"], "email": user["email"], "name": user.get("name") or "",
            "picture": user.get("picture") or "", "plan": plan,
            "is_admin": user.get("is_admin", False),
            "theme": user.get("theme") or "light",
            "newsletter_subscribed": bool(user.get("newsletter_subscribed")),
            "trial_started_at": user.get("trial_started_at"),
        },
        "categories": out_cats,
        "total_categories": total_categories,
        "free_cap": 5,           # Free plan max categories (matches /api/me/categories)
        "latest_computed_at": latest_overall,
    }


def _all_status() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT package_name, state, count, table_name, started_at, completed_at, error FROM app_review_status"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "package_name": r[0], "state": r[1], "count": r[2],
            "table_name": r[3], "started_at": r[4], "completed_at": r[5], "error": r[6],
        }
        for r in rows
    ]


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        # quieter logs
        if any(s in args[0] for s in ("/api/", ".db")):
            return
        super().log_message(format, *args)

    def _send_json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- auth helpers per-request ----

    def _current_user(self) -> dict | None:
        token = auth_mod.parse_cookie(self.headers.get("Cookie"), auth_mod.SESSION_COOKIE)
        return auth_mod.get_user_by_session(token) if token else None

    def _send_redirect(self, location: str, set_cookie: str | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_xml(self, code: int, xml: str) -> None:
        body = xml.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sitemap(self) -> None:
        site_url = os.environ.get("SITE_URL", "http://localhost:8000").rstrip("/")
        urls = [f"{site_url}/", f"{site_url}/help"]
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT category_id, year, week, generated_at FROM weekly_reports "
                "ORDER BY year DESC, week DESC"
            ).fetchall()
        finally:
            conn.close()
        items = "".join(
            f"<url><loc>{site_url}/reports/{r[0]}/{r[1]}-W{int(r[2]):02d}</loc>"
            f"<lastmod>{(r[3] or '')[:10]}</lastmod></url>"
            for r in rows
        )
        roots = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{roots}{items}</urlset>"
        )
        self._send_xml(200, xml)

    def _send_html(self, code: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)

        # ---- auth routes ----
        if url.path == "/auth/google/start":
            try:
                auth_url = auth_mod.google_auth_url(next_url="/dashboard")
            except RuntimeError as e:
                self._send_html(500, f"<h1>Auth misconfigured</h1><p>{e}. Add GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET to .env.</p>")
                return
            self._send_redirect(auth_url)
            return

        if url.path == "/auth/google/callback":
            qs = parse_qs(url.query)
            code = (qs.get("code") or [""])[0]
            state = (qs.get("state") or [""])[0]
            if not code or not state:
                self._send_html(400, "<h1>Missing code/state</h1>")
                return
            try:
                token, next_url, is_new = auth_mod.handle_oauth_callback(
                    code, state,
                    ip=self.client_address[0],
                    ua=self.headers.get("User-Agent"),
                )
            except Exception as e:
                self._send_html(500, f"<h1>Sign-in failed</h1><pre>{e}</pre>")
                return
            user = auth_mod.get_user_by_session(token)
            if user and is_new:
                # Fire welcome email asynchronously
                try:
                    email_mod.fire_welcome(user)
                except Exception:
                    pass
            if user:
                auth_mod.log_activity(user["id"], "login", metadata={"new_user": is_new})
            self._send_redirect(next_url, set_cookie=auth_mod.session_cookie(token))
            return

        if url.path == "/auth/logout":
            token = auth_mod.parse_cookie(self.headers.get("Cookie"), auth_mod.SESSION_COOKIE)
            if token:
                # Log before destroying session so we still know who it was
                user = auth_mod.get_user_by_session(token)
                if user:
                    auth_mod.log_activity(user["id"], "logout")
                auth_mod.destroy_session(token)
            self._send_redirect("/", set_cookie=auth_mod.clear_cookie())
            return

        # ---- help page (auth-gated) ----
        if url.path == "/help" or url.path == "/help/":
            user = self._current_user()
            if not user:
                self._send_redirect("/auth/google/start")
                return
            try:
                self._send_html(200, (Path(__file__).parent / "help.html").read_text())
            except Exception as e:
                self._send_html(500, f"<h1>Help page missing</h1><pre>{e}</pre>")
            return

        # ---- public landing page ----
        if url.path in ("/", "/index", "/landing"):
            try:
                self._send_html(200, (Path(__file__).parent / "landing.html").read_text())
            except Exception as e:
                self._send_html(500, f"<h1>Landing page missing</h1><pre>{e}</pre>")
            return

        # ---- auth-gated app shell ----
        # /dashboard is the canonical home (default landing); /app remains as the
        # tab-deep-link surface (Reviews/Ranking/Niches). Both render the same shell —
        # the URL pathname seeds the initial view via window.location.
        if url.path in ("/app", "/app/", "/dashboard", "/dashboard/"):
            user = self._current_user()
            if not user:
                self._send_redirect("/auth/google/start")
                return
            try:
                html = (Path(__file__).parent / "index.html").read_text()
                self._send_html(200, _inject_vfuser(html, user))
            except Exception as e:
                self._send_html(500, f"<h1>App shell missing</h1><pre>{e}</pre>")
            return

        # Per-app detail page: /app/<pkg>/details (?show=review|developer|revenue)
        m = _APP_DETAIL_RE.match(url.path)
        if m:
            user = self._current_user()
            if not user:
                self._send_redirect("/auth/google/start")
                return
            try:
                html = (Path(__file__).parent / "app_detail.html").read_text()
                self._send_html(200, _inject_vfuser(html, user))
            except Exception as e:
                self._send_html(500, f"<h1>App detail page missing</h1><pre>{e}</pre>")
            return

        # ---- protected APIs ----
        if url.path.startswith("/api/admin/"):
            user = self._current_user()
            if not user or not user.get("is_admin"):
                self._send_json(403, {"ok": False, "error": "admin only"})
                return
            if url.path == "/api/admin/email_stats":
                self._send_json(200, email_mod.email_stats())
                return

        if url.path == "/api/me":
            user = self._current_user()
            self._send_json(200, {"user": user})
            return

        if url.path == "/api/me/usage":
            user = self._current_user()
            if not user:
                self._send_json(401, {"ok": False})
                return
            self._send_json(200, {
                "plan": user.get("plan", "free"),
                "usage": auth_mod.get_feature_usage_summary(user["id"]),
                "quota": auth_mod.get_quota_status(user["id"], user.get("plan", "free")),
            })
            return

        if url.path == "/api/me/dashboard":
            user = self._current_user()
            if not user:
                self._send_json(401, {"ok": False})
                return
            cats = _user_category_ids(user)
            plan = user.get("plan", "free")
            self._send_json(200, _dashboard_payload(user, cats, plan))
            return

        if url.path == "/api/me/watchlist":
            user = self._current_user()
            if not user:
                self._send_json(401, {"ok": False})
                return
            self._send_json(200, {"items": auth_mod.watchlist_list(user["id"])})
            return

        # Bead R8: latest good review for the hero excerpt.
        if url.path.startswith("/api/app/") and url.path.endswith("/latest-good-review"):
            parts = url.path.strip("/").split("/")
            if len(parts) == 4:
                pkg = parts[2]
                self._send_json(200, _latest_good_review(pkg))
                return

        # /api/app/<pkg>/about — description + Q&A snapshot
        if url.path.startswith("/api/app/") and url.path.endswith("/about"):
            parts = url.path.strip("/").split("/")
            if len(parts) == 4:
                pkg = parts[2]
                self._send_json(200, _app_about(pkg))
                return

        if url.path == "/api/me/saved-searches":
            user = self._current_user()
            if not user:
                self._send_json(401, {"ok": False})
                return
            self._send_json(200, {"items": auth_mod.saved_searches_list(user["id"])})
            return

        # Public weekly report: /reports/{category_id}/{year}-W{week}
        if url.path.startswith("/reports/"):
            parts = [p for p in url.path.split("/") if p]
            # ['reports', '{cat}', '{yyyy-Www}']
            if len(parts) == 3:
                cat_id = parts[1]
                slug = parts[2]
                try:
                    yy, ww = slug.split("-W")
                    yy = int(yy); ww = int(ww)
                except Exception:
                    self._send_html(404, "<h1>Report not found</h1>")
                    return
                conn = _conn()
                try:
                    row = conn.execute(
                        "SELECT payload, generated_at FROM weekly_reports "
                        "WHERE category_id=? AND year=? AND week=?",
                        (cat_id, yy, ww),
                    ).fetchone()
                finally:
                    conn.close()
                if not row:
                    self._send_html(404, "<h1>Report not found</h1>")
                    return
                try: payload = json.loads(row[0])
                except Exception: payload = {}
                html = _render_weekly_report_html(cat_id, yy, ww, payload, row[1])
                self._send_html(200, html)
                return
            self._send_html(404, "<h1>Report not found</h1>")
            return

        if url.path == "/sitemap.xml":
            self._send_sitemap()
            return

        if url.path == "/api/me/activity":
            user = self._current_user()
            if not user:
                self._send_json(401, {"ok": False})
                return
            qs = parse_qs(url.query)
            try: limit = max(1, min(500, int((qs.get("limit") or ["100"])[0])))
            except: limit = 100
            self._send_json(200, {"items": auth_mod.get_recent_activity(user["id"], limit)})
            return

        if url.path == "/api/onboarding/state":
            user = self._current_user()
            if not user:
                self._send_json(401, {"ok": False})
                return
            cats = (user.get("picked_categories") or "").split(",")
            cats = [c for c in cats if c]
            self._send_json(200, {
                "onboarded": user.get("onboarded", False),
                "picked_categories": cats,
                "picked_goal": user.get("picked_goal") or None,
            })
            return

        if url.path == "/api/status":
            self._send_json(200, {
                "items": _all_status(),
                "queue_size": _fetch_queue.qsize(),
                "workers": NUM_FETCH_WORKERS,
            })
            return
        if url.path == "/api/categories":
            self._send_json(200, {"items": _list_categories_with_status()})
            return
        if url.path == "/api/discovery_status":
            self._send_json(200, _discovery_progress())
            return
        super().do_GET()

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(length) or b"{}") if length else {}
        except Exception:
            return {}

    def do_DELETE(self):
        url = urlparse(self.path)
        user = self._current_user()
        if not user:
            self._send_json(401, {"ok": False})
            return
        if url.path == "/api/me/watchlist":
            qs = parse_qs(url.query)
            pkg = (qs.get("pkg") or [""])[0].strip()
            if not pkg:
                self._send_json(400, {"ok": False, "error": "pkg required"})
                return
            auth_mod.watchlist_remove(user["id"], pkg)
            self._send_json(200, {"ok": True})
            return
        if url.path.startswith("/api/me/saved-searches/"):
            try:
                sid = int(url.path.rsplit("/", 1)[1])
            except Exception:
                self._send_json(400, {"ok": False})
                return
            auth_mod.saved_search_remove(user["id"], sid)
            self._send_json(200, {"ok": True})
            return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        url = urlparse(self.path)

        # ---- watchlist toggle ----
        if url.path == "/api/me/watchlist":
            user = self._current_user()
            if not user:
                self._send_json(401, {"ok": False}); return
            body = self._read_json_body()
            pkg = (body.get("package_name") or "").strip()
            if not pkg:
                self._send_json(400, {"ok": False, "error": "package_name required"}); return
            auth_mod.watchlist_add(user["id"], pkg)
            auth_mod.log_activity(user["id"], "watchlist_add", context=pkg)
            self._send_json(200, {"ok": True})
            return

        # ---- saved searches create ----
        if url.path == "/api/me/saved-searches":
            user = self._current_user()
            if not user:
                self._send_json(401, {"ok": False}); return
            body = self._read_json_body()
            name = (body.get("name") or "").strip()[:80]
            payload = body.get("payload") or {}
            if not name:
                self._send_json(400, {"ok": False, "error": "name required"}); return
            sid = auth_mod.saved_search_add(user["id"], name, payload)
            auth_mod.log_activity(user["id"], "saved_search_add", context=name)
            self._send_json(200, {"ok": True, "id": sid})
            return

        # ---- newsletter signup ----
        if url.path == "/api/me/newsletter":
            user = self._current_user()
            if not user:
                self._send_json(401, {"ok": False}); return
            conn = sqlite3.connect(DB_PATH, timeout=30.0)
            try:
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("UPDATE users SET newsletter_subscribed = 1 WHERE id = ?", (user["id"],))
                conn.commit()
            finally:
                conn.close()
            auth_mod.log_activity(user["id"], "newsletter_subscribed")
            self._send_json(200, {"ok": True, "newsletter_subscribed": True})
            return

        # ---- 14-day trial start (lightweight: just records the timestamp; gating logic later) ----
        if url.path == "/api/me/trial/start":
            user = self._current_user()
            if not user:
                self._send_json(401, {"ok": False}); return
            if user.get("trial_started_at"):
                self._send_json(200, {"ok": True, "trial_started_at": user["trial_started_at"], "already": True})
                return
            now = datetime.now(timezone.utc).isoformat()
            conn = sqlite3.connect(DB_PATH, timeout=30.0)
            try:
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("UPDATE users SET trial_started_at = ? WHERE id = ?", (now, user["id"]))
                conn.commit()
            finally:
                conn.close()
            auth_mod.log_activity(user["id"], "trial_started")
            try:
                # Notify the founder so they can manually onboard / upgrade plan if needed
                email_mod.fire_paywall_hit({**user, "trial_started_at": now}, "trial_signup")
            except Exception:
                pass
            self._send_json(200, {"ok": True, "trial_started_at": now})
            return

        # ---- theme ----
        if url.path == "/api/me/theme":
            user = self._current_user()
            if not user:
                self._send_json(401, {"ok": False}); return
            body = self._read_json_body()
            theme = (body.get("theme") or "").strip()
            if theme not in ("dark", "light"):
                self._send_json(400, {"ok": False, "error": "theme must be 'dark' or 'light'"}); return
            auth_mod.set_user_theme(user["id"], theme)
            self._send_json(200, {"ok": True, "theme": theme})
            return

        # ---- export logging (gated by weekly cap) ----
        if url.path == "/api/me/export":
            user = self._current_user()
            if not user:
                self._send_json(401, {"ok": False}); return
            qs = parse_qs(url.query)
            kind = (qs.get("kind") or [""])[0].strip() or "ranking"
            allowed, details = auth_mod.can_use_feature(user["id"], "export", user.get("plan", "free"), kind)
            if not allowed:
                try: email_mod.fire_paywall_hit(user, "export")
                except Exception: pass
                auth_mod.log_activity(user["id"], "paywall_hit",
                                      context=kind, metadata={"feature": "export", **details})
                self._send_json(403, {"ok": False, "error": details.get("reason", "weekly_limit_hit"),
                                       "feature": "export", **details})
                return
            auth_mod.record_feature_use(user["id"], "export", kind)
            auth_mod.log_activity(user["id"], "export", context=kind)
            self._send_json(200, {"ok": True, "kind": kind, **details})
            return

        # ---- update saved categories (Settings modal posts here) ----
        if url.path == "/api/me/categories":
            user = self._current_user()
            if not user:
                self._send_json(401, {"ok": False, "error": "not signed in"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except Exception:
                body = {}
            cats_in = body.get("categories") or []
            if not isinstance(cats_in, list):
                cats_in = []
            valid_ids = {c["id"] for c in load_categories()}
            cats = [str(c) for c in cats_in if str(c) in valid_ids]
            cap = 5 if user.get("plan", "free") == "free" else 33
            cats = cats[:cap]
            cats_csv = ",".join(cats)
            conn = sqlite3.connect(DB_PATH, timeout=30.0)
            try:
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute(
                    "UPDATE users SET picked_categories = ? WHERE id = ?",
                    (cats_csv, user["id"]),
                )
                conn.commit()
            finally:
                conn.close()
            auth_mod.log_activity(user["id"], "categories_updated", metadata={"categories": cats})
            self._send_json(200, {"ok": True, "categories": cats})
            return

        # ---- onboarding ----
        if url.path == "/api/onboarding/complete":
            user = self._current_user()
            if not user:
                self._send_json(401, {"ok": False, "error": "not signed in"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except Exception:
                body = {}
            cats_in = body.get("categories") or []
            if not isinstance(cats_in, list):
                cats_in = []
            cats = ",".join([str(c)[:80] for c in cats_in if c])[:600]
            goal = (body.get("goal") or "")[:60]
            conn = sqlite3.connect(DB_PATH, timeout=30.0)
            try:
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute(
                    "UPDATE users SET onboarded=1, picked_categories=?, picked_goal=? WHERE id=?",
                    (cats, goal, user["id"]),
                )
                conn.commit()
            finally:
                conn.close()
            user["onboarded"] = True
            user["picked_categories"] = cats
            user["picked_goal"] = goal
            try:
                email_mod.fire_onboarded(user)
            except Exception:
                pass
            auth_mod.log_activity(user["id"], "onboarding_complete",
                                  context=goal, metadata={"categories": cats.split(",") if cats else []})
            self._send_json(200, {"ok": True})
            return

        if url.path == "/api/fetch":
            qs = parse_qs(url.query)
            pkg = (qs.get("pkg") or [""])[0].strip()
            if not pkg:
                self._send_json(400, {"ok": False, "error": "pkg required"})
                return
            user = self._current_user()
            uid = user["id"] if user else None
            # Plan-aware gate: Free plan gets 1 distinct review_fetch lifetime.
            if user:
                allowed, details = auth_mod.can_use_feature(user["id"], "review_fetch", user.get("plan", "free"), pkg)
                if not allowed:
                    try: email_mod.fire_paywall_hit(user, "review_fetch")
                    except Exception: pass
                    auth_mod.log_activity(user["id"], "paywall_hit",
                                          context=pkg, metadata={"feature": "review_fetch", **details})
                    self._send_json(403, {"ok": False,
                                            "error": details.get("reason", "weekly_limit_hit"),
                                            "feature": "review_fetch", **details})
                    return
                auth_mod.record_feature_use(user["id"], "review_fetch", pkg)
                auth_mod.log_activity(user["id"], "review_fetch", context=pkg)
            self._send_json(202, _start_fetch(pkg, uid))
            return
        if url.path == "/api/discover_category":
            qs = parse_qs(url.query)
            cat_id = (qs.get("id") or [""])[0].strip()
            if not cat_id:
                self._send_json(400, {"ok": False, "error": "id required"})
                return
            user = self._current_user()
            try:
                result = discover_category_full(cat_id)
                if user:
                    auth_mod.log_activity(user["id"], "discover_category",
                                          context=cat_id, metadata={"found": result.get("found", 0)})
                self._send_json(200, {"ok": True, **result})
            except ValueError as e:
                self._send_json(404, {"ok": False, "error": str(e)})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)[:300]})
            return
        if url.path == "/api/developer":
            qs = parse_qs(url.query)
            dev_id = (qs.get("devId") or [""])[0].strip()
            dev_name = (qs.get("name") or [""])[0].strip()
            if not dev_id and not dev_name:
                self._send_json(400, {"ok": False, "error": "devId or name required"})
                return
            user = self._current_user()
            # Plan-aware gate: Free plan gets 1 distinct developer_lookup lifetime.
            if user:
                allowed, details = auth_mod.can_use_feature(user["id"], "developer_lookup", user.get("plan", "free"), dev_id or dev_name)
                if not allowed:
                    try: email_mod.fire_paywall_hit(user, "developer_lookup")
                    except Exception: pass
                    auth_mod.log_activity(user["id"], "paywall_hit",
                                          context=dev_id or dev_name, metadata={"feature": "developer_lookup"})
                    self._send_json(403, {"ok": False, "error": "free_tier_used", "feature": "developer_lookup", **details})
                    return
                auth_mod.record_feature_use(user["id"], "developer_lookup", dev_id or dev_name)
                auth_mod.log_activity(user["id"], "developer_lookup",
                                      context=dev_id or dev_name, metadata={"name": dev_name})
            try:
                result = discover_developer(dev_id, dev_name)
                if user:
                    auth_mod.log_activity(user["id"], "developer_lookup_done",
                                          context=dev_id or dev_name, metadata={"found": result.get("found", 0)})
                self._send_json(200, {"ok": True, **result})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)[:300]})
            return
        self.send_response(404)
        self.end_headers()


def _render_weekly_report_html(category_id: str, year: int, week: int, payload: dict, generated_at: str) -> str:
    """Renders one weekly report by templating report.html with payload data."""
    try:
        tmpl = (Path(__file__).parent / "report.html").read_text()
    except Exception:
        return f"<h1>Report template missing</h1>"
    cat_name = payload.get("category_name") or category_id.replace("_", " ").title()
    apps = payload.get("apps", [])
    site_url = os.environ.get("SITE_URL", "http://localhost:8000").rstrip("/")
    canonical = f"{site_url}/reports/{category_id}/{year}-W{week:02d}"

    # Build rows
    rows_html = []
    for i, a in enumerate(apps, 1):
        installs = a.get("installs_per_month") or 0
        installs_str = _humanize_installs(installs)
        delta_pct = a.get("delta_pct")
        if delta_pct is None:
            delta_str = '<span class="delta delta-new">NEW</span>'
        elif delta_pct >= 0:
            delta_str = f'<span class="delta delta-up">▲ +{delta_pct:.1f}%</span>'
        else:
            delta_str = f'<span class="delta delta-down">▼ {delta_pct:.1f}%</span>'
        icon_html = f'<img src="{a.get("icon") or ""}" alt="" loading="lazy" />' if a.get("icon") else '<span class="ico-fallback"></span>'
        rows_html.append(
            f'<li class="rep-row">'
            f'<span class="rep-rank">{i}</span>'
            f'<span class="rep-icon">{icon_html}</span>'
            f'<span class="rep-body">'
            f'  <a class="rep-title" href="https://play.google.com/store/apps/details?id={a.get("package_name","")}" rel="nofollow noopener">{(a.get("title") or a.get("package_name") or "")}</a>'
            f'  <span class="rep-meta">{installs_str} installs/mo · {delta_str}</span>'
            f'</span>'
            f'</li>'
        )

    # JSON-LD Article schema
    json_ld = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": f"Top 5 viral apps in {cat_name} — week {week}, {year}",
        "datePublished": generated_at,
        "author": {"@type": "Organization", "name": "ViralFinder"},
        "mainEntityOfPage": canonical,
    }

    title = f"Top 5 viral apps in {cat_name} — Week {week}, {year} | ViralFinder"
    description = (
        f"The fastest-growing Google Play apps in {cat_name} for week {week} of {year}. "
        f"Live install velocity, week-over-week change, and signal for indie creators."
    )

    out = tmpl
    out = out.replace("{{TITLE}}", _html_escape(title))
    out = out.replace("{{DESCRIPTION}}", _html_escape(description))
    out = out.replace("{{CANONICAL}}", _html_escape(canonical))
    out = out.replace("{{H1}}", _html_escape(f"Top 5 viral apps in {cat_name}"))
    out = out.replace("{{SUBTITLE}}", _html_escape(f"Week {week}, {year}"))
    out = out.replace("{{ROWS}}", "\n".join(rows_html))
    out = out.replace("{{PREV_LINK}}", _prev_week_link(category_id, year, week, site_url))
    out = out.replace("{{NEXT_LINK}}", _next_week_link(category_id, year, week, site_url))
    out = out.replace("{{JSON_LD}}", json.dumps(json_ld))
    return out


def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _humanize_installs(n: int | float | None) -> str:
    if not n: return "0"
    n = int(n)
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.0f}K"
    return str(n)


def _prev_week_link(cat: str, year: int, week: int, site_url: str) -> str:
    py, pw = (year - 1, 52) if week <= 1 else (year, week - 1)
    return f'<a href="{site_url}/reports/{cat}/{py}-W{pw:02d}">← Week {pw}, {py}</a>'


def _next_week_link(cat: str, year: int, week: int, site_url: str) -> str:
    ny, nw = (year + 1, 1) if week >= 52 else (year, week + 1)
    return f'<a href="{site_url}/reports/{cat}/{ny}-W{nw:02d}">Week {nw}, {ny} →</a>'


def _email_scheduler() -> None:
    """Background loop that fires weekly-digest + re-engagement emails on schedule."""
    import time
    last_weekly_run = None
    last_reengage_run = None
    last_reports_run = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            # Weekly digest: run once a week (Mondays at any hour the loop wakes; the email_service is idempotent within the calendar week)
            if now.weekday() == 0:  # Monday
                wkkey = now.isocalendar()[:2]
                if last_weekly_run != wkkey:
                    n = email_mod.fire_weekly_digest_all()
                    print(f"[email scheduler] weekly digest: {n} sent", flush=True)
                    last_weekly_run = wkkey
                if last_reports_run != wkkey:
                    written = _generate_weekly_reports()
                    print(f"[reports] weekly snapshot: {written} categories written", flush=True)
                    last_reports_run = wkkey
            # Re-engagement: run hourly; the email_service caps to one per user
            daykey = (now.year, now.month, now.day, now.hour)
            if last_reengage_run != daykey:
                n = email_mod.fire_reengage_eligible()
                if n:
                    print(f"[email scheduler] re-engagement: {n} sent", flush=True)
                last_reengage_run = daykey

            # Bead 14: poller jobs (gated by poller_runs idempotency, not in-memory state).
            try:
                import poller as _poller
                results = _poller.tick_due_jobs(now)
                for job_name, rows in results:
                    print(f"[poller] {job_name}: {rows} rows", flush=True)
            except Exception as pe:
                print(f"[poller] tick error: {pe}", flush=True)
        except Exception as e:
            print(f"[email scheduler] error: {e}", flush=True)
        time.sleep(900)  # 15 minutes


def main() -> None:
    _ensure_schema()
    _sweep_stale_fetches()  # clean up rows orphaned by previous restart
    threading.Thread(target=_email_scheduler, daemon=True).start()
    addr = ("127.0.0.1", 8000)
    httpd = ThreadingHTTPServer(addr, Handler)
    print(f"serving on http://{addr[0]}:{addr[1]} (db={DB_PATH})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()
