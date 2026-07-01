# SiteOps — UI Design Mockups (v3 · two skins to compare)

Static HTML mockups for the proposed UI redesign. **No React codebase touched.** Tailwind via CDN, Iconify via CDN, Google Fonts via CDN — open the files directly in a browser.

## Two skins side-by-side

You can A/B these by opening the two landing pages and clicking "Sign In" on each:

| Skin | Landing | App shell | Vibe |
|---|---|---|---|
| **Red Noir** (v2) | `landing.html` | `app-mockup.html` | Black + crimson red, starfield, Atkinson Hyperlegible (low-vision optimized), Manrope display, sharp borders |
| **Glass + Emerald** (v3, NEW) | `landing-glass.html` | `app-mockup-glass.html` | Black + zinc + emerald, grain noise overlay, Inter, glassmorphic surfaces (blur 12-14px, 1px white/10 borders), 1.5rem-2.5rem corner rhythm, cubic-bezier motion |

Same content. Same 10 fully-built app pages. Same 19-column Tasks table. Same accessibility toolbar. Two completely different visual languages so Sid can pick which one feels right.

## What's new in the Glass + Emerald skin

Built against your design-system spec: glassmorphism, depth, monochrome palette, vibrant emerald (#34D399) highlights.

- **Palette**: #000 base · #18181B zinc accent · #F4F4F5 light · **#34D399 emerald** (replaces the red brand accent everywhere except semantic-danger states)
- **Typography**: Inter primary (font-weight 500-600 for headings, tracking -0.05em, line-height 1.05). Atkinson Hyperlegible kept loaded as the accessibility toggle for low-vision users
- **Glass surfaces**: `rgba(255,255,255,0.05)` background + `backdrop-filter: blur(14px)` + 1px white-10% border + 1.5rem corner radius. Hover state scales subtly + emerald glow shadow
- **Grain overlay**: SVG `feTurbulence` noise at 15% opacity with `mix-blend-mode: overlay` — kills color banding on the dark gradients and adds analog depth per spec
- **Background**: Soft zinc radial gradient with three layered emerald glow ovals (top-right, bottom-left, center) at 12% / 6% / 4% opacity, all blur-140px+
- **Animations**: All entry / hover transitions on `cubic-bezier(0.16, 1, 0.3, 1)` for the smoother glass feel
- **Sidebar nav active state**: 3px emerald left bar + 12px emerald glow (instead of red)
- **Logo**: Same stacked-floors mark but the foundation is now emerald (#34D399) — works at every size from sidebar (32px) to landing footer (40px)

### What stays red (intentionally — semantic danger)

Even in the all-emerald palette, the following keep their semantic red because they signal **danger / blocker / hard-stop**:

- Critical-path row tint in Tasks (`bg-red/5`)
- `+N` slip indicators when behind schedule
- Negative remaining float
- `.pill-status.pending` (blockers)
- Reject / delete icons
- "AI suggestion BLOCKED by rule" callout in Recovery
- Alert-triangle icons in Approvals queue

That's a usability call — if everything is emerald, the critical signals don't pop. The brand becomes emerald; danger stays red.

## Accessibility toolbar (both skins)

Floating bottom-right button or **Alt + A**. All toggles work and persist via `localStorage`:

| Feature | Effect |
|---|---|
| **Text size** A / A+ / A++ / A+++ | Scales body 100% → 200% (WCAG 2.2 max) |
| **Line spacing** 1× / 1.5× / 2× | Increases line-height for easier line tracking |
| **High contrast** | Pure black + pure white + brighter accent (~21:1) |
| **Sunlight mode** | Even more extreme — kills gradients, max contrast for outdoor screens |
| **Low-vision font** | Switches to **Atkinson Hyperlegible** (Braille Institute, designed for low-vision and dyslexia) |
| **Large click targets** | 48-52 px buttons for gloves / imprecise touch |
| **Reduce motion** | Disables parallax + animations |
| **Magnifier (hover-zoom)** | Hover any cell/heading → scales 1.25-1.35× with outline |
| **Reading guide** | Horizontal band follows cursor (helps track rows in 19-column Tasks) |
| **Read selection aloud** | Highlight text → click → browser SpeechSynthesis TTS |
| **Reset all** | Restore defaults |

In the **Glass version** the toolbar accent is emerald. In **Red Noir** it's red.

## All 10 fully-built app pages (identical content across skins)

1. Dashboard — 8-tile KPI band, CP cascade blockers, AI recovery suggestions
2. Projects — table with progress bars, contract numbers, status pills
3. Daily reports (list) — Upload DR Template button, status pills
4. Daily reports (detail) — 8-KPI band, auto-populated project header, weather, crew presence, activity grid, Internal/External/Client Format exports
5. **Tasks — full 19-column table** matching the actual React `Tasks.jsx` (Description with work-category badge, Resource, Predecessors with MS-Project shorthand, Trade, Plan start/finish, Impacted start/finish, Slip wd, Dur, %, Status pill, Blockers with d/r/s/w breakdown, CP/CP\*, Float, Rem float, Gap severity, Actions) plus Sort-by-Slip, Show-only-impacted, Export CSV, Recompute CPM, New activity controls
6. Look ahead — 3-week pull-plan card layout
7. Approvals — queue with Approve / Reject / Review actions
8. Recovery — AI mitigation 3-card A/B/C with the **"1 suggestion BLOCKED by rule DW-FL-001"** callout demonstrating Sid's proposed constructibility engine
9. AI Agents — claude-sonnet-4-5 registry with run counts, cost, status, including a "Constructibility Filter — PROPOSED · Q3 2026" entry
10. Settings — tenant profile, AI gating toggles, FAR-compliant SLA defaults

Other 20 sidebar entries (Photos, Delays, RFIs, Submittals, Crews, Productivity, etc.) route to a generic "module coming" page that demonstrates the shell.

## How to view + send to Sid

Double-click any of the four HTMLs. The two landing pages cross-link to their respective app shells via the "Sign In" button. The SiteOps logo in either app sidebar jumps back to that skin's landing.

The accessibility toolbar (red or emerald button bottom-right) is on every page.

Tell Sid:
1. Open `landing.html` (Red Noir) — that's the original
2. Open `landing-glass.html` (Glass + Emerald) — that's the new skin
3. Try `Alt + A` → toggle **Sunlight mode** + **Large click targets** on either to see the jobsite-tablet look
4. Try **Magnifier** then hover the Tasks page — pops cells up 1.35× with outline
5. Open both app mockups in two browser windows side-by-side to compare the data-dense pages

## Migration

Once a skin is approved, the React migration is a separate task — replace Tailwind classes per page, update shared `AppLayout` / `Sidebar` / `Topbar`, port the accessibility toolbar as an `<AccessibilityProvider>` context. Estimate: 4-6 weeks for full app.

## Files

```
landing.html              Red Noir landing
app-mockup.html           Red Noir app
landing-glass.html        Glass + Emerald landing  (NEW)
app-mockup-glass.html     Glass + Emerald app      (NEW)
README.md                 This file
```
