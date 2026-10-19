"""Compose stores originals on the filesystem backend, on one volume every reader and writer mounts.

Regression test for #888: the default stack ran `minio/minio:latest`, which can
no longer be pulled, so a fresh `docker compose up` failed before Harbor Clerk
started. Compose now uses the filesystem backend (`STORAGE_BACKEND=filesystem`,
`STORAGE_PATH`), as the macOS app does, on the named volume `originals`.

Which services touch stored originals is derived from the code, not listed:
every module under `src/harbor_clerk` that names the storage backend must be
mapped below to the service that runs it (worker stages map through
QUEUE_STAGES, so moving a stage moves the requirement). A module that calls a
writing operation makes its service a writer. Each such service selects the
filesystem backend and mounts the volume at STORAGE_PATH, read-write exactly
where something writes and read-only where it only reads; nothing else mounts
it. MinIO survives only behind the opt-in `minio` profile, with an image the
operator must name.
"""

import copy
import sys
from pathlib import Path

import pytest
import yaml

from harbor_clerk.worker.entry import QUEUE_STAGES
from harbor_clerk.worker.stages import STAGE_FUNCTIONS
from tests.test_docker_compose_queues import COMPOSE_PATH, _load_services, _queues_for_service
from tests.test_docker_compose_watch_mount import _code_names, _resolve, _strict_true

SRC = Path(__file__).resolve().parents[1] / "src" / "harbor_clerk"

VOLUME = "originals"
STORAGE_PATH = "/data/originals"

# Names that reach the storage backend; storage.py itself defines them.
STORAGE_NAMES = {"get_storage", "FilesystemBackend", "MinIOBackend"}
WRITE_OPS = {"put_object", "put_new_object", "copy_object", "copy_and_delete", "remove_object", "ensure_bucket"}

# Shared helpers that reach storage on their callers' behalf (#851): a module that calls
# one of the listed functions reaches storage itself, read-only, and is mapped like any
# other; the helper module is not mapped to a service of its own.
STORAGE_HELPERS = {
    "watched_source.py": {"read_document_bytes"},  # extract and OCR read stored originals through it
}

# Every non-stage module that reaches storage, and the compose service that runs it.
MODULE_SERVICES = {
    "api/app.py": "app",  # startup ensure_bucket, stale-upload reaper
    "api/routes/documents.py": "app",  # download
    "api/routes/system.py": "app",  # health check, storage stats, purge, delete-all
    "api/routes/uploads.py": "app",
    "mcp_server.py": "app",  # mounted on the API at /mcp; health check
    "maintenance/rename_originals.py": "app",  # run at API startup
    "maintenance/copy_minio_originals.py": "app",  # `docker compose run app ...` (#888)
    "mail/ingest.py": "watcher",  # IMAP sync runs in the watcher's mail observer
}


def _storage_modules() -> dict[str, set[str]]:
    """Each module under src/harbor_clerk that names the storage backend, with the names it uses."""
    helper_names = set().union(*STORAGE_HELPERS.values())
    found = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == "storage.py" or rel in STORAGE_HELPERS:
            continue
        names = _code_names(path)
        if names & (STORAGE_NAMES | helper_names):
            found[rel] = names
    return found


def _stage_modules() -> dict[str, set]:
    """Stage module (relative path) -> the stages its functions implement."""
    modules: dict[str, set] = {}
    for stage, function in STAGE_FUNCTIONS.items():
        path = Path(sys.modules[function.__module__].__file__ or "").resolve()
        modules.setdefault(path.relative_to(SRC).as_posix(), set()).add(stage)
    return modules


def _services_for_module(rel: str, services: dict) -> set[str]:
    if rel in MODULE_SERVICES:
        return {MODULE_SERVICES[rel]}
    stages = _stage_modules().get(rel)
    assert stages, (
        f"{rel} reaches the storage backend but is neither a pipeline stage nor in MODULE_SERVICES; "
        "add it with the compose service that runs it"
    )
    queues = {q for q, claimed in QUEUE_STAGES.items() if set(claimed) & stages}
    runners = {name for name, service in services.items() if _queues_for_service(service) & queues}
    assert runners, f"no compose service subscribes to {sorted(queues)}, which claims {rel}'s stage"
    return runners


