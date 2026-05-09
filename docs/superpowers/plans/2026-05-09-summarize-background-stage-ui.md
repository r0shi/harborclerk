# Summarize-as-Background-Stage UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface "summarize is a non-blocking side-channel" across all four UI surfaces it touches: per-doc row chip, queue tray section + pill state, Observatory pipeline diagram, Observatory timing chart + Summary Backlog widget.

**Architecture:** Backend ships small API additions (one new derived field, one new array on the queue snapshot, one new endpoint, plus map-reduce progress on the existing `IngestionJob` row). Frontend adds three new components (`SummaryChip`, `SummarizingRow`, `SummaryBacklogPanel`), drops `summarize` from existing pipeline-stage constants in two places, and modifies the existing PipelineDiagram + DocumentsPage row layout. All four surfaces share the same backend additions.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async (backend), React 19 + TypeScript + Tailwind 4 (frontend), `@tanstack/react-virtual` (virtualization, already installed), pytest (backend tests), `tsc --noEmit` + ESLint + Prettier (frontend verification — no jest/vitest in this codebase).

**Spec:** [`docs/superpowers/specs/2026-05-09-summarize-background-stage-ui-design.md`](../specs/2026-05-09-summarize-background-stage-ui-design.md)

**Backend prerequisite:** PR #327 already merged. `summarize` is in `_BACKGROUND_STAGES`; doc reaches `ready` without waiting on summarize.

---

## File map

| File | Type | Responsibility |
|--|--|--|
| `src/harbor_clerk/api/schemas/documents.py` | Modify | Add `summarize_job_status` to `DocumentSummary` |
| `src/harbor_clerk/api/routes/documents.py` | Modify | Populate `summarize_job_status` in the doc list response |
| `src/harbor_clerk/api/schemas/system.py` | Modify | Add `SummaryBacklogResponse` schema |
| `src/harbor_clerk/api/routes/system.py` | Modify | Add `/system/summary-backlog` endpoint |
| `src/harbor_clerk/api/routes/jobs.py` | Modify | Add `summarizing[]` array to queue snapshot |
| `src/harbor_clerk/worker/stages/summarize.py` | Modify | Update `progress_current/total` on the IngestionJob row as map-reduce progresses |
| `src/harbor_clerk/llm/summarize.py` | Modify | Accept a `progress_callback` so the worker can update the row between map calls |
| `tests/test_documents.py` | Modify | Test `summarize_job_status` field on doc list |
| `tests/test_system.py` | Modify | Test `/system/summary-backlog` endpoint |
| `tests/test_summarize.py` | Modify | Test `progress_callback` is called between map iterations |
| `frontend/src/components/SummaryChip.tsx` | Create | 5-state chip (Generating / Pending / Extractive Only / Summarized / Failed) |
| `frontend/src/pages/DocumentsPage.tsx` | Modify | Wire `SummaryChip` into row grid; fixed 170px chip column |
| `frontend/src/hooks/useQueueTray.ts` | Modify | Drop summarize from `PIPELINE_STAGES`; fetch `summarizing[]`; expose `summarizeBacklog` count |
| `frontend/src/components/queue-tray/SummarizingRow.tsx` | Create | Single-track purple progress bar row |
| `frontend/src/components/queue-tray/QueuePanel.tsx` | Modify | Add Summarizing section between Active and Completed |
| `frontend/src/components/queue-tray/QueuePill.tsx` | Modify | Add Summarizing rest state with priority logic |
| `frontend/src/components/stats/PipelineDiagram.tsx` | Modify | Drop summarize from main canvas; render BACKGROUND panel |
| `frontend/src/components/stats/PipelineTimingChart.tsx` | Modify | Remove `summarize` from `STAGES` |
| `frontend/src/components/stats/SummaryBacklogPanel.tsx` | Create | Throughput · p50 · composite In Queue + sparkline |
| `frontend/src/pages/ObservatoryPage.tsx` | Modify | Render `SummaryBacklogPanel` below `PipelineTimingChart` |

---

## Phase 0 — Backend foundations

### Task 1: Add `summarize_job_status` to doc list response

**Why:** `SummaryChip` needs to know whether the latest summarize job is queued, running, done, or errored. Without this field the chip can only distinguish presence/absence of `doc.summary` — not "running" vs "queued" vs "failed".

**Files:**
- Modify: `src/harbor_clerk/api/schemas/documents.py:20-35` (`DocumentSummary`)
- Modify: `src/harbor_clerk/api/routes/documents.py:127-144` (the loop that builds `summaries`)
- Modify: `tests/test_documents.py` (find the existing list-docs test and extend it)

- [ ] **Step 1: Read the existing test file to understand patterns**

```bash
grep -n "def test_.*list\|/api/docs" tests/test_documents.py | head -10
```

- [ ] **Step 2: Write a failing test for `summarize_job_status`**

In `tests/test_documents.py`, add this test next to the other doc-list tests:

```python
@pytest.mark.asyncio
async def test_list_docs_includes_summarize_job_status(
    db_session, async_client, admin_principal_headers
):
    """Doc list rows expose the latest summarize-stage job status so the
    frontend SummaryChip can pick its display state."""
    from harbor_clerk.models import Document, IngestionJob
    from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus

    doc = Document(
        title="Status carrier",
        canonical_filename="status.pdf",
        status="active",
        sha256=b"s" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    db_session.add(
        IngestionJob(
            doc_id=doc.doc_id,
            stage=JobStage.summarize,
            status=JobStatus.running,
        )
    )
    await db_session.commit()

    response = await async_client.get("/api/docs", headers=admin_principal_headers)
    assert response.status_code == 200
    data = response.json()
    row = next(r for r in data["items"] if r["doc_id"] == str(doc.doc_id))
    assert row["summarize_job_status"] == "running"
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
uv run pytest tests/test_documents.py::test_list_docs_includes_summarize_job_status -v
```

Expected: FAIL — `KeyError: 'summarize_job_status'` or pydantic ValidationError.

- [ ] **Step 4: Add the field to the schema**

In `src/harbor_clerk/api/schemas/documents.py`, modify the `DocumentSummary` class to add one line:

```python
class DocumentSummary(BaseModel):
    doc_id: str
    title: str
    canonical_filename: str | None = None
    status: str
    pipeline_status: str | None = None
    created_at: datetime
    updated_at: datetime
    summary: str | None = None
    summary_model: str | None = None
    doc_type: str | None = None
    source_path: str | None = None
    topic_id: int | None = None
    watch_source_path: str | None = None
    watch_status: str | None = None
    folder_name: str | None = None
    summarize_job_status: str | None = None  # NEW
```

- [ ] **Step 5: Populate the field in the route**

In `src/harbor_clerk/api/routes/documents.py`, replace the `summaries.append(...)` block (around line 127-144) and the surrounding logic. Find this block:

```python
result = await session.execute(query)
docs = result.scalars().all()

summaries = []
for doc in docs:
    summaries.append(
        DocumentSummary(
            doc_id=str(doc.doc_id),
            title=doc.title,
            ...
        )
    )
```

Add a single batched query for the latest summarize-stage job per doc, and pass its status into each `DocumentSummary`:

```python
result = await session.execute(query)
docs = result.scalars().all()

# Batch-fetch the latest summarize job status for every doc in this page.
# DISTINCT ON keeps only the most recent row per doc.
sum_job_status_by_doc: dict[str, str] = {}
if docs:
    doc_ids = [d.doc_id for d in docs]
    rows = await session.execute(
        select(IngestionJob.doc_id, IngestionJob.status)
        .where(IngestionJob.doc_id.in_(doc_ids))
        .where(IngestionJob.stage == JobStage.summarize)
        .order_by(IngestionJob.doc_id, IngestionJob.created_at.desc())
        .distinct(IngestionJob.doc_id)
    )
    for did, jst in rows.all():
        sum_job_status_by_doc[str(did)] = jst.value if hasattr(jst, "value") else str(jst)

summaries = []
for doc in docs:
    summaries.append(
        DocumentSummary(
            doc_id=str(doc.doc_id),
            title=doc.title,
            canonical_filename=doc.canonical_filename,
            status=doc.status,
            pipeline_status=doc.pipeline_status.value if doc.pipeline_status else None,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
            summary=doc.summary,
            summary_model=doc.summary_model,
            doc_type=doc.doc_type,
            source_path=doc.source_path,
            topic_id=doc.topic_id,
            summarize_job_status=sum_job_status_by_doc.get(str(doc.doc_id)),  # NEW
        )
    )
```

