# Frontend Design Pass — "Calm Cartography" Hybrid

**Date:** 2026-05-03
**Status:** Design (awaiting user approval before implementation plan)
**Scope:** React SPA only (`frontend/`). No backend, no schema, no API changes.

## Goals

The current SPA is functional but visually flat and monotonous. Pages are visually interchangeable: top nav is plain text with a tab underline, every page sits on the same dark canvas with the same hairline-card treatment, and color appears in only three places (entity-type pills in DocumentDetail, status pills, and the Research page's amber CTA). The audit pass identified the boring spots: System Settings reads as a list of links with no visual hint, Folders feels half-empty, the Ask conversation list is a stack of identical-looking text rows, the Documents table blends into its own chrome.

This pass introduces categorical color, depth, and a serif title face, in service of three things:

1. **Navigability.** A glance at any page should tell you which area you're in without reading the label.
2. **Aesthetic identity.** The app should feel intentional, not template-default.
3. **Working color.** Color appears where it carries meaning (page area, document type, status, entity), not as decoration.

## Direction

**"Calm Cartography" hybrid** — option A's structure (per-page hue + icon + subtle depth) executed with option B's restraint (muted earth-tone palette, serif title face, hairline rules). Specifically:

- Each top-level area gets its own hue + icon. The hue appears on the active tab underline, the page title's accent bar, and a small icon next to the tab label — but **not** on the page background or the body content. Data still reads on neutral surfaces.
- Page titles render in a serif (New York, the macOS system serif, with Georgia fallback). Body, labels, and numerics stay on the system sans (SF Pro).
- Cards pick up a subtle gradient (5% top→bottom highlight in dark mode, faint top→bottom shadow in light mode) so they read as elevated surfaces instead of being indistinguishable from background.
- Document types get icons (📕 Novel, 📊 Spreadsheet, ✉ Email, 📄 PDF, 📜 Plain text, etc.). Status pills get glyphs (●, ⟳, ○).
- Existing entity-type colors and Observatory chart colors stay untouched — they're already working.

This direction is more surgical than a full visual overhaul. It introduces personality through tokens, not through a redesign of any specific screen.

## Design Tokens

### Per-area accent palette

Eight hues, one per top-level area. Dark-mode value listed first; light-mode is the same hue pushed ~20–25% darker and slightly more saturated.

| Area | Hue family | Dark | Light |
|---|---|---|---|
| Ask | terracotta | `#a8745a` | `#8a5a42` |
| Research | ochre | `#b88a4a` | `#966c2e` |
| Folders | warm khaki | `#8a8576` | `#6e6a5a` |
| Documents | dusty blue | `#5b8aa8` | `#3f6885` |
| Explore | mauve | `#8a6a8a` | `#6a4d6a` |
| Search | dusty teal | `#6a96a8` | `#4a7585` |
| Observatory | sage | `#7a9670` | `#5a7556` |
| System Settings | slate | `#7a7a82` | `#5a5a64` |

These are accent colors only — used on the tab indicator (`border-bottom`), the page title accent bar, the area's tab icon when active, focus rings within that area, and the area's "primary action" buttons (e.g. "+ New conversation" in Ask). They are **not** applied to page backgrounds, card surfaces, or body text.

Each accent ships with two derived tokens:

- `--area-accent-tint`: the accent at ~12% opacity, used as icon-tile background within that area.
- `--area-accent-text`: a lighter variant of the accent (dark mode) or the accent itself (light mode), used as icon-tile foreground.

CSS-vars naming convention: `--area-{name}-accent`, `--area-{name}-accent-tint`, `--area-{name}-accent-text`. The active area's vars get aliased to `--area-accent` (etc.) on the layout root via a per-route hook.

### Typography

- **Page title** (one per page, e.g. "Documents"): `font-family: 'New York', 'NewYork', ui-serif, Georgia, serif; font-weight: 600; font-size: 28-32px; letter-spacing: -0.02em.`
- **Section headings within page** (e.g. "Access & identity" on Settings): same serif, `font-weight: 600; font-size: 14-16px; letter-spacing: -0.01em`.
- **KPI numerals** (e.g. "3,150" in Observatory): same serif, `font-weight: 600; font-size: 24-28px; letter-spacing: -0.02em`. The serif gives them weight and stops them feeling float-y.
- **Everything else** (body, labels, table cells, button labels, captions): existing system sans stack — unchanged.

New York is shipped with macOS system-wide and is available without download in the WKWebView client. For the Docker SPA it is not present; the Georgia fallback handles that case acceptably (similar feel, slightly different forms). No webfont download is added — keeping the change CSS-only and zero-network.

### Card / surface elevation

Cards (everything currently using the `.rounded-xl bg-(--color-bg-primary) shadow-mac ring-1 ring-(--color-border)` pattern) gain a subtle gradient:

- **Dark mode:** `background: linear-gradient(180deg, rgba(255,255,255,0.025) 0%, transparent 100%);` over the existing `--color-bg-primary` surface. Border opacity bumps from `rgba(255,255,255,0.08)` → `rgba(255,255,255,0.10)` so the edge stays defined.
- **Light mode:** `background: linear-gradient(180deg, rgba(0,0,0,0.012) 0%, transparent 100%);` over the existing surface. Border `rgba(0,0,0,0.07)`.

Hover state: border opacity bumps another 4–6% on hover; no transform or shadow change (we want quiet feedback, not bouncing cards).

### Status pill conventions

Pills get a leading glyph **in addition to** the existing label, and the background opacity drops while the foreground hue gets crisper:

| State | Glyph | Hue family | Dark bg / fg | Light bg / fg |
|---|---|---|---|---|
| active | ● | sage | `rgba(122,150,112,0.18)` / `#a8c49a` | `rgba(74,108,73,0.10)` / `#4a6c49` |
| running / embedding | ⟳ | ochre | `rgba(184,138,74,0.18)` / `#d4a574` | `rgba(150,108,46,0.10)` / `#966c2e` |
| idle | ○ | slate | `rgba(122,122,130,0.18)` / `#a0a0a8` | `rgba(90,90,100,0.10)` / `#5a5a64` |
| error | ⚠ | rust | `rgba(168,90,90,0.20)` / `#d49a9a` | `rgba(168,90,90,0.10)` / `#a85a5a` |
| pending | ◐ | dusty blue | `rgba(91,138,168,0.18)` / `#8aafc4` | `rgba(63,104,133,0.10)` / `#3f6885` |

These replace the current single-color status pills; existing call sites (Folders status, Documents status, ingestion job badges) all migrate to the new pill component.

### Document-type icons

A new `documentTypeIcon(category)` helper maps document categories (already returned by the backend in `DocumentDetail.category` / file-extension fallback) to glyphs and to the matching icon-tile palette:

| Category | Glyph | Icon hue |
|---|---|---|
| Novel / book | 📕 | dusty blue (Documents area accent) |
| PDF | 📄 | terracotta |
| Spreadsheet | 📊 | sage |
| Email | ✉ | ochre |
| Plain text / notes | 📜 | slate |
| Code / source | 💻 | mauve |
| Image | 🖼 | dusty teal |
| Presentation | 🎞 | mauve |
| Audio / transcript | 🎙 | terracotta |
| HTML / web | 🌐 | mauve |
| Unknown / other | 📦 | slate |

The icon tile is a 28-30px rounded square with a 12% tint of the category hue and the hue itself as text color (or, in dark mode, a slightly lighter variant for legibility).

This icon appears on Documents-page rows and on the DocumentDetail header. It does not replace the document title — title still leads.

### Hairlines and dividers

The nav/body separator and section dividers go from "essentially invisible" to "visible but quiet":

- Dark mode: `1px solid rgba(255,255,255,0.06)` (was `rgba(255,255,255,0.08)` but applied inconsistently).
- Light mode: `1px solid rgba(0,0,0,0.06)`.

Settings page section labels get a serif heading + a hairline above each non-first group.

## Component-by-component changes

These are concrete UI changes mapped to existing components/files. They reuse the tokens above; nothing introduces a token not defined in the token section.

### Top navigation (`frontend/src/components/Layout.tsx` — `TabLink`)

- Each `TabLink` gets an icon prop (small emoji or single-glyph SVG, paired with the tab name it already renders).
- The active-tab underline color is bound to `--area-accent` (the area's hue). Inactive tabs keep the existing secondary-text color and have no underline.
- Hover on inactive tabs subtly tints the icon toward the area's accent.
- The nav strip itself gains a hairline bottom border (the dark/light-mode hairline above).

### Page title block (new shared component, e.g. `PageHeader`)

Used by every page that currently renders a plain `<h1>Documents</h1>`. Renders:

- The serif title at the page-title scale.
- A short accent bar (~48-56px wide × 3px tall) below the title in `--area-accent`.
- Optional subtitle line in secondary text.

This component replaces the ad-hoc `<h1 className="...">` patterns in `DocumentsPage`, `SearchPage`, `ResearchPage`, `FoldersPage`, `LanguagesPage`, `UsersPage`, `ApiKeysPage`, `ApiKeyDashboardPage`, `ModelsPage`, `SystemMaintenancePage`, `SystemStatusPage`, `RetrievalSettingsPage`, `RateLimitSettingsPage`, `ServiceLogsPage`, `StatsPage` (Observatory), `ExplorePage`, `SystemSettingsPage` (Settings hub), `IntegrationsPage`, `PreferencesPage`, `SetupPage`, and the chat sidebar header.

### Card / surface treatment

Every existing `.rounded-xl ... ring-1 ring-(--color-border) ... shadow-mac` site gets the subtle gradient applied. The simplest path: extend the existing utility/class so all cards pick it up centrally rather than per-site. Concrete options to evaluate during implementation:

- Tailwind v4 component class via `@layer components { .surface-card { ... } }` in `index.css`, then sweep replace the existing pattern.
- Or a small `<Card>` wrapper component the existing call sites adopt.

Either is fine; the implementation plan will pick one. The token values must match the table above.

### Status pills

A single `<StatusPill state="active|running|idle|error|pending">` component replaces the ad-hoc inline pill markup currently scattered across `FoldersPage`, `DocumentsPage`, `DocumentDetailPage` ingestion jobs section, the queue tray, and `StatsPage`. The component handles glyph + color tokens.

### Per-page changes (mapped to files)

**Ask** (`frontend/src/pages/HomePage.tsx`, `ChatPage.tsx`, sidebar component):
- Sidebar "+ New conversation" button gains the area accent (terracotta tinted bg + accent border + accent text).
- Each conversation row gets a colored topic dot. **v1 derivation:** deterministic hash of `conversation_id` mapped onto the eight area-accent hues (terracotta / ochre / khaki / dusty blue / mauve / dusty teal / sage / slate). Stable per conversation, harmonizes with the page palette, ships now. Smarter "color by detected topic / top entity" is a deferred enhancement (see Out of scope).
- Hero/empty state: a 64px rounded-square tile in the area's accent tint with a single emoji (📚), serif "Ask your documents" headline, sans subtitle, suggestion chips. Replaces the current monochrome icon and plain h2.
- Suggestion chips on hover: tint into the area accent.

**Research** (`frontend/src/pages/ResearchPage.tsx`):
- Octopus illustration stays. Iconic, has personality.
- Page title block uses the ochre area accent.
- "Start Research" CTA stays the loud orange/amber (it is the canonical primary action of this page; the area accent rhymes with it).
- Sidebar "+ New Research" button picks up the ochre area treatment.
- Conversation rows in sidebar get topic dots (same scheme as Ask).

**Folders** (`frontend/src/pages/FoldersPage.tsx`):
- Page title block + warm khaki accent bar.
- "Add Folder" button: filled-but-quieter — khaki accent tint + accent text + accent border (instead of the current iOS-blue filled button), so the area identity shows up.
- Folder rows in the table get the gradient card treatment row-by-row (each row is its own elevated surface with row spacing — replaces the current near-borderless table).
- Each row leads with a small folder icon tile in the area's accent.
- Status pill migrated to the new `StatusPill`.
- Empty state: when zero folders, a centered hero block (similar shape to the Ask hero) with the "Add Folder" CTA.

**Documents** (`frontend/src/pages/DocumentsPage.tsx`):
- Page title block + dusty-blue accent bar.
- Filter row (filename / language / category / folder dropdowns) keeps its existing layout but the active sort indicator picks up the accent color.
- Each row in the list: icon tile (document-type) → title + meta → status pill → date. The expanded-row metadata inherits the gradient card treatment so it visually nests inside the row instead of bleeding into table chrome.
- "Continue viewing" link at the top picks up the area accent.

**Explore** (`frontend/src/pages/ExplorePage.tsx`):
- Page title block + mauve accent bar.
- The People / Places / Organizations section pills tint into their entity-type colors (PERSON = blue, GPE = amber/orange, ORG = green, etc.) — using the existing `ENTITY_COLORS` palette from `components/stats/CorpusCharts`. Pills currently render gray; they switch to a ~12% tint of the entity-type color with the hue as text. This is the highest-leverage spot for entity colors outside DocumentDetail.
- Topic Cluster cards keep their existing topic-color dot but the card body gets the gradient card treatment.

**Search** (`frontend/src/pages/SearchPage.tsx`):
- Page title block + dusty teal accent bar.
- Search input focus ring uses the area accent.
- "Search" button picks up the area's primary-action treatment (tint bg + accent text + accent border).
- Recent-search history rows: consistent row spacing + hover treatment matching the row patterns elsewhere.

**Observatory** (`frontend/src/pages/StatsPage.tsx`):
- Page title block + sage accent bar.
- KPI cards' large numerals re-rendered in the serif at the KPI scale. Optional `--kpi-trend` pill can show delta (out of scope for this pass; see "deferred" section).
- Charts unchanged in palette — the existing entity colors, the topic treemap colors, the network-graph colors all stay. Pulse dot in chart card titles uses the sage area accent.
- Sub-tabs ("Corpus Statistics" / "Processing Pipeline") get the area-accent active indicator.

**System Settings hub** (`frontend/src/pages/SystemSettingsPage.tsx`):
- Page title block + slate accent bar.
- The flat list of links is **grouped into four labeled sections**: "Access & identity" (Users, API Keys), "Models & languages" (Models, Languages), "Behavior & limits" (Retrieval, Rate Limits), "Operations" (System Status, Service Logs, System Maintenance). Section labels use the serif at section-heading scale.
- Each item becomes a card-row with a colored icon tile (drawn from the per-area accents — e.g. Models gets sage, Languages gets mauve), the existing title + subtitle, and the chevron.
- Two-column grid on wider widths; single-column on narrow.

**DocumentDetail** (`frontend/src/pages/DocumentDetailPage.tsx`):
- Page title block + dusty-blue accent bar (it's a Documents-area page).
- Document title row gets the document-type icon tile (left of the title).
- Existing entity-type colored pills stay (they were the original "color" win on this page — the `ENTITY_TYPE_COLORS` map continues to source the entity badges).
- Ingestion-jobs collapsible section: each job row picks up the new `StatusPill` and the gradient card treatment.
- "Reveal in Finder" / "Download" / "Reprocess" / "Delete" actions: keep current colors (the destructive Delete stays red, etc.), just gain consistent button heights and the card-gradient treatment on the action surface.

**Languages** (`frontend/src/pages/LanguagesPage.tsx`):
- Page title block + slate accent bar (it's a Settings-area subpage).
- Language rows get the gradient card treatment, install-state pill (`StatusPill`), per-row install button uses the slate accent.

**Users / API Keys / Models / Retrieval / Rate Limits / System Status / Service Logs / System Maintenance**: all Settings-area subpages get the slate accent + serif title block + grouped/card treatment matching the hub.

**Login** (`frontend/src/pages/LoginPage.tsx`):
- Page title block (no area accent — pre-auth, no nav). Just the serif title.
- Card gradient applied to the login surface.

**Onboarding wizard** (`frontend/src/components/OnboardingWizard.tsx`):
- Page indicators / step dots use the active step's area accent if the step targets an area (Languages step → slate); otherwise neutral.
- "Install N & continue" CTA uses the slate accent (it's a Settings-area action).
- Otherwise unchanged structurally.

## What stays the same

These are working today and must not change in this pass:

- The `ENTITY_COLORS` palette in `components/stats/CorpusCharts.tsx` and its usage on DocumentDetail / Top Entities / Entity Network / Topic Distribution / Documents-by-Topic charts. Entity-type semantic colors are correct as-is.
- The Observatory's Topic Distribution treemap colors. The treemap is the most visually rich element on the site and works well.
- The Entity Network force-graph layout and node colors. (PR #226 work.)
- The Research page's octopus illustration.
- The Documents page's "Continue viewing" affordance and its existing filter-row layout.
- All current backend behavior, schemas, API surfaces, MCP tools, ingestion pipeline, etc. This is a CSS/JSX change.
- All existing tests must continue to pass. Any new test coverage is additive.

## Out of scope / deferred (capture in `pr_followups.md`)

These came up during brainstorming but aren't in this pass — flag for follow-up:

1. **Motion / micro-interactions** beyond the existing 0.15s color transitions. No hover-lift animations, no animated chart entries, no page transitions. Worth a follow-up pass once the static design lands.
2. **KPI trend deltas** (the "+47 today" line under each Observatory KPI in the mock). Requires backend support for time-windowed counts; out of scope for a CSS pass.
3. **Topic-aware conversation dot color.** v1 ships with a hash-of-id mapping (above). A smarter "color by the conversation's dominant topic or top-cited entity" requires either a backend join (conversations → top entities/topic) or client-side derivation from message contents. Real follow-up once the visual is in place and we know whether the hash version reads "random" or "intentional".
4. **Light-mode user toggle** in the menubar / preferences (if not already present). Currently the SPA respects `prefers-color-scheme`; an explicit override is a separate ticket.
5. **Webfont fallback for non-macOS clients** (the Docker-served SPA). Georgia fallback is acceptable for v1; a Source Serif Pro or similar webfont download is a follow-up.
6. **Login page brand mark / illustration.** The login page is currently very plain — the gradient + serif treatment helps but doesn't add identity. Post-pass consideration.
7. **Per-page background atmosphere** (option C from brainstorming). Explicitly not in this pass; revisit if the muted-accent approach feels too subtle in practice.
8. **Tailwind v4 design-token migration audit.** This pass adds new CSS vars; a follow-up could move all color tokens onto the `@theme` block uniformly. Out of scope here.

## Files affected (working list — implementation plan refines)

Token / global:
- `frontend/src/index.css` — add `--area-{name}-accent[-tint|-text]` vars (8 areas × 3 = 24 dark vars + 24 light vars), card-gradient utility, hairline utility, status-pill base, document-type-icon helper map, font-family stack with New York.
- `frontend/src/auth.tsx` or a new `frontend/src/hooks/useAreaAccent.ts` — per-route hook that aliases the active area's vars to `--area-accent` etc. on the layout root.

New shared components:
- `frontend/src/components/PageHeader.tsx` — serif title + accent bar.
- `frontend/src/components/StatusPill.tsx` — glyph + label + state-tinted bg.
- `frontend/src/components/IconTile.tsx` — 28-30px rounded-square tinted tile, optional hue override.
- `frontend/src/components/Card.tsx` (or layer-class equivalent) — gradient surface wrapper.
- `frontend/src/utils/documentTypeIcon.ts` — category → glyph + hue.

Modified:
- `frontend/src/components/Layout.tsx` — `TabLink` gains icon, active underline binds to `--area-accent`, nav hairline.
- `frontend/src/pages/HomePage.tsx`, `ChatPage.tsx`, sidebar — Ask treatment, hero, conversation dots.
- `frontend/src/pages/ResearchPage.tsx` — area accent, sidebar treatment.
- `frontend/src/pages/FoldersPage.tsx` — accent, card-rows, status-pill, empty state.
- `frontend/src/pages/DocumentsPage.tsx` — accent, icon-tile per row, status-pill.
- `frontend/src/pages/DocumentDetailPage.tsx` — accent, icon-tile, status-pill on jobs.
- `frontend/src/pages/SearchPage.tsx` — accent, focus ring, button.
- `frontend/src/pages/ExplorePage.tsx` — accent, entity-type tinted pills.
- `frontend/src/pages/StatsPage.tsx` (Observatory) — accent, serif KPI numerals.
- `frontend/src/pages/SystemSettingsPage.tsx` (Settings hub) — accent, grouped sections, icon tiles.
- All Settings subpages (`UsersPage`, `ApiKeysPage`, `ApiKeyDashboardPage`, `ModelsPage`, `LanguagesPage`, `RetrievalSettingsPage`, `RateLimitSettingsPage`, `SystemStatusPage`, `ServiceLogsPage`, `SystemMaintenancePage`, `IntegrationsPage`, `PreferencesPage`) — slate accent, page header.
- `frontend/src/pages/LoginPage.tsx` — page header, card gradient.
- `frontend/src/components/OnboardingWizard.tsx` — slate accent on Languages step.

The exact list of components to extract vs. inline is decided in the implementation plan.

## Testing

**Frontend type-check + lint + prettier** must pass: `npm run type-check && npm run lint && npm run format:check`. Existing CI gates apply.

**Existing tests** continue to pass: Python `pytest`, all backend tests untouched (this is a CSS-only change).

**No new automated tests are required** — the project doesn't currently have a JS unit-test setup, and adding one is out of scope for a styling pass.

**Manual smoke checklist** (the implementation plan's last task includes walking through this):
- Log in, walk every top-level page, confirm:
  - Active tab underline is the area's hue, in both modes.
  - Page title is serif with a short accent bar.
  - Card rows / surfaces show the subtle top-down gradient.
  - At least one status pill is visible per state (active / running / idle / error / pending) and renders correctly in both modes.
- DocumentDetail still shows the existing entity-type colored badges (regression check on the work that already shipped).
- Observatory charts (Top Entities bars, Topic Distribution treemap, Entity Network) still render in their existing colors (regression check).
- Switch system theme (System Settings → Appearance → Light/Dark) and re-walk one or two pages to confirm light-mode tokens kick in.
- Onboarding wizard renders with slate accent on the Languages step.

## Risk / rollout

- **Single-PR feasibility:** the change is large in surface (every page touched) but small in mechanism (tokens + a few shared components + sweep replacements). The implementation plan should batch by component family (tokens first → shared components → page sweeps in order of complexity), so each commit is a meaningful unit and CI can run between commits.
- **Visual regression risk:** without a screenshot test setup, regressions are caught manually. The smoke checklist above is the gate.
- **Rollout:** ships as a single PR. No feature flag, no progressive rollout — it's CSS, and there's only one deployment surface (the bundled SPA in the Mac native app + Docker SPA).
- **Rollback:** revert the PR. No data, no schema, no API surface affected.

## Companion mockup files (for reference during implementation)

The brainstorming visual companion files live in `.superpowers/brainstorm/` (gitignored — already in `.gitignore` per existing brainstorming skill). They show the agreed-on direction for Documents, Ask, Observatory, Settings (in both dark and light modes) and can be opened in any browser via the brainstorming server while implementing. Reference, not source of truth — the tables in this spec are the source of truth.
