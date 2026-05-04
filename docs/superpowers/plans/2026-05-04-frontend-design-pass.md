# Frontend Design Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the "Calm Cartography" hybrid design pass (per-area accent palette + serif title face + subtle depth + document-type icons) across every route in the React SPA, in dark + light mode.

**Architecture:** All changes are CSS + JSX inside `frontend/`. New design tokens go to `index.css`; a small `useAreaAccent` hook binds the active route's hue to `--area-accent` on the layout root; six new shared components (`PageHeader`, `StatusPill`, `IconTile`, `Card`, plus a `documentTypeIcon` util and a `topicDotColor` util) carry the new conventions; every page imports those components instead of repeating the patterns inline.

**Tech Stack:** React 19, React Router 7, Tailwind CSS 4 (CSS-first config in `frontend/src/index.css`), Vite 7.3, TypeScript 5, ESLint 10. No new dependencies. No JS unit-test setup (project doesn't have one — we verify with type-check + lint + format + manual smoke).

**Spec:** [`docs/superpowers/specs/2026-05-03-frontend-design-pass-design.md`](../specs/2026-05-03-frontend-design-pass-design.md)

---

## File map

**Create:**
- `frontend/src/components/PageHeader.tsx` — serif title + accent bar + optional subtitle
- `frontend/src/components/StatusPill.tsx` — glyph + label, state-tinted bg
- `frontend/src/components/IconTile.tsx` — 28-30px rounded-square tinted tile, hue prop
- `frontend/src/components/Card.tsx` — gradient surface wrapper (thin React component)
- `frontend/src/utils/documentTypeIcon.ts` — `(doc: { doc_type, mime_type, canonical_filename }) => { glyph, hue }`
- `frontend/src/utils/topicDotColor.ts` — deterministic-hash-of-string → one of eight area-accent hues
- `frontend/src/hooks/useAreaAccent.ts` — reads route, returns `{ areaName, accentVar }`, optionally applies to root

**Modify (tokens / global):**
- `frontend/src/index.css` — add `--area-{name}-accent[-tint|-text]` vars (8 areas × 3 = 24 dark + 24 light), font-family stack with New York, hairline tokens (`--color-border` already exists; bump consistency)

**Modify (top nav):**
- `frontend/src/components/Layout.tsx` — `TabLink` gets icon prop; active tab uses `--area-accent` instead of hard-coded blue; layout root gets `data-layout-root` + the `useAreaAccent` hook

**Modify (per-page sweeps — see Tasks 9-19):**
- `SystemSettingsPage` (hub), 12 Settings subpages, `DocumentsPage`, `DocumentDetailPage`, `FoldersPage`, `ExplorePage`, `SearchPage`, `StatsPage`, `ResearchPage`, `ChatPage` + `HomePage` + chat sidebar, `LoginPage`, `OnboardingWizard`

---

## Task 1: Token foundation in `index.css`

**Files:**
- Modify: `frontend/src/index.css`

- [ ] **Step 1: Read the current token block to see what's already there**

```bash
sed -n '1,90p' frontend/src/index.css
```

Expected: existing `@theme` block, light + `.dark` variable definitions, scrollbar styles. Note where the dark-mode variables end (around line 56 in the audit snapshot — re-verify before editing).

- [ ] **Step 2: Append the per-area accent tokens after the existing `.dark` block**

Add this block after the closing `}` of the `.dark { ... }` selector (and before the `*` selector for font smoothing). Both light-mode (root) and dark-mode (under `.dark`) variants are required.

```css
/* ---- Per-area accent tokens (light mode = :root) ---- */

:root {
  /* Ask — terracotta (light) */
  --area-ask-accent: #8a5a42;
  --area-ask-accent-tint: rgba(138, 90, 66, 0.10);
  --area-ask-accent-text: #8a5a42;

  /* Research — ochre (light) */
  --area-research-accent: #966c2e;
  --area-research-accent-tint: rgba(150, 108, 46, 0.10);
  --area-research-accent-text: #966c2e;

  /* Folders — warm khaki (light) */
  --area-folders-accent: #6e6a5a;
  --area-folders-accent-tint: rgba(110, 106, 90, 0.10);
  --area-folders-accent-text: #6e6a5a;

  /* Docs — dusty blue (light) */
  --area-docs-accent: #3f6885;
  --area-docs-accent-tint: rgba(63, 104, 133, 0.10);
  --area-docs-accent-text: #3f6885;

  /* Explore — mauve (light) */
  --area-explore-accent: #6a4d6a;
  --area-explore-accent-tint: rgba(106, 77, 106, 0.10);
  --area-explore-accent-text: #6a4d6a;

  /* Search — dusty teal (light) */
  --area-search-accent: #4a7585;
  --area-search-accent-tint: rgba(74, 117, 133, 0.10);
  --area-search-accent-text: #4a7585;

  /* Observatory — sage (light) */
  --area-observatory-accent: #5a7556;
  --area-observatory-accent-tint: rgba(90, 117, 86, 0.10);
  --area-observatory-accent-text: #5a7556;

  /* Settings — slate (light) */
  --area-settings-accent: #5a5a64;
  --area-settings-accent-tint: rgba(90, 90, 100, 0.10);
  --area-settings-accent-text: #5a5a64;

  /* Default — bound by useAreaAccent on the layout root.
     Pre-auth pages (login/setup) get the docs accent as a neutral fallback. */
  --area-accent: var(--area-docs-accent);
  --area-accent-tint: var(--area-docs-accent-tint);
  --area-accent-text: var(--area-docs-accent-text);
}

.dark {
  /* Ask — terracotta (dark) */
  --area-ask-accent: #a8745a;
  --area-ask-accent-tint: rgba(168, 116, 90, 0.12);
  --area-ask-accent-text: #d49d80;

  /* Research — ochre (dark) */
  --area-research-accent: #b88a4a;
  --area-research-accent-tint: rgba(184, 138, 74, 0.12);
  --area-research-accent-text: #d4a574;

  /* Folders — warm khaki (dark) */
  --area-folders-accent: #8a8576;
  --area-folders-accent-tint: rgba(138, 133, 118, 0.12);
  --area-folders-accent-text: #b0aa9a;

  /* Docs — dusty blue (dark) */
  --area-docs-accent: #5b8aa8;
  --area-docs-accent-tint: rgba(91, 138, 168, 0.15);
  --area-docs-accent-text: #8aafc4;

  /* Explore — mauve (dark) */
  --area-explore-accent: #8a6a8a;
  --area-explore-accent-tint: rgba(138, 106, 138, 0.15);
  --area-explore-accent-text: #b890b8;

  /* Search — dusty teal (dark) */
  --area-search-accent: #6a96a8;
  --area-search-accent-tint: rgba(106, 150, 168, 0.15);
  --area-search-accent-text: #90b8c8;

  /* Observatory — sage (dark) */
  --area-observatory-accent: #7a9670;
  --area-observatory-accent-tint: rgba(122, 150, 112, 0.15);
  --area-observatory-accent-text: #a8c49a;

  /* Settings — slate (dark) */
  --area-settings-accent: #7a7a82;
  --area-settings-accent-tint: rgba(122, 122, 130, 0.15);
  --area-settings-accent-text: #a0a0a8;

  /* Default — same fallback chain as light */
  --area-accent: var(--area-docs-accent);
  --area-accent-tint: var(--area-docs-accent-tint);
  --area-accent-text: var(--area-docs-accent-text);
}
```

- [ ] **Step 3: Add the serif font stack to the `@theme` block**

Find the existing `--font-sans` declaration in `@theme { ... }` and add the serif stack alongside:

```css
@theme {
  --font-sans: -apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, sans-serif;
  --font-serif: 'New York', 'NewYork', ui-serif, Georgia, serif;
  /* (existing --shadow-mac and --shadow-mac-lg lines stay) */
}
```

This makes `font-serif` available as a Tailwind utility (`font-serif`) per Tailwind v4's `@theme` convention.

- [ ] **Step 4: Add the surface-card layer-class for the gradient surface treatment**

After the design-tokens block (after the `.dark { ... }` selector, can sit alongside the area tokens), add:

```css
@layer components {
  .surface-card {
    background-color: var(--color-bg-primary);
    background-image: linear-gradient(180deg, rgba(0, 0, 0, 0.012) 0%, transparent 100%);
    border: 1px solid var(--color-border);
    border-radius: 0.625rem; /* matches existing rounded-[10px] elsewhere */
    transition: border-color 150ms ease;
  }

  .dark .surface-card {
    background-image: linear-gradient(180deg, rgba(255, 255, 255, 0.025) 0%, transparent 100%);
  }

  .surface-card:hover {
    border-color: rgba(0, 0, 0, 0.14);
  }

  .dark .surface-card:hover {
    border-color: rgba(255, 255, 255, 0.18);
  }
}
```

- [ ] **Step 5: Verify build still works**

Run:

```bash
cd frontend && npm run build 2>&1 | tail -20
```

Expected: build succeeds. If it fails, the most likely cause is a CSS syntax error in the appended block — fix and re-run.

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/index.css
git commit -m "$(cat <<'EOF'
feat(frontend): add per-area accent tokens + serif stack + surface-card layer

Eight per-area accent triples (--area-{name}-accent[-tint|-text]) for
both light and dark modes — the foundation for the design pass. Adds
--font-serif (New York, falls back to Georgia). Adds .surface-card
component class with the subtle top→bottom gradient (highlight in dark,
shadow in light). Default --area-accent fallback points at docs (dusty
blue) for pre-auth pages; useAreaAccent hook (next task) overrides it
per-route on the layout root.

No JSX changes yet; tokens unused. Follow-up tasks pick them up.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `useAreaAccent` hook + layout-root binding

**Files:**
- Create: `frontend/src/hooks/useAreaAccent.ts`
- Modify: `frontend/src/components/Layout.tsx`

- [ ] **Step 1: Create the hook**

Write `frontend/src/hooks/useAreaAccent.ts`:

```typescript
import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'

/** Single source of truth for the eight area names used throughout the design system. */
export type AreaHue =
  | 'ask'
  | 'research'
  | 'folders'
  | 'docs'
  | 'explore'
  | 'search'
  | 'observatory'
  | 'settings'

const ROUTE_TO_AREA: { test: (path: string) => boolean; area: AreaHue }[] = [
  // Order matters — most specific first.
  { test: (p) => p.startsWith('/research'), area: 'research' },
  { test: (p) => p.startsWith('/folders'), area: 'folders' },
  { test: (p) => p.startsWith('/docs'), area: 'docs' },
  { test: (p) => p.startsWith('/explore'), area: 'explore' },
  { test: (p) => p.startsWith('/search'), area: 'search' },
  { test: (p) => p.startsWith('/stats'), area: 'observatory' },
  { test: (p) => p.startsWith('/admin') || p.startsWith('/integrations') || p.startsWith('/preferences'), area: 'settings' },
  // Ask is the default — matches '/' and '/c/:conversationId'.
  { test: () => true, area: 'ask' },
]

/**
 * Returns the active area name for the current route.
 * Side effect: applies the area's accent vars to the layout root element
 * (the element with `data-layout-root`), which makes `--area-accent`,
 * `--area-accent-tint`, `--area-accent-text` resolve to the right hue.
 */
export function useAreaAccent(): { area: AreaHue } {
  const location = useLocation()
  const area = ROUTE_TO_AREA.find((m) => m.test(location.pathname))!.area

  useEffect(() => {
    const root = document.querySelector<HTMLElement>('[data-layout-root]')
    if (!root) return
    root.style.setProperty('--area-accent', `var(--area-${area}-accent)`)
    root.style.setProperty('--area-accent-tint', `var(--area-${area}-accent-tint)`)
    root.style.setProperty('--area-accent-text', `var(--area-${area}-accent-text)`)
  }, [area])

  return { area }
}
```

- [ ] **Step 2: Wire the hook into Layout.tsx**

Open `frontend/src/components/Layout.tsx`. Find the outermost layout container (the top-level wrapper that everything else is inside — typically a `<div>` near the start of the JSX returned by `Layout`). Add `data-layout-root` to it.

The exact diff depends on the existing Layout structure. Verify by reading lines 70-110 of `Layout.tsx`. Most likely candidate: the `<div className="min-h-screen ...">` wrapper. Add:

```tsx
<div data-layout-root className="min-h-screen ...">
```

Then add the hook import + call near the other hooks at the top of the `Layout` function:

```tsx
import { useAreaAccent } from '../hooks/useAreaAccent'
// ...
function Layout() {
  // (existing hooks: useAuth, useState, etc.)
  useAreaAccent()
  // (rest of body)
}
```

- [ ] **Step 3: Type-check + lint**

```bash
cd frontend && npm run type-check && npm run lint
```

Expected: both pass.

- [ ] **Step 4: Verify in HarborClerk**

Open the HarborClerk client app (or rebuild + open if changes need a fresh bundle):

```bash
cd frontend && npm run build
```

Then open HarborClerk, log in, navigate to `/docs`. Open DevTools (right-click → Inspect — works inside WKWebView in dev mode). On the layout-root element, confirm the inline style includes `--area-accent: var(--area-docs-accent)`. Navigate to `/admin` and re-check — should switch to `--area-settings-accent`.

If DevTools isn't accessible, skip — Task 3 onward will surface visible regressions if the binding is wrong.

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/hooks/useAreaAccent.ts frontend/src/components/Layout.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): useAreaAccent hook + bind active area on layout root

