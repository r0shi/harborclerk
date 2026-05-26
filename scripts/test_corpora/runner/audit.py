"""Harness-audit pure functions.

Read-only analysis over already-loaded answer-eval captures + verdicts.
No I/O, no LLM calls — CLI orchestrator handles those. Each function
takes loaded dicts and returns a dict suitable for JSON-encoding.

Used by: scripts/test_corpora/audit_answer_eval.py.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# UUID v4 shape: 8-4-4-4-12 hex chars. Matches what HC's tool responses use
# for doc_id. False positives possible (a query string echoed in result_summary
# could match), false negatives possible (a tool that doesn't include doc_id);
# documented as a heuristic in the spec and surfaced in the markdown report.
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


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


# Substrings that mark a question as "earliest/latest-style" by qid.
# Tuned to match the convention used by enron + synthetic ground-truth gen.
_EARLIEST_LATEST_TRIGGERS = ("earliest", "latest", "oldest", "newest", "first", "last")

# Substrings in the judge's rationale that suggest the model hit ambiguity.
# Lossy heuristic — false positives possible, intentional for v1.
_AMBIGUITY_TRIGGERS = ("ambiguous", "multiple", "several", "which")

# Score threshold for "low correctness". 0-5 scale; <=2 means clearly wrong.
_LOW_CORRECTNESS = 2

# Tool-call threshold for "low tool use".
_LOW_TOOL_COUNT = 1


def failure_correlation(captures: list[dict], verdicts: list[dict]) -> dict:
    """Join captures + verdicts by qid; surface actionable failure patterns.

    Each pattern key is always present in the output, with [] when no
    items match — callers can blindly index without KeyError.

    Returns:
      {
        "low_correctness_low_tool_use": [{qid, tools_used, correctness, title_fragment}, ...],
        "earliest_latest_questions_no_by_date_tool": [{qid, tools_used, correctness}, ...],
        "ambiguous_id_questions_no_verify_tool": [{qid, tools_used, correctness}, ...],
      }
    """
    cap_by_qid: dict = {}
    for c in captures:
        qid = c.get("question_id")
        if qid in cap_by_qid:
            log.warning("duplicate capture question_id %r — keeping last", qid)
        cap_by_qid[qid] = c

    ver_by_qid: dict = {}
    for v in verdicts:
        qid = v.get("id")
        if qid in ver_by_qid:
            log.warning("duplicate verdict id %r — keeping last", qid)
        ver_by_qid[qid] = v

    low_corr: list[dict] = []
    earliest_latest: list[dict] = []
    ambiguous: list[dict] = []

    for qid, cap in cap_by_qid.items():
        ver = ver_by_qid.get(qid)
        if ver is None:
            continue  # capture without verdict — silently skip
        transcript = cap.get("tool_transcript") or []
        tools_used = [t.get("tool", "") for t in transcript if isinstance(t, dict)]
        tool_count = len(tools_used)
        correctness = ver.get("correctness", 0)
        rationale_lc = (ver.get("rationale") or "").lower()

        if correctness <= _LOW_CORRECTNESS and tool_count <= _LOW_TOOL_COUNT:
            low_corr.append(
                {
                    "qid": qid,
                    "tools_used": tools_used,
                    "correctness": correctness,
                    "title_fragment": (cap.get("question") or "")[:80],
                }
            )

        qid_lc = qid.lower()
        if any(t in qid_lc for t in _EARLIEST_LATEST_TRIGGERS) and "kb_documents_by_date" not in tools_used:
            earliest_latest.append(
                {
                    "qid": qid,
                    "tools_used": tools_used,
                    "correctness": correctness,
                }
            )

        if (
            correctness <= _LOW_CORRECTNESS
            and any(t in rationale_lc for t in _AMBIGUITY_TRIGGERS)
            and "kb_verify_identifier" not in tools_used
        ):
            ambiguous.append(
                {
                    "qid": qid,
                    "tools_used": tools_used,
                    "correctness": correctness,
                }
            )

    return {
        "low_correctness_low_tool_use": low_corr,
        "earliest_latest_questions_no_by_date_tool": earliest_latest,
        "ambiguous_id_questions_no_verify_tool": ambiguous,
    }


def citation_hygiene(captures: list[dict]) -> dict:
    """For each capture, compare cited_doc_ids against doc_ids seen in any
    tool transcript result_summary. Each capture lands in exactly one bucket:

      - no_citations: capture has empty cited_doc_ids
      - fabricated_citations: at least one cited doc_id is NOT in any
        result_summary's UUID set
      - grounded (count): every cited doc_id is in the seen set
    """
    no_citations: list[dict] = []
    fabricated: list[dict] = []
    grounded = 0

    for cap in captures:
        cited = cap.get("cited_doc_ids") or []
        transcript = cap.get("tool_transcript") or []
        seen_uuids: set[str] = set()
        for call in transcript:
            if not isinstance(call, dict):
                continue
            summary = call.get("result_summary") or ""
            if isinstance(summary, str):
                seen_uuids.update(_UUID_RE.findall(summary))

        if not cited:
            no_citations.append(
                {
                    "qid": cap.get("question_id", ""),
                    "answer_preview": (cap.get("answer") or "")[:200],
                }
            )
            continue

        missing = [c for c in cited if c not in seen_uuids]
        if missing:
            fabricated.append(
                {
                    "qid": cap.get("question_id", ""),
                    "cited": cited,
                    "seen_in_transcript": sorted(seen_uuids),
                }
            )
        else:
            grounded += 1

    return {
        "no_citations": no_citations,
        "fabricated_citations": fabricated,
        "grounded_count": grounded,
        "total": len(captures),
    }
