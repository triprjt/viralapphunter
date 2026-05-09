# Dashboard Redesign v2: "Sensor-Tower-style Leaderboard + Per-Category Hero" — Chain of Beads

## Context

The current minimalist dashboard (just shipped: 5 stacked cards × 3 categories, 4 chips per card) lands at 2/10. The user pain:
- **No emotional hook** — feels like a CSV, not a tool that makes you want to build the next viral app
- **Cards feel sterile** — apps don't feel real
- **Value prop too weak** — should be flooded with real trends, install analytics, revenue, monthly downloads

The user picked a clear redesign:
1. **Layout:** Sensor-Tower-style leaderboard table per saved category (rank · app · sparkline · installs/mo · Δ% · ★)
2. **Hero banner per category:** big featured #1 app at the top of each category's section, with a large 30-day growth chart and the "this is exploding" feel
3. **New signals on every row:** install-velocity sparkline + weekly review-volume bar + auto-generated "why it's rising" tag

The deliverable is a redesigned per-category section with two parts stacked: hero banner (big, emotional) + leaderboard table (dense, analytical). Same scoring + cold-start fallback chain that's already in place; this is an aggressive UI rewrite, not a data-layer rewrite.

What's already done (from previous chain): minimalist tab-less shell, `monthly_hot_apps` table, composite viral score, `app_history` / `reviews_daily` / `poller_runs` time-series tables, four poller jobs, `_should_run` idempotency. The new chain reuses every bit of that data.

A note on **revenue:** the user asked for "real revenue data." Real per-app revenue is enterprise-only (Sensor Tower / data.ai start at $15k/yr) — not realistic at our tier. The plan ships a **Fermi estimate** for monthly revenue (install band × ARPU heuristic by genre) clearly labeled "est." until we either upgrade our data source or the user accepts the estimate.

---

## The redesign — what each saved category section looks like

```
─── 🚀 Spiritual & Religious ────────────────────────────  (chip: 12 apps tracked · updated 2h ago)

╔══ APP OF THE WEEK ═══════════════════════════════════════════════════╗
║  ┌────────┐   TinyFlow Yoga                            🏷 NEW SURGE   ║
║  │  icon  │   Lotus Studios · Kids' yoga · Released 5w ago            ║
║  │        │                                                             ║
║  │        │   ┌─────────────────────────────────────────────┐          ║
║  │        │   │  ▁▂▂▃▃▄▅▆▇█  30-day install velocity        │          ║
║  └────────┘   │                                              │          ║
║               │  Mar 9      ──────►   Apr 8                  │          ║
║               └─────────────────────────────────────────────┘          ║
║                                                                         ║
║   268K installs/mo   ▲ +24% MoM    ⭐ 8.4 viral score                  ║
║   $4.2K est. revenue  22% 1-2★ recent  142 reviews this week           ║
║                                                                         ║
║   "Finally a yoga app my 6yo loves" — latest 5★ review                 ║
║   [Read all reviews →]  [Open in Play Store ↗]                          ║
╚════════════════════════════════════════════════════════════════════════╝

#  │ App                  │ Trend         │ Inst/mo │ Δ% MoM │  Rev est. │ ★ Score │ Why
───┼──────────────────────┼───────────────┼─────────┼────────┼───────────┼─────────┼────────────────
2  │ 🎵 Daily Mantra      │ ▂▂▃▃▃▄▄▅▅█    │  122K   │  +8%   │  $1.9K    │  7.1   │ Sustained
3  │ 🎵 Sadhguru: Miracle │ ▁▂▂▃▄▅▆▆▇█    │   66K   │ +24%   │  $0.8K    │  6.6   │ New surge
4  │ 🎵 Astrotalk         │ █▇▇▆▆▅▅▅▄▄    │  480K   │  -3%   │ $12.4K    │  5.2   │ Cooling
5  │ 🎵 HiAstro           │ ▁▂▃▃▄▄▅▆▇█    │   50K   │ +15%   │  $0.6K    │  5.0   │ New release
... up to #10
```

The hero banner is **a separate, taller section** with visual weight (gradient background, screenshot, big growth chart). Below it, the leaderboard renders rows 2-10 in a tight table with monospace columns. The user's eye lands on the hero, then drills down via the table.

---

## What's new vs. existing data