Hook reads location.pathname, maps it to one of eight area names
(ask / research / folders / docs / explore / search / observatory /
settings), and applies that area's accent vars to the layout root via
data-layout-root selector + element.style.setProperty.

After this commit --area-accent etc. resolve to the active area's hue
on every page. Components in subsequent tasks consume those vars.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `PageHeader` shared component

**Files:**
- Create: `frontend/src/components/PageHeader.tsx`

- [ ] **Step 1: Create the component**

Write `frontend/src/components/PageHeader.tsx`:

```tsx
import { ReactNode } from 'react'

interface PageHeaderProps {
  title: string
  subtitle?: ReactNode
  /** Optional content rendered to the right of the title (action button, search input, etc.). */
  actions?: ReactNode
}

/**
 * Standard page-title block: serif title + short accent bar tinted with
 * the active area's hue + optional subtitle line + optional right-aligned
 * actions slot.
 *
 * The accent bar uses var(--area-accent) which is bound by the
 * useAreaAccent hook on the layout root — so every page that renders
 * inside Layout automatically gets the right color.
 */
export function PageHeader({ title, subtitle, actions }: PageHeaderProps) {
  return (
    <div className="mb-4 flex items-start justify-between gap-4">
      <div>
        <h1 className="font-serif text-3xl font-semibold tracking-tight text-(--color-text-primary)">
          {title}
        </h1>
        <div
          className="mt-1.5 h-[3px] w-12 rounded-sm"
          style={{ background: 'var(--area-accent)' }}
        />
        {subtitle && (
          <p className="mt-2 text-sm text-(--color-text-secondary)">{subtitle}</p>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  )
}

export default PageHeader
```

- [ ] **Step 2: Type-check + lint**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check
```

Expected: all pass. If `format:check` fails, run `npx prettier --write frontend/src/components/PageHeader.tsx` to auto-fix.

- [ ] **Step 3: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/components/PageHeader.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): add PageHeader shared component (serif title + accent bar)

Renders the standard page-title block: serif title (font-serif, the
New York stack), short 48px×3px accent bar in the active area's hue
(via --area-accent), optional subtitle, optional right-aligned actions
slot. Used by every page in the next tasks; replaces the ad-hoc
<h1 className="text-2xl font-bold mb-4">…</h1> patterns sprinkled
across the page files.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `StatusPill` shared component

**Files:**
- Create: `frontend/src/components/StatusPill.tsx`

- [ ] **Step 1: Create the component**

Write `frontend/src/components/StatusPill.tsx`:

```tsx
export type PillState = 'active' | 'running' | 'idle' | 'error' | 'pending'

interface StatusPillProps {
  state: PillState
  /** Override the default label for the state (e.g. "embedding" instead of "running"). */
  label?: string
  /** Override the default glyph for the state. */
  glyph?: string
}

const STATE_CONFIG: Record<PillState, { glyph: string; label: string; cls: string }> = {
  active: {
    glyph: '●',
    label: 'active',
    // Sage tints — light mode bg/fg first, dark mode after the colon.
    cls: 'bg-[rgba(74,108,73,0.10)] text-[#4a6c49] dark:bg-[rgba(122,150,112,0.18)] dark:text-[#a8c49a]',
  },
  running: {
    glyph: '⟳',
    label: 'running',
    cls: 'bg-[rgba(150,108,46,0.10)] text-[#966c2e] dark:bg-[rgba(184,138,74,0.18)] dark:text-[#d4a574]',
  },
  idle: {
    glyph: '○',
    label: 'idle',
    cls: 'bg-[rgba(90,90,100,0.10)] text-[#5a5a64] dark:bg-[rgba(122,122,130,0.18)] dark:text-[#a0a0a8]',
  },
  error: {
    glyph: '⚠',
    label: 'error',
    cls: 'bg-[rgba(168,90,90,0.10)] text-[#a85a5a] dark:bg-[rgba(168,90,90,0.20)] dark:text-[#d49a9a]',
  },
  pending: {
    glyph: '◐',
    label: 'pending',
    cls: 'bg-[rgba(63,104,133,0.10)] text-[#3f6885] dark:bg-[rgba(91,138,168,0.18)] dark:text-[#8aafc4]',
  },
}

/**
 * Status pill — leading glyph + label, with state-tinted background and
 * matching foreground color in both light and dark modes.
 */
export function StatusPill({ state, label, glyph }: StatusPillProps) {
  const cfg = STATE_CONFIG[state]
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold ${cfg.cls}`}
    >
      <span aria-hidden>{glyph ?? cfg.glyph}</span>
      <span>{label ?? cfg.label}</span>
    </span>
  )
}

export default StatusPill
```

- [ ] **Step 2: Type-check + lint + format**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/components/StatusPill.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): add StatusPill component (5 states with glyph + tint)

Single component for status indicators across the app. Five states —
active (● sage), running (⟳ ochre), idle (○ slate), error (⚠ rust),
pending (◐ dusty blue) — each with glyph, label, and matched bg+fg
hues for both light and dark modes. Optional label/glyph props for
state aliases like "embedding" instead of "running".

Replaces the ad-hoc inline pill markup currently scattered across
FoldersPage, DocumentsPage, DocumentDetailPage, the queue tray, and
StatsPage. Sweep happens in subsequent tasks.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `IconTile` shared component

**Files:**
- Create: `frontend/src/components/IconTile.tsx`

- [ ] **Step 1: Create the component**

Write `frontend/src/components/IconTile.tsx`:

```tsx
import { ReactNode } from 'react'
import type { AreaHue } from '../hooks/useAreaAccent'

interface IconTileProps {
  /** Area name whose accent variables drive the tint. Falls back to the active area when omitted. */
  hue?: AreaHue
  /** Glyph or small element rendered inside (e.g. "📕" or "🔑"). */
  children: ReactNode
  /** Tile size in pixels. Default 30. */
  size?: number
}

/**
 * Small rounded-square colored tile used as a category/type indicator
 * (document type next to a doc title, settings-section icon, etc.).
 *
 * Uses --area-{hue}-accent-tint as the background and --area-{hue}-accent-text
 * as the foreground so dark/light variants come along for free. When hue
 * is omitted the active area's --area-accent-tint / --area-accent-text are
 * used instead (so the tile matches the page it lives on).
 */
export function IconTile({ hue, children, size = 30 }: IconTileProps) {
  const style = hue
    ? {
        backgroundColor: `var(--area-${hue}-accent-tint)`,
        color: `var(--area-${hue}-accent-text)`,
        width: size,
        height: size,
        fontSize: Math.round(size * 0.47),
      }
    : {
        backgroundColor: 'var(--area-accent-tint)',
        color: 'var(--area-accent-text)',
        width: size,
        height: size,
        fontSize: Math.round(size * 0.47),
      }

  return (
    <div
      className="flex shrink-0 items-center justify-center rounded-[7px]"
      style={style}
      aria-hidden
    >
      {children}
    </div>
  )
}

export default IconTile
```

- [ ] **Step 2: Type-check + lint + format**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/components/IconTile.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): add IconTile component (rounded tinted square for icons)

Renders a 28-30px (default 30) rounded square with a 12-15% tint of the
chosen area's accent and the accent itself as foreground. Defaults to
the active area when no hue is passed (uses --area-accent-tint /
--area-accent-text). Used by document-type rows, settings sections,
and category indicators in subsequent tasks.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `Card` shared component

**Files:**
- Create: `frontend/src/components/Card.tsx`

- [ ] **Step 1: Create the component**

Write `frontend/src/components/Card.tsx`:

```tsx
import { HTMLAttributes, forwardRef } from 'react'

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** Render as a different element type. Default 'div'. */
  as?: 'div' | 'section' | 'article' | 'a'
}

/**
 * Surface card with the standard gradient + hairline border treatment.
 * Wraps the .surface-card layer-class defined in index.css. Forwarded ref
 * for callers that need it (e.g. focus management).
 *
 * Usage: <Card className="p-4">…</Card>
 *
 * The .surface-card class supplies bg, gradient, border, and hover state.
 * Padding, layout, and per-instance overrides come from className as usual.
 */
export const Card = forwardRef<HTMLDivElement, CardProps>(function Card(
  { className = '', children, as: _as = 'div', ...rest },
  ref,
) {
  return (
    <div ref={ref} className={`surface-card ${className}`} {...rest}>
      {children}
    </div>
  )
})

export default Card
```

(The `as` prop is in the interface but unused for now — `'div'` only. Future task can extend if anchors/sections become useful.)

- [ ] **Step 2: Type-check + lint + format**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/components/Card.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): add Card component wrapping the .surface-card layer-class

Tiny React wrapper around the .surface-card class so call sites get
ref forwarding + an as-prop seam without sweeping every existing
.rounded-xl shadow-mac ring-1 ring-border block. Used heavily in the
per-page sweeps to follow.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: `documentTypeIcon` utility

**Files:**
- Create: `frontend/src/utils/documentTypeIcon.ts`

- [ ] **Step 1: Create the utility**

Write `frontend/src/utils/documentTypeIcon.ts`:

```typescript
import type { AreaHue } from '../hooks/useAreaAccent'

export interface DocLike {
  doc_type?: string | null
  mime_type?: string | null
  canonical_filename?: string | null
}

export interface DocumentIconResult {
  glyph: string
  hue: AreaHue
}

/** Pattern → icon. First match wins, so order matters (most specific first). */
const DOC_TYPE_PATTERNS: { pattern: RegExp; glyph: string; hue: AreaHue }[] = [
  // The travel-vs-guide tie: 'travel' must come before generic 'guide'.
  { pattern: /\btravel(ogue|\s+(writing|journal|guide))?\b/i, glyph: '🧭', hue: 'explore' },
  { pattern: /\b(restaurant\s+menu|menu)\b/i, glyph: '🍽', hue: 'observatory' },
  { pattern: /\b(recipe|cookbook|cooking\s+guide)\b/i, glyph: '🍳', hue: 'observatory' },
  { pattern: /\breview\b/i, glyph: '⭐', hue: 'research' },
  { pattern: /\b(magazine\s+article|article|blog)\b/i, glyph: '📰', hue: 'docs' },
  { pattern: /\bmagazine\b/i, glyph: '🗞', hue: 'explore' },
  { pattern: /\b(essay|philosophical\s+dialogue|dialogue)\b/i, glyph: '🎓', hue: 'search' },
  { pattern: /\bguide\b/i, glyph: '🗺', hue: 'explore' },
  { pattern: /\b(memoir|personal\s+narrative|narrative)\b/i, glyph: '📓', hue: 'ask' },
  { pattern: /\b(research\s+paper|journal\s+article|paper)\b/i, glyph: '🔬', hue: 'docs' },
  { pattern: /\b(contract|agreement|license|legal)\b/i, glyph: '⚖', hue: 'settings' },
  { pattern: /\b(personal\s+letter|letter)\b/i, glyph: '💌', hue: 'ask' },
  { pattern: /\b(invoice|receipt)\b/i, glyph: '🧾', hue: 'observatory' },
  // Plain "novel" / "book" — but not "cookbook" / "recipe book" (caught above).
  { pattern: /\b(novel|book)\b/i, glyph: '📕', hue: 'docs' },
  { pattern: /\bdescription\b/i, glyph: '📋', hue: 'settings' },
]

/** MIME-type → icon (used when doc_type doesn't match anything). */
const MIME_PATTERNS: { test: (mime: string) => boolean; glyph: string; hue: AreaHue }[] = [
  { test: (m) => m === 'application/pdf', glyph: '📄', hue: 'ask' },
  { test: (m) => m.includes('spreadsheet') || m === 'text/csv', glyph: '📊', hue: 'observatory' },
  { test: (m) => m === 'message/rfc822', glyph: '✉', hue: 'research' },
  { test: (m) => m.includes('presentation'), glyph: '🎞', hue: 'explore' },
  { test: (m) => m.startsWith('audio/'), glyph: '🎙', hue: 'ask' },
  { test: (m) => m.startsWith('image/'), glyph: '🖼', hue: 'search' },
  { test: (m) => m === 'text/html', glyph: '🌐', hue: 'explore' },
  { test: (m) => m.startsWith('text/x-') || m === 'application/json', glyph: '💻', hue: 'explore' },
  { test: (m) => m.startsWith('text/'), glyph: '📜', hue: 'settings' },
]

/** Filename-extension → icon (final fallback when both above miss). */
const EXT_PATTERNS: { exts: string[]; glyph: string; hue: AreaHue }[] = [
  { exts: ['pdf'], glyph: '📄', hue: 'ask' },
  { exts: ['xlsx', 'xls', 'csv', 'numbers'], glyph: '📊', hue: 'observatory' },
  { exts: ['eml', 'msg', 'mbox'], glyph: '✉', hue: 'research' },
  { exts: ['pptx', 'ppt', 'key'], glyph: '🎞', hue: 'explore' },
  { exts: ['mp3', 'wav', 'm4a', 'flac', 'aac', 'ogg'], glyph: '🎙', hue: 'ask' },
  { exts: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'tiff', 'bmp', 'heic'], glyph: '🖼', hue: 'search' },
  { exts: ['html', 'htm'], glyph: '🌐', hue: 'explore' },
  { exts: ['py', 'js', 'ts', 'tsx', 'jsx', 'rb', 'go', 'rs', 'c', 'h', 'cpp', 'java', 'json'], glyph: '💻', hue: 'explore' },
  { exts: ['txt', 'md', 'log', 'rtf'], glyph: '📜', hue: 'settings' },
]

const FALLBACK: DocumentIconResult = { glyph: '📦', hue: 'settings' }

/**
 * Resolve a document to an icon glyph + area hue.
 *
 * Resolution order:
 *   1. doc_type (LLM-derived, free-form — case-insensitive substring match).
 *   2. mime_type.
 *   3. canonical_filename extension.
 *   4. Fallback: 📦 (slate).
 *
 * The doc_type list and coverage rationale are documented in the
 * 2026-05-03 spec ("Coverage audit" section).
 */
export function documentTypeIcon(doc: DocLike): DocumentIconResult {
  if (doc.doc_type) {
    const dt = doc.doc_type
    for (const { pattern, glyph, hue } of DOC_TYPE_PATTERNS) {
      if (pattern.test(dt)) return { glyph, hue }
    }
  }

  if (doc.mime_type) {
    const mime = doc.mime_type.toLowerCase()
    for (const { test, glyph, hue } of MIME_PATTERNS) {
      if (test(mime)) return { glyph, hue }
    }
  }

  if (doc.canonical_filename) {
    const dot = doc.canonical_filename.lastIndexOf('.')
    const ext = dot >= 0 ? doc.canonical_filename.slice(dot + 1).toLowerCase() : ''
    if (ext) {
      for (const { exts, glyph, hue } of EXT_PATTERNS) {
        if (exts.includes(ext)) return { glyph, hue }
      }
    }
  }

  return FALLBACK
}
```

- [ ] **Step 2: Type-check + lint + format**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check
```

Expected: all pass.

- [ ] **Step 3: Sanity check the patterns by hand**

Open the file in your editor and walk through the corpus's top doc types from the spec's coverage table mentally:
- "Wine Review" → matches `/\breview\b/i` first → ⭐ research ✓
- "Recipe Book" → matches `/\b(recipe|cookbook|cooking\s+guide)\b/i` first (recipe matches before book) → 🍳 observatory ✓
- "Travel Guide" → matches `/\btravel(ogue|\s+(writing|journal|guide))?\b/i` first (travel guide is in the alternation) → 🧭 explore ✓
- "Beekeeping Guide" → no travel match, falls to `/\bguide\b/i` → 🗺 explore ✓
- "Magazine Article" → matches `/\b(magazine\s+article|article|blog)\b/i` → 📰 docs ✓
- "Personal Letter" → matches `/\b(personal\s+letter|letter)\b/i` → 💌 ask ✓
- "Cookbook" → matches recipe/cookbook → 🍳 observatory ✓ (and not the `/\bbook\b/i` pattern because cookbook matches earlier)
- "Novel" → matches `/\b(novel|book)\b/i` → 📕 docs ✓
- "Food Writing" → no match → falls to mime → fallback → 📦 settings (acknowledged limitation in spec)

If any case feels wrong, adjust the pattern + re-run lint.

- [ ] **Step 4: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/utils/documentTypeIcon.ts
git commit -m "$(cat <<'EOF'
feat(frontend): add documentTypeIcon util (LLM-aware doc → glyph + hue)

Resolves (DocLike doc) → { glyph, hue } via three-tier lookup:
priority-ordered regex over doc_type (LLM free-form text), then
mime_type, then filename extension. Patterns + coverage rationale
documented in the 2026-05-03 spec; ~80%+ coverage on the live corpus's
top-30 doc_types.

Travel-vs-guide tie-breaker: "travel" alternation comes before generic
guide, so "Travel Guide" → 🧭 (travel-writing) and "Wine Guide" → 🗺
(generic guide). Recipe before book, so "Recipe Book" → 🍳 not 📕.

Used by DocumentsPage rows + DocumentDetailPage in subsequent tasks.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: `topicDotColor` utility

**Files:**
- Create: `frontend/src/utils/topicDotColor.ts`

- [ ] **Step 1: Create the utility**

Write `frontend/src/utils/topicDotColor.ts`:

```typescript
import type { AreaHue } from '../hooks/useAreaAccent'

const HUES: AreaHue[] = [
  'ask',
  'research',
  'folders',
  'docs',
  'explore',
  'search',
  'observatory',
  'settings',
]

/**
 * Deterministic-hash-of-id → one of the eight area-accent hues.
 *
 * Used by the conversation list (Ask + Research sidebars) so each
 * conversation gets a stable colored dot. v1 derivation per the
 * 2026-05-03 spec: hash of conversation_id mod 8. Smarter "color by
 * dominant topic / top-cited entity" is a deferred follow-up.
 */
