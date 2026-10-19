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

Sources are never modified (AGENTS.md), so every bind of the watch directory,
the watcher's included, is read-only, and so is any bind of a directory that
contains it.

WATCH_ROOT (#871) is the discovery path, not the mount target: the target is
fixed at /data/watch, and WATCH_ROOT must be that directory or one under it.
Taking the target from WATCH_ROOT moved every source_path on an upgrade
(`WATCH_ROOT=/data/watch/library` re-ingested everything), could shadow
another volume (`/models`) or the image's WORKDIR (`/app`). Every check runs
with WATCH_ROOT unset and set to a directory under the mount.
"""

import copy
import io
import posixpath
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

# Compose's environment for each run: WATCH_ROOT unset, and set to a directory under the mount.
UNSET = {}
LIBRARY = {"WATCH_ROOT": "/data/watch/library"}
ENVIRONMENTS = [
    pytest.param(UNSET, "/data/watch", id="WATCH_ROOT-unset"),
    pytest.param(LIBRARY, "/data/watch/library", id="WATCH_ROOT=/data/watch/library"),
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


def _norm(path: str) -> str:
    """`./data/watch/` and `./data/watch` are the same source; `/data/watch/` the same target."""
    return posixpath.normpath(path)


def _strict_true(value: object) -> bool:
    """Compose's `read_only`: YAML true or the string "true". `bool("false")` is True, so never bool()."""
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _bind_mounts(service: dict, env: dict[str, str]) -> dict[tuple[str, str], bool]:
    """Map each host bind mount (source, target), normalised, to whether it is read-only.

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
            read_only = _strict_true(volume.get("read_only", False))
        else:
            parts = _resolve(str(volume), env).split(":")
            if len(parts) < 2 or "/" not in parts[0]:
                continue
            source, target = parts[0], parts[1]
            modes = parts[2].split(",") if len(parts) > 2 else []
            read_only = "ro" in modes
        mounts[(_norm(source), _norm(target))] = read_only
    return mounts


def _under(path: str, root: str) -> bool:
    """`path` is `root` or inside it."""
    path, root = _norm(path), _norm(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def _overlaps(a: str, b: str) -> bool:
    return _under(a, b) or _under(b, a)


def _watcher(services: dict) -> tuple[str, dict]:
    watchers = [
        (name, service)
        for name, service in services.items()
        if WATCHER_ENTRYPOINT in " ".join(map(str, service.get("command") or []))
    ]
    assert len(watchers) == 1, f"expected exactly one {WATCHER_ENTRYPOINT} service, found {[n for n, _ in watchers]}"
    return watchers[0]


def _watch_root(services: dict, env: dict[str, str]) -> str:
    watcher_name, watcher = _watcher(services)
    raw = (watcher.get("environment") or {}).get("WATCH_ROOT")
    assert raw, f"{watcher_name} does not set WATCH_ROOT"
    return _norm(_resolve(str(raw), env))


def _watch_mount(services: dict, env: dict[str, str]) -> tuple[str, str]:
    """The watcher's bind that holds WATCH_ROOT: its target is WATCH_ROOT or the nearest ancestor."""
    watcher_name, watcher = _watcher(services)
    root = _watch_root(services, env)
    holding = [(s, t) for s, t in _bind_mounts(watcher, env) if _under(root, t)]
    assert holding, (
        f"{watcher_name} has no bind at WATCH_ROOT ({root}) or above it; WATCH_ROOT must be the watch mount "
        f"or a directory under it. Its binds: {sorted(_bind_mounts(watcher, env))}"
    )
    return max(holding, key=lambda bind: len(bind[1]))


def _watch_binds(services: dict, env: dict[str, str]) -> set[tuple[str, str]]:
    """The watcher's binds a reader needs: the one holding WATCH_ROOT, and any nested under WATCH_ROOT."""
    _, watcher = _watcher(services)
    root = _watch_root(services, env)
    nested = {(s, t) for s, t in _bind_mounts(watcher, env) if _under(t, root)}
    return nested | {_watch_mount(services, env)}


def _reader_services(services: dict) -> set[str]:
    reading_queues = {q for q, stages in QUEUE_STAGES.items() if set(stages) & set(BYTE_READING_STAGES)}
    claimed = {s for q in reading_queues for s in QUEUE_STAGES[q]}
    assert set(BYTE_READING_STAGES) <= claimed, (
        f"no queue in QUEUE_STAGES claims {sorted(s.value for s in set(BYTE_READING_STAGES) - claimed)}"
    )
    readers = {name for name, service in services.items() if _queues_for_service(service) & reading_queues}
    # Not vacuous: a reading queue nobody subscribes to would leave nothing to check.
    covered = set().union(*(_queues_for_service(services[name]) for name in readers), set())
    assert reading_queues <= covered, f"no compose service subscribes to queue(s) {sorted(reading_queues - covered)}"
    return readers | {APP_SERVICE}


def _missing_reader_binds(services: dict, env: dict[str, str]) -> list[str]:
    watch_binds = _watch_binds(services, env)
    problems = []
    for name in sorted(_reader_services(services)):
        mounts = _bind_mounts(services[name], env)
        for source, target in sorted(watch_binds):
            if (source, target) not in mounts:
                problems.append(f"{name} does not mount {source}:{target}")
    return problems


def _writable_watch_binds(services: dict, env: dict[str, str]) -> list[str]:
    """Writable binds of the watch directory, of anything inside it, or of anything containing it."""
    source, target = _watch_mount(services, env)
    return [
        f"{name}: {s}:{t}"
        for name, service in sorted(services.items())
        for (s, t), read_only in sorted(_bind_mounts(service, env).items())
        if not read_only and (_overlaps(s, source) or _overlaps(t, target))
    ]


@pytest.mark.parametrize(("env", "expected_root"), ENVIRONMENTS)
def test_watch_root_is_the_mount_or_under_it(env: dict[str, str], expected_root: str) -> None:
    services = _load_services()
    assert _watch_root(services, env) == expected_root
    _, target = _watch_mount(services, env)
    assert _under(expected_root, target)


def test_watch_mount_target_does_not_follow_watch_root() -> None:
    """The target is fixed: a WATCH_ROOT under it must not move every source_path (#871)."""
    services = _load_services()
    assert _watch_mount(services, UNSET) == _watch_mount(services, LIBRARY)
    assert _watch_mount(services, UNSET)[1] == "/data/watch"


@pytest.mark.parametrize(("env", "expected_root"), ENVIRONMENTS)
def test_document_readers_bind_the_watchers_mounts_at_the_same_path(env: dict[str, str], expected_root: str) -> None:
    """Presence and path only; read-only is checked by test_every_bind_of_the_watch_dir_is_read_only."""
    problems = _missing_reader_binds(_load_services(), env)
    assert not problems, (
        "Watched documents are read in place from the path the watcher recorded, so every "
        "service that reads document bytes must mount the watcher's host directory at the same "
        "container path:\n  " + "\n  ".join(problems)
    )


@pytest.mark.parametrize(("env", "expected_root"), ENVIRONMENTS)
def test_every_bind_of_the_watch_dir_is_read_only(env: dict[str, str], expected_root: str) -> None:
    """Sources are never modified; a read-only mount enforces it, the watcher included."""
    writable = _writable_watch_binds(_load_services(), env)
    assert not writable, "bind(s) of the watch directory are writable; they must be :ro:\n  " + "\n  ".join(writable)


# The parser's blind spots, each pinned on a copy of the real compose file.


def _services_with(name: str, volumes: list) -> dict:
    services = copy.deepcopy(_load_services())
    services[name]["volumes"] = volumes
    return services


def test_long_syntax_read_only_string_false_is_writable() -> None:
    services = _services_with(
        APP_SERVICE, [{"type": "bind", "source": "./data/watch", "target": "/data/watch", "read_only": "false"}]
    )
    assert _writable_watch_binds(services, UNSET) == ["app: data/watch:/data/watch"]


def test_long_syntax_read_only_true_counts() -> None:
    for value in (True, "true"):
        services = _services_with(
            APP_SERVICE, [{"type": "bind", "source": "./data/watch", "target": "/data/watch", "read_only": value}]
        )
        assert _writable_watch_binds(services, UNSET) == []
        assert _missing_reader_binds(services, UNSET) == []


def test_writable_bind_of_an_ancestor_fails() -> None:
    """`./data:/data` exposes the watch directory writable at /data/watch on any service."""
    services = _services_with("worker-llm", ["./data:/data"])
    assert _writable_watch_binds(services, UNSET) == ["worker-llm: data:/data"]


def test_writable_bind_inside_the_watch_target_fails() -> None:
    services = _services_with("worker-llm", ["/srv/elsewhere:/data/watch/extra"])
    assert _writable_watch_binds(services, UNSET) == ["worker-llm: /srv/elsewhere:/data/watch/extra"]


def test_trailing_slash_in_the_source_is_the_same_bind() -> None:
    services = _services_with("worker-io", ["./data/watch/:/data/watch/:ro"])
    assert _missing_reader_binds(services, UNSET) == []
    assert _writable_watch_binds(services, UNSET) == []


def test_watch_root_env_matches_the_watcher() -> None:
    """The app reads WATCH_ROOT: Docker vs native picker (POST /folders is 409 on Docker) and its startup check."""
    services = _load_services()
    watcher_name, watcher = _watcher(services)
    watcher_root = (watcher.get("environment") or {}).get("WATCH_ROOT")
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
