"""Screening and ranking, as pure functions over plain dicts.

The policy (`watchlist.yaml`) decides what is in range; this module applies it
and orders what is left. Nothing here touches the network, so every rule has
an offline test and a change to a rule is a reviewed diff.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

POLICY_FILE = Path(__file__).with_name("watchlist.yaml")

# Family stems, longest first so "gpt-oss" wins over "gpt" and "ministral" over "mistral".
_FAMILIES = [
    "gpt-oss", "ministral", "magistral", "mistral", "deepseek", "nemotron", "hunyuan", "granite", "smollm",
    "agents", "apertus", "command", "minimax", "hermes", "apriel", "trinity", "qwen", "gemma", "llama", "ernie",
    "kimi", "olmo", "ling", "ring", "step", "glm", "lfm", "phi", "aya",
]  # fmt: skip


@dataclass(frozen=True)
class Policy:
    orgs: list[str]
    gguf_publishers: list[str]
    quant: str
    ceiling_gb: float
    min_context: int
    permissive_licenses: list[str]
    exclude_name_fragments: list[str]
    flag_name_fragments: list[str]
    pipeline_tags: list[str]
    family_orgs: dict[str, str]


def load_policy(path: Path = POLICY_FILE) -> Policy:
    return Policy(**yaml.safe_load(path.read_text(encoding="utf-8")))


def family_of(name: str) -> str | None:
    low = name.lower().split("/")[-1]
    return next((f for f in _FAMILIES if f in low), None)


def generation_of(name: str) -> tuple[int, ...]:
    """The version that follows the family stem: Qwen3.8-27B -> (3, 8),
    gemma-4-12b -> (4,), Qwen3-8B -> (3,). Parameter counts are not versions:
    a number directly followed by b/m (8B, 270m) or preceded by 'a' (A3B) is
    skipped."""
    low = name.lower().split("/")[-1]
    family = family_of(name)
    if not family:
        return ()
    rest = low.split(family, 1)[1]
    m = re.match(r"[-_ ]?v?(\d+)(?:\.(\d+))?(?![\d.]*[bm](?:\b|[-_]))", rest)
    if not m:
        return ()
    return tuple(int(g) for g in m.groups() if g is not None)


def params_class(name: str) -> float | None:
    """Total parameters in billions as the repo name states them: Qwen3.5-9B ->
    9, Qwen3.6-35B-A3B -> 35 (the active-parameter count after "A" is not the
    size), gpt-oss-20b -> 20. None when the name states no size."""
    m = re.search(r"(?<![a-z\d.])(\d+(?:\.\d+)?)b(?![a-z])", name.lower().split("/")[-1])
    return float(m.group(1)) if m else None


def size_matched_successors(
    curated: dict[str, Any], org_rows: list[dict[str, Any]], policy: Policy
) -> list[dict[str, Any]]:
    """Newer generations of a curated model's family in the same size class
    (0.6x to 1.6x its parameters), from the vendor's whole catalogue rather
    than the survey window: a tier's replacement may be older than the window.
    Newest generation first, then closest in size."""
    size = params_class(curated["repo"])
    if size is None:
        return []
    found = []
    for row in org_rows:
        repo = row["id"]
        if family_of(repo) != curated["family"] or "gguf" in repo.lower() or excluded_fragment(repo, policy):
            continue
        if row.get("pipeline_tag") not in policy.pipeline_tags:
            continue
        gen, other = generation_of(repo), params_class(repo)
        if other is None or gen <= tuple(curated["generation"]) or not (0.6 * size <= other <= 1.6 * size):
            continue
        found.append({"repo": repo, "created": (row.get("createdAt") or "")[:10], "generation": gen, "params_b": other})
    return sorted(found, key=lambda f: (tuple(-g for g in f["generation"]), abs(f["params_b"] - size)))


def ladder_gap_fillers(
    curated: list[dict[str, Any]], org_rows: list[dict[str, Any]], family: str, policy: Policy, min_gap_gb: float = 4.0
) -> list[dict[str, Any]]:
    """Members of a curated family whose estimated Q4 size (0.6 GB per billion
    parameters) lands in a gap of the curated size ladder wider than
    `min_gap_gb`. These are neither new nor successors, so the window and the
    successor search both miss them: Gemma 4 12B sat in the 5 to 11.6 GB gap
    for months.

    A filler must be at least as new as the newest generation the registry
    carries for its family, or newer than the curated model it would sit
    above when that model is its own family. Anything older is a model the
    registry has already moved past."""
    ladder = sorted(curated, key=lambda c: c["size_bytes"])
    gaps = [
        (lo, hi)
        for lo, hi in zip(ladder, ladder[1:], strict=False)
        if (hi["size_bytes"] - lo["size_bytes"]) / 1e9 > min_gap_gb
    ]
    mine = [c for c in curated if c["family"] == family]
    if not gaps or not mine:
        return []
    newest = max(tuple(c["generation"]) for c in mine)
    carried = {c["params_b"] for c in mine if c.get("params_b") is not None}
    ids = {row["id"].lower() for row in org_rows}
    found = []
    for row in org_rows:
        repo = row["id"]
        if family_of(repo) != family or "gguf" in repo.lower() or excluded_fragment(repo, policy):
            continue
        if row.get("pipeline_tag") not in policy.pipeline_tags or f"{repo.lower()}-it" in ids:
            continue
        size, generation = params_class(repo), generation_of(repo)
        if size is None or size in carried:
            continue
        est = size * 0.6
        for lo, hi in gaps:
            if not lo["size_bytes"] / 1e9 + 1 < est < hi["size_bytes"] / 1e9 - 1:
                continue
            above_own_family = lo["family"] == family and generation > tuple(lo["generation"])
            if generation >= newest or above_own_family:
                found.append(
                    {
                        "repo": repo,
                        "created": (row.get("createdAt") or "")[:10],
                        "params_b": size,
                        "gap_gb": [round(lo["size_bytes"] / 1e9, 1), round(hi["size_bytes"] / 1e9, 1)],
                    }
                )
    return sorted(found, key=lambda f: (tuple(-g for g in generation_of(f["repo"])), f["repo"]))


def excluded_fragment(name: str, policy: Policy) -> str | None:
    low = name.lower()
    return next((f for f in policy.exclude_name_fragments if f in low), None)


def flags_for(name: str, policy: Policy) -> list[str]:
    low = name.lower()
    return [f for f in policy.flag_name_fragments if f in low]


def screen(
    model: dict[str, Any], gguf: dict[str, Any] | None, policy: Policy, *, arch_at_pin: set[str], arch_at_head: set[str]
) -> dict[str, Any]:
    """Why a release is or is not a candidate. Every reason is recorded, not
    just the first, so the report can say what would have to change."""
    reasons: list[str] = []
    notes: list[str] = []
    if model.get("pipeline_tag") not in policy.pipeline_tags:
        reasons.append(f"not a chat model on the Hub (pipeline: {model.get('pipeline_tag')})")
    license_id = (model.get("license") or "unknown").lower()
    if license_id not in policy.permissive_licenses:
        reasons.append(f"licence is {license_id}")
    if gguf is None:
        reasons.append("no GGUF from the vendor or a trusted publisher")
    else:
        size = gguf.get("quant_bytes")
        if not size:
            reasons.append(f"no {policy.quant} file in {gguf['repo']}")
        elif size > policy.ceiling_gb * 1e9:
            reasons.append(f"{policy.quant} is {size / 1e9:.1f} GB, over the {policy.ceiling_gb:g} GB ceiling")
        ctx = gguf.get("context_length") or 0
        if ctx < policy.min_context:
            reasons.append(f"context {ctx} is under {policy.min_context}")
        if not gguf.get("tool_calls_in_template"):
            reasons.append("chat template has no tool calling")
        arch = gguf.get("architecture")
        if arch and arch not in arch_at_head:
            reasons.append(f"llama.cpp cannot load architecture {arch!r}, even at head")
        elif arch and arch not in arch_at_pin:
            notes.append(f"architecture {arch!r} needs a llama.cpp upgrade past the pin")
    return {"verdict": "screened" if reasons else "candidate", "reasons": reasons, "notes": notes}


def _tier_gap(size_gb: float, curated_sizes_gb: list[float], min_gap_gb: float = 4.0) -> bool:
    """True when `size_gb` lands in a gap of the curated ladder wider than `min_gap_gb`."""
    ladder = sorted(curated_sizes_gb)
    return any(lo + 1 < size_gb < hi - 1 for lo, hi in zip(ladder, ladder[1:], strict=False) if hi - lo > min_gap_gb)


def score(candidate: dict[str, Any], curated: list[dict[str, Any]], today: date) -> dict[str, float]:
    """Score components, so the report can show why something ranks where it does.

    A newer generation of a family already curated dominates: the product has
    tuned prompts, slot counts and context for that family, so its successor is
    the cheapest large win. Popularity and recency order the rest."""
    model, gguf = candidate["model"], candidate["gguf"]
    family, generation = family_of(model["repo"]), generation_of(model["repo"])
    same_family = [c for c in curated if c["family"] == family and family]
    newest_curated = max((c["generation"] for c in same_family), default=())
    parts: dict[str, float] = {}
    if same_family and generation > newest_curated:
        parts["successor of a curated family"] = 40.0
    elif same_family:
        parts["same family as a curated model"] = 15.0
    parts["popularity"] = round(min(20.0, 5.0 * math.log10((model.get("likes") or 0) + 1)), 1)
    try:
        age_days = (today - date.fromisoformat(model["created"])).days
    except (KeyError, ValueError):
        age_days = 365
    parts["recency"] = round(max(0.0, 10.0 - age_days / 9.0), 1)
    size_gb = (gguf.get("quant_bytes") or 0) / 1e9
    if _tier_gap(size_gb, [c["size_bytes"] / 1e9 for c in curated]):
        parts["fills a gap in the size ladder"] = 10.0
    return parts


def rank(candidates: list[dict[str, Any]], curated: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    ranked = []
    for c in candidates:
        parts = score(c, curated, today)
        ranked.append({**c, "score": round(sum(parts.values()), 1), "score_parts": parts})
    return sorted(ranked, key=lambda c: (-c["score"], c["model"]["repo"]))
