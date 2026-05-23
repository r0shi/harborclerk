# Phase 3: Heading-Aware Chunking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `_split_text` prefer heading boundaries as chunk break points and never split inside a fenced code block, so chunks align with document structure instead of arbitrarily cutting through sections and code.

**Architecture:** Two new pure helpers in `chunk.py` — `_find_code_fence_ranges(text)` scans the chunker's input for fenced-code spans, and `_find_heading_positions_in_text(text, headings)` line-anchors each heading title to find its char offset in the concatenated text the chunker sees. `_split_text` gains two optional parameters (`heading_positions`, `code_fence_ranges`); the break-selection logic prefers heading boundaries above the existing paragraph/sentence/word fallback, and any chunk end that would fall inside a fence is pushed to the fence's end. `run_chunk` queries `document_headings` for the doc, computes the two inputs, and passes them through.

**Tech Stack:** Python 3.12, SQLAlchemy (existing `DocumentHeading` model), pytest, ruff. No new third-party dependencies.

**Working directory:** the `feat/markdown-text-handling` worktree. All paths below are relative to its root. Phase 2 must be committed before starting (HEAD must include `extract_markdown` producing `DocumentHeading` rows for `.md`/`.markdown` files).

**Spec:** `docs/superpowers/specs/2026-05-22-markdown-text-handling-design.md` (Phase 3).

---

## File Structure

- **Modify** `src/harbor_clerk/worker/stages/chunk.py` — add the two helpers (`_find_code_fence_ranges`, `_find_heading_positions_in_text`); extend `_split_text` to accept and honor `heading_positions` + `code_fence_ranges`; extend `run_chunk` to query `document_headings`, build the two inputs, and pass them through.
- **Modify** `tests/test_chunking.py` — add unit tests for the two helpers and for the new behaviors of `_split_text`.
- **Modify** `tests/test_pipeline.py` — add a DB-backed integration test confirming end-to-end Markdown ingestion produces chunks aligned with headings.

Build order: Task 1 → 2 → 3 → 4 → 5 → 6. Tasks 1 and 2 are independent of each other but share the file. Task 3 depends on neither (uses parameters), but its tests reference behaviors that map to the helpers' output shape. Task 4 depends on Tasks 1, 2, 3. Task 5 depends on Task 4.

---

### Task 1: `_find_code_fence_ranges` helper

A pure helper that scans the chunker's input text for fenced-code-block spans and returns their char ranges, so the splitter can avoid breaking inside one.

**Files:**
- Modify: `src/harbor_clerk/worker/stages/chunk.py`
- Modify: `tests/test_chunking.py`

- [ ] **Step 1: Write the failing tests**

Extend the existing import from `chunk.py` in `tests/test_chunking.py` so it includes `_find_code_fence_ranges`, then append:

```python
from harbor_clerk.worker.stages.chunk import _find_code_fence_ranges


# --- _find_code_fence_ranges ---


def test_fence_ranges_empty_text():
    assert _find_code_fence_ranges("") == []


def test_fence_ranges_no_fence():
    text = "Just prose.\n\nSome more prose.\n"
    assert _find_code_fence_ranges(text) == []


def test_fence_ranges_single_fence():
    text = "Intro.\n\n```python\ncode_line()\n```\n\nOutro.\n"
    ranges = _find_code_fence_ranges(text)
    assert len(ranges) == 1
    start, end = ranges[0]
    # Opening fence starts at char 8 (after "Intro.\n\n").
    assert text[start:].startswith("```python")
    # End is the start of the line after the closing fence (i.e. start of "\n").
    assert text[start:end].endswith("```\n")


def test_fence_ranges_multiple_fences():
    text = "```\nA\n```\n\nMid.\n\n```py\nB\n```\n"
    ranges = _find_code_fence_ranges(text)
    assert len(ranges) == 2
    # Each range covers exactly one fence.
    for s, e in ranges:
        block = text[s:e]
        assert block.count("```") == 2  # opening + closing


def test_fence_ranges_tilde_fence():
    """``~~~`` fences (CommonMark alternative) are also recognized."""
    text = "Intro.\n\n~~~\ncode\n~~~\n\nOutro.\n"
    ranges = _find_code_fence_ranges(text)
    assert len(ranges) == 1


