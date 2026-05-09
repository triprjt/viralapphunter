"""Per-app Q&A builder: turns the raw Play Store data we already capture into
plain-English answers to the questions a creator scoping a niche actually asks.

Persisted in apps_enriched.qa_json so the detail page can render instantly.

Run as a CLI:
    python3 app_qa.py --all                # build for every enriched app
    python3 app_qa.py --pkg com.foo.bar    # build for one
    python3 app_qa.py --stale 7            # rebuild any older than 7 days

The 10 questions and their data sources:
    1. what_it_does       — first sentence of description (discovered_apps.summary)
    2. target_user        — inferred from genre + content_rating + keywords
    3. why_loved          — top 4–5★ review excerpt with user_name
    4. user_pain          — top 1–2★ review excerpt
    5. monetization       — combination of contains_ads + offers_iap + iap_price_range
    6. niche_density      — count of competitors in same categories CSV
    7. niche_leader       — top app by installs_per_month in the same niche
    8. dev_seriousness    — # of sibling apps + total install footprint
    9. freshness          — days since last update + recent_changes excerpt
   10. verdict            — composite read combining velocity + low-star + saturation

Each value is a dict with `label` (UI heading) and `answer` (one-line text), so
the front-end can render uniformly.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("reviews.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


# ---------- Helpers ----------

def _first_sentence(text: str | None, max_chars: int = 220) -> str:
    """Pull the first sensible sentence from a long description. Falls back to
    a hard char-cap if no sentence terminator appears in the first ~max_chars."""
    if not text:
        return ""
    t = text.strip()
    # Normalize whitespace + drop noisy header lines (quoted reviews, all-caps)
    t = re.sub(r"\s+", " ", t)
    # Find first sentence break in the first ~600 chars
    head = t[:600]
    # Match end of sentence: . ! ? followed by space + Capital letter or end
    m = re.search(r"[.!?](?=\s+[A-Z])", head)
    if m:
        end = m.end()
        sent = head[:end].strip()
        if 30 <= len(sent) <= max_chars:
            return sent
    # Fallback: hard truncate to max_chars at word boundary
    cut = t[:max_chars]
    if " " in cut[-30:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(",;:- ") + ("…" if len(t) > len(cut) else "")


def _money_label(contains_ads: int | None, offers_iap: int | None, iap_price: str | None,
                 free: int | None, price: float | None) -> str:
    paid = (free is not None and not free) or (price is not None and price > 0)
    if paid:
        return f"💰 Paid app (${price:.2f})" if price else "💰 Paid app"
    if offers_iap:
        if iap_price:
            return f"💳 Free + in-app purchases ({iap_price})"
        return "💳 Free + in-app purchases"
    if contains_ads:
        return "📢 Free, ad-supported"
    return "🎁 Fully free, no ads"


def _humanize_count(n: int | None) -> str:
    if not n:
        return "0"
    n = int(n)
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:     return f"{n/1_000:.0f}K"
    return str(n)


def _days_since(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso[:10]).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).days
    except Exception:
        return None


def _category_ids(cats_csv: str | None) -> list[str]:
    return [c for c in (cats_csv or "").split(",") if c]


# ---------- Question builders ----------

def _q_what_it_does(description: str | None, title: str | None) -> str:
    s = _first_sentence(description, max_chars=220)
    if s:
        return s
    if title:
        return f"App called \"{title}\". No description text was captured during discovery."
    return "Description not captured."


def _q_target_user(genre: str | None, content_rating: str | None, description: str | None) -> str:
    g = (genre or "").strip()
    cr = (content_rating or "").strip()
    desc_lower = (description or "").lower()[:1500]

    # Audience hints from description keywords
    audience_hints = []
    if any(k in desc_lower for k in ("kids", "children", "preschool", "toddler", "5-year")):
        audience_hints.append("kids / parents")
    if any(k in desc_lower for k in ("students", "exam", "neet", "upsc", "jee", "study", "school")):
        audience_hints.append("students")
    if any(k in desc_lower for k in ("women", "mothers", "pregnan", "postpartum")):
        audience_hints.append("women")
    if any(k in desc_lower for k in ("men ", " men,", "gentlem")):
        audience_hints.append("men")
    if any(k in desc_lower for k in ("seniors", "elderly", "retire")):
        audience_hints.append("seniors")
    if any(k in desc_lower for k in ("muslim", "namaz", "ramadan", "quran")):
        audience_hints.append("Muslim users")
    if any(k in desc_lower for k in ("hindu", "bhakti", "temple", "puja", "mantra")):
        audience_hints.append("Hindu / spiritual users")
    if any(k in desc_lower for k in ("freelancer", "creator", "entrepreneur", "small business")):
        audience_hints.append("solo professionals")
    if any(k in desc_lower for k in ("fitness", "workout", "gym", "yoga")):
        audience_hints.append("fitness-curious adults")

    parts = []
    if audience_hints:
        parts.append(", ".join(audience_hints[:2]))
    if g:
        parts.append(f"interested in {g.lower()}")
    if cr and cr.lower() != "everyone":
        parts.append(f"({cr})")

    if parts:
        return "People " + ", ".join(parts) + "."
    if g:
        return f"General users in the {g} category."
    return "Audience not clearly indicated by metadata."


def _load_sentiment(conn: sqlite3.Connection, pkg: str) -> dict | None:
    """Load the cached sentiment analysis for a package, if any."""
    row = conn.execute(
        "SELECT review_sentiment_json FROM apps_enriched WHERE package_name = ?",
        (pkg,),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def _format_themes(themes: list[dict], max_themes: int = 3) -> str:
    """'spiritual learning (13%), daily spiritual (8%), calming content (8%)'"""
    parts = []
    for t in themes[:max_themes]:
        theme_name = t.get("theme", "")
        pct = t.get("freq_pct")
        if pct is not None:
            parts.append(f"{theme_name} ({pct:.0f}%)")
        else:
            parts.append(theme_name)
    return ", ".join(parts)


def _q_why_loved(conn: sqlite3.Connection, pkg: str) -> str:
    """Theme-based aggregation when we have sentiment data; otherwise fall back
    to single-review excerpt; otherwise prompt to fetch reviews."""
    s = _load_sentiment(conn, pkg)
    if s:
        bucket = s.get("buckets", {}).get("high") or {}
        themes = bucket.get("themes") or []
        n_high = bucket.get("review_count", 0)
        if themes:
            theme_list = _format_themes(themes, max_themes=3)
            ex = themes[0].get("exemplar") or {}
            ex_snippet = (ex.get("snippet") or "").strip()
            ex_user = ex.get("user") or "a recent user"
            quote = f' Top quote: "{ex_snippet[:140]}{"…" if len(ex_snippet) > 140 else ""}" — {ex_user}' if ex_snippet else ""
            return f"Across {n_high} 4–5★ reviews, users praise: {theme_list}.{quote}"
        # Sentiment ran but found no themes (too few reviews) — fall through

    # Fallback: single-review excerpt
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'reviews_%' "
        "AND name != 'reviews_daily'"
    ).fetchall()]
    best = None
    for tbl in tables:
        try:
            row = conn.execute(
                f"SELECT user_name, rating, text, COALESCE(thumbs_up, 0) AS th, posted_at "
                f"FROM {tbl} WHERE package_name=? AND rating >= 4 "
                f"AND text IS NOT NULL AND length(text) > 25 "
                f"ORDER BY th DESC, posted_at DESC LIMIT 1",
                (pkg,),
            ).fetchone()
        except Exception:
            continue
        if row and (best is None or (row[3] or 0) > (best[3] or 0)):
            best = row
    if not best:
        return "No reviews fetched yet — click Fetch reviews to see what users praise."
    user, rating, text, _th, _posted = best
    excerpt = re.sub(r"\s+", " ", text).strip()[:160]
    return f'"{excerpt}" — {user or "a recent user"} ({rating}★)'


def _q_user_pain(conn: sqlite3.Connection, pkg: str) -> str:
    """Theme-based aggregation when we have sentiment data; otherwise fall back
    to single 1-2★ review excerpt."""
    s = _load_sentiment(conn, pkg)
    if s:
        bucket = s.get("buckets", {}).get("low") or {}
        themes = bucket.get("themes") or []
        n_low = bucket.get("review_count", 0)
        low_pct = s.get("low_star_pct", 0)
        if themes:
            theme_list = _format_themes(themes, max_themes=3)
            ex = themes[0].get("exemplar") or {}
            ex_snippet = (ex.get("snippet") or "").strip()
            ex_user = ex.get("user") or "a frustrated user"
            quote = f' Top quote: "{ex_snippet[:140]}{"…" if len(ex_snippet) > 140 else ""}" — {ex_user}' if ex_snippet else ""
            return f"Across {n_low} 1–2★ reviews ({low_pct:.0f}% of fetched), users complain about: {theme_list}.{quote}"
        if n_low > 0:
            # We have low-star reviews but no clear themes (too few data points).
            return f"{n_low} 1–2★ reviews found but no recurring complaint themes yet — fetch more reviews."
        return "No 1–2★ reviews in our sample — users seem happy with this app."

    # Fallback: single excerpt
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'reviews_%' "
        "AND name != 'reviews_daily'"
    ).fetchall()]
    worst = None
    for tbl in tables:
        try:
            row = conn.execute(
                f"SELECT user_name, rating, text, COALESCE(thumbs_up, 0) AS th, posted_at "
                f"FROM {tbl} WHERE package_name=? AND rating <= 2 "
                f"AND text IS NOT NULL AND length(text) > 25 "
                f"ORDER BY th DESC, posted_at DESC LIMIT 1",
                (pkg,),
            ).fetchone()
        except Exception:
            continue
        if row and (worst is None or (row[3] or 0) > (worst[3] or 0)):
            worst = row
    if not worst:
        return "No 1–2★ reviews fetched yet — fetch reviews to see what users complain about."
    user, rating, text, _th, _posted = worst
    excerpt = re.sub(r"\s+", " ", text).strip()[:160]
    return f'"{excerpt}" — {user or "a frustrated user"} ({rating}★)'


def _q_monetization(app: dict) -> str:
    return _money_label(
        app.get("contains_ads"),
        app.get("offers_iap"),
        app.get("iap_price_range"),
        app.get("free"),
        app.get("price"),
    )


def _q_niche_density(conn: sqlite3.Connection, app: dict, pkg: str) -> tuple[str, int]:
    """Count apps in the same categories. Returns (label, count)."""
    cats = _category_ids(app.get("categories"))
    if not cats:
        return ("Not in any tracked niche.", 0)
    # Apps that share at least one category
    placeholders = " OR ".join("(',' || COALESCE(d.categories,'') || ',') LIKE ?" for _ in cats)
    params = [f"%,{c},%" for c in cats]
    try:
        n = conn.execute(
            f"SELECT COUNT(DISTINCT d.package_name) "
            f"FROM discovered_apps d JOIN apps_enriched e ON e.package_name=d.package_name "
            f"WHERE ({placeholders}) AND d.package_name != ? AND e.installs_per_month_v IS NULL",
            (*params, pkg),
        ).fetchone()
    except Exception:
        # apps_ranked view if installs_per_month_v doesn't exist
        n = conn.execute(
            f"SELECT COUNT(DISTINCT d.package_name) FROM discovered_apps d "
            f"WHERE ({placeholders}) AND d.package_name != ?",
            (*params, pkg),
        ).fetchone()
    count = int(n[0] if n else 0)
    if count == 0:
        return ("0 competitors tracked. Either niche is empty or undertracked.", 0)
    if count < 20:
        label = f"{count} competitors tracked — sparse niche, easier to break in."
    elif count < 100:
        label = f"{count} competitors tracked — moderately competitive."
    elif count < 500:
        label = f"{count} competitors tracked — saturated niche."
    else:
        label = f"{count} competitors tracked — very saturated; differentiation is hard."
    return (label, count)


def _q_niche_leader(conn: sqlite3.Connection, app: dict, pkg: str) -> str:
    cats = _category_ids(app.get("categories"))
    if not cats:
        return "n/a — app isn't in a tracked niche."
    # Use only the first/primary niche so a multi-category app doesn't pull in
    # the global leader of an unrelated category.
    primary = cats[0]
    try:
        row = conn.execute(
            "SELECT title, installs_per_month, package_name FROM apps_ranked "
            "WHERE (',' || COALESCE(categories,'') || ',') LIKE ? "
            "  AND package_name != ? AND installs_per_month IS NOT NULL "
            "ORDER BY installs_per_month DESC LIMIT 1",
            (f"%,{primary},%", pkg),
        ).fetchone()
    except Exception:
        return "Couldn't compute niche leader."
    if not row:
        return "No installs data for this niche yet."
    title, ipm, leader_pkg = row
    niche_label = primary.replace("_", " ").title()
    return f"{title or leader_pkg} ({niche_label}) — ~{_humanize_count(ipm)}/mo installs."


def _q_dev_seriousness(conn: sqlite3.Connection, app: dict, pkg: str) -> str:
    dev_id = app.get("developer_id")
    dev_name = app.get("developer") or "the developer"
    if not dev_id:
        return f"{dev_name} — developer ID not captured."
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(min_installs), 0) FROM apps_enriched "
            "WHERE developer_id = ? AND package_name != ?",
            (dev_id, pkg),
        ).fetchone()
    except Exception:
        return f"{dev_name} — couldn't compute portfolio."
    sibling_count = int(row[0] if row else 0)
    sibling_installs = int(row[1] if row else 0)
    if sibling_count == 0:
        return f"{dev_name} — single-app developer (no other apps in our DB)."
    if sibling_count < 3:
        verdict = "small portfolio"
    elif sibling_count < 10:
        verdict = "active studio"
    else:
        verdict = "large studio"
    return f"{dev_name} — {sibling_count} sibling apps ({_humanize_count(sibling_installs)} cumulative installs); {verdict}."


def _q_freshness(app: dict) -> str:
    days = _days_since(app.get("updated_at"))
    rc = (app.get("recent_changes") or "").strip()
    rc_excerpt = re.sub(r"\s+", " ", rc)[:120]

    if days is None:
        return "Update timeline unknown."
    if days < 7:
        verdict = "Actively maintained"
    elif days < 60:
        verdict = "Healthy update cadence"
    elif days < 180:
        verdict = "Slowing down"
    else:
        verdict = "Stale (no update in 6+ months)"

    label = f"{verdict} — last updated {days} day{'s' if days != 1 else ''} ago"
    if rc_excerpt:
        label += f". \"{rc_excerpt}{'…' if len(rc) > 120 else ''}\""
    return label


def _q_verdict(app: dict, niche_count: int, sentiment: dict | None = None) -> str:
    """Composite read: combine velocity + low_star + saturation + age + sentiment themes."""
    ipm = app.get("installs_per_month") or 0
    rating = app.get("score") or 0
    h1 = app.get("histogram_1") or 0
    h2 = app.get("histogram_2") or 0
    h_total = sum(app.get(k) or 0 for k in ("histogram_1","histogram_2","histogram_3","histogram_4","histogram_5"))
    low_star_pct = ((h1 + h2) / h_total) if h_total else 0
    days_old = _days_since(app.get("released")) or 999

    signals = []
    score = 0
    # Velocity
    if ipm > 100_000: score += 2; signals.append("strong install velocity")
    elif ipm > 10_000: score += 1; signals.append("moderate install velocity")
    else: signals.append("low install velocity")
    # Recency
    if days_old < 90: score += 2; signals.append("recently launched")
    elif days_old < 365: score += 1; signals.append("year-old")

    # Sentiment-aware dissatisfaction signal: prefer real review themes when available.
    # When we have sentiment, "unmet user needs" is only a buy-signal if the pain themes
    # are concrete product complaints (logging in, ads, bugs) rather than generic
    # frustration. We approximate this by checking whether the low-bucket has ≥2 themes.
    sent_low_themes = []
    sent_high_themes = []
    if sentiment:
        sent_low_themes  = (sentiment.get("buckets", {}).get("low")  or {}).get("themes") or []
        sent_high_themes = (sentiment.get("buckets", {}).get("high") or {}).get("themes") or []

    if sent_low_themes and ipm > 10_000:
        # Real evidence of unmet needs — concrete pain themes from real users.
        score += 2
        top = ", ".join(t["theme"] for t in sent_low_themes[:2])
        signals.append(f"users complain about [{top}] (real opportunity signal)")
    elif low_star_pct > 0.25 and ipm > 10_000:
        score += 2; signals.append(f"high 1-2★ rate ({int(low_star_pct*100)}%) = possible unmet user needs")
    elif low_star_pct > 0.4:
        signals.append(f"high 1-2★ rate ({int(low_star_pct*100)}%) but low traction — may just be broken")

    # Bonus when high-bucket themes show clear user delight points (something to copy)
    if sent_high_themes:
        top = ", ".join(t["theme"] for t in sent_high_themes[:2])
        signals.append(f"users praise [{top}] (positioning hint)")

    # Crowding
    if niche_count == 0:
        signals.append("undertracked niche (verify before betting)")
    elif niche_count < 20:
        score += 1; signals.append("sparse niche")
    elif niche_count > 200:
        score -= 1; signals.append("saturated niche")

    # Rating quality
    if rating > 0 and rating < 3:
        score -= 1; signals.append(f"chronically low rating ({rating:.1f}) — likely just broken")

    if score >= 4:
        verdict = "🔥 Strong opportunity — worth deeper research."
    elif score >= 2:
        verdict = "👀 Worth a look — has some signal."
    elif score >= 0:
        verdict = "🟡 Mediocre signal — probably not worth the focus."
    else:
        verdict = "🚫 Skip — too saturated, too broken, or too quiet."

    return verdict + " (" + "; ".join(signals[:4]) + ")"


# ---------- Main builder ----------

def compute_app_qa(conn: sqlite3.Connection, package_name: str) -> dict | None:
    """Build the full Q&A dict for one package. Returns None if app not enriched."""
    row = conn.execute(
        """
        SELECT e.package_name, e.title, e.developer, e.developer_id, e.genre, e.content_rating,
               e.released, e.updated_at, e.version, e.recent_changes,
               e.installs, e.min_installs, e.score, e.ratings, e.reviews,
               e.histogram_1, e.histogram_2, e.histogram_3, e.histogram_4, e.histogram_5,
               e.contains_ads, e.offers_iap, e.iap_price_range, e.free, e.price,
               d.summary, d.categories,
               (SELECT installs_per_month FROM apps_ranked WHERE package_name = e.package_name) AS installs_per_month
        FROM apps_enriched e
        LEFT JOIN discovered_apps d ON d.package_name = e.package_name
        WHERE e.package_name = ?
        """,
        (package_name,),
    ).fetchone()
    if not row:
        return None
    cols = [
        "package_name","title","developer","developer_id","genre","content_rating",
        "released","updated_at","version","recent_changes",
        "installs","min_installs","score","ratings","reviews",
        "histogram_1","histogram_2","histogram_3","histogram_4","histogram_5",
        "contains_ads","offers_iap","iap_price_range","free","price",
        "summary","categories","installs_per_month",
    ]
    app = dict(zip(cols, row))

    niche_label, niche_count = _q_niche_density(conn, app, package_name)
    sentiment = _load_sentiment(conn, package_name)

    qa = {
        "what_it_does":   {"label": "What does this app do?",        "answer": _q_what_it_does(app["summary"], app["title"])},
        "target_user":    {"label": "Who's it for?",                 "answer": _q_target_user(app["genre"], app["content_rating"], app["summary"])},
        "why_loved":      {"label": "Why do people love it?",        "answer": _q_why_loved(conn, package_name)},
        "user_pain":      {"label": "What do users hate about it?",  "answer": _q_user_pain(conn, package_name)},
        "monetization":   {"label": "How does it make money?",       "answer": _q_monetization(app)},
        "niche_density":  {"label": "How crowded is this niche?",    "answer": niche_label},
        "niche_leader":   {"label": "Who's the leader to beat?",     "answer": _q_niche_leader(conn, app, package_name)},
        "dev_seriousness":{"label": "Is the developer serious?",     "answer": _q_dev_seriousness(conn, app, package_name)},
        "freshness":      {"label": "How fresh is it?",              "answer": _q_freshness(app)},
        "verdict":        {"label": "Verdict — opportunity or skip?","answer": _q_verdict(app, niche_count, sentiment)},
    }
    return qa


def build_qa_for(conn: sqlite3.Connection, package_name: str) -> bool:
    qa = compute_app_qa(conn, package_name)
    if qa is None:
        return False
    conn.execute(
        "UPDATE apps_enriched SET qa_json = ?, qa_updated_at = ? WHERE package_name = ?",
        (json.dumps(qa, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), package_name),
    )
    conn.commit()
    return True


def build_qa_all(conn: sqlite3.Connection, only_stale_days: int | None = None,
                  limit: int | None = None) -> tuple[int, int]:
    """Build Q&A for every enriched app. Returns (built, skipped)."""
    if only_stale_days is not None:
        cutoff = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=only_stale_days)).isoformat()
        rows = conn.execute(
            "SELECT package_name FROM apps_enriched "
            "WHERE qa_json IS NULL OR qa_updated_at IS NULL OR qa_updated_at < ? "
            "ORDER BY package_name",
            (cutoff,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT package_name FROM apps_enriched ORDER BY package_name").fetchall()
    pkgs = [r[0] for r in rows]
    if limit:
        pkgs = pkgs[:limit]

    built, skipped = 0, 0
    for i, pkg in enumerate(pkgs, 1):
        try:
            ok = build_qa_for(conn, pkg)
            if ok: built += 1
            else: skipped += 1
        except Exception as e:
            skipped += 1
            print(f"[qa] {pkg} err: {e}")
        if i % 250 == 0:
            print(f"[qa] {i}/{len(pkgs)} ({built} built, {skipped} skipped)…", flush=True)
    return built, skipped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="reviews.db")
    parser.add_argument("--all", action="store_true", help="Build for every enriched app")
    parser.add_argument("--pkg", help="Build for one package")
    parser.add_argument("--stale", type=int, help="Build only those older than N days")
    parser.add_argument("--limit", type=int, help="Cap the number of apps processed")
    parser.add_argument("--show", help="Print the Q&A for one package and exit")
    args = parser.parse_args()
    global DB_PATH
    DB_PATH = Path(args.db)

    conn = _conn()
    try:
        if args.show:
            qa = compute_app_qa(conn, args.show)
            print(json.dumps(qa, indent=2, ensure_ascii=False))
            return 0
        if args.pkg:
            ok = build_qa_for(conn, args.pkg)
            print(f"{'built' if ok else 'app not found'}: {args.pkg}")
            return 0 if ok else 1
        if args.all or args.stale is not None:
            built, skipped = build_qa_all(conn, only_stale_days=args.stale, limit=args.limit)
            print(f"built={built} skipped={skipped}")
            return 0
        parser.print_help()
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
