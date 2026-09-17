"""Turn a run's JUnit XML into the release-smoke report under docs/reports/.

The smoke matrix (docs/release-smoke-test.md) has nine areas; each check ID
maps onto one, and an area is `pass` when every check in it passed, `partial`
when some skipped or expected-failed, `fail` when any failed. Areas this tier
does not cover say so. Every `HC_*` value in the environment is redacted from
the text before it is written, so a login failure cannot carry the admin
password into a committed report.

    uv run python -m acceptance.report --junit junit.xml --out docs/reports/ \\
        --api-base http://localhost:8100 --run-id live4 [--instance-build e500116] [--model gpt-oss-20b]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import platform
import re
import socket
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from acceptance.config import _flag

AREAS = [
    "Startup and onboarding",
    "Ingest and Status",
    "Search and Find All",
    "Documents and citations",
    "Ask and Research",
    "MCP and CLI",
    "API key scope and audit",
    "Recovery and backup docs",
    "Release docs and boundaries",
]
# Check-ID prefix -> smoke area (1-based). H1 is a retrieval regression; H2 is folder lifecycle.
PREFIX_AREA = {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6, "g": 7}
CHECK_AREA_OVERRIDES = {"h1": 3, "h2": 2}
NOT_IN_THIS_TIER = {8: "native tier (Status UI, recovery actions, backup docs)", 9: "docs review; manual"}

_ID = re.compile(r"^test_([a-h]\d+[a-z]?)_")  # a1, h1b, ...
_PARAM = re.compile(r"(\[.*\])$")


@dataclass
class Check:
    id: str
    name: str
    outcome: str  # passed | failed | skipped | xfailed | error
    message: str = ""


@dataclass
class Summary:
    checks: list[Check]
    duration_s: float
    counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for c in self.checks:
            self.counts[c.outcome] = self.counts.get(c.outcome, 0) + 1


def parse_junit(path: Path) -> Summary:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    checks: list[Check] = []
    duration = 0.0
    for suite in suites:
        duration += float(suite.get("time", 0) or 0)
        for case in suite.iter("testcase"):
            name = case.get("name", "")
            m = _ID.match(name)
            param = _PARAM.search(name)
            check_id = (m.group(1) + (param.group(1) if param else "")) if m else name
            outcome, message = "passed", ""
            for tag, label in (("failure", "failed"), ("error", "error")):
                node = case.find(tag)
                if node is not None:
                    outcome, message = label, (node.get("message") or node.text or "").strip()
            skipped = case.find("skipped")
            if skipped is not None and outcome == "passed":  # a teardown error on a skipped test stays an error
                outcome = "xfailed" if (skipped.get("type") or "") == "pytest.xfail" else "skipped"
                message = (skipped.get("message") or "").strip()
            checks.append(Check(check_id, name, outcome, message))
    return Summary(checks, duration)


def area_of(check_id: str) -> int | None:
    base = check_id.split("[")[0].rstrip("abcdefghijklmnopqrstuvwxyz")  # h1b -> h1
    if base in CHECK_AREA_OVERRIDES:
        return CHECK_AREA_OVERRIDES[base]
    return PREFIX_AREA.get(base[:1])


UNMAPPED = 0  # pseudo-area for checks whose id maps to no smoke area; never dropped silently


def area_results(summary: Summary) -> dict[int, tuple[str, list[Check]]]:
    by_area: dict[int, list[Check]] = {}
    for c in summary.checks:
        by_area.setdefault(area_of(c.id) or UNMAPPED, []).append(c)
    out: dict[int, tuple[str, list[Check]]] = {}
    for n in [*range(1, len(AREAS) + 1), UNMAPPED]:
        checks = by_area.get(n, [])
        if n == UNMAPPED and not checks:
            continue
        if not checks:
            out[n] = ("not run", [])
        elif any(c.outcome in ("failed", "error") for c in checks):
            out[n] = ("fail", checks)
        elif any(c.outcome in ("skipped", "xfailed") for c in checks):
            out[n] = ("partial", checks)
        else:
            out[n] = ("pass", checks)
    return out


_BOOLEAN_SPELLINGS = {"1", "0", "true", "false", "yes", "no", "on", "off"}


def _is_secret(key: str, value: str) -> bool:
    """Credential-bearing settings, or anything that looks like a credential;
    never flag spellings or plain numbers, which appear in reports legitimately."""
    if not key.startswith("HC_") or len(value) <= 3:
        return False
    if value.lower() in _BOOLEAN_SPELLINGS or value.isdigit():
        return False
    return key in ("HC_USERNAME", "HC_PASSWORD") or any(w in key for w in ("PASSWORD", "TOKEN", "SECRET", "KEY"))


def redact(text: str, env: dict[str, str] | None = None) -> str:
    """Replace every credential-bearing HC_* value wherever it appears."""
    env = os.environ if env is None else env
    for key, value in env.items():
        if _is_secret(key, value):
            text = text.replace(value, "***")
    return text


def _git_describe(repo: Path) -> str:
    """The suite's commit, with `-dirty` when the tree had uncommitted changes:
    a report generated from a dirty tree cites a commit that did not produce it."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "describe", "--always", "--dirty", "--exclude=*"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _machine() -> str:
    if sys.platform == "darwin":
        try:
            model = subprocess.run(["sysctl", "-n", "hw.model"], capture_output=True, text=True).stdout.strip()
            chip = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True
            ).stdout.strip()
            mem = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip())
            return f"{model}, {chip}, {mem // 2**30} GB"
        except (OSError, ValueError):
            pass
    return platform.platform()