def _access(services: dict, modules: dict[str, set[str]] | None = None) -> dict[str, bool]:
    """Service -> whether it writes stored originals, for every service that touches them."""
    access: dict[str, bool] = {}
    for rel, names in (_storage_modules() if modules is None else modules).items():
        writes = bool(names & WRITE_OPS)
        for name in _services_for_module(rel, services):
            access[name] = access.get(name, False) or writes
    return access


def _default(services: dict) -> dict:
    return {name: s for name, s in services.items() if not s.get("profiles")}


def _volume_mounts(service: dict) -> dict[str, tuple[str, bool]]:
    """Named-volume mounts: volume -> (target, read_only). Short and long syntax."""
    mounts = {}
    for volume in service.get("volumes") or []:
        if isinstance(volume, dict):
            if volume.get("type") != "volume":
                continue
            mounts[str(volume["source"])] = (str(volume["target"]), _strict_true(volume.get("read_only", False)))
        else:
            parts = str(volume).split(":")
            if len(parts) < 2 or "/" in parts[0] or parts[0].startswith("."):
                continue
            modes = parts[2].split(",") if len(parts) > 2 else []
            mounts[parts[0]] = (parts[1].rstrip("/") or "/", "ro" in modes)
    return mounts


def _targets(service: dict) -> list[str]:
    """Every mount target, bind or volume."""
    out = []
    for volume in service.get("volumes") or []:
        if isinstance(volume, dict):
            out.append(str(volume.get("target", "")))
        else:
            parts = str(volume).split(":")
            if len(parts) >= 2:
                out.append(parts[1])
    return [t.rstrip("/") or "/" for t in out]


def _image(service: dict, env: dict[str, str]) -> str:
    return _resolve(str(service.get("image") or ""), env)


def _is_minio(service: dict) -> bool:
    raw = str(service.get("image") or "")
    return "minio" in raw.lower()


def _problems(services: dict) -> list[str]:
    problems = []
    access = _access(services)
    for name, writes in sorted(access.items()):
        service = services.get(name)
        if service is None:
            problems.append(f"{name} touches stored originals but is not a compose service")
            continue
        env = service.get("environment") or {}
        backend = _resolve(str(env.get("STORAGE_BACKEND", "")), {})
        if backend != "filesystem":
            problems.append(f"{name}: STORAGE_BACKEND resolves to {backend!r}, not 'filesystem'")
        if env.get("STORAGE_PATH") != STORAGE_PATH:
            problems.append(f"{name}: STORAGE_PATH is {env.get('STORAGE_PATH')!r}, not {STORAGE_PATH!r}")
        mount = _volume_mounts(service).get(VOLUME)
        if mount is None or mount[0] != STORAGE_PATH:
            problems.append(f"{name} does not mount the {VOLUME} volume at {STORAGE_PATH}")
        elif mount[1] == writes:
            want = "read-write" if writes else "read-only"
            problems.append(f"{name} must mount {VOLUME} {want}: it {'writes' if writes else 'only reads'}")
    for name, service in sorted(services.items()):
        if name in access:
            continue
        if VOLUME in _volume_mounts(service) or STORAGE_PATH in _targets(service):
            problems.append(f"{name} mounts stored originals but nothing it runs touches them")
    return problems


def test_storage_modules_are_found() -> None:
    """Not vacuous: the scan sees the known readers and writers."""
    modules = _storage_modules()
    assert {"api/routes/uploads.py", "mail/ingest.py", "worker/stages/extract.py", "worker/stages/ocr.py"} <= set(
        modules
    )
    assert set(MODULE_SERVICES) <= set(modules), f"stale MODULE_SERVICES rows: {set(MODULE_SERVICES) - set(modules)}"


def test_expected_access() -> None:
    """The derived roles, pinned so a change in them is a visible decision."""
    access = _access(_load_services())
    assert access == {"app": True, "watcher": True, "worker-io": False, "worker-cpu": False}


def test_every_storage_service_uses_the_filesystem_volume() -> None:
    problems = _problems(_load_services())
    assert not problems, "stored originals (#888):\n  " + "\n  ".join(problems)


def test_originals_volume_is_declared() -> None:
    assert VOLUME in (yaml.safe_load(COMPOSE_PATH.read_text()).get("volumes") or {})


def _minio_problems(services: dict) -> list[str]:
    default = _default(services)
    problems = [f"{name} uses MinIO" for name, service in default.items() if _is_minio(service) or name == "minio"]
    for name, service in sorted(default.items()):
        outside = sorted(set(service.get("depends_on") or []) - set(default))
        if outside:
            problems.append(f"{name} depends on {outside}, outside the default profile")
    return problems


