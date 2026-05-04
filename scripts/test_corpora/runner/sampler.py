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
