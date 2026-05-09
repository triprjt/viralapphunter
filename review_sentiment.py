"""Pure-stdlib review sentiment / theme extractor.

For each app whose reviews have been fetched into reviews_*, splits the reviews
into low (1–2★) and high (4–5★) buckets and extracts the most distinctive
bigram/trigram themes per bucket using contrastive term frequency: words that
appear often in this bucket but rarely in the other bucket.

Output is persisted to apps_enriched.review_sentiment_json so app_qa.py can
consume it when building the "why people love it / hate it / verdict" answers.

Run as a CLI:
    python3 review_sentiment.py --pkg com.foo.bar
    python3 review_sentiment.py --all-with-reviews   # every package that has reviews
    python3 review_sentiment.py --show com.foo.bar   # dry-run, print to stdout

Zero new deps — uses only stdlib (re, collections, json, sqlite3).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("reviews.db")

# Stopwords — generic English + app-domain words too noisy to be themes.
_STOPWORDS = {
    # Articles, pronouns, prepositions
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "had",
    "her", "was", "one", "our", "out", "day", "get", "has", "him", "his", "how",
    "its", "may", "new", "now", "old", "see", "two", "who", "boy", "did", "down",
    "from", "have", "into", "just", "like", "make", "many", "more", "most",
    "other", "over", "said", "some", "such", "take", "than", "that", "their",
    "them", "then", "there", "these", "they", "this", "those", "thus", "very",
    "want", "well", "what", "when", "with", "your", "would", "could", "should",
    "will", "been", "being", "doing", "having", "going",
    # App-domain noise
    "app", "apps", "android", "google", "play", "store", "phone", "device",
    "user", "users", "version", "update", "use", "using", "used", "useful",
    "great", "good", "best", "love", "nice", "thanks", "please", "really",
    "much", "even", "ever", "still", "able", "back", "also", "way", "lot",
    "thing", "things", "everything", "something", "anything", "nothing",
    "feature", "features",
    "experience", "interface",  # too generic to be a theme
    # Filler verbs
    "feel", "feels", "felt", "say", "says", "said", "tell", "told", "ask",
    "asked", "give", "gave", "given", "show", "shown", "see", "seen",
    "look", "looks", "looking", "find", "found", "finding", "got",
    "make", "makes", "made", "making", "want", "wants", "wanted",
    "need", "needs", "needed", "try", "tries", "tried", "trying",
    "open", "opens", "opened", "opening", "close", "closed",
    "work", "works", "worked", "working",
    # Common typos / contractions
    "dont", "doesnt", "isnt", "arent", "wasnt", "werent", "havent", "hasnt",
    "wont", "wouldnt", "couldnt", "shouldnt", "cant", "ive", "ill", "youre",
    "theyre", "im",
    # Numbers / time
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "first", "second", "third", "last", "next",
    # Generic affect words
    "amazing", "awesome", "excellent", "perfect", "wonderful", "fantastic",
    "horrible", "terrible", "awful", "worst", "bad", "good",
}

# Min word length and chars allowed (letters only)
_TOKEN_RE = re.compile(r"[a-zA-Z']{3,}")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


# ---------- Tokenization + n-grams ----------

def tokenize(text: str | None) -> list[str]:
    if not text:
        return []
    raw = _TOKEN_RE.findall(text.lower())
    return [w.replace("'", "") for w in raw if w not in _STOPWORDS and len(w) >= 3]


def bigrams(tokens: list[str]) -> list[str]:
    return [f"{tokens[i]} {tokens[i+1]}" for i in range(len(tokens) - 1)
            if tokens[i] not in _STOPWORDS and tokens[i+1] not in _STOPWORDS]


def trigrams(tokens: list[str]) -> list[str]:
    out = []
    for i in range(len(tokens) - 2):
        a, b, c = tokens[i], tokens[i+1], tokens[i+2]
        if a in _STOPWORDS or c in _STOPWORDS:
            continue
        out.append(f"{a} {b} {c}")
    return out


# ---------- Contrastive theme extraction ----------

def _bucket_review_rows(conn: sqlite3.Connection, package_name: str) -> list[tuple]:
    """Return [(rating, text, thumbs_up, user_name, posted_at), ...] across all reviews_* tables."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'reviews_%' "
        "AND name != 'reviews_daily'"
    ).fetchall()]
    rows = []
    for tbl in tables:
        try:
            r = conn.execute(
                f"SELECT rating, text, COALESCE(thumbs_up, 0), user_name, posted_at "
                f"FROM {tbl} WHERE package_name = ? AND text IS NOT NULL AND length(text) > 15",
                (package_name,),
            ).fetchall()
            rows.extend(r)
        except Exception:
            continue
    return rows