def test_fence_ranges_unterminated():
    """A fence that never closes extends to end-of-text."""
    text = "Intro.\n\n```\nstart of code\nmore code\n"
    ranges = _find_code_fence_ranges(text)
    assert len(ranges) == 1
    start, end = ranges[0]
    assert end == len(text)
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `uv run pytest tests/test_chunking.py -k fence_ranges -v`
Expected: `ImportError: cannot import name '_find_code_fence_ranges'`.

- [ ] **Step 3: Implement `_find_code_fence_ranges`**

Add to `src/harbor_clerk/worker/stages/chunk.py`, after the `SENTENCE_RE` constant:

```python
def _find_code_fence_ranges(text: str) -> list[tuple[int, int]]:
    """Return char ranges covering each fenced code block in ``text``.

    Each range is ``(start, end)`` where ``start`` is the char offset of the
    opening fence line's first char and ``end`` is the char offset just past
    the closing fence line (i.e., the start of the next line, or ``len(text)``
    if the closing fence is unterminated or at end-of-text).

    Recognizes both backtick (``` ``` ```) and tilde (``~~~``) fences.
    """
    if not text:
        return []
    lines = text.splitlines(keepends=True)
    line_starts = [0]
    for line in lines:
        line_starts.append(line_starts[-1] + len(line))

    ranges: list[tuple[int, int]] = []
    in_fence = False
    fence_start_char = 0
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        is_fence = stripped.startswith("```") or stripped.startswith("~~~")
        if not in_fence and is_fence:
            in_fence = True
            fence_start_char = line_starts[i]
        elif in_fence and is_fence:
            in_fence = False
            ranges.append((fence_start_char, line_starts[i + 1]))

    # Unterminated fence at EOF — treat as extending through the rest of the text.
    if in_fence:
        ranges.append((fence_start_char, len(text)))

    return ranges
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_chunking.py -k fence_ranges -v`
Expected: PASS (6 tests).

Run `uv run ruff check src/harbor_clerk/worker/stages/chunk.py tests/test_chunking.py` — confirm clean.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/worker/stages/chunk.py tests/test_chunking.py
git commit -m "feat(chunk): detect fenced-code-block char ranges in chunker input"
```

---

### Task 2: `_find_heading_positions_in_text` helper

A pure helper that locates each heading's title at a line start in the concatenated text the chunker sees. Necessary because `document_headings.position` is set during extraction and drifts relative to the chunker's concatenated `full_text` (page separators add `\n`s).

**Files:**
- Modify: `src/harbor_clerk/worker/stages/chunk.py`
- Modify: `tests/test_chunking.py`

- [ ] **Step 1: Write the failing tests**

Extend the import to include `_find_heading_positions_in_text`, then append to `tests/test_chunking.py`:

```python
from harbor_clerk.worker.stages.chunk import _find_heading_positions_in_text


# --- _find_heading_positions_in_text ---


def test_heading_positions_empty():
    assert _find_heading_positions_in_text("", []) == []
    assert _find_heading_positions_in_text("text", []) == []
    assert _find_heading_positions_in_text("", [{"title": "X"}]) == []


def test_heading_positions_at_start():
    text = "Heading One\n\nBody text.\n"
    headings = [{"title": "Heading One"}]
    assert _find_heading_positions_in_text(text, headings) == [0]


def test_heading_positions_multiple_in_order():
    text = "Section A\n\nProse here.\n\nSection B\n\nMore prose.\n"
    headings = [{"title": "Section A"}, {"title": "Section B"}]
    out = _find_heading_positions_in_text(text, headings)
    assert len(out) == 2
    assert out[0] < out[1]
    assert text[out[0]:].startswith("Section A")
    assert text[out[1]:].startswith("Section B")


def test_heading_positions_skips_prose_occurrence():
    """Title appearing in prose (not at line start) is NOT matched."""
    text = "Refers to the Budget section.\n\nBudget\n\nActual content.\n"
    headings = [{"title": "Budget"}]
    out = _find_heading_positions_in_text(text, headings)
    assert len(out) == 1
    # Position must be at the line-start occurrence (after the first \n\n),
    # not the prose occurrence at offset 14.
    assert text[out[0]:].startswith("Budget")
    if out[0] > 0:
        assert text[out[0] - 1] == "\n"


def test_heading_positions_low_water_keeps_order():
    """Repeated titles are matched in document order via low-water mark."""
    text = "Notes\n\nDetail.\n\nNotes\n\nMore.\n"
    headings = [{"title": "Notes"}, {"title": "Notes"}]
    out = _find_heading_positions_in_text(text, headings)
    assert len(out) == 2
    assert out[0] < out[1]


def test_heading_positions_skips_missing_title():
    """A heading whose title can't be located is skipped (not in output)."""
    text = "Real Heading\n\nBody.\n"
    headings = [{"title": "Real Heading"}, {"title": "Not Present"}]
    out = _find_heading_positions_in_text(text, headings)
    assert len(out) == 1
    assert text[out[0]:].startswith("Real Heading")


