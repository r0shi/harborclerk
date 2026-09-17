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
each one that records the run's inputs as well as its outputs. The Hub dates a
repo by its creation, and vendors create repos privately and publish them days
later (Gemma 3: eleven days), so the window opens 30 days before the previous
report and skips what earlier runs already examined, which the state also
records. What that report could not settle is examined again however old it
is: releases that may pass later (no GGUF yet, an architecture llama.cpp
cannot load yet, a template or context a publisher may fix), and releases over
the per-vendor cap. A vendor the
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
    full_precision_duplicate,
    generation_of,
    instruct_sibling,
    ladder_gap_fillers,
    load_policy,
    may_pass_later,
    params_class,
    rank,
    release_name,
    screen,
    size_matched_successors,
)

REPORTS = Path(__file__).resolve().parents[2] / "docs" / "reports"
PER_ORG = 12  # releases examined per vendor per run, most liked first; the rest are carried to the next run
LISTING_LIMIT = 500  # repos listed per vendor, newest first
SUCCESSORS_EXAMINED = 12  # every size-matched successor of a tier is screened, up to this many; a cut is reported
GAP_FILLERS_EXAMINED = 12  # the same, per curated family
OVERLAP_DAYS = 30  # how far before the previous report the window opens, for repos published after they were created
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
    it could not settle and what it examined, for the run that follows it.
    Carried releases keep their creation date, so one that cannot be read for
    a run can still be aged out instead of being dropped or kept for ever."""
    return {
        "format": STATE_FORMAT,
        "generated_at": now.isoformat(timespec="seconds"),
        "since": facts["since"],
        "inputs": {
            "carried": facts["carried"],
            "known_orgs": facts["known_orgs"],
            "seen": facts["seen"],
            "left_out": facts["left_out"],
            "first_run_since": facts["first_run_since"],
            "since_source": facts["since_source"],
        },
        "outputs": {
            "waiting": {w["repo"]: w["created"] for w in facts["waiting"]},
            "over_cap": {r["repo"]: r["created"] for r in facts["not_examined"]},
            # A vendor that listed nothing was renamed or mistyped. Once the
            # watchlist is fixed it must count as new, so it is not recorded.
            "vendors": [v["org"] for v in facts["vendors"] if v["listed"]],
            "examined": facts["examined_log"],
            "left_out": facts["left_out_log"],
        },
    }


def _iso_day(value: Any) -> bool:
    try:
        return isinstance(value, str) and date.fromisoformat(value) is not None
    except ValueError:
        return False


def _days_by_repo(value: Any) -> bool:
    """repo -> creation day; the day may be empty when it was never known."""
    return isinstance(value, dict) and all(isinstance(k, str) and (v == "" or _iso_day(v)) for k, v in value.items())


def _names(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


_STATE_SHAPE = {
    "generated_at": lambda v: isinstance(v, str) and _iso_day(v[:10]),
    "since": _iso_day,
    "inputs": {
        "carried": _days_by_repo,
        "known_orgs": lambda v: v is None or _names(v),
        "seen": _days_by_repo,
        "left_out": _days_by_repo,
        "first_run_since": _iso_day,
        "since_source": lambda v: isinstance(v, str),
    },
    "outputs": {
        "waiting": _days_by_repo,
        "over_cap": _days_by_repo,
        "vendors": _names,
        "examined": _days_by_repo,
        "left_out": _days_by_repo,
    },
}


def _fits(value: Any, shape: Any) -> bool:
    if isinstance(shape, dict):
        # A key must be present, not merely acceptable when absent: `None` is a value some keys allow.
        return isinstance(value, dict) and all(k in value and _fits(value[k], s) for k, s in shape.items())
    return bool(shape(value))


def state_from_report(text: str) -> dict[str, Any] | None:
    """The state block of a report, or None when there is none this tool can
    read: no block, JSON that does not parse (a merge conflict), another
    format, a key missing or a value that is not what it should be (a
    hand-edited report). Nothing else in a report is parsed: headings and
    lists are for people and may change."""
    found = re.findall(rf"^{re.escape(STATE_HEADING)}\n.*?^```json\n(.*?)\n```", text, flags=re.DOTALL | re.MULTILINE)
    if not found:
        return None
    try:
        state = json.loads(found[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(state, dict) or state.get("format") != STATE_FORMAT or not _fits(state, _STATE_SHAPE):
        return None
    return state


def pending(state: dict[str, Any]) -> dict[str, str]:
    """What a report could not settle, with each release's creation day: may
    pass later, or over the per-vendor cap."""
    return {**state["outputs"]["waiting"], **state["outputs"]["over_cap"]}


def previous_report(directory: Path | None = None) -> Path | None:
    """The newest survey report: by the date in its name, then by when its
    state says it was generated, because run ids do not sort (`ix-10` comes
    before `ix-3`). A report whose state cannot be read has no such time; it
    counts as the newest of its day, so the run stops at it instead of
    quietly following or replacing an older one."""
    directory = REPORTS if directory is None else directory
    if not directory.is_dir():
        return None

    def order(path: Path) -> tuple[str, bool, str, str]:
        state = state_from_report(path.read_text(encoding="utf-8"))
        return (path.name[:10], state is None, str((state or {}).get("generated_at", "")), path.name)

    found = sorted(directory.glob("*-model-survey-*.md"), key=order)
    return found[-1] if found else None


def gguf_repo_names(repo: str, policy: Policy) -> list[str]:
    """Where a trusted GGUF build of `org/name` would live, most trusted first."""
    org, name = release_name(repo, policy).split("/", 1)
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
    recheck: dict[str, str] | list[str] | tuple[str, ...] = (),
    known_orgs: set[str] | None = None,
    seen: dict[str, str] | None = None,
    left_out: dict[str, str] | None = None,
    first_run_since: date | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """`known_orgs` are the vendors the previous report watched. A watched
    vendor that is not among them has never been surveyed, so its window opens
    at `first_run_since`, not the few days since the previous report.

    `seen` maps each release earlier runs examined to its creation date. The
    window overlaps the previous one, and those are skipped. `left_out` maps
    each release earlier reports listed as left out by name or task; those
    are counted here, not listed again. Both are records, not inferences.

    `recheck` maps each carried release to its creation day ("" when it was
    never known). A list is accepted and means the days are unknown."""
    today = today or utc_now().date()
    seen = {k.lower(): v for k, v in (seen or {}).items()}
    left_out = {k.lower(): v for k, v in (left_out or {}).items()}
    known_orgs = {o.lower() for o in known_orgs} if known_orgs is not None else None
    carried = dict(recheck) if isinstance(recheck, dict) else dict.fromkeys(recheck, "")
    first_run_since = first_run_since or today - timedelta(days=FIRST_RUN_DAYS)
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
        current = model["repo"]  # not `repo` when the owner has renamed it: builds are published under the new name
        gguf = find_gguf(hub, current, policy)
        verdict = screen(model, gguf, policy, **arch)
        if note:
            verdict["notes"] = [*verdict["notes"], note]
        return {"model": model, "gguf": gguf, "flags": flags_for(current, policy), **verdict}

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

    overlap_excluded = 0
    left_out_log: dict[str, str] = {}
    unreadable: list[dict[str, str]] = []
    dropped: set[str] = set()

    def left_out_reason(repo: str, ids: set[str], tag: str | None) -> str | None:
        """Why a release is not examined at all. The window and the carried
        list both ask, so a carried release meets the same exclusions. A
        carried release comes without a task, which `chat_task` lets through."""
        fragment = excluded_fragment(repo, policy)
        if fragment:
            return f"name contains '{fragment}'"
        if not chat_task(tag, policy):
            return f"task is {tag}"
        if instruct_sibling(repo, ids):
            return "pretrained base of its -it sibling"
        copy_of = full_precision_duplicate(repo, ids, policy)
        return f"full-precision copy of {copy_of.split('/', 1)[1]}" if copy_of else None

    def leave_out(repo: str, created: str, why: str) -> None:
        """Listed once. The state records it, and a later window that reaches
        it again counts it instead: that it was listed is known, not inferred
        from dates, which a repo published after it was created would defeat."""
        nonlocal overlap_excluded
        left_out_log[repo.lower()] = created
        if repo.lower() in left_out:
            overlap_excluded += 1
        else:
            excluded.append({"repo": repo, "created": created or "carried", "why": why})

    def keep_unreadable(repo: str, created: str) -> None:
        """A repo that reads as absent: renamed to another owner, private for a
        re-upload, or a 401 that will pass. One bad run must not lose it,
        whichever list it came from: it is named, and carried until it is too
        old to wait for. It is never logged as examined."""
        try:
            keep = bool(created) and (today - date.fromisoformat(created)).days <= WAIT_DAYS
        except ValueError:
            keep = False
        why = "not readable this run (renamed, private or gated); " + ("carried to the next" if keep else "dropped")
        excluded.append({"repo": repo, "created": created or "carried", "why": why})
        if keep:
            unreadable.append({"repo": repo, "created": created})
        else:
            dropped.add(repo.lower())

    for org in policy.orgs:
        listing = listing_of(org)
        ids = {row["id"].lower() for row in listing}
        first_run = known_orgs is not None and org.lower() not in known_orgs
        org_since = min(since, first_run_since) if first_run else since
        if len(listing) >= LISTING_LIMIT and _day(listing[-1]) >= org_since.isoformat():
            coverage.append(
                f"`{org}` has more than {LISTING_LIMIT} repos since {org_since}; the oldest were not listed"
            )
        eligible = []
        in_window = already = 0
        for row in listing:
            repo = row["id"]
            if _day(row) < org_since.isoformat() or "gguf" in repo.lower():
                continue
            in_window += 1
            if repo.lower() in seen:
                already += 1
                continue
            why = left_out_reason(repo, ids, row.get("pipeline_tag"))
            if why:
                leave_out(repo, _day(row), why)
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
                keep_unreadable(row["id"], _day(row))
        not_examined += [
            {"repo": r["id"], "created": _day(r), "likes": r.get("likes") or 0} for r in eligible[PER_ORG:]
        ]
        vendors.append(
            {
                "org": org,
                "listed": len(listing),
                "in_window": in_window,
                "already_examined": already,
                "examined": examined_here,
                "since": org_since.isoformat(),
                "first_run": first_run,
            }
        )

    handled = {c["model"]["repo"].lower() for c in considered} | {e["repo"].lower() for e in excluded}
    watched = {o.lower(): o for o in policy.orgs}
    for repo, created in carried.items():
        if repo.lower() in handled:
            continue
        handled.add(repo.lower())
        org = watched.get(repo.split("/")[0].lower())
        if org is None:
            # Its own `-GGUF` repo was trusted because it was a watched vendor's. It no longer is.
            leave_out(repo, created, "its vendor is no longer on the watchlist")
            continue
        ids = {row["id"].lower() for row in listing_of(org)}
        why = left_out_reason(repo, ids, None)  # the policy or the listing changed since it was carried
        if why:
            leave_out(repo, created, why)
            continue
        examined = examine(repo, CARRIED_NOTE)
        if examined is None:
            keep_unreadable(repo, created)
        elif examined["model"]["repo"].lower() not in handled - {repo.lower()}:
            # The owner may have renamed it; from here on it goes by its current name.
            handled.add(examined["model"]["repo"].lower())
            considered.append(examined)
    examined_ids = {c["model"]["repo"].lower() for c in considered}
    for c in considered:  # a carried release the window reached first is still a carried release
        if c["model"]["repo"].lower() in {r.lower() for r in carried} and CARRIED_NOTE not in c["notes"]:
            c["notes"] = [*c["notes"], CARRIED_NOTE]
    rechecked = [c["model"]["repo"] for c in considered if CARRIED_NOTE in c["notes"]]
    not_examined = [r for r in not_examined if r["repo"].lower() not in examined_ids]
    # What the next run may skip inside its overlap. Entries older than any overlap could reach are dropped.
    horizon = (today - timedelta(days=OVERLAP_DAYS + FIRST_RUN_DAYS)).isoformat()
    examined_log = {k: v for k, v in seen.items() if v >= horizon and k not in dropped}
    left_out_log = {**{k: v for k, v in left_out.items() if v >= horizon}, **left_out_log}
    examined_log.update({c["model"]["repo"].lower(): c["model"]["created"] for c in considered})

    candidates = rank([c for c in considered if c["verdict"] == "candidate"], curated, today, policy)
    screened = sorted((c for c in considered if c["verdict"] == "screened"), key=lambda c: -(c["model"]["likes"] or 0))
    waiting = []
    for c in screened:
        try:
            age = (today - date.fromisoformat(c["model"]["created"])).days
        except ValueError:
            continue
        if may_pass_later(c["reasons"], policy) and age <= WAIT_DAYS:
            waiting.append({"repo": c["model"]["repo"], "created": c["model"]["created"]})
    waiting += unreadable

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
    gap_fillers.sort(key=lambda f: bool(f["reasons"]))  # those that pass first

    not_outside = {o.lower() for o in policy.orgs} | {p.lower() for p in policy.gguf_publishers}
    outside: dict[str, dict[str, Any]] = {}
    for tag in policy.pipeline_tags:
        for r in hub.trending(tag):
            author = r["id"].split("/")[0].lower()
            if author in not_outside or _day(r) < since.isoformat():
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
        "carried": carried,
        "left_out": left_out,
        "left_out_log": left_out_log,
        "since_source": "given by the caller",
        "mode": "follows" if seen else "first",
        "known_orgs": sorted(known_orgs) if known_orgs is not None else None,
        "seen": seen,
        "first_run_since": first_run_since.isoformat(),
        "examined_log": examined_log,
        "overlap_excluded": overlap_excluded,
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
        f"- **Run id:** `{run_id}` · commit `{commit}` · window: releases since {facts['since']} ({facts['since_source']}) · {len(policy.orgs)} vendors watched",
        (
            f"- **Previous survey:** {previous} · {len(facts['carried'])} release(s) carried into this run, "
            f"{len(facts['rechecked'])} of them examined (the rest are listed under By name or task with the reason)"
            + (
                f" · the window overlaps earlier runs by {OVERLAP_DAYS} days, and "
                f"{sum(v['already_examined'] for v in facts['vendors'])} release(s) they examined were skipped"
                if facts["mode"] == "follows"
                else ""
            )
            if previous
            else "- **Previous survey:** none; this is a first run"
        ),
        f"- **Policy:** {policy.quant} at most {policy.ceiling_gb:g} GB, context at least {policy.min_context}, tool calling in the chat "
        f"template, permissive licence, GGUF from the vendor or {', '.join(policy.gguf_publishers)} (`scripts/model_survey/watchlist.yaml`)",
        f"- **Coverage:** {examined} releases examined · {len(facts['excluded'])} left out by name or task"
        f"{f' (and {facts["overlap_excluded"]} more that an earlier report listed)' if facts['overlap_excluded'] else ''} · "
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
        "## May pass later",
        "",
        f"Screened only for reasons time can cure: no trusted {policy.quant} build yet, an architecture llama.cpp cannot load "
        "yet, or a template or context a publisher may fix. The next survey examines these again however old they are. A "
        f"release drops off {WAIT_DAYS} days after its repo was created.",
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
        f"{f' · {v["already_examined"]} examined by an earlier run' if v['already_examined'] else ''}"
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


def _git(*args: str) -> bool:
    try:
        r = subprocess.run(["git", *args], cwd=REPORTS.parent, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def provenance_warning(report: Path, verb: str = "follows") -> str | None:
    """The loop's state is whatever report this checkout holds, which is not
    always what `main` holds: last week's report may sit in an open PR, or a
    closed one may have left its file behind. Following such a report is
    allowed, and the report that does so says it."""
    try:
        relative = report.resolve().relative_to(REPORTS.parent.parent.resolve()).as_posix()
    except ValueError:
        return None  # a report outside the repository, as --previous allows
    if _git("cat-file", "-e", f"origin/main:{relative}"):
        return None
    return f"`{report.name}` is not on origin/main (an open PR, or a file a closed one left behind), and this run {verb} it"


def _fresh_plan(since: date, source: str, today: date, label: str | None, warnings: list[str]) -> dict[str, Any]:
    return {
        "mode": "fresh" if label else "first",  # fresh: a previous report exists, but its state could not be read
        "since": since,
        "since_source": source,
        "recheck": {},
        "known_orgs": None,
        "seen": {},
        "left_out": {},
        "first_run_since": today - timedelta(days=FIRST_RUN_DAYS),
        "label": label,
        "warnings": warnings,
    }


GIVEN = "given with --since"
REPEATED = "repeated from the replaced report"


def plan(previous: Path | None, since: date | None, redo: bool, today: date) -> dict[str, Any]:
    """The window, the carried releases and the known vendors for this run.

    Following a report: the window opens before its date, and its outputs are
    carried. Replacing one (it is dated today, or `--redo`): its inputs are
    repeated exactly, so whatever it found through a carried release or a
    vendor's first-run window is found again. Its outputs would not do: a
    release it settled is no longer pending, and a vendor it surveyed for the
    first time is no longer new."""
    if previous is None:
        if since:
            return _fresh_plan(since, GIVEN, today, None, [])
        return _fresh_plan(
            today - timedelta(days=FIRST_RUN_DAYS), f"{FIRST_RUN_DAYS} days, a first run's", today, None, []
        )
    state = state_from_report(previous.read_text(encoding="utf-8"))
    if state is None:
        if since is None:
            raise PlanError(
                f"{previous.name} has no state block this tool can read (format {STATE_FORMAT}). "
                "Pass --since to open a fresh window; nothing will be carried."
            )
        warning = f"the state of `{previous.name}` could not be read: nothing was carried and no vendor counts as new"
        return _fresh_plan(since, GIVEN, today, f"`{previous.name}`", [warning])
    repeat = redo or report_date(previous) == today
    inputs, outputs = state["inputs"], state["outputs"]
    if repeat:
        opened = date.fromisoformat(state["since"])
        was = inputs["since_source"]
        source = was if was.startswith(REPEATED) else f"{REPEATED}, where it was {was}"
        recheck, known, seen, left_out = inputs["carried"], inputs["known_orgs"], inputs["seen"], inputs["left_out"]
        first_run_since = date.fromisoformat(inputs["first_run_since"])
    else:
        # The name's date, or for a report passed with --previous under another name, the day its state was written.
        followed = report_date(previous) or date.fromisoformat(state["generated_at"][:10])
        opened, source = followed - timedelta(days=OVERLAP_DAYS), f"{OVERLAP_DAYS} days before the report it follows"
        recheck, known, seen, left_out = pending(state), outputs["vendors"], outputs["examined"], outputs["left_out"]
        first_run_since = today - timedelta(days=FIRST_RUN_DAYS)
    warning = provenance_warning(previous, "replaces" if repeat else "follows")
    return {
        "mode": "replaces" if repeat else "follows",
        "since": since or opened,
        "since_source": GIVEN if since else source,
        "recheck": dict(recheck),
        "known_orgs": {o.lower() for o in known} if known is not None else None,
        "seen": dict(seen),
        "left_out": dict(left_out),
        "first_run_since": first_run_since,
        "label": f"`{previous.name}`" + (", which this run replaces and whose inputs it repeats" if repeat else ""),
        "warnings": [warning] if warning else [],
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

    if args.previous and not args.previous.is_file():
        print(f"--previous {args.previous} is not a file", file=sys.stderr)
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
            policy,
            run["since"],
            hub,
            client,
            recheck=run["recheck"],
            known_orgs=run["known_orgs"],
            seen=run["seen"],
            left_out=run["left_out"],
            first_run_since=run["first_run_since"],
            today=today,
        )
        facts["coverage"] = [*run["warnings"], *facts["coverage"]]
        facts["since_source"] = run["since_source"]
        facts["mode"] = run["mode"]
        authenticated = hub.authenticated
        print(f"{hub.requests} Hub requests", file=sys.stderr)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = render(facts, policy, args.run_id, now, commit_id(), previous=run["label"], authenticated=authenticated)
    try:
        with out.open("x", encoding="utf-8") as f:  # the check above was minutes ago; another run may have written it
            f.write(text)
    except FileExistsError:
        print(f"refusing to overwrite {out}; one file per run", file=sys.stderr)
        return 2
    if args.json:
        args.json.write_text(json.dumps(facts, indent=2, default=str), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
