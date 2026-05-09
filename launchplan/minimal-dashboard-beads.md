# Minimal Dashboard: "Viral Apps in the Last Month" — Chain of Beads

## Context

After sign-in, the dashboard is the *only* surface. No tab bar. No side widgets. One job: show **viral apps in the last 30 days, in the user's saved categories**, ranked by (release recency, viral score, installs momentum, 1–2★ review concentration).

This plan is a chain of small, sequential, individually-verifiable beads. Each bead is ≤1 day, ships value or unlocks the next one, and has a clear "done when" check. The chain delivers a minimalist dashboard powered first by cold-start data (works on day 1) and then by a time-series scraping foundation (richer by week 5).

The data backbone (`app_history`, `reviews_daily`, `monthly_hot_apps`, `poller_runs`) and the composite viral score formula are documented inside the relevant beads — read them in order.

---

## The chain

### Phase A — Strip the dashboard down to one purpose (UX)

**🪨 Bead 1 — Hide the tab bar**
- **Goal:** Remove `<div class="tabs">` from primary navigation. `setView()` stays callable internally for deep-link `/app#reviews` style navigation but the tab chrome is gone.
- **Files:** `index.html` — delete the `<div class="tabs">…</div>` block; CSS for `.tabs` becomes dead but harmless.
- **Done when:** sign in → no tabs visible at top of dashboard. Direct URL `/app#reviews` still renders the reviews view (verified by paste-into-browser).

**🪨 Bead 2 — Gut `renderHomeView()`**
- **Goal:** Strip away every widget that isn't "viral apps per saved category". Remove: welcome banner, this-week quota card, saved-categories chip cluster, quick-links grid, weekly-reports cards, fetched-reviews list, watchlist, saved searches, niche-of-week, activity feed, exports section, plan card, tip-of-day. Replace with a single `<div class="dash-wrap">` containing one `<section>` per saved category and a placeholder of "Loading…".
- **Files:** `index.html` — `renderHomeView()` body.
- **Done when:** `/dashboard` renders just the page title "This month in your categories" + one empty `<section>` placeholder per saved category + a footer "Edit categories →" link.

**🪨 Bead 3 — Chip CSS + per-app card markup**
- **Goal:** Add CSS for the four chip styles (release-recency, installs+delta, low-star %, viral score) and the `.app-card` layout (icon, title, dev, chip row, "Read reviews →" link).
- **Files:** `index.html` — CSS block + a `cardHtml(app)` helper inside the dashboard render.
- **Done when:** `cardHtml({title:"Test", icon:"...", released:"2026-04-01", installs_per_month:268000, delta_pct:24, low_star_pct:0.22, viral_score:8.4})` produces the four-chip card seen in the layout sketch.

**🪨 Bead 4 — Cold-start render path**
- **Goal:** Use the *existing* `/api/me/dashboard` payload (which already returns `top_viral` per saved category) to populate the page. For each saved category, query `apps_ranked` filtered to that category, take top 5 by `installs_per_month`, render cards. Compute three of the four chip values from snapshot data: `released`, `installs_per_month`, `(histogram_1+histogram_2)/sum(histogram_*)`. Compute `viral_score` client-side using existing `computeViralScore()` (already in JS).
- **Files:** `index.html` — `renderHomeView()` data fetch + render.
- **Done when:** dashboard renders 5 cards per saved category with all four chips populated. No 404s, no spinners stuck.

**🪨 Bead 5 — Card → reviews deep link**
- **Goal:** Clicking the "Read reviews →" link on a card calls `setView('reviews')` and pre-filters reviews to that package. Browser back returns to dashboard.
- **Files:** `index.html` — wire the click handler in `cardHtml`'s onclick path; reuse the existing `state.pkg = pkg; setView('reviews')` flow.
- **Done when:** click any card → reviews view loads for that package; browser back button returns to dashboard.

### Phase B — Server-side cold-start scoring

**🪨 Bead 6 — `monthly_hot_apps` table**
- **Goal:** Add the cached top-N per category table.
- **Files:** `auth.py` — append to DDL:
  ```sql
  CREATE TABLE IF NOT EXISTS monthly_hot_apps (
    category_id   TEXT NOT NULL,
    computed_at   TEXT NOT NULL,
    rank          INTEGER NOT NULL,
    package_name  TEXT NOT NULL,
    payload       TEXT NOT NULL,
    PRIMARY KEY (category_id, computed_at, rank)
  );
  CREATE INDEX IF NOT EXISTS idx_mha_latest ON monthly_hot_apps(category_id, computed_at DESC);
  ```
- **Done when:** `python3 -c "import auth; auth.init_schema()"` adds the table; `.schema monthly_hot_apps` matches.

