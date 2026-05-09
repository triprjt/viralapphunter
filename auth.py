"""Google OAuth + session management for ViralFinder."""
from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path("reviews.db")
SESSION_TTL_DAYS = 30


# ---- Schema ----

DDL = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT UNIQUE NOT NULL,
  name          TEXT,
  google_sub    TEXT UNIQUE,
  picture       TEXT,
  plan          TEXT NOT NULL DEFAULT 'free',
  is_admin      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  last_seen_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS sessions (
  token       TEXT PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  ip          TEXT,
  user_agent  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS email_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
  email         TEXT NOT NULL,
  campaign      TEXT NOT NULL,
  subject       TEXT,
  status        TEXT NOT NULL,
  error         TEXT,
  scheduled_at  TEXT NOT NULL,
  sent_at       TEXT,
  mailjet_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_email_log_user_camp ON email_log(user_id, campaign);
CREATE INDEX IF NOT EXISTS idx_email_log_scheduled ON email_log(scheduled_at);

-- Track which one-shot triggers have fired per user (e.g. welcome, first-fetch).
CREATE TABLE IF NOT EXISTS email_triggers_seen (
  user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  trigger   TEXT NOT NULL,
  fired_at  TEXT NOT NULL,
  PRIMARY KEY (user_id, trigger)
);

CREATE TABLE IF NOT EXISTS oauth_states (
  state       TEXT PRIMARY KEY,
  created_at  TEXT NOT NULL,
  next_url    TEXT
);

-- Lifetime feature samples for "generous taster" Free-tier enforcement.
-- Composite PK (user_id, feature, context) lets the same target be retried without double-counting,
-- while still enforcing 1-distinct-target per lifetime per feature.
CREATE TABLE IF NOT EXISTS feature_usage (
  user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  feature   TEXT NOT NULL,
  used_at   TEXT NOT NULL,
  context   TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (user_id, feature, context)
);
CREATE INDEX IF NOT EXISTS idx_feature_usage_user ON feature_usage(user_id, feature);

-- Per-user activity log: every meaningful action the user takes inside the app.
-- Used for the Activity panel + future analytics.
CREATE TABLE IF NOT EXISTS user_activity (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  action      TEXT NOT NULL,        -- 'login' | 'logout' | 'onboarding_complete' | 'review_fetch' | 'developer_lookup' | 'discover_category' | 'view_reviews' | 'paywall_hit'
  context     TEXT,                 -- pkg / dev id / category id / niche term
  metadata    TEXT,                 -- JSON blob with extras (count, success, etc.)
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_user_time ON user_activity(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS user_watchlist (
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  package_name TEXT NOT NULL,
  starred_at   TEXT NOT NULL,
  PRIMARY KEY (user_id, package_name)
);

CREATE TABLE IF NOT EXISTS user_saved_searches (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS weekly_reports (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  category_id   TEXT NOT NULL,
  year          INTEGER NOT NULL,
  week          INTEGER NOT NULL,
  generated_at  TEXT NOT NULL,
  payload       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_weekly_unique ON weekly_reports(category_id, year, week);

-- Cached top-N viral apps per category, refreshed weekly. Drives the dashboard.
-- Bead 6 + Bead 7.
CREATE TABLE IF NOT EXISTS monthly_hot_apps (
  category_id   TEXT NOT NULL,
  computed_at   TEXT NOT NULL,
  rank          INTEGER NOT NULL,
  package_name  TEXT NOT NULL,
  payload       TEXT NOT NULL,
  PRIMARY KEY (category_id, computed_at, rank)
);
CREATE INDEX IF NOT EXISTS idx_mha_latest ON monthly_hot_apps(category_id, computed_at DESC);

-- Per-package metadata snapshots; one row per detected change. Drives 30-day deltas.
-- Bead 9.
CREATE TABLE IF NOT EXISTS app_history (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  package_name  TEXT NOT NULL,
  captured_at   TEXT NOT NULL,
  min_installs  INTEGER,
  score         REAL,
  ratings_count INTEGER,
  reviews_count INTEGER,
  histogram_1   INTEGER, histogram_2 INTEGER, histogram_3 INTEGER,
  histogram_4   INTEGER, histogram_5 INTEGER,
  version       TEXT,
  recent_changes TEXT
);
CREATE INDEX IF NOT EXISTS idx_apphist ON app_history(package_name, captured_at DESC);

-- Daily review buckets per (package, country, date). Powers low-star-% trend.
-- Bead 9.
CREATE TABLE IF NOT EXISTS reviews_daily (
  package_name TEXT NOT NULL,
  country      TEXT NOT NULL,
  date         TEXT NOT NULL,
  count        INTEGER NOT NULL,
  avg_rating   REAL,
  rating_1     INTEGER, rating_2 INTEGER, rating_3 INTEGER,
  rating_4     INTEGER, rating_5 INTEGER,
  PRIMARY KEY (package_name, country, date)
);

-- Idempotency + observability for scheduled poller jobs.
-- Bead 9 + Bead 10.
CREATE TABLE IF NOT EXISTS poller_runs (
  job_name      TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  status        TEXT,
  rows_written  INTEGER DEFAULT 0,
  errors        TEXT,
  PRIMARY KEY (job_name, started_at)
);
CREATE INDEX IF NOT EXISTS idx_poller_runs_job ON poller_runs(job_name, started_at DESC);
"""


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_schema() -> None:
    conn = _conn()
    try:
        conn.executescript(DDL)
        # Defensive ALTERs for pre-existing DBs that don't have onboarding columns yet.
        existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "onboarded" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 0")
        if "picked_categories" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN picked_categories TEXT")
        if "picked_goal" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN picked_goal TEXT")
        if "theme" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN theme TEXT NOT NULL DEFAULT 'light'")
        if "newsletter_subscribed" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN newsletter_subscribed INTEGER NOT NULL DEFAULT 0")
        if "trial_started_at" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN trial_started_at TEXT")
        # Defensive ALTER for apps_enriched.qa_json (per-app Q&A snapshot for the
        # detail page's About section). Built by app_qa.py.
        try:
            ae_cols = {r[1] for r in conn.execute("PRAGMA table_info(apps_enriched)").fetchall()}
            if ae_cols and "qa_json" not in ae_cols:
                conn.execute("ALTER TABLE apps_enriched ADD COLUMN qa_json TEXT")
            if ae_cols and "qa_updated_at" not in ae_cols:
                conn.execute("ALTER TABLE apps_enriched ADD COLUMN qa_updated_at TEXT")
            if ae_cols and "review_sentiment_json" not in ae_cols:
                conn.execute("ALTER TABLE apps_enriched ADD COLUMN review_sentiment_json TEXT")
            if ae_cols and "review_sentiment_updated_at" not in ae_cols:
                conn.execute("ALTER TABLE apps_enriched ADD COLUMN review_sentiment_updated_at TEXT")
        except sqlite3.OperationalError:
            # apps_enriched not yet created on a fresh install — discovery DDL handles it.
            pass
        conn.commit()
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- Google OAuth flow ----

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


def _site_url() -> str:
    return os.environ.get("SITE_URL", "http://localhost:8000").rstrip("/")


def _redirect_uri() -> str:
    return f"{_site_url()}/auth/google/callback"


def google_auth_url(next_url: str = "/dashboard") -> str:
    """Generates the Google OAuth authorization URL with a fresh CSRF state token."""
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    if not client_id:
        raise RuntimeError("GOOGLE_CLIENT_ID not set")
    state = secrets.token_urlsafe(24)
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO oauth_states (state, created_at, next_url) VALUES (?, ?, ?)",
            (state, now_iso(), next_url),
        )
        conn.commit()
    finally:
        conn.close()
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _consume_state(state: str) -> str | None:
    """Returns the stored next_url for a state token, deleting it (one-time use). None if invalid."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT next_url, created_at FROM oauth_states WHERE state = ?", (state,)
        ).fetchone()
        if not row:
            return None
        # Reject stale states older than 10 minutes
        try:
            created = datetime.fromisoformat(row[1])
            if datetime.now(timezone.utc) - created > timedelta(minutes=10):
                conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
                conn.commit()
                return None
        except Exception:
            pass
        conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        conn.commit()
        return row[0] or "/dashboard"
    finally:
        conn.close()


def _exchange_code_for_token(code: str) -> dict:
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "redirect_uri": _redirect_uri(),
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _fetch_userinfo(access_token: str) -> dict:
    req = urllib.request.Request(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def handle_oauth_callback(code: str, state: str, ip: str = None, ua: str = None) -> tuple[str, str, bool]:
    """Returns (session_token, next_url, is_new_user). Raises on invalid state/code."""
    next_url = _consume_state(state)
    if next_url is None:
        raise ValueError("invalid or expired OAuth state")
    token = _exchange_code_for_token(code)
    if "access_token" not in token:
        raise ValueError(f"OAuth token exchange failed: {token}")
    info = _fetch_userinfo(token["access_token"])
    google_sub = info.get("id") or info.get("sub")
    email = info.get("email")
    if not email:
        raise ValueError("no email returned from Google")

    conn = _conn()
    try:
        existing = conn.execute(
            "SELECT id, name, picture FROM users WHERE google_sub = ? OR email = ?",
            (google_sub, email),
        ).fetchone()
        is_new = existing is None
        if existing:
            user_id = existing[0]
            conn.execute(
                "UPDATE users SET google_sub=?, name=?, picture=?, last_seen_at=? WHERE id=?",
                (google_sub, info.get("name") or existing[1], info.get("picture") or existing[2], now_iso(), user_id),
            )
        else:
            cur = conn.execute(
                "INSERT INTO users (email, name, google_sub, picture, created_at, last_seen_at) VALUES (?,?,?,?,?,?)",
                (email, info.get("name"), google_sub, info.get("picture"), now_iso(), now_iso()),
            )
            user_id = cur.lastrowid
        sess_token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)).isoformat()
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at, ip, user_agent) VALUES (?,?,?,?,?,?)",
            (sess_token, user_id, now_iso(), expires, ip, (ua or "")[:300]),
        )
        conn.commit()
    finally:
        conn.close()
    return sess_token, next_url, is_new


# ---- Session helpers ----

def get_user_by_session(token: str) -> dict | None:
    if not token:
        return None
    conn = _conn()
    try:
        row = conn.execute(
            """SELECT u.id, u.email, u.name, u.picture, u.plan, u.is_admin,
                      u.onboarded, u.picked_categories, u.picked_goal, u.theme,
                      u.newsletter_subscribed, u.trial_started_at, s.expires_at
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ?""",
            (token,),
        ).fetchone()
        if not row:
            return None
        try:
            if datetime.fromisoformat(row[12]) < datetime.now(timezone.utc):
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                conn.commit()
                return None
        except Exception:
            pass
        conn.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (now_iso(), row[0]))
        conn.commit()
        return {
            "id": row[0], "email": row[1], "name": row[2], "picture": row[3],
            "plan": row[4], "is_admin": bool(row[5]),
            "onboarded": bool(row[6]),
            "picked_categories": row[7] or "",
            "picked_goal": row[8] or "",
            "theme": row[9] or "light",
            "newsletter_subscribed": bool(row[10]),
            "trial_started_at": row[11],
        }
    finally:
        conn.close()


