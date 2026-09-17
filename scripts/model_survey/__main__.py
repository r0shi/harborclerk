"""Survey the Hub for local models worth curating, and audit the curated set.

    uv run python -m scripts.model_survey --out docs/reports/ [--since 2026-07-22] [--run-id NAME] [--json facts.json]

Gathers facts (new releases per watched vendor, trusted GGUF builds, the
llama.cpp pin against upstream, drift in the curated registry), applies the
policy in `watchlist.yaml`, and writes a dated report: the ranking, why each
examined release was screened, and what was left out before examination (by
name, by task, or by the per-vendor cap). The "Reading" section at the top is
left for whoever runs the loop: the script states facts, a person or an agent
states what to do about them.

The loop's state lives in the reports, in a versioned JSON block at the end of
each one that records the run's inputs as well as its outputs. By default the
window opens at the previous report's date, and what that report could not
settle is examined again although it is older than the window: releases
waiting for a GGUF, and releases over the per-vendor cap. A vendor the
previous report did not watch gets a first-run window. A run on the same day
as the previous report, or with `--redo`, replaces that report: it repeats its
inputs exactly (window, carried releases, known vendors), which is what a
re-run after a policy fix needs. A previous report whose state cannot be read
ends the run unless `--since` starts a fresh window.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from scripts.model_survey import llamacpp
from scripts.model_survey.hf import Hub, file_size, summarize_gguf, summarize_model
from scripts.model_survey.screen import (
    Policy,
    chat_task,
    excluded_fragment,
    family_of,
    flags_for,
    generation_of,
    ladder_gap_fillers,
    load_policy,
    only_waiting_for_gguf,
    params_class,
    rank,
    screen,
    size_matched_successors,
)

REPORTS = Path(__file__).resolve().parents[2] / "docs" / "reports"
PER_ORG = 12  # releases examined per vendor per run, most liked first; the rest are carried to the next run
LISTING_LIMIT = 500  # repos listed per vendor, newest first
SUCCESSORS_EXAMINED = 4  # size-matched successors screened per tier; more than that is reported, not hidden
GAP_FILLERS_EXAMINED = 4  # the same, per curated family
FIRST_RUN_DAYS = 90  # the window of a first run, and of a vendor the previous report did not watch
WAIT_DAYS = 120  # how long a release stays on the waiting list
QUIET_LIKES = 10  # screened releases under this many likes are listed on one line, not tabulated
STATE_FORMAT = 1
STATE_HEADING = "## State"
CARRIED_NOTE = "carried from the previous survey"


class PlanError(RuntimeError):
    """The previous report cannot tell this run what to do."""


def utc_now() -> datetime:
    """The Hub's dates are UTC, so the window and the report's date are too."""
    return datetime.now(UTC)


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
            "params_b": params_class(m.huggingface_repo),
        }
        for m in MODELS.values()
    ]


def report_date(path: Path) -> date | None:
    try:
        return date.fromisoformat(path.name[:10])
    except ValueError:
        return None


def state_of(facts: dict[str, Any], now: datetime) -> dict[str, Any]:
    """What the next run needs from this one. `inputs` are what this run was
    given, so a run that replaces it can repeat it exactly; `outputs` are what
    it could not settle, for the run that follows it."""
    return {
        "format": STATE_FORMAT,
        "generated_at": now.isoformat(timespec="seconds"),
        "since": facts["since"],
        "inputs": {"carried": facts["carried"], "known_orgs": facts["known_orgs"]},
        "outputs": {
            "waiting": [w["repo"] for w in facts["waiting"]],
            "over_cap": [r["repo"] for r in facts["not_examined"]],
            "vendors": [v["org"] for v in facts["vendors"]],
        },
    }


def state_from_report(text: str) -> dict[str, Any] | None:
    """The state block of a report, or None when there is none this tool can
    read. Nothing else in a report is parsed: headings and lists are for
    people and may change."""
    found = re.findall(rf"^{re.escape(STATE_HEADING)}\n.*?^```json\n(.*?)\n```", text, flags=re.DOTALL | re.MULTILINE)
    if not found:
        return None
    try:
        state = json.loads(found[-1])
    except json.JSONDecodeError:
        return None
    return state if isinstance(state, dict) and state.get("format") == STATE_FORMAT else None


def pending(state: dict[str, Any]) -> list[str]:
    """What a report could not settle: waiting for a GGUF, or over the per-vendor cap."""
    return list(dict.fromkeys([*state["outputs"]["waiting"], *state["outputs"]["over_cap"]]))


def previous_report(directory: Path | None = None) -> Path | None:
    """The newest survey report: by the date in its name, then by when its
    state says it was generated, because run ids do not sort (`ix-10` comes
    before `ix-3`)."""
    directory = REPORTS if directory is None else directory
    if not directory.is_dir():
        return None

    def order(path: Path) -> tuple[str, str, str]:
        state = state_from_report(path.read_text(encoding="utf-8")) or {}
        return (path.name[:10], str(state.get("generated_at", "")), path.name)

    found = sorted(directory.glob("*-model-survey-*.md"), key=order)
    return found[-1] if found else None


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
    """The most trusted build that would pass the screen's GGUF-side checks;
    failing that, the most trusted build with the quant; failing that, the
    first repo that exists. A vendor build with a short context or a template
    without tools must not hide a trusted publisher's build that has both."""
    with_quant = existing = None
    for candidate in gguf_repo_names(repo, policy):
        info = hub.model(candidate, blobs=True)
        if not info:
            continue
        summary = summarize_gguf(info, policy.quant)
        existing = existing or summary
        if not summary["quant_bytes"]:
            continue
        with_quant = with_quant or summary
        if (summary["context_length"] or 0) >= policy.min_context and summary["tool_calls_in_template"]:
            return summary
    return with_quant or existing