| Signal | Source | Status |
|---|---|---|
| Install-velocity sparkline (30-day) | `app_history.min_installs` series | Will be live in 25 days; cold-start uses synthetic curve from `installs_per_month` + linear release-date interpolation |
| Weekly review count | `reviews_daily` (last 7 days SUM(count)) | Live as soon as `aggregate_reviews_daily` runs once |
| "Why it's rising" tag | Computed from score components | Live now (`_viral_score_v1` already returns components) |
| Estimated revenue (Fermi) | Genre × install band × ARPU table | New helper, deterministic from existing fields |
| Latest review excerpt | `reviews_*` ORDER BY posted_at DESC LIMIT 1 WHERE rating>=4 | Live now |
| Hero growth chart | Inline SVG line chart, 30 data points | Cold-start uses synthetic; real after app_history fills |

No new tables. All five signals derive from data we already have or already plan to have. The redesign is pure UI + a few server-side helpers.

---

## The chain

### Phase A — Per-category hero banner

**🪨 Bead R1 — Hero banner CSS + skeleton markup**
- **Goal:** Add `.hero-card` styles (gradient background, 2-column flex: icon-zone left, content-zone right; large numbers; tag pill; "Read reviews →" + "Open in Play Store ↗" CTA row).
- **Files:** `index.html` — CSS block + a `heroHtml(app)` helper that takes the top-1 app in a category and renders the banner.
- **Done when:** `heroHtml(sample)` produces the visual shown in the layout sketch with all four numeric stats, the tag pill, the latest-review line, and both CTAs.

**🪨 Bead R2 — Inline SVG sparkline + hero growth chart**
- **Goal:** Build a tiny vanilla-JS SVG chart helper. `sparkline(values, w, h)` for 80×24 inline rows; `growthChart(values, w, h, opts)` for the larger 600×140 hero chart with axis labels and a gradient area-under-line. No external library.
- **Files:** `index.html` — chart helpers (~80 LOC).
- **Done when:** `sparkline([10,12,14,18,30])` produces a 5-point SVG path; `growthChart([...30 points], 600, 140, {label:'Mar 9 → Apr 8'})` renders an axis + area chart inline. Sample values produce a visibly upward-trending shape.

**🪨 Bead R3 — Server-side hero data + sparkline series**
- **Goal:** New helper `compute_install_series(package_name, days=30)` reads `app_history` to produce 30 data points (linear-interpolated when sparse; synthetic from `installs_per_month` × age when no history yet). Add `series_30d` array to each app's `monthly_hot_apps.payload`. Update `compute_monthly_hot_apps` to compute and write it.
- **Files:** `server.py`.
- **Done when:** every payload row has a 30-element `series_30d` array of integers (real when `app_history` has 25+ days, synthetic ascending curve otherwise).

### Phase B — Sensor-Tower-style leaderboard

**🪨 Bead R4 — Leaderboard table CSS + markup helper**
- **Goal:** Replace per-row `cardHtml(app)` with `leaderboardRowHtml(app, rank)` and wrap rows in a `<table class="leaderboard">`. Columns: rank · icon+name · sparkline · installs/mo · Δ% · revenue · ⭐score · rising-tag. Sticky header. Monospace number columns.
- **Files:** `index.html` — `cardHtml` removed/replaced; new `leaderboardHtml(apps)` builder.
- **Done when:** rendering 9 sample rows produces a clean table with all 8 columns aligned, sparkline cells render, deltas color green/red.

**🪨 Bead R5 — "Why it's rising" tag computation**
- **Goal:** Server-side helper `_rising_tag(components, released_iso)` returning one of: `NEW SURGE` (recency dominates), `MOMENTUM` (momentum dominates), `SUSTAINED` (velocity dominates with stable momentum), `OPPORTUNITY` (low-star dominates), `COOLING` (negative momentum), `NEW RELEASE` (released < 30 days). Persisted in payload.
- **Files:** `server.py`.
- **Done when:** every payload has a non-null `rising_tag` string. Spot-check 3 apps: a fast-rising new app gets `NEW SURGE`, a long-tenured high-installs app gets `SUSTAINED`, an app with negative delta gets `COOLING`.

**🪨 Bead R6 — Estimated revenue (Fermi) + leaderboard wiring**
- **Goal:** Helper `compute_revenue_estimate(genre, installs_per_month, contains_ads, offers_iap)` returns a USD/month integer using a hand-tuned ARPU table per genre (e.g. games $0.50/MAU, finance $4, productivity $1.5; further multiplied by 0.4 if ads-only, 1.0 if IAP). Always labeled "est." in UI. Stored in payload as `revenue_est_usd`.
- **Files:** `server.py` (helper + ARPU table), `index.html` (column rendering with "est." superscript).
- **Done when:** every payload has an integer `revenue_est_usd`. Manual spot-check: a $0.99/mo subscription app with 100K installs comes out near sane order of magnitude.

