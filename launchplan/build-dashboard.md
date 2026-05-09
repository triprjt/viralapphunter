# User Dashboard — Free-tier Control Center + Weekly Quota + Weekly Public Reports

## Context

Free users currently land on the **Reviews** tab by default and have to discover for themselves what they're allowed to do, what they've already done, and how to manage settings. The lifetime "1 review fetch ever" cap is also too restrictive — it converts skeptics but doesn't let curious users *learn* the product.

The user wants:
1. A **separate dashboard page** (a "Home" tab that becomes the default landing) summarizing what's possible on the current plan, surfacing recent activity, and acting as a control center.
2. **Categories editor** reachable from the dashboard (reuses the existing Settings modal).
3. **My fetched reviews** — list of every app the user has manually fetched, with deep-link to read.
4. **Quota change**: from `1 lifetime` to **3 review fetches per week** on Free.
5. **Generate report / export to Excel** — let users pull their scoped data out as CSV.

**Additional features** that make this dashboard genuinely useful (not just a control panel):
6. **Quota tracker** with progress bars and reset countdown.
7. **Weekly viral-app reports** (the core of the product) — every Monday, a "Top 5 viral apps in {category}" snapshot is generated for each category and published as a **public SEO-indexed page** at `/reports/{category}/{year}-W{week}`. The dashboard surfaces the user's-categories versions; non-signed-in visitors can browse them as content.
8. **Niche of the week** — featured "best opportunity" niche from saved categories.
9. **Recent activity feed** — last 8 actions, condensed.
10. **Quick links** — 4 cards routing to common flows.
11. **Plan & upgrade card** — passive nudge for Free users.
12. **Watchlist** — DB-backed "starred" apps with their current viral score (syncs across devices).
13. **Saved searches** — DB-backed: save a Viral Ranking filter combo and recall it from Home (syncs across devices).
14. **Tip rotation** — one-line product tip, deterministic by date (no per-user storage).

The intended outcome: Free users sign in, land on a dashboard that immediately tells them *"here's what you can do this week, here's what's growing in your niches, here's where you've been"*. They can act, change scope, and export — all from one place. Anonymous SEO traffic from the public weekly reports drives the marketing top-of-funnel.

## Scope

Files modified:
- **`index.html`** — new Home tab + view, quota tracker, mini widgets, export buttons (with weekly cap), watchlist + saved-searches LocalStorage, tip rotator. Make Home the default tab.
- **`auth.py`** — change `can_use_feature()` for `review_fetch` to a rolling 7-day window with cap of 3 (Free), and a parallel rule for new `export` feature with cap=1 (Free) / 10 (Solo) / unlimited (paid). Add `weekly_reports`, `user_watchlist`, `user_saved_searches` tables and `theme` column on `users` to DDL.
- **`server.py`** — new `GET /api/me/dashboard` endpoint, new **public** `GET /reports/{category}/{year}-W{week}` route, new `GET /sitemap.xml`, new background task in `_email_scheduler` to generate weekly reports each Monday.
- **`email_service.py`** — paywall copy: "this week" instead of "lifetime" for `review_fetch`; same labels added for `export`.
- **`report.html`** (new) — public template for a single weekly report.

## Quota model change (the most important behavior change)

**Before:** Free = 1 lifetime distinct review_fetch / developer_lookup.
**After:** Free = **3 review_fetch / week** (rolling 7-day window). Developer_lookup stays at 1 lifetime (low-volume action). AI summary stays at 1 lifetime (still aspirational endpoint).

Server-side `auth.py:can_use_feature(user_id, feature, plan, context)`:
```python
if plan in PAID_PLANS: return True, {}  # unchanged
if feature == "review_fetch":
    # Rolling 7-day window. Identical-context retries don't double-count.
    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    same = conn.execute(
        "SELECT 1 FROM feature_usage WHERE user_id=? AND feature='review_fetch' AND context=?",
        (user_id, context)
    ).fetchone()
    if same: return True, {"reason": "already_used_same_context"}
    n = conn.execute(
        "SELECT COUNT(DISTINCT context) FROM feature_usage WHERE user_id=? AND feature='review_fetch' AND used_at >= ?",
        (user_id, seven_days_ago)
    ).fetchone()[0]
    if n >= 3:
        next_reset = conn.execute(
            "SELECT MIN(used_at) FROM feature_usage WHERE user_id=? AND feature='review_fetch' AND used_at >= ?",
            (user_id, seven_days_ago)
        ).fetchone()[0]
        return False, {"reason": "weekly_limit_hit", "feature": feature, "used": n, "cap": 3, "next_reset": next_reset}
    return True, {}
# other features keep the existing lifetime check
```

