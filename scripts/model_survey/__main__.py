"""Survey the Hub for local models worth curating, and audit the curated set.

    uv run python -m scripts.model_survey --out docs/reports/ [--since 2026-07-22] [--run-id NAME] [--json facts.json]

Gathers facts (new releases per watched vendor, trusted GGUF builds, the
llama.cpp pin against upstream, drift in the curated registry), applies the
policy in `watchlist.yaml`, and writes a dated report with the ranking and the
reason every screened release was screened. The "Reading" section at the top
is left for whoever runs the loop: the script states facts, a person or an
agent states what to do about them.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from scripts.model_survey import llamacpp
from scripts.model_survey.hf import Hub, summarize_gguf, summarize_model
from scripts.model_survey.screen import (
    Policy,
    excluded_fragment,
    family_of,
    flags_for,
    generation_of,
    load_policy,
    rank,
    screen,
    size_matched_successors,
)

PER_ORG = 10


def curated_models() -> list[dict[str, Any]]:
    from harbor_clerk.llm.models import MODELS

    return [
        {
            "id": m.id,
            "name": m.name,
            "repo": m.huggingface_repo,
            "filename": m.filename,
            "size_bytes": m.size_bytes,
            "context_window": m.context_window,
            "family": family_of(m.huggingface_repo),
            "generation": generation_of(m.huggingface_repo),
        }
        for m in MODELS.values()
    ]


def gguf_repo_names(repo: str, policy: Policy) -> list[str]:
    """Where a trusted GGUF build of `org/name` would live, most trusted first."""
    org, name = repo.split("/", 1)
    names = [f"{org}/{name}-GGUF"]
    for pub in policy.gguf_publishers:
        names.append(f"{pub}/{name}-GGUF")
        if pub == "bartowski":
            names.append(f"bartowski/{org}_{name}-GGUF")
    return names


def find_gguf(hub: Hub, repo: str, policy: Policy) -> dict[str, Any] | None:
    fallback = None
    for candidate in gguf_repo_names(repo, policy):
        info = hub.model(candidate, blobs=True)
        if not info:
            continue
        summary = summarize_gguf(info, policy.quant)
        if summary["quant_bytes"]:
            return summary
        fallback = fallback or summary
    return fallback


def survey(policy: Policy, since: date, hub: Hub, client: httpx.Client) -> dict[str, Any]:
    pin = llamacpp.pinned_tag()
    arch_pin, arch_head = llamacpp.architectures_at(pin, client), llamacpp.architectures_at("master", client)
    curated = curated_models()

    considered: list[dict[str, Any]] = []
    for org in policy.orgs:
        rows = [r for r in hub.org_models(org) if (r.get("createdAt") or "")[:10] >= since.isoformat()]
        rows = [r for r in rows if r.get("pipeline_tag") in policy.pipeline_tags]
        rows = [r for r in rows if not excluded_fragment(r["id"], policy) and "gguf" not in r["id"].lower()]
        for row in rows[:PER_ORG]:
            info = hub.model(row["id"])
            if not info:
                continue
            model = summarize_model(info)
            gguf = find_gguf(hub, row["id"], policy)
            verdict = screen(model, gguf, policy, arch_at_pin=arch_pin, arch_at_head=arch_head)
            considered.append({"model": model, "gguf": gguf, "flags": flags_for(row["id"], policy), **verdict})

    candidates = rank([c for c in considered if c["verdict"] == "candidate"], curated, date.today())
    screened = sorted((c for c in considered if c["verdict"] == "screened"), key=lambda c: -(c["model"]["likes"] or 0))

    audit = []
    catalogue: dict[str, list[dict[str, Any]]] = {}
    for c in curated:
        info = hub.model(c["repo"], blobs=True)
        gguf = summarize_gguf(info, policy.quant) if info else None
        org = policy.family_orgs.get(c["family"] or "")
        if org and org not in catalogue:
            catalogue[org] = hub.org_models(org, limit=500)
        successors = []
        for s in size_matched_successors(c, catalogue.get(org or "", []), policy)[:2]:
            sg = find_gguf(hub, s["repo"], policy)
            sm = summarize_model(hub.model(s["repo"]) or {"id": s["repo"]})
            successors.append({**s, "generation": list(s["generation"]), "license": sm.get("license"), "gguf": sg})
        findings = []
        ctx = (gguf or {}).get("context_length")
        if ctx and ctx < c["context_window"]:
            findings.append(f"registry context_window {c['context_window']} EXCEEDS the GGUF's {ctx}")
        elif ctx and ctx > c["context_window"] * 1.3:
            findings.append(f"registry context_window {c['context_window']} leaves the GGUF's {ctx} unused")
        if gguf and gguf["quant_bytes"] and abs(gguf["quant_bytes"] - c["size_bytes"]) > 0.05 * c["size_bytes"]:
            findings.append(
                f"registry size {c['size_bytes'] / 1e9:.2f} GB but the file is {gguf['quant_bytes'] / 1e9:.2f} GB"
            )
        if gguf and gguf["architecture"] and gguf["architecture"] not in arch_pin:
            findings.append(f"architecture {gguf['architecture']!r} is not in the pinned llama.cpp")
        if successors:
            findings.append("size-matched successor: " + ", ".join(s["repo"] for s in successors))
        elif any(
            family_of(k["model"]["repo"]) == c["family"] and generation_of(k["model"]["repo"]) > c["generation"]
            for k in candidates
        ):
            findings.append("a newer generation of the family exists, but not in this size class")
        audit.append(
            {**c, "generation": list(c["generation"]), "gguf": gguf, "successors": successors, "findings": findings}
        )

    watched = {o.lower() for o in policy.orgs} | {p.lower() for p in policy.gguf_publishers}
    outside: dict[str, dict[str, Any]] = {}
    for tag in policy.pipeline_tags:
        for r in hub.trending(tag):
            author = r["id"].split("/")[0].lower()
            if author in watched or (r.get("createdAt") or "")[:10] < since.isoformat():
                continue
            if excluded_fragment(r["id"], policy) or "gguf" in r["id"].lower():
                continue
            outside[r["id"]] = {
                "repo": r["id"],
                "created": (r.get("createdAt") or "")[:10],
                "likes": r.get("likes", 0),
                "downloads": r.get("downloads", 0),
            }

    return {
        "since": since.isoformat(),
        "llamacpp": {
            "pin": pin,
            "latest": llamacpp.latest_release(client),
            "architectures_at_pin": len(arch_pin),
            "architectures_at_head": len(arch_head),
            "only_at_head": sorted(arch_head - arch_pin),
        },
        "curated": audit,
        "candidates": candidates,
        "screened": screened,
        "outside_watchlist": sorted(outside.values(), key=lambda r: -r["likes"])[:12],
    }


def _gb(n: int | None) -> str:
    return f"{n / 1e9:.1f}" if n else "–"


def _b(n: int | None) -> str:
    return f"{n / 1e9:.1f}B" if n else "–"


def render(facts: dict[str, Any], policy: Policy, run_id: str, now: datetime) -> str:
    lc = facts["llamacpp"]
    out = [
        f"# Model survey — {now:%Y-%m-%d}",
        "",
        f"- **Run id:** `{run_id}` · window: releases since {facts['since']} · {len(policy.orgs)} vendors watched",
        f"- **Policy:** {policy.quant} at most {policy.ceiling_gb:g} GB, context at least {policy.min_context}, tool calling in the chat "
        f"template, permissive licence, GGUF from the vendor or {', '.join(policy.gguf_publishers)} (`scripts/model_survey/watchlist.yaml`)",
        f"- **llama.cpp:** pinned `{lc['pin']}` ({lc['architectures_at_pin']} architectures); upstream latest "
        f"`{lc['latest']['tag']}` ({lc['latest']['published']}, {lc['architectures_at_head']} architectures at head)",
        "- **Method:** Hugging Face Hub API and GitHub, unauthenticated; no model was run. Judge model: n/a · cloud spend: n/a",
        "",
        "## Reading",
        "",
        "_Written by whoever ran the loop: what the facts below mean and what to do next._",
        "",
        "## Candidates, ranked",
        "",
        "| # | Release | Released | Licence | Params | GGUF | Q4 GB | Context | Arch | Score | Why |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, c in enumerate(facts["candidates"], start=1):
        m, g = c["model"], c["gguf"]
        why = "; ".join(f"{k} +{v:g}" for k, v in c["score_parts"].items())
        notes = " ".join(f"⚠ {n}." for n in c["notes"]) + (f" flags: {', '.join(c['flags'])}" if c["flags"] else "")
        out.append(
            f"| {i} | `{m['repo']}` | {m['created']} | {m['license']} | {_b(m['params'])} | `{g['repo']}` | {_gb(g['quant_bytes'])} | "
            f"{g['context_length']} | {g['architecture']} | {c['score']:g} | {why}{('. ' + notes) if notes.strip() else ''} |"
        )
    if not facts["candidates"]:
        out.append("| – | none in the window | | | | | | | | | |")
    out += [
        "",
        "## The curated set",
        "",
        "| Model | GGUF repo | Registry ctx | GGUF ctx | Repo last modified | Findings |",
        "|---|---|---|---|---|---|",
    ]
    for c in facts["curated"]:
        g = c["gguf"] or {}
        out.append(
            f"| `{c['id']}` | `{c['repo']}` | {c['context_window']} | {g.get('context_length', '–')} | {g.get('modified', '–')} | "
            f"{'; '.join(c['findings']) or 'none'} |"
        )
    out += [
        "",
        "## What replaces each tier",
        "",
        "Size-matched successors from the vendor's whole catalogue, not only the window.",
        "",
    ]
    out += [
        "| Curated | Successor | Released | Licence | GGUF | Q4 GB | Context | Arch |",
        "|---|---|---|---|---|---|---|---|",
    ]
    tier_rows = 0
    for c in facts["curated"]:
        for s in c["successors"]:
            g = s["gguf"] or {}
            tier_rows += 1
            out.append(
                f"| `{c['id']}` ({c['size_bytes'] / 1e9:.1f} GB) | `{s['repo']}` | {s['created']} | {s['license']} | "
                f"`{g.get('repo', '–')}` | {_gb(g.get('quant_bytes'))} | {g.get('context_length', '–')} | {g.get('architecture', '–')} |"
            )
    if not tier_rows:
        out.append("| – | none found | | | | | | |")
    out += [
        "",
        "## llama.cpp",
        "",
        f"Architectures upstream can load that the pin cannot: {', '.join(f'`{a}`' for a in lc['only_at_head']) or 'none'}.",
    ]
    out += ["", "## Screened out", "", "| Release | Released | Likes | Why not |", "|---|---|---|---|"]
    for c in facts["screened"][:30]:
        m = c["model"]
        out.append(f"| `{m['repo']}` | {m['created']} | {m['likes']} | {'; '.join(c['reasons'])} |")
    out += [
        "",
        "## Trending outside the watchlist",
        "",
        "Not screened; listed so a new publisher is noticed. Add the org to the watchlist to bring it in.",
        "",
    ]
    out += [f"- `{r['repo']}` · {r['created']} · {r['likes']} likes" for r in facts["outside_watchlist"]] or ["- none"]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path, help="docs/reports/ or a path ending in .md")
    ap.add_argument("--since", type=date.fromisoformat, default=date.today() - timedelta(days=90))
    ap.add_argument("--run-id", default=f"survey-{datetime.now():%Y%m%d-%H%M}")
    ap.add_argument("--json", type=Path, help="also write the gathered facts here")
    ap.add_argument("--cache", type=Path, help="directory for a 6-hour response cache, so re-runs do not re-fetch")
    args = ap.parse_args(argv)

    policy = load_policy()
    with httpx.Client(timeout=30, headers={"User-Agent": "harbor-clerk-model-survey"}) as client:
        hub = Hub(cache_dir=args.cache)
        facts = survey(policy, args.since, hub, client)
        print(f"{hub.requests} Hub requests", file=sys.stderr)
    out = args.out
    if out.suffix != ".md":
        out.mkdir(parents=True, exist_ok=True)
        out = out / f"{date.today():%Y-%m-%d}-model-survey-{args.run_id}.md"
    if out.exists():
        print(f"refusing to overwrite {out}; one file per run", file=sys.stderr)
        return 2
    out.write_text(render(facts, policy, args.run_id, datetime.now()), encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps(facts, indent=2, default=str), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