def _theme_scores(this_bucket: list[str], other_bucket: list[str], top_n: int = 6) -> list[tuple[str, float, int]]:
    """Score each n-gram by contrastive term frequency.

    score(term) = freq_in_this_bucket / total_this − freq_in_other / total_other
    Only terms that appear at least twice in this bucket are eligible.
    Returns [(term, score, raw_count_in_this_bucket), ...] descending.
    """
    if not this_bucket:
        return []
    this_counts = Counter(this_bucket)
    other_counts = Counter(other_bucket)
    n_this = sum(this_counts.values()) or 1
    n_other = sum(other_counts.values()) or 1
    scored = []
    for term, c_this in this_counts.items():
        if c_this < 2:
            continue
        c_other = other_counts.get(term, 0)
        score = (c_this / n_this) - (c_other / n_other)
        if score > 0:
            scored.append((term, score, c_this))
    scored.sort(key=lambda x: (-x[1], -x[2]))
    return scored[:top_n]


def _exemplar(reviews_in_bucket: list[tuple], term: str) -> dict | None:
    """Pick the most-thumbed-up review in this bucket whose text contains `term` (case-insensitive).
    Falls back to the most-thumbed-up review overall if no match found."""
    candidates = [r for r in reviews_in_bucket if r[1] and term.lower() in r[1].lower()]
    if not candidates:
        return None
    candidates.sort(key=lambda r: (-(r[2] or 0), r[4] or ""), reverse=False)
    candidates.sort(key=lambda r: -(r[2] or 0))
    rating, text, thumbs, user, posted = candidates[0]
    snippet = re.sub(r"\s+", " ", text).strip()
    return {
        "user": user or "anonymous",
        "rating": int(rating) if rating else None,
        "thumbs_up": int(thumbs or 0),
        "snippet": snippet[:200] + ("…" if len(snippet) > 200 else ""),
    }


def analyze(conn: sqlite3.Connection, package_name: str) -> dict | None:
    rows = _bucket_review_rows(conn, package_name)
    if not rows:
        return None

    low_rows  = [r for r in rows if r[0] in (1, 2)]
    high_rows = [r for r in rows if r[0] in (4, 5)]

    # Build n-gram corpora — combine bigrams + trigrams
    def corpus(bucket_rows):
        out = []
        for _, text, *_ in bucket_rows:
            t = tokenize(text)
            out.extend(bigrams(t))
            out.extend(trigrams(t))
        return out

    low_corpus  = corpus(low_rows)
    high_corpus = corpus(high_rows)

    low_themes_raw  = _theme_scores(low_corpus,  high_corpus, top_n=6)
    high_themes_raw = _theme_scores(high_corpus, low_corpus,  top_n=6)

    low_themes = []
    for term, score, count in low_themes_raw:
        ex = _exemplar(low_rows, term)
        low_themes.append({
            "theme": term,
            "score": round(score, 4),
            "count": count,
            "freq_pct": round((count / max(1, len(low_rows))) * 100, 1),
            "exemplar": ex,
        })

    high_themes = []
    for term, score, count in high_themes_raw:
        ex = _exemplar(high_rows, term)
        high_themes.append({
            "theme": term,
            "score": round(score, 4),
            "count": count,
            "freq_pct": round((count / max(1, len(high_rows))) * 100, 1),
            "exemplar": ex,
        })

    avg = lambda rs: round(sum(r[0] for r in rs) / len(rs), 2) if rs else None

    return {
        "reviews_analyzed": len(rows),
        "buckets": {
            "low":  {"review_count": len(low_rows),  "avg_rating": avg(low_rows),  "themes": low_themes},
            "high": {"review_count": len(high_rows), "avg_rating": avg(high_rows), "themes": high_themes},
        },
        "low_star_pct": round(len(low_rows) / len(rows) * 100, 1) if rows else 0,
    }


# ---------- Persist ----------

def build_for(conn: sqlite3.Connection, package_name: str) -> bool:
    s = analyze(conn, package_name)
    if s is None:
        return False
    conn.execute(
        "UPDATE apps_enriched SET review_sentiment_json = ?, review_sentiment_updated_at = ? "
        "WHERE package_name = ?",
        (json.dumps(s, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), package_name),
    )
    conn.commit()
    return True


def packages_with_reviews(conn: sqlite3.Connection) -> list[str]:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'reviews_%' "
        "AND name != 'reviews_daily'"
    ).fetchall()]
    pkgs = set()
    for tbl in tables:
        try:
            for r in conn.execute(f"SELECT DISTINCT package_name FROM {tbl} WHERE package_name IS NOT NULL").fetchall():
                pkgs.add(r[0])
        except Exception:
            continue
    return sorted(pkgs)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="reviews.db")
    parser.add_argument("--pkg")
    parser.add_argument("--all-with-reviews", action="store_true")
    parser.add_argument("--show", help="dry-run; print to stdout")
    args = parser.parse_args()
    global DB_PATH
    DB_PATH = Path(args.db)

    conn = _conn()
    try:
        if args.show:
            s = analyze(conn, args.show)
            print(json.dumps(s, indent=2, ensure_ascii=False))
            return 0
        if args.pkg:
            ok = build_for(conn, args.pkg)
            print(f"{'built' if ok else 'no reviews for'}: {args.pkg}")
            return 0 if ok else 1
        if args.all_with_reviews:
            pkgs = packages_with_reviews(conn)
            built = 0
            for p in pkgs:
                if build_for(conn, p):
                    built += 1
            print(f"built sentiment for {built}/{len(pkgs)} packages with reviews")
            return 0
        parser.print_help()
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
