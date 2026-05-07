# Launch Plan: ViralFinder (working title)

## Context

What we have built locally is, in MBB terms, a **competitive intelligence and viral-app discovery tool for the Google Play ecosystem**. Today's enterprise incumbents (Sensor Tower, data.ai, Apptopia, AppFollow) charge $5k–$50k/year and target VCs and large studios. The under-served wedge is **the indie-developer / solo-founder / micro-studio segment** that needs the same insights at a $30–300/month price point — and crucially, needs not just data but **a recipe**: which app to build, what's missing in the existing apps, and a starter blueprint.

This document is the launch strategy. The intended outcome is a SaaS product that crosses **$20K MRR within 6 months** at sub-15% gross-cost-of-revenue, defensible through (a) cheaper pricing than enterprise incumbents, (b) AI ideation features they don't have, and (c) an SEO content moat from auto-generated niche reports.

---

## 1 · Executive Summary

| | |
|---|---|
| **Product** | Browser dashboard + email reports surfacing fast-growing apps, niche saturation, and AI-generated app-idea blueprints across Google Play |
| **Primary buyer** | Indie devs, solo founders, micro-studios, ASO consultants, growth marketers |
| **Wedge** | Enterprise-grade signals + AI ideation at indie pricing. Incumbents charge 30–100× more |
| **Pricing** | Free → $29 (Indie) → $99 (Pro) → $299 (Studio) → custom |
| **6-mo target** | 200 paying users, **$18.2k MRR / $218k ARR**, 87% gross margin |
| **Defensibility** | (i) SEO from auto-generated niche reports, (ii) cumulative scrape history (cohort velocity is impossible to backfill), (iii) AI ideation prompts tuned on real user wins |
| **Capex to start** | ~$1.5k (domain, proxy budget, Stripe activation, beta-tester credits) |

---

## 2 · Market & Positioning

### Total Addressable Market

- 32M apps + games on Google Play; ~75K developer studios releasing 1+ apps/year
- **Indie segment**: ~150K solo/duo developers globally generating <$10K/mo (per IndieHackers, Twitter buildinpublic surveys)
- **Service buyers**: ~5K active ASO agencies, ~10K mobile-growth consultants
- **Conservative SOM (Year 1):** 100K reachable indie devs × 1% paid conversion × $50 avg ARPU = **$60M/yr ceiling for the indie tier alone**

### Competitive Map

| Player | Price | Audience | Our advantage |
|---|---|---|---|
| Sensor Tower | $40K+/yr | Enterprise | 100× cheaper; AI ideation |
| data.ai (App Annie) | $25K+/yr | Enterprise | Same |
| AppFollow | $80–500/mo | Mid-market ASO | We add ideation + niche-saturation, not just ASO/reviews |
| Apptopia | Custom enterprise | VCs, M&A | We're SMB-shaped; weekly content moat they ignore |
| Free scraper scripts | $0 | Hackers | We provide curation + AI + UI; they don't |

**Positioning statement:**
> *"For indie developers and small studios who want to find the next viral app idea, ViralFinder is a Google Play intelligence dashboard that combines velocity tracking with AI-generated app blueprints — unlike Sensor Tower's enterprise pricing or DIY scraping scripts."*

### Pitch (3 versions)

- **One-liner**: *"Find your next viral app idea before everyone else."*
- **30-second**: *"We crawl all 33 categories of Google Play, score apps by velocity (installs/month, not just lifetime installs), and use AI to read every review so you know exactly what users complain about — turning competitive intelligence into a build-this-app blueprint, in $29/month instead of $40K/year."*
- **Founder-friendly framing for ProductHunt**: *"Stop guessing what app to build. We tell you which niches are growing, which incumbents users hate, and what to differentiate on — pulled from 10K+ real apps and millions of real reviews."*

---

## 3 · Product Strategy: Free vs Paid Feature Split

The split is engineered to (a) make the free tier genuinely useful for SEO traffic + organic word-of-mouth, (b) gate the *recurring-value* features (alerting, AI, exports), and (c) move the capacity-cost features (review scraping, AI calls) behind paid plans.

