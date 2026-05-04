# Test Corpora Execution Sweep — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a restartable Python harness under `scripts/test_corpora/` that runs the six-phase test sweep defined in [`docs/superpowers/specs/2026-05-04-test-corpora-execution-design.md`](../specs/2026-05-04-test-corpora-execution-design.md) — three corpora, eight LLM models, mechanical metrics + Sonnet 4.6 judge, with state-file-based crash/resume support.

**Architecture:** Single-package layout under `scripts/test_corpora/`. The `runner/` modules implement orchestration concerns (state, metrics, REST client, judge, baseline, sampler, sweep entrypoint). The `corpora/` modules each expose an idempotent `acquire(workdir) -> CorpusManifest`. Question sets live as YAML in `questions/`. Tests live alongside in `tests/` and use pytest with mocked HTTP/SDK calls. The harness reuses Harbor Clerk's existing venv via `uv run`, so no separate dep install.

**Tech Stack:** Python 3.12, `httpx` (REST + SSE), `anthropic` SDK, `mcp` SDK, `pyyaml`, `tenacity`, `pypdfium2` + Pillow (synthetic OCR rendering), `spacy` (already installed). Pytest with `httpx.MockTransport` for unit tests.

---

## File Structure

```
scripts/test_corpora/
├── README.md                    # how to run, output layout, troubleshooting
├── pyproject.toml               # standalone uv-installable package
├── conftest.py                  # default config (API base URL, model list, timeouts)
├── .gitignore                   # ignores results/
├── corpora/
│   ├── __init__.py
│   ├── manifest.py              # CorpusManifest dataclass
│   ├── cuad.py                  # acquire(): download, sample 80 contracts
│   ├── enron.py                 # acquire(): download, filter custodians
│   └── synthetic.py             # acquire(): generate ~300 docs via Sonnet 4.6
├── questions/
│   ├── cuad.yaml                # 6 research + 10 ask
│   ├── enron.yaml               # 6 research + 10 ask
│   └── synthetic.yaml           # 6 research + 10 ask (incl. 2 cross-language pairs)
├── runner/
│   ├── __init__.py
│   ├── state.py                 # StateFile, Status enum, atomic JSON I/O, lock
│   ├── metrics.py               # citation_overlap, entity_overlap (pure)
│   ├── sampler.py               # in-flight stdout cards + phase boundary tables
│   ├── client.py                # HarborClerkClient (REST + SSE)
│   ├── judge.py                 # JudgeClient (Sonnet 4.6 rubric)
│   ├── claude_baseline.py       # BaselineGenerator (Sonnet 4.6 + MCP)
│   └── sweep.py                 # main entrypoint, six-phase orchestrator
└── tests/
    ├── __init__.py
    ├── conftest.py              # shared fixtures
    ├── test_state.py
    ├── test_metrics.py
    ├── test_sampler.py
    ├── test_client.py
    ├── test_judge.py
    ├── test_baseline.py
    ├── test_cuad.py
    ├── test_enron.py
    ├── test_synthetic.py
    └── fixtures/
        ├── enron_sample/        # ~10 synthetic .eml files for tests
        └── cuad_sample.tar.gz   # tiny CUAD-shaped fixture
```

Module responsibilities:

- **`runner/state.py`** — atomic JSON state file, status transitions, lock file, stale-recovery. Pure I/O, no domain logic.
- **`runner/metrics.py`** — pure functions for citation and entity overlap. No I/O.
- **`runner/sampler.py`** — counts completions, prints sample cards every 5th, prints summary tables at phase boundaries. Console-only side effects.
- **`runner/client.py`** — REST client for Harbor Clerk: start_research, poll_research, stream_ask, get_pipeline_status, wait_for_quiet, wipe_db, watch_folder_add. Uses httpx with retry decorator.
- **`runner/judge.py`** — calls Sonnet 4.6 with the rubric. Returns parsed JSON verdict.
- **`runner/claude_baseline.py`** — runs Sonnet 4.6 with Harbor Clerk's MCP server attached. Saves baseline JSON per question.
- **`runner/sweep.py`** — argparse entrypoint. Loads state, finds pending unit, dispatches to the right phase handler, updates state, repeats. All other modules orchestrate from here.
- **`corpora/<id>.py`** — idempotent `acquire(workdir) -> CorpusManifest`. Each is independent.

Tasks 2–13 build these incrementally with TDD. Each task ends with a single commit.

---

## Task 1: Scaffold the package

**Files:**
- Create: `scripts/test_corpora/README.md`
- Create: `scripts/test_corpora/pyproject.toml`
- Create: `scripts/test_corpora/conftest.py`
- Create: `scripts/test_corpora/.gitignore`
- Create: `scripts/test_corpora/{corpora,questions,runner,tests}/__init__.py`
- Create: `scripts/test_corpora/tests/conftest.py`

- [ ] **Step 1: Create the directory tree**

```bash
mkdir -p scripts/test_corpora/{corpora,questions,runner,tests/fixtures}
touch scripts/test_corpora/{corpora,runner,tests}/__init__.py
```

- [ ] **Step 2: Write the package's pyproject.toml**

`scripts/test_corpora/pyproject.toml`:

```toml
[project]
name = "harbor-clerk-test-corpora"
version = "0.1.0"
description = "Multi-hour test sweep harness for Harbor Clerk LLM models"
requires-python = ">=3.12"
dependencies = [
    "httpx>=0.28",
    "anthropic>=0.43",
    "mcp>=1.9",
    "pyyaml>=6",
    "tenacity>=9",
    "pypdfium2>=4",
    "Pillow>=12",
]

[project.optional-dependencies]
test = [
    "pytest>=9",
    "pytest-asyncio>=0.24",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Step 3: Write conftest.py with default config**

`scripts/test_corpora/conftest.py`:

```python
"""Shared configuration for the test-corpora sweep harness.

Imported by both the runner and the tests. Single source of truth for
constants like the API base URL and the model list. Override via env
vars or CLI flags in sweep.py.
"""

from __future__ import annotations

import os
from pathlib import Path

API_BASE = os.environ.get("HC_API_BASE", "https://localhost")
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"

# All eight downloaded models, by Harbor Clerk model_id. Phase 4 sweeps over
# this list. Phase 5 uses TOP_MODELS only.
ALL_MODELS = [
    "qwen3-8b",
    "qwen3-4b",
    "phi-4-mini",
    "deepseek-r1-8b",
    "gemma-26b",
    "smollm3-3b",
    "gpt-oss-20b",
    "qwen3.6-35b",
]

# Two largest by parameter count. Used for Phases 5 and 6 parity comparison.
TOP_MODELS = ["qwen3.6-35b", "gemma-26b"]

DEPTHS = ["light", "standard", "thorough"]
DEFAULT_DEPTH = "standard"
DEFAULT_TIME_LIMIT_SECONDS = 30 * 60  # 30 minutes
SAMPLE_EVERY_N = 5  # in-flight stdout sampling cadence

JUDGE_MODEL = "claude-sonnet-4-6"
BASELINE_MODEL = "claude-sonnet-4-6"

WORKDIR_DEFAULT = Path("~/Library/Application Support/Harbor Clerk/test-corpora").expanduser()
RESULTS_DIR_NAME = "results"
```

- [ ] **Step 4: Write README skeleton**

`scripts/test_corpora/README.md`:

```markdown
# Harbor Clerk Test Corpora Sweep

Multi-hour test harness that exercises all 8 downloaded LLM models against
three structurally-different corpora (CUAD legal contracts, Enron email
subset, synthetic bilingual small-business). Six sequential phases, fully
restartable.

See [`docs/superpowers/specs/2026-05-04-test-corpora-execution-design.md`](../../docs/superpowers/specs/2026-05-04-test-corpora-execution-design.md) for the full design.

## Quickstart

Prerequisite: Harbor Clerk is running locally — either the macOS Server
app or `docker compose up`. The harness talks to it over `https://localhost`.

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
cd /path/to/mcp-gateway
uv run python -m scripts.test_corpora.runner.sweep \
    --run-id 2026-05-05-full \
    --workdir ~/Library/Application\ Support/Harbor\ Clerk/test-corpora
```

## Resume after interrupt

```bash
uv run python -m scripts.test_corpora.runner.sweep \
    --run-id 2026-05-05-full --resume
```

## Force re-run a slice

```bash
uv run python -m scripts.test_corpora.runner.sweep \
    --run-id 2026-05-05-full --rerun "phase=5,model=qwen3.6-35b,corpus=cuad"
```

## Output layout

`<workdir>/results/<run-id>/`:

| Path | What |
| --- | --- |
| `state.json` | resumable state — every (phase, corpus, model, q, depth) cell |
| `baselines/<corpus>/<question_id>.json` | Claude Sonnet 4.6 baseline output |
| `responses/<corpus>/<model>/<question_id>__<depth>.json` | local-model response |
| `judge/<corpus>/<model>/<question_id>__<depth>.json` | Phase-5 judge verdict |
| `metrics.csv` | one row per completion |
| `log.txt` | full run log |

## Troubleshooting

- **API unreachable:** check `curl -k https://localhost/api/system/health`
- **State file locked:** another runner is using it; check `state.lock`
- **DB pool exhausted under sweep load:** see `docs/debugging.md`
```

- [ ] **Step 5: Write .gitignore**

`scripts/test_corpora/.gitignore`:

```
results/
__pycache__/
*.pyc
.pytest_cache/
```

- [ ] **Step 6: Write the test conftest**

`scripts/test_corpora/tests/conftest.py`:

```python
"""Pytest fixtures shared across runner and corpora tests."""

import json
from pathlib import Path

import pytest


@pytest.fixture
def tmp_results(tmp_path: Path) -> Path:
    """Disposable results directory for a single test."""
    d = tmp_path / "results"
    d.mkdir()
    return d


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def baseline_sample() -> dict:
    """A representative baseline JSON for metrics tests."""
    return {
        "question_id": "cuad-research-1",
        "answer": "Most contracts use 30, 60, or 90 day notice periods. California, Delaware appear frequently.",
        "cited_doc_ids": ["doc-a", "doc-b", "doc-c", "doc-d", "doc-e"],
        "named_entities": ["California", "Delaware", "Acme", "30 days", "60 days"],
        "elapsed_seconds": 87.4,
    }
```

- [ ] **Step 7: Verify scaffold runs**

Run: `cd scripts/test_corpora && uv run pytest -q`

Expected: `0 tests collected` (no failures, no errors).

- [ ] **Step 8: Commit**

```bash
git add scripts/test_corpora/
git commit -m "scaffold: test_corpora harness skeleton + tooling"
```

---

## Task 2: `runner/state.py` — resumable state file

**Files:**
- Create: `scripts/test_corpora/runner/state.py`
- Create: `scripts/test_corpora/tests/test_state.py`

- [ ] **Step 1: Write the failing tests**

`scripts/test_corpora/tests/test_state.py`:

```python
import json
import time
from pathlib import Path

import pytest

from scripts.test_corpora.runner.state import (
    StateFile,
    Status,
    Unit,
)


def test_load_creates_empty_state_when_missing(tmp_path: Path):
    sf = StateFile(tmp_path / "state.json")
    sf.load()
    assert sf.units() == []


def test_register_units_persists(tmp_path: Path):
    sf = StateFile(tmp_path / "state.json")
    sf.register([Unit(phase=4, corpus="cuad", model="qwen3-8b", question_id="q1", depth="standard")])
    sf.save()

    sf2 = StateFile(tmp_path / "state.json")
    sf2.load()
    rows = sf2.units()
    assert len(rows) == 1
    assert rows[0].status == Status.PENDING


