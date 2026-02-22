"""RQ worker entry point.

Usage:
    harbor-clerk-worker                    # reads RQ_QUEUES env var (default: io)
    harbor-clerk-worker --queues io cpu    # explicit queue names
"""

import argparse
import logging
import os

from redis import Redis
from rq import Worker

from harbor_clerk.config import get_settings
import harbor_clerk.worker.stages  # noqa: F401 — register stage functions for RQ


def main():
    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="Harbor Clerk RQ worker")
    parser.add_argument(
        "--queues",
        nargs="+",
        default=os.environ.get("RQ_QUEUES", "io").split(","),
        help="Queue names to listen on",
    )
    args = parser.parse_args()

    conn = Redis.from_url(settings.redis_url)
    worker = Worker(args.queues, connection=conn)
    worker.work()


if __name__ == "__main__":
    main()