def test_heading_positions_skips_empty_title():
    """A heading with an empty title is skipped without error."""
    text = "Section\n\nBody.\n"
    headings = [{"title": ""}, {"title": "Section"}]
    out = _find_heading_positions_in_text(text, headings)
    assert len(out) == 1
    assert text[out[0]:].startswith("Section")
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `uv run pytest tests/test_chunking.py -k heading_positions -v`
Expected: `ImportError: cannot import name '_find_heading_positions_in_text'`.

- [ ] **Step 3: Implement `_find_heading_positions_in_text`**

Add to `src/harbor_clerk/worker/stages/chunk.py` (next to `_find_code_fence_ranges`):

```python
def _find_heading_positions_in_text(text: str, headings: list[dict]) -> list[int]:
    """Locate each heading's ``title`` at a line start in ``text``.

    Returns a list of char offsets (one per heading whose title is found),
    in the order the headings were given. A low-water mark advances after
    each match so repeated titles match in document order.

    Headings with empty titles or titles that don't appear at a line start
    in ``text`` are silently skipped.
    """
    if not text or not headings:
        return []

    positions: list[int] = []
    low_water = 0
    for h in headings:
        title = h.get("title") or ""
        if not title:
            continue
        # Find the title at a line start (preceded by '\n' or at text start).
        search_from = low_water
        while search_from < len(text):
            idx = text.find(title, search_from)
            if idx < 0:
                break
            if idx == 0 or text[idx - 1] == "\n":
                positions.append(idx)
                low_water = idx + len(title)
                break
            search_from = idx + 1

    return positions
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_chunking.py -k heading_positions -v`
Expected: PASS (7 tests).

Run `uv run ruff check src/harbor_clerk/worker/stages/chunk.py tests/test_chunking.py` — confirm clean.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/worker/stages/chunk.py tests/test_chunking.py
git commit -m "feat(chunk): line-anchored heading-title position lookup"
```

---

### Task 3: `_split_text` honors heading positions and protects code fences

Extend `_split_text` to (a) prefer heading boundaries as break points when one falls in the target window's upper half, and (b) never end a chunk inside a fenced code-block range — if the proposed end falls inside a fence, push the end to the fence's far edge (potentially producing an oversized chunk; acceptable because the alternative is to split a code block).

**Files:**
- Modify: `src/harbor_clerk/worker/stages/chunk.py`
- Modify: `tests/test_chunking.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chunking.py`:

```python
# --- _split_text heading-aware ---


def test_split_text_prefers_heading_boundary():
    """A heading position in the upper half of the target window becomes the break."""
    # Pad so the heading falls within (target//2, target) on the first pass.
    pre = "p" * 600
    heading_line = "\nHeading B\n"
    rest = "x" * 600
    text = pre + heading_line + rest
    heading_pos = len(pre) + 1  # char after the first \n, i.e. 'H' of "Heading B"
    result = _split_text(
        text,
        target=900,
        overlap=50,
        heading_positions=[heading_pos],
    )
    # The first chunk should end at the heading position (or very close to it).
    assert len(result) >= 2
    assert result[0][1] == heading_pos