**🪨 Bead 7 — `compute_monthly_hot_apps()` (cold-start version)**
- **Goal:** New function in `server.py`. For each category present in any user's `picked_categories`, score every app via the cold-start composite formula and write top-10 to `monthly_hot_apps`. Cold-start formula:
  ```
  viral_score =
    0.30 × normalize(installs_per_month, log10)
    0.25 × normalize(weekly_reports.payload.delta_pct, [-50, 200])
    0.25 × recency_score(released)
    0.20 × snapshot_low_star_pct
  ```
- **Files:** `server.py` — new helper + a runnable `if __name__ == "__main__"` entry for ad-hoc invocation.
- **Done when:** `python3 -c "import server; server.compute_monthly_hot_apps(force=True)"` writes ~10 rows × N categories to `monthly_hot_apps`. SELECT confirms scores monotonically decrease per category.

**🪨 Bead 8 — `/api/me/dashboard` reads `monthly_hot_apps`**
- **Goal:** Switch the endpoint to query `monthly_hot_apps` for the latest `computed_at` per saved category, fall back to live `apps_ranked` aggregation if `monthly_hot_apps` is empty for that category.
- **Files:** `server.py` — `/api/me/dashboard` handler.
- **Done when:** signed-in user hitting `/api/me/dashboard` gets the new shape `{categories: [{id, name, apps: [...]}]}`. Each category lists 5 apps with the four chip fields.

### Phase C — Time-series scraping foundation

**🪨 Bead 9 — `app_history`, `reviews_daily`, `poller_runs` tables**
- **Goal:** Add the three time-series + observability tables.
- **Files:** `auth.py` — append DDL:
  ```sql
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

  CREATE TABLE IF NOT EXISTS poller_runs (
    job_name      TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT,
    rows_written  INTEGER DEFAULT 0,
    errors        TEXT,
    PRIMARY KEY (job_name, started_at)
  );
  ```
- **Done when:** `auth.init_schema()` adds all three; manual INSERT works.

**🪨 Bead 10 — `poller.py` scaffold + `_should_run()` helper**
- **Goal:** New module. Provides `_should_run(job_name, schedule)` that consults `poller_runs` to decide whether to start, plus a `_record_run(job_name, status, rows, err)` writer. No actual jobs yet — just the framework.
- **Files:** `poller.py` (new).
- **Done when:** unit-style test: `_should_run('test', 'daily 03:00 UTC')` returns True the first call, False on a same-day second call.

**🪨 Bead 11 — `poll_apps_daily` job**
- **Goal:** For each package in `apps_enriched`, call `gp_app()`, diff against latest `app_history` row, insert a new row when `min_installs / score / ratings_count / reviews_count / version / histogram_*` changed. Concurrent (12 workers — same pattern as `enrich_apps.py:fetch_one`).
- **Files:** `poller.py`.
- **Done when:** `python3 -c "import poller; poller.poll_apps_daily(force=True)"` writes ≥1 `app_history` row for any package whose metadata changed since last enrichment. `poller_runs` shows `status='ok'`.

**🪨 Bead 12 — `poll_reviews_weekly` job**
- **Goal:** Identify top-300 most-installed apps across all users' saved categories. For each, call `gp_reviews(sort=NEWEST)` and paginate until we hit a known `review_id`. Upsert into `reviews_*`.
- **Files:** `poller.py`. Reuses `fetch_reviews.fetch_reviews` and `fetch_reviews.upsert_reviews`.
- **Done when:** running the job adds new `reviews_*` rows for at least one app whose latest review is now newer than the previous `posted_at`.

**🪨 Bead 13 — `aggregate_reviews_daily` job**
- **Goal:** Bucket `reviews_*` into `reviews_daily` (count + avg_rating + per-rating-bin counts per package per day). Incremental: only re-aggregate rows where `date IN (yesterday, today)`.
- **Files:** `poller.py`.
- **Done when:** `SELECT * FROM reviews_daily WHERE date >= date('now','-2 days')` returns one row per (package, country, day) with non-zero counts.

**🪨 Bead 14 — Wire all four jobs into `_email_scheduler`**
- **Goal:** Add four `_should_run` checks to the existing `_email_scheduler` 15-min loop in `server.py:1326`. Schedule:
  - `poll_apps_daily` daily 03:00 UTC
  - `poll_reviews_weekly` weekly Tue 02:00 UTC
  - `aggregate_reviews_daily` daily 04:00 UTC
  - `compute_monthly_hot_apps` weekly Mon 08:00 UTC
- **Files:** `server.py` — `_email_scheduler()` body.
- **Done when:** server runs through 24h, `poller_runs` has one ok row per scheduled job. (Manual fast-forward: temporarily change schedules to `every 5 min` for testing, then restore.)

### Phase D — Composite score v2 (real 30-day trends)