### Free tier (acquisition)

- Browse the **5 most-popular pre-curated categories** (Spiritual, Health, Education, Finance, Productivity)
- View **top 25 viral apps per category**
- See basic columns: rating, installs bucket, release date, developer
- **1 free niche-saturation report** per category (interactive, but PDF/CSV export gated)
- Use the public, SEO-indexed weekly Niche Report blog (always free — this is the marketing engine)

### Indie ($29/mo, $24/mo annual)

Everything in Free, plus:
- All **33 categories** + ability to add **3 custom keyword bundles**
- Per-developer catalog lookup (the "all apps by Sri Mandir Team" feature)
- **100 review-fetch credits/month** (1 credit = 1 app fetched up to 5,000 reviews)
- **50 AI summaries/month** (themes + complaints + feature requests per app)
- **Weekly email** of new viral candidates in your saved categories
- CSV export of apps + niches
- 1 user seat

### Pro ($99/mo, $79/mo annual)

Everything in Indie, plus:
- **Unlimited review fetches**
- **500 AI summaries/month**
- **AI app-idea generator**: "Generate 3 differentiated app concepts for niche X" (uses top-10-app reviews + saturation data)
- **Daily email digests** + push alerts (Slack webhook, mobile push)
- Comparison reports: app A vs app B feature delta
- Historical velocity tracking (12-week rolling)
- **Sheets/Notion integration** for live exports
- 3 user seats

### Studio ($299/mo, $239/mo annual)

Everything in Pro, plus:
- **5,000 AI summaries/month**
- **API access** (REST + webhooks): rate-limited 60 req/min
- **Custom category curation** (we hand-build keyword sets for your vertical)
- White-label report option
- Dedicated Slack channel + 1 monthly strategy call
- Unlimited seats

### One-time / non-subscriber products

- **Niche Deep Dive** ($49 one-time): hand-curated PDF report on one niche (we already have the auto-generated content — packaging it for non-subscribers is pure margin). Drives top-of-funnel for the subscription.
- **App Idea Generator credits** (10 for $19, 50 for $79): for users who want to try AI ideation without committing to a subscription.

### Variables we need to test post-launch

1. **Indie pricing**: $29 vs $39 — both within indie willingness-to-pay; A/B in week 4.
2. **Free-tier review fetches**: do we give 1 free fetch ever, or 1/month? Affects activation rate vs cost.
3. **AI summary volume per plan**: track actual usage — likely under-utilized; can lower limits and cut cost.
4. **Annual discount**: 17% (2 months free) vs 25%. 17% is the SaaS norm.
5. **Studio dedicated-channel cost** (~30 min/week × 20 customers = 10h/wk founder time at scale — model whether it pencils).

---

## 4 · AI-Enabled Features (the differentiator)

Anthropic Claude (Sonnet 4.6 for ideation, Haiku 4.5 for summarization) is the right vendor because of (a) prompt caching to reduce per-app costs, (b) high-quality structured output, (c) lower hallucination on factual extraction tasks vs. competitors. **Always include prompt caching** for category-keyword corpora and review templates — this drops per-call cost ~70%.

| Feature | Plan | Model | Tokens (in/out) | Cost/call | Use |
|---|---|---|---|---|---|
| Review theme extraction | Indie+ | Haiku 4.5 | 8K / 1K | $0.013 | Cluster reviews into "complaints / praises / feature requests" |
| Sentiment trend over time | Pro+ | Haiku 4.5 | 12K / 1.5K | $0.020 | Monthly rolling sentiment per app |
| Differentiation gap report | Pro+ | Sonnet 4.6 | 15K / 2K | $0.075 | "What's missing in the top 10 apps for niche X?" |
| App idea generator | Pro+ | Sonnet 4.6 | 20K / 3K | $0.105 | 3 differentiated concepts + value props + initial feature list |
| Translate reviews → EN | Indie+ | Haiku 4.5 | 5K / 5K | $0.030 | Make Hindi/Tamil/Korean reviews readable |
| Comparison: app A vs B | Pro+ | Sonnet 4.6 | 18K / 2K | $0.084 | Feature/sentiment delta |
| Auto-generated weekly Niche Report | All (public) | Sonnet 4.6 | 25K / 4K | $0.135 | SEO content engine |