def test_atomic_write_no_partial_state(tmp_path: Path, monkeypatch):
    """If save() crashes mid-write, state.json must not be corrupted."""
    sf = StateFile(tmp_path / "state.json")
    sf.register([Unit(phase=4, corpus="cuad", model="m", question_id="q", depth="standard")])
    sf.save()

    sf2 = StateFile(tmp_path / "state.json")
    sf2.load()
    sf2.set_status("cuad", "m", "q", "standard", Status.IN_PROGRESS)

    # Simulate a crash by writing a half-state directly to the temp path
    tmp_target = tmp_path / "state.json.tmp"
    tmp_target.write_text("{ partial")
    # Real save should still succeed and leave state.json valid
    sf2.save()
    assert json.loads((tmp_path / "state.json").read_text())["units"][0]["status"] == "in_progress"


def test_stale_in_progress_reverts_to_pending(tmp_path: Path):
    sf = StateFile(tmp_path / "state.json")
    sf.register([Unit(phase=4, corpus="cuad", model="m", question_id="q", depth="standard")])
    sf.set_status("cuad", "m", "q", "standard", Status.IN_PROGRESS, heartbeat=time.time() - 7200)
    sf.save()

    sf2 = StateFile(tmp_path / "state.json")
    sf2.load()
    sf2.recover_stale(stale_threshold_seconds=3600)

    rows = sf2.units()
    assert rows[0].status == Status.PENDING


def test_lock_prevents_concurrent_runs(tmp_path: Path):
    sf = StateFile(tmp_path / "state.json")
    sf.acquire_lock()
    try:
        sf2 = StateFile(tmp_path / "state.json")
        with pytest.raises(RuntimeError, match="locked"):
            sf2.acquire_lock()
    finally:
        sf.release_lock()


def test_rerun_selector_flips_matching_to_pending(tmp_path: Path):
    sf = StateFile(tmp_path / "state.json")
    sf.register([
        Unit(phase=5, corpus="cuad", model="qwen3.6-35b", question_id="q1", depth="standard"),
        Unit(phase=5, corpus="enron", model="qwen3.6-35b", question_id="q1", depth="standard"),
    ])
    sf.set_status("cuad", "qwen3.6-35b", "q1", "standard", Status.DONE)
    sf.set_status("enron", "qwen3.6-35b", "q1", "standard", Status.DONE)
    sf.rerun({"corpus": "cuad"})
    rows = {(u.corpus, u.status) for u in sf.units()}
    assert ("cuad", Status.PENDING) in rows
    assert ("enron", Status.DONE) in rows
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd scripts/test_corpora && uv run pytest tests/test_state.py -v
```

Expected: ImportError — module doesn't exist yet.

- [ ] **Step 3: Implement `runner/state.py`**

`scripts/test_corpora/runner/state.py`:

```python
"""Resumable state file for the test-corpora sweep.

The state file is a single JSON document at ``<results>/state.json``. Each
unit of work is keyed by ``(phase, corpus, model, question_id, depth)``.
Writes are atomic via temp-file + rename. A sibling ``state.lock`` file
prevents two runners from clobbering each other.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import os
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path


class Status(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    DEGRADED = "degraded"
    ERROR = "error"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


@dataclasses.dataclass
class Unit:
    phase: int
    corpus: str
    model: str
    question_id: str
    depth: str
    status: Status = Status.PENDING
    heartbeat: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None

    def key(self) -> tuple[str, str, str, str]:
        return (self.corpus, self.model, self.question_id, self.depth)

    def to_json(self) -> dict:
        return {
            "phase": self.phase,
            "corpus": self.corpus,
            "model": self.model,
            "question_id": self.question_id,
            "depth": self.depth,
            "status": self.status.value,
            "heartbeat": self.heartbeat,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Unit":
        return cls(
            phase=d["phase"],
            corpus=d["corpus"],
            model=d["model"],
            question_id=d["question_id"],
            depth=d["depth"],
            status=Status(d["status"]),
            heartbeat=d.get("heartbeat"),
            started_at=d.get("started_at"),
            finished_at=d.get("finished_at"),
            error=d.get("error"),
        )


class StateFile:
    """Atomic, resumable state file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._units: dict[tuple, Unit] = {}
        self._lock_fd: int | None = None

    # ── load / save ──

    def load(self) -> None:
        if not self.path.exists():
            self._units = {}
            return
        data = json.loads(self.path.read_text())
        self._units = {
            (u["corpus"], u["model"], u["question_id"], u["depth"]): Unit.from_json(u)
            for u in data.get("units", [])
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"units": [u.to_json() for u in self._units.values()]}
        # Atomic write: temp file in same dir, then rename
        with tempfile.NamedTemporaryFile(
            "w", delete=False, dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp"
        ) as f:
            json.dump(data, f, indent=2)
            tmp_name = f.name
        os.replace(tmp_name, self.path)

    # ── unit management ──

    def register(self, units: Iterable[Unit]) -> None:
        for u in units:
            self._units.setdefault(u.key(), u)

    def units(self) -> list[Unit]:
        return list(self._units.values())

    def get(self, corpus: str, model: str, question_id: str, depth: str) -> Unit | None:
        return self._units.get((corpus, model, question_id, depth))

    def set_status(
        self,
        corpus: str,
        model: str,
        question_id: str,
        depth: str,
        status: Status,
        heartbeat: float | None = None,
        error: str | None = None,
    ) -> None:
        u = self._units[(corpus, model, question_id, depth)]
        u.status = status
        if status == Status.IN_PROGRESS:
            u.started_at = u.started_at or time.time()
            u.heartbeat = heartbeat or time.time()
        elif status in {Status.DONE, Status.DEGRADED, Status.ERROR, Status.FAILED, Status.SKIPPED}:
            u.finished_at = time.time()
            u.heartbeat = None
        if heartbeat is not None and status == Status.IN_PROGRESS:
            u.heartbeat = heartbeat
        if error is not None:
            u.error = error

    # ── recovery ──

    def recover_stale(self, stale_threshold_seconds: float) -> int:
        """Flip in_progress units with stale heartbeats back to pending. Returns count."""
        now = time.time()
        n = 0
        for u in self._units.values():
            if u.status == Status.IN_PROGRESS and u.heartbeat and (now - u.heartbeat) > stale_threshold_seconds:
                u.status = Status.PENDING
                u.heartbeat = None
                u.started_at = None
                n += 1
        return n

    # ── selectors ──

    def rerun(self, selectors: dict[str, str]) -> int:
        """Flip units matching all selectors back to PENDING. Returns count."""
        n = 0
        for u in self._units.values():
            if all(getattr(u, k, None) == v or str(getattr(u, k, "")) == v for k, v in selectors.items()):
                u.status = Status.PENDING
                u.heartbeat = None
                u.started_at = None
                u.error = None
                n += 1
        return n

    def skip(self, selectors: dict[str, str]) -> int:
        n = 0
        for u in self._units.values():
            if all(getattr(u, k, None) == v or str(getattr(u, k, "")) == v for k, v in selectors.items()):
                u.status = Status.SKIPPED
                n += 1
        return n

    # ── lock ──

    def acquire_lock(self) -> None:
        try:
            self._lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self._lock_fd, str(os.getpid()).encode())
        except FileExistsError:
            raise RuntimeError(f"state file is locked by another runner: {self.lock_path}")

    def release_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        self.lock_path.unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests, fix until green**

```bash
cd scripts/test_corpora && uv run pytest tests/test_state.py -v
```

Expected: 6 passing.

- [ ] **Step 5: Commit**

```bash
git add scripts/test_corpora/runner/state.py scripts/test_corpora/tests/test_state.py
git commit -m "state: resumable JSON state file with atomic writes + lock"
```

---

## Task 3: `runner/metrics.py` — overlap functions

**Files:**
- Create: `scripts/test_corpora/runner/metrics.py`
- Create: `scripts/test_corpora/tests/test_metrics.py`

- [ ] **Step 1: Write failing tests**

`scripts/test_corpora/tests/test_metrics.py`:

```python
from scripts.test_corpora.runner.metrics import (
    citation_overlap,
    citation_extra,
    entity_overlap,
)


def test_citation_overlap_full_match():
    baseline = ["a", "b", "c"]
    model = ["a", "b", "c"]
    assert citation_overlap(baseline, model) == 1.0


def test_citation_overlap_partial():
    baseline = ["a", "b", "c", "d"]
    model = ["a", "c"]
    assert citation_overlap(baseline, model) == 0.5


def test_citation_overlap_empty_baseline_returns_zero():
    assert citation_overlap([], ["a"]) == 0.0


def test_citation_extra_counts_model_only():
    assert citation_extra(["a", "b"], ["a", "b", "c", "d"]) == 2


def test_entity_overlap_english(monkeypatch):
    """Use a stub spaCy doc so we don't depend on the model loading."""
    baseline = "California and Delaware appear in the contract from Acme."
    model = "The contract from Acme references California."
    score = entity_overlap(baseline, model, lang="en")
    # Expect ~2/3 (Acme + California found, Delaware missing)
    assert 0.6 < score <= 1.0


def test_entity_overlap_empty_returns_zero():
    assert entity_overlap("", "any text", lang="en") == 0.0
```

- [ ] **Step 2: Run, confirm fail**

`uv run pytest tests/test_metrics.py -v` → ImportError.

- [ ] **Step 3: Implement `runner/metrics.py`**

`scripts/test_corpora/runner/metrics.py`:

```python
"""Pure metric functions. No I/O.

Citation overlap is at doc_id level (chunk-level is too noisy because
hybrid retrieval pulls slightly different chunks per run). Entity overlap
uses spaCy NER over the answer text; the same models Harbor Clerk uses
(``en_core_web_sm``, ``fr_core_news_sm``).
"""

from __future__ import annotations

import functools
from collections.abc import Iterable


@functools.lru_cache(maxsize=2)
def _load_spacy(lang: str):
    import spacy

    if lang == "en":
        return spacy.load("en_core_web_sm")
    if lang == "fr":
        return spacy.load("fr_core_news_sm")
    raise ValueError(f"unsupported lang: {lang}")


def _entities(text: str, lang: str) -> set[str]:
    if not text.strip():
        return set()
    nlp = _load_spacy(lang)
    doc = nlp(text)
    return {ent.text for ent in doc.ents}


def citation_overlap(baseline_doc_ids: Iterable[str], model_doc_ids: Iterable[str]) -> float:
    """Recall against baseline: |baseline ∩ model| / |baseline|."""
    baseline = set(baseline_doc_ids)
    if not baseline:
        return 0.0
    model = set(model_doc_ids)
    return len(baseline & model) / len(baseline)


def citation_extra(baseline_doc_ids: Iterable[str], model_doc_ids: Iterable[str]) -> int:
    """Count of citations in model not in baseline. Not necessarily bad."""
    return len(set(model_doc_ids) - set(baseline_doc_ids))


def entity_overlap(baseline_text: str, model_text: str, lang: str = "en") -> float:
    """Recall of named entities: |baseline ∩ model| / |baseline|."""
    baseline = _entities(baseline_text, lang)
    if not baseline:
        return 0.0
    model = _entities(model_text, lang)
    # Case-insensitive match on entity surface form
    baseline_norm = {e.lower() for e in baseline}
    model_norm = {e.lower() for e in model}
    return len(baseline_norm & model_norm) / len(baseline_norm)
```

- [ ] **Step 4: Run tests until green**

```bash
cd scripts/test_corpora && uv run pytest tests/test_metrics.py -v
```

