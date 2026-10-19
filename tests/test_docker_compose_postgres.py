"""Postgres's PGDATA is the data volume's mount target (#894).

The pg18+ postgres images default PGDATA to /var/lib/postgresql/18/docker and
refuse to start when a volume is mounted at the old /var/lib/postgresql/data
with PGDATA unset ("unused mount/volume"). The floating `pg18` tag picked that
check up, so a fresh `docker compose up` never got a healthy database.

PGDATA is pinned to the mount rather than the mount moved: existing installs
have their cluster at the root of `pg_data`, mounted at /var/lib/postgresql/data,
and moving the mount would hide it behind a new, empty cluster.
"""

import copy

import pytest

from tests.test_docker_compose_queues import _load_services
from tests.test_docker_compose_storage import _volume_mounts
from tests.test_docker_compose_watch_mount import _resolve

SERVICE = "postgres"
VOLUME = "pg_data"
# Where every install to date has its cluster; see the module docstring.
DATA_DIR = "/var/lib/postgresql/data"


def _problems(services: dict) -> list[str]:
    service = services[SERVICE]
    problems = []
    mount = _volume_mounts(service).get(VOLUME)
    if mount is None:
        return [f"{SERVICE} does not mount the {VOLUME} volume"]
    target, read_only = mount
    if target != DATA_DIR:
        problems.append(f"{VOLUME} is mounted at {target}, not {DATA_DIR}: existing clusters would not be found")
    if read_only:
        problems.append(f"{VOLUME} is mounted read-only")
    raw = (service.get("environment") or {}).get("PGDATA")
    pgdata = _resolve(str(raw), {}).rstrip("/") if raw is not None else None
    if pgdata != target:
        problems.append(f"PGDATA is {pgdata!r}, not the {VOLUME} mount target {target!r}")
    return problems


def test_pgdata_is_the_data_volume_mount() -> None:
    problems = _problems(_load_services())
    assert not problems, "postgres (#894):\n  " + "\n  ".join(problems)


def test_postgres_service_is_the_image_it_describes() -> None:
    """Not vacuous: the check is on the service that runs the pg18+ image."""
    image = str(_load_services()[SERVICE].get("image", ""))
    assert "postgres" in image or "pgvector" in image, image


def _mutated(change) -> dict:
    services = copy.deepcopy(_load_services())
    change(services[SERVICE])
    return services


def _set_volumes(*volumes):
    def change(service):
        service["volumes"] = [v for v in service["volumes"] if not str(v).startswith(f"{VOLUME}:")] + list(volumes)

    return change


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda s: s["environment"].pop("PGDATA"), "PGDATA is None"),
        (
            lambda s: s["environment"].update(PGDATA="/var/lib/postgresql/18/docker"),
            "PGDATA is '/var/lib/postgresql/18",
        ),
        (lambda s: s["environment"].update(PGDATA="${PGDATA:-}"), "PGDATA is ''"),
        (_set_volumes(f"{VOLUME}:/var/lib/postgresql"), f"{VOLUME} is mounted at /var/lib/postgresql,"),
        (_set_volumes(f"{VOLUME}:{DATA_DIR}:ro"), f"{VOLUME} is mounted read-only"),
        (_set_volumes(), f"{SERVICE} does not mount"),
    ],
)
def test_checker_catches(change, expected) -> None:
    problems = _problems(_mutated(change))
    assert any(p.startswith(expected) for p in problems), problems


def test_trailing_slash_is_the_same_directory() -> None:
    assert _problems(_mutated(lambda s: s["environment"].update(PGDATA=DATA_DIR + "/"))) == []