Also add the imports if missing (top of file):

```python
from harbor_clerk.models import IngestionJob
from harbor_clerk.models.enums import JobStage
```

- [ ] **Step 6: Run the test to verify it passes**

```bash
uv run pytest tests/test_documents.py::test_list_docs_includes_summarize_job_status -v
```

Expected: PASS.

- [ ] **Step 7: Run the full doc test suite to verify nothing regressed**

```bash
uv run pytest tests/test_documents.py -q
```

Expected: all green.

- [ ] **Step 8: Lint + format**

```bash
uv run ruff check src/harbor_clerk/api/ && uv run ruff format src/harbor_clerk/api/
```

- [ ] **Step 9: Commit**

```bash
git add src/harbor_clerk/api/schemas/documents.py src/harbor_clerk/api/routes/documents.py tests/test_documents.py
git commit -m "feat(api): add summarize_job_status to doc list response

Frontend SummaryChip needs the latest summarize-stage job status to
distinguish queued/running/error from terminal states. Batch-fetch via
DISTINCT ON to keep the page query at O(1) extra queries."
```

---

### Task 2: Add `summarizing[]` array to queue snapshot

**Why:** The queue tray's new Summarizing section needs the live list of in-flight summarize jobs with per-job map-reduce progress. The QueuePill needs the backlog count.

**Files:**
- Modify: `src/harbor_clerk/api/routes/jobs.py` (find the queue snapshot endpoint)
- Test: `tests/test_jobs_api.py` if it exists, otherwise extend an existing test file

- [ ] **Step 1: Find the queue snapshot endpoint**

```bash
grep -nE "queue.*snapshot|@router.*queue|stages.*queued.*running" src/harbor_clerk/api/routes/jobs.py | head
```

Look for the function that returns `{"io": {...}, "cpu": {...}, "llm": {...}}`.

- [ ] **Step 2: Read the current shape of the response**

```bash
grep -B2 -A 20 "def.*queue.*status\|/jobs/queue\|summary.*status_breakdown" src/harbor_clerk/api/routes/jobs.py | head -50
```

- [ ] **Step 3: Write a failing test for the `summarizing` array**

In `tests/test_jobs_api.py` (create if missing — pattern from `tests/test_documents.py`):

```python
import pytest
from sqlalchemy import select

from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus


@pytest.mark.asyncio
async def test_queue_snapshot_includes_summarizing_array(
    db_session, async_client, admin_principal_headers
):
    """Queue snapshot exposes in-flight summarize jobs as a separate
    `summarizing` array so the frontend can render them in their own
    section."""
    doc = Document(
        title="Summary in flight",
        canonical_filename="sum.pdf",
        status="active",
        sha256=b"s" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    db_session.add(
        IngestionJob(
            doc_id=doc.doc_id,
            stage=JobStage.summarize,
            status=JobStatus.running,
            progress_current=3,
            progress_total=9,
        )
    )
    await db_session.commit()

    response = await async_client.get("/api/jobs/queue", headers=admin_principal_headers)
    assert response.status_code == 200
    data = response.json()
    assert "summarizing" in data
    assert len(data["summarizing"]) == 1
    job = data["summarizing"][0]
    assert job["doc_id"] == str(doc.doc_id)
    assert job["filename"] == "sum.pdf"
    assert job["status"] == "running"
    assert job["progress_current"] == 3
    assert job["progress_total"] == 9
```

- [ ] **Step 4: Run the test to verify it fails**

```bash
uv run pytest tests/test_jobs_api.py::test_queue_snapshot_includes_summarizing_array -v
```

Expected: FAIL — `assert 'summarizing' in data`.

- [ ] **Step 5: Add the `summarizing` array to the snapshot endpoint**

In `src/harbor_clerk/api/routes/jobs.py`, locate the queue snapshot handler (it returns a dict with `io`, `cpu`, `llm` keys per the line `"llm": {"queued": 0, "running": 0}` we saw earlier at line 176). Append the new array. Add this near the existing queue handler:

```python
# After the existing per-queue counts dict is assembled — typically
# right before `return {"io": ..., "cpu": ..., "llm": ...}` — fetch
# the in-flight summarize jobs themselves (not just counts) so the
# queue tray can render per-doc rows with map-reduce progress.

summarizing_rows = (
    await session.execute(
        select(IngestionJob, Document)
        .join(Document, IngestionJob.doc_id == Document.doc_id)
        .where(IngestionJob.stage == JobStage.summarize)
        .where(IngestionJob.status.in_((JobStatus.queued, JobStatus.running)))
        .order_by(IngestionJob.created_at)
    )
).all()

summarizing = [
    {
        "doc_id": str(job.doc_id),
        "filename": doc.canonical_filename or doc.title,
        "status": job.status.value,
        "progress_current": job.progress_current or 0,
        "progress_total": job.progress_total or 0,
        "created_at": job.created_at.isoformat(),
    }
    for job, doc in summarizing_rows
]
```

Add `summarizing` to the response dict — for example, if the existing return is:

```python
return {"io": io_counts, "cpu": cpu_counts, "llm": llm_counts}
```

change it to:

```python
return {
    "io": io_counts,
    "cpu": cpu_counts,
    "llm": llm_counts,
    "summarizing": summarizing,
}
```

If the schema uses a Pydantic response model, also add the new field there.

- [ ] **Step 6: Run the test to verify it passes**

```bash
uv run pytest tests/test_jobs_api.py::test_queue_snapshot_includes_summarizing_array -v
```

Expected: PASS.

- [ ] **Step 7: Lint + format**

```bash
uv run ruff check src/harbor_clerk/api/ && uv run ruff format src/harbor_clerk/api/
```

- [ ] **Step 8: Commit**

```bash
git add src/harbor_clerk/api/routes/jobs.py tests/test_jobs_api.py
git commit -m "feat(api): expose in-flight summarize jobs on queue snapshot

Adds a 'summarizing' array to /api/jobs/queue with per-doc
filename + map-reduce progress counters. Powers the new Summarizing
section in the queue tray."
```

---

### Task 3: Worker — update `progress_current/total` during map-reduce

**Why:** The Summarizing rows render a progress bar fed by `progress_current / progress_total` on the `IngestionJob` row. Without worker updates the bar stays at 0 the whole time.

**Files:**
- Modify: `src/harbor_clerk/llm/summarize.py` (`_summarize_long`, `generate_summary`)
- Modify: `src/harbor_clerk/worker/stages/summarize.py` (`run_summarize`)
- Test: `tests/test_summarize.py`

- [ ] **Step 1: Read the current `_summarize_long` signature**

```bash
grep -n "def _summarize_long\|def generate_summary" src/harbor_clerk/llm/summarize.py
```

PR #327 already added `yield_check: Callable[[], None] | None = None` — we'll add a sibling `progress_callback`.

- [ ] **Step 2: Write a failing test for `progress_callback`**

In `tests/test_summarize.py`, find the existing tests for `_summarize_long` (or `generate_summary`). Add:

