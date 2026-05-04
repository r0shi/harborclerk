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

    def key(self) -> tuple[int, str, str, str, str]:
        return (self.phase, self.corpus, self.model, self.question_id, self.depth)

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
        self._units: dict[tuple[int, str, str, str, str], Unit] = {}
        self._lock_fd: int | None = None

    # ── load / save ──

    def load(self) -> None:
        if not self.path.exists():
            self._units = {}
            return
        data = json.loads(self.path.read_text())
        self._units = {
            (u["phase"], u["corpus"], u["model"], u["question_id"], u["depth"]): Unit.from_json(u)
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

    def get(self, phase: int, corpus: str, model: str, question_id: str, depth: str) -> Unit | None:
        return self._units.get((phase, corpus, model, question_id, depth))

    def set_status(
        self,
        phase: int,
        corpus: str,
        model: str,
        question_id: str,
        depth: str,
        status: Status,
        heartbeat: float | None = None,
        error: str | None = None,
    ) -> None:
        u = self._units[(phase, corpus, model, question_id, depth)]
        u.status = status
        if status == Status.IN_PROGRESS:
            u.started_at = u.started_at or time.time()
            u.heartbeat = heartbeat if heartbeat is not None else time.time()
        elif status in {Status.DONE, Status.DEGRADED, Status.ERROR, Status.FAILED, Status.SKIPPED, Status.BLOCKED}:
            u.finished_at = time.time()
            u.heartbeat = None
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
            if all(str(getattr(u, k, "")) == str(v) for k, v in selectors.items()):
                u.status = Status.PENDING
                u.heartbeat = None
                u.started_at = None
                u.error = None
                n += 1
        return n

    def skip(self, selectors: dict[str, str]) -> int:
        n = 0
        for u in self._units.values():
            if all(str(getattr(u, k, "")) == str(v) for k, v in selectors.items()):
                u.status = Status.SKIPPED
                n += 1
        return n

    # ── lock ──

    def acquire_lock(self) -> None:
        try:
            self._lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self._lock_fd, str(os.getpid()).encode())
        except FileExistsError:
            try:
                holder_pid = self.lock_path.read_text().strip()
            except OSError:
                holder_pid = "unknown"
            raise RuntimeError(
                f"state file is locked by another runner (pid {holder_pid}); "
                f"if no such process exists, delete {self.lock_path} and retry"
            )

    def release_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        self.lock_path.unlink(missing_ok=True)

    # ── context manager ──

    def __enter__(self) -> "StateFile":
        self.acquire_lock()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release_lock()
