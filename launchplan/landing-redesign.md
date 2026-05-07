# Landing Page Redesign — Bright + Sensor-Tower-shaped

## Reference analysis

Three Sensor Tower screenshots provided. The structural moves to borrow:

| Element | Source detail | What we adopt |
|---|---|---|
| **Sticky nav with pill CTAs** | Black pill "Contact Sales" + black pill "Log In" right-aligned | Same: rounded-full primary CTA + secondary outline |
| **Full-bleed teal logo bar** | Bright teal (#3FE0B6-ish) with black wordmark logos centered, "regularly cited by" caption | Same vibe but **bright multi-color band** with placeholder indie-dev press mentions |
| **3-column "How it works"** | Simple line icons + step number + 1-line title + 3-line description | Same exactly — but icons get **flat 2-color** treatment |
| **Dark navy testimonial slab** | Full-bleed deep navy with giant quote on left, Duolingo wordmark on right | Same — dark slab at midpoint to give the page rhythm |
| **Alternating L/R product sections** | Title + paragraph on one side, floating product mockup on the other | Same: 3 alternating rows showcasing Ranking / AI / Niches |
| **Generous whitespace** | 80–120px section padding, large type | Match |

The current `landing.html` is dark-mode and copy-heavy. We pivot to **light-mode, bright-accent** layout — same content, very different feel.

## Visual system

### Palette (4 bright accents, not just one)

| Token | Color | Use |
|---|---|---|
| `--bg` | `#FFFCF5` | warm off-white page background |
| `--text` | `#0F172A` | near-black body |
| `--muted` | `#64748B` | slate gray for secondary copy |
| `--mint` | `#06D6A0` | hero gradient + logo bar (Sensor Tower equivalent, punchier) |
| `--coral` | `#FF6B6B` | primary CTA, viral-score energy |
| `--sunshine` | `#FFD23F` | highlight chips, "new" badges, dot patterns |
| `--violet` | `#7C3AED` | feature accents, alternating section backgrounds |
| `--ink` | `#0B1220` | dark testimonial slab (Sensor Tower navy, deeper) |
| `--peach` | `#FFE8D6` | soft section background for "how it works" |

The hero headline uses a 3-stop gradient `coral → violet → mint` for the keyword "viral app" — modern, punchy, instantly differentiates from competitors.

### Typography

- Body: `Inter` (Google Fonts) — clean, free, default for SaaS
- Display: `Inter` 800 weight + tight letter-spacing for headlines
- Optional: `Geist Mono` for tagline / data flourishes

### Buttons

- **Primary CTA**: solid `--coral`, white text, full pill, drop shadow on hover
- **Google sign-in**: white pill with Google G icon (kept from current design)
- **Secondary**: text-only with arrow `Learn more →`

## Section-by-section layout (top-to-bottom)

### 1. Sticky nav
- Logo (wordmark) left
- Center: `Features` `How it works` `Pricing` (text links, slate)
- Right: `Sign in` (text) + `Get started free` (coral pill)
- White background, subtle bottom border on scroll

### 2. Hero
- 2-column: left text, right product mockup
- **Left**:
  - Soft yellow `--sunshine` pill badge: `🔥 10,172 apps tracked across 33 categories all over the world`
  - 56px headline: `Find your next [viral app gradient] idea before everyone else.`
  - 19px slate lede (existing copy)
  - Dual CTA: coral primary `Get started — it's free` + secondary `See how it works ↓`
  - Fineprint: `No credit card required.`
- **Right**:
  - Floating laptop-frame screenshot of the actual Viral Ranking tab
  - Behind it: 2 colored blob shapes (mint + violet) at low opacity for depth
  - On top of it (bottom-left corner): a smaller floating phone-frame screenshot of the niche pill / detail card

### 3. Bright logo bar (full-bleed)
- `--mint` background, full edge-to-edge
- Caption: `As featured in our beta cohort:`
- 6 placeholder press logos in black: `INDIE HACKERS · BUILD IN PUBLIC · PRODUCT HUNT · TRENDS.VC · MICROCONF · APP MASTERS`
- (Real logos when we get press; for now, those names work)

### 4. "How it works" — 3 steps with line icons
- White section, soft `--peach` highlight band behind the icons
- Three columns:
  - **Step 1: We crawl Google Play** — line icon: globe + scanner. *We pull every app + review across 33 categories so you don't have to.*
  - **Step 2: We score velocity, not vanity** — line icon: rocket on a curve. *Lifetime installs lie. Our viral score weights installs/month so brand-new entrants surface.*
  - **Step 3: AI tells you what to build** — line icon: sparkle + lightbulb. *Every review summarized. Every niche gap mapped. Every developer's catalog one click away.*
- Each icon is 2-color (line in `--ink`, fill highlight in one of `--mint`/`--coral`/`--violet`)

### 5. Feature row A: Viral Ranking (alternating L)
- Left: heading `Velocity-ranked, not lifetime-ranked.` + body + small `Learn more →`
- Right: floating laptop mockup with the actual ranking table (use our existing screenshot; until we have one I'll use a styled CSS-mock that mimics it)
- Background: subtle `--mint` blob top-right at 8% opacity

### 6. Feature row B: AI Idea Generator (alternating R)
- Left: floating "AI panel" mockup with a fake conversation box generating 3 app ideas
- Right: heading `Stop guessing. Generate.` + body + `Learn more →`
- Background: subtle `--violet` blob top-left

### 7. Feature row C: Niche Saturation (alternating L)
- Left: heading `Find the corners no one's built yet.` + body
- Right: floating mockup showing the Niches tab with "Best opportunity" sort
- Background: subtle `--coral` blob bottom-right

### 8. Dark testimonial slab (full-bleed `--ink`)
- Big serif/italic quote on left, customer name in `--mint` underneath (matches Sensor Tower's teal)
- "Logo" placeholder on right (we don't have logos yet so use a stylized `INDIE STUDIO` text mark in `--mint`)
- Cycle 3 quotes via simple JS (auto-rotate every 6s, dots below)

### 9. Pricing (bright)
- White section, 4 cards
- Free / Indie ($29) / Pro ($99 — featured, coral border + "Most popular" badge) / Studio ($299)
- Card hover: lift + slight tilt
- Each plan still uses the same feature lists from the strategy doc

### 10. Final CTA banner
- Full-bleed gradient background: `coral → violet → mint` (subtle, ~60% opacity)
- Big centered heading: `Ready to find your next viral app?`
- Single big white pill button: `Sign in with Google to start free`
- Trust line below: `No credit card. No sales call. Cancel any time.`

### 11. Footer
- Light gray, 3 columns: Product / Resources / Legal
- Brand mark, copyright, social icons
- Newsletter signup bar (saves email, fires welcome later)

## Files to modify

- **`/Users/triprjt/garage/googleplaystorereviews/landing.html`** — full overwrite
- Inline all CSS (consistent with project style; no build step)
- SVGs inline for icons (4 total: globe, rocket, sparkle, Google G)
- Use Google Fonts `Inter` via `<link>` tag
- Product mockups: pure-CSS divs styled to look like the dashboard screenshots; placeholder boxes for the laptop/phone frames using `border-radius` + drop-shadow

## What stays from the existing landing.html

- Existing copy (hero headline, lede, feature descriptions, pricing tier features, testimonials)
- Google sign-in OAuth flow (`/auth/google/start` link)
- Pricing tier amounts ($29 / $99 / $299) and feature lists

## Verification

After deploy:
1. `curl http://localhost:8000/` returns 200 with the new HTML
2. View on desktop (1440px), tablet (768px), mobile (375px) — should be responsive
3. All CTAs route to `/auth/google/start`
4. No console errors; no missing fonts/icons
5. Lighthouse desktop performance: aim for >90 (light-mode + minimal JS)

## Out of scope (this round)

- Real product screenshots — using CSS mocks and placeholder boxes; we swap real images later
- Real customer logos — using indie-newsletter-style brand mentions for now
- Animations — keep to subtle hover lifts; no Framer Motion
- A/B test variants — just one version
- Privacy/Terms pages — link is there but page itself is a separate task