### Phase C — Hero polish

**🪨 Bead R7 — Hero growth chart inside hero banner**
- **Goal:** Render the bigger 600×140 growth chart inside the hero banner using `series_30d` from R3. Add axis labels (left: 0/peak, bottom: date range), a gradient area fill, and the actual data line.
- **Files:** `index.html` — `heroHtml` calls `growthChart(series, 600, 140, ...)`.
- **Done when:** hero banner shows a visible curve labeled with start/end dates. Chart updates on dashboard reload.

**🪨 Bead R8 — Latest 5★ review excerpt in hero**
- **Goal:** New endpoint `GET /api/app/{pkg}/latest-good-review` returns one row from `reviews_*` where `rating >= 4` ORDER BY `posted_at` DESC LIMIT 1. Hero pulls it with a per-app fetch (lazy: only for the top-1 of each category, ~3-5 fetches per page render).
- **Files:** `server.py` (endpoint), `index.html` (lazy fetch + render under stats).
- **Done when:** hero shows a real review excerpt for any app that has reviews; falls back to "No reviews yet" gracefully.

**🪨 Bead R9 — Hero "Open in Play Store ↗" CTA**
- **Goal:** Both CTAs in the hero work. "Read reviews →" calls existing `setView('reviews')` flow with `state.pkg=pkg`. "Open in Play Store ↗" opens a new tab to `https://play.google.com/store/apps/details?id={pkg}`.
- **Files:** `index.html` — wire in `bindDashboardEvents`.
- **Done when:** clicking each CTA does the right thing.

### Phase D — Leaderboard polish

**🪨 Bead R10 — Sparkline cells + deltas color scale**
- **Goal:** Each leaderboard row renders an 80×24 sparkline (R2 helper) for `series_30d`. Δ% is colored on a 4-step scale: green (>+10%), mint (0..+10%), amber (-10..0%), coral (<-10%).
- **Files:** `index.html`.
- **Done when:** sparklines visually render in every row; deltas colored per scale.

**🪨 Bead R11 — Weekly review-volume mini-bar**
- **Goal:** Add a mini-bar (a single colored block whose width is proportional to `weekly_review_count`) under the app name in column 2 ("Reviews this week: 142"). Server payload field `weekly_review_count` from `reviews_daily` last 7 days SUM.
- **Files:** `server.py` (compute + add to payload), `index.html` (render).
- **Done when:** every row that has reviews shows the mini-bar; rows with zero hide it.

**🪨 Bead R12 — Mobile collapse: leaderboard → compact card list**
- **Goal:** Below 720px the table becomes a stack of compact rows (icon · name+sparkline · big install number · delta chip). Hero banner reflows: chart full-width below stats.
- **Files:** `index.html` CSS media queries.
- **Done when:** Chrome devtools at iPhone 12 viewport: hero readable, table reflows to compact rows, no horizontal scroll.

### Phase E — Cold-start friendliness

**🪨 Bead R13 — Cold-start hero/sparkline fallback**
- **Goal:** When `series_30d` is synthetic (no real `app_history`), the hero growth chart renders the synthetic curve dimmed with a subtle "Trend data accumulating — real curve in 25 days" footnote. Sparklines in the table appear faded for synthetic rows. Differentiates real vs estimated visually.
- **Files:** Server payload tags `series_30d_real: bool`. `index.html` styles the chart accordingly.
- **Done when:** today, the hero says "Trend data accumulating…" and curves are dimmed; after 25 days of polling, the footnote disappears and curves render full-color.

**🪨 Bead R14 — "Updated" + "N apps tracked" subhead per category**
- **Goal:** Replace the global `Updated Mon, May 12 · Refreshes weekly` line with a per-category chip line: `🚀 Spiritual & Religious  ·  12 apps tracked  ·  updated 2h ago`. Pulls from `monthly_hot_apps.computed_at` for each category and `COUNT(DISTINCT package_name)` of apps in that category in `apps_ranked`.
- **Files:** `server.py` (add per-category metadata to payload), `index.html` (render).
- **Done when:** each category section has its own subhead with the live tracked-count + age.

---

## Files

