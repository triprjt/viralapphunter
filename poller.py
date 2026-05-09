"""Scheduled poller jobs for ViralFinder.

Four scheduled jobs feed the time-series tables defined in auth.py:
  - poll_apps_daily            (Bead 11) → app_history
  - poll_reviews_weekly        (Bead 12) → reviews_*
  - aggregate_reviews_daily    (Bead 13) → reviews_daily
  - compute_monthly_hot_apps   (Bead 7/17, lives in server.py — invoked from here too)

All jobs are gated by `_should_run(job_name, schedule)` which consults
`poller_runs` for idempotency. Each run is bracketed with `_record_run`
markers so the admin can see status and rows-written.

Schedules used:
    'daily HH:MM UTC'           — at most once per UTC calendar day, after HH:MM
    'weekly DOW HH:MM UTC'      — at most once per ISO week, after DOW HH:MM
                                  (DOW: 0=Mon..6=Sun)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path("reviews.db")
NUM_WORKERS_APPS = 12
NUM_WORKERS_REVIEWS = 4
TOP_N_REVIEW_APPS = 300


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


# ---- Scheduling primitives (Bead 10) ----

def _parse_schedule(spec: str) -> dict:
    """Parse 'daily HH:MM UTC' or 'weekly DOW HH:MM UTC' into a dict the runner uses."""
    parts = spec.strip().split()
    if not parts:
        raise ValueError(f"empty schedule: {spec!r}")
    kind = parts[0]
    if kind == "daily":
        # daily HH:MM UTC
        if len(parts) < 2:
            raise ValueError(spec)
        hh, mm = (int(x) for x in parts[1].split(":"))
        return {"kind": "daily", "h": hh, "m": mm}
    if kind == "weekly":
        # weekly DOW HH:MM UTC
        if len(parts) < 3:
            raise ValueError(spec)
        dow = int(parts[1])  # 0=Mon..6=Sun
        hh, mm = (int(x) for x in parts[2].split(":"))
        return {"kind": "weekly", "dow": dow, "h": hh, "m": mm}
    raise ValueError(f"unknown schedule kind: {kind!r}")


def _last_ok_at(job_name: str) -> datetime | None:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT MAX(started_at) FROM poller_runs WHERE job_name=? AND status='ok'",
            (job_name,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except Exception:
        return None


def _should_run(job_name: str, schedule: str, now: datetime | None = None) -> bool:
    """Return True if the job is due now and hasn't run since its last scheduled trigger."""
    now = now or _now()
    spec = _parse_schedule(schedule)
    last = _last_ok_at(job_name)

    if spec["kind"] == "daily":
        # The most recent past trigger time
        trigger = now.replace(hour=spec["h"], minute=spec["m"], second=0, microsecond=0)
        if trigger > now:
            trigger -= timedelta(days=1)
        return last is None or last < trigger

    if spec["kind"] == "weekly":
        # Walk back to the most recent past (DOW, HH:MM)
        weekday = now.weekday()
        days_back = (weekday - spec["dow"]) % 7
        candidate = (now - timedelta(days=days_back)).replace(hour=spec["h"], minute=spec["m"], second=0, microsecond=0)
        if candidate > now:
            candidate -= timedelta(days=7)
        return last is None or last < candidate

    return False


def _record_run_start(job_name: str) -> str:
    started_at = _now_iso()
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO poller_runs (job_name, started_at, status, rows_written) "
            "VALUES (?, ?, 'running', 0)",
            (job_name, started_at),
        )
        conn.commit()
    finally:
        conn.close()
    return started_at


def _record_run_finish(job_name: str, started_at: str, status: str, rows_written: int = 0, errors: str | None = None) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE poller_runs SET finished_at=?, status=?, rows_written=?, errors=? "
            "WHERE job_name=? AND started_at=?",
            (_now_iso(), status, rows_written, errors, job_name, started_at),
        )
        conn.commit()
    finally:
        conn.close()


def _wrap(job_name: str, fn, *, force: bool):
    """Run a job function with poller_runs bookkeeping. Returns rows written."""
    started_at = _record_run_start(job_name)
    try:
        rows = fn()
        _record_run_finish(job_name, started_at, "ok", rows_written=rows or 0)
        return rows or 0
    except Exception as e:
        err = (str(e) + "\n" + traceback.format_exc())[:2000]
        _record_run_finish(job_name, started_at, "error", rows_written=0, errors=err)
        if force:
            raise
        print(f"[poller] {job_name} failed: {e}", flush=True)
        return 0