```python
def test_summarize_long_calls_progress_callback_per_map_step(monkeypatch):
    """Map-reduce summarize must report progress between sub-calls so the
    queue tray's progress bar can fill in chunks."""
    from harbor_clerk.llm import summarize as sum_mod

    # Stub _call_llm so each map call returns a fixed string and the
    # reduce returns the joined result. We don't actually hit a model.
    def fake_call_llm(*args, **kwargs):
        return "section-summary"

    monkeypatch.setattr(sum_mod, "_call_llm", fake_call_llm)

    # Force long-tier path: > _LONG_THRESHOLD chunks worth of input.
    chunks = ["x" * 8000 for _ in range(150)]

    progress_calls: list[tuple[int, int]] = []

    def record(current: int, total: int) -> None:
        progress_calls.append((current, total))

    sum_mod._summarize_long(chunks, max_input_chars=80_000, progress_callback=record)

    # Expect at least one progress update per map call + one before reduce.
    # Specifically: total should be the same on every call (number of
    # groups + 1 for reduce); current should monotonically increase.
    assert len(progress_calls) >= 2
    totals = {t for _, t in progress_calls}
    assert len(totals) == 1, f"total varied: {totals}"
    currents = [c for c, _ in progress_calls]
    assert currents == sorted(currents), "current must be monotonic"
    assert currents[-1] == currents[0] + len(progress_calls) - 1
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
uv run pytest tests/test_summarize.py::test_summarize_long_calls_progress_callback_per_map_step -v
```

Expected: FAIL — `_summarize_long()` doesn't accept `progress_callback`.

- [ ] **Step 4: Add `progress_callback` to `_summarize_long`**

In `src/harbor_clerk/llm/summarize.py`, modify `_summarize_long`:

```python
def _summarize_long(
    chunks: list[str],
    max_input_chars: int,
    *,
    yield_check: Callable[[], None] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> str | None:
    """Long docs: map-reduce — group summaries then final summary.

    `yield_check`, if provided, is called before each LLM sub-call so the
    summarize worker can yield to interactive chat/research workloads
    between map iterations and before the reduce step.

    `progress_callback`, if provided, is called with (chunks_done,
    total_chunks) before each LLM sub-call so the worker can update its
    IngestionJob row's progress counters. total_chunks counts each map
    call plus the reduce as one chunk each.
    """
    groups = _group_chunks_for_mapreduce(chunks, max_input_chars)
    logger.info("Map-reduce summarization: %d groups from %d chunks", len(groups), len(chunks))

    total_chunks = len(groups) + 1  # +1 for the reduce step
    section_summaries: list[str] = []
    for idx, group in enumerate(groups):
        if yield_check is not None:
            yield_check()
        if progress_callback is not None:
            progress_callback(idx, total_chunks)
        result = _call_llm(
            _PROMPT_MAP,
            group,
            max_tokens=300,
            timeout=90.0,
            max_attempts=2,
            response_key="summary",
            json_schema=_SUMMARY_SCHEMA,
            phase=f"summarize-map[{idx + 1}/{len(groups)}]",
        )
        if result:
            section_summaries.append(result[:300])
        else:
            snippet = group[:200].strip()
            if snippet:
                section_summaries.append(snippet)

    if not section_summaries:
        return None

    if yield_check is not None:
        yield_check()
    if progress_callback is not None:
        progress_callback(len(groups), total_chunks)
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(section_summaries))
    reduce_input = numbered[:max_input_chars]
    return _call_llm(
        _PROMPT_REDUCE,
        reduce_input,
        max_tokens=500,
        timeout=120.0,
        response_key="summary",
        json_schema=_SUMMARY_SCHEMA,
        phase="summarize-reduce",
    )
```

Also plumb the parameter through `generate_summary`:

```python
def generate_summary(
    chunks: list[str],
    max_chars: int | None = None,
    *,
    yield_check: Callable[[], None] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[str, str]:
    # ... existing code ...
    # In the tier dispatch, pass progress_callback to _summarize_long:
    else:
        result = _summarize_long(
            chunks,
            max_input_chars,
            yield_check=yield_check,
            progress_callback=progress_callback,
        )
```