export function topicDotColor(id: string): AreaHue {
  // FNV-1a, 32-bit. Deterministic, low collision, no deps.
  let hash = 0x811c9dc5
  for (let i = 0; i < id.length; i++) {
    hash ^= id.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return HUES[hash % HUES.length]
}

/** Convenience: returns the CSS var() reference for the hue's accent value. */
export function topicDotVar(id: string): string {
  return `var(--area-${topicDotColor(id)}-accent)`
}
```

- [ ] **Step 2: Type-check + lint + format**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/utils/topicDotColor.ts
git commit -m "$(cat <<'EOF'
feat(frontend): add topicDotColor util (conversation-id → stable hue)

FNV-1a hash of an id mod 8 mapped onto the area-accent hues, plus a
topicDotVar convenience that returns the matching var() reference for
inline styles. Used by the Ask + Research conversation sidebars in the
following tasks. Smarter "color by dominant topic" is a deferred
follow-up per the spec.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Update `Layout.tsx` TabLink with icons + accent

**Files:**
- Modify: `frontend/src/components/Layout.tsx`

- [ ] **Step 1: Read the current TabLink + nav structure**

```bash
sed -n '1,110p' frontend/src/components/Layout.tsx
```

Understand: the current `TabLink` returns a `<NavLink>` with the active state styled blue (`bg-blue-50 text-blue-700 ring-blue-200/60` and dark variants). We're replacing the hard-coded blue with the active area's accent and adding an icon prop.

- [ ] **Step 2: Replace `TabLink` with the new version**

Find the `TabLink` function (currently around line 11) and replace its entire body with:

```tsx
function TabLink({
  to,
  end,
  icon,
  children,
}: {
  to: string
  end?: boolean
  icon?: string
  children: React.ReactNode
}) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        `relative inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] font-medium transition-colors ${
          isActive
            ? 'text-(--area-accent-text) ring-1'
            : 'text-(--color-text-secondary) hover:bg-black/4 dark:hover:bg-white/6 hover:text-(--color-text-primary)'
        }`
      }
      style={({ isActive }) =>
        isActive
          ? {
              backgroundColor: 'var(--area-accent-tint)',
              boxShadow: 'inset 0 0 0 1px var(--area-accent)',
            }
          : undefined
      }
    >
      {icon && <span aria-hidden>{icon}</span>}
      <span>{children}</span>
    </NavLink>
  )
}
```

The `boxShadow: inset 0 0 0 1px var(--area-accent)` substitutes for the `ring-1 ring-blue-200/60` pattern because Tailwind's `ring-` utilities can't reference CSS variables for the color in v4 without a workaround. Inset box-shadow gives the same visual.

- [ ] **Step 3: Pass the icon prop to each TabLink call**

In the same file, find the nav block (around lines 90-99). Update each TabLink call to include the icon. Example (with the full replacement):

```tsx
<TabLink to="/" end icon="💬">Ask</TabLink>
<TabLink to="/research" icon="🐙">Research</TabLink>
<TabLink to="/folders" icon="📁">Folders</TabLink>
<TabLink to="/docs" icon="📄">Documents</TabLink>
<TabLink to="/explore" icon="🌍">Explore</TabLink>
<TabLink to="/search" icon="🔍">Search</TabLink>
<TabLink to="/stats" icon="📊">Observatory</TabLink>
{isAdmin && <TabLink to="/integrations" icon="🔌">Integrations</TabLink>}
{isAdmin && <TabLink to="/admin" icon="⚙">System Settings</TabLink>}
```

If your existing nav has the Ask tab written as a different element (e.g. a logo + text instead of a TabLink), preserve that and only add the icon to actual TabLinks. Verify by reading the current JSX.

- [ ] **Step 4: Type-check + lint + format + build**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check && npm run build 2>&1 | tail -10
```

Expected: all pass; build succeeds.

- [ ] **Step 5: Smoke-check in HarborClerk**

Open HarborClerk, log in, click through every top-level tab. Expected:
- Each tab now shows a small emoji to the left of the label.
- The active tab's pill background + ring is the area's accent hue (terracotta on Ask, dusty blue on Documents, sage on Observatory, slate on System Settings, etc.) — not blue.
- Inactive tabs look like before.

If a tab's accent looks wrong (e.g. Documents stays blue), check that `useAreaAccent` from Task 2 is wired up.

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/components/Layout.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): TabLink icons + per-area accent active state

Each top-nav tab gets a small emoji icon next to the label. Active-tab
pill + ring switch from hard-coded blue to the active area's accent
(via --area-accent-tint background + inset box-shadow ring + accent-text
text color), so the nav strip is the first place users see the area's
hue. Hover state on inactive tabs unchanged.

Inset box-shadow workaround used in lieu of Tailwind's ring-{color}
utilities, which don't accept CSS variables for the color in v4
without a workaround.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Sweep Settings hub + simple Settings subpages

**Files:**
- Modify: `frontend/src/pages/SystemSettingsPage.tsx`
- Modify: `frontend/src/pages/UsersPage.tsx`
- Modify: `frontend/src/pages/ApiKeysPage.tsx`
- Modify: `frontend/src/pages/ApiKeyDashboardPage.tsx`
- Modify: `frontend/src/pages/RetrievalSettingsPage.tsx`
- Modify: `frontend/src/pages/RateLimitSettingsPage.tsx`
- Modify: `frontend/src/pages/ServiceLogsPage.tsx`
- Modify: `frontend/src/pages/IntegrationsPage.tsx`
- Modify: `frontend/src/pages/PreferencesPage.tsx`

- [ ] **Step 1: Rewrite `SystemSettingsPage.tsx` with grouped sections + icon tiles**

The current file (44 lines) is a flat list of `<Link>` rows. Replace with grouped sections per the spec. New full file:

```tsx
import { Link } from 'react-router-dom'
import { useAuth } from '../auth'
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'
import { IconTile, type AreaHue } from '../components/IconTile'

interface SettingsItem {
  to: string
  label: string
  sub: string
  icon: string
  hue: AreaHue
}

const SECTIONS: { label: string; items: SettingsItem[] }[] = [
  {
    label: 'Access & identity',
    items: [
      { to: '/admin/users', label: 'Users', sub: 'Manage accounts and roles', icon: '👥', hue: 'docs' },
      { to: '/admin/keys', label: 'API Keys', sub: 'Create and revoke API keys', icon: '🔑', hue: 'research' },
    ],
  },
  {
    label: 'Models & languages',
    items: [
      { to: '/admin/models', label: 'Models', sub: 'Download and manage LLM models', icon: '🧠', hue: 'observatory' },
      { to: '/admin/languages', label: 'Languages', sub: 'OCR & entity language packs', icon: '🌐', hue: 'explore' },
    ],
  },
  {
    label: 'Behavior & limits',
    items: [
      { to: '/admin/retrieval', label: 'Retrieval', sub: 'Chat & MCP search behavior', icon: '🔍', hue: 'search' },
      { to: '/admin/rate-limits', label: 'Rate Limits', sub: 'Default API key rate limits', icon: '⏱', hue: 'ask' },
    ],
  },
  {
    label: 'Operations',
    items: [
      { to: '/admin/system/status', label: 'System Status', sub: 'Health checks and statistics', icon: '💚', hue: 'observatory' },
      { to: '/admin/system/logs', label: 'Service Logs', sub: 'View log files and tail commands', icon: '📜', hue: 'settings' },
      { to: '/admin/system/maintenance', label: 'System Maintenance', sub: 'Purge, reaper, and cleanup', icon: '🧹', hue: 'ask' },
    ],
  },
]

export default function SystemSettingsPage() {
  const { user } = useAuth()
  if (user?.role !== 'admin') {
    return (
      <div className="mx-auto max-w-4xl px-4 py-8">
        <PageHeader title="System Settings" />
        <p className="text-sm text-(--color-text-secondary)">Admins only.</p>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-4xl px-4 py-8">
      <PageHeader title="System Settings" />
      {SECTIONS.map((section) => (
        <div key={section.label} className="mb-6">
          <h2 className="mb-2 font-serif text-base font-semibold tracking-tight text-(--color-text-primary)">
            {section.label}
          </h2>
          <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
            {section.items.map((item) => (
              <Card key={item.to} className="p-0">
                <Link
                  to={item.to}
                  className="flex items-center gap-3 px-3.5 py-3 text-sm"
                >
                  <IconTile hue={item.hue} size={28}>{item.icon}</IconTile>
                  <div className="flex-1">
                    <div className="font-medium text-(--color-text-primary)">{item.label}</div>
                    <div className="text-[11px] text-(--color-text-secondary)">{item.sub}</div>
                  </div>
                  <span className="text-(--color-text-secondary) opacity-50">›</span>
                </Link>
              </Card>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
```

- [ ] **Step 2: Sweep the simple Settings subpages — apply PageHeader, replace ad-hoc h1s**

For each of these files: read the existing content, find the page-title `<h1>` (or `<h2>` standing in for one), and replace with `<PageHeader title="…" />`. Drop the import line for `PageHeader` at the top. Preserve all other JSX and logic.

Files to sweep (in this order, one commit per pair if you prefer smaller commits, otherwise one commit at the end of step 3):

1. **`UsersPage.tsx`** — find `<h1>Users</h1>` (or similar), replace with `<PageHeader title="Users" />`. Wrap the user table in `<Card className="overflow-hidden">…</Card>` if it isn't already in some kind of card.
2. **`ApiKeysPage.tsx`** — same: `<PageHeader title="API Keys" />`.
3. **`ApiKeyDashboardPage.tsx`** — `<PageHeader title={`Key: ${keyName}`} />` if there's a key in scope, else `<PageHeader title="API Key" />`.
4. **`RetrievalSettingsPage.tsx`** — `<PageHeader title="Retrieval" subtitle="Chat & MCP search behavior" />`.
5. **`RateLimitSettingsPage.tsx`** — `<PageHeader title="Rate Limits" />`.
6. **`ServiceLogsPage.tsx`** — `<PageHeader title="Service Logs" />`.
7. **`IntegrationsPage.tsx`** — `<PageHeader title="Integrations" subtitle="MCP and external integrations" />`.
8. **`PreferencesPage.tsx`** — `<PageHeader title="Preferences" subtitle="Personal settings" />`.

For each one: also wrap the largest top-level content block in a `<Card className="p-4">` if it currently lives on raw page background. If it's already a card (e.g. uses `rounded-xl ring-1 ring-(--color-border)`), replace those classes with `<Card className="p-4">`.

- [ ] **Step 3: Type-check + lint + format + build**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check && npm run build 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 4: Smoke-check**