# ---- Feature gating (Free-tier "generous taster") ----

PAID_PLANS = {"solo", "pro", "studio"}

# Quota matrix: per (feature, plan) → (cap, window). cap=None means unlimited.
# Window 'week' = rolling 7 days. 'lifetime' = forever.
QUOTAS = {
    "review_fetch": {
        "free":   (3,    "week"),
        "solo":   (None, "week"),
        "pro":    (None, "week"),
        "studio": (None, "week"),
    },
    "export": {
        "free":   (1,    "week"),
        "solo":   (10,   "week"),
        "pro":    (None, "week"),
        "studio": (None, "week"),
    },
    "developer_lookup": {
        "free":   (1,    "lifetime"),
        "solo":   (None, "lifetime"),
        "pro":    (None, "lifetime"),
        "studio": (None, "lifetime"),
    },
    "ai_summary": {
        "free":   (1,    "lifetime"),
        "solo":   (None, "lifetime"),
        "pro":    (None, "lifetime"),
        "studio": (None, "lifetime"),
    },
}


def _quota_for(feature: str, plan: str) -> tuple[int | None, str]:
    return QUOTAS.get(feature, {}).get(plan, (1, "lifetime"))


def can_use_feature(user_id: int, feature: str, plan: str, context: str = "") -> tuple[bool, dict]:
    """Returns (allowed, details). Cap+window come from QUOTAS; idempotent same-context retries pass."""
    cap, window = _quota_for(feature, plan)
    if cap is None:
        return True, {}
    conn = _conn()
    try:
        same = conn.execute(
            "SELECT 1 FROM feature_usage WHERE user_id=? AND feature=? AND context=?",
            (user_id, feature, context or ""),
        ).fetchone()
        if same and window == "lifetime":
            # Lifetime: same-context retry is always free.
            return True, {"reason": "already_used_same_context"}
        if window == "week":
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            # Count distinct contexts inside the window.
            n = conn.execute(
                "SELECT COUNT(DISTINCT context) FROM feature_usage "
                "WHERE user_id=? AND feature=? AND used_at >= ?",
                (user_id, feature, cutoff),
            ).fetchone()[0]
            # Same-context inside window? Free retry.
            in_window_same = conn.execute(
                "SELECT 1 FROM feature_usage WHERE user_id=? AND feature=? AND context=? AND used_at >= ?",
                (user_id, feature, context or "", cutoff),
            ).fetchone()
            if in_window_same:
                return True, {"reason": "already_used_same_context", "window": window, "used": n, "cap": cap}
            if n >= cap:
                next_reset_row = conn.execute(
                    "SELECT MIN(used_at) FROM feature_usage WHERE user_id=? AND feature=? AND used_at >= ?",
                    (user_id, feature, cutoff),
                ).fetchone()
                next_reset = None
                if next_reset_row and next_reset_row[0]:
                    try:
                        next_reset = (datetime.fromisoformat(next_reset_row[0]) + timedelta(days=7)).isoformat()
                    except Exception:
                        next_reset = None
                return False, {
                    "reason": "weekly_limit_hit", "feature": feature, "window": window,
                    "used": n, "cap": cap, "next_reset": next_reset,
                }
            return True, {"window": window, "used": n, "cap": cap}
        # Lifetime
        n = conn.execute(
            "SELECT COUNT(*) FROM feature_usage WHERE user_id=? AND feature=?",
            (user_id, feature),
        ).fetchone()[0]
    finally:
        conn.close()
    if n >= cap:
        return False, {"reason": "free_tier_used", "feature": feature, "window": "lifetime",
                        "used": n, "cap": cap}
    return True, {"window": "lifetime", "used": n, "cap": cap}