| File | Change |
|---|---|
| `server.py` | Add `compute_install_series`, `_rising_tag`, `compute_revenue_estimate`, `weekly_review_count` helpers; extend `compute_monthly_hot_apps` payload to include `series_30d`, `series_30d_real`, `rising_tag`, `revenue_est_usd`, `weekly_review_count`, `latest_good_review_pkg` flag; add `/api/app/{pkg}/latest-good-review` endpoint; extend `_dashboard_payload` with per-category `tracked_count` + `computed_at` |
| `index.html` | New `heroHtml(app)`, `leaderboardHtml(apps)`, `leaderboardRowHtml(app, rank)`, `sparkline()`, `growthChart()` helpers; replace `cardHtml`/`renderHomeView` body with hero+leaderboard rendering; CSS for `.hero-card`, `.leaderboard`, `.spark`, `.delta-cell`, `.rising-tag`, `.review-volume-bar`; mobile media queries |
| `categories.json` | No change |
| `auth.py` | No change |
| `poller.py` | No change |

Reused functions:
- `_viral_score_v1` already produces `score_components` — `_rising_tag` reads them
- `compute_monthly_hot_apps` is the single write site for the payload — every new field flows through it
- `_top_viral_for_categories` cold-start fallback in `_dashboard_payload` — needs the same payload-shape upgrade for consistency
- `setView('reviews')` + `state.pkg` — existing deep-link flow

---

## Implementation order

1. **R1, R2** (1 day): hero CSS + chart helpers — pure UI, no backend deps
2. **R3** (0.5 day): `compute_install_series` + payload field
3. **R7** (0.5 day): wire chart into hero banner
4. **R5** (0.5 day): `_rising_tag` server helper
5. **R6** (0.5 day): Fermi revenue estimate + ARPU table
6. **R4** (1 day): leaderboard table CSS + row helper — the structural rewrite
7. **R10, R11** (1 day): sparkline cells, delta color scale, weekly-review mini-bar
8. **R8** (0.5 day): latest-review endpoint + hero hookup
9. **R9** (0.25 day): CTA wiring
10. **R12** (0.5 day): mobile responsive
11. **R13, R14** (0.5 day): cold-start polish + per-category subhead

Total: ~7 working days. **Critical-path UI** (R1, R2, R4, R6, R10) ships at day 5 with full hero+leaderboard visuals running on real install/revenue data plus the existing composite score; the rest is polish.

---

## Verification

1. **Hero renders for each saved category:** sign in with 3 saved categories → 3 hero banners visible, each with the #1 app of that category. Numbers (installs/mo, Δ%, viral score, est. revenue) all populated.
2. **Sparklines render in every leaderboard row:** rows 2-10 each show an 80×24 sparkline. Synthetic rows (no `app_history`) appear dimmed with the cold-start footnote on the hero.
3. **Hero growth chart matches series:** `JSON.stringify(monthly_hot_apps[0].payload.series_30d)` matches the chart's last 30 plotted points (visually count peaks).
4. **"Why it's rising" tag is sensible:** sort the leaderboard mentally — apps tagged `NEW SURGE` should have high recency component; `COOLING` apps should have negative delta.
5. **Revenue estimate sane:** known ad-supported app with 1M installs/mo and games genre yields rev_est in the $5–50k/mo range; subscription productivity app at 100K installs yields $1–10k/mo.
6. **Latest review pulls real data:** for any tracked package with ≥4★ reviews, the hero shows the most recent one. Falls back gracefully when no reviews fetched yet.
7. **Mobile reflow:** Chrome devtools at 360px width — hero stacks vertically, table becomes compact rows, sparklines visible, no horizontal scroll.
8. **Performance:** `/api/me/dashboard` payload < 80KB; full dashboard renders in < 200ms after fetch.
9. **Cold-start friendly:** delete `app_history` rows → page still renders; sparklines dimmed; footnote visible. Re-run `compute_monthly_hot_apps()` → page reloads cleanly.
10. **Per-category subhead:** each category section shows `· N apps tracked · updated Xh ago` reflecting live counts.

---

## Out of scope (deferred — not in this redesign)

- **Real revenue from Sensor Tower / data.ai** — enterprise-priced, not realistic at our tier; Fermi estimate w/ disclaimer
- **DAU / MAU / retention / sessions** — same; deferred indefinitely
- **Per-country breakdown of installs/revenue** — single-country (IN default) until poller expands
- **Per-app deep page** (`/app/{pkg}`) — covered by `launchplan/rich-data.md`; the hero CTA still goes to the existing reviews view
- **Filterable leaderboard** (sort columns, filter by tag) — not interactive in v1; cards click through to reviews only
- **Compare two apps side-by-side** — v3 feature, deferred
- **Email-as-dashboard** — same data via Monday email; deferred
- **AI-summarized "why it's rising" prose** — current tag is rule-based from score components; LLM prose later
- **Historical archive** (`/digest/{week}`) — viable v2
