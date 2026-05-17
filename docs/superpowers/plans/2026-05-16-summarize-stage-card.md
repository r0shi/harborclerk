# Summarize stage card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the narrow `BACKGROUND · Summarize · N queued` footer strip on the Observatory pipeline diagram with a tall rounded-rectangle card that lives in the lower-left empty quadrant of the SVG, showing per-document progress, the running/queued split, and the active LLM model name.

**Architecture:** One backend change (extend `/api/chat/models/status` with a `model_name` field, sourced from the existing `MODELS` map) and one frontend change (rewrite the bottom of `PipelineGraph` in `PipelineDiagram.tsx` to drop the old strip and absolutely-position a new `<SummarizeCard>` inside a `relative` wrapper around the SVG). The summarize circle re-implements the ring + 1.6 s heartbeat-particle visual treatment inline so the two coordinate systems (main SVG vs mini-SVG inside the card) stay independent.

**Tech Stack:** Python 3.12 + FastAPI on the backend; React 19 + TypeScript + Tailwind v4 + inline SVG on the frontend. Backend tests run on pytest with the existing `client` / `admin_token` fixtures from `tests/conftest.py`. The frontend has no test runner today (per repo convention `tsc --noEmit` + ESLint cover correctness; manual browser verification covers UI).

**Spec:** [docs/superpowers/specs/2026-05-16-summarize-stage-card-design.md](../specs/2026-05-16-summarize-stage-card-design.md)

---

## File Structure

| File                                                       | Action  | Purpose                                                                                  |
|------------------------------------------------------------|---------|------------------------------------------------------------------------------------------|
| `src/harbor_clerk/api/routes/chat.py`                      | Modify  | `llm_status()` returns the new `model_name` field in all three response branches.        |
| `tests/api/test_chat_models_status.py`                     | Create  | Backend tests covering the four state×model combinations from the spec.                  |
| `frontend/src/hooks/useLLMStatus.ts`                       | Modify  | `LLMStatus` interface gains `model_name: string \| null`; initial state default updated. |
| `frontend/src/components/stats/PipelineDiagram.tsx`        | Modify  | Three changes: shift `NODE_POSITIONS` + `Ready` badge up 30 units; wrap SVG in `relative`; add `<SummarizeCard>` and remove old strip + dotted separator. |

All four changes land on the worktree branch `claude/thirsty-swanson-b37bfc`. Final PR opens against `main`.

---

## Task 1: Backend — add `model_name` to `/api/chat/models/status`

**Files:**
- Modify: `src/harbor_clerk/api/routes/chat.py:386-427`
- Test: `tests/api/test_chat_models_status.py` (new)

`get_model` is already imported at `src/harbor_clerk/api/routes/chat.py:36`. The change is purely additive — three return dicts each gain a `model_name` key sourced from a single helper expression.

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_chat_models_status.py`:

```python
"""Tests for /api/chat/models/status — focuses on the `model_name` field
added so the Observatory's summarize card can render the human-readable
LLM name without re-fetching the full model list.
"""

from unittest.mock import patch

import httpx
import pytest

from harbor_clerk.config import get_settings
from tests.conftest import auth_header


@pytest.fixture
def _set_llm_model_id(monkeypatch):
    """Set settings.llm_model_id for a single test.

    Also no-ops refresh_llm_settings so our monkey-patched value isn't
    overwritten by a real config.json on disk during the test.
    """

    def _set(model_id: str | None):
        settings = get_settings()
        monkeypatch.setattr(settings, "llm_model_id", model_id or "")
        monkeypatch.setattr("harbor_clerk.api.routes.chat.refresh_llm_settings", lambda: None)

    return _set


