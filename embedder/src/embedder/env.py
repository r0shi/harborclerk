"""Environment reads shared by the embedder and the reranker, total by construction.

Both services read their knobs at import, so a bad value would stop uvicorn binding at all, on the services
this code exists to keep alive. An unset compose passthrough (`NAME=${NAME}`) or a bare `NAME=` line in .env
both arrive as the empty string.
"""

import logging
import os

logger = logging.getLogger(__name__)


def positive_int_env(name: str, default: int) -> int:
    """A positive int from the environment, or `default`; junk is logged, never raised."""
    raw = os.environ.get(name, "")
    try:
        return max(1, int(raw))
    except ValueError:
        if raw:
            logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
