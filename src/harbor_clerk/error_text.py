"""One way to render an exception for a human.

Three formats had grown up independently:

    f"error: {e}"                  api/routes/system.py, mcp_server.py
    f"{type(e).__name__}: {e}"     worker/entry.py, lang_packs/manager.py
    str(e)                         the /system/stats blocks

All three lose information on exactly the exceptions that matter most.
`httpx.ReadTimeout('')`, `httpx.ConnectTimeout('')` and a bare `TimeoutError()`
all carry an empty message, so the first renders the literal `"error: "`, the
second renders `"ReadTimeout: "` with a dangling colon, and the third renders
nothing at all.

That is not hypothetical. The Mac mini reported
`{"checks":{"embedder":"error: "}}` while its embedder was timing out, and the
Status page rendered "Search services need attention — embedder: error:". A
timeout is precisely the failure worth naming, and the blank string is part of
why an investigation went looking for a crash that had not happened.

The worker path matters more than the health path: `worker/entry.py` writes its
rendering into `ingestion_jobs.error` and `documents.error`, which is what a
user sees as the reason a document failed.
"""

from __future__ import annotations

# Bound for untrusted-reader surfaces. A SQLAlchemy OperationalError stringifies
# with the failing statement and connection detail, and /api/system/health is
# unauthenticated — see the caller in api/routes/system.py. This is a length
# bound, not redaction: it limits how much leaks, it does not make the prefix
# safe to publish. Anything genuinely secret must not reach an exception message.
HEALTH_DETAIL_LIMIT = 200


def describe_error(exc: BaseException, *, max_detail: int | None = None) -> str:
    """Render `exc` as ``"TypeName: message"``, or ``"TypeName"`` when empty.

    Never returns a string ending in a dangling colon, which is the whole point
    — a reader must always learn at least what kind of failure occurred.

    `max_detail` truncates the message (not the type name) for callers that
    expose the result to an unauthenticated reader.
    """
    name = type(exc).__name__
    detail = str(exc).strip()
    if not detail:
        return name
    if max_detail is not None and len(detail) > max_detail:
        detail = detail[:max_detail].rstrip() + "…"
    return f"{name}: {detail}"
