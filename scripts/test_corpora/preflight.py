"""Is this machine fit to be measured on?

A benchmark number is a claim about a model. It is only that if the machine was in a state to run one:
every check here is something that silently turned a measurement into a measurement of something else.

- 2026-09: the mini reported thermal pressure "Sleeping" at idle and held its GPU at the lowest of fifteen
  clock steps while the driver asked for the tenth. Decode ran at a seventh of its speed for weeks, through
  a llama.cpp upgrade and two smoke tests, and `pmset -g therm` recorded nothing (#652).
- The same week, the llama-server inside the installed app was still the build from before the pin moved,
  and a launch agent held a 25 GB model armed on every interface.

    uv run python -m scripts.test_corpora.preflight --json /tmp/preflight.json

Exit 0: fit to measure (warnings are printed and travel with the report). Exit 1: a check failed; a number
taken now is not a baseline. Nothing here changes the machine.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import ipaddress
import json
import os
import platform
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
PASS, WARN, FAIL, SKIPPED = "pass", "warn", "fail", "skipped"
# OSThermalPressureLevel, as `notifyutil -g com.apple.system.thermalpressurelevel` reports it.
THERMAL_LEVELS = {0: "Nominal", 1: "Moderate", 2: "Heavy", 3: "Trapping", 4: "Sleeping"}
# Processes that serve models and are not Harbor Clerk's own.
FOREIGN_SERVERS = re.compile(r"\b(ollama|mlx_lm|mlx-lm|lm-studio|lmstudio|koboldcpp|text-generation|vllm)\b", re.I)
APP_LLAMA_SERVER = Path("/Applications/HarborClerkServer.app/Contents/Resources/llama/llama-server")
MIN_FREE_PERCENT = 50
MAX_IDLE_GPU_PERCENT = 25
GIB = 1024**3

Run = Callable[[list[str]], str | None]


@dataclasses.dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def run_command(argv: list[str]) -> str | None:
    """The command's output, or None when it is missing, fails or hangs. A check that cannot look says so."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout + done.stderr if done.returncode == 0 else None


def pinned_tag() -> str:
    script = (REPO / "macos/scripts/build-llama.sh").read_text()
    return re.search(r'LLAMA_CPP_TAG="\$\{LLAMA_CPP_TAG:-(v[\d.]+)\}"', script).group(1)


def registry() -> dict[str, dict[str, int]]:
    """id -> the memory figures of every curated model, read from the registry's source. The harness is its
    own project and does not import the app; parsing keeps it that way."""
    tree = ast.parse((REPO / "src/harbor_clerk/llm/models.py").read_text())
    models = {}
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "ModelInfo"]
    for node in sorted(calls, key=lambda n: n.lineno):  # the registry's own order
        kw = {k.arg: k.value.value for k in node.keywords if isinstance(k.value, ast.Constant)}
        written = {k.arg for k in node.keywords}
        # Required, or written but not as a literal: read as absent, a KV figure would become 0 and every
        # model would "fit".
        missing = ({"id", "size_bytes"} - set(kw)) | ((written & {"kv_bytes_per_token", "kv_fixed_bytes"}) - set(kw))
        if missing:
            # The harness imports this at collection. Say what is wrong rather than die on a KeyError.
            raise ValueError(
                f"models.py line {node.lineno}: ModelInfo's {sorted(missing)} must be a literal (not an "
                "expression) for the eval harness to read the registry without importing the app"
            )
        models[kw["id"]] = {
            "size_bytes": kw["size_bytes"],
            "kv_bytes_per_token": kw.get("kv_bytes_per_token", 0),
            "kv_fixed_bytes": kw.get("kv_fixed_bytes", 0),
        }
    return models


def _constant(name: str) -> int:
    source = (REPO / "src/harbor_clerk/llm/models.py").read_text()
    return int(re.search(rf"^{name} = ([\d_]+)", source, re.M).group(1).replace("_", ""))


# ── the checks ──


def check_thermal(run: Run) -> Check:
    out = run(["notifyutil", "-g", "com.apple.system.thermalpressurelevel"])
    found = re.search(r"thermalpressurelevel\s+(\d+)", out or "")
    if not found:
        return Check("thermal pressure", SKIPPED, "could not read com.apple.system.thermalpressurelevel")
    level = int(found.group(1))
    name = THERMAL_LEVELS.get(level, str(level))
    if level == 0:
        return Check("thermal pressure", PASS, name)
    return Check(
        "thermal pressure",
        FAIL,
        f"{name} (level {level}): macOS is holding the CPU and GPU down. Every timing taken now is a timing of "
        "the throttle. `sudo powermetrics --samplers thermal,gpu_power -n 1` shows it; see #652.",
    )