def _day(row: dict[str, Any]) -> str:
    return (row.get("createdAt") or "")[:10]


def survey(
    policy: Policy,
    since: date,
    hub: Hub,
    client: httpx.Client,
    *,
    curated: list[dict[str, Any]] | None = None,
    recheck: list[str] | tuple[str, ...] = (),
    known_orgs: set[str] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """`known_orgs` are the vendors the previous report watched. A watched
    vendor that is not among them has never been surveyed, so its window is a
    first run's, not the few days since the previous report."""
    today = today or utc_now().date()
    pin = llamacpp.pinned_tag()
    latest = llamacpp.latest_release(client)
    arch_pin = llamacpp.architectures_at(pin, client)
    arch_release = llamacpp.architectures_at(latest["tag"], client)
    arch_head = llamacpp.architectures_at("master", client)
    curated = curated_models() if curated is None else curated
    arch = {
        "arch_at_pin": arch_pin,
        "arch_at_head": arch_head,
        "arch_at_release": arch_release,
        "release_tag": latest["tag"],
    }

    def examine(repo: str, note: str | None = None) -> dict[str, Any] | None:
        info = hub.model(repo)
        if not info:
            return None
        model = summarize_model(info)
        gguf = find_gguf(hub, repo, policy)
        verdict = screen(model, gguf, policy, **arch)
        if note:
            verdict["notes"] = [*verdict["notes"], note]
        return {"model": model, "gguf": gguf, "flags": flags_for(repo, policy), **verdict}

    considered: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    not_examined: list[dict[str, Any]] = []
    coverage: list[str] = []
    vendors: list[dict[str, Any]] = []
    catalogue: dict[str, list[dict[str, Any]]] = {}

    def listing_of(org: str) -> list[dict[str, Any]]:
        if org not in catalogue:
            catalogue[org] = hub.org_models(org, limit=LISTING_LIMIT)
            if not catalogue[org]:
                # The Hub answers 200 [] for an org that was renamed or never existed.
                coverage.append(f"`{org}` lists no repos at all: renamed or mistyped in the watchlist?")
        return catalogue[org]

    for org in policy.orgs:
        listing = listing_of(org)
        first_run = known_orgs is not None and org.lower() not in known_orgs
        org_since = min(since, today - timedelta(days=FIRST_RUN_DAYS)) if first_run else since
        if len(listing) >= LISTING_LIMIT and _day(listing[-1]) >= org_since.isoformat():
            coverage.append(
                f"`{org}` has more than {LISTING_LIMIT} repos since {org_since}; the oldest were not listed"
            )
        eligible = []
        in_window = 0
        for row in listing:
            if _day(row) < org_since.isoformat() or "gguf" in row["id"].lower():
                continue
            in_window += 1
            fragment = excluded_fragment(row["id"], policy)
            if fragment:
                excluded.append({"repo": row["id"], "created": _day(row), "why": f"name contains '{fragment}'"})
            elif not chat_task(row.get("pipeline_tag"), policy):
                excluded.append({"repo": row["id"], "created": _day(row), "why": f"task is {row.get('pipeline_tag')}"})
            else:
                eligible.append(row)
        eligible.sort(key=lambda r: -(r.get("likes") or 0))
        examined_here = 0
        for row in eligible[:PER_ORG]:
            examined = examine(row["id"])
            if examined:
                considered.append(examined)
                examined_here += 1
            else:
                excluded.append({"repo": row["id"], "created": _day(row), "why": "not readable (gated or removed)"})
        not_examined += [
            {"repo": r["id"], "created": _day(r), "likes": r.get("likes") or 0} for r in eligible[PER_ORG:]
        ]
        vendors.append(
            {
                "org": org,
                "listed": len(listing),
                "in_window": in_window,
                "examined": examined_here,
                "since": org_since.isoformat(),
                "first_run": first_run,
            }
        )

    seen = {c["model"]["repo"].lower() for c in considered} | {e["repo"].lower() for e in excluded}
    rechecked = []
    for repo in recheck:
        if repo.lower() in seen:
            continue
        seen.add(repo.lower())
        fragment = excluded_fragment(repo, policy)
        if fragment:  # the policy changed since the release was carried
            excluded.append({"repo": repo, "created": "carried", "why": f"name contains '{fragment}'"})
            continue
        examined = examine(repo, CARRIED_NOTE)
        if examined:
            considered.append(examined)
            rechecked.append(repo)
    examined_ids = {c["model"]["repo"].lower() for c in considered}
    not_examined = [r for r in not_examined if r["repo"].lower() not in examined_ids]

    candidates = rank([c for c in considered if c["verdict"] == "candidate"], curated, today, policy)
    screened = sorted((c for c in considered if c["verdict"] == "screened"), key=lambda c: -(c["model"]["likes"] or 0))
    waiting = []
    for c in screened:
        try:
            age = (today - date.fromisoformat(c["model"]["created"])).days
        except ValueError:
            continue
        if only_waiting_for_gguf(c["reasons"], policy) and age <= WAIT_DAYS:
            waiting.append({"repo": c["model"]["repo"], "created": c["model"]["created"]})

    def screened_pair(repo: str) -> dict[str, Any]:
        """A successor or gap filler gets the same screen a candidate gets."""
        gguf = find_gguf(hub, repo, policy)
        model = summarize_model(hub.model(repo) or {"id": repo})
        verdict = screen(model, gguf, policy, **arch)
        return {
            "license": model.get("license"),
            "likes": model.get("likes"),
            "gguf": gguf,
            "reasons": verdict["reasons"],
            "notes": verdict["notes"],
        }

    audit = []
    for c in curated:
        info = hub.model(c["repo"], blobs=True)
        gguf = summarize_gguf(info, policy.quant) if info else None
        org = policy.family_orgs.get(c["family"] or "")
        matched = size_matched_successors(c, listing_of(org) if org else [], policy)
        if len(matched) > SUCCESSORS_EXAMINED:
            coverage.append(
                f"`{c['id']}` has {len(matched)} size-matched successors; only the first {SUCCESSORS_EXAMINED} were screened"
            )
        successors = [
            {**s, "generation": list(s["generation"]), **screened_pair(s["repo"])}
            for s in matched[:SUCCESSORS_EXAMINED]
        ]
        successors.sort(key=lambda s: bool(s["reasons"]))  # those that pass first; the order within each is kept
        passing = [s["repo"] for s in successors if not s["reasons"]]
        findings = []
        if info is None:
            findings.append("the GGUF repo is not reachable on the Hub (deleted, renamed, private or gated)")
        else:
            # The file the product downloads, not whichever file looks like the quant.
            actual = file_size(info, c["filename"])
            if actual is None:
                findings.append(f"registry file `{c['filename']}` is not in the repo")
            elif actual and abs(actual - c["size_bytes"]) > 0.05 * c["size_bytes"]:
                findings.append(f"registry size {c['size_bytes'] / 1e9:.2f} GB but the file is {actual / 1e9:.2f} GB")
        ctx = (gguf or {}).get("context_length")
        if ctx and ctx < c["context_window"]:
            findings.append(f"registry context_window {c['context_window']} EXCEEDS the GGUF's {ctx}")
        elif ctx and ctx > c["context_window"] * 1.3:
            findings.append(f"registry context_window {c['context_window']} leaves the GGUF's {ctx} unused")
        if gguf and gguf["architecture"] and gguf["architecture"] not in arch_pin:
            findings.append(f"architecture {gguf['architecture']!r} is not in the pinned llama.cpp")
        if passing:
            findings.append("size-matched successor: " + ", ".join(passing))
        elif successors:
            findings.append("size-matched successors exist, but none passes the screen yet")
        elif any(
            family_of(k["model"]["repo"]) == c["family"]
            and k["model"]["repo"].split("/")[0].lower() == (org or "").lower()
            and generation_of(k["model"]["repo"]) > tuple(c["generation"])
            for k in candidates
        ):
            findings.append("a newer generation of the family exists, but not in this size class")
        audit.append(
            {**c, "generation": list(c["generation"]), "gguf": gguf, "successors": successors, "findings": findings}
        )

    gap_fillers = []
    curated_ids = {c["repo"].lower() for c in curated}
    for family, org in policy.family_orgs.items():
        listing = listing_of(org)
        if len(listing) >= LISTING_LIMIT:
            coverage.append(
                f"the successor and gap search saw only the newest {LISTING_LIMIT} repos of `{org}`, back to {_day(listing[-1])}"
            )
        fillers = ladder_gap_fillers(curated, listing, family, policy)
        if len(fillers) > GAP_FILLERS_EXAMINED:
            coverage.append(
                f"`{family}` has {len(fillers)} gap fillers; only the first {GAP_FILLERS_EXAMINED} were screened"
            )
        for f in fillers[:GAP_FILLERS_EXAMINED]:
            pair = screened_pair(f["repo"])
            if pair["gguf"] and pair["gguf"]["repo"].lower() in curated_ids:
                continue
            gap_fillers.append({**f, **pair})

    watched = {o.lower() for o in policy.orgs} | {p.lower() for p in policy.gguf_publishers}
    outside: dict[str, dict[str, Any]] = {}
    for tag in policy.pipeline_tags:
        for r in hub.trending(tag):
            author = r["id"].split("/")[0].lower()
            if author in watched or _day(r) < since.isoformat():
                continue
            if excluded_fragment(r["id"], policy) or "gguf" in r["id"].lower():
                continue
            outside[r["id"]] = {
                "repo": r["id"],
                "created": _day(r),
                "likes": r.get("likes", 0),
                "downloads": r.get("downloads", 0),
            }

    return {
        "since": since.isoformat(),
        "llamacpp": {
            "pin": pin,
            "latest": latest,
            "architectures_at_pin": len(arch_pin),
            "architectures_at_release": len(arch_release),
            "architectures_at_head": len(arch_head),
            "in_release_not_pin": sorted(arch_release - arch_pin),
            "only_on_master": sorted(arch_head - arch_release - arch_pin),
        },
        "curated": audit,
        "gap_fillers": gap_fillers,
        "candidates": candidates,
        "screened": screened,
        "waiting": waiting,
        "carried": list(recheck),
        "known_orgs": sorted(known_orgs) if known_orgs is not None else None,
        "rechecked": rechecked,
        "vendors": vendors,
        "excluded": excluded,
        "not_examined": not_examined,
        "coverage": coverage,
        "outside_watchlist": sorted(outside.values(), key=lambda r: -r["likes"])[:12],
    }


def _gb(n: int | None) -> str:
    return f"{n / 1e9:.1f}" if n else "–"


def _b(n: int | None) -> str:
    return f"{n / 1e9:.1f}B" if n else "–"


def _screen_cell(item: dict[str, Any]) -> str:
    return "; ".join([*item["reasons"], *(f"⚠ {n}" for n in item["notes"])]) or "passes"


def commit_id() -> str:
    """The commit the tool and its policy ran from; `-dirty` when the tree was not clean."""
    try:
        r = subprocess.run(
            ["git", "describe", "--always", "--dirty"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "unknown"


def render(
    facts: dict[str, Any],
    policy: Policy,
    run_id: str,
    now: datetime,
    commit: str = "unknown",
    previous: str | None = None,
    authenticated: bool = False,
) -> str:
    lc = facts["llamacpp"]
    examined = len(facts["candidates"]) + len(facts["screened"])
    out = [
        f"# Model survey — {now:%Y-%m-%d}",
        "",
        f"- **Run id:** `{run_id}` · commit `{commit}` · window: releases since {facts['since']} · {len(policy.orgs)} vendors watched",
        f"- **Previous survey:** {previous or 'none'} · {len(facts['carried'])} release(s) carried from its lists, "
        f"{len(facts['rechecked'])} examined again (the rest were already in the window, or are gone)",
        f"- **Policy:** {policy.quant} at most {policy.ceiling_gb:g} GB, context at least {policy.min_context}, tool calling in the chat "
        f"template, permissive licence, GGUF from the vendor or {', '.join(policy.gguf_publishers)} (`scripts/model_survey/watchlist.yaml`)",
        f"- **Coverage:** {examined} releases examined · {len(facts['excluded'])} left out by name or task · "
        f"{len(facts['not_examined'])} over the cap of {PER_ORG} per vendor · all three are listed below, with a count per vendor"
        + "".join(f" · ⚠ {w}" for w in facts["coverage"]),
        f"- **llama.cpp:** pinned `{lc['pin']}` ({lc['architectures_at_pin']} architectures); latest stable release "
        f"`{lc['latest']['tag']}` ({lc['latest']['published']}, {lc['architectures_at_release']}); master {lc['architectures_at_head']}",
        f"- **Method:** Hugging Face Hub API ({'with a token' if authenticated else 'unauthenticated'}) and GitHub; no model was "
        "run, so machine, deployment and corpus do not apply. Judge model: n/a · cloud spend: n/a",
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
        "Size-matched successors from the vendor's whole catalogue, not only the window, put through the same screen.",
        "",
        "| Curated | Successor | Released | Licence | GGUF | Q4 GB | Context | Arch | Screen |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    tier_rows = 0
    for c in facts["curated"]:
        for s in c["successors"]:
            g = s["gguf"] or {}
            tier_rows += 1
            out.append(
                f"| `{c['id']}` ({c['size_bytes'] / 1e9:.1f} GB) | `{s['repo']}` | {s['created']} | {s['license']} | "
                f"`{g.get('repo', '–')}` | {_gb(g.get('quant_bytes'))} | {g.get('context_length', '–')} | "
                f"{g.get('architecture', '–')} | {_screen_cell(s)} |"
            )
    if not tier_rows:
        out.append("| – | none found | | | | | | | |")
    out += [
        "",
        "## Gaps in the size ladder",
        "",
        "Members of a curated family that would sit in a gap of the ladder wider than 4 GB, put through the same screen.",
        "",
        "| Release | Released | Licence | Gap | GGUF | Q4 GB | Context | Arch | Screen |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for f in facts["gap_fillers"]:
        g = f["gguf"] or {}
        out.append(
            f"| `{f['repo']}` | {f['created']} | {f['license']} | {f['gap_gb'][0]:g} to {f['gap_gb'][1]:g} GB | `{g.get('repo', '–')}` | "
            f"{_gb(g.get('quant_bytes'))} | {g.get('context_length', '–')} | {g.get('architecture', '–')} | {_screen_cell(f)} |"
        )
    if not facts["gap_fillers"]:
        out.append("| none found | | | | | | | | |")
    out += [
        "",
        "## llama.cpp",
        "",
        f"In the stable release `{lc['latest']['tag']}` but not in the pin: "
        f"{', '.join(f'`{a}`' for a in lc['in_release_not_pin']) or 'none'}.",
        "",
        f"Only on master, in no stable release yet: {', '.join(f'`{a}`' for a in lc['only_on_master']) or 'none'}.",
        "",
        "## Screened out",
        "",
        "Every release that was examined and failed the screen, with every reason.",
        "",
        "| Release | Released | Likes | Why not |",
        "|---|---|---|---|",
    ]
    loud = [c for c in facts["screened"] if (c["model"]["likes"] or 0) >= QUIET_LIKES]
    quiet = [c for c in facts["screened"] if (c["model"]["likes"] or 0) < QUIET_LIKES]
    for c in loud:
        m = c["model"]
        out.append(f"| `{m['repo']}` | {m['created']} | {m['likes']} | {'; '.join(c['reasons'])} |")
    if not loud:
        out.append("| none | | | |")
    if quiet:
        out += [
            "",
            f"And {len(quiet)} with under {QUIET_LIKES} likes: "
            + "; ".join(f"`{c['model']['repo']}` ({', '.join(c['reasons'])})" for c in quiet)
            + ".",
        ]
    out += [
        "",
        "## Waiting for a GGUF",
        "",
        f"Screened only because no trusted {policy.quant} build exists yet. The next survey examines "
        f"these again although they will be older than its window. A release drops off {WAIT_DAYS} days after it was published.",
        "",
    ]
    out += [f"- `{w['repo']}` · {w['created']}" for w in facts["waiting"]] or ["- none"]
    out += [
        "",
        "## Left out before examination",
        "",
        "### Over the per-vendor cap",
        "",
        f"The least liked of a vendor's eligible releases, past the {PER_ORG} that were examined. The next survey "
        "examines them.",
        "",
    ]
    out += [f"- `{r['repo']}` · {r['created']} · {r['likes']} likes" for r in facts["not_examined"]] or ["- none"]
    out += [
        "",
        "### By name or task",
        "",
        "This is how fine-tuning artefacts, quantised duplicates and non-chat models are kept out. A model wrongly "
        "listed here means a fragment in the policy is too greedy.",
        "",
    ]
    by_org: dict[str, list[str]] = {}
    for e in facts["excluded"]:
        org, name = e["repo"].split("/", 1)
        by_org.setdefault(org, []).append(f"`{name}` ({e['why']})")
    out += [f"- `{org}`: {'; '.join(names)}" for org, names in sorted(by_org.items())] or ["- none"]
    out += [
        "",
        "## Vendors",
        "",
        "What each watched vendor's listing held. A vendor the next survey does not find here gets a "
        f"first-run window of {FIRST_RUN_DAYS} days.",
        "",
    ]
    out += [
        f"- `{v['org']}` · {v['listed']} listed · {v['in_window']} since {v['since']}"
        f"{' (first run for this vendor)' if v['first_run'] else ''} · {v['examined']} examined"
        for v in facts["vendors"]
    ] or ["- none"]
    out += [
        "",
        "## Trending outside the watchlist",
        "",
        "Not screened; listed so a new publisher is noticed. Add the org to the watchlist to bring it in.",
        "",
    ]
    out += [f"- `{r['repo']}` · {r['created']} · {r['likes']} likes" for r in facts["outside_watchlist"]] or ["- none"]
    out += [
        "",
        STATE_HEADING,
        "",
        "Read by the next run, not by people: the window and carried releases this run was given, and what it could not "
        "settle. Nothing else in a report is parsed.",
        "",
        "```json",
        json.dumps(state_of(facts, now), indent=1, sort_keys=True),
        "```",
    ]
    return "\n".join(out) + "\n"


def plan(previous: Path | None, since: date | None, redo: bool, today: date) -> dict[str, Any]:
    """The window, the carried releases and the known vendors for this run.

    Following a report: the window opens at its date, and its outputs are
    carried. Replacing one (it is dated today, or `--redo`): its inputs are
    repeated exactly, so whatever it found through a carried release or a
    vendor's first-run window is found again. Its outputs would not do: a
    release it settled is no longer pending, and a vendor it surveyed for the
    first time is no longer new."""
    if previous is None:
        return {
            "since": since or today - timedelta(days=FIRST_RUN_DAYS),
            "recheck": [],
            "known_orgs": None,
            "label": None,
            "warnings": [],
        }
    state = state_from_report(previous.read_text(encoding="utf-8"))
    if state is None:
        if since is None:
            raise PlanError(
                f"{previous.name} has no state block this tool can read (format {STATE_FORMAT}). "
                "Pass --since to open a fresh window; nothing will be carried."
            )
        warning = f"the state of `{previous.name}` could not be read: nothing was carried and no vendor counts as new"
        return {"since": since, "recheck": [], "known_orgs": None, "label": f"`{previous.name}`", "warnings": [warning]}
    repeat = redo or report_date(previous) == today
    if repeat:
        opened = date.fromisoformat(state["since"])
        recheck, known = state["inputs"]["carried"], state["inputs"]["known_orgs"]
    else:
        opened = report_date(previous) or date.fromisoformat(state["generated_at"][:10])
        recheck, known = pending(state), state["outputs"]["vendors"]
    return {
        "since": since or opened,
        "recheck": list(recheck),
        "known_orgs": {o.lower() for o in known} if known is not None else None,
        "label": f"`{previous.name}`" + (", which this run replaces and whose inputs it repeats" if repeat else ""),
        "warnings": [],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path, help="docs/reports/ or a path ending in .md")
    ap.add_argument("--since", type=date.fromisoformat, help="override the window's start")
    ap.add_argument("--previous", type=Path, help="the previous survey report; default: the newest in docs/reports")
    ap.add_argument(
        "--redo", action="store_true", help="replace the previous report: repeat its inputs, after a policy or rule fix"
    )
    ap.add_argument("--run-id", default=f"survey-{utc_now():%Y%m%d-%H%M}")
    ap.add_argument("--json", type=Path, help="also write the gathered facts here")
    ap.add_argument("--cache", type=Path, help="directory for a 6-hour response cache, so re-runs do not re-fetch")
    args = ap.parse_args(argv)

    now = utc_now()
    today = now.date()
    out = args.out
    if out.suffix != ".md":
        out = out / f"{today:%Y-%m-%d}-model-survey-{args.run_id}.md"
    if out.exists():  # before any request: a refused run costs the Hub nothing
        print(f"refusing to overwrite {out}; one file per run", file=sys.stderr)
        return 2

    try:
        run = plan(args.previous or previous_report(), args.since, args.redo, today)
    except PlanError as e:
        print(e, file=sys.stderr)
        return 2
    print(
        f"window since {run['since']}; {len(run['recheck'])} carried; previous: {run['label'] or 'none'}",
        file=sys.stderr,
    )

    policy = load_policy()
    with (
        httpx.Client(timeout=30, headers={"User-Agent": "harbor-clerk-model-survey"}) as client,
        Hub(cache_dir=args.cache) as hub,
    ):
        facts = survey(
            policy, run["since"], hub, client, recheck=run["recheck"], known_orgs=run["known_orgs"], today=today
        )
        facts["coverage"] = [*run["warnings"], *facts["coverage"]]
        authenticated = hub.authenticated
        print(f"{hub.requests} Hub requests", file=sys.stderr)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = render(facts, policy, args.run_id, now, commit_id(), previous=run["label"], authenticated=authenticated)
    out.write_text(text, encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps(facts, indent=2, default=str), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