**Cost guardrails:**
- Per-user soft cap on AI calls (numbers above)
- Cache identical (app, week) summaries
- For the SEO Niche Reports: budget $50/week × 52 = $2,600/yr, generates ~400 indexed landing pages

---

## 5 · Data Export & Email Reports

### Export formats (matrix)

| Format | Free | Indie | Pro | Studio |
|---|---|---|---|---|
| CSV (apps, reviews) | – | ✓ | ✓ | ✓ |
| JSON (download) | – | ✓ | ✓ | ✓ |
| PDF/Markdown reports | – | ✓ (10/mo) | ✓ (50/mo) | ✓ (unlimited) |
| Google Sheets connector | – | – | ✓ | ✓ |
| Notion sync | – | – | ✓ | ✓ |
| REST API + webhooks | – | – | – | ✓ |

### Email cadence

- **Free**: 1 monthly digest (top 10 viral apps across all categories) — pure top-of-funnel
- **Indie**: weekly digest of new viral candidates in saved categories
- **Pro**: daily digest + Slack/Discord webhook
- **Studio**: daily digest + custom alert thresholds (e.g. "ping me when any app crosses 1M installs/mo in the Astrology niche")

### Email infrastructure

- Resend ($20/mo for first 50K emails) → SendGrid at scale
- All emails generated from React Email templates (one template; data-driven) so we don't pay design cost per email type
- One-click unsubscribe (CAN-SPAM, but also for engagement health)

---

## 6 · Server & Infrastructure Costs

### Architecture (post-launch, target ~500 users)

```
Cloudflare Pages (static frontend, $0)
        ↓
Fly.io API (3 regions, autoscale 1–3 instances) — $80–200/mo
        ↓
Postgres on Neon (Scale plan) — $69/mo
        ↓
Background workers (Render or Fly machines) — $50/mo
        ↓
Cloudflare R2 (export blobs, ~50GB) — $5–10/mo
        ↓
Upstash Redis (cache, rate limit) — Free tier → $10/mo
```

**External services**

| Service | Cost | Why |
|---|---|---|
| Bright Data / SmartProxy rotation | $75/mo | Avoid Google IP-blocking on scrape volume |
| Anthropic Claude API | Variable (see below) | AI features |
| Resend | $20/mo | Email |
| Stripe | 2.9% + 30¢/txn | Billing |
| Sentry | $26/mo | Error tracking |
| PostHog | $0 (free tier ~1M events) | Product analytics |

### Total Infra OpEx by user count

| Stage | Users | Infra | AI | Total | $/user/mo |
|---|---|---|---|---|---|
| Closed beta | 20 | $50 | $30 | $80 | $4.00 |
| Public launch | 100 | $200 | $250 | $450 | $4.50 |
| 6-mo target | 500 | $420 | $1,800 | $2,220 | $4.44 |
| Year 1 stretch | 1,500 | $850 | $4,500 | $5,350 | $3.57 |

Key observation: **per-user infra cost is roughly flat at ~$4/user/mo**, and AI costs scale with usage tier (already priced into plans). Gross margin holds at 85–90% across all sustainable scales.

### Cost-control levers

1. **Prompt caching** on every Claude call — saves ~70% on repeated category corpora.
2. **Move expensive scrapes to overnight batch** — single proxy IP serves many users when not real-time.
3. **Cache aggressive** at the (category, week) level — most users in the same niche see the same data.
4. **Move review fetching to a credit system** with monthly carry-over (capped at 2× monthly allotment) — rewards bursty usage without runaway cost.
5. **Tier the proxy budget**: free/Indie users go through cheap shared proxies; Pro/Studio get residential rotation.

---

## 7 · Pricing & Unit Economics

### Mix assumption at 6 months (200 paying users)

