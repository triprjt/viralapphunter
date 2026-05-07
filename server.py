from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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


DB_PATH = Path("reviews.db")
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
                if row and row[1] == "done" and user_row:
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


def _start_fetch(pkg: str, user_id: int | None = None) -> dict:
    """Enqueue a reviews-fetch job. The bounded worker pool consumes it."""
    _ensure_fetch_workers()
    # Skip if already pending/running
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT state FROM app_review_status WHERE package_name = ?", (pkg,)
        ).fetchone()
    finally:
        conn.close()
    if row and row[0] in ("queued", "fetching"):
        return {"ok": True, "state": row[0], "already_running": True, "queue_size": _fetch_queue.qsize()}
    _set_status(pkg, state="queued", error=None, started_at=None, completed_at=None)
    _fetch_queue.put((pkg, user_id))
    return {"ok": True, "state": "queued", "queue_size": _fetch_queue.qsize()}


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
                auth_url = auth_mod.google_auth_url(next_url="/app")
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
        if url.path == "/app" or url.path == "/app/":
            user = self._current_user()
            if not user:
                self._send_redirect("/auth/google/start")
                return
            try:
                html = (Path(__file__).parent / "index.html").read_text()
                # Inject user info as a meta tag the dashboard can read.
                meta = (
                    f'<meta name="vf-user" '
                    f'data-email="{user["email"]}" '
                    f'data-name="{user.get("name") or ""}" '
                    f'data-picture="{user.get("picture") or ""}" '
                    f'data-admin="{"1" if user.get("is_admin") else "0"}" '
                    f'data-plan="{user.get("plan", "free")}" '
                    f'data-onboarded="{"1" if user.get("onboarded") else "0"}" '
                    f'data-categories="{user.get("picked_categories") or ""}" '
                    f'data-goal="{user.get("picked_goal") or ""}" />'
                )
                html = html.replace("</head>", meta + "</head>", 1)
                self._send_html(200, html)
            except Exception as e:
                self._send_html(500, f"<h1>App shell missing</h1><pre>{e}</pre>")
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
            })
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

    def do_POST(self):
        url = urlparse(self.path)

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
                                          context=pkg, metadata={"feature": "review_fetch"})
                    self._send_json(403, {"ok": False, "error": "free_tier_used", "feature": "review_fetch", **details})
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


def _email_scheduler() -> None:
    """Background loop that fires weekly-digest + re-engagement emails on schedule."""
    import time
    last_weekly_run = None
    last_reengage_run = None
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
            # Re-engagement: run hourly; the email_service caps to one per user
            daykey = (now.year, now.month, now.day, now.hour)
            if last_reengage_run != daykey:
                n = email_mod.fire_reengage_eligible()
                if n:
                    print(f"[email scheduler] re-engagement: {n} sent", flush=True)
                last_reengage_run = daykey
        except Exception as e:
            print(f"[email scheduler] error: {e}", flush=True)
        time.sleep(900)  # 15 minutes


def main() -> None:
    _ensure_schema()
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
