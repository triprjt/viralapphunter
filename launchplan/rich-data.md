# Rich Competitive-Intelligence Data — Phase 1 + Phase 2

## Context

ViralFinder today captures point-in-time snapshots: one row per `apps_enriched`, on-demand `reviews_*`, and a single `weekly_reports` snapshot per category. The user wants the depth that DianDian/Sensor Tower expose — hourly ranking, ratings/reviews trends, ASO keyword positions, downloads/revenue, asset history, developer-portfolio tracking. Most of those signals are buildable from public sources + scheduled polling; only DAU/MAU/revenue-precision are enterprise-priced and out of scope.

The deliverable is a **time-series data layer + per-app deep page** that lets users see how an app's velocity, ranking, keyword footprint, and assets have evolved — the core surface that justifies the Solo tier and differentiates from Sensor Tower's enterprise pricing.

Research summary:
- ~85% of competitor surface is DIY at <$50/mo infra
- 15% (DAU/MAU/revenue precision) skipped — substituted by Fermi estimates + proxy signals
- Top 10 countries chosen (US, IN, BR, ID, MX, JP, KR, DE, GB, RU) — covers ~70% of Play traffic

## Scope: Phase 1 + Phase 2 (DIY surface)

| # | Signal | Phase | Source | Storage strategy |
|---|---|---|---|---|
| 1 | Hourly → daily chart rank (per category × country) | P1 | `gp_search` `list(collection=TOP_FREE/TOP_GROSSING/TRENDING)` | `ranking_history` table, downsample daily after 7d |
| 2 | Daily reviews/ratings trend | P1 | derived from existing `reviews_*` (`posted_at`) | `reviews_daily` MV-style view, refreshed nightly |
| 3 | Editor's Choice + chart presence flag | P1 | `gp_app().editorsChoice` + chart membership | column on `apps_enriched`, daily refresh |
| 4 | Update frequency / version history | P1 | `gp_app().version`, `updated_at`, `recentChanges` | `app_history` table (one row per change) |
| 5 | Asset change detection (icon/screenshots) | P1 | hash `icon_url` + `screenshots` JSON daily | `asset_history` table |
| 6 | Organic keyword rank tracking | P2 | reverse-search via `gp_search(keyword)` daily | `keyword_rank_history` table |
| 7 | Developer portfolio tracking | P2 | already have `discover_developer`; record over time | `developer_history` table |
| 8 | Fermi download estimate | P2 | install band × review velocity × age formula | computed column on `apps_ranked` view |
| 9 | Review sentiment (local model) | P2 | DistilBERT pinned, batched on review insert | `sentiment` column on `reviews_*` |

**Skipped** (out of scope at Solo tier): real DAU/MAU, real revenue, keyword search-volume data, paid-keyword scrape. Replaced by proxy signals in plan.

## New tables

All in `reviews.db` (existing SQLite). At 10 countries × 33 categories × top 50 apps × 24 snapshots/day = ~400K ranking rows/day, so we **store hourly only for last 7 days**, then downsample to daily. Estimated ~6–8M rows/year for ranking + ~1M for reviews_daily — fits SQLite WAL fine; Postgres trigger if we cross 50M.

```sql
-- Hourly ranking snapshots; downsample to daily after 7 days via cron
CREATE TABLE IF NOT EXISTS ranking_history (
  category_id  TEXT NOT NULL,
  country      TEXT NOT NULL,
  collection   TEXT NOT NULL,         -- 'TOP_FREE'|'TOP_GROSSING'|'TRENDING'
  package_name TEXT NOT NULL,
  position     INTEGER NOT NULL,
  captured_at  TEXT NOT NULL,
  PRIMARY KEY (category_id, country, collection, package_name, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_rank_pkg ON ranking_history(package_name, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_rank_cat ON ranking_history(category_id, country, captured_at DESC);

-- Daily aggregates of reviews per app (computed nightly)
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

-- Per-app metadata change log; one row each time anything changes
CREATE TABLE IF NOT EXISTS app_history (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  package_name  TEXT NOT NULL,
  captured_at   TEXT NOT NULL,
  version       TEXT,
  updated_at    TEXT,
  recent_changes TEXT,
  min_installs  INTEGER,
  score         REAL,
  ratings_count INTEGER,
  reviews_count INTEGER,
  editors_choice INTEGER,
  in_top_charts TEXT                  -- CSV of "country:collection:position"
);
CREATE INDEX IF NOT EXISTS idx_apphist ON app_history(package_name, captured_at DESC);

-- Asset change log (icon/screenshot hashes)
CREATE TABLE IF NOT EXISTS asset_history (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  package_name  TEXT NOT NULL,
  captured_at   TEXT NOT NULL,
  icon_hash     TEXT,
  screenshots_hash TEXT,                -- hash of sorted-tuple of screenshot URLs
  icon_url      TEXT,
  screenshots   TEXT                    -- JSON array
);
CREATE INDEX IF NOT EXISTS idx_assethist ON asset_history(package_name, captured_at DESC);

-- Keyword rank history; one row per (keyword, country, app, day)
CREATE TABLE IF NOT EXISTS keyword_rank_history (
  keyword       TEXT NOT NULL,
  country       TEXT NOT NULL,
  package_name  TEXT NOT NULL,
  position      INTEGER NOT NULL,
  captured_at   TEXT NOT NULL,
  PRIMARY KEY (keyword, country, package_name, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_krh_pkg ON keyword_rank_history(package_name, captured_at DESC);

-- Developer portfolio over time
CREATE TABLE IF NOT EXISTS developer_history (
  developer_id  TEXT NOT NULL,
  captured_at   TEXT NOT NULL,
  app_count     INTEGER NOT NULL,
  packages      TEXT NOT NULL,         -- CSV of all package_names
  PRIMARY KEY (developer_id, captured_at)
);
```

