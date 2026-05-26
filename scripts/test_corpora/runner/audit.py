"""Harness-audit pure functions.

Read-only analysis over already-loaded answer-eval captures + verdicts.
No I/O, no LLM calls — CLI orchestrator handles those. Each function
takes loaded dicts and returns a dict suitable for JSON-encoding.

Used by: scripts/test_corpora/audit_answer_eval.py.
"""

from __future__ import annotations


def tool_use_stats(captures: list[dict]) -> dict:
    """Aggregate tool-use distribution + per-tool counts across captures.

    Returns:
      {
        "total_captures": int,
        "tool_call_distribution": {0: N, 1: N, 2: N, ..., "4+": N},
        "tool_call_counts_per_tool": {"kb_search": N, ...},
        "captures_by_tool_count": {qid: {"tool_count": N, "tools_used": [...]}}
      }

    The "4+" bin keeps the structure stable across runs (don't enumerate
    every possible call count). Per-tool counts cover every kb_* tool
    name encountered in any transcript.
    """
    distribution: dict[int | str, int] = {}
    per_tool: dict[str, int] = {}
    by_qid: dict[str, dict] = {}

    for cap in captures:
        qid = cap.get("question_id", "")
        transcript = cap.get("tool_transcript") or []
        tools_used = [t.get("tool", "") for t in transcript if isinstance(t, dict)]
        count = len(tools_used)
        bucket: int | str = "4+" if count >= 4 else count
        distribution[bucket] = distribution.get(bucket, 0) + 1
        for name in tools_used:
            if name:
                per_tool[name] = per_tool.get(name, 0) + 1
        by_qid[qid] = {"tool_count": count, "tools_used": tools_used}

    return {
        "total_captures": len(captures),
        "tool_call_distribution": distribution,
        "tool_call_counts_per_tool": per_tool,
        "captures_by_tool_count": by_qid,
    }
