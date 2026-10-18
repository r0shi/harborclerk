"""an insertion-order key on chat_messages

``chat_messages.created_at`` defaults to ``now()``, the start of the transaction, and ``chat_stream``
writes a whole turn in one transaction: the turn's user question (on every turn but a
conversation's first, whose question the title commit writes on its own), each assistant tool-call
row, each tool result and the final answer share one ``created_at``. ``message_id`` is a random UUID,
so nothing broke the tie, and every reader ordered by ``created_at`` alone got the tied rows in
heap order: a tool result before its call, a turn's answer before its question, and a history trim
that split a call from its result (#809).

``seq`` is a ``bigint GENERATED ALWAYS AS IDENTITY``: the sequence hands out a strictly increasing
value to each row as it is inserted, so rows one session inserts in order get increasing values,
and every reader orders by ``(created_at, seq)``. ALWAYS rather than BY DEFAULT, so no writer can
supply a value out of order. ``idx_messages_conv`` becomes ``(conversation_id, created_at, seq)``,
the readers' whole ordering.

Backfill. The insertion order of rows that already tie on ``created_at`` was never recorded, so it
cannot be recovered; existing rows are numbered in a stated best-effort order, deterministic for
a given table: by ``created_at``, then ``conversation_id`` (a tie group is one conversation's
turn), then within a tie group

1. ``user`` rows first (the turn's question);
2. then each assistant tool-call row (a non-empty ``tool_calls`` array) followed at once by the
   ``tool`` rows that answer it (matched on ``tool_call_id`` to an id in that call's
   ``tool_calls``, within the same conversation and ``created_at``), in the order of the ids in its
   array; the calls among themselves by ``message_id``, which is arbitrary: which search round came
   first is not recoverable, but each call stays next to its results;
3. ``tool`` rows that match no call in their tie group, by ``message_id``, after the calls;
4. then every other row, last (an assistant answer or a stored error ends its turn).

Limits: the order of tool-call rounds within one turn is arbitrary (by ``message_id``); a tool row
whose id matches two calls in one tie group (a repeated id, such as the ``call_<name>`` fallback)
goes after the first of them by ``message_id``; a tie group holding more than one question or
more than one answer (which ``chat_stream`` does not write) keeps them in ``message_id`` order.
Rows with distinct timestamps keep their order exactly.

The identity's sequence is then set past the largest backfilled value, so new rows continue above.

The ALTER takes ACCESS EXCLUSIVE on ``chat_messages``. At startup nothing holds it; beside a live
API (the Run Migrations route) a chat turn holds its transaction open for the whole answer, so the
ALTER waits at most 10 s for its lock and otherwise fails, rather than queueing every chat read
behind it (0009's convention). Everything runs in the migration's transaction, so an interrupted
run rolls back whole.

Downgrade drops ``seq`` (and its sequence) and restores ``idx_messages_conv`` on
``(conversation_id, created_at)``.

Revision ID: 0010_chat_message_seq
Revises: 0009_fts_over_folded_text
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_chat_message_seq"
down_revision: str | None = "0009_fts_over_folded_text"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]

LOCK_TIMEOUT = "10s"
LOCK_NOT_AVAILABLE = "55P03"

# Numbers every existing row in the best-effort order described above. ``pair_key`` is the call row's
# message_id for a call and for each tool row that answers it, so a call and its results sort together.
BACKFILL_SQL = """
WITH calls AS (
    SELECT m.message_id AS call_id, m.conversation_id, m.created_at, e.ord, e.elem ->> 'id' AS tc_id
    FROM public.chat_messages AS m
    CROSS JOIN LATERAL jsonb_array_elements(m.tool_calls) WITH ORDINALITY AS e(elem, ord)
    WHERE m.role = 'assistant' AND jsonb_typeof(m.tool_calls) = 'array'
),
keyed AS (
    SELECT
        m.message_id,
        m.conversation_id,
        m.created_at,
        CASE
            WHEN m.role = 'user' THEN 0
            WHEN m.role = 'assistant' AND jsonb_typeof(m.tool_calls) = 'array'
                 AND jsonb_array_length(m.tool_calls) > 0 THEN 1
            WHEN m.role = 'tool' AND c.call_id IS NOT NULL THEN 1
            WHEN m.role = 'tool' THEN 2
            ELSE 3
        END AS role_rank,
        COALESCE(c.call_id, m.message_id) AS pair_key,
        CASE WHEN c.call_id IS NOT NULL THEN 1 ELSE 0 END AS after_call,
        COALESCE(c.ord, 0) AS call_ord
    FROM public.chat_messages AS m
    LEFT JOIN LATERAL (
        SELECT calls.call_id, calls.ord
        FROM calls
        WHERE m.role = 'tool'
          AND calls.conversation_id = m.conversation_id
          AND calls.created_at = m.created_at
          AND calls.tc_id = m.tool_call_id
        ORDER BY calls.call_id, calls.ord
        LIMIT 1
    ) AS c ON true
)
UPDATE public.chat_messages AS m
SET seq = numbered.n
FROM (
    SELECT
        message_id,
        row_number() OVER (
            ORDER BY created_at, conversation_id, role_rank, pair_key, after_call, call_ord, message_id
        ) AS n
    FROM keyed
) AS numbered
WHERE numbered.message_id = m.message_id
"""


def _lock_chat_messages() -> None:
    """ACCESS EXCLUSIVE on chat_messages within LOCK_TIMEOUT, or a RuntimeError that says why."""
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    try:
        op.execute("LOCK TABLE public.chat_messages IN ACCESS EXCLUSIVE MODE")
    except sa.exc.DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != LOCK_NOT_AVAILABLE:
            raise
        raise RuntimeError(
            f"0010 could not lock public.chat_messages within {LOCK_TIMEOUT}: another transaction holds it "
            "(a chat turn being answered on a running API). Retry when it is idle, or restart the app (or the "
            "API container) so the migration runs at startup, before anything else opens the table."
        ) from exc
    op.execute("SET LOCAL lock_timeout = DEFAULT")


def upgrade() -> None:
    _lock_chat_messages()
    op.execute("ALTER TABLE public.chat_messages ADD COLUMN seq bigint")
    op.execute(BACKFILL_SQL)
    op.execute("ALTER TABLE public.chat_messages ALTER COLUMN seq SET NOT NULL")
    op.execute("ALTER TABLE public.chat_messages ALTER COLUMN seq ADD GENERATED ALWAYS AS IDENTITY")
    # Past the largest backfilled value; is_called false on an empty table so the first row gets 1.
    op.execute(
        "SELECT setval(pg_get_serial_sequence('public.chat_messages', 'seq'), "
        "COALESCE(MAX(seq), 1), MAX(seq) IS NOT NULL) FROM public.chat_messages"
    )
    op.execute("DROP INDEX public.idx_messages_conv")
    op.execute("CREATE INDEX idx_messages_conv ON public.chat_messages USING btree (conversation_id, created_at, seq)")


def downgrade() -> None:
    _lock_chat_messages()
    op.execute("DROP INDEX public.idx_messages_conv")
    op.execute("ALTER TABLE public.chat_messages DROP COLUMN seq")
    op.execute("CREATE INDEX idx_messages_conv ON public.chat_messages USING btree (conversation_id, created_at)")
