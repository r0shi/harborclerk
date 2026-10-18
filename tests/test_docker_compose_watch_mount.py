"""Every service that reads a watched document's bytes must see the watcher's files.

Regression test for #869. Watched-folder documents have no stored original:
the watcher records the path it saw (`source_path`, e.g. `/data/watch/x/a.pdf`)
and extract, OCR and the download route read that path in place. Only the
`watcher` service mounted the watch root, so under Compose every watched
document failed extraction ("No source for doc ...") and could not be
downloaded. Nothing failed at startup, so it needs a static check.

The readers are `app` (the download route) and every service subscribed to a
queue that claims extract or OCR. Workers claim by QUEUE_STAGES
(`claim_next_job` in worker/entry.py), so that is what the readers are derived
from; moving a stage to another queue moves the requirement with it.

Sources are never modified (AGENTS.md), so every bind of the watch root, the
watcher's included, is read-only.

WATCH_ROOT is configurable (#871): the bind target is taken from it, so every
check runs both with it unset (compose's default) and set to another path.
"""

import io
import re
import sys
import tokenize
from pathlib import Path

import pytest

from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.entry import QUEUE_STAGES
from harbor_clerk.worker.stages import STAGE_FUNCTIONS
from tests.test_docker_compose_queues import _load_services, _queues_for_service

WATCHER_ENTRYPOINT = "harbor-clerk-watcher"

# Stages that read a document's original bytes (worker/stages/extract.py, ocr.py).
BYTE_READING_STAGES = (JobStage.extract, JobStage.ocr)

# Reads source_path for GET /api/docs/{id}/download, and WATCH_ROOT for the watch API.
APP_SERVICE = "app"

# Compose's environment for each run: WATCH_ROOT unset, and set to a non-default path.
ENVIRONMENTS = [
    pytest.param({}, "/data/watch", id="WATCH_ROOT-unset"),
    pytest.param({"WATCH_ROOT": "/srv/docs"}, "/srv/docs", id="WATCH_ROOT=/srv/docs"),
]

_VARIABLE = re.compile(r"\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<op>:?-)(?P<default>[^}]*))?\}")


def _resolve(value: str, env: dict[str, str]) -> str:
    """Interpolate `${VAR}`, `${VAR:-default}` and `${VAR-default}` as compose does."""

    def substitute(match: re.Match) -> str:
        var, op, default = match.group("var"), match.group("op"), match.group("default") or ""
        if op == ":-":
            return env.get(var) or default
        if op == "-":
            return env.get(var, default)
        return env.get(var, "")

    return _VARIABLE.sub(substitute, value)


def _bind_mounts(service: dict, env: dict[str, str]) -> dict[tuple[str, str], bool]:
    """Map each host bind mount (source, target) to whether it is read-only.

    Handles the short syntax (`src:dst[:mode]`, interpolated before it is split,
    since `${VAR:-default}` contains a colon) and the long syntax
    (`{type: bind, source, target, read_only}`). Named volumes are skipped:
    their source has no path separator and is not a host directory.
    """
    mounts: dict[tuple[str, str], bool] = {}
    for volume in service.get("volumes") or []:
        if isinstance(volume, dict):
            if volume.get("type") != "bind":
                continue
            source, target = _resolve(str(volume["source"]), env), _resolve(str(volume["target"]), env)
            read_only = bool(volume.get("read_only", False))
        else:
            parts = _resolve(str(volume), env).split(":")
            if len(parts) < 2 or "/" not in parts[0]:
                continue
            source, target = parts[0], parts[1]
            modes = parts[2].split(",") if len(parts) > 2 else []
            read_only = "ro" in modes
        mounts[(source, target.rstrip("/") or "/")] = read_only
    return mounts


def _under(target: str, root: str) -> bool:
    target, root = target.rstrip("/"), root.rstrip("/")
    return target == root or target.startswith(root + "/")


def _watcher() -> tuple[str, dict]:
    watchers = [
        (name, service)
        for name, service in _load_services().items()
        if WATCHER_ENTRYPOINT in " ".join(map(str, service.get("command") or []))
    ]
    assert len(watchers) == 1, f"expected exactly one {WATCHER_ENTRYPOINT} service, found {[n for n, _ in watchers]}"
    return watchers[0]


def _watch_root(env: dict[str, str]) -> str:
    watcher_name, watcher = _watcher()
    raw = (watcher.get("environment") or {}).get("WATCH_ROOT")
    assert raw, f"{watcher_name} does not set WATCH_ROOT"
    return _resolve(str(raw), env).rstrip("/")


def _watch_binds(env: dict[str, str]) -> set[tuple[str, str]]:
    """The watcher's binds of WATCH_ROOT or a path under it."""
    watcher_name, watcher = _watcher()
    root = _watch_root(env)
    binds = {(s, t) for s, t in _bind_mounts(watcher, env) if _under(t, root)}
    assert any(t == root for _, t in binds), (
        f"{watcher_name} binds nothing at WATCH_ROOT ({root}); its binds: {sorted(_bind_mounts(watcher, env))}"
    )
    return binds