Add `sentiment REAL` column to each `reviews_*` table via defensive ALTER.

## Polling architecture

A new `poller.py` module with four independently scheduled jobs (drives off existing `_email_scheduler` thread loop in server.py:1326):

1. **Hourly chart poller** (`poll_charts`) — runs every hour
   - For each (country in TOP_10, category in categories.json, collection in [TOP_FREE, TOP_GROSSING, TRENDING]): call `gp_search.list()`, take top 50, write to `ranking_history`. Throttled with random 1–3s sleep between calls; backoff on 429.
   - Cost: 10 × 33 × 3 = 990 calls/hour. Single IP, well within rate limits if jittered.
   - Writes ~12.5K rows/hour to `ranking_history`.

2. **Daily app refresher** (`poll_apps_daily`) — runs at 03:00 UTC
   - For every package in `apps_enriched`, call `gp_app()`, compare against last `app_history` row; if any of (`version`, `min_installs`, `score`, `ratings_count`, `reviews_count`, `editors_choice`, `recent_changes`) changed → insert new `app_history` row.
   - Compute `icon_hash` (SHA1 of icon URL) and `screenshots_hash`; if changed → insert into `asset_history`.
   - Concurrent (12 workers, same pattern as `enrich_apps.py`).

3. **Daily aggregations + downsample** (`poll_aggregations`) — runs at 04:00 UTC
   - Aggregate `reviews_*` → `reviews_daily` (incremental: only re-aggregate yesterday + today).
   - Downsample `ranking_history` rows older than 7 days to daily granularity (keep midnight UTC sample, drop the rest).

4. **Weekly keyword rank poller** (`poll_keywords`) — runs every Sunday 02:00 UTC
   - For each (keyword from categories.json, country in TOP_10): `gp_search(keyword, country)`, take top 50, insert positions into `keyword_rank_history`.
   - ~33 categories × ~5 keywords avg × 10 countries = 1,650 calls/week. Trivial load.

Each job writes start/end markers to a new `poller_runs` table for observability + idempotency. All jobs respect the existing `_email_scheduler` 15-min wake cycle; on each wake they check "should this job run now?" against the run log.

## Per-app detail page (the killer surface)

New route `/app/{package_name}` (auth-gated, scope-checked for Free users):

| Section | Source |
|---|---|
| Header (icon, title, dev, current rank chips per country) | `apps_ranked` + latest `ranking_history` |
| **Rank history chart** (line graph, one line per country, last 30 days) | `ranking_history` |
| **Reviews trend** (daily count + avg rating, dual-axis last 90 days) | `reviews_daily` |
| **Rating histogram timelapse** (5 stacked bars, last 6 months) | `reviews_daily` |
| **Update timeline** (vertical list: version → date → recent_changes excerpt) | `app_history` |
| **Asset history** (icon-evolution row of thumbnails + dates of change) | `asset_history` |
| **Keyword footprint** (table: keyword → current rank → 30-day delta) | `keyword_rank_history` |
| **Estimated downloads** (Fermi line graph) | computed: `min_installs` band × review velocity × age |
| **Sentiment timeline** (positive/neutral/negative split per week) | `reviews_*.sentiment` |
| **Developer portfolio** (sibling apps with their own ranks) | `developer_history` + `apps_ranked` |
| **Editor's Choice / chart appearances** badges | `app_history.editors_choice` + `ranking_history` |

Charts use a small ~5KB inline SVG chart library (or hand-rolled — most of these are simple lines). No frontend framework needed.

Free users get the page but capped at last 7 days of history. Solo+ unlocks 90 days.

## Files

