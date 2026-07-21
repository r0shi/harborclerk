"""Generate the ingestion-pipeline stage table from the worker configuration.

This is the block that would have prevented #537. The hand-written docs claimed
two queues and showed `summarize` gating `finalize`; the code has three queues
and treats `summarize` as a background stage. Both facts are derived here.
"""

from __future__ import annotations


def _load():
    from harbor_clerk.worker.entry import QUEUE_STAGES
    from harbor_clerk.worker.pipeline import (
        _BACKGROUND_STAGES,
        _PARALLEL_STAGES,
        _SEQUENTIAL_STAGES,
        STAGE_CONFIG,
    )

    return STAGE_CONFIG, QUEUE_STAGES, _SEQUENTIAL_STAGES, _PARALLEL_STAGES, _BACKGROUND_STAGES


def _role(stage, sequential, parallel, background) -> str:
    if stage in sequential:
        return f"sequential ({list(sequential).index(stage) + 1} of {len(sequential)})"
    if stage in parallel:
        return "parallel — gates `finalize`"
    if stage in background:
        return "background — does **not** gate `finalize`"
    return "fan-in"


def generate() -> str:
    stage_config, queue_stages, sequential, parallel, background = _load()

    lines = [
        f"**{len(stage_config)} stages across {len(queue_stages)} queues** "
        f"({', '.join(f'`{q}`' for q in sorted(queue_stages))}).",
        "",
        "| Stage | Queue | Timeout | Role |",
        "|---|---|---|---|",
    ]
    for stage, config in stage_config.items():
        queue, timeout = config[0], config[1]
        role = _role(stage, sequential, parallel, background)
        lines.append(f"| `{stage.value}` | `{queue}` | {timeout}s | {role} |")

    lines += ["", "Queue subscriptions:", ""]
    for queue in sorted(queue_stages):
        members = ", ".join(f"`{s.value}`" for s in queue_stages[queue])
        lines.append(f"- `{queue}` — {members}")
    return "\n".join(lines)