# ---- Job 1: poll_apps_daily (Bead 11) ----

# Columns we treat as "interesting" for change detection.
_TRACKED_COLS = ("min_installs", "score", "ratings_count", "reviews_count",
                 "histogram_1", "histogram_2", "histogram_3", "histogram_4", "histogram_5",
                 "version", "recent_changes")


def _latest_app_history(conn: sqlite3.Connection, package_name: str) -> dict | None:
    row = conn.execute(
        f"SELECT {', '.join(_TRACKED_COLS)} FROM app_history "
        f"WHERE package_name=? ORDER BY captured_at DESC LIMIT 1",
        (package_name,),
    ).fetchone()
    if not row:
        return None
    return dict(zip(_TRACKED_COLS, row))


def _has_changed(latest: dict | None, fresh: dict) -> bool:
    if latest is None:
        return True
    for c in _TRACKED_COLS:
        if (latest.get(c) or 0) != (fresh.get(c) or 0) and not (
            latest.get(c) is None and fresh.get(c) is None
        ):
            # Treat None ↔ 0 as "no change" for numeric fields
            a, b = latest.get(c), fresh.get(c)
            if a == b:
                continue
            if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a == b:
                continue
            return True
    return False


def _gp_app_to_history_row(pkg: str, info: dict) -> dict:
    return {
        "package_name": pkg,
        "min_installs": info.get("minInstalls"),
        "score": info.get("score"),
        "ratings_count": info.get("ratings"),
        "reviews_count": info.get("reviews"),
        "histogram_1": (info.get("histogram") or {}).get(1) if isinstance(info.get("histogram"), dict) else None,
        "histogram_2": (info.get("histogram") or {}).get(2) if isinstance(info.get("histogram"), dict) else None,
        "histogram_3": (info.get("histogram") or {}).get(3) if isinstance(info.get("histogram"), dict) else None,
        "histogram_4": (info.get("histogram") or {}).get(4) if isinstance(info.get("histogram"), dict) else None,
        "histogram_5": (info.get("histogram") or {}).get(5) if isinstance(info.get("histogram"), dict) else None,
        "version": info.get("version"),
        "recent_changes": info.get("recentChanges"),
    }


def _fetch_one_app(pkg: str) -> tuple[str, dict | None]:
    """Lightweight gp_app() wrapper isolated for thread-safety."""
    try:
        from google_play_scraper import app as gp_app
        info = gp_app(pkg, lang="en", country="in")
        return pkg, _gp_app_to_history_row(pkg, info)
    except Exception as e:
        return pkg, {"__error": str(e)[:300]}


def poll_apps_daily(force: bool = False) -> int:
    """Refresh every package in apps_enriched, write app_history when fields changed."""
    def _job() -> int:
        conn = _conn()
        try:
            rows = conn.execute("SELECT package_name FROM apps_enriched").fetchall()
            packages = [r[0] for r in rows]
        finally:
            conn.close()

        if not packages:
            return 0

        written = 0
        with ThreadPoolExecutor(max_workers=NUM_WORKERS_APPS) as ex:
            futures = [ex.submit(_fetch_one_app, p) for p in packages]
            buf: list[dict] = []
            for f in as_completed(futures):
                pkg, payload = f.result()
                if not payload or payload.get("__error"):
                    continue
                buf.append(payload)
                # Flush in batches of 50 to keep transactions short
                if len(buf) >= 50:
                    written += _flush_app_history(buf)
                    buf.clear()
            if buf:
                written += _flush_app_history(buf)
        return written

    return _wrap("poll_apps_daily", _job, force=force)


def _flush_app_history(buf: list[dict]) -> int:
    conn = _conn()
    written = 0
    try:
        captured_at = _now_iso()
        for fresh in buf:
            pkg = fresh["package_name"]
            latest = _latest_app_history(conn, pkg)
            if not _has_changed(latest, fresh):
                continue
            conn.execute(
                "INSERT INTO app_history (package_name, captured_at, min_installs, score, "
                "ratings_count, reviews_count, histogram_1, histogram_2, histogram_3, "
                "histogram_4, histogram_5, version, recent_changes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pkg, captured_at,
                    fresh.get("min_installs"), fresh.get("score"),
                    fresh.get("ratings_count"), fresh.get("reviews_count"),
                    fresh.get("histogram_1"), fresh.get("histogram_2"),
                    fresh.get("histogram_3"), fresh.get("histogram_4"),
                    fresh.get("histogram_5"),
                    fresh.get("version"), fresh.get("recent_changes"),
                ),
            )
            written += 1
        conn.commit()
    finally:
        conn.close()
    return written