Open HarborClerk, navigate to `/admin`. Expected:
- Hub page now grouped into four serif-titled sections.
- Each item is a card with a colored icon tile, hover state, chevron.
- Active "System Settings" tab in nav is slate.
- Click into Users / API Keys / etc. — each subpage has the serif `PageHeader` with a slate accent bar.

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/pages/SystemSettingsPage.tsx \
        frontend/src/pages/UsersPage.tsx \
        frontend/src/pages/ApiKeysPage.tsx \
        frontend/src/pages/ApiKeyDashboardPage.tsx \
        frontend/src/pages/RetrievalSettingsPage.tsx \
        frontend/src/pages/RateLimitSettingsPage.tsx \
        frontend/src/pages/ServiceLogsPage.tsx \
        frontend/src/pages/IntegrationsPage.tsx \
        frontend/src/pages/PreferencesPage.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): sweep Settings hub + simple subpages with new design tokens

System Settings hub rewritten: grouped into four sections (Access &
identity / Models & languages / Behavior & limits / Operations) with
serif section labels and IconTile-tile icons drawn from the per-area
accents (Users → docs/blue, API Keys → research/ochre, Models →
observatory/sage, Languages → explore/mauve, etc.).

Simple subpages (Users, API Keys, API Key Dashboard, Retrieval, Rate
Limits, Service Logs, Integrations, Preferences) get PageHeader with
the auto-bound slate accent (via the Settings area route prefix), and
their primary content surfaces wrapped in <Card>. No behavior changes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Sweep richer Settings subpages (Models / Languages / SystemStatus / SystemMaintenance)

**Files:**
- Modify: `frontend/src/pages/ModelsPage.tsx`
- Modify: `frontend/src/pages/LanguagesPage.tsx`
- Modify: `frontend/src/pages/SystemStatusPage.tsx`
- Modify: `frontend/src/pages/SystemMaintenancePage.tsx`

- [ ] **Step 1: ModelsPage — PageHeader + Card-per-model + StatusPill for download state**

Read the file. Replace the page title `<h1>` with `<PageHeader title="Models" subtitle="Download and manage local LLM models" />`. Find the loop that renders one card per model (most likely `models.map((m) => (...))` or similar). Wrap each model's row in `<Card className="p-4 mb-2">…</Card>`.

If model state is shown as text or a colored span (e.g. "ready", "downloading", "missing"), replace with `<StatusPill state="active" label="ready" />` / `<StatusPill state="running" label="downloading" />` / `<StatusPill state="idle" label="missing" />` / `<StatusPill state="error" />` as appropriate.

Imports to add:
```tsx
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'
import { StatusPill } from '../components/StatusPill'
```

- [ ] **Step 2: LanguagesPage — PageHeader + Card-per-language-row + StatusPill for install state**

Same pattern. Replace the page title `<h1>` with `<PageHeader title="Languages" subtitle="OCR and entity language packs" />`. Wrap each language row in `<Card className="p-3 mb-2">`. Replace install-state badges with `<StatusPill>`.

The existing language pack states map to:
- "installed" → `<StatusPill state="active" label="installed" />`
- "installing" → `<StatusPill state="running" label="installing" />`
- "available" / "not installed" → `<StatusPill state="idle" label="available" />`
- "failed" → `<StatusPill state="error" />`

- [ ] **Step 3: SystemStatusPage — PageHeader + Cards for the existing status sections**

Find the page title and replace with `<PageHeader title="System Status" subtitle="Health checks and service statistics" />`. The existing health/status cards (worker queues, postgres, embedder, etc.) likely already live in some kind of card markup — replace those wrappers with `<Card className="p-4">`.

Inside each card, if there's a per-service status indicator (green dot, "running" text, etc.) replace with the appropriate `<StatusPill>`.

- [ ] **Step 4: SystemMaintenancePage — PageHeader + Cards for action sections**

Find the page title and replace with `<PageHeader title="System Maintenance" subtitle="Purge, reaper, and cleanup actions" />`. Wrap each action group (Purge / Reaper / Cleanup) in `<Card className="p-4 mb-3">`. Destructive buttons keep their existing red styling.

- [ ] **Step 5: Type-check + lint + format + build**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check && npm run build 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 6: Smoke-check**

Open `/admin/models`, `/admin/languages`, `/admin/system/status`, `/admin/system/maintenance`. Expected:
- Each page has the slate `PageHeader`.
- State indicators render as the new StatusPill.
- Cards have the subtle gradient.

- [ ] **Step 7: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/pages/ModelsPage.tsx \
        frontend/src/pages/LanguagesPage.tsx \
        frontend/src/pages/SystemStatusPage.tsx \
        frontend/src/pages/SystemMaintenancePage.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): sweep richer Settings subpages with PageHeader + Card + StatusPill

Models, Languages, SystemStatus, SystemMaintenance get the same
PageHeader + Card + StatusPill treatment as the simple subpages.
Install/download/health states all map onto the new five-state pill
component (active/running/idle/error/pending). No behavioral change.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Sweep `DocumentsPage`

**Files:**
- Modify: `frontend/src/pages/DocumentsPage.tsx`

- [ ] **Step 1: Read the file structure**

```bash
sed -n '1,80p' frontend/src/pages/DocumentsPage.tsx
grep -n "h1\|h2\|className=\"text-\|status\|active\|filter" frontend/src/pages/DocumentsPage.tsx | head -30
```

Understand: where the page title is rendered, where the filter row is, where the row-rendering loop is (the `documents.map(...)` or similar), and where the status pill markup lives.

- [ ] **Step 2: Add imports + replace the page title**

At the top of the file, add:

```tsx
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'
import { StatusPill } from '../components/StatusPill'
import { IconTile } from '../components/IconTile'
import { documentTypeIcon } from '../utils/documentTypeIcon'
```

Find the existing page title `<h1>` (probably `<h1 className="text-2xl font-bold mb-4">Documents</h1>` or similar) and replace with:

```tsx
<PageHeader title="Documents" actions={<FoldersLink />} />
```

…where `<FoldersLink />` represents the existing top-right "Folders" button. If it's a single inline element rather than a separate component, just put that JSX into the actions slot directly.

If there's a "Continue viewing" affordance currently rendered above the title, leave it where it is (above the PageHeader); the spec preserves it.

- [ ] **Step 3: Update each row to use IconTile + StatusPill**

Find the row-rendering JSX (the `.map((doc) => (...))` block). Inside the row's JSX, add an `IconTile` at the start (left of the title) and replace the existing inline status badge with `<StatusPill state={...} />`.

Example (the exact JSX depends on existing structure — adapt; keep all existing data bindings):

```tsx
{documents.map((doc) => {
  const { glyph, hue } = documentTypeIcon(doc)
  return (
    <Card key={doc.doc_id} className="mb-1.5 flex items-center gap-3 px-3.5 py-3">
      <IconTile hue={hue} size={30}>{glyph}</IconTile>
      <div className="min-w-0 flex-1">
        <Link to={`/docs/${doc.doc_id}`} className="block truncate font-medium text-(--color-text-primary) hover:underline">
          {doc.title}
        </Link>
        <p className="truncate text-[11px] text-(--color-text-secondary)">
          {doc.canonical_filename ?? doc.title}
        </p>
      </div>
      <StatusPill state={mapDocStatusToPillState(doc.pipeline_status)} />
      <span className="shrink-0 text-[11px] text-(--color-text-secondary)">
        {formatDate(doc.updated_at)}
      </span>
      <DownloadIcon doc={doc} />
    </Card>
  )
})}
```

Add a small helper at the top of the file (or at the bottom — wherever existing helpers live). The `pipeline_status` enum values are defined in `src/harbor_clerk/models/enums.py` (`PipelineStatus`):

```tsx
import type { PillState } from '../components/StatusPill'

// Maps backend pipeline_status (PipelineStatus enum) → frontend pill state.
// The enum values: queued, extracting, extracted, ocr_running, ocr_done,
// chunking, chunked, extracting_entities, entities_done, embedding, embedded,
// summarizing, summarized, finalizing, ready, error.
function mapDocStatusToPillState(pipeline: string): PillState {
  if (pipeline === 'error') return 'error'
  if (pipeline === 'ready') return 'active'
  if (pipeline === 'queued') return 'pending'
  // All other states are in-progress (extracting / chunking / embedding / etc.).
  return 'running'
}
```

(Pass `doc.pipeline_status` to this when rendering each row.)

- [ ] **Step 4: Update the expanded-row metadata block**

When a row is expanded (the chevron-click reveals "DOCUMENT TYPE", "SUMMARY", "TOPIC", "SOURCE"), wrap the expanded section's content in a slightly inset `<Card className="mt-2 ml-9 p-3">` (or whatever margin matches the existing visual). The expanded content should look like a nested elevated surface, not blend into the row above.

- [ ] **Step 5: Type-check + lint + format + build**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check && npm run build 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 6: Smoke-check**

Open HarborClerk → Documents. Expected:
- PageHeader serif title + dusty-blue accent bar.
- Each row: type-icon tile (left) → title + filename → status pill → date → download icon.
- Different doc types show different icons (Novel = 📕 / PDF = 📄 / etc.).
- Clicking a row's expand caret reveals nested Card with the metadata.
- Active "Documents" tab in nav is dusty blue.

- [ ] **Step 7: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/pages/DocumentsPage.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): sweep DocumentsPage with PageHeader + IconTile + StatusPill

PageHeader replaces the inline h1 (dusty-blue accent bar via the
auto-bound docs area). Each row gets an IconTile with the document-
type glyph (Novel / PDF / Spreadsheet / Email / etc.) drawn from
documentTypeIcon. Status text replaced with StatusPill. Expanded-row
metadata wraps in a nested Card so it visually nests instead of
bleeding into table chrome.

Continue-viewing affordance preserved at the top. Filter row layout
unchanged. No behavior changes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: Sweep `DocumentDetailPage`

**Files:**
- Modify: `frontend/src/pages/DocumentDetailPage.tsx`

- [ ] **Step 1: Replace the page title block + add IconTile next to the title**

At the top of the file add the imports (PageHeader, Card, StatusPill, IconTile, documentTypeIcon — same set as DocumentsPage; some may already be imported).

Find the existing title block (probably `<h1 className="...">{doc.title}</h1>` near the top of the JSX). Replace with:

```tsx
{doc && (
  <div className="mb-4 flex items-start gap-3">
    <IconTile hue={documentTypeIcon(doc).hue} size={36}>
      {documentTypeIcon(doc).glyph}
    </IconTile>
    <div className="flex-1">
      <PageHeader
        title={doc.title}
        subtitle={doc.canonical_filename}
        actions={
          <>
            <button onClick={onReprocess} className="...">Reprocess</button>
            <button onClick={onDelete} className="...">Delete</button>
          </>
        }
      />
    </div>
  </div>
)}
```