def test_split_text_ignores_heading_below_half_target():
    """A heading in the first half of the target window is NOT preferred."""
    text = "Heading\n" + "x" * 1500  # heading at offset 0
    result = _split_text(text, target=1000, overlap=50, heading_positions=[0])
    # The first chunk MUST NOT end at offset 0 (degenerate empty chunk).
    assert result[0][1] > 0


def test_split_text_no_heading_falls_back_to_paragraph():
    """With no headings in window, the existing paragraph/sentence/word fallback runs."""
    para1 = "A" * 600
    para2 = "B" * 600
    text = para1 + "\n\n" + para2
    result = _split_text(text, target=800, overlap=50, heading_positions=[])
    # Should still break at the paragraph boundary near 602.
    assert len(result) >= 2
    first_end = result[0][1]
    assert abs(first_end - 602) <= 10


# --- _split_text fence-protected ---


def test_split_text_does_not_break_inside_code_fence():
    """If the proposed chunk end falls inside a fence range, push it past the fence."""
    pre = "p" * 800
    fence = "```\n" + ("L\n" * 100) + "```\n"  # ~210 chars
    text = pre + fence + ("x" * 200)
    fence_start = len(pre)
    fence_end = len(pre) + len(fence)
    result = _split_text(
        text,
        target=900,
        overlap=50,
        code_fence_ranges=[(fence_start, fence_end)],
    )
    # The first chunk's end must be either ≤ fence_start (didn't enter the fence)
    # or ≥ fence_end (jumped past it). It must NOT be strictly inside the fence.
    first_end = result[0][1]
    assert not (fence_start < first_end < fence_end), (
        f"chunk ends at {first_end}, inside fence [{fence_start}, {fence_end})"
    )


def test_split_text_fence_protection_with_heading():
    """Heading break and fence protection compose correctly: a heading inside a
    fence range is irrelevant (headings don't appear inside fences); a heading
    just past a fence end is fine."""
    pre = "p" * 600
    fence = "```\ncode\n```\n"
    rest = "Heading\n" + "x" * 400
    text = pre + fence + rest
    fence_start = len(pre)
    fence_end = len(pre) + len(fence)
    heading_pos = fence_end  # right after the fence
    result = _split_text(
        text,
        target=800,
        overlap=50,
        heading_positions=[heading_pos],
        code_fence_ranges=[(fence_start, fence_end)],
    )
    # The first chunk's end is at the heading boundary.
    assert result[0][1] == heading_pos