def _os_version() -> str:
    if sys.platform == "darwin":
        try:
            return (
                "macOS " + subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True).stdout.strip()
            )
        except OSError:
            pass
    return platform.platform()


def render(
    summary: Summary,
    *,
    api_base: str,
    run_id: str,
    repo: Path,
    instance_build: str | None,
    model: str | None,
    deployment: str,
    wipe: bool = False,
    now: dt.datetime | None = None,
) -> str:
    now = now or dt.datetime.now().astimezone()
    areas = area_results(summary)
    mode = "wipe mode (instance emptied first)" if wipe else "folder-scoped; every search scoped"
    corpus = "acceptance fixtures rendered into one watched folder"
    corpus += "; instance emptied first" if wipe else " (folder-scoped run); instance database as found"
    lines = [
        f"# Acceptance run — {now:%Y-%m-%d} — {socket.gethostname().split('.')[0]}",
        "",
        "- **Suite commit:** `" + _git_describe(repo) + "`",
        f"- **Instance:** {api_base} · build `{instance_build or 'unknown'}` · {deployment}",
        f"- **Machine:** {_machine()} · {_os_version()}",
        f"- **Corpus:** {corpus}",
        f"- **Model under test:** {model or 'none active (Ask checks skipped)'} · judge model: n/a · cloud spend: n/a",
        f"- **Run id:** `{run_id}` · duration {summary.duration_s:.0f} s · "
        + ", ".join(f"{v} {k}" for k, v in sorted(summary.counts.items())),
        f"- **Method:** `uv run pytest acceptance/ -m acceptance`, API tier only; {mode}; fresh captures",
        "",
        "| Area | Result | Checks |",
        "| --- | --- | --- |",
    ]
    for n, name in enumerate(AREAS, start=1):
        result, checks = areas[n]
        if result == "not run":
            note = NOT_IN_THIS_TIER.get(n, "no checks in this tier")
            lines.append(f"| {name} | not run | {note} |")
            continue
        ids = ", ".join(f"{c.id} {_glyph(c.outcome)}" for c in checks)
        lines.append(f"| {name} | {result} | {ids} |")
    if UNMAPPED in areas:
        result, checks = areas[UNMAPPED]
        ids = ", ".join(f"{c.id} {_glyph(c.outcome)}" for c in checks)
        lines.append(f"| Unmapped checks (fix `report.PREFIX_AREA`) | {result} | {ids} |")
    failed = [c for c in summary.checks if c.outcome in ("failed", "error")]
    partial = [c for c in summary.checks if c.outcome in ("skipped", "xfailed")]
    lines += ["", "## Failing checks", ""]
    lines += [
        f"- **{c.id}** `{c.name}`: {c.message.splitlines()[0] if c.message else 'no message'}" for c in failed
    ] or ["- none"]
    lines += ["", "## Skipped or expected to fail", ""]
    lines += [f"- **{c.id}** ({c.outcome}): {c.message or 'no reason recorded'}" for c in partial] or ["- none"]
    lines.append("")
    return redact("\n".join(lines))