Adapt the action buttons' classes to match existing — don't restyle them, just preserve the existing classNames.

- [ ] **Step 2: Wrap the ingestion-jobs and stats sections in Cards**

Find the Ingestion Jobs section and wrap its body in `<Card className="p-3 mb-4">`. Inside the jobs list, replace the per-job status indicator with `<StatusPill state={...} />` mapping the job's status to the pill state.

Find the Document Statistics section and wrap in `<Card className="p-4 mb-4">`. Existing entity-type colored badges in this section MUST remain unchanged (the existing `ENTITY_TYPE_COLORS` map continues to power them).

- [ ] **Step 3: Type-check + lint + format + build**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check && npm run build 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 4: Smoke-check**

Open any document's detail page. Expected:
- Type-icon tile to the left of the serif title.
- Dusty-blue accent bar under the title.
- Ingestion Jobs section in its own Card with StatusPill per job.
- Document Statistics in a Card with entity-type colored badges UNCHANGED.
- Reprocess + Delete buttons stay where they were.
- Reveal in Finder button (if user is on macOS) unchanged.

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/pages/DocumentDetailPage.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): sweep DocumentDetailPage with PageHeader + IconTile + Cards

Title row picks up the document-type icon (left of the serif title).
Ingestion jobs wrapped in a Card with StatusPill per job. Document
statistics wrapped in a Card. Existing entity-type colored badges
preserved exactly as-is — they were the working color story on this
page already.

Reprocess + Delete + Reveal in Finder action buttons unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 14: Sweep `FoldersPage`

**Files:**
- Modify: `frontend/src/pages/FoldersPage.tsx`

- [ ] **Step 1: Add imports + replace the page title**

At the top of the file add:

```tsx
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'
import { StatusPill } from '../components/StatusPill'
import { IconTile } from '../components/IconTile'
```

Find the page title `<h1>` and replace with:

```tsx
<PageHeader
  title="Folders"
  actions={
    <button
      onClick={onAddFolder}
      className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium"
      style={{
        backgroundColor: 'var(--area-accent-tint)',
        color: 'var(--area-accent-text)',
        border: '1px solid var(--area-accent)',
      }}
    >
      <span aria-hidden>＋</span> Add Folder
    </button>
  }
/>
```

The button uses inline styles with the area-accent vars (the page is in the Folders area, so the active accent is warm khaki). This replaces the current iOS-blue "Add Folder" button.

- [ ] **Step 2: Replace the table with Card-per-row**

Find the existing table markup. Replace with a vertical stack of cards, one per folder. Example:

```tsx
{folders.map((folder) => (
  <Card key={folder.id} className="mb-2 flex items-center gap-3 px-3.5 py-3">
    <IconTile size={30}>📁</IconTile>
    <div className="min-w-0 flex-1">
      <div className="truncate font-medium text-(--color-text-primary)">{folder.path}</div>
      <div className="text-[11px] text-(--color-text-secondary)">
        {folder.progress_completed} / {folder.progress_total}
      </div>
    </div>
    <StatusPill state={mapFolderStatusToPillState(folder.status)} />
    <div className="flex shrink-0 items-center gap-2">
      <button onClick={() => onDisable(folder)} className="...">Disable</button>
      <button onClick={() => onDelete(folder)} className="...">Delete</button>
    </div>
  </Card>
))}
```

Add a helper:

```tsx
function mapFolderStatusToPillState(status: string): 'active' | 'running' | 'idle' | 'error' | 'pending' {
  if (status === 'idle') return 'idle'
  if (status === 'scanning' || status === 'ingesting') return 'running'
  if (status === 'errored') return 'error'
  return 'active'
}
```

Adapt the status enum strings to match the actual values returned by the backend (verify by grepping `src/harbor_clerk/models/watched_folder.py` or the API schema).

- [ ] **Step 3: Add empty state**

Wrap the folders list in a check for `folders.length === 0` and render a centered hero block in that case:

```tsx
{folders.length === 0 ? (
  <Card className="mx-auto mt-8 flex max-w-md flex-col items-center gap-3 p-8 text-center">
    <IconTile size={56}>📁</IconTile>
    <h3 className="font-serif text-lg font-semibold">No folders yet</h3>
    <p className="text-sm text-(--color-text-secondary)">
      Add a folder to start watching for documents to ingest.
    </p>
    <button onClick={onAddFolder} className="..." style={{ /* same as the header button */ }}>
      ＋ Add Folder
    </button>
  </Card>
) : (
  <div>{/* the .map(...) above */}</div>
)}
```

- [ ] **Step 4: Type-check + lint + format + build**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check && npm run build 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 5: Smoke-check**

Open /folders. Expected:
- Serif "Folders" title with khaki accent bar.
- "Add Folder" button uses khaki-tinted styling (not blue).
- Each folder rendered as a Card row with folder icon tile + path + progress + StatusPill + Disable/Delete buttons.
- Empty state (if you have access to a fresh install): centered hero card.

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/pages/FoldersPage.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): sweep FoldersPage with khaki accent + Card-per-row + empty state

Page title now uses PageHeader (khaki accent bar via Folders area).
Add Folder button switches from iOS-blue to khaki-tinted styling
(inline styles using --area-accent-tint / --area-accent-text /
--area-accent border). Each folder rendered as a Card row with folder
icon tile, path, progress text, StatusPill, and the existing Disable
+ Delete actions. Empty state added — centered hero card with the
add-folder CTA when zero folders.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 15: Sweep `ExplorePage` (entity-type tinted pills)

**Files:**
- Modify: `frontend/src/pages/ExplorePage.tsx`

- [ ] **Step 1: Add imports + replace page title**

```tsx
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'
import { ENTITY_COLORS } from '../components/stats/CorpusCharts'
```

Find the page title and replace with:

```tsx
<PageHeader
  title="Explore"
  subtitle="People, places, organizations, and topic clusters in your corpus"
  actions={
    <input
      type="search"
      placeholder="Search topics & entities…"
      value={searchTerm}
      onChange={(e) => setSearchTerm(e.target.value)}
      className="w-64 rounded-md bg-(--color-bg-secondary) px-3 py-1.5 text-sm ring-1 ring-(--color-border) focus:outline-none focus:ring-2"
      style={{ outlineColor: 'var(--area-accent)' }}
    />
  }
/>
```

(Re-use whatever search-input class is already in use in the file; what matters is replacing the surrounding title block.)

- [ ] **Step 2: Tint the People / Places / Organizations pills with entity-type colors**

Find the pills loop for each section (likely something like `people.map((p) => <span className="bg-gray-...">{p.name}</span>)`).

Replace the gray pill class with inline-styled pills using the entity type's color from the existing `ENTITY_COLORS` map. People = PERSON (blue), Places = GPE (orange), Organizations = ORG (green). Example:

```tsx
{people.map((p) => (
  <span
    key={p.id}
    className="inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs"
    style={{
      backgroundColor: `${ENTITY_COLORS.PERSON}1f`, // 12% opacity hex
      color: ENTITY_COLORS.PERSON,
    }}
  >
    {p.name}
    <span className="text-[10px] opacity-70">{p.count}</span>
  </span>
))}
```

The `1f` suffix on the hex is 12% opacity in 8-digit hex. (Tailwind v4 supports `bg-[var(...)/12]` for opacity-on-vars too — either form is fine.)

Repeat for the Places loop using `ENTITY_COLORS.GPE` and Organizations using `ENTITY_COLORS.ORG`.

- [ ] **Step 3: Wrap each Topic Cluster card body in `<Card>`**

Find the Topic Clusters grid. Each cluster's container becomes:

```tsx
<Card key={cluster.id} className="p-3.5">
  <div className="mb-1.5 flex items-center gap-2">
    <span
      className="inline-block h-2 w-2 rounded-full"
      style={{ backgroundColor: cluster.color }}
    />
    <h4 className="font-medium text-(--color-text-primary)">{cluster.name}</h4>
  </div>
  {/* existing tag chips, document count, etc. */}
</Card>
```

(The `cluster.color` field is already populated from the existing topic-color logic; preserve it.)

- [ ] **Step 4: Type-check + lint + format + build**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check && npm run build 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 5: Smoke-check**

Open /explore. Expected:
- Serif "Explore" title with mauve accent bar.
- People pills tinted blue (PERSON), Places tinted orange (GPE), Organizations tinted green (ORG).
- Topic Cluster cards have the gradient surface treatment.
- Active "Explore" tab in nav is mauve.

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/pages/ExplorePage.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): sweep ExplorePage — entity-type tinted pills + topic-cluster Cards

People / Places / Organizations pills switch from monochrome gray to
12%-tint pills colored with their entity type's hue from ENTITY_COLORS
(PERSON = blue, GPE = orange, ORG = green) — the highest-leverage spot
for entity colors outside DocumentDetail. Topic cluster cards wrapped
in <Card>. PageHeader with mauve accent.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 16: Sweep `SearchPage`

**Files:**
- Modify: `frontend/src/pages/SearchPage.tsx`

- [ ] **Step 1: Replace page title + accent the focus ring + button**

Add imports:

```tsx
import { PageHeader } from '../components/PageHeader'
```

Replace the existing page title with `<PageHeader title="Search" subtitle="Hybrid lexical + semantic retrieval" />`.

Find the "Search" submit button. Replace its current blue styling with the area's accent treatment (the search input's existing focus-ring stays as-is — restyling Tailwind ring colors with CSS vars is fiddly enough to defer):

```tsx
<button
  type="submit"
  className="rounded-md px-4 py-1.5 text-sm font-medium"
  style={{
    backgroundColor: 'var(--area-accent-tint)',
    color: 'var(--area-accent-text)',
    border: '1px solid var(--area-accent)',
  }}
>
  Search
</button>
```

- [ ] **Step 2: Type-check + lint + format + build**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check && npm run build 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 3: Smoke-check**

Open /search. Expected:
- Serif title with dusty-teal accent bar.
- Search button styled in dusty teal (not blue).
- Active "Search" tab in nav is dusty teal.

- [ ] **Step 4: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/pages/SearchPage.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): sweep SearchPage with PageHeader + dusty-teal button accent

Page title block, focus ring, and Search submit button switch to the
dusty-teal area accent. No behavior change.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 17: Sweep `StatsPage` (Observatory) — serif KPI numerals + sage accent

