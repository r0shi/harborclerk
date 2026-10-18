"""Every service that reads a watched document's bytes must see the watcher's files.

Regression test for #869. Watched-folder documents have no stored original:
the watcher records the path it saw (`source_path`, e.g. `/data/watch/x/a.pdf`)
and extract, OCR and the download route read that path in place. Only the
`watcher` service mounted the watch root, so under Compose every watched
document failed extraction ("No source for doc ...") and could not be
downloaded. Nothing failed at startup, so it needs a static check.

The readers are `app` (the download route) and every service subscribed to a
queue that runs extract or OCR. The stage-to-queue mapping is read from
STAGE_CONFIG, so moving a stage to another queue moves the requirement with it.
"""

from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.pipeline import STAGE_CONFIG
from tests.test_docker_compose_queues import _load_services, _queues_for_service

WATCHER_ENTRYPOINT = "harbor-clerk-watcher"

# Stages that read a document's original bytes (worker/stages/extract.py, ocr.py).
BYTE_READING_STAGES = (JobStage.extract, JobStage.ocr)

# Reads source_path for GET /api/docs/{id}/download, and WATCH_ROOT for the watch API.
APP_SERVICE = "app"


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


def _watcher() -> tuple[str, dict]:
    watchers = [
        (name, service)
        for name, service in _load_services().items()
        if WATCHER_ENTRYPOINT in " ".join(map(str, service.get("command") or []))
    ]
    assert len(watchers) == 1, f"expected exactly one {WATCHER_ENTRYPOINT} service, found {[n for n, _ in watchers]}"
    return watchers[0]


def _reader_services() -> set[str]:
    reading_queues = {STAGE_CONFIG[stage][0] for stage in BYTE_READING_STAGES}
    readers = {name for name, service in _load_services().items() if _queues_for_service(service) & reading_queues}
    # Not vacuous: a queue nobody subscribes to would leave a stage with no reader to check.
    covered = set().union(*(_queues_for_service(_load_services()[name]) for name in readers), set())
    assert reading_queues <= covered, (
        f"no compose service subscribes to queue(s) {sorted(reading_queues - covered)}, "
        f"which run {[s.value for s in BYTE_READING_STAGES]}"
    )
    return readers | {APP_SERVICE}


def test_document_readers_mount_the_watch_root_read_only() -> None:
    watcher_name, watcher = _watcher()
    watch_mounts = set(_bind_mounts(watcher))
    assert watch_mounts, f"{watcher_name} has no host bind mount; nothing to hold the readers to"

    services = _load_services()
    problems = []
    for name in sorted(_reader_services()):
        mounts = _bind_mounts(services[name])
        for source, target in sorted(watch_mounts):
            if (source, target) not in mounts:
                problems.append(f"{name} does not mount {source}:{target}")
            elif not mounts[(source, target)]:
                problems.append(f"{name} mounts {source}:{target} writable; it must be :ro")
    assert not problems, (
        "Watched documents are read in place from the path the watcher recorded, so every "
        "service that reads document bytes must mount the watcher's host directory at the same "
        "container path, read-only (sources are never modified):\n  " + "\n  ".join(problems)
    )


def test_app_watch_root_matches_the_watcher() -> None:
    """The watch API reads WATCH_ROOT (Docker vs native picker, folder confinement)."""
    watcher_name, watcher = _watcher()
    watcher_root = (watcher.get("environment") or {}).get("WATCH_ROOT")
    assert watcher_root, f"{watcher_name} does not set WATCH_ROOT"
    app_root = (_load_services()[APP_SERVICE].get("environment") or {}).get("WATCH_ROOT")
    assert app_root == watcher_root, (
        f"{APP_SERVICE} sets WATCH_ROOT={app_root!r}, the watcher {watcher_root!r}. "
        "The app would offer the native folder picker on Docker, or confine folders to the wrong root."
    )
