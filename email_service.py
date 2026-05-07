"""Mailjet integration + email templates + per-trigger dispatchers.

Triggers (one-shot per user, idempotent via email_triggers_seen):
  - WELCOME on signup
  - FIRST_FETCH on first reviews fetch
Recurring triggers (idempotent per send-window):
  - WEEKLY_DIGEST every Monday for active users
  - REENGAGE_7D once per user when last_seen_at is 7 days ago
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path("reviews.db")
MAILJET_URL = "https://api.mailjet.com/v3.1/send"


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- Mailjet send ----

def _mailjet_credentials() -> tuple[str, str, str, str] | None:
    api_key = os.environ.get("MAILJET_API_KEY")
    api_secret = os.environ.get("MAILJET_API_SECRET")
    from_email = os.environ.get("MAILJET_FROM_EMAIL")
    from_name = os.environ.get("MAILJET_FROM_NAME", "ViralFinder")
    if not (api_key and api_secret and from_email):
        return None
    return api_key, api_secret, from_email, from_name


def _send_mailjet(to_email: str, to_name: str | None, subject: str, html: str, text: str) -> tuple[bool, str | None, str | None]:
    """Returns (success, mailjet_id, error). When credentials are absent, prints to stderr and returns success=True."""
    creds = _mailjet_credentials()
    if not creds:
        print(f"[email/STUB] To: {to_email} | Subject: {subject}", file=sys.stderr)
        return True, "stub", None

    api_key, api_secret, from_email, from_name = creds
    body = {
        "Messages": [{
            "From": {"Email": from_email, "Name": from_name},
            "To": [{"Email": to_email, "Name": to_name or to_email}],
            "Subject": subject,
            "TextPart": text,
            "HTMLPart": html,
        }]
    }
    auth = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    req = urllib.request.Request(
        MAILJET_URL,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
        msg = (data.get("Messages") or [{}])[0]
        if msg.get("Status") == "success":
            mj_id = str((msg.get("To") or [{}])[0].get("MessageID", ""))
            return True, mj_id, None
        return False, None, json.dumps(msg)[:300]
    except Exception as e:
        return False, None, str(e)[:300]


# ---- Logging + idempotency ----

def _log_email(user_id: int | None, email: str, campaign: str, subject: str,
               status: str, error: str | None = None, mailjet_id: str | None = None) -> None:
    conn = _conn()
    try:
        conn.execute(
            """INSERT INTO email_log (user_id, email, campaign, subject, status, error,
                                       scheduled_at, sent_at, mailjet_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (user_id, email, campaign, subject, status, error,
             _now_iso(), _now_iso() if status == "sent" else None, mailjet_id),
        )
        conn.commit()
    finally:
        conn.close()