def check_power(run: Run) -> list[Check]:
    checks = []
    settings = run(["pmset", "-g"]) or ""
    low = re.search(r"lowpowermode\s+(\d)", settings)
    if low:
        checks.append(
            Check("low power mode", FAIL if low.group(1) == "1" else PASS, "on" if low.group(1) == "1" else "off")
        )
    source = run(["pmset", "-g", "ps"]) or ""
    if "AC Power" in source:
        checks.append(Check("power source", PASS, "AC"))
    elif "Battery Power" in source:
        checks.append(Check("power source", FAIL, "on battery: the machine will not run at full power"))
    return checks or [Check("power", SKIPPED, "pmset not available")]


def check_memory(run: Run) -> list[Check]:
    level = run(["sysctl", "-n", "kern.memorystatus_level"])
    swap = re.search(r"used = ([\d.]+)M", run(["sysctl", "-n", "vm.swapusage"]) or "")
    checks = []
    if level and level.strip().isdigit():
        free = int(level.strip())
        detail = f"{free}%"
        if free < MIN_FREE_PERCENT:
            detail += f" (under {MIN_FREE_PERCENT}%). Largest resident: {_largest_resident(run)}. If that is Harbor "
            detail += "Clerk's own llama-server, deactivate the model first; the sweep activates what it needs."
        checks.append(Check("memory free", PASS if free >= MIN_FREE_PERCENT else FAIL, detail))
    if swap:
        used = float(swap.group(1))
        checks.append(Check("swap in use", PASS if used < 2048 else WARN, f"{used:.0f} MB"))
    return checks or [Check("memory", SKIPPED, "sysctl not available")]


def _largest_resident(run: Run) -> str:
    rows = []
    for line in (run(["ps", "-axo", "pid=,rss=,command="]) or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1].isdigit():
            rows.append((int(parts[1]), parts[0], parts[2]))
    top = sorted(rows, reverse=True)[:3]
    return (
        "; ".join(f"{rss / 1048576:.1f} GB pid {pid} {Path(cmd.split()[0]).name}" for rss, pid, cmd in top) or "unknown"
    )


def check_foreign_servers(run: Run, fetch: Callable[[str], dict | None]) -> list[Check]:
    checks = []
    listing = run(["ps", "-axo", "pid=,rss=,command="]) or ""
    foreign = []
    for line in listing.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and FOREIGN_SERVERS.search(parts[2]) and "preflight" not in parts[2]:
            foreign.append(f"pid {parts[0]} ({int(parts[1]) / 1048576:.1f} GB) {parts[2][:70]}")
    loaded = fetch("http://127.0.0.1:11434/api/ps")
    held = [f"{m.get('name')} ({m.get('size_vram', 0) / 1e9:.1f} GB)" for m in (loaded or {}).get("models", [])]
    if held:
        checks.append(
            Check(
                "other model servers",
                FAIL,
                "Ollama holds " + ", ".join(held) + ": it shares this machine's memory and GPU. Stop it (a launch "
                "agent with KeepAlive needs `launchctl bootout`, not a kill).",
            )
        )
    elif foreign:
        checks.append(
            Check(
                "other model servers",
                WARN,
                "running, no model loaded that this check can see: " + "; ".join(foreign) + ". A request to it "
                "mid-run would load one.",
            )
        )
    else:
        checks.append(Check("other model servers", PASS, "none"))
    return checks


def check_idle_gpu(run: Run) -> Check:
    out = run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"]) or ""
    found = re.search(r'"Device Utilization %"=(\d+)', out)
    if not found:
        return Check("GPU at idle", SKIPPED, "no IOAccelerator utilisation to read")
    busy = int(found.group(1))
    if busy <= MAX_IDLE_GPU_PERCENT:
        return Check("GPU at idle", PASS, f"{busy}% busy")
    return Check(
        "GPU at idle",
        WARN,
        f"{busy}% busy before anything is run: a screensaver, a dynamic wallpaper or a screen-sharing session is "
        "rendering. Set the screensaver to Never and let the display sleep.",
    )


def check_llama_server(run: Run, binary: Path) -> Check:
    tag = pinned_tag()
    if not binary.exists():
        return Check("llama-server at the pin", SKIPPED, f"{binary} not found (pass --llama-server)")
    out = run([str(binary), "--version"]) or ""
    version = next((line.strip() for line in out.splitlines() if line.lower().startswith("version")), "")
    # A build of the pin prints `version: 0.4.1-dev (build 1, commit b29c606)`; the May build printed
    # `version: 1 (c84e6d6)` (both observed on the mini, 2026-09-18). The whole number: 0.4.1 is not 0.4.10.
    if re.search(rf"(?<![\d.]){re.escape(tag.lstrip('v'))}(?![\d.]*\d)", version):
        return Check("llama-server at the pin", PASS, f"{version} matches {tag}")
    return Check(
        "llama-server at the pin",
        FAIL,
        f"{binary} reports {version or 'nothing'!r}, and the pin is {tag}. The installed app predates the pin: "
        "rebuild it (`make apps`) before measuring through it.",
    )