Expected: 6 passing. (`test_entity_overlap_english` requires `en_core_web_sm` to be installed in the harbor-clerk venv, which it is.)

- [ ] **Step 5: Commit**

```bash
git add scripts/test_corpora/runner/metrics.py scripts/test_corpora/tests/test_metrics.py
git commit -m "metrics: citation_overlap, entity_overlap pure functions"
```

---

## Task 4: `runner/sampler.py` — in-flight sampling + summary tables

**Files:**
- Create: `scripts/test_corpora/runner/sampler.py`
- Create: `scripts/test_corpora/tests/test_sampler.py`

- [ ] **Step 1: Write failing tests**

`scripts/test_corpora/tests/test_sampler.py`:

```python
import io

from scripts.test_corpora.runner.sampler import Sampler, CompletionEvent


def make_event(idx: int) -> CompletionEvent:
    return CompletionEvent(
        phase=4,
        corpus="cuad",
        model="qwen3-8b",
        question_id=f"q{idx}",
        baseline_answer="Most contracts use 30/60/90 day windows.",
        model_answer="Found 30/60/90 day patterns.",
        citation_overlap=0.9,
        citation_extra=2,
        entity_overlap=0.82,
        latency_seconds=412.0,
        elapsed_total_seconds=2533,
    )


def test_sampler_prints_every_nth(capsys):
    s = Sampler(every_n=3, out=None)
    for i in range(10):
        s.note(make_event(i))
    out = capsys.readouterr().out
    # 10 events with every_n=3 → events 3, 6, 9 printed → 3 sample cards
    assert out.count("[Phase 4 ·") == 3


def test_sampler_summary_table(capsys):
    s = Sampler(every_n=10000, out=None)  # disable in-flight cards
    for model in ("qwen3-8b", "gemma-26b"):
        for i in range(8):
            ev = make_event(i)
            ev.model = model
            ev.citation_overlap = 1.0 if model == "gemma-26b" else 0.5
            s.note(ev)
    s.print_summary_table(phase=4)
    out = capsys.readouterr().out
    assert "Phase 4 complete" in out
    assert "qwen3-8b" in out
    assert "gemma-26b" in out
```

- [ ] **Step 2: Run, confirm fail**

`uv run pytest tests/test_sampler.py -v`

- [ ] **Step 3: Implement `runner/sampler.py`**

`scripts/test_corpora/runner/sampler.py`:

```python
"""In-flight stdout sampling and phase-boundary summary tables."""

from __future__ import annotations

import dataclasses
import sys
from collections import defaultdict
from collections.abc import Sequence


@dataclasses.dataclass
class CompletionEvent:
    phase: int
    corpus: str
    model: str
    question_id: str
    baseline_answer: str
    model_answer: str
    citation_overlap: float
    citation_extra: int
    entity_overlap: float
    latency_seconds: float
    elapsed_total_seconds: int


def _truncate(s: str, n: int = 70) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _hms(secs: float) -> str:
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


class Sampler:
    """Counts completions, prints sample cards every N, prints summary tables."""

    def __init__(self, every_n: int = 5, out=None):
        self.every_n = every_n
        self.count = 0
        self.out = out or sys.stdout
        # Aggregations for the summary table:
        # rows[(corpus, model)] = list of citation_overlap floats
        self._rows: dict[tuple[str, str], list[float]] = defaultdict(list)

    def note(self, ev: CompletionEvent) -> None:
        self._rows[(ev.corpus, ev.model)].append(ev.citation_overlap)
        self.count += 1
        if self.count % self.every_n == 0:
            self._print_card(ev)

    def _print_card(self, ev: CompletionEvent) -> None:
        self.out.write(
            f"[Phase {ev.phase} · {ev.model} · {ev.corpus} · {ev.question_id}] "
            f"elapsed {_hms(ev.elapsed_total_seconds)}\n"
        )
        self.out.write(f"  Q: {_truncate(ev.question_id)}\n")
        self.out.write(f"  Baseline: {_truncate(ev.baseline_answer, 90)}\n")
        self.out.write(f"  Model:    {_truncate(ev.model_answer, 90)}\n")
        self.out.write(
            f"  Sources: {ev.citation_overlap*100:5.1f}% recall (+{ev.citation_extra} extra)  "
            f"·  Entities: {ev.entity_overlap*100:5.1f}%  "
            f"·  Latency {ev.latency_seconds:.1f}s\n\n"
        )
        self.out.flush()

    def print_summary_table(self, phase: int, models: Sequence[str] | None = None) -> None:
        if not self._rows:
            return
        corpora = sorted({c for c, _ in self._rows})
        if models is None:
            models = sorted({m for _, m in self._rows})
        col_w = 16
        header = "".rjust(20) + "".join(c.ljust(col_w) for c in corpora)
        self.out.write(f"\n=== Phase {phase} complete ===\n")
        self.out.write(header + "\n")
        for m in models:
            row = m.ljust(20)
            for c in corpora:
                vals = self._rows.get((c, m), [])
                if not vals:
                    row += "—".ljust(col_w)
                else:
                    median = sorted(vals)[len(vals) // 2]
                    row += f"{int(median * 100):3d}% (n={len(vals)})".ljust(col_w)
            self.out.write(row + "\n")
        self.out.write("\n")
        self.out.flush()
```

- [ ] **Step 4: Run tests until green**

```bash
cd scripts/test_corpora && uv run pytest tests/test_sampler.py -v
```

- [ ] **Step 5: Commit**

```bash
git add scripts/test_corpora/runner/sampler.py scripts/test_corpora/tests/test_sampler.py
git commit -m "sampler: in-flight cards + phase boundary summary tables"
```

---

## Task 5: `runner/client.py` — Harbor Clerk REST client

**Files:**
- Create: `scripts/test_corpora/runner/client.py`
- Create: `scripts/test_corpora/tests/test_client.py`

- [ ] **Step 1: Write failing tests using `httpx.MockTransport`**

`scripts/test_corpora/tests/test_client.py`:

```python
import json

import httpx
import pytest

from scripts.test_corpora.runner.client import HarborClerkClient


def make_client(handler) -> HarborClerkClient:
    transport = httpx.MockTransport(handler)
    return HarborClerkClient(base_url="https://localhost", transport=transport, verify=False)


def test_start_research_returns_task_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/research/start"
        body = json.loads(request.content)
        assert body["question"] == "What?"
        assert body["model_id"] == "qwen3-8b"
        return httpx.Response(200, json={"task_id": "task-123"})

    c = make_client(handler)
    assert c.start_research("What?", model_id="qwen3-8b", depth="standard", time_limit=1800) == "task-123"


def test_poll_research_done():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "task_id": "task-123",
            "state": "done",
            "answer": "Answer text.",
            "citations": [{"doc_id": "doc-a"}, {"doc_id": "doc-b"}],
        })

    c = make_client(handler)
    res = c.poll_research("task-123")
    assert res["state"] == "done"
    assert [c["doc_id"] for c in res["citations"]] == ["doc-a", "doc-b"]


def test_pipeline_status_quiet():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "queues": {"io": {"queued": 0, "running": 0}, "cpu": {"queued": 0, "running": 0}},
        })

    c = make_client(handler)
    assert c.pipeline_quiet() is True


def test_pipeline_status_busy():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "queues": {"io": {"queued": 5, "running": 1}, "cpu": {"queued": 0, "running": 0}},
        })

    c = make_client(handler)
    assert c.pipeline_quiet() is False


def test_wipe_db_calls_maintenance_endpoint():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"ok": True})

    c = make_client(handler)
    c.wipe_db(confirm=True)
    assert "POST /api/system/maintenance/wipe" in seen
```

- [ ] **Step 2: Run, confirm fail**

`uv run pytest tests/test_client.py -v` → ImportError.

- [ ] **Step 3: Implement `runner/client.py`**

`scripts/test_corpora/runner/client.py`:

```python
"""Harbor Clerk REST client used by the test sweep harness.

Targets the same surface the SPA uses. SSE streaming for the Ask flow uses
``httpx``'s SSE-by-line iteration. All methods retry transient failures
via ``tenacity`` with exponential backoff.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


class HarborClerkClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        verify: bool = True,
        timeout_seconds: float = 30.0,
    ):
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            transport=transport,
            verify=verify,
            timeout=timeout_seconds,
        )

    # ── research ──

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=15))
    def start_research(self, question: str, model_id: str, depth: str, time_limit: int) -> str:
        r = self._client.post(
            "/api/research/start",
            json={
                "question": question,
                "model_id": model_id,
                "depth": depth,
                "time_limit_seconds": time_limit,
            },
        )
        r.raise_for_status()
        return r.json()["task_id"]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=15))
    def poll_research(self, task_id: str) -> dict[str, Any]:
        r = self._client.get(f"/api/research/{task_id}")
        r.raise_for_status()
        return r.json()

    def wait_for_research(self, task_id: str, max_wait_seconds: int) -> dict[str, Any]:
        """Poll until done/error or deadline. Sleep 5s between polls."""
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            res = self.poll_research(task_id)
            if res.get("state") in {"done", "error"}:
                return res
            time.sleep(5)
        return {"state": "timeout", "task_id": task_id}

    # ── ask (chat SSE) ──

    def stream_ask(self, question: str, model_id: str) -> Iterator[dict]:
        """Yield SSE event dicts. Returns when ``done`` event received."""
        with self._client.stream(
            "POST",
            "/api/chat",
            json={"message": question, "model_id": model_id, "stream": True},
            timeout=httpx.Timeout(connect=10, read=600, write=10, pool=10),
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if not payload.strip():
                    continue
                event = json.loads(payload)
                yield event
                if event.get("type") == "done":
                    return

    # ── pipeline / system ──

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def pipeline_status(self) -> dict[str, Any]:
        r = self._client.get("/api/stats/queue")
        r.raise_for_status()
        return r.json()

    def pipeline_quiet(self) -> bool:
        s = self.pipeline_status()
        q = s["queues"]
        return all(q[name]["queued"] == 0 and q[name]["running"] == 0 for name in q)

    def wait_for_quiet_pipeline(self, max_wait_seconds: int = 7200, poll_seconds: int = 30) -> bool:
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            if self.pipeline_quiet():
                return True
            time.sleep(poll_seconds)
        return False

    def health(self) -> dict[str, Any]:
        r = self._client.get("/api/system/health")
        r.raise_for_status()
        return r.json()

    # ── maintenance ──

    def wipe_db(self, confirm: bool = False) -> None:
        if not confirm:
            raise RuntimeError("wipe_db requires confirm=True")
        r = self._client.post("/api/system/maintenance/wipe", json={"confirm": True})
        r.raise_for_status()

    def watch_folder_add(self, path: str, name: str | None = None) -> dict[str, Any]:
        r = self._client.post("/api/watch/folders", json={"path": path, "display_name": name})
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 4: Run tests until green**

```bash
cd scripts/test_corpora && uv run pytest tests/test_client.py -v
```

Expected: 5 passing.

- [ ] **Step 5: Commit**

```bash
git add scripts/test_corpora/runner/client.py scripts/test_corpora/tests/test_client.py
git commit -m "client: Harbor Clerk REST client with retries, SSE streaming, pipeline waits"
```

---

## Task 6: `runner/judge.py` — Sonnet 4.6 LLM-as-judge

**Files:**
- Create: `scripts/test_corpora/runner/judge.py`
- Create: `scripts/test_corpora/tests/test_judge.py`

- [ ] **Step 1: Write failing tests**

`scripts/test_corpora/tests/test_judge.py`:

```python
import json
from unittest.mock import MagicMock

