"""Prompts for the research verifier loop.

Lives separately from research.py so prompt-tuning is decoupled from the
pipeline code. The verifier asks the model to judge whether a cited
source actually supports the claims the report attributes to it; the
revision prompt (added in stage 2) feeds those verdicts back to the
synthesizer for one corrective pass.

See `docs/superpowers/specs/2026-06-01-verifier-loop-design.md`.
"""

from __future__ import annotations

# Verifier system prompt — keep terse. The judge model only needs the verdict
# vocabulary and the JSON shape; everything else is in the user content.
VERIFIER_SYSTEM = (
    "You are a citation verifier. Given a report excerpt that cites a source, "
    "and the source's evidence, judge whether the evidence supports the "
    "claims attributed to it.\n"
    "\n"
    "Verdicts:\n"
    "- supported: the evidence directly states or clearly entails every claim "
    "the report makes about this source.\n"
    "- partial: the evidence relates to the claims but is missing at least one "
    "key element (a number, a date, a name, a qualifier).\n"
    "- unsupported: the evidence does not address the claims; the report has "
    "fabricated or substantially misattributed.\n"
    "\n"
    "Reply with ONLY a JSON object on a single line:\n"
    '{"verdict": "supported"|"partial"|"unsupported", "reason": "<one short sentence>"}'
)


def render_verifier_user(*, report_excerpt: str, doc_title: str, page: str | None, source_excerpt: str) -> str:
    """Build the user-content side of the verifier prompt for a single citation.

    `report_excerpt` is the portion of the synthesized report that references
    this source. `source_excerpt` is the corresponding evidence text from the
    research notes. Both are truncated upstream — this function does no
    additional length management beyond inlining them as-is.
    """
    page_label = f", p{page}" if page else ""
    return (
        f"## REPORT EXCERPT (cites {doc_title}{page_label})\n"
        f"{report_excerpt}\n"
        f"\n"
        f"## SOURCE EVIDENCE (from {doc_title}{page_label})\n"
        f"{source_excerpt}\n"
        f"\n"
        "Judge whether the source evidence supports what the report says about "
        "it. Reply with the JSON verdict object."
    )


# Revision system prompt — keep tight. The model has to revise an existing
# draft, NOT compose a new report. The "do not introduce new claims" rule is
# load-bearing: without it, the second pass becomes a self-amplifying redraft
# rather than a corrective edit.
REVISION_SYSTEM = (
    "You are revising a research report based on citation-by-citation "
    "verification feedback. The verifier checked each cited source against "
    "the claims in the previous draft and flagged where the evidence does "
    "not fully support the claim. Your job is to fix the mismatches.\n"
    "\n"
    "Rules:\n"
    "- Replace, soften, or remove flagged claims. Do NOT add new claims.\n"
    "- Do NOT introduce citations the verifier has not already seen.\n"
    "- Preserve correct (unflagged) claims and citations exactly as written.\n"
    "- Output the complete revised report — not a diff and not just the changes."
)


def render_revision_user(
    *,
    user_question: str,
    draft_report: str,
    notes: str,
    feedback_items: list[dict],
) -> str:
    """Build the revision user prompt.

    `feedback_items` is the subset of verifier verdicts with verdict in
    {'partial', 'unsupported'} — supported and skipped verdicts are dropped
    so the model focuses on what needs fixing.

    `notes` carries the underlying research evidence so the revision pass
    can replace problematic numbers/dates with sourced ones rather than
    simply softening every flagged claim.
    """
    lines = ["## ORIGINAL QUESTION", user_question, "", "## PREVIOUS DRAFT", draft_report, ""]
    if notes:
        lines.extend(["## RESEARCH NOTES (evidence for the citations)", notes, ""])
    lines.append("## CITATION VERIFICATION FEEDBACK")
    if feedback_items:
        for item in feedback_items:
            doc_title = item.get("doc_title") or ""
            page = item.get("page")
            page_label = f", p{page}" if page else ""
            verdict = item.get("verdict") or ""
            reason = (item.get("reason") or "").strip()
            lines.append(f"- [{doc_title}{page_label}] verdict: {verdict}")
            if reason:
                lines.append(f"  Reason: {reason}")
    else:
        # Defensive — the caller should only invoke when there is feedback,
        # but if it does happen the model still gets a sensible prompt.
        lines.append("(no flagged citations — revision should be a no-op)")
    lines.extend(
        [
            "",
            "Revise the draft. Output the complete revised report.",
        ]
    )
    return "\n".join(lines)