def test_no_default_service_uses_minio() -> None:
    problems = _minio_problems(_load_services())
    assert not problems, "the default stack must not need MinIO, whose image can no longer be pulled:\n  " + (
        "\n  ".join(problems)
    )


def test_minio_profile_image_has_no_default() -> None:
    """The opt-in service pulls nothing unless the operator names an image."""
    minio = {name: s for name, s in _load_services().items() if _is_minio(s)}
    assert list(minio) == ["minio"]
    service = minio["minio"]
    assert service.get("profiles") == ["minio"]
    assert _image(service, {}) == "", f"minio image resolves to {_image(service, {})!r} with MINIO_IMAGE unset"
    assert _image(service, {"MINIO_IMAGE": "registry.example/minio:pinned"}) == "registry.example/minio:pinned"
    assert "minio_data" in _volume_mounts(service), "the profile must reach the existing install's minio_data"


# The checker's blind spots, each pinned on a copy of the real compose file.


def _mutated(name: str, change) -> dict:
    services = copy.deepcopy(_load_services())
    change(services[name])
    return services


def _swap_volume(mount: str):
    def change(service):
        service["volumes"] = [v for v in service["volumes"] if not str(v).startswith(f"{VOLUME}:")] + [mount]

    return change


@pytest.mark.parametrize(
    ("name", "change", "expected"),
    [
        ("watcher", _swap_volume(f"{VOLUME}:{STORAGE_PATH}:ro"), "watcher must mount originals read-write"),
        ("worker-io", _swap_volume(f"{VOLUME}:{STORAGE_PATH}"), "worker-io must mount originals read-only"),
        ("worker-cpu", _swap_volume(f"{VOLUME}:/elsewhere:ro"), "worker-cpu does not mount the originals volume"),
        (
            "worker-cpu",
            lambda s: s["environment"].pop("STORAGE_BACKEND"),
            "worker-cpu: STORAGE_BACKEND resolves to ''",
        ),
        (
            "app",
            lambda s: s["environment"].update(STORAGE_BACKEND="${STORAGE_BACKEND:-minio}"),
            "app: STORAGE_BACKEND resolves to 'minio'",
        ),
        ("worker-llm", lambda s: s.setdefault("volumes", []).append(f"{VOLUME}:{STORAGE_PATH}:ro"), "worker-llm"),
        (
            "worker-io",
            _swap_volume({"type": "volume", "source": VOLUME, "target": STORAGE_PATH, "read_only": "false"}),
            "worker-io must mount originals read-only",
        ),
    ],
)
def test_checker_catches(name, change, expected) -> None:
    problems = _problems(_mutated(name, change))
    assert any(p.startswith(expected) for p in problems), problems


def test_long_syntax_read_only_true_counts() -> None:
    long = {"type": "volume", "source": VOLUME, "target": STORAGE_PATH, "read_only": True}
    assert _problems(_mutated("worker-io", _swap_volume(long))) == []


def test_unmapped_storage_module_fails() -> None:
    modules = {**_storage_modules(), "zz_rogue.py": {"get_storage"}}
    with pytest.raises(AssertionError, match="neither a pipeline stage nor in MODULE_SERVICES"):
        _access(_load_services(), modules)


def test_a_writing_reader_module_makes_its_service_a_writer() -> None:
    modules = _storage_modules()
    modules["worker/stages/ocr.py"] = modules["worker/stages/ocr.py"] | {"put_object"}
    assert _access(_load_services(), modules)["worker-cpu"] is True


def test_comment_mentions_do_not_count(tmp_path) -> None:
    sample = tmp_path / "m.py"
    sample.write_text('"""get_storage put_object"""\n# MinIOBackend\nx = 1\n')
    assert not _code_names(sample) & (STORAGE_NAMES | WRITE_OPS)


@pytest.mark.parametrize(
    ("name", "change", "expected"),
    [
        ("app", lambda s: s["depends_on"].update(minio={"condition": "service_healthy"}), "app depends on ['minio']"),
        ("minio", lambda s: s.pop("profiles"), "minio uses MinIO"),
        ("tika", lambda s: s.update(image="quay.io/minio/minio:latest"), "tika uses MinIO"),
    ],
)
def test_minio_checker_catches(name, change, expected) -> None:
    assert any(p.startswith(expected) for p in _minio_problems(_mutated(name, change)))