def get_quota_status(user_id: int, plan: str) -> dict:
    """Returns per-feature {used, cap, window, next_reset} for the dashboard quota tracker."""
    out = {}
    conn = _conn()
    try:
        for feature in ("review_fetch", "developer_lookup", "ai_summary", "export"):
            cap, window = _quota_for(feature, plan)
            if cap is None:
                out[feature] = {"used": 0, "cap": None, "window": window, "next_reset": None}
                continue
            if window == "week":
                cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                n = conn.execute(
                    "SELECT COUNT(DISTINCT context) FROM feature_usage "
                    "WHERE user_id=? AND feature=? AND used_at >= ?",
                    (user_id, feature, cutoff),
                ).fetchone()[0]
                next_reset = None
                if n >= cap:
                    row = conn.execute(
                        "SELECT MIN(used_at) FROM feature_usage WHERE user_id=? AND feature=? AND used_at >= ?",
                        (user_id, feature, cutoff),
                    ).fetchone()
                    if row and row[0]:
                        try:
                            next_reset = (datetime.fromisoformat(row[0]) + timedelta(days=7)).isoformat()
                        except Exception:
                            pass
                out[feature] = {"used": n, "cap": cap, "window": "week", "next_reset": next_reset}
            else:
                n = conn.execute(
                    "SELECT COUNT(*) FROM feature_usage WHERE user_id=? AND feature=?",
                    (user_id, feature),
                ).fetchone()[0]
                out[feature] = {"used": n, "cap": cap, "window": "lifetime", "next_reset": None}
    finally:
        conn.close()
    return out