**🪨 Bead 15 — 30-day momentum from `app_history`**
- **Goal:** Add a helper `compute_momentum_30d(package_name)` that returns `(min_installs_today − min_installs_30d_ago) / min_installs_30d_ago` clipped to [-50, 200]. Returns None when fewer than 25 days of history (cold-start threshold).
- **Files:** `server.py`.
- **Done when:** for a package with 30+ `app_history` rows, the helper returns a real number; for a package with <25 rows, returns None and the cold-start fallback kicks in inside `compute_monthly_hot_apps`.

**🪨 Bead 16 — 30-day low-star % from `reviews_daily`**
- **Goal:** Add helper `compute_low_star_30d(package_name)` → `(rating_1+rating_2)/total` over the last 30 days from `reviews_daily`. Returns None when total <50 reviews in window.
- **Files:** `server.py`.
- **Done when:** SELECT against the helper output matches a hand-computed query for one well-known app.

**🪨 Bead 17 — Score v2 wiring**
- **Goal:** `compute_monthly_hot_apps` now uses the v15/v16 helpers when available; falls back to cold-start values otherwise. No UI change — just better numbers.
- **Files:** `server.py`.
- **Done when:** rerun `compute_monthly_hot_apps(force=True)` after 30 days of polling. Inspect a payload — `momentum_30d` and `low_star_30d` are now real numbers, not snapshot fallbacks. Top-5 ranking shifts noticeably for fast-moving apps.

### Phase E — Polish

**🪨 Bead 18 — Empty states**
- **Goal:** Two empty states. (a) User has no `picked_categories` → "Pick categories to see this month's viral apps →" CTA → opens existing Settings modal. (b) Categories saved but `monthly_hot_apps` empty for all of them → "Crunching this week's data… check back in a few hours." with a friendly skeleton.
- **Files:** `index.html`.
- **Done when:** clearing `users.picked_categories` for a test account renders state (a); deleting `monthly_hot_apps` rows renders state (b).

**🪨 Bead 19 — "Updated X · Refreshes weekly" timestamp**
- **Goal:** Below the page title, render the most recent `MAX(computed_at)` from `monthly_hot_apps` in a friendly format (e.g. "Updated Mon, May 12 · Refreshes weekly").
- **Files:** `server.py` (add `latest_computed_at` to dashboard payload), `index.html` (render).
- **Done when:** dashboard header shows the line; updates after `compute_monthly_hot_apps` reruns.

**🪨 Bead 20 — Mobile**
- **Goal:** Cards stack single-column at <720px. Chips wrap. All chip text legible at 360px.
- **Files:** `index.html` — CSS media queries.
- **Done when:** Chrome devtools at iPhone 12 viewport: layout reads cleanly, no horizontal scroll.

---

## Dependencies between beads

- 1 → 2 → 3 → 4 → 5 ships **the visible product** powered by snapshot data alone
- 6 → 7 → 8 introduce caching + server-side scoring (no UI regression — same data shape, just precomputed)
- 9 → 10 → 11 → 12 → 13 → 14 build the time-series engine (no UI regression)
- 15 → 16 → 17 swap the cold-start branches inside the existing scoring helper for real 30-day trends
- 18 → 19 → 20 are independent polish; can ship interleaved with B/C if needed

**Critical path to "user sees the product":** beads 1–5 (≈3 days). The dashboard is live with cold-start data after bead 5. Beads 6–17 silently improve the numbers without changing the look. Beads 18–20 polish.

---

## Verification per phase

- **End of Phase A (after bead 5):** Sign in → minimalist dashboard with 5 cards × N categories, four chips per card, "Read reviews →" deep-links. No tabs visible.
- **End of Phase B (after bead 8):** `/api/me/dashboard` < 100ms; reads from `monthly_hot_apps`; rerunning `compute_monthly_hot_apps` updates the dashboard on next refresh.
- **End of Phase C (after bead 14):** 24h after deploy, `poller_runs` shows one `ok` row per scheduled job. `app_history` and `reviews_daily` are accumulating rows daily.
- **End of Phase D (after bead 17):** 30 days after deploy, payload `momentum_30d` and `low_star_30d` are real (not fallback). Composite ranking visibly differs from cold-start.
- **End of Phase E:** Mobile review on a real device; empty-state copy reviewed; timestamp visible.

---

## Out of scope (deferred)

- **Welcome splash modal** — dashboard is the experience
- **Other tabs in primary nav** — hidden but route-accessible for deep links
- **Per-country breakdown** — single-country (IN default) for v1
- **Weekly archive page** (`/digest/{year}-W{week}`) — viable but not for dashboard
- **AI-summarized digest** — needs LLM, deferred
- **Email-as-dashboard** — same data via Monday email, viable v2
- **Real DAU/MAU/retention/revenue** — enterprise pricing only
- **Per-app deep page** (`/app/{pkg}`) — covered by `launchplan/rich-data.md`; cards link to reviews for now
- **Personalization beyond saved categories** — v2 with telemetry