**Files:**
- Modify: `frontend/src/pages/StatsPage.tsx`

- [ ] **Step 1: Add imports + replace page title**

```tsx
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'
```

Replace the existing title block with `<PageHeader title="Observatory" subtitle="Corpus statistics and processing pipeline" />`.

Sub-tabs ("Corpus Statistics" / "Processing Pipeline") keep their existing tab pattern; the active tab indicator should use `var(--area-accent)` instead of hard-coded blue. If the sub-tab markup is custom (not TabLink), update the active-state class/style to use the var.

- [ ] **Step 2: KPI numerals get the serif treatment**

Find the four KPI cards (Documents / Chunks / Pages / Entities at the top). For each, update the number element to use `font-serif`:

```tsx
<div className="font-serif text-3xl font-semibold tracking-tight text-(--color-text-primary)">
  {data.documents.toLocaleString()}
</div>
```

If the KPI cards are already wrapped in card-like markup, keep that wrapper and just update the inner number's classes. If they're not wrapped, wrap each one in `<Card className="p-4">`.

- [ ] **Step 3: Existing charts stay untouched**

Important: the entity-color charts (Top Entities bar chart, Topic Distribution treemap, Entity Network graph) are NOT to be changed in this pass. Their colors are working. Just confirm they still render unchanged after the page-header swap.

If the chart container divs use ad-hoc card markup (`rounded-xl bg-... shadow-mac ring-1 ring-...`), you can replace those wrappers with `<Card className="p-4">` to pick up the gradient surface — but DO NOT alter chart contents or palettes.

- [ ] **Step 4: Type-check + lint + format + build**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check && npm run build 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 5: Smoke-check**

Open /stats. Expected:
- Serif "Observatory" title with sage accent bar.
- KPI numerals render in serif (visibly different from the labels above them).
- Top Entities bar chart, Topic Distribution treemap, Entity Network graph all render with their existing colors and layouts — no regression.
- Sub-tab active indicator is sage.
- Active "Observatory" tab in nav is sage.

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/pages/StatsPage.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): sweep StatsPage (Observatory) with sage accent + serif KPIs

PageHeader with sage accent bar. KPI numerals render in font-serif so
they read as confident headline numbers instead of generic large text.
Sub-tab active indicator switches to sage. Existing entity-colored
charts (Top Entities, Topic Distribution, Entity Network) unchanged —
they were already the page's working color story.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 18: Sweep `ResearchPage`

**Files:**
- Modify: `frontend/src/pages/ResearchPage.tsx`

- [ ] **Step 1: Add imports + replace page title**

```tsx
import { PageHeader } from '../components/PageHeader'
import { topicDotVar } from '../utils/topicDotColor'
```

Find the page title block. Replace the title with `<PageHeader title="Research" subtitle="Deep multi-step research over your corpus" />`.

Important: leave the octopus illustration and the "Start Research" CTA (the current loud amber/orange button) UNCHANGED. The spec preserves both — the CTA "rhymes" with the area accent and the octopus is iconic.

- [ ] **Step 2: Sidebar "+ New Research" button + conversation topic dots**

Find the sidebar "+ New Research" button. Update it to use the ochre area accent:

```tsx
<button
  onClick={onNewResearch}
  className="mb-3 inline-flex w-full items-center gap-1.5 rounded-md px-3 py-2 text-sm font-medium"
  style={{
    backgroundColor: 'var(--area-accent-tint)',
    color: 'var(--area-accent-text)',
    border: '1px solid var(--area-accent)',
  }}
>
  <span aria-hidden>＋</span> New Research
</button>
```

Find the conversation list `.map()` in the sidebar. For each row, prepend a colored topic dot using `topicDotVar(conv.id)`:

```tsx
{conversations.map((conv) => (
  <div key={conv.id} className="flex items-center gap-2 rounded px-2 py-1.5 hover:bg-(--color-bg-secondary)">
    <span
      className="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
      style={{ background: topicDotVar(conv.id) }}
    />
    <Link to={`/research/${conv.id}`} className="min-w-0 flex-1 truncate text-sm">
      {conv.title}
    </Link>
    <span className="shrink-0 text-[10px] text-(--color-text-secondary)">
      {formatRelative(conv.updated_at)}
    </span>
  </div>
))}
```

(Adapt to existing sidebar JSX — the point is: prepend a colored dot, swap the new-research button to ochre.)

- [ ] **Step 3: Type-check + lint + format + build**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check && npm run build 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 4: Smoke-check**

Open /research. Expected:
- Octopus + serif "Research" title + ochre accent bar.
- "Start Research" CTA still loud amber/orange — unchanged.
- Sidebar "+ New Research" button now ochre-tinted.
- Each conversation in the sidebar shows a colored topic dot.
- Active "Research" tab in nav is ochre.

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/pages/ResearchPage.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): sweep ResearchPage with ochre accent + sidebar topic dots

PageHeader with ochre accent. Sidebar New Research button switches to
the ochre area treatment. Each conversation in the sidebar gets a
colored topic dot via topicDotColor (FNV-1a hash mod 8 onto the area-
accent hues). Octopus illustration and the loud orange Start Research
CTA preserved per spec — the existing color story rhymes with the
area accent.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 19: Sweep `HomePage` + `ChatPage` + chat sidebar (Ask area)

**Files:**
- Modify: `frontend/src/pages/HomePage.tsx`
- Modify: `frontend/src/pages/ChatPage.tsx` (the sidebar lives inside ChatPage — there's no separate sidebar component; verified 2026-05-04)

- [ ] **Step 1: Sidebar — apply terracotta accent + topic dots**

In `ChatPage.tsx`, find the sidebar markup (the conversation list `.map()` and the "+ New conversation" button). Change the "+ New conversation" button to terracotta:

```tsx
import { topicDotVar } from '../utils/topicDotColor'
// ...
<button
  onClick={onNewConversation}
  className="mb-3 inline-flex w-full items-center gap-1.5 rounded-md px-3 py-2 text-sm font-medium"
  style={{
    backgroundColor: 'var(--area-accent-tint)',
    color: 'var(--area-accent-text)',
    border: '1px solid var(--area-accent)',
  }}
>
  <span aria-hidden>＋</span> New conversation
</button>
```

Update each conversation row to prepend a colored topic dot:

```tsx
<Link
  to={`/c/${conv.id}`}
  className="flex items-center gap-2 rounded px-2 py-1.5 hover:bg-(--color-bg-secondary)"
>
  <span
    className="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
    style={{ background: topicDotVar(conv.id) }}
  />
  <span className="min-w-0 flex-1 truncate text-sm text-(--color-text-primary)">{conv.title}</span>
  <span className="shrink-0 text-[10px] text-(--color-text-secondary)">{formatRelative(conv.updated_at)}</span>
</Link>
```

- [ ] **Step 2: HomePage / ChatPage empty state — terracotta hero block**

In whichever component renders the chat empty state ("Ask your documents" + the icon + the suggestion chips), find the title block and update it to:

```tsx
import { PageHeader } from '../components/PageHeader'
import { IconTile } from '../components/IconTile'
// ...
<div className="flex flex-col items-center justify-center gap-4 py-16 text-center">
  <IconTile size={64}>📚</IconTile>
  <div>
    <h2 className="font-serif text-2xl font-semibold tracking-tight">Ask your documents</h2>
    <p className="mt-2 text-sm text-(--color-text-secondary)">
      Start a conversation to search, read, and reason over your library using a local LLM.
    </p>
  </div>
  <div className="flex flex-wrap justify-center gap-2">
    {SUGGESTION_CHIPS.map((chip) => (
      <button
        key={chip}
        onClick={() => onChipClick(chip)}
        className="rounded-full px-3 py-1 text-xs hover:opacity-80"
        style={{
          backgroundColor: 'var(--area-accent-tint)',
          color: 'var(--area-accent-text)',
          border: '1px solid var(--area-accent)',
        }}
      >
        {chip}
      </button>
    ))}
  </div>
</div>
```

The IconTile picks up the active area's accent automatically (no `hue` prop = uses `--area-accent`), so this becomes a terracotta tinted square on Ask.

Suggestion chip strings come from the existing constants in ChatPage / HomePage — preserve them.

- [ ] **Step 3: Type-check + lint + format + build**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check && npm run build 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 4: Smoke-check**

Open / (Ask). Expected:
- Empty state: terracotta IconTile (📚) + serif "Ask your documents" + terracotta suggestion chips.
- Sidebar "+ New conversation" button is terracotta.
- Each conversation in the sidebar has a colored topic dot.
- Active "Ask" tab in nav is terracotta.
- Click into a conversation: sidebar still shows dots.

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/pages/HomePage.tsx \
        frontend/src/pages/ChatPage.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): sweep Ask area — terracotta accent, hero IconTile, topic dots

Sidebar New conversation button switches to terracotta-tinted styling.
Each conversation row gets a colored topic dot via topicDotColor.
Empty state hero gets a 64px terracotta IconTile + serif title +
terracotta suggestion chips. Active Ask tab in nav is terracotta.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 20: Sweep `LoginPage` + `OnboardingWizard` + `SetupPage`

**Files:**
- Modify: `frontend/src/pages/LoginPage.tsx`
- Modify: `frontend/src/pages/SetupPage.tsx`
- Modify: `frontend/src/components/OnboardingWizard.tsx`

- [ ] **Step 1: LoginPage — PageHeader + Card**

Add imports:

```tsx
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'
```

Find the existing centered login form. Wrap the form in `<Card className="mx-auto max-w-sm p-6">`. Above the form fields, add:

```tsx
<PageHeader title="Harbor Clerk" subtitle="Sign in to continue" />
```

Note: LoginPage is pre-auth so the layout-root binding doesn't run; `--area-accent` falls back to the docs default (dusty blue). That's fine — login doesn't need an area identity.

- [ ] **Step 2: SetupPage — same treatment**

Add the same PageHeader + Card wrapping. Title: "Setup" with a subtitle describing the step (the existing JSX likely has step text — preserve it as the subtitle).

- [ ] **Step 3: OnboardingWizard — apply slate accent on Languages step**

The wizard already lives inside Layout (`/onboarding` route), so `useAreaAccent` is active. The Languages step is one of the wizard's pages. On that step:

- The "Install N & continue" CTA button uses inline styles bound to `var(--area-settings-accent-tint)` etc. (Settings = slate). Even though the active area might technically be Ask (since the wizard appears on /), we explicitly want this step to feel like a Settings interaction. So use `--area-settings-*` directly, not `--area-accent-*`.