def test_split_text_backward_compat_no_params():
    """Calling _split_text without the new parameters keeps existing behavior."""
    text = "word " * 300
    result_old = _split_text(text, target=500, overlap=50)
    result_new = _split_text(text, target=500, overlap=50, heading_positions=None, code_fence_ranges=None)
    assert result_old == result_new
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `uv run pytest tests/test_chunking.py -k "split_text and (heading or fence or backward)" -v`
Expected: at least some FAIL (the existing `_split_text` doesn't accept `heading_positions` / `code_fence_ranges` arguments → `TypeError`).

- [ ] **Step 3: Extend `_split_text`**

Replace the existing `_split_text` function in `src/harbor_clerk/worker/stages/chunk.py` with this version:

```python
def _split_text(
    text: str,
    target: int = 1000,
    overlap: int = 150,
    heading_positions: list[int] | None = None,
    code_fence_ranges: list[tuple[int, int]] | None = None,
) -> list[tuple[int, int]]:
    """Return list of ``(char_start, char_end)`` for chunks.

    Break-preference order (highest to lowest priority):
      1. A heading position in ``heading_positions`` that falls in the upper
         half of the target window (``start + target // 2 < pos <= start + target``).
      2. A paragraph break (``\\n\\n``) in the upper half of the window.
      3. A sentence boundary in the upper half of the window.
      4. A word boundary in the upper half of the window.
      5. Hard cut at ``start + target``.

    Code-fence protection: if the chosen end falls strictly inside any range
    in ``code_fence_ranges``, the end is pushed to the range's far edge (the
    chunk is extended to cover the entire fence). Headings do not appear inside
    fences in practice, but the protection runs after heading selection too.
    """
    if not text:
        return []
    heading_positions = heading_positions or []
    code_fence_ranges = code_fence_ranges or []

    chunks: list[tuple[int, int]] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + target, text_len)

        if end < text_len:
            # Preference 1: a heading boundary in the upper half of the window.
            heading_break: int | None = None
            half = start + target // 2
            for hpos in heading_positions:
                if half < hpos <= end:
                    heading_break = hpos  # last match wins → rightmost in window

            if heading_break is not None:
                end = heading_break
            else:
                # Existing paragraph > sentence > word fallback (unchanged).
                para_break = text.rfind("\n\n", start, end)
                if para_break > start + target // 2:
                    end = para_break + 2
                else:
                    search_region = text[start:end]
                    sentences = list(SENTENCE_RE.finditer(search_region))
                    if sentences and sentences[-1].start() > target // 2:
                        end = start + sentences[-1].end()
                    else:
                        space = text.rfind(" ", start, end)
                        if space > start + target // 2:
                            end = space + 1

            # Code-fence protection: never end inside a fence.
            for fstart, fend in code_fence_ranges:
                if fstart < end < fend:
                    end = fend
                    break

        chunks.append((start, end))

        next_start = end - overlap
        if next_start <= start:
            next_start = end
        start = next_start

    return chunks
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_chunking.py -v`
Expected: PASS — existing tests still pass + 6 new heading/fence tests pass.

Run `uv run ruff check src/harbor_clerk/worker/stages/chunk.py tests/test_chunking.py` — confirm clean.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/worker/stages/chunk.py tests/test_chunking.py
git commit -m "feat(chunk): heading-aware break preference + code-fence protection"
```

---

### Task 4: Wire heading positions and fence ranges into `run_chunk`

`run_chunk` currently calls `_split_text(full_text, target=..., overlap=...)`. Extend it to query `document_headings` for the document, compute the heading positions in `full_text` via `_find_heading_positions_in_text`, compute the fence ranges via `_find_code_fence_ranges`, and pass both through.

**Files:**
- Modify: `src/harbor_clerk/worker/stages/chunk.py`

No new helper-level test in this task — the helpers are tested in Tasks 1–3 and the wire-up is verified by the integration test in Task 5 plus the regression suite passing.

- [ ] **Step 1: Update the imports in `chunk.py`**

In `src/harbor_clerk/worker/stages/chunk.py`, the import block already has:

```python
from harbor_clerk.models import Chunk, Document, DocumentPage
```

Extend it to also import `DocumentHeading`:

```python
from harbor_clerk.models import Chunk, Document, DocumentHeading, DocumentPage
```

- [ ] **Step 2: Replace the `_split_text` call in `run_chunk`**

Locate the existing line in `run_chunk` (currently around line 142):

```python
        chunk_ranges = _split_text(full_text, target=settings.chunk_target_size, overlap=settings.chunk_overlap)
```

Immediately before it, add the heading + fence computation. The full replacement block:

```python
        # Look up headings for break-preference. Ordered by position so the
        # search in `_find_heading_positions_in_text` advances monotonically.
        heading_rows = (
            session.execute(
                select(DocumentHeading)
                .where(DocumentHeading.doc_id == doc_id)
                .order_by(DocumentHeading.position)
            )
            .scalars()
            .all()
        )
        heading_dicts = [{"title": h.title} for h in heading_rows]
        heading_positions = _find_heading_positions_in_text(full_text, heading_dicts)
        code_fence_ranges = _find_code_fence_ranges(full_text)

        # Split into chunks (compute before race check)
        settings = get_settings()
        chunk_ranges = _split_text(
            full_text,
            target=settings.chunk_target_size,
            overlap=settings.chunk_overlap,
            heading_positions=heading_positions,
            code_fence_ranges=code_fence_ranges,
        )
```

Note: the existing `settings = get_settings()` line that immediately precedes the old `chunk_ranges = _split_text(...)` call moves into the new block above (placed right before the new `_split_text` call), so it isn't duplicated. Read the existing code first to confirm the exact lines you're replacing.

- [ ] **Step 3: Lint and run the chunking + pipeline tests**

Run: `uv run ruff check src/harbor_clerk/worker/stages/chunk.py`
Expected: clean.

Run: `uv run pytest tests/test_chunking.py -v`
Expected: PASS (all 19+ chunking tests still pass — no behavior change for docs without headings/fences).

Run: `uv run pytest tests/test_pipeline.py tests/test_watch_pipeline.py -q`
Expected: PASS (no regression in pipeline integration tests).

- [ ] **Step 4: Commit**

```bash
git add src/harbor_clerk/worker/stages/chunk.py
git commit -m "feat(chunk): pass heading positions + fence ranges to splitter in run_chunk"
```

---

### Task 5: Integration test — end-to-end Markdown chunking aligns with headings

Verify that a real Markdown document, ingested through `run_extract` and then `run_chunk`, produces chunks whose boundaries align with the document's headings (and don't split a code fence).

**Files:**
- Modify: `tests/test_pipeline.py`

- [ ] **Step 1: Look at the existing pipeline-integration pattern**

Read `tests/test_pipeline.py` — specifically the integration test added in Phase 2 (`test_run_extract_markdown_writes_headings_and_overrides_title`) for the fixture and assertion patterns. Use the same patterns: `sync_session`, `tmp_path` for the on-disk markdown file, `Document` + `IngestionJob` setup, then direct calls to `run_extract` and `run_chunk`.

- [ ] **Step 2: Add the integration test**

Append this test to `tests/test_pipeline.py` (place it near the existing Markdown integration test, matching the established style):

```python
def test_run_chunk_markdown_aligns_with_headings_and_preserves_code_fence(sync_session, tmp_path):
    """Integration: a Markdown doc with multiple headings and a code fence
    produces chunks whose boundaries prefer heading lines and never split
    inside the fence."""
    import hashlib

    from sqlalchemy import select

    from harbor_clerk.models.chunk import Chunk
    from harbor_clerk.models.document import Document
    from harbor_clerk.models.document_heading import DocumentHeading
    from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
    from harbor_clerk.models.ingestion_job import IngestionJob
    from harbor_clerk.worker.stages.chunk import run_chunk
    from harbor_clerk.worker.stages.extract import run_extract

    # Body chosen so it produces multiple chunks AND includes a code fence
    # the chunker must not split. Pad each section so the chunker hits the
    # default target (1000) inside each section.
    pad_a = ("A " * 250)
    pad_b = ("B " * 250)
    pad_c = ("C " * 250)
    md = (
        "# Section A\n\n"
        + pad_a
        + "\n\n# Section B\n\n"
        + pad_b
        + "\n\n```python\n"
        + ("print('keep me intact')\n" * 30)
        + "```\n\n"
        + "# Section C\n\n"
        + pad_c
        + "\n"
    )
    md_path = tmp_path / "note.md"
    md_path.write_text(md)

    doc = Document(
        title=md_path.stem,
        canonical_filename=md_path.name,
        status="active",
        sha256=hashlib.sha256(md_path.read_bytes()).digest(),
        source_path=str(md_path),
        pipeline_status=PipelineStatus.queued,
    )
    sync_session.add(doc)
    sync_session.flush()
    sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
    sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.chunk, status=JobStatus.queued))
    sync_session.commit()

    run_extract(doc.doc_id)
    run_chunk(doc.doc_id)

    sync_session.expire_all()
    headings = sync_session.execute(
        select(DocumentHeading).where(DocumentHeading.doc_id == doc.doc_id).order_by(DocumentHeading.position)
    ).scalars().all()
    chunks = sync_session.execute(
        select(Chunk).where(Chunk.doc_id == doc.doc_id).order_by(Chunk.chunk_num)
    ).scalars().all()

    assert len(headings) == 3, f"expected 3 headings, got {len(headings)}"
    assert {h.title for h in headings} == {"Section A", "Section B", "Section C"}
    assert len(chunks) >= 2, "expected the doc to produce multiple chunks"

    # The Python code fence must not be split: at least one chunk must contain
    # both the opening ``` and the closing ``` in a single chunk_text.
    fence_marker = "print('keep me intact')"
    chunks_with_fence = [c for c in chunks if fence_marker in c.chunk_text]
    assert chunks_with_fence, "fence content was not retained in any chunk"
    for c in chunks_with_fence:
        # Each chunk that contains fence content must contain the fence as a
        # complete contiguous block (the closing ``` is in the same chunk as
        # the opening ```).
        text = c.chunk_text
        # Both an opening ``` and a closing ``` must appear in this chunk.
        assert text.count("```") >= 2, (
            f"chunk {c.chunk_num} contains fence content but not a complete fence "
            f"(``` count = {text.count('```')})"
        )

    # Heading alignment: at least one chunk boundary should coincide with a
    # heading's title (a chunk's char_start equals the heading's position in
    # the concatenated full_text, or the chunk_text starts at a heading line).
    # The reliable check: at least one chunk's chunk_text begins with one of
    # the heading titles (allowing for an overlap-bleed of a few leading chars
    # off the previous chunk).
    heading_titles = {"Section A", "Section B", "Section C"}
    aligned = 0
    for c in chunks:
        head = c.chunk_text.lstrip()[:30]
        if any(head.startswith(title) for title in heading_titles):
            aligned += 1
    assert aligned >= 1, (
        "expected at least one chunk to start at a heading boundary; "
        f"chunk heads: {[c.chunk_text[:30] for c in chunks]!r}"
    )