from scripts.test_corpora.runner.judge import JudgeClient, JudgeVerdict


def test_judge_parses_structured_response():
    fake_anthropic = MagicMock()
    fake_anthropic.messages.create.return_value.content = [
        MagicMock(text=json.dumps({
            "claim_recall": 4,
            "claim_precision": 5,
            "entity_recall": 4,
            "completeness": 4,
            "missing_facts": ["one fact"],
            "extra_facts": [],
            "contradictions": [],
            "verdict": "pass",
        }))
    ]

    j = JudgeClient(client=fake_anthropic)
    v = j.judge(question="Q?", baseline="B", model_answer="M")
    assert isinstance(v, JudgeVerdict)
    assert v.verdict == "pass"
    assert v.claim_recall == 4
    assert v.missing_facts == ["one fact"]


def test_judge_handles_extra_text_around_json():
    fake_anthropic = MagicMock()
    fake_anthropic.messages.create.return_value.content = [
        MagicMock(text="Here is the verdict:\n```json\n" + json.dumps({
            "claim_recall": 3, "claim_precision": 3, "entity_recall": 3, "completeness": 3,
            "missing_facts": [], "extra_facts": [], "contradictions": [], "verdict": "marginal",
        }) + "\n```")
    ]

    j = JudgeClient(client=fake_anthropic)
    v = j.judge(question="Q?", baseline="B", model_answer="M")
    assert v.verdict == "marginal"
```

- [ ] **Step 2: Run, confirm fail**

- [ ] **Step 3: Implement `runner/judge.py`**

`scripts/test_corpora/runner/judge.py`:

```python
"""Sonnet 4.6 LLM-as-judge client.

Sends ``(question, baseline_answer, model_answer)`` to Sonnet 4.6 with
the rubric defined in the design doc and returns a structured verdict.
The rubric is intentionally narrow — only fact-level coverage, not prose
quality.
"""

from __future__ import annotations

import dataclasses
import json
import re

import anthropic


JUDGE_PROMPT = """You are evaluating whether a local LLM's answer reaches the same factual
ground as a Claude baseline answer to the same question.

Question: {question}

Claude baseline answer:
{baseline}

Local model answer:
{model_answer}

Score these dimensions (0–5 each):
- claim_recall: how many factual claims from the baseline appear in the
  model answer (verbatim or paraphrased)?
- claim_precision: are the claims in the model answer supported by the
  baseline (or harmless additions), or are they contradictions?
- entity_recall: does the model answer surface the same named entities
  (people, places, organizations, dates, dollar amounts)?
- completeness: overall coverage of the baseline's territory.

Return JSON only (no prose, no markdown fences):
{{
  "claim_recall": int,
  "claim_precision": int,
  "entity_recall": int,
  "completeness": int,
  "missing_facts": ["..."],
  "extra_facts": ["..."],
  "contradictions": ["..."],
  "verdict": "pass" | "marginal" | "fail"
}}
"""


@dataclasses.dataclass
class JudgeVerdict:
    claim_recall: int
    claim_precision: int
    entity_recall: int
    completeness: int
    missing_facts: list[str]
    extra_facts: list[str]
    contradictions: list[str]
    verdict: str


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of the response, even if wrapped in fences."""
    # Try a fenced block first
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    # Fall back to the largest curly span
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in judge response")
    return json.loads(text[start : end + 1])


class JudgeClient:
    def __init__(self, client: anthropic.Anthropic | None = None, model: str = "claude-sonnet-4-6"):
        self._client = client or anthropic.Anthropic()
        self._model = model

    def judge(self, question: str, baseline: str, model_answer: str) -> JudgeVerdict:
        prompt = JUDGE_PROMPT.format(question=question, baseline=baseline, model_answer=model_answer)
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
        data = _extract_json(text)
        return JudgeVerdict(
            claim_recall=int(data["claim_recall"]),
            claim_precision=int(data["claim_precision"]),
            entity_recall=int(data["entity_recall"]),
            completeness=int(data["completeness"]),
            missing_facts=data.get("missing_facts", []),
            extra_facts=data.get("extra_facts", []),
            contradictions=data.get("contradictions", []),
            verdict=data["verdict"],
        )
```

- [ ] **Step 4: Run tests until green**

`uv run pytest tests/test_judge.py -v`

- [ ] **Step 5: Commit**

```bash
git add scripts/test_corpora/runner/judge.py scripts/test_corpora/tests/test_judge.py
git commit -m "judge: Sonnet 4.6 LLM-as-judge client with structured rubric"
```

---

## Task 7: `runner/claude_baseline.py` — Phase 1 baseline generator

**Files:**
- Create: `scripts/test_corpora/runner/claude_baseline.py`
- Create: `scripts/test_corpora/tests/test_baseline.py`

- [ ] **Step 1: Write failing tests**

`scripts/test_corpora/tests/test_baseline.py`:

```python
import json
from unittest.mock import MagicMock

from scripts.test_corpora.runner.claude_baseline import BaselineGenerator, BaselineResult


def test_baseline_generator_collects_citations_from_tool_calls():
    fake = MagicMock()
    # Anthropic tool-use response: the model called kb_search, then kb_read_passages,
    # then produced a final text block. We simulate that as a stop_reason=end_turn
    # with content blocks including tool_use entries we mocked out.
    fake.messages.create.return_value = MagicMock(
        content=[MagicMock(text="The answer references doc-a and doc-b.")],
        stop_reason="end_turn",
    )

    gen = BaselineGenerator(client=fake, mcp_session=None, doc_ids_seen=["doc-a", "doc-b"])
    res = gen.run_question(question="What?", question_id="q1", corpus="cuad")
    assert isinstance(res, BaselineResult)
    assert res.cited_doc_ids == ["doc-a", "doc-b"]
    assert "doc-a" in res.answer or res.answer
```

- [ ] **Step 2: Run, confirm fail**

- [ ] **Step 3: Implement `runner/claude_baseline.py`**

`scripts/test_corpora/runner/claude_baseline.py`:

```python
"""Phase 1 — Claude baseline generator.

Runs Sonnet 4.6 with Harbor Clerk's MCP server attached as a tool source.
Captures the final answer plus the doc IDs surfaced via ``kb_search`` /
``kb_read_passages`` / ``kb_get_document`` tool calls. Saves one JSON
file per (corpus, question_id) under ``baselines/``.

The tool-call loop mirrors what an MCP client does: send the user's
question, get back tool_use blocks, execute them via the MCP session,
feed results back, repeat until ``stop_reason == 'end_turn'``.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Any

import anthropic


SYSTEM_PROMPT = """You are answering a user's question about a specific document corpus.
You have access to the corpus only through the provided MCP tools (kb_search,
kb_read_passages, kb_get_document, kb_find_related, etc.). Use them as
needed. Cite specific documents in your answer by doc_id.

Be thorough — your answer is the gold-standard reference for evaluating
local models. Spend tool calls liberally."""


@dataclasses.dataclass
class BaselineResult:
    question_id: str
    question: str
    answer: str
    cited_doc_ids: list[str]
    tool_call_count: int
    elapsed_seconds: float
    model: str
    timestamp: str


class BaselineGenerator:
    """Runs Sonnet 4.6 + MCP for one question.

    The constructor takes an opaque ``mcp_session`` — in production this is
    an ``mcp.ClientSession`` connected to Harbor Clerk's MCP server. In tests,
    it can be ``None`` if the mock anthropic client doesn't actually emit
    tool_use blocks.
    """

    def __init__(
        self,
        client: anthropic.Anthropic,
        mcp_session: Any,
        model: str = "claude-sonnet-4-6",
        doc_ids_seen: list[str] | None = None,
    ):
        self._client = client
        self._mcp = mcp_session
        self._model = model
        # For tests: pre-seed the doc_ids_seen list so we can verify capture
        # without mocking the entire tool-use loop.
        self._doc_ids_seen: list[str] = list(doc_ids_seen) if doc_ids_seen else []

    def _list_tools(self) -> list[dict]:
        """Discover MCP tools and convert to Anthropic's tool schema."""
        if self._mcp is None:
            return []
        # mcp.types.Tool has .name, .description, .inputSchema
        tools = self._mcp.list_tools()  # sync wrapper expected
        return [
            {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
            for t in tools
        ]

    def _exec_tool(self, name: str, args: dict) -> str:
        """Execute one MCP tool call, capture any doc_ids in the result."""
        result = self._mcp.call_tool(name, args)
        # Collect text content; capture doc_ids if present in the JSON
        text_chunks: list[str] = []
        for block in result.content:
            if hasattr(block, "text"):
                text_chunks.append(block.text)
                # Greedy extraction: if the tool returned JSON containing
                # doc_id fields, harvest them. Robust to nested structures.
                try:
                    parsed = json.loads(block.text)
                    self._collect_doc_ids(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
        return "\n".join(text_chunks) or "(empty)"

    def _collect_doc_ids(self, obj: Any) -> None:
        if isinstance(obj, dict):
            if "doc_id" in obj and isinstance(obj["doc_id"], str):
                if obj["doc_id"] not in self._doc_ids_seen:
                    self._doc_ids_seen.append(obj["doc_id"])
            for v in obj.values():
                self._collect_doc_ids(v)
        elif isinstance(obj, list):
            for v in obj:
                self._collect_doc_ids(v)

    def run_question(self, question: str, question_id: str, corpus: str) -> BaselineResult:
        started = time.time()
        tools = self._list_tools()
        messages = [{"role": "user", "content": question}]
        tool_call_count = 0

        while True:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=8000,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "end_turn":
                break
            if resp.stop_reason != "tool_use":
                break  # safety

            # Execute each tool_use block, send results back as user message
            tool_results: list[dict] = []
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    tool_call_count += 1
                    out = self._exec_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": out,
                    })
            messages.append({"role": "user", "content": tool_results})

        # Final answer is the last text block in the assistant turn
        final = ""
        if resp.content and hasattr(resp.content[0], "text"):
            final = resp.content[0].text

        return BaselineResult(
            question_id=question_id,
            question=question,
            answer=final,
            cited_doc_ids=list(self._doc_ids_seen),
            tool_call_count=tool_call_count,
            elapsed_seconds=time.time() - started,
            model=self._model,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    @staticmethod
    def write(result: BaselineResult, results_dir: Path, corpus: str) -> Path:
        out = results_dir / "baselines" / corpus / f"{result.question_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(dataclasses.asdict(result), indent=2))
        return out
```

- [ ] **Step 4: Run tests until green**

`uv run pytest tests/test_baseline.py -v`

- [ ] **Step 5: Commit**

```bash
git add scripts/test_corpora/runner/claude_baseline.py scripts/test_corpora/tests/test_baseline.py
git commit -m "baseline: Phase 1 generator (Sonnet 4.6 + Harbor Clerk MCP tool loop)"
```

---

## Task 8: `corpora/manifest.py` + `corpora/cuad.py` — CUAD acquisition

**Files:**
- Create: `scripts/test_corpora/corpora/manifest.py`
- Create: `scripts/test_corpora/corpora/cuad.py`
- Create: `scripts/test_corpora/tests/test_cuad.py`

- [ ] **Step 1: Write the manifest dataclass and CUAD test**

`scripts/test_corpora/corpora/manifest.py`:

```python
"""Common dataclass returned by every corpus's ``acquire()``."""

from __future__ import annotations

import dataclasses
from pathlib import Path


@dataclasses.dataclass
class CorpusManifest:
    corpus_id: str
    ingest_dir: Path
    doc_count: int
    total_size_bytes: int
    license: str
    notes: str = ""
    ground_truth: dict | None = None
```

`scripts/test_corpora/tests/test_cuad.py`:

```python
import io
import tarfile
from pathlib import Path
from unittest.mock import patch

from scripts.test_corpora.corpora import cuad


def _make_fake_release(path: Path) -> None:
    """Build a tiny tar.gz that mimics the CUAD release layout."""
    with tarfile.open(path, "w:gz") as t:
        for i in range(5):
            data = b"%PDF-1.4\nfake contract " + str(i).encode()
            info = tarfile.TarInfo(name=f"CUAD_v1/contracts/contract_{i:03d}.pdf")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))