def fits(figures: dict[str, int], ram_bytes: int, *, overhead: int, headroom: int) -> bool:
    """Whether the app would load the model at any context: `max_context(model, ram) > 0` in the registry's
    terms (src/harbor_clerk/llm/models.py). A copy of that arithmetic, because the harness does not import
    the app; tests/test_eval_preflight_parity.py holds the two together."""
    spare = ram_bytes - headroom - overhead - figures["size_bytes"] - figures["kv_fixed_bytes"]
    if spare <= 0:
        return False
    per_token = figures["kv_bytes_per_token"]
    return per_token == 0 or spare // per_token // 1024 * 1024 >= 4096


def check_models_fit(models: list[str], ram_bytes: int | None, *, named: bool = True) -> list[Check]:
    """`named`: the operator asked for these models, so one that cannot load fails the preflight. Checking
    the whole registry by default, a model this machine cannot load is only news: the sweep skips it."""
    known = registry()
    overhead, headroom = _constant("RUNTIME_OVERHEAD_BYTES"), _constant("HOST_HEADROOM_BYTES")
    checks = []
    for model in models:
        if model not in known:
            checks.append(Check(f"fits: {model}", FAIL, "not in the registry (src/harbor_clerk/llm/models.py)"))
        elif not ram_bytes:
            checks.append(Check(f"fits: {model}", SKIPPED, "physical memory unknown"))
        elif fits(known[model], ram_bytes, overhead=overhead, headroom=headroom):
            checks.append(Check(f"fits: {model}", PASS, f"fits in {ram_bytes / GIB:.0f} GiB"))
        else:
            checks.append(
                Check(
                    f"fits: {model}",
                    FAIL if named else WARN,
                    f"does not fit in {ram_bytes / GIB:.0f} GiB; the app will refuse to load it"
                    + ("" if named else " and the sweep will skip it"),
                )
            )
    return checks


def check_instance(fetch: Callable[[str], dict | None], api_base: str) -> Check:
    health = fetch(api_base.rstrip("/") + "/api/system/health")
    if health is None:
        return Check("Harbor Clerk instance", FAIL, f"{api_base} does not answer /api/system/health")
    status = health.get("status")
    return Check(
        "Harbor Clerk instance",
        PASS if status == "healthy" else FAIL,
        f"{status}, build {health.get('build', 'unknown')}",
    )


def fetch_json(url: str) -> dict | None:
    import httpx

    # Compose serves a self-signed certificate on loopback. Anywhere else, the certificate is checked.
    host = urlsplit(url).hostname or ""
    try:
        return_unverified = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return_unverified = False
    try:
        r = httpx.get(url, timeout=5, verify=not return_unverified)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def preflight(
    *,
    models: list[str],
    api_base: str | None,
    llama_server: Path = APP_LLAMA_SERVER,
    models_named: bool = True,
    run: Run = run_command,
    fetch: Callable[[str], dict | None] = fetch_json,
    system: str | None = None,
) -> list[Check]:
    checks: list[Check] = []
    if (system or platform.system()) == "Darwin":
        checks.append(check_thermal(run))
        checks += check_power(run)
        checks += check_memory(run)
        checks.append(check_idle_gpu(run))
        checks.append(check_llama_server(run, llama_server))
        ram = run(["sysctl", "-n", "hw.memsize"])
        ram_bytes = int(ram.strip()) if ram and ram.strip().isdigit() else None
    else:
        checks.append(Check("machine state", SKIPPED, "thermal, power, GPU and memory checks are written for macOS"))
        ram_bytes = None
    checks += check_foreign_servers(run, fetch)
    checks += check_models_fit(models, ram_bytes, named=models_named)
    if api_base:
        checks.append(check_instance(fetch, api_base))
    return checks


def verdict(checks: list[Check]) -> str:
    if any(c.status == FAIL for c in checks):
        return FAIL
    return WARN if any(c.status == WARN for c in checks) else PASS


def as_record(checks: list[Check]) -> dict:
    return {
        "verdict": verdict(checks),
        "host": platform.node(),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pinned_llama_cpp": pinned_tag(),
        "checks": [dataclasses.asdict(c) for c in checks],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--models", default="", help="comma-separated model ids the run will use; default: all curated")
    p.add_argument(
        "--api-base",
        default=os.environ.get("HC_API_BASE", "http://localhost:8100"),
        help="the instance the sweep will measure (default: HC_API_BASE, as the sweep reads it); '' to skip",
    )
    p.add_argument("--llama-server", type=Path, default=APP_LLAMA_SERVER)
    p.add_argument("--json", type=Path, default=None, help="write the record here; the report reads it")
    args = p.parse_args(argv)

    named = [m.strip() for m in args.models.split(",") if m.strip()]
    checks = preflight(
        models=named or list(registry()),
        api_base=args.api_base or None,
        llama_server=args.llama_server,
        models_named=bool(named),
    )
    for c in checks:
        print(f"  {c.status.upper():8s} {c.name}: {c.detail}")
    record = as_record(checks)
    print(f"preflight: {record['verdict']}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(record, indent=2) + "\n")
    return 1 if record["verdict"] == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
