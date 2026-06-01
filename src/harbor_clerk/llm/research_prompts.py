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