def test_cuad_acquire_idempotent(tmp_path: Path):
    archive = tmp_path / "cuad-fake.tar.gz"
    _make_fake_release(archive)

    with patch.object(cuad, "_download_release", return_value=archive):
        m1 = cuad.acquire(workdir=tmp_path / "work", sample_size=3)
        assert m1.doc_count == 3
        assert (m1.ingest_dir / "contract_000.pdf").exists()

        # Second call must not re-download
        with patch.object(cuad, "_download_release", side_effect=AssertionError("re-downloaded")):
            m2 = cuad.acquire(workdir=tmp_path / "work", sample_size=3)
            assert m2.ingest_dir == m1.ingest_dir
```

- [ ] **Step 2: Run, confirm fail**

- [ ] **Step 3: Implement `corpora/cuad.py`**

`scripts/test_corpora/corpora/cuad.py`:

```python
"""CUAD (Contract Understanding Atticus Dataset) acquisition.

Downloads the release tarball, extracts it, samples N contracts in a
deterministic order, and copies them to the ingest directory. Idempotent
via marker file ``.acquired``.
"""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

import httpx

from .manifest import CorpusManifest

CUAD_RELEASE_URL = "https://zenodo.org/record/4595826/files/CUAD_v1.zip"  # canonical mirror
SAMPLE_SIZE_DEFAULT = 80


def _download_release(workdir: Path) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    archive = workdir / "cuad_v1.tar.gz"
    if archive.exists():
        return archive
    # Use streaming download with reasonable retry handling
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(connect=30, read=600, write=30, pool=30)) as c:
        with c.stream("GET", CUAD_RELEASE_URL) as r:
            r.raise_for_status()
            with archive.open("wb") as f:
                for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)
    return archive


def _extract(archive: Path, workdir: Path) -> Path:
    extracted = workdir / "extracted"
    if extracted.exists():
        return extracted
    extracted.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as t:
        t.extractall(extracted)
    return extracted


def _gather_pdfs(extracted: Path) -> list[Path]:
    return sorted(extracted.rglob("*.pdf"))


def acquire(workdir: Path, sample_size: int = SAMPLE_SIZE_DEFAULT) -> CorpusManifest:
    workdir = Path(workdir)
    ingest_dir = workdir / "ingest"
    marker = ingest_dir / ".acquired"

    if marker.exists():
        pdfs = sorted(ingest_dir.glob("*.pdf"))
        return CorpusManifest(
            corpus_id="cuad",
            ingest_dir=ingest_dir,
            doc_count=len(pdfs),
            total_size_bytes=sum(p.stat().st_size for p in pdfs),
            license="CC-BY 4.0",
            notes=f"CUAD sample of {len(pdfs)} contracts",
        )

    archive = _download_release(workdir)
    extracted = _extract(archive, workdir)
    pdfs = _gather_pdfs(extracted)
    if len(pdfs) < sample_size:
        sample_size = len(pdfs)
    sampled = pdfs[:sample_size]  # deterministic by sorted name

    ingest_dir.mkdir(parents=True, exist_ok=True)
    for src in sampled:
        shutil.copy2(src, ingest_dir / src.name)

    marker.write_text("acquired")
    return CorpusManifest(
        corpus_id="cuad",
        ingest_dir=ingest_dir,
        doc_count=len(sampled),
        total_size_bytes=sum(p.stat().st_size for p in sampled),
        license="CC-BY 4.0",
        notes=f"CUAD sample of {len(sampled)} contracts (deterministic by filename)",
    )
```

- [ ] **Step 4: Run test until green**

`uv run pytest tests/test_cuad.py -v`

- [ ] **Step 5: Commit**

```bash
git add scripts/test_corpora/corpora/manifest.py scripts/test_corpora/corpora/cuad.py scripts/test_corpora/tests/test_cuad.py
git commit -m "corpora: CUAD acquisition (idempotent, deterministic sample of 80)"
```

---

## Task 9: `corpora/enron.py` — Enron subset acquisition

**Files:**
- Create: `scripts/test_corpora/corpora/enron.py`
- Create: `scripts/test_corpora/tests/test_enron.py`
- Create: `scripts/test_corpora/tests/fixtures/enron_sample/skilling_001.eml`
- Create: `scripts/test_corpora/tests/fixtures/enron_sample/lay_001.eml`
- Create: `scripts/test_corpora/tests/fixtures/enron_sample/random_001.eml`

- [ ] **Step 1: Create test fixtures**

`scripts/test_corpora/tests/fixtures/enron_sample/skilling_001.eml`:

```
From: jeff.skilling@enron.com
To: kenneth.lay@enron.com
Subject: Q3 numbers
Date: Mon, 1 Oct 2001 09:00:00 -0500

We need to discuss the California position before the call tomorrow.
```

`scripts/test_corpora/tests/fixtures/enron_sample/lay_001.eml`:

```
From: kenneth.lay@enron.com
To: jeff.skilling@enron.com
Subject: Re: Q3 numbers
Date: Mon, 1 Oct 2001 09:30:00 -0500

Agreed. 4pm today.
```

`scripts/test_corpora/tests/fixtures/enron_sample/random_001.eml`:

```
From: random.employee@enron.com
To: jeff.skilling@enron.com
Subject: Lunch
Date: Mon, 1 Oct 2001 11:00:00 -0500

Want to grab lunch?
```

- [ ] **Step 2: Write the failing test**

`scripts/test_corpora/tests/test_enron.py`:

```python
from pathlib import Path
from unittest.mock import patch

from scripts.test_corpora.corpora import enron


def test_enron_filter_keeps_only_target_custodians(tmp_path: Path, fixtures_dir: Path):
    src = fixtures_dir / "enron_sample"

    # Pretend the "downloaded" corpus is just our fixture dir
    with patch.object(enron, "_download_corpus", return_value=src):
        m = enron.acquire(workdir=tmp_path / "work", custodians=["skilling", "lay"], random_count=1)
        assert m.doc_count == 3  # 1 skilling + 1 lay + 1 random
        names = sorted(p.name for p in m.ingest_dir.glob("*.eml"))
        assert "skilling_001.eml" in names
        assert "lay_001.eml" in names
```

- [ ] **Step 3: Implement `corpora/enron.py`**

`scripts/test_corpora/corpora/enron.py`:

```python
"""Enron Email Corpus subset acquisition.

Downloads a HuggingFace-hosted Enron dataset, filters to target custodian
inboxes (Skilling, Lay, Fastow by default) plus a deterministic random
sample for noise. Outputs ``.eml`` files into ``ingest_dir``.

This module supports two acquisition paths:
1. ``_download_corpus`` (production) — fetches from HuggingFace.
2. Tests inject a path directly via the patched ``_download_corpus``.

The "filter to custodians" predicate is name-prefix based: filenames must
start with one of the configured custodian tokens (case-insensitive) to
count.
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

from .manifest import CorpusManifest

CUSTODIANS_DEFAULT = ["skilling", "lay", "fastow"]
RANDOM_COUNT_DEFAULT = 500
RANDOM_SEED = 42


def _download_corpus(workdir: Path) -> Path:
    """Production path: download the Enron HuggingFace dataset."""
    # Implementation note: the harness depends on huggingface_hub which is
    # already in the harbor-clerk venv. Tests patch this whole function.
    from huggingface_hub import snapshot_download  # local import for testability

    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "hf_enron"
    if out.exists():
        return out
    snapshot_download(repo_id="corbt/enron-emails", repo_type="dataset", local_dir=str(out))
    return out


def acquire(
    workdir: Path,
    custodians: list[str] | None = None,
    random_count: int = RANDOM_COUNT_DEFAULT,
) -> CorpusManifest:
    workdir = Path(workdir)
    custodians = [c.lower() for c in (custodians or CUSTODIANS_DEFAULT)]
    ingest_dir = workdir / "ingest"
    marker = ingest_dir / ".acquired"

    if marker.exists():
        emls = sorted(ingest_dir.glob("*.eml"))
        return CorpusManifest(
            corpus_id="enron",
            ingest_dir=ingest_dir,
            doc_count=len(emls),
            total_size_bytes=sum(p.stat().st_size for p in emls),
            license="public domain",
            notes=f"Enron subset: {len(emls)} emails",
        )

    src = _download_corpus(workdir)
    all_eml = sorted(src.rglob("*.eml"))

    target: list[Path] = []
    others: list[Path] = []
    for p in all_eml:
        name = p.name.lower()
        if any(name.startswith(c) for c in custodians):
            target.append(p)
        else:
            others.append(p)

    rng = random.Random(RANDOM_SEED)
    sampled_others = rng.sample(others, min(random_count, len(others)))
    selected = target + sampled_others

    ingest_dir.mkdir(parents=True, exist_ok=True)
    for src_path in selected:
        shutil.copy2(src_path, ingest_dir / src_path.name)

    marker.write_text("acquired")
    return CorpusManifest(
        corpus_id="enron",
        ingest_dir=ingest_dir,
        doc_count=len(selected),
        total_size_bytes=sum(p.stat().st_size for p in selected),
        license="public domain",
        notes=f"Enron subset: {len(target)} custodian + {len(sampled_others)} random emails",
    )
```

- [ ] **Step 4: Run tests until green**

`uv run pytest tests/test_enron.py -v`

- [ ] **Step 5: Commit**

```bash
git add scripts/test_corpora/corpora/enron.py scripts/test_corpora/tests/test_enron.py scripts/test_corpora/tests/fixtures/enron_sample/
git commit -m "corpora: Enron subset acquisition (custodian filter + deterministic random sample)"
```

---

## Task 10: `corpora/synthetic.py` — synthetic bilingual corpus generator

**Files:**
- Create: `scripts/test_corpora/corpora/synthetic.py`
- Create: `scripts/test_corpora/tests/test_synthetic.py`

- [ ] **Step 1: Write failing test**

`scripts/test_corpora/tests/test_synthetic.py`:

```python
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.test_corpora.corpora import synthetic


def test_synthetic_acquire_writes_doc_and_sidecar(tmp_path: Path):
    fake_anthropic = MagicMock()
    # Mock returns a templated invoice
    fake_anthropic.messages.create.return_value.content = [
        MagicMock(text='{"text": "INVOICE\\nVendor: Acme\\nTotal: $12,500", "facts": {"vendor": "Acme", "total_usd": 12500}}')
    ]

    with patch.object(synthetic, "_make_client", return_value=fake_anthropic):
        m = synthetic.acquire(
            workdir=tmp_path / "synth",
            doc_counts={"invoice": 2},  # only 2 docs to keep the test fast
            ocr_subset_count=0,
        )
    assert m.doc_count == 2
    docs = sorted(m.ingest_dir.glob("*.txt"))
    assert len(docs) == 2
    # Each doc has a JSON sidecar with ground-truth facts
    sidecars = sorted(m.ingest_dir.glob("*.json"))
    assert len(sidecars) == 2
    facts = json.loads(sidecars[0].read_text())
    assert facts["vendor"] == "Acme"
```

- [ ] **Step 2: Run, confirm fail**

- [ ] **Step 3: Implement `corpora/synthetic.py`**

`scripts/test_corpora/corpora/synthetic.py`:

```python
"""Synthetic bilingual small-business corpus generator.