| Plan | Users | MRR contribution |
|---|---|---|
| Free | ~3,000 | $0 (acquisition fuel) |
| Indie ($29) | 80 | $2,320 |
| Pro ($99) | 100 | $9,900 |
| Studio ($299) | 20 | $5,980 |
| **Total MRR** | **200** | **$18,200** |

### Per-user gross margin

| Tier | ARPU | Infra | AI | COGS | GM% |
|---|---|---|---|---|---|
| Indie | $29 | $4 | $0.65 | $4.65 | **84%** |
| Pro | $99 | $4 | $6.50 | $10.50 | **89%** |
| Studio | $299 | $4 | $65 | $69 | **77%** |
| **Blended** | $91 | $4 | $10 | $14 | **85%** |

Blended GM% is conservative — real Studio users likely use far fewer than 5K AI summaries.

### LTV / CAC math (assumed monthly churn 5% Indie, 3% Pro, 2% Studio)

| Tier | Avg lifetime | LTV (gross-margin-adjusted) | CAC target | LTV/CAC |
|---|---|---|---|---|
| Indie | 20 mo | $487 | <$100 | **4.9×** |
| Pro | 33 mo | $2,910 | <$300 | **9.7×** |
| Studio | 50 mo | $11,500 | <$1,500 | **7.7×** |

All tiers comfortably above the 3× LTV/CAC SaaS threshold. **Pro is the unit-economic sweet spot** — focus paid acquisition there.

### CAC strategy

| Channel | Expected CAC | Expected mix |
|---|---|---|
| SEO from auto Niche Reports | <$10 (content amortizes) | 50% |
| Cold outbound to ASO/agency lists | $80 | 15% |
| ProductHunt + Twitter buildinpublic | $20 (founder time) | 15% |
| Newsletter sponsorships | $150 | 10% |
| Affiliate (30% recurring/12mo) | $87 | 10% |

---

## 8 · Go-to-Market: 6-Month Sequencing

### Month 0–1: Closed beta
- Hand-pick 20 indie devs from Twitter/IndieHackers
- Free access in exchange for weekly feedback calls
- Goal: confirm value prop, identify the 2 must-have features Free→Indie conversion needs
- Ship the **single most-loved feature** as a public free demo

### Month 2: Public launch
- ProductHunt launch (target top 5 of day)
- 1 long-form launch post (HN, Indie Hackers front page)
- Pre-built Niche Reports for 33 categories live as SEO landing pages from day one
- Free tier live; Indie tier live; introductory annual deal at 30% off (limited 100 spots)

### Month 3–4: Pro tier + content engine
- Ship AI summaries (Indie) and AI ideation (Pro)
- Weekly Niche Report goes live as paid newsletter ($9/mo) + free (delayed-by-2-weeks) version
- First newsletter sponsorship deal ($200 in Trends.vc-style newsletter)
- Goal: 100 paying users by end of M4

### Month 5–6: API + Studio
- API tier ships
- First white-labeled customer (1 ASO agency)
- ProductHunt retro launch ("Made with ViralFinder" gallery)
- Goal: 200 paying users, $18K MRR, 87% GM

### Month 7–12 (post-launch roadmap)
- Apple App Store integration (huge market, currently a gap)
- Mobile companion app (push alerts)
- Multi-language scraping at scale (Mandarin, Spanish, Hindi each unlock distinct niches)
- Affiliate program with 30% recurring/12mo

---