def _has_fired(user_id: int, trigger: str) -> bool:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM email_triggers_seen WHERE user_id = ? AND trigger = ?",
            (user_id, trigger),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def _mark_fired(user_id: int, trigger: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO email_triggers_seen (user_id, trigger, fired_at) VALUES (?,?,?)",
            (user_id, trigger, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def _send_in_thread(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


# ---- Templates ----

SITE_URL = os.environ.get("SITE_URL", "http://localhost:8000")


def _wrap_html(title: str, body: str) -> str:
    return f"""<!doctype html><html><body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#f7f8fb;margin:0;padding:24px">
<div style="max-width:520px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">
  <div style="font-size:14px;font-weight:600;color:#6ea8ff;letter-spacing:0.04em;text-transform:uppercase">ViralFinder</div>
  <h1 style="font-size:22px;margin:8px 0 16px">{title}</h1>
  {body}
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0"/>
  <p style="color:#888;font-size:12px;margin:0">You're receiving this because you signed up for ViralFinder. <a href="{SITE_URL}/account">Manage emails</a>.</p>
</div></body></html>"""


def _welcome(user: dict) -> tuple[str, str, str]:
    name = (user.get("name") or "there").split(" ")[0]
    subject = f"Welcome to ViralFinder, {name}"
    html = _wrap_html(
        f"Welcome, {name} 👋",
        f"""<p>Thanks for signing in. ViralFinder helps you find the next viral app idea by surfacing fast-growing apps and underserved niches across Google Play.</p>
        <p><strong>Try this first:</strong> open the <a href="{SITE_URL}/app">Viral Ranking</a> tab and switch to a category you care about. The list is sorted by velocity (installs/month), not lifetime installs.</p>
        <p>Want a 1:1 to map your niche? Just reply to this email.</p>
        <p style="margin-top:24px"><a href="{SITE_URL}/app" style="display:inline-block;background:#6ea8ff;color:#0f1115;padding:10px 16px;border-radius:6px;text-decoration:none;font-weight:600">Open dashboard</a></p>""",
    )
    text = f"Welcome to ViralFinder, {name}.\n\nOpen your dashboard: {SITE_URL}/app\n\nReply to this email if you want a hand mapping your niche."
    return subject, html, text


def _first_fetch(user: dict, package_name: str, count: int) -> tuple[str, str, str]:
    subject = f"Nice — first reviews fetched ({count})"
    html = _wrap_html(
        "You fetched your first app reviews",
        f"""<p>You just pulled <strong>{count:,} reviews</strong> for <code>{package_name}</code>.</p>
        <p>Next high-leverage moves:</p>
        <ul>
          <li>Sort by rating ascending — read 1★/2★ reviews to find <em>differentiation gaps</em>.</li>
          <li>Use the developer column to see every other app from this team — sometimes the best ideas are their own under-built side projects.</li>
          <li>Star this app in your watchlist (coming soon) so we can email you when its review velocity spikes.</li>
        </ul>
        <p style="margin-top:24px"><a href="{SITE_URL}/app" style="display:inline-block;background:#6ea8ff;color:#0f1115;padding:10px 16px;border-radius:6px;text-decoration:none;font-weight:600">Read the reviews</a></p>""",
    )
    text = f"Pulled {count} reviews for {package_name}.\n\nOpen reviews: {SITE_URL}/app"
    return subject, html, text


def _weekly_digest(user: dict, items: list[dict]) -> tuple[str, str, str]:
    rows = "".join(
        f"<li><strong>{i['title']}</strong> — {i.get('developer','')} · {i.get('installs','?')} installs · {round(i.get('viral_score',0),1)} viral score</li>"
        for i in items[:10]
    )
    subject = f"Your weekly viral picks ({len(items)})"
    html = _wrap_html(
        "This week's viral candidates",
        f"<p>Here are the apps that gained the most velocity this week in the niches you've explored:</p><ol>{rows}</ol>"
        f"<p style='margin-top:24px'><a href='{SITE_URL}/app' style='display:inline-block;background:#6ea8ff;color:#0f1115;padding:10px 16px;border-radius:6px;text-decoration:none;font-weight:600'>Open the full ranking</a></p>",
    )
    text = "This week's viral candidates:\n\n" + "\n".join(f"- {i['title']}" for i in items[:10]) + f"\n\n{SITE_URL}/app"
    return subject, html, text


def _reengage(user: dict) -> tuple[str, str, str]:
    name = (user.get("name") or "there").split(" ")[0]
    subject = "Found anything yet?"
    html = _wrap_html(
        f"Quick check-in, {name}",
        f"""<p>You signed up about a week ago and haven't been back. We thought we'd nudge.</p>
        <p>If ViralFinder isn't clicking yet, what's missing? Reply to this email — even 1-line answers help.</p>
        <p>If you're still curious, here's the fastest way to get value:</p>
        <ol>
          <li>Open <a href="{SITE_URL}/app">the Niches tab</a></li>
          <li>Pick <strong>"Best opportunity"</strong> sort</li>
          <li>Click the niche with the lowest competition + decent install ceiling</li>
        </ol>""",
    )
    text = f"Hey {name}, you signed up a week ago and haven't been back. Anything broken or missing? Reply to this email.\n\n{SITE_URL}/app"
    return subject, html, text


# ---- Public API: triggers ----

def fire_welcome(user: dict) -> None:
    if _has_fired(user["id"], "WELCOME"):
        return
    subject, html, text = _welcome(user)
    def _go():
        ok, mjid, err = _send_mailjet(user["email"], user.get("name"), subject, html, text)
        _log_email(user["id"], user["email"], "WELCOME", subject,
                   "sent" if ok else "failed", err, mjid)
        if ok:
            _mark_fired(user["id"], "WELCOME")
    _send_in_thread(_go)


def fire_first_fetch(user: dict, package_name: str, count: int) -> None:
    if _has_fired(user["id"], "FIRST_FETCH"):
        return
    subject, html, text = _first_fetch(user, package_name, count)
    def _go():
        ok, mjid, err = _send_mailjet(user["email"], user.get("name"), subject, html, text)
        _log_email(user["id"], user["email"], "FIRST_FETCH", subject,
                   "sent" if ok else "failed", err, mjid)
        if ok:
            _mark_fired(user["id"], "FIRST_FETCH")
    _send_in_thread(_go)


def fire_weekly_digest_all() -> int:
    """Send the weekly digest to every active user. Idempotent within the calendar week."""
    conn = _conn()
    try:
        users = conn.execute("SELECT id, email, name, picture, plan, is_admin FROM users").fetchall()
    finally:
        conn.close()
    sent = 0
    for u in users:
        user = {"id": u[0], "email": u[1], "name": u[2], "picture": u[3], "plan": u[4], "is_admin": bool(u[5])}
        # ID this week: WEEKLY_DIGEST_<isocalendar-year-week>
        year, week, _ = datetime.now(timezone.utc).isocalendar()
        trigger = f"WEEKLY_DIGEST_{year}W{week:02d}"
        if _has_fired(user["id"], trigger):
            continue
        items = _top_viral_for_user(user)
        if not items:
            continue
        subject, html, text = _weekly_digest(user, items)
        ok, mjid, err = _send_mailjet(user["email"], user.get("name"), subject, html, text)
        _log_email(user["id"], user["email"], "WEEKLY_DIGEST", subject,
                   "sent" if ok else "failed", err, mjid)
        if ok:
            _mark_fired(user["id"], trigger)
            sent += 1
    return sent


def fire_reengage_eligible() -> int:
    """For each user inactive ~7d, fire one re-engagement email (one-shot per user)."""
    cutoff_low = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    cutoff_high = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    conn = _conn()
    try:
        users = conn.execute(
            """SELECT id, email, name, picture, plan, is_admin FROM users
               WHERE last_seen_at IS NOT NULL
                 AND last_seen_at >= ? AND last_seen_at <= ?""",
            (cutoff_low, cutoff_high),
        ).fetchall()
    finally:
        conn.close()
    sent = 0
    for u in users:
        user = {"id": u[0], "email": u[1], "name": u[2], "picture": u[3], "plan": u[4], "is_admin": bool(u[5])}
        if _has_fired(user["id"], "REENGAGE_7D"):
            continue
        subject, html, text = _reengage(user)
        ok, mjid, err = _send_mailjet(user["email"], user.get("name"), subject, html, text)
        _log_email(user["id"], user["email"], "REENGAGE_7D", subject,
                   "sent" if ok else "failed", err, mjid)
        if ok:
            _mark_fired(user["id"], "REENGAGE_7D")
            sent += 1
    return sent


def _top_viral_for_user(user: dict, limit: int = 10) -> list[dict]:
    """Lightweight top-N from apps_ranked. Doesn't yet personalize by saved niches."""
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT title, developer, installs, min_installs, score, released
               FROM apps_ranked
               WHERE min_installs >= 10000 AND released >= date('now', '-2 years')
               ORDER BY (CAST(min_installs AS REAL) /
                         MAX(1.0, (julianday('now') - julianday(released)) / 30.0)) DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"title": r[0], "developer": r[1], "installs": r[2],
         "min_installs": r[3], "score": r[4], "released": r[5],
         "viral_score": _ipm(r[3], r[5])}
        for r in rows
    ]


def _ipm(min_installs: int | None, released: str | None) -> float:
    if not min_installs or not released:
        return 0.0
    try:
        d = datetime.fromisoformat(released)
    except Exception:
        return 0.0
    months = max(1.0, (datetime.now(timezone.utc) - d.replace(tzinfo=timezone.utc)).days / 30.0)
    return min_installs / months


# ---- Stats for admin dashboard ----

def email_stats(days: int = 30) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT date(scheduled_at) AS d, campaign, status, COUNT(*) AS n
               FROM email_log WHERE scheduled_at >= ?
               GROUP BY d, campaign, status
               ORDER BY d""",
            (cutoff,),
        ).fetchall()
        by_camp = conn.execute(
            """SELECT campaign,
                      SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent,
                      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
               FROM email_log WHERE scheduled_at >= ?
               GROUP BY campaign""",
            (cutoff,),
        ).fetchall()
        recent = conn.execute(
            """SELECT id, email, campaign, subject, status, error, scheduled_at, sent_at
               FROM email_log ORDER BY scheduled_at DESC LIMIT 50"""
        ).fetchall()
    finally:
        conn.close()
    return {
        "daily": [{"date": r[0], "campaign": r[1], "status": r[2], "count": r[3]} for r in rows],
        "by_campaign": [{"campaign": r[0], "sent": r[1], "failed": r[2]} for r in by_camp],
        "recent": [
            {"id": r[0], "email": r[1], "campaign": r[2], "subject": r[3],
             "status": r[4], "error": r[5], "scheduled_at": r[6], "sent_at": r[7]}
            for r in recent
        ],
    }