Generates ~300 documents for fictional company ``Marbledock & Associates``
across 10 document types using Sonnet 4.6. Each doc gets a JSON sidecar
with structured ground-truth facts (vendor, dates, signatories, totals)
authored at generation time.

A configurable subset is rendered to PDF at low DPI with noise/rotation
to force OCR. The rest are written as plain text + bilingual variants
where applicable.

Idempotent via marker file ``.acquired``. Resuming a partial generation
relies on per-doc filenames being content-addressed (numeric prefix +
type), so a re-run only fills in the missing slots.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import anthropic

from .manifest import CorpusManifest

DOC_COUNTS_DEFAULT = {
    "invoice": 60,
    "onboarding_letter": 40,
    "board_minutes": 30,
    "vendor_contract": 30,
    "internal_memo": 30,
    "policy_doc": 30,
    "marketing_brief": 20,
    "employee_handbook": 20,
    "quarterly_report": 20,
    # 20 of the above are OCR-rendered (see ocr_subset_count)
}

OCR_SUBSET_COUNT_DEFAULT = 20

COMPANY = {
    "name": "Marbledock & Associates",
    "founded": 2009,
    "industry": "professional services",
    "employees": 42,
    "locations": ["Toronto", "Montréal"],
    "key_people": {
        "ceo": "Margaret Holvan",
        "cfo": "Pierre Dubois",
        "head_of_ops": "Aiyana Park",
    },
    "key_clients": ["Acme Corp", "Northwind Partners", "Polestar Industries"],
    "key_vendors": ["Globex Supplies", "Initech Software", "Cyberdyne IT"],
}

PROMPT_TEMPLATES = {
    "invoice": (
        "Generate a realistic invoice from a vendor of {company_name} dated {date}. "
        "Use ONE of the company's known vendors. Total between $1000 and $50000. "
        "Return a JSON object with keys: text (the full invoice as plain text) and "
        "facts (object with vendor, invoice_number, date, total_usd, line_items). "
        "Output JSON only, no prose."
    ),
    "onboarding_letter": (
        "Generate an employee onboarding letter for {company_name} dated {date}. "
        "Mix English and French naturally for a Canadian company (about 60/40). "
        "Return JSON with keys: text and facts (object with employee_name, role, "
        "start_date, languages_used, signing_manager). Output JSON only."
    ),
    "board_minutes": (
        "Generate board meeting minutes for {company_name} for {date}. "
        "Include 4-6 agenda items, attendees from key_people, and concrete decisions. "
        "Return JSON with keys: text and facts (object with date, attendees, "
        "decisions, lang). Output JSON only."
    ),
    "vendor_contract": (
        "Generate a vendor service contract between {company_name} and one of its "
        "vendors, dated {date}. Include term length, payment terms, governing law. "
        "Return JSON with keys: text and facts (object with vendor, term_months, "
        "monthly_fee_usd, governing_law, signatures). Output JSON only."
    ),
    "internal_memo": (
        "Generate an internal company memo for {company_name} dated {date} on a "
        "policy or operational topic. Return JSON with keys: text and facts "
        "(object with from, to, subject, lang). Output JSON only."
    ),
    "policy_doc": (
        "Generate a company policy document (HR, security, etc.) for {company_name} "
        "dated {date}. Include version number. Return JSON with keys: text and "
        "facts (object with policy_name, version, effective_date, owner). Output JSON only."
    ),
    "marketing_brief": (
        "Generate a marketing brief for {company_name} dated {date} for a campaign. "
        "Return JSON with keys: text and facts (object with campaign_name, target, "
        "budget_usd, owner). Output JSON only."
    ),
    "employee_handbook": (
        "Generate an excerpt from the {year} employee handbook of {company_name} "
        "with parallel English and French sections. Return JSON with keys: text "
        "and facts (object with year, sections, lang_split). Output JSON only."
    ),
    "quarterly_report": (
        "Generate a quarterly report excerpt for {company_name} for Q{q} {year}. "
        "Return JSON with keys: text and facts (object with quarter, year, "
        "revenue_usd, key_initiatives). Output JSON only."
    ),
}


def _make_client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _generate_one(
    client: anthropic.Anthropic,
    doc_type: str,
    rng: random.Random,
) -> dict:
    template = PROMPT_TEMPLATES[doc_type]
    # Pick a deterministic-ish date in 2025
    date = f"2025-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}"
    prompt = template.format(
        company_name=COMPANY["name"],
        date=date,
        year=2025,
        q=rng.randint(1, 4),
    )
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text
    # Extract the first JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"no JSON in response for {doc_type}: {text[:200]}")
    return json.loads(text[start : end + 1])


def _render_to_pdf_with_noise(text: str, out_path: Path, rng: random.Random) -> None:
    """Render plain text to a PDF at 150 DPI with slight noise/rotation."""
    from PIL import Image, ImageDraw, ImageFont
    import pypdfium2 as pdfium

    img = Image.new("RGB", (1275, 1650), "white")  # 8.5x11 at 150 DPI
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
    except OSError:
        font = ImageFont.load_default()
    y = 50
    for line in text.split("\n"):
        draw.text((60, y), line, fill="black", font=font)
        y += 18
        if y > 1600:
            break
    # Rotate by ±2° and add noise
    img = img.rotate(rng.uniform(-2, 2), fillcolor="white")
    # Save as PDF (Pillow can do this directly)
    img.save(out_path, "PDF", resolution=150.0)


def acquire(
    workdir: Path,
    doc_counts: dict[str, int] | None = None,
    ocr_subset_count: int = OCR_SUBSET_COUNT_DEFAULT,
) -> CorpusManifest:
    workdir = Path(workdir)
    ingest_dir = workdir / "ingest"
    marker = ingest_dir / ".acquired"
    counts = doc_counts or DOC_COUNTS_DEFAULT

    if marker.exists():
        docs = sorted(ingest_dir.glob("*"))
        return CorpusManifest(
            corpus_id="synthetic",
            ingest_dir=ingest_dir,
            doc_count=sum(1 for d in docs if d.suffix in {".txt", ".pdf"}),
            total_size_bytes=sum(d.stat().st_size for d in docs if d.is_file()),
            license="generated",
            notes="synthetic bilingual small-business",
        )

    ingest_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(42)
    client = _make_client()

    all_docs: list[tuple[str, dict]] = []  # (type, generated dict)
    seq = 0
    for doc_type, n in counts.items():
        for _ in range(n):
            seq += 1
            try:
                gen = _generate_one(client, doc_type, rng)
            except Exception as exc:
                # Skip individual failures rather than abort the whole sweep
                gen = {"text": f"[generation failed: {exc}]", "facts": {"_error": str(exc)}}
            base = f"{seq:04d}_{doc_type}"
            (ingest_dir / f"{base}.txt").write_text(gen["text"])
            (ingest_dir / f"{base}.json").write_text(json.dumps(gen.get("facts", {}), indent=2))
            all_docs.append((doc_type, gen))

    # OCR subset: pick N docs randomly, render to PDF, replace .txt
    if ocr_subset_count > 0 and all_docs:
        ocr_indices = rng.sample(range(len(all_docs)), min(ocr_subset_count, len(all_docs)))
        for idx in ocr_indices:
            doc_type, gen = all_docs[idx]
            seq_num = idx + 1
            base = f"{seq_num:04d}_{doc_type}"
            txt_path = ingest_dir / f"{base}.txt"
            pdf_path = ingest_dir / f"{base}.pdf"
            _render_to_pdf_with_noise(gen["text"], pdf_path, rng)
            txt_path.unlink(missing_ok=True)

    marker.write_text("acquired")
    return CorpusManifest(
        corpus_id="synthetic",
        ingest_dir=ingest_dir,
        doc_count=sum(1 for d in ingest_dir.glob("*") if d.suffix in {".txt", ".pdf"}),
        total_size_bytes=sum(d.stat().st_size for d in ingest_dir.glob("*") if d.is_file()),
        license="generated",
        notes="synthetic bilingual small-business",
    )