async def test_status_returns_model_name_for_known_model(
    client, admin_user, admin_token, _set_llm_model_id
):
    _set_llm_model_id("qwen3-8b")
    # Force the llama-server probe to fail so we exercise the "loading"
    # branch deterministically without needing a real llama-server.
    with patch.object(
        httpx.AsyncClient, "get", side_effect=httpx.ConnectError("boom")
    ):
        resp = await client.get(
            "/api/chat/models/status", headers=auth_header(admin_token)
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "loading"
    assert body["model_id"] == "qwen3-8b"
    assert body["model_name"] == "Qwen3 8B"


async def test_status_returns_null_model_name_when_deactivated(
    client, admin_user, admin_token, _set_llm_model_id
):
    _set_llm_model_id(None)
    resp = await client.get(
        "/api/chat/models/status", headers=auth_header(admin_token)
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "deactivated"
    assert body["model_id"] is None
    assert body["model_name"] is None


async def test_status_returns_null_model_name_for_unknown_model_id(
    client, admin_user, admin_token, _set_llm_model_id
):
    _set_llm_model_id("not-a-real-model-id")
    with patch.object(
        httpx.AsyncClient, "get", side_effect=httpx.ConnectError("boom")
    ):
        resp = await client.get(
            "/api/chat/models/status", headers=auth_header(admin_token)
        )

    assert resp.status_code == 200
    body = resp.json()
    # The id is preserved as the source of truth; only the human label is null.
    assert body["model_id"] == "not-a-real-model-id"
    assert body["model_name"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/api/test_chat_models_status.py -v`

Expected: FAIL — `KeyError: 'model_name'` on each assertion (the response dict does not yet contain that key).

- [ ] **Step 3: Implement the change**

Edit `src/harbor_clerk/api/routes/chat.py`, replacing the body of `llm_status` (lines 406-427) with this version. Only the three `return {...}` dicts are touched; the rest is unchanged.

```python
    # See list_available_models — refresh to catch menubar-side writes
    # to config.json that bypass this process.
    refresh_llm_settings()
    settings = get_settings()
    model_id = settings.llm_model_id or None
    info = get_model(model_id) if model_id else None
    model_name = info.name if info else None

    if not model_id:
        return {"state": "deactivated", "model_id": None, "model_name": None}

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{settings.llama_server_url}/health")
            if r.status_code == 200:
                return {
                    "state": "ready",
                    "model_id": model_id,
                    "model_name": model_name,
                }
            # Non-200 — model still coming up (llama-server returns
            # 503 during model load) or in some other transient state.
            return {
                "state": "loading",
                "model_id": model_id,
                "model_name": model_name,
            }
    except (httpx.ConnectError, httpx.TimeoutException):
        # Connection refused (server stopped or restarting) or didn't
        # answer within 2s (loading weights, mmap'ing the gguf, etc.).
        return {
            "state": "loading",
            "model_id": model_id,
            "model_name": model_name,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/api/test_chat_models_status.py -v`

Expected: PASS — all three test cases green.

- [ ] **Step 5: Run lint + format**

Run: `uv run ruff check src/harbor_clerk/api/routes/chat.py tests/api/test_chat_models_status.py && uv run ruff format --check src/harbor_clerk/api/routes/chat.py tests/api/test_chat_models_status.py`

Expected: clean. If format fails, run `uv run ruff format src/harbor_clerk/api/routes/chat.py tests/api/test_chat_models_status.py` and re-check.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/api/routes/chat.py tests/api/test_chat_models_status.py
git commit -m "feat(api): add model_name to /api/chat/models/status

Exposes the human-readable LLM display name alongside the existing model_id
so frontend consumers (Observatory summarize card) don't need to fetch the
full models list to label what's running. Field is null when no model is
configured or when the configured id isn't in the curated MODELS map."
```

---

## Task 2: Frontend — extend `LLMStatus` interface with `model_name`

**Files:**
- Modify: `frontend/src/hooks/useLLMStatus.ts`

Purely a type extension + default update. No runtime branching changes.

- [ ] **Step 1: Add `model_name` to the interface and initial state**

Edit `frontend/src/hooks/useLLMStatus.ts`. Replace the interface (lines 6-9) with:

```ts
export interface LLMStatus {
  state: LLMState
  model_id: string | null
  model_name: string | null
}
```

Then update the `useState` initialiser at line 28:

```ts
  const [status, setStatus] = useState<LLMStatus>({
    state: 'unknown',
    model_id: null,
    model_name: null,
  })
```

No other lines in this file change — the existing `setStatus(data)` call at line 56 already forwards every field returned by the endpoint, so `model_name` flows through automatically once the type accepts it.

- [ ] **Step 2: Run type-check**

Run: `cd frontend && npm run type-check`

Expected: PASS. No call site uses `model_name` yet (Task 4 is the first consumer), so this is a clean additive change.

- [ ] **Step 3: Run lint**

Run: `cd frontend && npm run lint`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/hooks/useLLMStatus.ts
git commit -m "feat(frontend): add model_name to LLMStatus type

Tracks the new field returned by /api/chat/models/status. No call site
consumes it yet — that lands with the Observatory summarize card."
```

---

## Task 3: Frontend — shift the pipeline graph up 30 viewBox units

**Files:**
- Modify: `frontend/src/components/stats/PipelineDiagram.tsx:52-77, 327-329`

The spec calls for moving every stage's y coordinate up by 30 viewBox units so the SummarizeCard (added in Task 4) has more vertical headroom in the lower-left quadrant. The `Ready` badge text above Finalize also shifts.

This task is intentionally separate from Task 4 so the layout shift can be reviewed/reverted independently of the new card.

- [ ] **Step 1: Shift `NODE_POSITIONS` y values**

Edit `frontend/src/components/stats/PipelineDiagram.tsx`, replacing the `NODE_POSITIONS` constant (lines 52-59):

```ts
// Hand-laid-out positions for the 6-stage main flow.
// Sequential prefix flows left-to-right at y=130, then chunk fans out
// to entities/embed (two lanes at y=70 / y=190), then re-converges at
// finalize. Summarize runs separately as a BACKGROUND stage rendered
// in a card overlaying the lower-left empty quadrant — it is intentionally
// NOT part of the main flow so the diagram visually communicates "Ready
// is the terminus of the express line; summarize is independent."
const NODE_POSITIONS: Record<string, { x: number; y: number }> = {
  extract: { x: 60, y: 130 },
  ocr: { x: 160, y: 130 },
  chunk: { x: 260, y: 130 },
  entities: { x: 390, y: 70 },
  embed: { x: 390, y: 190 },
  finalize: { x: 520, y: 130 },
}
```

- [ ] **Step 2: Shift the `Ready` badge text**

In the same file, locate the `Ready` badge near the end of the SVG (currently `<text x={520} y={135} ...>Ready</text>` around line 327). Change `y={135}` to `y={105}`:

```tsx
        <text x={520} y={105} textAnchor="middle" fontSize={10} fill="#30d158" fontWeight={600}>
          Ready
        </text>
```

- [ ] **Step 3: Run type-check + lint**

Run: `cd frontend && npm run type-check && npm run lint`

Expected: PASS — these are constant-value changes only.

- [ ] **Step 4: Manual visual check**

Start dev: `cd frontend && npm run dev` (or use the running stack), open the Observatory page → Processing Pipeline tab. Verify the stages now sit visibly higher in the SVG with more empty space below — but the graph still reads as a single diagram (no clipping, no labels overlapping).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/stats/PipelineDiagram.tsx
git commit -m "refactor(observatory): shift pipeline stages up 30 viewBox units

Makes room in the lower-left quadrant for the upcoming summarize card.
Pure layout shift — same SVG height, same labels, same animations."
```

---

## Task 4: Frontend — add `<SummarizeCard>` and remove the old strip

**Files:**
- Modify: `frontend/src/components/stats/PipelineDiagram.tsx`

This is the big one. We're replacing the bottom 25 lines of `PipelineGraph`'s return value (the dotted separator + narrow purple strip) with a new component-scoped layout: wrap the SVG in a relative div, position `<SummarizeCard>` absolute bottom-left at 48% width, drop the old strip entirely.

The new component reads from the existing `useLLMStatus()` hook and from `snapshot.summarizing[]` which is already in the snapshot type. Inside the card, a mini inline SVG renders the same ring + heartbeat treatment as the main-flow stages.

- [ ] **Step 1: Import the LLM status context and `SummarizingRow`**

At the top of `frontend/src/components/stats/PipelineDiagram.tsx` add:

```ts
import { useLLMStatusContext } from '../LLMStatusBanner'
import { type LLMState } from '../../hooks/useLLMStatus'
import SummarizingRow from '../queue-tray/SummarizingRow'
```

We use `useLLMStatusContext()` (which reads from the `LLMStatusProvider` mounted in `Layout.tsx:180`) rather than calling `useLLMStatus()` directly — the raw hook spins up its own poll loop per consumer, and the existing project convention (see [DocumentsPage.tsx:154-156](../../../frontend/src/pages/DocumentsPage.tsx)) is to read from the shared context so the Observatory page doesn't double the `/api/chat/models/status` request rate.

- [ ] **Step 2: Add the `<SummarizeCard>` component**

Insert this new component above the existing `PipelineGraph` function (i.e. just below the imports / `EDGES` declarations, around line 116). The component takes the snapshot's summarize-related data plus the live LLM status and renders the card.

```tsx
const SUMMARIZE_COLOR = '#c4a4ff'

interface SummarizeCardProps {
  queued: number
  running: number
  summarizing: NonNullable<QueueSnapshot['summarizing']>
}

function modelLabel(state: LLMState, modelName: string | null, modelId: string | null): { text: string; muted: boolean; italic: boolean } {
  if (state === 'ready' && modelName) return { text: `Model · ${modelName}`, muted: false, italic: false }
  if (state === 'loading') {
    const label = modelName ?? modelId ?? 'loading…'
    return { text: `Model · ${label} (loading…)`, muted: true, italic: false }
  }
  if (state === 'deactivated') return { text: 'Model · not loaded', muted: true, italic: true }
  return { text: 'Model · …', muted: true, italic: false }
}

function SummarizeCircle({ queued, running }: { queued: number; running: number }) {
  // Same sqrt scale + heartbeat treatment as the main-flow nodes, in its
  // own 80×80 viewBox so the coordinate system stays decoupled.
  const total = queued + running
  const r = Math.min(26, 14 + Math.sqrt(total) * 2.5)
  const isBusy = running > 0
  const innerR = r * 0.55
  const innerRMin = innerR * 0.86
  const innerRMax = innerR * 1.04
  return (
    <svg viewBox="0 0 80 80" width={64} height={64} aria-hidden>
      <defs>
        <filter id="summarize-glow" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="2.5" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>
      <circle
        cx={40}
        cy={40}
        r={r}
        fill="none"
        stroke={SUMMARIZE_COLOR}
        strokeWidth={1.2}
        strokeOpacity={isBusy ? 0.75 : 0.45}
        style={{ transition: 'r 400ms ease-out, stroke-opacity 400ms' }}
      />
      {isBusy && (
        <circle cx={40} cy={40} fill={SUMMARIZE_COLOR} style={{ filter: 'url(#summarize-glow)' }}>
          <animate
            attributeName="r"
            values={`${innerRMin};${innerRMax};${innerRMin}`}
            dur="1.6s"
            repeatCount="indefinite"
            calcMode="spline"
            keySplines="0.4 0 0.6 1; 0.4 0 0.6 1"
          />
          <animate
            attributeName="opacity"
            values="0.78;1;0.78"
            dur="1.6s"
            repeatCount="indefinite"
            calcMode="spline"
            keySplines="0.4 0 0.6 1; 0.4 0 0.6 1"
          />
        </circle>
      )}
    </svg>
  )
}

function SummarizeCard({ queued, running, summarizing }: SummarizeCardProps) {
  const { status } = useLLMStatusContext()
  const model = modelLabel(status.state, status.model_name, status.model_id)

  // Running first (most-advanced map step on top), then queued in FIFO.
  // Backend already orders by created_at; we resort here just for the
  // running-first split.
  const sortedItems = [...summarizing].sort((a, b) => {
    if (a.status !== b.status) return a.status === 'running' ? -1 : 1
    if (a.status === 'running') return b.progress_current - a.progress_current
    return 0
  })
  const visible = sortedItems.slice(0, 4)
  const overflow = sortedItems.length - visible.length
  const hasItems = sortedItems.length > 0

  return (
    <div
      className="absolute bottom-0 left-0 w-[48%] rounded-xl p-3"
      style={{
        background: 'rgba(186,140,255,0.06)',
        border: '1px solid rgba(186,140,255,0.4)',
      }}
    >
      <div className="text-[9px] font-semibold tracking-wider" style={{ color: SUMMARIZE_COLOR }}>
        BACKGROUND
      </div>
      <div className="mt-1 flex gap-3">
        <div className="flex flex-col items-center" style={{ flex: '0 0 72px' }}>
          <SummarizeCircle queued={queued} running={running} />
          <div className="mt-1 text-[10px] font-medium" style={{ color: SUMMARIZE_COLOR }}>
            Summarize
          </div>
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline justify-between gap-2 text-[11px]">
            <span
              className="truncate"
              style={{
                color: SUMMARIZE_COLOR,
                opacity: model.muted ? 0.7 : 1,
                fontStyle: model.italic ? 'italic' : 'normal',
              }}
            >
              {model.text}
            </span>
            <span className="tabular-nums text-[10px] text-(--color-text-secondary)" style={{ flex: '0 0 auto' }}>
              {queued} queued · {running} running
            </span>
          </div>
          {hasItems && (
            <>
              <div
                className="mt-1.5 mb-1 border-t"
                style={{ borderColor: 'rgba(186,140,255,0.25)' }}
              />
              <div className="space-y-0">
                {visible.map((item) => (
                  <SummarizingRow key={item.doc_id} item={item} />
                ))}
                {overflow > 0 && (
                  <div className="pt-0.5 text-[10px] text-(--color-text-secondary)">
                    + {overflow} more
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 3: Wrap the SVG in a relative container and remove the old strip**

Locate the return block of `PipelineGraph` (currently lines 163-354). Replace the entire return statement with this version. The two changes are: (a) wrap `<svg>` in `<div className="relative">` plus mount `<SummarizeCard>` inside it; (b) delete the dotted-separator div and the old narrow purple strip.

```tsx
  return (
    <div className="relative">
      <svg
        viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
        className="w-full"
        style={{ aspectRatio: `${VIEW_WIDTH} / ${VIEW_HEIGHT}` }}
      >
        {/* ... entire existing SVG body unchanged: defs, edges, particles,
            nodes, "Ready" badge ... */}
      </svg>
      <SummarizeCard
        queued={snapshot.queues.llm?.queued ?? 0}
        running={snapshot.queues.llm?.running ?? 0}
        summarizing={snapshot.summarizing ?? []}
      />
    </div>
  )
```

**Important:** preserve the entire existing SVG content (defs / edges / particles / nodes / Ready badge) verbatim inside `<svg>…</svg>`. The diff is structural:
1. Outer `<div>` → `<div className="relative">`.
2. Add `<SummarizeCard …/>` as a sibling to `<svg>` (still inside the relative div).
3. Delete the dotted separator (`<div className="mt-2 border-t border-dashed …" />`).
4. Delete the existing BACKGROUND strip (`<div className="mt-2 flex items-center gap-3 …">` block).
5. Delete the now-unused `summarizeQueued` local (the new component reads from props).

- [ ] **Step 4: Run type-check + lint**

Run: `cd frontend && npm run type-check && npm run lint && npm run format:check`

Expected: PASS on all three. If format:check fails, run `npm run format` and re-check.

- [ ] **Step 5: Manual visual check — empty / idle state**

With no documents queued for summarization (`snapshot.queues.llm.queued === 0` and `snapshot.summarizing.length === 0`), open the Observatory → Processing Pipeline tab.

Expected:
- The new card sits at the lower-left of the SVG area, ~48% wide, with the purple-tint background.
- Inside the card: `BACKGROUND` label top-left; ring circle with `Summarize` label below it on the left column; `Model · <name>` and `0 queued · 0 running` on the right column.
- No item list, no divider.
- The old narrow `BACKGROUND · Summarize · N queued` strip is gone.

- [ ] **Step 6: Manual visual check — running state**

Ingest a small document (or wait for the next watched-folder pickup) so summarize actually runs. With at least one job in `running` and ideally one or two in `queued`:

Expected:
- The ring gets a pulsing inner purple particle (1.6 s heartbeat).
- `M queued · N running` reflects live counts.
- The item list shows the running doc with its `map N/M` label and an inline progress bar.
- Queued docs render with the `queued` label, no bar.
- If `summarizing.length > 4`, the 4th visible row is replaced by a `+ N more` row.

- [ ] **Step 7: Manual visual check — loading / deactivated states**

Toggle the active model:
- Hit `/api/chat/models/{id}/deactivate` (or use the Models page deactivate button) → card should show `Model · not loaded` muted italic.
- Activate a model → during the loading window (`~30–60s`) card should show `Model · <name> (loading…)` muted.
- After ready → `Model · <name>` solid.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/components/stats/PipelineDiagram.tsx
git commit -m "feat(observatory): summarize stage card with per-doc progress

Replaces the narrow BACKGROUND·Summarize footer strip with a tall
rounded-rectangle card overlaying the SVG's lower-left empty quadrant.
The card carries the same ring + heartbeat-particle treatment as the
main-flow stages, the active LLM model name, the running/queued split,
and a sorted list of up to four in-flight documents (running first by
map progress, then queued in FIFO). Reads from the existing snapshot
endpoint (summarizing[] is already populated) and useLLMStatus."
```

---

## Task 5: Final verification

**Files:** none modified.

Sanity sweep before opening the PR. Catches anything the per-task checks missed.

- [ ] **Step 1: Run the full backend test suite**

Run: `uv run pytest tests/ -x`

Expected: PASS — no regressions from the chat.py change.

- [ ] **Step 2: Run backend lint + format**

Run: `uv run ruff check . && uv run ruff format --check .`

Expected: clean.

- [ ] **Step 3: Run frontend full checks**

Run: `cd frontend && npm run type-check && npm run lint && npm run format:check`

Expected: clean.

- [ ] **Step 4: Verify the worktree state**

Run: `git status && git log --oneline main..HEAD`

Expected: four commits ahead of `main` (one per Task 1–4), no untracked files except the design + plan docs already committed.

- [ ] **Step 5: Open the PR**

Use the standard PR template — Summary points should reference the spec and call out the visual change. Test plan should be checkboxes for the four manual visual checks from Task 4 (steps 5–7) plus the backend test suite.

---

## Spec Coverage Check

| Spec section                            | Implementing task                |
|-----------------------------------------|----------------------------------|
| Container — relative wrapper + bubble   | Task 4 step 3                    |
| Remove old strip + dotted separator     | Task 4 step 3                    |
| Graph headroom — shift stages up 30u    | Task 3                           |
| Bubble internal layout                  | Task 4 step 2                    |
| Circle behaviour (ring + heartbeat)     | Task 4 step 2 (`SummarizeCircle`)|
| Model display + states                  | Task 4 step 2 (`modelLabel`) + Task 1 |
| Item list + sorting + overflow          | Task 4 step 2                    |
| Empty / loading / deactivated states    | Task 4 step 2 + manual checks 5–7|
| Backend `model_name` field              | Task 1                           |
| Frontend `LLMStatus` type extension     | Task 2                           |
| Backend test for `model_name`           | Task 1                           |
| Frontend render tests                   | **Deferred** — frontend has no test runner today; manual checks substitute per project convention |