def probe_instance(
    api_base: str, build: str | None, model: str | None, deployment: str
) -> tuple[str | None, str | None, str]:
    """Fill what the flags left blank from the running instance. Credentials
    come from the environment the suite itself uses; nothing is printed."""
    from acceptance.hc_client import HarborClerk

    username, password = os.environ.get("HC_USERNAME", ""), os.environ.get("HC_PASSWORD", "")
    with HarborClerk(api_base, verify=not _flag("HC_INSECURE")) as client:
        build = build or client.health().get("build")
        if username and password:
            client.login(username, password)
            status = client.model_status()
            model = model or (status.get("model_id") if status.get("state") == "ready" else None)
            platform_name = client.watch_system().get("platform")
            deployment = {"docker": "Docker Compose", "macos": "macOS native"}.get(
                platform_name, platform_name or deployment
            )
    return build, model, deployment


def verify_clean(api_base: str, run_id: str) -> list[str]:
    """What an acceptance run should have removed and did not, as lines; empty
    means clean. Needs HC_USERNAME/HC_PASSWORD, like --probe."""
    from acceptance.hc_client import HarborClerk

    with HarborClerk(api_base, verify=not _flag("HC_INSECURE")) as client:
        client.login(os.environ["HC_USERNAME"], os.environ["HC_PASSWORD"])
        left: list[str] = []
        left += [
            f"watched folder still registered: {f['path']}"
            for f in client.folder_list()
            if "hc-acceptance" in (f.get("path") or "")
        ]
        keys = client._json("GET", "/api/api-keys")
        left += [
            f"API key still active: {k['name']}" for k in keys if k["name"].startswith("acceptance-") and k["is_active"]
        ]
        convs = client._json("GET", "/api/chat/conversations") or []
        left += [
            f"conversation left: {c.get('title')}" for c in convs if (c.get("title") or "").startswith("acceptance-")
        ]
        left += [
            f"document from a run folder still active: {d['doc_id']}"
            for d in client.list_documents(q="hc-acceptance", limit=20)["items"]
        ]
        return left


def _glyph(outcome: str) -> str:
    return {"passed": "✓", "failed": "✗", "error": "✗", "skipped": "skip", "xfailed": "xfail"}[outcome]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--junit", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path, help="docs/reports/ directory, or a file path ending in .md")
    ap.add_argument("--api-base", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--instance-build")
    ap.add_argument("--model")
    ap.add_argument("--deployment", default="macOS native")
    ap.add_argument(
        "--probe",
        action="store_true",
        help="log in with HC_USERNAME/HC_PASSWORD and read build, active model and platform from the instance",
    )
    ap.add_argument("--wipe", action="store_true", help="the run emptied the instance first (HC_ACCEPTANCE_WIPE=1)")
    ap.add_argument(
        "--verify-clean",
        action="store_true",
        help="log in and list anything the run left: hc-acceptance folders, active acceptance keys, conversations",
    )
    args = ap.parse_args(argv)
    if args.verify_clean:
        leftovers = verify_clean(args.api_base, args.run_id)
        for line in leftovers:
            print(line)
        return 1 if leftovers else 0
    summary = parse_junit(args.junit)
    repo = Path(__file__).resolve().parents[1]
    build, model, deployment = args.instance_build, args.model, args.deployment
    if args.probe:
        build, model, deployment = probe_instance(args.api_base, build, model, deployment)
    text = render(
        summary,
        api_base=args.api_base,
        run_id=args.run_id,
        repo=repo,
        instance_build=build,
        model=model,
        deployment=deployment,
        wipe=args.wipe,
    )
    out = args.out
    if out.suffix != ".md":
        out.mkdir(parents=True, exist_ok=True)
        out = out / f"{dt.date.today():%Y-%m-%d}-acceptance-{socket.gethostname().split('.')[0]}-{args.run_id}.md"
    if out.exists():
        print(f"refusing to overwrite {out}; one file per run", file=sys.stderr)
        return 2
    out.write_text(text, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