(Short and Medium tiers don't use map-reduce; no progress callback needed there. The bar stays at 0% with pulse, then snaps to 100% on completion.)

- [ ] **Step 5: Run the test to verify it passes**

```bash
uv run pytest tests/test_summarize.py::test_summarize_long_calls_progress_callback_per_map_step -v
```

Expected: PASS.

- [ ] **Step 6: Wire it into the worker stage**

In `src/harbor_clerk/worker/stages/summarize.py`, replace the existing call to `generate_summary` with a version that updates the row:

```python
def run_summarize(doc_id: uuid.UUID) -> None:
    """Generate a summary for the document from all its chunks."""
    _wait_for_idle_llm()

    if not mark_stage_running(doc_id, JobStage.summarize):
        return

    refresh_llm_settings()

    session = get_sync_session()
    try:
        chunks = (
            session.execute(select(Chunk.chunk_text).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_num))
            .scalars()
            .all()
        )

        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        worker_seq = doc.pipeline_seq

        if chunks:
            # Update the IngestionJob row's progress counters between map calls
            # so the queue tray's progress bar fills as the summary advances.
            def _progress(current: int, total: int) -> None:
                inner_session = get_sync_session()
                try:
                    inner_session.execute(
                        update(IngestionJob)
                        .where(IngestionJob.doc_id == doc_id)
                        .where(IngestionJob.stage == JobStage.summarize)
                        .values(progress_current=current, progress_total=total)
                    )
                    inner_session.commit()
                finally:
                    inner_session.close()

            try:
                summary, model_used = generate_summary(
                    list(chunks),
                    yield_check=_wait_for_idle_llm,
                    progress_callback=_progress,
                )
            except Exception:
                logger.warning("Summary generation failed for %s", doc_id, exc_info=True)
                summary, model_used = None, None

            # ... rest unchanged: classify_doc_type, race check, write results ...
```

Imports at the top of the file:

```python
from sqlalchemy import select, update
from harbor_clerk.models import ChatMessage, Chunk, Document, IngestionJob
```

- [ ] **Step 7: Lint + format**

```bash
uv run ruff check src/harbor_clerk/ && uv run ruff format src/harbor_clerk/
```

- [ ] **Step 8: Run the full backend test suite**

```bash
uv run pytest tests/ -x -q
```

Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add src/harbor_clerk/llm/summarize.py src/harbor_clerk/worker/stages/summarize.py tests/test_summarize.py
git commit -m "feat(summarize): map-reduce progress callback updates IngestionJob row

Long-tier summaries are slow — they need to surface mid-flight progress
in the queue tray. progress_callback is invoked before each map call
and before the reduce step; the worker writes (current, total) to the
IngestionJob row so the queue tray's purple bar fills in chunks as the
summary advances."
```

---

### Task 4: New `/system/summary-backlog` endpoint

**Why:** Observatory's Summary Backlog widget needs four numbers: current depth, throughput per minute, p50 wall time, and a depth-over-time series for the sparkline.

**Files:**
- Modify: `src/harbor_clerk/api/schemas/system.py` (or wherever system schemas live)
- Modify: `src/harbor_clerk/api/routes/system.py`
- Test: `tests/test_system.py`

- [ ] **Step 1: Find the system schemas + routes**

```bash
ls src/harbor_clerk/api/schemas/system.py 2>&1; grep -n "^class\|@router\.get.*system" src/harbor_clerk/api/routes/system.py | head -20
```

If `schemas/system.py` doesn't exist, define the response model inline in the route file (look at how other system endpoints do it — `resummarize-all` for instance).

- [ ] **Step 2: Write a failing test for the endpoint shape**

In `tests/test_system.py`:

```python
@pytest.mark.asyncio
async def test_summary_backlog_endpoint_returns_all_four_fields(
    db_session, async_client, admin_principal_headers
):
    """The Observatory Summary Backlog widget needs depth, throughput,
    p50, and depth-over-time history. Endpoint must return all four."""
    response = await async_client.get(
        "/api/system/summary-backlog", headers=admin_principal_headers
    )
    assert response.status_code == 200
    data = response.json()
    assert "queue_depth" in data
    assert "throughput_per_min" in data
    assert "p50_seconds" in data
    assert "depth_history" in data
    assert isinstance(data["depth_history"], list)
    # Each history point is a (timestamp, depth) pair
    if data["depth_history"]:
        assert len(data["depth_history"][0]) == 2
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
uv run pytest tests/test_system.py::test_summary_backlog_endpoint_returns_all_four_fields -v
```

Expected: FAIL — 404.

- [ ] **Step 4: Add the endpoint**

In `src/harbor_clerk/api/routes/system.py`, append:

```python
@router.get("/system/summary-backlog")
async def get_summary_backlog(
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Stats for the Observatory Summary Backlog widget.

    - `queue_depth`: count of summarize jobs currently queued or running
    - `throughput_per_min`: completed summarize jobs / minute over the
      last 10 minutes
    - `p50_seconds`: median wall time of summarize jobs completed in the
      last 100 jobs
    - `depth_history`: [(unix_ts, depth), ...] sampled every 5 minutes
      over the last hour for the trend sparkline
    """
    now = datetime.now(UTC)

    queue_depth = (
        await session.execute(
            select(func.count())
            .select_from(IngestionJob)
            .where(IngestionJob.stage == JobStage.summarize)
            .where(IngestionJob.status.in_((JobStatus.queued, JobStatus.running)))
        )
    ).scalar() or 0

    ten_min_ago = now - timedelta(minutes=10)
    completed_in_window = (
        await session.execute(
            select(func.count())
            .select_from(IngestionJob)
            .where(IngestionJob.stage == JobStage.summarize)
            .where(IngestionJob.status == JobStatus.done)
            .where(IngestionJob.finished_at >= ten_min_ago)
        )
    ).scalar() or 0
    throughput_per_min = round(completed_in_window / 10.0, 2)

    # p50 over the last 100 completed summarize jobs
    durations = (
        await session.execute(
            select(
                func.extract("epoch", IngestionJob.finished_at - IngestionJob.started_at).label("dur")
            )
            .where(IngestionJob.stage == JobStage.summarize)
            .where(IngestionJob.status == JobStatus.done)
            .where(IngestionJob.started_at.is_not(None))
            .where(IngestionJob.finished_at.is_not(None))
            .order_by(IngestionJob.finished_at.desc())
            .limit(100)
        )
    ).scalars().all()
    if durations:
        sorted_d = sorted(float(d) for d in durations if d is not None)
        p50 = sorted_d[len(sorted_d) // 2]
    else:
        p50 = 0.0

    # Depth history: sample every 5 minutes over the last hour. We
    # approximate "depth at time T" as count(jobs created before T and
    # not finished before T).
    depth_history: list[tuple[float, int]] = []
    for offset_min in range(60, -1, -5):
        ts = now - timedelta(minutes=offset_min)
        d = (
            await session.execute(
                select(func.count())
                .select_from(IngestionJob)
                .where(IngestionJob.stage == JobStage.summarize)
                .where(IngestionJob.created_at <= ts)
                .where(
                    (IngestionJob.finished_at.is_(None))
                    | (IngestionJob.finished_at > ts)
                )
            )
        ).scalar() or 0
        depth_history.append((ts.timestamp(), int(d)))

    return {
        "queue_depth": int(queue_depth),
        "throughput_per_min": float(throughput_per_min),
        "p50_seconds": float(round(p50, 1)),
        "depth_history": depth_history,
    }
```

Add imports if missing:

```python
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from harbor_clerk.models import IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
uv run pytest tests/test_system.py::test_summary_backlog_endpoint_returns_all_four_fields -v
```

Expected: PASS.

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/harbor_clerk/api/ && uv run ruff format src/harbor_clerk/api/
git add src/harbor_clerk/api/routes/system.py tests/test_system.py
git commit -m "feat(api): /system/summary-backlog endpoint for Observatory widget

Returns queue_depth, throughput_per_min, p50_seconds, and a 5-min-
resolution depth_history series for the sparkline. Powers the
Summary Backlog panel below the Avg Pipeline Timing chart."
```

---

## Phase 1 — Per-doc chip (Surface 1)

### Task 5: `SummaryChip` component

**Why:** The 5-state chip is reused by DocumentsPage rows and would in principle suit the document detail page too (out of scope for this round but the abstraction is the same).

**Files:**
- Create: `frontend/src/components/SummaryChip.tsx`

- [ ] **Step 1: Create the file with the component**

```tsx
// frontend/src/components/SummaryChip.tsx

export type SummaryState =
  | 'generating'
  | 'pending'
  | 'extractive'
  | 'summarized'
  | 'failed'
  | 'none'

interface SummaryChipProps {
  state: SummaryState
}

const LABELS: Record<Exclude<SummaryState, 'none'>, string> = {
  generating: 'Summary Generating',
  pending: 'Summary Pending',
  extractive: 'Extractive Only',
  summarized: 'Summarized',
  failed: 'Summary Failed',
}

const STYLES: Record<Exclude<SummaryState, 'none'>, { bg: string; fg: string }> = {
  generating: { bg: 'rgba(186,140,255,0.16)', fg: '#c4a4ff' },
  pending: { bg: 'rgba(255,214,10,0.12)', fg: '#ffd60a' },
  extractive: { bg: 'rgba(255,159,10,0.14)', fg: '#ff9f0a' },
  summarized: { bg: 'rgba(48,209,88,0.13)', fg: '#30d158' },
  failed: { bg: 'rgba(255,69,58,0.13)', fg: '#ff453a' },
}

/**
 * Inline chip surfacing the latest summarize-stage state for a document.
 * Used in the DocumentsPage row's fixed 170px chip column. Renders nothing
 * for state="none" so callers can pass derived state and let layout
 * collapse naturally.
 *
 * Vertical alignment contract: when used in a fixed-width column with
 * `justify-self: start`, the dot's X position is constant across rows
 * so dots line up vertically. The chip's right edge is variable.
 */
export default function SummaryChip({ state }: SummaryChipProps) {
  if (state === 'none') return null
  const { bg, fg } = STYLES[state]
  const label = LABELS[state]
  const pulseDot = state === 'generating'
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-medium whitespace-nowrap"
      style={{ background: bg, color: fg, justifySelf: 'start' }}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${pulseDot ? 'animate-pulse' : ''}`}
        style={{ background: fg, flex: '0 0 auto' }}
      />
      {label}
    </span>
  )
}

/**
 * Resolve the SummaryState from the doc's persisted fields + latest
 * job status. Order matters — running/queued/error take precedence
 * over terminal-state inferences from `summary` / `summary_model`.
 */
export function resolveSummaryState(
  summary: string | null | undefined,
  summary_model: string | null | undefined,
  summarize_job_status: string | null | undefined,
): SummaryState {
  if (summarize_job_status === 'running') return 'generating'
  if (summarize_job_status === 'queued') return 'pending'
  if (summarize_job_status === 'error') return 'failed'
  if (summary && summary.trim()) {
    if (summary_model === 'extractive') return 'extractive'
    return 'summarized'
  }
  return 'none'
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npm run type-check
```

Expected: no errors.

- [ ] **Step 3: Lint + format**

```bash
cd frontend && npm run lint -- src/components/SummaryChip.tsx && npm run format:check
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/SummaryChip.tsx
git commit -m "feat(frontend): SummaryChip component with 5-state resolver

Exports SummaryChip + resolveSummaryState() so DocumentsPage rows can
render the chip from raw doc fields without knowing the resolution
order. Pulse animation on the 'generating' state; nothing rendered
for state='none' so the column collapses cleanly."
```

---

### Task 6: Wire `SummaryChip` into DocumentsPage row

**Why:** Now that the component exists and the API returns `summarize_job_status`, the row needs a fixed 170px chip column where `Ready` aligns vertically across rows.

**Files:**
- Modify: `frontend/src/pages/DocumentsPage.tsx`

- [ ] **Step 1: Find the row layout**

```bash
grep -n "doc.title\|doc.canonical_filename\|pipeline_status\|StageBar" frontend/src/pages/DocumentsPage.tsx | head -10
```

Locate the doc row's grid/flex container. The current layout has title + pipeline pill + status text + timestamp. We're adding a 170px chip column.

- [ ] **Step 2: Add the type field for `summarize_job_status`**

Find the `Doc` (or `Document`) interface near the top of `DocumentsPage.tsx`. Add:

```typescript
interface Doc {
  // ... existing fields ...
  summary?: string
  summary_model?: string
  summarize_job_status?: string  // NEW
}
```

- [ ] **Step 3: Import the chip + resolver**

At the top of `DocumentsPage.tsx`:

```typescript
import SummaryChip, { resolveSummaryState } from '../components/SummaryChip'
```

- [ ] **Step 4: Update the row layout**

Find the row's container className. If it's flex, switch to grid with the fixed columns. Example diff (the exact location is around line 950 — adjust to match the current row markup):

```tsx
{/* Existing row, simplified shape: */}
<div className="grid items-center gap-3.5"
     style={{ gridTemplateColumns: '1fr 110px 70px 170px' }}>
  <span className="truncate font-medium">{doc.canonical_filename ?? doc.title}</span>
  <StageBar stages={stages} />
  <span className="text-xs text-(--color-text-secondary)">{statusLabel}</span>
  <SummaryChip state={resolveSummaryState(doc.summary, doc.summary_model, doc.summarize_job_status)} />
</div>
```

The `170px` column reserves space even when the chip is `state="none"` (which renders nothing), so empty rows still align with non-empty ones.

- [ ] **Step 5: Verify type-check + dev render**

```bash
cd frontend && npm run type-check
```

If you have the dev server running, navigate to `/docs` and visually verify:
1. Ready text aligns vertically across rows.
2. Chips line up on their dots.
3. Chip right edges vary (intentional).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/DocumentsPage.tsx
git commit -m "feat(frontend): SummaryChip on DocumentsPage rows in fixed 170px column

Row grid: 1fr | 110px pill | 70px status | 170px chip. Locks Ready
text and chip dot positions across rows; chip right margin is
intentionally uneven."
```

---

## Phase 2 — Pipeline-stages constant cleanup

### Task 7: Drop `summarize` from `PIPELINE_STAGES`

**Why:** The 7-segment StageBar should drop to 6 segments now that summarize lives in its own section. The constant is shared by `useQueueTray` and rendered by `StageBar`.

**Files:**
- Modify: `frontend/src/hooks/useQueueTray.ts:38`

- [ ] **Step 1: Find the constant**

```bash
grep -n "PIPELINE_STAGES" frontend/src/hooks/useQueueTray.ts frontend/src/components/queue-tray/StageBar.tsx
```

- [ ] **Step 2: Remove `'summarize'` from the array**

In `frontend/src/hooks/useQueueTray.ts:38`, change:

```typescript
export const PIPELINE_STAGES = ['extract', 'ocr', 'chunk', 'entities', 'embed', 'summarize', 'finalize']
```

to:

```typescript
export const PIPELINE_STAGES = ['extract', 'ocr', 'chunk', 'entities', 'embed', 'finalize']
```

The StageBar reads `PIPELINE_STAGES.length` and `PIPELINE_STAGES.map(...)`, so the bar drops from 7 to 6 segments automatically.

- [ ] **Step 3: Verify the existing `computeOverallProgress` / `computeCurrentStage` / `computeItemStatus` helpers don't special-case `summarize`**

```bash
grep -nE "summarize" frontend/src/hooks/useQueueTray.ts
```

If any line references `'summarize'` as a string literal (other than in PIPELINE_STAGES), evaluate whether it should be removed. Most likely there are none.

- [ ] **Step 4: Type-check**

```bash
cd frontend && npm run type-check
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/hooks/useQueueTray.ts
git commit -m "refactor(frontend): drop summarize from PIPELINE_STAGES (6-segment bar)

Pipeline pill now reflects the gating stages only — extract / ocr /
chunk / entities / embed / finalize. Summarize moves into its own
queue tray section in a follow-up task."
```

---

## Phase 3 — Queue tray Summarizing section (Surface 2)

### Task 8: `SummarizingRow` component

**Why:** The new Summarizing section's rows have a different visual treatment from Active rows — single-track purple progress bar instead of 6-segment colored bar.

**Files:**
- Create: `frontend/src/components/queue-tray/SummarizingRow.tsx`

- [ ] **Step 1: Create the component**

```tsx
// frontend/src/components/queue-tray/SummarizingRow.tsx

export interface SummarizingItem {
  doc_id: string
  filename: string
  status: 'queued' | 'running'
  progress_current: number
  progress_total: number
}

interface SummarizingRowProps {
  item: SummarizingItem
}

function progressLabel(item: SummarizingItem): string {
  if (item.status === 'queued') return 'queued'
  if (item.progress_total <= 0) return 'running'
  // Long-tier: show map step number. progress_total = map_count + 1.
  // progress_current ∈ [0, map_count] during map; == map_count during reduce.
  const mapCount = item.progress_total - 1
  if (item.progress_current >= mapCount) return 'reduce'
  return `map ${item.progress_current + 1}/${mapCount}`
}

/**
 * One row of the queue tray's Summarizing section. Shows the doc
 * filename, a single-track purple progress bar (no visible segments),
 * and a state label (queued / map N/M / reduce / running).
 *
 * Bar mechanics: width = progress_current / progress_total. Discrete
 * jumps as the worker updates the IngestionJob row mid-flight.
 */
export default function SummarizingRow({ item }: SummarizingRowProps) {
  const fraction =
    item.progress_total > 0 ? Math.min(1, item.progress_current / item.progress_total) : 0
  const isRunning = item.status === 'running'
  const label = progressLabel(item)

  return (
    <div className="flex items-center gap-2.5 py-1.5 text-[11px]">
      <span className="flex-1 truncate">{item.filename}</span>
      <span
        className="inline-block overflow-hidden rounded-sm align-middle"
        style={{
          width: 60,
          height: 4,
          background: 'rgba(186,140,255,0.16)',
          flex: '0 0 auto',
        }}
      >
        <span
          className={`block h-full rounded-sm transition-[width] duration-500 ease-out ${
            isRunning ? 'animate-pulse' : ''
          }`}
          style={{ width: `${fraction * 100}%`, background: '#c4a4ff' }}
        />
      </span>
      <span
        className="text-[10px] tabular-nums text-right"
        style={{ color: '#c4a4ff', minWidth: 60, flex: '0 0 auto' }}
      >
        {label}
      </span>
    </div>
  )
}
```

- [ ] **Step 2: Type-check + lint**

```bash
cd frontend && npm run type-check && npm run lint -- src/components/queue-tray/SummarizingRow.tsx
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/queue-tray/SummarizingRow.tsx
git commit -m "feat(frontend): SummarizingRow component for queue tray

Single-track purple progress bar (no visible segments). Width =
progress_current / progress_total, so the worker's map-reduce
updates fill the bar in discrete chunks. Pulse on running, static
on queued."
```

---

### Task 9: Extend `useQueueTray` to fetch `summarizing[]`

**Why:** The hook is the single source of truth for queue tray data. Add `summarizing` and `summarizeBacklog` to its return type and fetch logic.

**Files:**
- Modify: `frontend/src/hooks/useQueueTray.ts`

- [ ] **Step 1: Read the existing hook return shape**

```bash
grep -n "interface.*Tray\|return\b" frontend/src/hooks/useQueueTray.ts | head -10
```

Look for the hook's return interface (or inline return shape).

- [ ] **Step 2: Add the type for `SummarizingItem` and extend the snapshot type**

In `frontend/src/hooks/useQueueTray.ts`, add:

```typescript
import type { SummarizingItem } from '../components/queue-tray/SummarizingRow'

// (in the existing types section)
export type { SummarizingItem }
```

If the hook has a typed snapshot like `interface QueueSnapshot {...}`, add:

```typescript
interface QueueSnapshot {
  // ... existing fields ...
  summarizing?: SummarizingItem[]
}
```

- [ ] **Step 3: Update the fetch / SSE handling to extract `summarizing`**

Find where the hook merges fetched data into local state. Add the `summarizing` array. The simplest shape is to expose it as a top-level returned value:

```typescript
export function useQueueTray() {
  // ... existing state hooks ...
  const [summarizing, setSummarizing] = useState<SummarizingItem[]>([])

  // In the existing fetch / SSE handler:
  // when data arrives, also update summarizing:
  setSummarizing(data.summarizing ?? [])

  return {
    // ... existing returns ...
    summarizing,
    summarizeBacklog: summarizing.length,
  }
}
```

- [ ] **Step 4: Type-check**

```bash
cd frontend && npm run type-check
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/hooks/useQueueTray.ts
git commit -m "feat(frontend): useQueueTray exposes summarizing[] and summarizeBacklog

Pulls the new backend array into the tray hook. Consumers can render
the section + count without an additional fetch."
```

---

### Task 10: Add Summarizing section to `QueuePanel`

**Why:** The queue tray drawer needs the new section between Active and Completed.

**Files:**
- Modify: `frontend/src/components/queue-tray/QueuePanel.tsx`

- [ ] **Step 1: Update the Props interface**

In `frontend/src/components/queue-tray/QueuePanel.tsx`, add `summarizing` to the props:

```typescript
interface Props {
  active: DocumentQueueItem[]
  completed: CompletedItem[]
  summarizing: SummarizingItem[]  // NEW
  // ... existing props ...
}
```

Import:

```typescript
import SummarizingRow, { type SummarizingItem } from './SummarizingRow'
```

- [ ] **Step 2: Insert the Summarizing section between Active and Completed**

In the JSX, find the divider between Active and Completed (it's around line 148: `{hasActive && hasCompleted && <div className="border-b border-(--color-border)" />}`). Insert the new section before that divider:

```tsx
{/* Existing Active section ... */}

{/* Summarizing section — new */}
{summarizing.length > 0 && (
  <>
    {hasActive && <div className="border-b border-(--color-border)" />}
    <div className="px-4 py-2">
      <div
        className="text-[11px] font-medium uppercase tracking-wider mb-2"
        style={{ color: '#c4a4ff' }}
      >
        Summarizing ({summarizing.length})
      </div>
      <div className="space-y-0.5">
        {summarizing.map((item) => (
          <SummarizingRow key={item.doc_id} item={item} />
        ))}
      </div>
    </div>
  </>
)}

{/* Existing divider before Completed */}
{(hasActive || summarizing.length > 0) && hasCompleted && (
  <div className="border-b border-(--color-border)" />
)}
```

The divider conditional is tweaked so a divider appears between Active and Summarizing when both are present, and between Summarizing and Completed when both are present.

- [ ] **Step 3: Update the QueueTray parent to pass `summarizing` through**

Find the parent that renders `<QueuePanel ...>` (likely `frontend/src/components/queue-tray/QueueTray.tsx`). Pass the new value from `useQueueTray()`:

```bash
grep -n "useQueueTray\|<QueuePanel" frontend/src/components/queue-tray/QueueTray.tsx
```

```typescript
const { active, completed, summarizing, summarizeBacklog, /* ... */ } = useQueueTray()
return <QueuePanel active={active} completed={completed} summarizing={summarizing} /* ... */ />
```

- [ ] **Step 4: Virtualize the Summarizing section above 50 rows**

Mirror the existing Active virtualization. The current Active section uses `useVirtualizer` from `@tanstack/react-virtual` (already imported in QueuePanel.tsx) and renders an absolute-positioned wrapper inside a fixed-height container. Apply the same pattern to Summarizing:

```tsx
{summarizing.length > 0 && (
  <>
    {hasActive && <div className="border-b border-(--color-border)" />}
    <div className="px-4 py-2">
      <div
        className="text-[11px] font-medium uppercase tracking-wider mb-2"
        style={{ color: '#c4a4ff' }}
      >
        Summarizing ({summarizing.length})
      </div>
      {summarizing.length > VIRTUALIZE_THRESHOLD ? (
        <div style={{ height: `${summarizeVirtualizer.getTotalSize()}px`, position: 'relative', width: '100%' }}>
          {summarizeVirtualizer.getVirtualItems().map((vi) => {
            const item = summarizing[vi.index]
            return (
              <div
                key={item.doc_id}
                ref={summarizeVirtualizer.measureElement}
                data-index={vi.index}
                style={{
                  position: 'absolute',
                  top: 0,
                  left: 0,
                  width: '100%',
                  transform: `translateY(${vi.start}px)`,
                }}
              >
                <SummarizingRow item={item} />
              </div>
            )
          })}
        </div>
      ) : (
        <div className="space-y-0.5">
          {summarizing.map((item) => (
            <SummarizingRow key={item.doc_id} item={item} />
          ))}
        </div>
      )}
    </div>
  </>
)}
```

And declare the second virtualizer near the existing one (top of the QueuePanel function body):

```typescript
const summarizeVirtualizer = useVirtualizer({
  count: summarizing.length,
  getScrollElement: () => scrollRef.current,
  estimateSize: () => 28,  // SummarizingRow is shorter than DocumentRow
  overscan: 6,
})
```

Reuse the existing `scrollRef` (used by the Active virtualizer). `VIRTUALIZE_THRESHOLD` is already defined at the top of the file.

- [ ] **Step 5: Type-check + render check**

```bash
cd frontend && npm run type-check && npm run lint
```

Spin up the dev server, seed a doc with a queued summarize job, and visually verify the section appears between Active and Completed.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/queue-tray/QueuePanel.tsx frontend/src/components/queue-tray/QueueTray.tsx
git commit -m "feat(frontend): Summarizing section in queue tray drawer

Renders between Active and Completed when summarizing.length > 0.
Hidden when empty. Section header purple to mark the lane;
virtualized at 50+ rows to match the Active section pattern."
```

---

### Task 11: `QueuePill` Summarizing rest state

**Why:** The always-visible pill should show `Summarizing N` (purple, soft glow) when ingest is empty but summaries are pending.

**Files:**
- Modify: `frontend/src/components/queue-tray/QueuePill.tsx`

- [ ] **Step 1: Extend the Props**

```typescript
interface QueuePillProps {
  activeCount: number
  completedCount: number
  summarizingCount: number  // NEW
  isPulsing: boolean
  onClick: () => void
}
```

- [ ] **Step 2: Add the new rest state**

In the existing `idle = activeCount === 0 && completedCount === 0` line, also condition on `summarizingCount`. Then add a new branch in the JSX:

```tsx
const idle = activeCount === 0 && completedCount === 0 && summarizingCount === 0
const summarizingOnly = activeCount === 0 && summarizingCount > 0
```

Replace the existing JSX inside the button with three conditional branches in priority order:

```tsx
{activeCount > 0 && (
  <>
    <span className="relative flex h-2 w-2">
      <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-75" />
      <span className="relative inline-flex h-2 w-2 rounded-full bg-amber-500" />
    </span>
    <span>{activeCount} processing</span>
  </>
)}
{activeCount === 0 && summarizingCount > 0 && (
  <>
    <span
      className="relative flex h-2 w-2 rounded-full animate-pulse"
      style={{ background: '#c4a4ff' }}
    />
    <span style={{ color: '#c4a4ff' }}>Summarizing {summarizingCount}</span>
  </>
)}
{activeCount === 0 && summarizingCount === 0 && completedCount > 0 && (
  <>
    {/* existing recent-state block */}
  </>
)}
{idle && (
  <>
    {/* existing idle-state block */}
  </>
)}
```

Update the button's outer `className` to add a soft purple glow when `summarizingOnly` is true:

```tsx
className={`
  ... (existing classes) ...
  ${summarizingOnly ? 'shadow-[0_0_14px_rgba(186,140,255,0.18)]' : ''}
`}
```

- [ ] **Step 3: Update the parent to pass `summarizingCount`**

In whichever component renders `<QueuePill ...>` (likely `QueueTray.tsx`), pass the new prop:

```typescript
<QueuePill
  activeCount={activeCount}
  completedCount={completedCount}
  summarizingCount={summarizeBacklog}
  isPulsing={isPulsing}
  onClick={onClick}
/>
```

- [ ] **Step 4: Type-check + visual verify**

```bash
cd frontend && npm run type-check && npm run lint
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/queue-tray/QueuePill.tsx frontend/src/components/queue-tray/QueueTray.tsx
git commit -m "feat(frontend): QueuePill 'Summarizing N' rest state

Priority: Active > Summarizing > Idle. When ingest is empty but
summary backlog > 0, pill shows 'Summarizing N' in purple with a
soft glow. Hidden behind 'N processing' when ingest is active."
```

---

## Phase 4 — Observatory pipeline diagram (Surface 3)

### Task 12: Split PipelineDiagram into main flow + BACKGROUND panel

**Why:** The diagram needs to visually communicate "Ready is the terminus of the express line; summarize runs separately."

**Files:**
- Modify: `frontend/src/components/stats/PipelineDiagram.tsx`

- [ ] **Step 1: Drop summarize from `NODE_POSITIONS` and `EDGES` (top-level constants)**

In `frontend/src/components/stats/PipelineDiagram.tsx`, find the two constants (around lines 53-72):

Replace:

```typescript
const NODE_POSITIONS: Record<string, { x: number; y: number }> = {
  extract: { x: 60, y: 160 },
  ocr: { x: 160, y: 160 },
  chunk: { x: 260, y: 160 },
  entities: { x: 390, y: 60 },
  embed: { x: 390, y: 160 },
  summarize: { x: 390, y: 260 },
  finalize: { x: 520, y: 160 },
}

const EDGES: { from: string; to: string }[] = [
  { from: 'extract', to: 'ocr' },
  { from: 'ocr', to: 'chunk' },
  { from: 'chunk', to: 'entities' },
  { from: 'chunk', to: 'embed' },
  { from: 'chunk', to: 'summarize' },
  { from: 'entities', to: 'finalize' },
  { from: 'embed', to: 'finalize' },
  { from: 'summarize', to: 'finalize' },
]
```

with:

```typescript
const NODE_POSITIONS: Record<string, { x: number; y: number }> = {
  extract: { x: 60, y: 160 },
  ocr: { x: 160, y: 160 },
  chunk: { x: 260, y: 160 },
  entities: { x: 390, y: 100 },  // shifted vertically since the third
  embed: { x: 390, y: 220 },     // fan-out node is gone
  finalize: { x: 520, y: 160 },
}

const EDGES: { from: string; to: string }[] = [
  { from: 'extract', to: 'ocr' },
  { from: 'ocr', to: 'chunk' },
  { from: 'chunk', to: 'entities' },
  { from: 'chunk', to: 'embed' },
  { from: 'entities', to: 'finalize' },
  { from: 'embed', to: 'finalize' },
]
```

Also update `STAGE_LABELS` — it still has `'Summarize'`. Drop the line:

```typescript
const STAGE_LABELS: Record<string, string> = {
  extract: 'Extract',
  ocr: 'OCR',
  chunk: 'Chunk',
  entities: 'Entities',
  embed: 'Embed',
  finalize: 'Finalize',
}
```

- [ ] **Step 2: Add the BACKGROUND panel below the main SVG**

Find the component's render method (search for `<svg`). The current diagram returns a single `<svg>`; we'll wrap the return in a fragment with the SVG and a panel below.

```tsx
return (
  <div>
    <svg viewBox="0 0 600 240" /* ... existing props ... */>
      {/* existing main-flow rendering */}

      {/* Ready badge above finalize */}
      <text x="520" y="135" textAnchor="middle" fontSize="10" fill="#30d158" fontWeight="600">
        Ready
      </text>
    </svg>

    {/* Dotted separator + BACKGROUND panel */}
    <div className="mt-2 border-t border-dashed border-(--color-border) opacity-60" />
    <div
      className="mt-2 rounded-lg p-3 flex items-center gap-3"
      style={{ background: 'rgba(186,140,255,0.06)', border: '1px solid rgba(186,140,255,0.4)' }}
    >
      <span
        className="text-[9px] font-semibold tracking-wider"
        style={{ color: '#c4a4ff' }}
      >
        BACKGROUND
      </span>
      <span
        className="inline-block h-3.5 w-3.5 rounded-full"
        style={{ background: 'rgba(186,140,255,0.25)', border: '1.5px solid #c4a4ff' }}
      />
      <span className="text-[12px]" style={{ color: '#c4a4ff' }}>Summarize</span>
      <span className="ml-auto text-[10px] text-(--color-text-secondary)">
        {summarizeQueued} queued
      </span>
    </div>
  </div>
)
```

- [ ] **Step 3: Source `summarizeQueued`**

The diagram component receives a `snapshot: QueueSnapshot` prop (per `function PipelineGraph({ snapshot })`). The snapshot already contains `llm: { queued: number; running: number }` (queue tray hook reads it). Use that directly:

```tsx
const summarizeQueued = (snapshot.llm?.queued ?? 0) + (snapshot.llm?.running ?? 0)
```

In the BACKGROUND panel JSX (from Step 2), drop the throughput display from this task — the dedicated `SummaryBacklogPanel` (Task 14) is the canonical place for throughput stats. The diagram's panel shows queue depth only:

```tsx
<span className="ml-auto text-[10px] text-(--color-text-secondary)">
  {summarizeQueued} queued
</span>
```

This keeps the diagram's BACKGROUND panel single-purpose: a visual marker that the side-channel exists and how loaded it is right now. Detailed metrics live one panel down in the Observatory.

- [ ] **Step 4: Type-check**

```bash
cd frontend && npm run type-check
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/stats/PipelineDiagram.tsx
git commit -m "refactor(frontend): split PipelineDiagram into main flow + BACKGROUND panel

Main canvas drops summarize node + its edges; entities and embed
shift to y=100 / y=220. Below the canvas, a dotted separator and a
purple-tinted BACKGROUND panel hold a single Summarize indicator
with queue depth. Ready badge above finalize cements 'this is the
terminus of the express line'."
```

---

## Phase 5 — Observatory timing chart + Summary Backlog widget (Surface 4)

### Task 13: Drop summarize from `PipelineTimingChart`

**Why:** The Avg Pipeline Timing chart should reflect the gating stages only. Map-reduce summarize is an outlier that distorts the visual scale.

**Files:**
- Modify: `frontend/src/components/stats/PipelineTimingChart.tsx:16,23`

- [ ] **Step 1: Drop `'summarize'` from `STAGES`**

Change:

```typescript
const STAGES = ['extract', 'ocr', 'chunk', 'entities', 'embed', 'summarize', 'finalize'] as const
```

to:

```typescript
const STAGES = ['extract', 'ocr', 'chunk', 'entities', 'embed', 'finalize'] as const
```

Also remove `summarize: 'Summarize'` from the labels object at line 23.

- [ ] **Step 2: Type-check**

```bash
cd frontend && npm run type-check
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/stats/PipelineTimingChart.tsx
git commit -m "refactor(frontend): drop summarize bar from Avg Pipeline Timing chart

Chart now reflects gating stages only (6 bars). Summary timing lives
in the new Summary Backlog widget below the chart."
```

---

### Task 14: `SummaryBacklogPanel` component

**Why:** The Observatory page needs a dedicated widget showing throughput, p50, and a composite In Queue + sparkline.

**Files:**
- Create: `frontend/src/components/stats/SummaryBacklogPanel.tsx`

- [ ] **Step 1: Create the component**

```tsx
// frontend/src/components/stats/SummaryBacklogPanel.tsx

import { useEffect, useState } from 'react'
import { useToken } from '../../contexts/AuthContext'  // adjust path to match codebase

interface BacklogResponse {
  queue_depth: number
  throughput_per_min: number
  p50_seconds: number
  depth_history: [number, number][]
}

function formatSeconds(s: number): string {
  if (s < 1) return '<1s'
  if (s < 60) return `${Math.round(s)}s`
  return `${(s / 60).toFixed(1)}m`
}

function trendLabel(history: [number, number][]): string {
  if (history.length < 4) return ''
  const first = history.slice(0, history.length / 4).reduce((a, [, d]) => a + d, 0) / Math.max(1, Math.floor(history.length / 4))
  const last = history.slice(-Math.floor(history.length / 4)).reduce((a, [, d]) => a + d, 0) / Math.max(1, Math.floor(history.length / 4))
  if (last < first - 1) return '↘ catching up'
  if (last > first + 1) return '↗ falling behind'
  return '→ steady'
}

function Sparkline({ history }: { history: [number, number][] }) {
  if (history.length < 2) return null
  const values = history.map(([, d]) => d)
  const max = Math.max(...values, 1)
  const points = history
    .map(([, d], i) => {
      const x = (i / (history.length - 1)) * 200
      const y = 30 - (d / max) * 28
      return `${x},${y}`
    })
    .join(' L ')
  const fillPoints = `${points} L 200,30 L 0,30 Z`
  return (
    <svg viewBox="0 0 200 30" preserveAspectRatio="none" className="w-full" style={{ height: 28 }}>
      <path d={`M ${fillPoints}`} fill="rgba(186,140,255,0.12)" />
      <path
        d={`M ${points}`}
        fill="none"
        stroke="#c4a4ff"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export default function SummaryBacklogPanel() {
  const { token } = useToken()
  const [data, setData] = useState<BacklogResponse | null>(null)

  useEffect(() => {
    if (!token) return
    let cancelled = false
    const fetchData = async () => {
      try {
        const res = await fetch('/api/system/summary-backlog', {
          headers: { Authorization: `Bearer ${token}` },
        })
        if (!res.ok) return
        const json = (await res.json()) as BacklogResponse
        if (!cancelled) setData(json)
      } catch {
        // silently retry on next interval
      }
    }
    fetchData()
    const interval = setInterval(fetchData, 30_000)  // refresh every 30s
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [token])

  if (!data) return null

  return (
    <div
      className="rounded-xl p-4 mt-4"
      style={{ background: 'rgba(38,38,40,0.97)', border: '1px solid rgba(255,255,255,0.1)' }}
    >
      <h3 className="text-[13px] mb-3" style={{ color: '#c4a4ff' }}>Summary Backlog</h3>
      <div className="grid items-stretch gap-6" style={{ gridTemplateColumns: 'auto auto 1fr' }}>
        {/* Throughput */}
        <div>
          <div className="text-[28px] font-semibold leading-none tabular-nums" style={{ color: '#c4a4ff' }}>
            {data.throughput_per_min.toFixed(1)}
            <span className="text-[13px] text-(--color-text-secondary) ml-0.5">/min</span>
          </div>
          <div className="text-[10px] uppercase tracking-wider mt-1.5 text-(--color-text-secondary)">
            Throughput
          </div>
        </div>
        {/* p50 */}
        <div>
          <div className="text-[28px] font-semibold leading-none tabular-nums" style={{ color: '#c4a4ff' }}>
            {formatSeconds(data.p50_seconds)}
          </div>
          <div className="text-[10px] uppercase tracking-wider mt-1.5 text-(--color-text-secondary)">
            p50
          </div>
        </div>
        {/* In Queue (composite: number + sparkline) */}
        <div
          className="rounded-lg p-2.5 grid gap-3 items-stretch"
          style={{
            gridTemplateColumns: 'auto 1fr',
            background: 'rgba(186,140,255,0.06)',
            border: '1px solid rgba(186,140,255,0.18)',
          }}
        >
          <div
            className="pr-3 flex flex-col justify-center"
            style={{ borderRight: '1px solid rgba(186,140,255,0.2)' }}
          >
            <div className="text-[28px] font-semibold leading-none tabular-nums" style={{ color: '#c4a4ff' }}>
              {data.queue_depth}
            </div>
            <div className="text-[10px] uppercase tracking-wider mt-1.5 text-(--color-text-secondary)">
              In Queue
            </div>
          </div>
          <div className="flex flex-col justify-between min-w-0">
            <div className="text-[10px] text-(--color-text-secondary)">
              queue depth · last hour {trendLabel(data.depth_history)}
            </div>
            <Sparkline history={data.depth_history} />
          </div>
        </div>
      </div>
    </div>
  )
}
```

If `useToken` is named differently in this codebase, adjust the import. Search:

```bash
grep -rn "export.*useToken\|export.*useAuth" frontend/src/contexts | head -5
```

- [ ] **Step 2: Type-check + lint**

```bash
cd frontend && npm run type-check && npm run lint -- src/components/stats/SummaryBacklogPanel.tsx
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/stats/SummaryBacklogPanel.tsx
git commit -m "feat(frontend): SummaryBacklogPanel for Observatory

Three-slot row: Throughput · p50 · composite In Queue widget.
The In Queue widget is one card with two sections — current depth
on the left, hour-long sparkline on the right, separated by a thin
purple divider. Trend label ('catching up' / 'falling behind' /
'steady') auto-derived from the history series."
```

---

### Task 15: Render `SummaryBacklogPanel` in ObservatoryPage

**Why:** The widget needs a parent — Observatory page below the timing chart.

**Files:**
- Modify: `frontend/src/pages/ObservatoryPage.tsx`

- [ ] **Step 1: Import + render**

```bash
grep -n "PipelineTimingChart\|<PipelineTimingChart" frontend/src/pages/ObservatoryPage.tsx
```

Find where `PipelineTimingChart` is rendered. Right after that JSX element, render the new panel:

```tsx
import SummaryBacklogPanel from '../components/stats/SummaryBacklogPanel'

// ... in the page body ...
<PipelineTimingChart {...props} />
<SummaryBacklogPanel />
```

- [ ] **Step 2: Type-check + visual check**

```bash
cd frontend && npm run type-check
```

Visit `/admin/observatory` (or wherever Observatory lives) and verify the new panel renders below the timing chart.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/ObservatoryPage.tsx
git commit -m "feat(frontend): render SummaryBacklogPanel below Avg Pipeline Timing"
```

---

## Self-review checklist

After all tasks land:

- [ ] **Spec coverage**: every named surface (per-doc chip, queue tray section, queue pill state, pipeline diagram, timing chart, backlog widget) is implemented.
- [ ] **Five chip states**: SummaryChip renders all five (generating / pending / extractive / summarized / failed).
- [ ] **API additions**: `summarize_job_status` on doc list, `summarizing[]` on queue snapshot, `/system/summary-backlog` endpoint, `progress_current/total` updated by worker.
- [ ] **Constants cleaned up**: `summarize` removed from `PIPELINE_STAGES` (useQueueTray.ts), `STAGES` (PipelineTimingChart.tsx), `NODE_POSITIONS` + `EDGES` + `STAGE_LABELS` (PipelineDiagram.tsx).
- [ ] **Pill priority**: Active > Summarizing > Idle.
- [ ] **Sections hidden when empty**: Summarizing section in queue tray hidden at backlog 0.
- [ ] **No regressions**: `uv run pytest tests/` green; `npm run type-check` + `npm run lint` clean.

---

## Out of scope (intentional)

- Visual regression tests / screenshot tests (no jest/vitest in this codebase; verification is manual).
- "Retry summary" button on the Summary Failed chip — `POST /api/docs/{doc_id}/resummarize` already exists; surfacing it in the UI is a follow-up.
- Per-doc detail page summary section — kept as-is for this round.
- "Summary blocked: no LLM model configured" surfacing.
- BACKGROUND panel particle animation in the diagram — future polish.