## 9 · Key Risks & Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| **Google scraper breaks** (HTML/JSON changes) | High; library-dependent | Maintain fork of `google-play-scraper`; fallback to SerpAPI ($50/mo) for redundancy |
| **IP rate-limiting at scale** | High | Rotate proxies (Bright Data $75/mo); shard scrape across regions |
| **Google ToS challenge** | Medium-low for public listing data; **Higher for review storage** | Anonymize reviewer names; aggregate sentiment; jurisdiction-shop hosting |
| **Anthropic outage / price hike** | Medium | Abstract LLM behind interface; OpenAI/Gemini fallback ready |
| **Cold-start data moat** | Medium | Pre-seed all 33 categories before launch (already done); accumulate cohort velocity history (week-over-week) which competitors can't backfill |
| **Enterprise incumbents launch indie tier** | Low (their cost structure prevents it) | Stay obsessively cheap and indie-native (community, content, mobile UX) |
| **Free tier cannibalises paid** | Medium | Strict review-fetch and category gating in Free; the gating is the conversion engine |
| **Founder bandwidth on Studio support** | Medium | Cap Studio tier at 30 customers until first hire |
| **High-AI-usage Studio user blows margin** | Low–Medium | Hard caps in Studio (5K/mo); negotiate enterprise on case-by-case at >5K |
| **Apple App Store gap** | Medium | Roadmap M7+; enterprise customers will ask early |

---

## 10 · 90-Day Execution Punch List

### Weeks 1–2: Foundations
- Migrate from local SQLite to multi-tenant Postgres (Neon)
- Stripe Billing integration; Stripe-hosted checkout
- Auth (Clerk or self-hosted Supabase Auth)
- Cloudflare Pages deploy of frontend
- Hetzner/Fly.io API deploy

### Weeks 3–4: Free→Paid plumbing
- Plan-aware feature gating (one decorator/middleware)
- Credit/quota tracking (review fetches, AI calls)
- Email infra (Resend + React Email templates)
- One transactional email per plan (welcome, weekly digest, paywall hit)

### Weeks 5–6: SEO content engine
- Auto-generate one Niche Report per category (33 reports, indexable)
- Schema.org markup for app listings (rich snippets)
- Sitemap + robots.txt; submit to Search Console
- One human-edited "Top 50 X apps in 2026" longform per week — content moat

### Weeks 7–8: AI features
- Review-summary pipeline (background worker)
- App idea generator (Pro endpoint)
- Prompt caching wired everywhere (≥70% cost reduction)

### Weeks 9–12: Launch
- Closed beta (20 users) → public launch on PH
- Affiliate program live
- First $1k of paid acquisition tested (newsletter ad + 1 cold-outbound experiment)
- Stretch: API tier scoped + alpha-tested

---

## 11 · Variables to Decide in Beta (open questions)

| # | Question | Default | Test plan |
|---|---|---|---|
| 1 | Free-tier review fetches: 0 or 1/month | 0 | A/B in week 4; measure activation→trial conversion |
| 2 | Indie pricing: $29 vs $39 | $29 | A/B with new signups; check 30-day retention |
| 3 | Annual discount: 17% vs 25% | 17% | Track annual vs monthly mix |
| 4 | One-time Niche Report: $49 vs $19 | $49 | Test in M3 |
| 5 | AI summary monthly quota: tighter or looser | Indie 50, Pro 500, Studio 5K | Track p95 usage; tighten if <30% |
| 6 | Niche Report: paid newsletter vs free with delay | Free with 2-week delay | Mirror Trends.vc playbook |
| 7 | Studio dedicated-channel: keep at scale or replace with priority email | Keep until scale forces hand | Track founder hours/customer |

---

## 12 · Success Criteria

By month 6, ship is "yes-launch" if **all** of the following hold:

- [ ] **$18K+ MRR** with mix-shift toward Pro tier
- [ ] **<5% gross monthly churn** (Indie blended)
- [ ] **>85% gross margin**
- [ ] **CAC <$100** for Indie tier (proxy: paid spend / new Indie subs)
- [ ] **At least 30 free→paid conversions** demonstrated on a 30-day cohort basis
- [ ] **One white-labeled Studio customer** (proves enterprise pull)
- [ ] **30+ SEO-indexed Niche Reports** ranking in top 50 for their target queries

If we hit those, raise a small $500K–$1M angel/pre-seed round at end-of-Y1 to hire (i) one full-stack engineer, (ii) one growth marketer, (iii) underwrite Apple App Store integration.

If we miss MRR target by >40%: pivot positioning toward agencies (sell Studio tier deeper) before scaling acquisition spend.