| File | Change |
|---|---|
| `auth.py` | Add 6 new tables to DDL; defensive ALTER for `sentiment` column on `reviews_*` |
| `poller.py` (new) | All four polling jobs + a `_should_run(job_name, interval)` helper backed by `poller_runs` table |
| `server.py` | Hook `poller` jobs into existing `_email_scheduler` loop (line 1326); new `/app/{pkg}` route serving `app_detail.html`; new endpoints `/api/app/{pkg}/history`, `/api/app/{pkg}/reviews-daily`, `/api/app/{pkg}/keyword-ranks`, `/api/app/{pkg}/assets`, `/api/app/{pkg}/sentiment`; admin endpoint `/api/admin/poller_status` |
| `app_detail.html` (new) | Per-app deep page template; charts hand-rolled SVG; same theme system as index/help |
| `index.html` | Make Viral Ranking rows clickable → `/app/{pkg}`; add new "Recent ranking change" widget on Home (top movers in user's categories) |
| `enrich_apps.py` | Reuse `gp_app()` → app_history diff helper extracted for poller reuse |
| `requirements.txt` (or pyproject) | Add `transformers` + `torch-cpu` for sentiment (~150 MB; gated behind a `--with-sentiment` flag at boot so single-instance deploy stays small) |
| `categories.json` | No change |

Reused functions: `enrich_apps.fetch_one`, `discover_apps.gp_search`, `auth.log_activity`, server's `_conn()`, `_email_scheduler` thread loop.

## Implementation phases (in execution order)

1. **DDL + poller scaffold** (1 day): add tables, `poller.py`, `poller_runs`, `_should_run` helper, wire into `_email_scheduler`. Verify all jobs run idempotently.
2. **Hourly chart poller + downsample job** (2 days): `poll_charts`, `poll_aggregations`. Verify ~12K rows/hour land in `ranking_history`.
3. **Daily app refresher + asset detection** (2 days): `poll_apps_daily`, `app_history`/`asset_history` writes.
4. **App detail page v1** (3 days): `/app/{pkg}` route, header + rank chart + reviews trend + update timeline. Free-tier 7-day cap enforced.
5. **Reviews daily aggregation + sentiment** (2 days): `reviews_daily` job, `sentiment` column, DistilBERT batch on insert.
6. **Keyword rank poller + footprint table** (2 days): `poll_keywords`, keyword footprint section on app detail.
7. **Developer portfolio tracking + sibling-apps section** (1 day): `developer_history` + UI section.
8. **Fermi download estimate + chart** (1 day): formula + line chart on app detail.
9. **Asset history visualization** (1 day): icon-evolution row.
10. **Home tab "Top movers" widget** (1 day): apps with biggest rank delta in user's categories last 24h.

Total: ~16 working days. Phase 1 (steps 1–4) ~8 days delivers the main user-visible surface; Phase 2 (5–10) adds depth.

## Verification

1. `python3 -c "import poller; poller.poll_charts(force=True, dry_run=False)"` writes ~12K rows to `ranking_history`; rerunning is idempotent (no dupes due to PK).
2. `_email_scheduler` thread visibly fires `poll_charts` on the hour and `poll_apps_daily` at 03:00 UTC; check `poller_runs` for start/end markers.
3. Visit `/app/com.bhakti.app` (or any tracked package). All 11 sections render. The rank-history chart shows ≥24 hourly points after a day of polling. Reviews-trend chart populated from `reviews_daily`.
4. Edit app metadata in DB to fake a version bump; rerun `poll_apps_daily` → new row appears in `app_history` with the version diff.
5. Hash a different icon URL into `apps_enriched`; rerun → new `asset_history` row.
6. As Free user: only last 7 days visible on charts; upgrade flow on attempted scroll-back.
7. As Solo: full 90 days. Sentiment timeline renders (DistilBERT batch must have run on at least one review).
8. Keyword rank: `keyword_rank_history` populated after Sunday job; `/app/{pkg}` shows current rank + 7-day delta per matched keyword.
9. Top-movers widget on Home: apps with positive rank delta in user's categories appear, sorted by absolute delta.
10. SQLite size at 1 month of polling stays under 2 GB (sanity check; trigger Postgres-migration plan if breach).
11. Smoke-test `gp_search.list()` against a sample country (e.g. JP) — confirm response shape is the same as IN; if not, parse-fallback per country.

## Out of scope (deferred)

- **Real DAU/MAU/revenue** — enterprise data sources only; Fermi estimate covers the user need at indie tier
- **Paid-keyword tracking** — UAC dominates app advertising; SEMrush data is low-signal here
- **Keyword search-volume index** — proprietary scoring; can integrate MobileAction ($69/mo) at Pro tier
- **Postgres migration** — keep on SQLite while under 50M rows; explicit migration plan when triggered
- **Residential proxy budget** — start single-IP; add proxies if 429s become regular (track in `poller_runs.errors`)
- **APK SDK fingerprinting** — bandwidth + storage prohibitive; consider Exodus Privacy API in v3
- **Cross-store (App Store) tracking** — Google Play only for v1; iOS doubles infra
- **Country-specific revenue split** — needs panel data, skipped
- **Real-time webhook alerts** (rank-spike notifications) — email-based v2; webhook v3