def _reader_services() -> set[str]:
    reading_queues = {q for q, stages in QUEUE_STAGES.items() if set(stages) & set(BYTE_READING_STAGES)}
    claimed = {s for q in reading_queues for s in QUEUE_STAGES[q]}
    assert set(BYTE_READING_STAGES) <= claimed, (
        f"no queue in QUEUE_STAGES claims {sorted(s.value for s in set(BYTE_READING_STAGES) - claimed)}"
    )
    services = _load_services()
    readers = {name for name, service in services.items() if _queues_for_service(service) & reading_queues}
    # Not vacuous: a reading queue nobody subscribes to would leave nothing to check.
    covered = set().union(*(_queues_for_service(services[name]) for name in readers), set())
    assert reading_queues <= covered, f"no compose service subscribes to queue(s) {sorted(reading_queues - covered)}"
    return readers | {APP_SERVICE}


@pytest.mark.parametrize(("env", "expected_root"), ENVIRONMENTS)
def test_watch_root_follows_the_variable(env: dict[str, str], expected_root: str) -> None:
    """The watcher's WATCH_ROOT, and so every bind target, is the variable's value (#871)."""
    assert _watch_root(env) == expected_root


@pytest.mark.parametrize(("env", "expected_root"), ENVIRONMENTS)
def test_document_readers_bind_the_watchers_mounts_at_the_same_path(env: dict[str, str], expected_root: str) -> None:
    """Presence and path only; read-only is checked by test_every_bind_of_the_watch_root_is_read_only."""
    watch_binds = _watch_binds(env)
    services = _load_services()
    problems = []
    for name in sorted(_reader_services()):
        mounts = _bind_mounts(services[name], env)
        if not any(t == expected_root for _, t in mounts):
            problems.append(f"{name} binds nothing at WATCH_ROOT ({expected_root})")
        for source, target in sorted(watch_binds):
            if (source, target) not in mounts:
                problems.append(f"{name} does not mount {source}:{target}")
    assert not problems, (
        "Watched documents are read in place from the path the watcher recorded, so every "
        "service that reads document bytes must mount the watcher's host directory at the same "
        "container path:\n  " + "\n  ".join(problems)
    )


@pytest.mark.parametrize(("env", "expected_root"), ENVIRONMENTS)
def test_every_bind_of_the_watch_root_is_read_only(env: dict[str, str], expected_root: str) -> None:
    """Sources are never modified; a read-only mount enforces it, the watcher included."""
    watch_sources = {s for s, _ in _watch_binds(env)}
    writable = [
        f"{name}: {source}:{target}"
        for name, service in sorted(_load_services().items())
        for (source, target), read_only in sorted(_bind_mounts(service, env).items())
        if (_under(target, expected_root) or source in watch_sources) and not read_only
    ]
    assert not writable, "bind(s) of the watch root are writable; they must be :ro:\n  " + "\n  ".join(writable)


def test_watch_root_env_matches_the_watcher() -> None:
    """The app's watch API reads WATCH_ROOT (Docker vs native picker, folder confinement)."""
    watcher_name, watcher = _watcher()
    watcher_root = (watcher.get("environment") or {}).get("WATCH_ROOT")
    services = _load_services()
    assert "WATCH_ROOT" in (services[APP_SERVICE].get("environment") or {}), (
        f"{APP_SERVICE} does not set WATCH_ROOT: the app would offer the native folder picker on Docker"
    )
    mismatched = {
        name: env["WATCH_ROOT"]
        for name, service in services.items()
        if "WATCH_ROOT" in (env := service.get("environment") or {}) and env["WATCH_ROOT"] != watcher_root
    }
    assert not mismatched, f"WATCH_ROOT differs from {watcher_name}'s {watcher_root!r}: {mismatched}"


def _code_names(path: Path) -> set[str]:
    """Identifier tokens in a module: comments and strings (docstrings included) don't count."""
    tokens = tokenize.generate_tokens(io.StringIO(path.read_text()).readline)
    return {tok.string for tok in tokens if tok.type == tokenize.NAME}


def test_stages_that_touch_source_path_are_byte_readers() -> None:
    """A stage that reads `source_path` reads the watched file, so it needs the mount.

    Keyed on the identifier in the stage's own module (comments and strings
    stripped). A read through a helper in another module is not seen: when such
    a helper exists, add its name to the token set below.
    """
    offenders = []
    for stage, function in STAGE_FUNCTIONS.items():
        module_file = Path(sys.modules[function.__module__].__file__ or "")
        if _code_names(module_file) & {"source_path"} and stage not in BYTE_READING_STAGES:
            offenders.append(f"{stage.value} ({module_file.name})")
    assert not offenders, (
        f"stage(s) {offenders} read source_path but are not in BYTE_READING_STAGES, so nothing "
        "checks that their workers mount the watch root"
    )