Same pattern for `feature == "export"` with cap = 1 (Free) / 10 (Solo) / unlimited (Pro/Studio). Paywall messaging in the dashboard updates accordingly: *"You've used 3/3 fetches this week. Next slot opens on {date}."* Email trigger label for `review_fetch` becomes `WEEKLY_LIMIT_REVIEW_FETCH`. Existing one-shot-per-user idempotency reused.

## The Home tab — layout (top to bottom)

A scrollable single-column page on mobile, two-column on desktop. Each section is a card with a title + content.

### 1. Welcome banner

```
👋 Welcome back, {first_name}        [FREE · ✨ Upgrade]
```

Uses existing tier-badge. On Pro/Studio/Solo, replace upgrade chip with a friendly "Thanks for being on {plan}".

### 2. This-week quota card

Three thin progress bars labeled with cap/used/next-reset:

```
🔍 Review fetches    ●●○ 2 / 3 this week    next slot in 3 days
👤 Developer lookup  ●○ 1 / 1 lifetime      —
✨ AI gap reports    ○ 0 / 1 lifetime       —
```

Colors: full bars in `--coral` (you've used it), empty in `--panel-2`. Hover any bar → tooltip with the reset rule.

### 3. Your saved categories

Chip cluster of the user's `picked_categories` (max 5 on Free). Edit pencil opens the existing Settings modal.

```
📁 Saved categories: [Spiritual] [Health] [Education]    [✏️ Edit]
```

If empty (skipped onboarding), inline CTA: "Pick categories →".

### 4. Quick links (4-card grid)

| Card | Action |
|---|---|
| 🎯 Pick a niche to build in | Niches tab → sort by Best opportunity |
| 🚀 See what's growing fast | Viral Ranking → sort by Installs/mo |
| 💬 Read app reviews | Reviews tab |
| 📖 How to use ViralFinder | /help |

### 5. Weekly viral-app reports (one card per saved category)

This is the **core content surface** of the app. Every Monday a backend job generates a "Top 5 viral apps for {category} — week of {date}" snapshot and saves it to a new `weekly_reports` table. The Home tab shows one card per saved category with its current week's report:

```
🚀 Top 5 viral apps in Spiritual & Religious — week of May 5
  1. Bhakti — 268K installs/mo  ▲ +12% vs last week
  2. Daily Mantra — 122K       ▲ +8%
  3. Sadhguru: Miracle of Mind — 66K  ▲ +24%
  4. Astrotalk — 480K           ↓ -3%
  5. HiAstro — 50K             ▲ +15%
[See full report →]
```

The "See full report →" link routes to `/reports/{category_id}/{year}-W{week}` — a **public, SEO-indexed page** (no auth required) that anyone can browse. Each weekly report is its own URL, generates fresh content for SEO, and acts as the marketing top-of-funnel that drives sign-ups.

### 6. My fetched reviews

Lists every package in `feature_usage` where `feature='review_fetch'`. Each entry:
```
com.foo.bar       fetched 2 days ago     242 reviews    [View →]
```
"View" deep-links to Reviews tab pre-filtered to that package.

### 7. Niche of the week

Picks the highest-opportunity niche from `niche_saturation` filtered to user's saved categories (lowest `app_count × log10(max_installs)`). Card shows niche name, # apps, max installs, "Explore →" button.

### 8. Activity feed (compact)

Last 8 entries from `user_activity`, condensed format:
```
🔍 Fetched reviews for com.foo · 2h ago
✨ Updated saved categories · yesterday
🔑 Signed in · 3 days ago
```
"See full log →" link opens existing Activity modal.

### 9. Watchlist (DB-backed)

Star icon on Viral Ranking rows. Clicking POSTs to `/api/me/watchlist` (toggle add/remove). Home shows watchlist items with current viral score + niche tag, joined against `apps_ranked` at render time. Click to open. Empty state: "Star apps in Viral Ranking to track their rise here." Syncs across devices.

New table:
```sql
CREATE TABLE IF NOT EXISTS user_watchlist (
  user_id      INTEGER NOT NULL,
  package_name TEXT NOT NULL,
  starred_at   TEXT NOT NULL,
  PRIMARY KEY (user_id, package_name)
);
```

### 10. Saved searches (DB-backed)

"Save current filter" button on Viral Ranking filters bar opens a tiny prompt for a name, then POSTs to `/api/me/saved-searches` with the `rankState` snapshot. Home lists them; click → GET the row, restore `rankState`, switch to Viral Ranking. Syncs across devices.

New table:
```sql
CREATE TABLE IF NOT EXISTS user_saved_searches (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL,
  name       TEXT NOT NULL,
  payload    TEXT NOT NULL,         -- JSON: rankState
  created_at TEXT NOT NULL,
  UNIQUE (user_id, name)
);
```

### 11. Export & reports (weekly cap on Free)

Three buttons: **Export Viral Ranking (CSV)**, **Export Reviews (CSV)**, **Export Niches (CSV)**.
Each runs the matching SQL through the existing sql.js DB on the client, formats as CSV, and triggers a download via `Blob` + `URL.createObjectURL`. CSVs open natively in Excel; no server-side export needed for v1.

**Export quota** (mirrors the review-fetch model — exports are valuable but cheap, so a similar weekly cap):
- **Free**: 1 CSV export / week (any of the 3 types). Single counter — pick wisely.
- **Solo**: 10 / week
- **Pro/Studio**: unlimited

Each export logs a `feature_usage` row with `feature='export'` and `context='ranking'|'reviews'|'niches'`. The dashboard shows a "Exports: 0 / 1 this week" mini-tracker next to the buttons. Hitting the cap shows a small inline upgrade nudge: *"Used your weekly export. Solo gets 10 per week. → Upgrade"*

A fourth button **Generate PDF report** is shown but disabled with a 🔒 + "Pro+" tooltip.

### 12. Plan card

Footer of the Home tab. Current plan badge, list of what's included on this plan, "Upgrade →" CTA on Free that opens the existing upgrade modal.

### 13. Tip of the day

Tiny one-liner pinned at the very top of the Home view:
```
💡 Tip: Sort Niches by "Best opportunity" to find under-served corners.
```
Pulled from a hardcoded list of ~10 tips. Index is **deterministic by date** — `day_of_year % len(tips)` — so every user sees the same tip on the same day, and there's no client-side counter to maintain. Computed server-side and inlined into the Home payload.

## Weekly public reports — the SEO content engine

A backend job runs every Monday at 09:00 UTC to compute a "Top 5 viral apps" snapshot for **every category** in `categories.json`. Each snapshot is stored in a new SQLite table and rendered at a public URL, indexed by Google.

### Schema (new table)

```sql
CREATE TABLE IF NOT EXISTS weekly_reports (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  category_id   TEXT NOT NULL,                 -- 'SPIRITUAL_RELIGIOUS' etc.
  year          INTEGER NOT NULL,              -- 2026
  week          INTEGER NOT NULL,              -- 1..53 (ISO calendar)
  generated_at  TEXT NOT NULL,
  payload       TEXT NOT NULL                  -- JSON: top 5 + each app's velocity, % change vs prior week
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_weekly_unique ON weekly_reports(category_id, year, week);
```

### Generation logic

For each category, compute its top 5 apps by `installs_per_month` from `apps_ranked` (filtered to that category via `categories LIKE %,catId,%`). For each app, compute `% change vs last week` by looking up the previous week's snapshot for the same package_name (if any). Store as JSON in `payload`.

Hooks into the existing `_email_scheduler` thread loop in `server.py` (already runs every 15 min) — the loop just adds a "is it Monday + haven't generated this week's reports yet?" check and runs the report generator at most once per ISO week.

### Public route: `GET /reports/{category_id}/{year}-W{week}`

Renders an HTML page (a new `report.html` template) that's:
- **Public** — no auth required, signed-out visitors can read
- **SEO-friendly** — proper `<title>`, `<meta description>`, `<h1>`, `<article>`, JSON-LD schema for "Article" type, canonical URL
- **Linkable** — each report has a permanent URL by year/week
- **Sitemap-included** — a `/sitemap.xml` route lists every weekly report URL so Google indexes them

Layout: brand wordmark header, h1 with category name + week, the top-5 app list with icons + numbers + % change, "Read previous week →" / "Read next week →" links, a CTA at the bottom: *"Want the gap report for any of these apps? Sign in →"*.

### Discovery from the dashboard

The Home tab's weekly-report card (Widget 5) shows the **current week's report** for each saved category, with the "See full report →" linking to the public URL. Internal users get the same content as the public; the marketing surface and the product surface share the data.

### Out of scope (deferred): per-week report archive page (`/reports/{category_id}/`) listing all weeks; AI-summarized prose around the top 5 apps; multi-language reports.

## Server side: `GET /api/me/dashboard`

Returns one JSON blob the Home tab consumes in a single fetch:

```json
{
  "user": { "name": "...", "email": "...", "plan": "free" },
  "categories": ["SPIRITUAL_RELIGIOUS", "HEALTH_FITNESS"],
  "quota": {
    "review_fetch": { "used": 2, "cap": 3, "window": "week", "next_reset": "2026-05-14T..." },
    "developer_lookup": { "used": 0, "cap": 1, "window": "lifetime" },
    "ai_summary": { "used": 0, "cap": 1, "window": "lifetime" },
    "export":       { "used": 0, "cap": 1, "window": "week" }
  },
  "fetched_reviews": [
    { "package_name": "com.foo", "used_at": "...", "review_count": 242 }
  ],
  "weekly_reports": [
    { "category_id": "SPIRITUAL_RELIGIOUS", "year": 2026, "week": 19, "top": [ /* top 5 with deltas */ ] }
  ],
  "niche_of_week": { "term": "...", "app_count": 5, "max_installs": 200000 },
  "activity": [ /* same shape as /api/me/activity, capped at 8 */ ]
}
```

Reuses existing helpers: `auth_mod.get_recent_activity`, raw SQL against `apps_ranked` and `niche_saturation`, `users.picked_categories`. New table `weekly_reports` is the only addition.

## What stays untouched

- Onboarding flow (3-step modal, 5-category cap)
- Settings modal (still the categories editor; the dashboard just links to it)
- Tier badge, theme toggle, hamburger menu, activity log modal
- /help page, /landing page, OAuth, Stripe placeholder
- The other three tabs (Reviews / Viral Ranking / Niches / Admin) — same as before, just no longer the default landing
- Existing email triggers (just the paywall_hit copy adjusts to "weekly limit hit")

## Files

| File | Change |
|---|---|
| `index.html` | New Home tab + render function; default `currentView` becomes 'home'; export-CSV utility; tip rendered from server payload; watchlist + saved-searches widgets fetch from new endpoints; star icon column in Viral Ranking; theme read from `vf-user data-theme` (no localStorage) |
| `auth.py` | `can_use_feature()` for `review_fetch` becomes a rolling 7-day window with cap=3 (Free); same pattern for new `export` feature with cap=1 (Free) / 10 (Solo) / unlimited (paid); add `weekly_reports`, `user_watchlist`, `user_saved_searches` tables and `theme` column on `users` to DDL |
| `server.py` | New `GET /api/me/dashboard` aggregator endpoint; new `GET/POST/DELETE /api/me/watchlist` and `GET/POST/DELETE /api/me/saved-searches` endpoints; new `POST /api/me/theme` (writes `users.theme`); new `GET /reports/{category}/{year}-W{week}` PUBLIC route serving `report.html`; new `GET /sitemap.xml`; new background task in `_email_scheduler` to generate weekly reports each Monday |
| `help.html`, `landing.html` | Theme bootstrap removed (was reading from localStorage). Signed-in pages get theme from `vf-user data-theme` meta tag (server-injected). Anonymous pages always render dark. |
| `email_service.py` | Update `PAYWALL_LABELS` + `_paywall_hit` body to mention "this week" instead of "lifetime" for `review_fetch`; same labels added for `export` |
| `report.html` (new) | Public template for one weekly report — same theme system as landing/help, SEO meta tags, JSON-LD structured data, prev/next-week navigation |

## Verification

1. **Sign in as Free user** → lands on Home tab by default. Welcome banner, quota tracker, categories chip, quick links, top-5 viral, fetched reviews, niche of the week, activity feed, exports, plan card all render.
2. **Quota tracker** initially shows `0 / 3` for review_fetch. Fetch one app → `1 / 3`. Fetch a second → `2 / 3`. Third → `3 / 3` and bar fills.
3. **Paywall on 4th attempt**: error returned says "weekly_limit_hit" with `next_reset` timestamp. UI shows "You've used 3/3 fetches this week. Next slot opens in X days."
4. **Same-package retry doesn't count**: try fetching the first package again → request returns 200 with `already_used_same_context`, quota stays at 3/3 (no double-count).
5. **My fetched reviews list** shows the 3 fetched packages with timestamps; click → opens Reviews tab pre-filtered.
6. **Top viral in your categories**: the 5 rows are all from user's saved categories; Solo+ user sees top 5 from across all 33.
7. **Categories edit**: pencil button opens Settings modal; save → quota tracker, top-5, niche-of-week all re-render with the new scope.
8. **Export CSV** (with weekly cap): click "Export Viral Ranking" → CSV downloads named `viralfinder-ranking-{date}.csv`; opens cleanly in Excel/Numbers; respects user's category scope. Free quota tracker shows `1 / 1 this week`. Try a second export → blocked with inline upgrade nudge.
9. **Weekly public report**: visit `/reports/SPIRITUAL_RELIGIOUS/2026-W19` while signed out → page loads, shows the top-5 snapshot, has proper `<title>` / `<meta description>` / canonical URL / JSON-LD `Article` schema. View source to confirm SEO markup. Hit `/sitemap.xml` → lists every published weekly report URL.
10. **Weekly report card on Home**: each saved category shows a card with that week's top-5 + % deltas; click → opens the public report URL.
11. **Generation job**: manually trigger via `python3 -c "import server; server._generate_weekly_reports()"` → new rows in `weekly_reports`. Hit the public URL → renders cleanly.
12. **Watchlist**: star an app on Viral Ranking → POST to `/api/me/watchlist` succeeds; row inserted into `user_watchlist`; appears in Home watchlist card. Un-star → DELETE removes the row. Sign in from a second browser → same starred apps visible (DB-backed, not localStorage).
13. **Saved searches**: configure a Viral Ranking filter, click "Save filter", name it → POST to `/api/me/saved-searches` inserts a row; appears in Home; click → loads `rankState` payload back and switches to Viral Ranking. Sign in elsewhere → same saved searches visible.
14. **Manual upgrade to Solo**: `UPDATE users SET plan='solo'` → quota tracker shows `0 / 100 this month` for review_fetch; export tracker shows `0 / 10 this week`; PDF export button enables.
15. **`/api/me/dashboard` endpoint** returns the JSON shape above; smoke-test with `curl` after setting a session cookie.
16. **Activity feed** on the Home tab matches `/api/me/activity?limit=8`; click "See full log →" opens the existing Activity modal.
17. **No regression**: Reviews / Viral Ranking / Niches tabs all still work; user can navigate between them and Home freely; breadcrumb resets sensibly.

## Out of scope this round

- **Server-side PDF generation** — button is shown but locked behind Pro+. Wire-up later.
- **Theme preference persistence for anonymous visitors** — landing/help pages always render dark when signed out; signed-in users get their saved theme from `users.theme`.
- **Historical viral-score deltas** for watchlist ("up 2.4 since you starred") — needs a timeseries table; v2.
- **Custom report builder** (pick columns, save as a template) — single-click CSV exports only for v1.
- **Email-attached PDF reports** — Pro+ feature; deferred.
- **Charts** (bar, line) on the dashboard — keep v1 text-and-table; add charts later.
- **Tip A/B testing** — fixed list of ~10 tips, deterministic rotation.
- **AI prose summaries on weekly reports** — top-5 list is enough for v1; AI-generated paragraphs come later.
- **Per-week archive page** (`/reports/{category}/`) — only the current week's URL is exposed for v1.
- **Multi-language weekly reports** — English only.