```

- [ ] **Step 3: Run the test, verify it passes**

Run: `uv run pytest tests/test_pipeline.py::test_run_chunk_markdown_aligns_with_headings_and_preserves_code_fence -v`
Expected: PASS.

Run `uv run ruff check tests/test_pipeline.py` — confirm clean.

- [ ] **Step 4: Commit**

```bash
git add tests/test_pipeline.py
git commit -m "test(pipeline): integration test for heading-aware Markdown chunking"
```

---

### Task 6: Phase 3 verification

Confirm the whole phase is clean before completing it.

- [ ] **Step 1: Lint and format**

Run: `uv run ruff check src/ tests/`
Expected: no errors.

Run: `uv run ruff format --check src/ tests/`
Expected: clean.

- [ ] **Step 2: Run Phase 3 + Phase 2 + Phase 1 test sets**

Run:
```bash
uv run pytest tests/test_chunking.py tests/test_pipeline.py tests/test_watch_pipeline.py tests/test_markdown_extract.py tests/test_extract_helpers.py tests/test_file_types.py -v
```
Expected: all PASS.

- [ ] **Step 3: Run the full suite (regression check)**

Run: `uv run pytest tests/ -q -m "not integration and not requires_models"`
Expected: PASS, count ≥ the Phase 2 baseline (836) plus the Phase 3 new tests.

- [ ] **Step 4: Confirm no stray `_split_text` callers were missed**

Run: `git grep -n '_split_text(' src/`
Expected: only the function definition in `chunk.py` and the call inside `run_chunk`. If any other site exists, it must either keep the old (2-arg) signature OR pass the new params explicitly. Verify each.

---

## Self-Review

Checked against the spec's Phase 3:

- **Headings as preferred break points:** Task 3's `_split_text` change — heading positions checked first in the break-selection cascade, accepted when in the upper half of the target window. Backed by `test_split_text_prefers_heading_boundary` and `test_split_text_ignores_heading_below_half_target`. Task 4 supplies the positions from `document_headings`.
- **Code fences never split:** Task 1 detects the fence ranges; Task 3 pushes any in-fence chunk end to the fence's far edge; `test_split_text_does_not_break_inside_code_fence` verifies. Task 4 supplies the ranges by scanning the chunker's input.
- **Heading position relocation against the chunker's concatenated text:** Task 2's `_find_heading_positions_in_text` line-anchors each title (mirroring the relocation already proven in Phase 2's `extract_markdown`); `test_heading_positions_skips_prose_occurrence` covers the same class of bug Phase 2 fixed.
- **Backward compatibility:** Task 3 keeps the existing 3-arg signature working — `heading_positions` and `code_fence_ranges` are optional. `test_split_text_backward_compat_no_params` pins this.
- **End-to-end:** Task 5 verifies the wired pipeline produces chunks aligned with headings and a code fence intact.

No placeholders. Type names are consistent across tasks (`heading_positions`, `code_fence_ranges`, `_find_code_fence_ranges`, `_find_heading_positions_in_text`). The heading-dict shape `{"title": ...}` used in Task 2 matches what Task 4 builds from `DocumentHeading` rows.
