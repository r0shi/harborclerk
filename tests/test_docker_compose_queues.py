"""Every worker queue defined in code must have a subscriber in docker-compose.

Regression test for the bug where `summarize` was moved onto its own `llm`
queue but `docker-compose.yml` only started workers for `io` and `cpu`. Nothing
claimed the `llm` queue, so on Docker Compose deployments summarize jobs were
enqueued and silently never ran — documents ingested fine but never received a
summary. macOS was unaffected because ServiceManager spawns an llm worker pool.

The failure mode is invisible at runtime (no error, no crash — jobs just sit),
so it needs a static check.
"""

from pathlib import Path

import yaml

from harbor_clerk.worker.entry import QUEUE_STAGES

COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.yml"
WORKER_ENTRYPOINT = "harbor-clerk-worker"


def _load_services() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())["services"]


def _queues_for_service(service: dict) -> set[str]:
    """Resolve which queues a compose service subscribes to.

    Mirrors the arg parsing in worker/entry.py: `--queues` uses nargs="+", so it
    consumes every following token until the next flag. When absent, the worker
    falls back to the RQ_QUEUES env var (comma-separated), defaulting to "io".
    """
    command = service.get("command") or []
    if not isinstance(command, list) or WORKER_ENTRYPOINT not in " ".join(map(str, command)):
        return set()

    if "--queues" in command:
        queues: set[str] = set()
        for token in command[command.index("--queues") + 1 :]:
            if str(token).startswith("-"):
                break
            queues.add(str(token).strip())
        return queues

    env = service.get("environment") or {}
    raw = env.get("RQ_QUEUES", "io") if isinstance(env, dict) else "io"
    return {q.strip() for q in str(raw).split(",") if q.strip()}


def _subscribed_queues() -> set[str]:
    return set().union(*(_queues_for_service(s) for s in _load_services().values()), set())


def test_every_code_queue_has_a_compose_worker() -> None:
    missing = set(QUEUE_STAGES) - _subscribed_queues()
    orphaned_stages = [stage.value for q in sorted(missing) for stage in QUEUE_STAGES[q]]
    assert not missing, (
        f"docker-compose.yml has no worker subscribing to queue(s) {sorted(missing)}. "
        f"Jobs for these stages would be enqueued and never claimed: {orphaned_stages}. "
        "Add a worker service with --queues <name>."
    )


def test_compose_workers_only_reference_known_queues() -> None:
    unknown = _subscribed_queues() - set(QUEUE_STAGES)
    assert not unknown, (
        f"docker-compose.yml subscribes to queue(s) {sorted(unknown)} that do not exist in "
        f"QUEUE_STAGES ({sorted(QUEUE_STAGES)}). Those workers would start and idle forever."
    )