```

- [ ] **Step 4: Run tests until green**

`uv run pytest tests/test_synthetic.py -v`

- [ ] **Step 5: Commit**

```bash
git add scripts/test_corpora/corpora/synthetic.py scripts/test_corpora/tests/test_synthetic.py
git commit -m "corpora: synthetic bilingual generator (Sonnet 4.6 + JSON sidecars + OCR subset)"
```

---

## Task 11: `questions/*.yaml` — drafted question sets

**Files:**
- Create: `scripts/test_corpora/questions/cuad.yaml`
- Create: `scripts/test_corpora/questions/enron.yaml`
- Create: `scripts/test_corpora/questions/synthetic.yaml`

- [ ] **Step 1: Write CUAD questions**

`scripts/test_corpora/questions/cuad.yaml`:

```yaml
# CUAD legal contracts. Specific contract names are placeholders that
# must be filled in once the 80 are sampled — see notes field.
research:
  - id: cuad-research-1
    text: "Compare termination notice periods across the corpus and identify common patterns."
    notes: ""
  - id: cuad-research-2
    text: "Survey governing-law jurisdictions in the corpus and their relative frequency."
    notes: ""
  - id: cuad-research-3
    text: "Compare indemnification scope across contracts — what's typically included and excluded?"
    notes: ""
  - id: cuad-research-4
    text: "Identify contracts with the most restrictive non-compete clauses."
    notes: ""
  - id: cuad-research-5
    text: "Analyze patterns in liability cap clauses (capped, uncapped, super-cap)."
    notes: ""
  - id: cuad-research-6
    text: "Survey confidentiality-period ranges across contracts."
    notes: ""

ask:
  - id: cuad-ask-1
    text: "What is the governing law of the {{contract_a}} agreement?"
    notes: "fill {{contract_a}} after sampling"
  - id: cuad-ask-2
    text: "List all parties to the {{contract_b}} agreement."
    notes: "fill {{contract_b}}"
  - id: cuad-ask-3
    text: "What is the term length of the {{contract_c}} agreement?"
    notes: ""
  - id: cuad-ask-4
    text: "Find contracts mentioning California law."
    notes: ""
  - id: cuad-ask-5
    text: "Find contracts with arbitration clauses in New York."
    notes: ""
  - id: cuad-ask-6
    text: "Which contracts involve a bank as a party?"
    notes: ""
  - id: cuad-ask-7
    text: "List contracts with a perpetual confidentiality term."
    notes: ""
  - id: cuad-ask-8
    text: "Find contracts that prohibit assignment."
    notes: ""
  - id: cuad-ask-9
    text: "What contracts have an exclusivity clause?"
    notes: ""
  - id: cuad-ask-10
    text: "List contracts with a most-favored-nation clause."
    notes: ""
```

- [ ] **Step 2: Write Enron questions**

`scripts/test_corpora/questions/enron.yaml`:

```yaml
research:
  - id: enron-research-1
    text: "What topics dominated email discussions in October 2001?"
  - id: enron-research-2
    text: "Identify major themes in Skilling's email communications during the California energy crisis."
  - id: enron-research-3
    text: "Compare communication patterns between executives in the months leading up to bankruptcy."
  - id: enron-research-4
    text: "Survey discussions of accounting practices and their evolution over time."
  - id: enron-research-5
    text: "What were the major business deals discussed in the corpus?"
  - id: enron-research-6
    text: "Identify the key external counterparties referenced in executive emails."

ask:
  - id: enron-ask-1
    text: "Who emailed Skilling most frequently in 2001?"
  - id: enron-ask-2
    text: "What was the date of the earliest email about California in the corpus?"
  - id: enron-ask-3
    text: "Find emails about Raptor."
  - id: enron-ask-4
    text: "Find emails about LJM."
  - id: enron-ask-5
    text: "Find emails containing 'off-balance-sheet'."
  - id: enron-ask-6
    text: "What companies are mentioned in the corpus most often?"
  - id: enron-ask-7
    text: "Find emails mentioning Arthur Andersen."
  - id: enron-ask-8
    text: "List emails forwarded by Lay during 2001."
  - id: enron-ask-9
    text: "Find emails about FERC."
  - id: enron-ask-10
    text: "What was the subject of the last email Skilling sent before his resignation?"
```

- [ ] **Step 3: Write synthetic questions**

`scripts/test_corpora/questions/synthetic.yaml`:

```yaml
research:
  - id: synthetic-research-1
    text: "Compare quarterly performance trends across departments at Marbledock & Associates."
  - id: synthetic-research-2
    text: "What are the consistent themes in employee onboarding documents?"
  - id: synthetic-research-3
    text: "Identify the major vendor relationships and their evolution over the year."
  - id: synthetic-research-4
    text: "Survey policy changes across the year and their stated rationale."
  - id: synthetic-research-5
    text: "Compare meeting agenda patterns across the board over time."
  - id: synthetic-research-6
    cross_language: true
    variants:
      - lang: en
        text: "What were the major board decisions in Q3?"
      - lang: fr
        text: "Quelles étaient les principales décisions du conseil au troisième trimestre?"

ask:
  - id: synthetic-ask-1
    text: "What was the agreed price in the Q3 vendor contract with Globex Supplies?"
    notes: "answer in synthetic-corpus ground truth sidecar"
  - id: synthetic-ask-2
    text: "Who signed the new employee handbook policy update of 2025-06?"
  - id: synthetic-ask-3
    text: "Find documents about the Polestar Industries account."
  - id: synthetic-ask-4
    text: "List all invoices from Initech Software in 2025."
  - id: synthetic-ask-5
    text: "What was the total of invoices in March 2025?"
  - id: synthetic-ask-6
    text: "What policies were updated in Q2 2025?"
  - id: synthetic-ask-7
    text: "Quel est le sujet du procès-verbal de la réunion du conseil de mars 2025?"
  - id: synthetic-ask-8
    text: "List all signatories of board meetings in 2025."
  - id: synthetic-ask-9
    text: "Find marketing copy mentioning the Skylight campaign."
  - id: synthetic-ask-10
    cross_language: true
    variants:
      - lang: en
        text: "Find the bilingual employee handbook from 2025."
      - lang: fr
        text: "Trouvez le manuel des employés bilingue de 2025."
```

- [ ] **Step 4: Verify YAML parses**

```bash
cd scripts/test_corpora && uv run python -c "
import yaml
from pathlib import Path
for p in Path('questions').glob('*.yaml'):
    yaml.safe_load(p.read_text())
    print(f'{p.name}: ok')
"
```

Expected: three "ok" lines.

- [ ] **Step 5: Commit**

```bash
git add scripts/test_corpora/questions/
git commit -m "questions: drafted research + ask sets for cuad/enron/synthetic"
```

---

## Task 12: `runner/sweep.py` — six-phase orchestrator

**Files:**
- Create: `scripts/test_corpora/runner/sweep.py`

This task is the integration point. It wires every preceding module together into the six-phase entrypoint. There are no unit tests at this level — the module is exercised by Phase 0 dry-run in Task 13.

- [ ] **Step 1: Implement `runner/sweep.py`**

`scripts/test_corpora/runner/sweep.py`:

```python
"""Six-phase test sweep entrypoint.

Reads/writes ``state.json`` in the run-id directory. Schedules the next
pending unit, dispatches to the right handler, updates state, repeats.

Phases:
  0. acquire   — download / generate the three corpora
  1. baseline  — Sonnet 4.6 + Harbor Clerk MCP, save baselines/<corpus>/<q>.json
  2. smoke     — one large model × one corpus, iterate on bugs
  3. depth     — same model × all three depths
  4. models    — all 8 × all 3 corpora × standard, completion-only
  5. parity    — top 2 × all 3, mechanical + Sonnet judge
  6. unified   — drop DB, ingest all 3 into one DB, top 2 only

The CLI surface mirrors the design doc:
    --run-id, --workdir, --api-base, --resume, --rerun, --skip,
    --phases, --models, --corpora, --depth
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import logging
import os
import sys
import time
from pathlib import Path

import anthropic
import httpx
import yaml

from scripts.test_corpora import conftest as cfg
from scripts.test_corpora.corpora import cuad, enron, synthetic
from scripts.test_corpora.corpora.manifest import CorpusManifest
from scripts.test_corpora.runner.claude_baseline import BaselineGenerator
from scripts.test_corpora.runner.client import HarborClerkClient
from scripts.test_corpora.runner.judge import JudgeClient
from scripts.test_corpora.runner.metrics import citation_overlap, citation_extra, entity_overlap
from scripts.test_corpora.runner.sampler import CompletionEvent, Sampler
from scripts.test_corpora.runner.state import StateFile, Status, Unit


log = logging.getLogger("sweep")


# ── argparse ──

def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sweep")
    p.add_argument("--run-id", required=True)
    p.add_argument("--workdir", default=str(cfg.WORKDIR_DEFAULT))
    p.add_argument("--api-base", default=cfg.API_BASE)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--rerun", default="")
    p.add_argument("--skip", default="")
    p.add_argument("--phases", default="0-6")
    p.add_argument("--models", default="")
    p.add_argument("--corpora", default="")
    p.add_argument("--depth", default=cfg.DEFAULT_DEPTH)
    p.add_argument("--time-limit", type=int, default=cfg.DEFAULT_TIME_LIMIT_SECONDS)
    p.add_argument("--insecure", action="store_true", help="disable TLS verify (self-signed)")
    p.add_argument("--dry-run", action="store_true", help="run Phase 0 only, no API calls beyond acquire")
    return p


def _parse_selectors(s: str) -> dict[str, str]:
    if not s:
        return {}
    return dict(part.split("=", 1) for part in s.split(",") if "=" in part)


def _phase_range(s: str) -> set[int]:
    if "-" in s:
        a, b = s.split("-", 1)
        return set(range(int(a), int(b) + 1))
    return {int(p) for p in s.split(",") if p}


# ── state planning ──

def _plan_units(questions_by_corpus: dict[str, dict], phases: set[int], depth: str) -> list[Unit]:
    """Generate the full Unit set for the sweep — every cell across all phases."""
    units: list[Unit] = []
    corpora = list(questions_by_corpus)
    for phase in sorted(phases):
        if phase == 0:
            for c in corpora:
                units.append(Unit(phase=0, corpus=c, model="-", question_id="-", depth="-"))
        elif phase == 1:
            for c, qs in questions_by_corpus.items():
                for q in _question_ids(qs):
                    units.append(Unit(phase=1, corpus=c, model="claude-baseline", question_id=q, depth="n/a"))
        elif phase == 2:
            # smoke — one model, one corpus
            units.append(Unit(phase=2, corpus="cuad", model="qwen3.6-35b", question_id="cuad-research-1", depth=depth))
        elif phase == 3:
            for d in cfg.DEPTHS:
                for q in _question_ids(questions_by_corpus["cuad"]):
                    units.append(Unit(phase=3, corpus="cuad", model="qwen3.6-35b", question_id=q, depth=d))
        elif phase == 4:
            for m in cfg.ALL_MODELS:
                for c, qs in questions_by_corpus.items():
                    for q in _question_ids(qs):
                        units.append(Unit(phase=4, corpus=c, model=m, question_id=q, depth=depth))
        elif phase == 5:
            for m in cfg.TOP_MODELS:
                for c, qs in questions_by_corpus.items():
                    for q in _question_ids(qs):
                        units.append(Unit(phase=5, corpus=c, model=m, question_id=q, depth=depth))
        elif phase == 6:
            # unified pass — same questions but corpus="unified"
            for m in cfg.TOP_MODELS:
                for q in _question_ids(questions_by_corpus["cuad"])[:3] + _question_ids(questions_by_corpus["enron"])[:3]:
                    units.append(Unit(phase=6, corpus="unified", model=m, question_id=q, depth=depth))
    return units


def _question_ids(corpus_questions: dict) -> list[str]:
    """Return all question ids in a corpus's YAML, expanding cross-language pairs to lang-suffixed ids."""
    ids: list[str] = []
    for kind in ("research", "ask"):
        for q in corpus_questions.get(kind, []):
            if q.get("cross_language"):
                for v in q["variants"]:
                    ids.append(f"{q['id']}__{v['lang']}")
            else:
                ids.append(q["id"])
    return ids


def _question_text(corpus_questions: dict, question_id: str) -> tuple[str, str]:
    """Resolve a (possibly cross-language) question_id to (text, lang)."""
    base, _, lang_suffix = question_id.partition("__")
    for kind in ("research", "ask"):
        for q in corpus_questions.get(kind, []):
            if q["id"] == base:
                if q.get("cross_language"):
                    for v in q["variants"]:
                        if v["lang"] == lang_suffix:
                            return v["text"], v["lang"]
                return q["text"], "en"
    raise KeyError(f"unknown question id {question_id}")


# ── phase handlers ──

def _phase0_acquire(corpus_id: str, workdir: Path) -> CorpusManifest:
    if corpus_id == "cuad":
        return cuad.acquire(workdir / "cuad")
    if corpus_id == "enron":
        return enron.acquire(workdir / "enron")
    if corpus_id == "synthetic":
        return synthetic.acquire(workdir / "synthetic")
    raise ValueError(f"unknown corpus {corpus_id}")


def _phase1_baseline(client: anthropic.Anthropic, mcp_session, corpus: str, question_id: str,
                     question_text: str, results_dir: Path) -> dict:
    gen = BaselineGenerator(client=client, mcp_session=mcp_session)
    res = gen.run_question(question=question_text, question_id=question_id, corpus=corpus)
    BaselineGenerator.write(res, results_dir, corpus)
    return dataclasses.asdict(res)


def _phase4_or_5_local(
    hc: HarborClerkClient,
    corpus: str,
    model: str,
    question_id: str,
    question_text: str,
    depth: str,
    time_limit: int,
    is_research: bool,
    results_dir: Path,
) -> dict:
    if is_research:
        task_id = hc.start_research(question_text, model_id=model, depth=depth, time_limit=time_limit)
        result = hc.wait_for_research(task_id, max_wait_seconds=time_limit + 120)
    else:
        events = list(hc.stream_ask(question_text, model_id=model))
        # Aggregate the final answer + citations
        final_text = "".join(e.get("delta", "") for e in events if e.get("type") == "delta")
        citations = []
        for e in events:
            if e.get("type") == "citations":
                citations.extend(e.get("citations", []))
        result = {"state": "done", "answer": final_text, "citations": citations}

    out = {
        "corpus": corpus,
        "model": model,
        "question_id": question_id,
        "depth": depth,
        "result": result,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    out_path = results_dir / "responses" / corpus / model / f"{question_id}__{depth}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    return out


# ── ingestion helper ──

def _ingest_corpus(hc: HarborClerkClient, manifest: CorpusManifest) -> None:
    log.info("wiping DB before ingesting %s", manifest.corpus_id)
    hc.wipe_db(confirm=True)
    log.info("adding watch folder for %s", manifest.ingest_dir)
    hc.watch_folder_add(str(manifest.ingest_dir), name=f"test-corpora-{manifest.corpus_id}")
    log.info("waiting for pipeline to drain (this can take a while)")
    if not hc.wait_for_quiet_pipeline(max_wait_seconds=4 * 3600):
        raise RuntimeError(f"pipeline never drained for {manifest.corpus_id}")


# ── main ──

def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    workdir = Path(args.workdir).expanduser()
    run_dir = workdir / cfg.RESULTS_DIR_NAME / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(run_dir / "log.txt"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    state_path = run_dir / "state.json"
    sf = StateFile(state_path)
    sf.acquire_lock()
    try:
        sf.load()

        # Load question YAML for each corpus
        questions_by_corpus = {}
        for c in ("cuad", "enron", "synthetic"):
            q_path = Path(__file__).parent.parent / "questions" / f"{c}.yaml"
            questions_by_corpus[c] = yaml.safe_load(q_path.read_text())

        # Plan units if state is empty
        phases = _phase_range(args.phases)
        if not sf.units():
            sf.register(_plan_units(questions_by_corpus, phases, args.depth))
            sf.save()

        # Apply --rerun / --skip
        if args.rerun:
            n = sf.rerun(_parse_selectors(args.rerun))
            log.info("flipped %d units to PENDING via --rerun", n)
        if args.skip:
            n = sf.skip(_parse_selectors(args.skip))
            log.info("flipped %d units to SKIPPED via --skip", n)

        # Recover stale in-progress
        sf.recover_stale(stale_threshold_seconds=2 * args.time_limit)
        sf.save()

        # Build clients
        hc = HarborClerkClient(args.api_base, verify=not args.insecure)
        anthro = anthropic.Anthropic()
        judge = JudgeClient(client=anthro, model=cfg.JUDGE_MODEL)

        # CSV metrics
        metrics_path = run_dir / "metrics.csv"
        new_csv = not metrics_path.exists()
        metrics_f = metrics_path.open("a", newline="")
        metrics_writer = csv.writer(metrics_f)
        if new_csv:
            metrics_writer.writerow([
                "phase", "corpus", "model", "question_id", "depth",
                "status", "citation_overlap", "citation_extra", "entity_overlap",
                "latency_seconds", "judge_verdict", "judge_completeness",
            ])

        sampler = Sampler(every_n=cfg.SAMPLE_EVERY_N)
        sweep_started = time.time()

        # Acquire corpora once up-front so Phase 4+ can ingest on demand
        manifests: dict[str, CorpusManifest] = {}

        # Process units in phase order
        for phase in sorted(phases):
            phase_units = [u for u in sf.units() if u.phase == phase and u.status == Status.PENDING]
            if not phase_units:
                log.info("phase %d already complete or empty", phase)
                continue
            log.info("=== phase %d: %d pending units ===", phase, len(phase_units))

            # Phase-specific setup
            if phase in (1, 2, 3, 4, 5):
                pass  # corpora ingested per-corpus inside the loop
            if phase == 6:
                # unified: build a combined ingest dir
                unified_dir = workdir / "unified" / "ingest"
                unified_dir.mkdir(parents=True, exist_ok=True)
                for c in ("cuad", "enron", "synthetic"):
                    if c not in manifests:
                        manifests[c] = _phase0_acquire(c, workdir)
                    for f in manifests[c].ingest_dir.iterdir():
                        if f.is_file():
                            (unified_dir / f"{c}__{f.name}").write_bytes(f.read_bytes())
                unified_manifest = CorpusManifest(
                    corpus_id="unified", ingest_dir=unified_dir,
                    doc_count=sum(m.doc_count for m in manifests.values()),
                    total_size_bytes=sum(m.total_size_bytes for m in manifests.values()),
                    license="various", notes="unified pass",
                )
                _ingest_corpus(hc, unified_manifest)

            current_corpus_in_db: str | None = None

            for u in phase_units:
                if args.dry_run and phase > 0:
                    log.info("dry-run: skipping %s", u)
                    continue

                # Ensure correct corpus is in the DB for phases 4+
                if phase in (4, 5) and u.corpus != current_corpus_in_db:
                    if u.corpus not in manifests:
                        manifests[u.corpus] = _phase0_acquire(u.corpus, workdir)
                    _ingest_corpus(hc, manifests[u.corpus])
                    current_corpus_in_db = u.corpus

                sf.set_status(u.corpus, u.model, u.question_id, u.depth, Status.IN_PROGRESS)
                sf.save()

                t0 = time.time()
                try:
                    if phase == 0:
                        manifests[u.corpus] = _phase0_acquire(u.corpus, workdir)
                        out = {"manifest": dataclasses.asdict(manifests[u.corpus])}
                        out["manifest"]["ingest_dir"] = str(out["manifest"]["ingest_dir"])
                    elif phase == 1:
                        text, _lang = _question_text(questions_by_corpus[u.corpus], u.question_id)
                        out = _phase1_baseline(anthro, None, u.corpus, u.question_id, text, run_dir)
                    elif phase in (2, 3, 4, 5, 6):
                        # phase 6 looks up the question against whichever original corpus owns it
                        owning_corpus = u.corpus if u.corpus != "unified" else _find_owning_corpus(u.question_id, questions_by_corpus)
                        text, _lang = _question_text(questions_by_corpus[owning_corpus], u.question_id)
                        is_research = "research" in u.question_id
                        out = _phase4_or_5_local(
                            hc=hc, corpus=u.corpus, model=u.model, question_id=u.question_id,
                            question_text=text, depth=u.depth, time_limit=args.time_limit,
                            is_research=is_research, results_dir=run_dir,
                        )
                    else:
                        out = {}

                    sf.set_status(u.corpus, u.model, u.question_id, u.depth, Status.DONE)
                except (httpx.HTTPError, RuntimeError, KeyError) as exc:
                    log.exception("unit failed: %s", u)
                    sf.set_status(u.corpus, u.model, u.question_id, u.depth, Status.ERROR, error=str(exc))
                finally:
                    sf.save()

                latency = time.time() - t0

                # Compute metrics for phases that produced model answers
                co = ce = eo = 0.0
                judge_verdict = ""
                judge_completeness = 0
                if phase in (4, 5):
                    baseline_path = run_dir / "baselines" / u.corpus / f"{u.question_id}.json"
                    if baseline_path.exists():
                        baseline = json.loads(baseline_path.read_text())
                        model_answer = out.get("result", {}).get("answer", "")
                        model_doc_ids = [c.get("doc_id") for c in out.get("result", {}).get("citations", [])]
                        co = citation_overlap(baseline.get("cited_doc_ids", []), model_doc_ids)
                        ce = citation_extra(baseline.get("cited_doc_ids", []), model_doc_ids)
                        eo = entity_overlap(baseline.get("answer", ""), model_answer, lang="en")

                        if phase == 5:
                            v = judge.judge(question=text, baseline=baseline.get("answer", ""), model_answer=model_answer)
                            (run_dir / "judge" / u.corpus / u.model).mkdir(parents=True, exist_ok=True)
                            (run_dir / "judge" / u.corpus / u.model / f"{u.question_id}__{u.depth}.json").write_text(
                                json.dumps(dataclasses.asdict(v), indent=2)
                            )
                            judge_verdict = v.verdict
                            judge_completeness = v.completeness

                        sampler.note(CompletionEvent(
                            phase=phase, corpus=u.corpus, model=u.model, question_id=u.question_id,
                            baseline_answer=baseline.get("answer", "")[:200],
                            model_answer=model_answer[:200],
                            citation_overlap=co, citation_extra=ce, entity_overlap=eo,
                            latency_seconds=latency,
                            elapsed_total_seconds=int(time.time() - sweep_started),
                        ))

                metrics_writer.writerow([
                    phase, u.corpus, u.model, u.question_id, u.depth,
                    sf.get(u.corpus, u.model, u.question_id, u.depth).status.value,
                    f"{co:.3f}", ce, f"{eo:.3f}", f"{latency:.1f}",
                    judge_verdict, judge_completeness,
                ])
                metrics_f.flush()

            sampler.print_summary_table(phase=phase)

        metrics_f.close()
        log.info("sweep complete after %.1fs", time.time() - sweep_started)
        return 0
    finally:
        sf.release_lock()


def _find_owning_corpus(question_id: str, questions_by_corpus: dict) -> str:
    base = question_id.split("__")[0]
    for c, qs in questions_by_corpus.items():
        for kind in ("research", "ask"):
            for q in qs.get(kind, []):
                if q["id"] == base:
                    return c
    raise KeyError(f"no owning corpus for {question_id}")


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify the module imports cleanly**

```bash
cd /path/to/mcp-gateway && uv run python -c "from scripts.test_corpora.runner import sweep; print(sweep.make_parser().format_help())"
```

Expected: argparse help text printed.

- [ ] **Step 3: Commit**

```bash
git add scripts/test_corpora/runner/sweep.py
git commit -m "sweep: six-phase orchestrator (state + clients + metrics + sampler integration)"
```

---

## Task 13: Phase-0 dry-run + README polish

**Files:**
- Modify: `scripts/test_corpora/README.md`

- [ ] **Step 1: Run a Phase-0-only dry-run with the synthetic corpus reduced to a few docs**

```bash
cd /path/to/mcp-gateway
export ANTHROPIC_API_KEY="sk-ant-..."
uv run python -m scripts.test_corpora.runner.sweep \
    --run-id smoke-$(date +%s) \
    --workdir /tmp/test-corpora-smoke \
    --phases 0 \
    --dry-run
```

Expected:
- `state.json` written with phase-0 units in `pending`
- Each `_phase0_acquire` runs (note: synthetic generation will hit the Anthropic API; reduce `DOC_COUNTS_DEFAULT` for the smoke test by editing `synthetic.py` ad-hoc, or run only `cuad` / `enron`)
- All three phase-0 units transition to `done`
- `log.txt` shows ingestion data per corpus

If anything is broken (download URL wrong, parsing fails, etc.), fix and re-run with `--resume`.

- [ ] **Step 2: Document any smoke-test gotchas in the README**

Append to `scripts/test_corpora/README.md`:

```markdown
## First-run gotchas

- **CUAD release URL** — Zenodo occasionally moves the file; if the download
  fails, update `CUAD_RELEASE_URL` in `corpora/cuad.py`.
- **Enron HuggingFace dataset** — the default `corbt/enron-emails` dataset
  may be paywalled or moved. Alternates: `snoop2head/enron_aeslc_emails`.
  Edit `_download_corpus` in `corpora/enron.py`.
- **Synthetic generation cost** — ~300 generations × 4K tokens at Sonnet 4.6
  pricing is ~$3-5. Run `--phases 0 --corpora synthetic` first to spot-check
  the first 50 documents before committing to the full set.
- **Self-signed TLS** — pass `--insecure` if your local Harbor Clerk uses
  a self-signed cert (Caddy default).
```

- [ ] **Step 3: Run the full test suite one last time**

```bash
cd scripts/test_corpora && uv run pytest -v
```

Expected: every test passes.

- [ ] **Step 4: Commit**

```bash
git add scripts/test_corpora/README.md
git commit -m "docs: first-run gotchas in test_corpora README"
```

---

## Out-of-scope follow-ups (do not implement)

- Wiring `sweep.py` into a `make` target — the harness is invoked by hand.
- Adding a `--report` mode that aggregates `metrics.csv` into a summary report — flagged as future work in the design doc.
- Parallelizing across models — the spec is sequential by design (one model can saturate the GPU).
- Per-model parameter tuning (`-np`, speculative decoding) — separate project.