Example button:

```tsx
<button
  onClick={onInstallAndContinue}
  className="rounded-md px-4 py-2 text-sm font-medium"
  style={{
    backgroundColor: 'var(--area-settings-accent-tint)',
    color: 'var(--area-settings-accent-text)',
    border: '1px solid var(--area-settings-accent)',
  }}
>
  Install {selectedCount} & continue
</button>
```

The Skip button stays as a quiet text button.

For other wizard steps (folder pick, progress overview), no per-step accent — let them use the active area's accent (which is whatever route the wizard appears on).

- [ ] **Step 4: Type-check + lint + format + build**

```bash
cd frontend && npm run type-check && npm run lint && npm run format:check && npm run build 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 5: Smoke-check**

- Log out. Expected: login page now in a Card with serif "Harbor Clerk" title.
- Trigger setup (if a fresh install is available, otherwise skip): same treatment.
- Trigger the onboarding wizard: Languages step's Install button is slate-tinted.

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway
git add frontend/src/pages/LoginPage.tsx \
        frontend/src/pages/SetupPage.tsx \
        frontend/src/components/OnboardingWizard.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): sweep LoginPage + SetupPage + OnboardingWizard

LoginPage and SetupPage get PageHeader + Card wrappers (pre-auth, so
they fall back to the docs/dusty-blue default — fine since neither
needs an area identity). Onboarding wizard's Languages step explicitly
binds its Install CTA to the settings/slate area accent because it's
conceptually a Settings interaction. Other wizard steps inherit the
active area's accent.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 21: Final validation + smoke checklist + open PR

**Files:** none (validation + PR creation)

- [ ] **Step 1: Full type-check + lint + format**

```bash
cd /Users/alex/mcp-gateway/frontend
npm run type-check
npm run lint
npm run format:check
npm run build 2>&1 | tail -20
```

All four must pass clean. Fix any remaining issues before proceeding.

- [ ] **Step 2: Manual smoke checklist (per spec)**

Open HarborClerk. Log in. Walk every top-level page in this order, verifying the bullets:

- **Ask** (`/`):
  - [ ] Active tab in nav is terracotta.
  - [ ] Empty state shows terracotta IconTile (📚) + serif title + terracotta suggestion chips.
  - [ ] Sidebar conversations have colored topic dots.
- **Research** (`/research`):
  - [ ] Active tab in nav is ochre.
  - [ ] Octopus illustration unchanged. Start Research CTA still loud amber.
  - [ ] Sidebar New Research button is ochre.
- **Folders** (`/folders`):
  - [ ] Active tab in nav is khaki.
  - [ ] Add Folder button is khaki-tinted (not blue).
  - [ ] Folder rows are Cards with status pills.
- **Documents** (`/docs`):
  - [ ] Active tab in nav is dusty blue.
  - [ ] PageHeader serif title + dusty blue accent bar.
  - [ ] Each row has a type-icon tile + status pill.
  - [ ] Filter row preserved.
- **DocumentDetail** (`/docs/<some-id>`):
  - [ ] Type-icon tile to the left of the serif title.
  - [ ] Existing entity-type colored badges unchanged.
  - [ ] Ingestion Jobs in a Card with StatusPills.
- **Explore** (`/explore`):
  - [ ] Active tab in nav is mauve.
  - [ ] People pills tinted blue (PERSON), Places tinted orange (GPE), Organizations tinted green (ORG).
  - [ ] Topic Cluster cards have gradient surface.
- **Search** (`/search`):
  - [ ] Active tab in nav is dusty teal.
  - [ ] Search button styled in dusty teal.
- **Observatory** (`/stats`):
  - [ ] Active tab in nav is sage.
  - [ ] KPI numerals render in serif.
  - [ ] Top Entities, Topic Distribution, Entity Network charts unchanged.
- **System Settings** (`/admin`):
  - [ ] Active tab in nav is slate.
  - [ ] Hub grouped into 4 sections with serif labels + IconTile-led cards.
  - [ ] Click into each subpage: each has a slate PageHeader.

- [ ] **Step 3: Switch to light mode and re-spot-check**

System Settings → Appearance → Light. Re-walk Ask, Documents, Observatory, Settings. For each:
- [ ] Page hue is the deeper light-mode variant (not the dark variant on a white background).
- [ ] Surface gradient flips to a faint top-down shadow (not a highlight).
- [ ] Status pills have ~10% opacity bg + the deeper hue text.

Switch back to Dark.

- [ ] **Step 4: Push branch and open PR**

```bash
cd /Users/alex/mcp-gateway
git push -u origin spec/frontend-design-pass
gh pr create --title "Frontend design pass — Calm Cartography hybrid" --body "$(cat <<'EOF'
## Summary

Implements [`docs/superpowers/specs/2026-05-03-frontend-design-pass-design.md`](docs/superpowers/specs/2026-05-03-frontend-design-pass-design.md) — a hybrid of option A (per-area hue + icon + subtle depth) executed with option B's restraint (muted earth-tone palette, serif title face, hairline rules).

## What changed

**Tokens (`frontend/src/index.css`):**
- 8 per-area accent triples (`--area-{name}-accent[-tint|-text]`) for both light and dark modes.
- `--font-serif` stack — New York with Georgia fallback.
- `.surface-card` layer-class with the subtle top→bottom gradient (highlight in dark, shadow in light).

**New shared components:**
- `PageHeader` — serif title + accent bar.
- `StatusPill` — 5 states (active/running/idle/error/pending) with glyph + state-tinted bg.
- `IconTile` — colored rounded-square tile for category indicators.
- `Card` — wrapper around `.surface-card` with ref forwarding.
- `documentTypeIcon` util — pattern-matched LLM doc_type → glyph + hue (~80% coverage on the live corpus's top doc_types).
- `topicDotColor` util — FNV-1a hash mod 8 → area-accent hue for conversation dots.
- `useAreaAccent` hook — binds the active route's hue to `--area-accent` on the layout root.

**Per-page sweeps:** every route gets the new PageHeader + StatusPill + IconTile + Card treatment with its area's accent — Ask (terracotta), Research (ochre), Folders (warm khaki), Documents (dusty blue), Explore (mauve), Search (dusty teal), Observatory (sage), Settings hub + 12 subpages (slate). LoginPage + SetupPage + OnboardingWizard also covered.

## What didn't change

Per the spec, these are working today and stay untouched:
- Entity-type colors in DocumentDetail (`ENTITY_TYPE_COLORS`).
- Observatory chart colors (Top Entities, Topic Distribution treemap, Entity Network graph).
- Research page octopus illustration + amber Start Research CTA.
- Documents page "Continue viewing" affordance + filter row layout.
- All backend behavior, schemas, API surfaces, MCP tools, ingestion pipeline.

## Out of scope (captured in `pr_followups.md`)

Per spec — motion / micro-interactions, KPI trend deltas, topic-aware conversation dot, webfont fallback for Docker-served SPA, login brand mark, per-page background atmosphere, Tailwind v4 token migration audit, document-type icon coverage rescan after each new test corpus.

## Validation

- `npm run type-check` ✓
- `npm run lint` ✓
- `npm run format:check` ✓
- `npm run build` ✓
- Manual smoke walk through every page in both dark and light modes — see plan task 21 step 2.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Capture out-of-scope items in `pr_followups.md`**

After the PR opens, append the following to `~/.claude/projects/-Users-alex-mcp-gateway/memory/pr_followups.md` under an appropriate category (or create new categories). The exact PR number is inserted after `gh pr create` returns the URL:

```markdown
## UX (frontend)

- **PR #<NUM>** — Frontend design pass: motion / micro-interactions deferred. No hover-lift animations, no animated chart entries. Worth a follow-up pass once the static design lands.
- **PR #<NUM>** — Frontend design pass: KPI trend deltas (the "+47 today" line under Observatory KPIs) deferred. Requires backend support for time-windowed counts.
- **PR #<NUM>** — Frontend design pass: topic-aware conversation dot color deferred. v1 ships with hash-of-id; smarter "color by detected topic / top entity" needs a backend join or client-side derivation.
- **PR #<NUM>** — Frontend design pass: webfont fallback for Docker-served SPA deferred. Georgia fallback acceptable for v1; a Source Serif Pro download is a follow-up.
- **PR #<NUM>** — Frontend design pass: login page brand mark / illustration deferred. Page is now slightly less plain (gradient + serif) but still doesn't add identity.
- **PR #<NUM>** — Frontend design pass: per-page background atmosphere (option C from brainstorming) explicitly deferred. Revisit if the muted-accent approach feels too subtle in practice.

## Cleanup

- **PR #<NUM>** — Frontend design pass: Tailwind v4 design-token migration audit deferred. This pass adds CSS vars; a follow-up could move all color tokens onto the `@theme` block uniformly.

## Coverage / data quality

- **PR #<NUM>** — Frontend design pass: document-type icon coverage rescan needed after each new test corpus is ingested. Snapshot SQL + protocol live in `project_test_corpora_plan.md`. Trigger: any future corpus dropping top-50 coverage below ~75%.
```

- [ ] **Step 6: Final commit**

If you appended to `pr_followups.md`, no commit needed — that file is in `~/.claude` not the repo. Otherwise, the PR's commit history is the final state.

```bash
echo "Done. PR URL above."
```

---

## Self-review notes (do these checks before handing off)

After every task lands, before calling the plan complete:

1. **Spec coverage:** every "Per-page changes" entry in the spec maps to a task above. Verified:
   - Ask → Task 19 ✓
   - Research → Task 18 ✓
   - Folders → Task 14 ✓
   - Documents → Task 12 ✓
   - DocumentDetail → Task 13 ✓
   - Search → Task 16 ✓
   - Explore → Task 15 ✓
   - Observatory → Task 17 ✓
   - System Settings hub → Task 10 ✓
   - All Settings subpages → Tasks 10 + 11 ✓
   - DocumentDetail (entity colors stay) → Task 13 ✓
   - Languages → Task 11 ✓
   - Login → Task 20 ✓
   - Onboarding wizard → Task 20 ✓

2. **Tokens used consistently:** every page uses `var(--area-accent)` etc., never hard-coded hex. Verified by spec → tasks.

3. **No new dependencies:** confirmed — every component uses React + Tailwind v4 only.

4. **No backend changes:** confirmed — spec is CSS/JSX-only and plan tasks all touch `frontend/`.

5. **No tests added:** correct per spec ("project doesn't currently have a JS unit-test setup, and adding one is out of scope"). Validation = type-check + lint + format + manual smoke checklist.
