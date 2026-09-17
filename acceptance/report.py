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

_ID = re.compile(r"^test_([a-h]\d+)_")


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
            check_id = m.group(1) if m else name
            outcome, message = "passed", ""
            for tag, label in (("failure", "failed"), ("error", "error")):
                node = case.find(tag)
                if node is not None:
                    outcome, message = label, (node.get("message") or node.text or "").strip()
            skipped = case.find("skipped")
            if skipped is not None:
                outcome = "xfailed" if (skipped.get("type") or "") == "pytest.xfail" else "skipped"
                message = (skipped.get("message") or "").strip()
            checks.append(Check(check_id, name, outcome, message))
    return Summary(checks, duration)


def area_of(check_id: str) -> int | None:
    base = check_id.split("[")[0]
    if base in CHECK_AREA_OVERRIDES:
        return CHECK_AREA_OVERRIDES[base]
    return PREFIX_AREA.get(base[:1])


def area_results(summary: Summary) -> dict[int, tuple[str, list[Check]]]:
    by_area: dict[int, list[Check]] = {}
    for c in summary.checks:
        area = area_of(c.id)
        if area is not None:
            by_area.setdefault(area, []).append(c)
    out: dict[int, tuple[str, list[Check]]] = {}
    for n in range(1, len(AREAS) + 1):
        checks = by_area.get(n, [])
        if not checks:
            out[n] = ("not run", [])
        elif any(c.outcome in ("failed", "error") for c in checks):
            out[n] = ("fail", checks)
        elif any(c.outcome in ("skipped", "xfailed") for c in checks):
            out[n] = ("partial", checks)
        else:
            out[n] = ("pass", checks)
    return out


def redact(text: str, env: dict[str, str] | None = None) -> str:
    """Replace every non-trivial HC_* environment value wherever it appears."""
    env = os.environ if env is None else env
    for key, value in env.items():
        if key.startswith("HC_") and len(value) > 3 and key not in ("HC_API_BASE", "HC_ACCEPTANCE_RUN_ID"):
            text = text.replace(value, "***")
    return text


def _git_short_head(repo: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], check=True, capture_output=True, text=True
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
    now: dt.datetime | None = None,
) -> str:
    now = now or dt.datetime.now().astimezone()
    areas = area_results(summary)
    lines = [
        f"# Acceptance run — {now:%Y-%m-%d} — {socket.gethostname().split('.')[0]}",
        "",
        "- **Suite commit:** `" + _git_short_head(repo) + "`",
        f"- **Instance:** {api_base} · build `{instance_build or 'unknown'}` · {deployment}",
        f"- **Machine:** {_machine()} · {_os_version()}",
        "- **Corpus:** acceptance fixtures rendered into one watched folder (folder-scoped run); instance database as found",
        f"- **Model under test:** {model or 'none active (Ask checks skipped)'} · judge model: n/a · cloud spend: n/a",
        f"- **Run id:** `{run_id}` · duration {summary.duration_s:.0f} s · "
        + ", ".join(f"{v} {k}" for k, v in sorted(summary.counts.items())),
        "- **Method:** `uv run pytest acceptance/ -m acceptance`, API tier only; every search folder-scoped; fresh captures",
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
    with HarborClerk(api_base, verify=os.environ.get("HC_INSECURE", "") != "1") as client:
        build = build or client.health().get("build")
        if username and password:
            client.login(username, password)
            status = client.model_status()
            model = model or (status.get("model_id") if status.get("state") == "ready" else None)
            platform_name = client.watch_system().get("platform")
            if platform_name == "docker":
                deployment = "Docker Compose"
    return build, model, deployment


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
    args = ap.parse_args(argv)
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
    )
    out = args.out
    if out.suffix != ".md":
        out.mkdir(parents=True, exist_ok=True)
        out = out / f"{dt.date.today():%Y-%m-%d}-acceptance-{socket.gethostname().split('.')[0]}.md"
    out.write_text(text, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
