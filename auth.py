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


def google_auth_url(next_url: str = "/app") -> str:
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
        return row[0] or "/app"
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
                      u.onboarded, u.picked_categories, u.picked_goal, s.expires_at
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ?""",
            (token,),
        ).fetchone()
        if not row:
            return None
        try:
            if datetime.fromisoformat(row[9]) < datetime.now(timezone.utc):
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
        }
    finally:
        conn.close()


# ---- Feature gating (Free-tier "generous taster") ----

# Each feature: 1 distinct context per lifetime on Free. Paid tiers: unlimited (for now;
# monthly quotas will layer on top later).
PAID_PLANS = {"solo", "pro", "studio"}


def can_use_feature(user_id: int, feature: str, plan: str, context: str = "") -> tuple[bool, dict]:
    """Returns (allowed, details). Free plan caps at 1 distinct context per feature, lifetime."""
    if plan in PAID_PLANS:
        return True, {}
    conn = _conn()
    try:
        # Already used this exact target? Allow (idempotent retry).
        same = conn.execute(
            "SELECT 1 FROM feature_usage WHERE user_id=? AND feature=? AND context=?",
            (user_id, feature, context or ""),
        ).fetchone()
        if same:
            return True, {"reason": "already_used_same_context"}
        # Distinct count
        n = conn.execute(
            "SELECT COUNT(*) FROM feature_usage WHERE user_id=? AND feature=?",
            (user_id, feature),
        ).fetchone()[0]
    finally:
        conn.close()
    if n >= 1:
        return False, {"reason": "free_tier_used", "feature": feature, "samples_used": n}
    return True, {}


def record_feature_use(user_id: int, feature: str, context: str = "") -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO feature_usage (user_id, feature, used_at, context) VALUES (?, ?, ?, ?)",
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