# ---- Job 2: poll_reviews_weekly (Bead 12) ----

def _top_review_targets(limit: int = TOP_N_REVIEW_APPS) -> list[str]:
    """Top-N apps by min_installs across categories that any user has saved.
    Falls back to top-N across the whole apps_enriched table if no users have picks yet."""
    conn = _conn()
    try:
        used = set()
        rows = conn.execute(
            "SELECT picked_categories FROM users WHERE picked_categories IS NOT NULL AND picked_categories != ''"
        ).fetchall()
        for (csv,) in rows:
            for c in (csv or "").split(","):
                if c:
                    used.add(c)
        if used:
            placeholders = " OR ".join(["(',' || COALESCE(categories,'') || ',') LIKE ?"] * len(used))
            params = [f"%,{c},%" for c in used] + [limit]
            packages = conn.execute(
                f"SELECT package_name FROM apps_ranked "
                f"WHERE installs_per_month IS NOT NULL AND ({placeholders}) "
                f"ORDER BY installs_per_month DESC LIMIT ?",
                params,
            ).fetchall()
        else:
            packages = conn.execute(
                "SELECT package_name FROM apps_ranked WHERE installs_per_month IS NOT NULL "
                "ORDER BY installs_per_month DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [p[0] for p in packages]
    finally:
        conn.close()


def poll_reviews_weekly(force: bool = False) -> int:
    """Re-fetch newest reviews for top-N priority apps; upsert into reviews_*.
    After ingest, rebuild sentiment analysis + Q&A for any package that gained
    new rows so the dashboard reflects the latest themes."""
    def _job() -> int:
        # Lazy import so poller can be loaded without google_play_scraper at boot.
        from fetch_reviews import (
            DEFAULT_FILTERS as FR_DEFAULTS, fetch_app_title,
            fetch_reviews as fr_fetch, normalize, upsert_app, upsert_reviews,
        )

        targets = _top_review_targets()
        if not targets:
            return 0

        # Fetch fewer reviews per app since we only want recent ones since last fetch.
        filters = dict(FR_DEFAULTS, max_reviews_per_app=500, sort="newest")

        written = 0
        updated_pkgs: set[str] = set()
        for pkg in targets:
            try:
                title = fetch_app_title(pkg, filters["country"], filters["language"])
                raw = fr_fetch(pkg, filters)
                rows = [normalize(item, pkg, filters) for item in raw]
                if not rows:
                    continue
                conn = _conn()
                try:
                    upsert_app(conn, pkg, title)
                    table = "reviews_" + filters["country"]
                    n = upsert_reviews(conn, table, rows)
                finally:
                    conn.close()
                if n:
                    written += n
                    updated_pkgs.add(pkg)
            except Exception as e:
                print(f"[poll_reviews_weekly] {pkg} failed: {e}", flush=True)
                continue

        # Refresh sentiment + Q&A for packages that gained new rows.
        if updated_pkgs:
            refresh_sentiment_and_qa(list(updated_pkgs))
        return written

    return _wrap("poll_reviews_weekly", _job, force=force)


def refresh_sentiment_and_qa(packages: list[str]) -> None:
    """For each package, rebuild review sentiment then rebuild qa_json so the
    'why people love it / hate it / verdict' answers reflect the latest reviews.

    Safe to call from any caller (poller, manual fetch, etc.)."""
    if not packages:
        return
    try:
        import review_sentiment as _sent
        import app_qa as _qa
    except Exception as e:
        print(f"[refresh] cannot import sentiment/qa modules: {e}", flush=True)
        return
    conn = _conn()
    try:
        for pkg in packages:
            try:
                _sent.build_for(conn, pkg)
            except Exception as e:
                print(f"[refresh] sentiment {pkg}: {e}", flush=True)
            try:
                _qa.build_qa_for(conn, pkg)
            except Exception as e:
                print(f"[refresh] qa {pkg}: {e}", flush=True)
    finally:
        conn.close()
    print(f"[refresh] rebuilt sentiment+qa for {len(packages)} package(s)", flush=True)


# ---- Job 3: aggregate_reviews_daily (Bead 13) ----

def _reviews_tables() -> list[str]:
    """Return only the reviews-content tables (with `posted_at`), not aggregates."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'reviews_%' "
            "AND name != 'reviews_daily'"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def aggregate_reviews_daily(force: bool = False) -> int:
    """Bucket reviews_* into reviews_daily (count + per-rating-bin counts) for yesterday + today."""
    def _job() -> int:
        tables = _reviews_tables()
        if not tables:
            return 0
        today = _now().date().isoformat()
        yesterday = (_now() - timedelta(days=1)).date().isoformat()
        day_filter = (yesterday, today)

        conn = _conn()
        written = 0
        try:
            for tbl in tables:
                # Country comes from the table column (we already store it per-row).
                # Aggregate yesterday + today; INSERT OR REPLACE for idempotency.
                rows = conn.execute(
                    f"SELECT package_name, country, substr(posted_at, 1, 10) AS d, "
                    f"       COUNT(*), AVG(rating), "
                    f"       SUM(CASE WHEN rating=1 THEN 1 ELSE 0 END), "
                    f"       SUM(CASE WHEN rating=2 THEN 1 ELSE 0 END), "
                    f"       SUM(CASE WHEN rating=3 THEN 1 ELSE 0 END), "
                    f"       SUM(CASE WHEN rating=4 THEN 1 ELSE 0 END), "
                    f"       SUM(CASE WHEN rating=5 THEN 1 ELSE 0 END) "
                    f"FROM {tbl} "
                    f"WHERE substr(posted_at, 1, 10) IN (?, ?) AND package_name IS NOT NULL "
                    f"GROUP BY package_name, country, d",
                    day_filter,
                ).fetchall()
                for r in rows:
                    pkg, country, d, count, avg, r1, r2, r3, r4, r5 = r
                    if not pkg or not country or not d:
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO reviews_daily "
                        "(package_name, country, date, count, avg_rating, rating_1, rating_2, rating_3, rating_4, rating_5) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (pkg, country, d, count, avg, r1 or 0, r2 or 0, r3 or 0, r4 or 0, r5 or 0),
                    )
                    written += 1
            conn.commit()
        finally:
            conn.close()
        return written

    return _wrap("aggregate_reviews_daily", _job, force=force)


# ---- Driver invoked from server.py:_email_scheduler (Bead 14) ----

# Schedule table
SCHEDULES = {
    "poll_apps_daily":           "daily 03:00 UTC",
    "aggregate_reviews_daily":   "daily 04:00 UTC",
    "poll_reviews_weekly":       "weekly 1 02:00 UTC",   # Tuesday
    "compute_monthly_hot_apps":  "weekly 0 08:00 UTC",   # Monday
}


def tick_due_jobs(now: datetime | None = None) -> list[tuple[str, int]]:
    """Run any jobs whose scheduled time has passed since their last successful run.
    Returns list of (job_name, rows_written) for jobs that actually ran."""
    now = now or _now()
    ran: list[tuple[str, int]] = []

    if _should_run("poll_apps_daily", SCHEDULES["poll_apps_daily"], now):
        ran.append(("poll_apps_daily", poll_apps_daily()))
    if _should_run("aggregate_reviews_daily", SCHEDULES["aggregate_reviews_daily"], now):
        ran.append(("aggregate_reviews_daily", aggregate_reviews_daily()))
    if _should_run("poll_reviews_weekly", SCHEDULES["poll_reviews_weekly"], now):
        ran.append(("poll_reviews_weekly", poll_reviews_weekly()))
    if _should_run("compute_monthly_hot_apps", SCHEDULES["compute_monthly_hot_apps"], now):
        # Imported here to avoid a circular import at module load.
        import server as _srv
        rows = _wrap("compute_monthly_hot_apps", _srv.compute_monthly_hot_apps, force=False)
        ran.append(("compute_monthly_hot_apps", rows))

    return ran


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python3 poller.py {tick|poll_apps_daily|poll_reviews_weekly|aggregate_reviews_daily|status}")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "tick":
        print(tick_due_jobs())
    elif cmd == "poll_apps_daily":
        print(poll_apps_daily(force=True))
    elif cmd == "poll_reviews_weekly":
        print(poll_reviews_weekly(force=True))
    elif cmd == "aggregate_reviews_daily":
        print(aggregate_reviews_daily(force=True))
    elif cmd == "status":
        conn = _conn()
        try:
            for r in conn.execute(
                "SELECT job_name, started_at, finished_at, status, rows_written, errors "
                "FROM poller_runs ORDER BY started_at DESC LIMIT 20"
            ).fetchall():
                print(r)
        finally:
            conn.close()
    else:
        print(f"unknown: {cmd}")
        sys.exit(1)
