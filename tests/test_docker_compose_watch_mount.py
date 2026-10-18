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
"""

import re

from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.entry import QUEUE_STAGES
from tests.test_docker_compose_queues import _load_services, _queues_for_service

WATCHER_ENTRYPOINT = "harbor-clerk-watcher"

# Stages that read a document's original bytes (worker/stages/extract.py, ocr.py).
BYTE_READING_STAGES = (JobStage.extract, JobStage.ocr)

# Reads source_path for GET /api/docs/{id}/download, and WATCH_ROOT for the watch API.
APP_SERVICE = "app"

_DEFAULTED = re.compile(r"^\$\{(?P<var>[A-Z_][A-Z0-9_]*):?-(?P<default>[^}]*)\}$")


def _resolve(value: str) -> str:
    """What compose renders for `${VAR:-default}` with VAR unset; a literal passes through."""
    match = _DEFAULTED.match(value)
    return match.group("default") if match else value


def _bind_mounts(service: dict) -> dict[tuple[str, str], bool]:
    """Map each host bind mount (source, target) to whether it is read-only.

    Handles the short syntax (`src:dst[:mode]`) and the long syntax
    (`{type: bind, source, target, read_only}`). Named volumes are skipped:
    their source has no path separator and is not a host directory.
    """
    mounts: dict[tuple[str, str], bool] = {}
    for volume in service.get("volumes") or []:
        if isinstance(volume, dict):
            if volume.get("type") != "bind":
                continue
            source, target = str(volume["source"]), str(volume["target"])
            read_only = bool(volume.get("read_only", False))
        else:
            parts = str(volume).split(":")
            if len(parts) < 2 or "/" not in parts[0]:
                continue
            source, target = parts[0], parts[1]
            modes = parts[2].split(",") if len(parts) > 2 else []
            read_only = "ro" in modes
        mounts[(source, target)] = read_only
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


def _watch_root() -> str:
    watcher_name, watcher = _watcher()
    raw = (watcher.get("environment") or {}).get("WATCH_ROOT")
    assert raw, f"{watcher_name} does not set WATCH_ROOT"
    return _resolve(str(raw))


def _watch_binds() -> set[tuple[str, str]]:
    """The watcher's binds of WATCH_ROOT or a path under it."""
    watcher_name, watcher = _watcher()
    root = _watch_root()
    binds = {(s, t) for s, t in _bind_mounts(watcher) if _under(t, root)}
    assert any(t.rstrip("/") == root.rstrip("/") for _, t in binds), (
        f"{watcher_name} does not bind anything at WATCH_ROOT ({root}); its binds: {sorted(_bind_mounts(watcher))}"
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


def test_document_readers_mount_the_watch_root_read_only() -> None:
    root = _watch_root()
    watch_binds = _watch_binds()
    services = _load_services()
    problems = []
    for name in sorted(_reader_services()):
        mounts = _bind_mounts(services[name])
        if not any(t.rstrip("/") == root.rstrip("/") for _, t in mounts):
            problems.append(f"{name} binds nothing at WATCH_ROOT ({root})")
        for source, target in sorted(watch_binds):
            if (source, target) not in mounts:
                problems.append(f"{name} does not mount {source}:{target}")
    assert not problems, (
        "Watched documents are read in place from the path the watcher recorded, so every "
        "service that reads document bytes must mount the watcher's host directory at the same "
        "container path:\n  " + "\n  ".join(problems)
    )


def test_every_bind_of_the_watch_root_is_read_only() -> None:
    """Sources are never modified; a read-only mount enforces it, the watcher included."""
    root = _watch_root()
    watch_sources = {s for s, _ in _watch_binds()}
    writable = [
        f"{name}: {source}:{target}"
        for name, service in sorted(_load_services().items())
        for (source, target), read_only in sorted(_bind_mounts(service).items())
        if (_under(target, root) or source in watch_sources) and not read_only
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