def record_feature_use(user_id: int, feature: str, context: str = "") -> None:
    """Upserts (user_id, feature, context) and refreshes used_at — so weekly windows track most-recent use."""
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO feature_usage (user_id, feature, used_at, context) VALUES (?, ?, ?, ?)",
            (user_id, feature, now_iso(), context or ""),
        )
        conn.commit()
    finally:
        conn.close()


def get_feature_usage_summary(user_id: int) -> dict:
    """Returns per-feature counts for the dashboard's gating UI."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT feature, COUNT(*) FROM feature_usage WHERE user_id=? GROUP BY feature",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return {feature: count for feature, count in rows}


def destroy_session(token: str) -> None:
    conn = _conn()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def parse_cookie(header: str | None, name: str) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return urllib.parse.unquote(v)
    return None


SESSION_COOKIE = "vf_session"


def session_cookie(token: str) -> str:
    return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL_DAYS*86400}"


def clear_cookie() -> str:
    return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


# ---- Per-user activity log ----

def log_activity(user_id: int, action: str, context: str = "", metadata: dict | None = None) -> None:
    """Record one user action. All inserts are best-effort — failures must not break the user flow."""
    try:
        conn = _conn()
        try:
            meta_json = json.dumps(metadata) if metadata else None
            conn.execute(
                "INSERT INTO user_activity (user_id, action, context, metadata, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, action, context or "", meta_json, now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # never crash a request because activity logging failed


# ---- Watchlist ----

def watchlist_list(user_id: int) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT package_name, starred_at FROM user_watchlist WHERE user_id=? ORDER BY starred_at DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [{"package_name": r[0], "starred_at": r[1]} for r in rows]


def watchlist_add(user_id: int, package_name: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO user_watchlist (user_id, package_name, starred_at) VALUES (?, ?, ?)",
            (user_id, package_name, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def watchlist_remove(user_id: int, package_name: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "DELETE FROM user_watchlist WHERE user_id=? AND package_name=?",
            (user_id, package_name),
        )
        conn.commit()
    finally:
        conn.close()


# ---- Saved searches ----

def saved_searches_list(user_id: int) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, name, payload, created_at FROM user_saved_searches WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try: payload = json.loads(r[2])
        except Exception: payload = {}
        out.append({"id": r[0], "name": r[1], "payload": payload, "created_at": r[3]})
    return out


def saved_search_add(user_id: int, name: str, payload: dict) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT OR REPLACE INTO user_saved_searches (user_id, name, payload, created_at) VALUES (?, ?, ?, ?)",
            (user_id, name, json.dumps(payload), now_iso()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def saved_search_remove(user_id: int, search_id: int) -> None:
    conn = _conn()
    try:
        conn.execute(
            "DELETE FROM user_saved_searches WHERE user_id=? AND id=?",
            (user_id, search_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---- Theme ----

def set_user_theme(user_id: int, theme: str) -> None:
    if theme not in ("dark", "light"):
        return
    conn = _conn()
    try:
        conn.execute("UPDATE users SET theme=? WHERE id=?", (theme, user_id))
        conn.commit()
    finally:
        conn.close()


def get_recent_activity(user_id: int, limit: int = 100) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, action, context, metadata, created_at FROM user_activity "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        meta = {}
        if r[3]:
            try: meta = json.loads(r[3])
            except Exception: meta = {}
        out.append({"id": r[0], "action": r[1], "context": r[2], "metadata": meta, "at": r[4]})
    return out
