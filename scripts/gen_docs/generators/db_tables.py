"""Generate the database table reference from SQLAlchemy metadata.

Grouping note: the obvious derivable signals both fail. Model modules are
effectively 1:1 with tables (27 modules for 29 tables), so grouping by module
yields 27 groups of one. Foreign-key clustering collapses into a single
26-table component, because nearly everything transitively reaches `documents`
or `users`.

So the group assignment below is explicit — but `test_every_table_has_a_group`
asserts it covers `Base.metadata` exactly. Add a table without assigning a
group and the build fails. That keeps the one hand-maintained element from
rotting silently, which is the property that matters; the columns and types
are fully derived.
"""

from __future__ import annotations

from collections import defaultdict

TABLE_GROUPS: dict[str, str] = {
    # Documents and the ingestion pipeline
    "documents": "Documents & ingestion",
    "document_pages": "Documents & ingestion",
    "document_headings": "Documents & ingestion",
    "document_links": "Documents & ingestion",
    "chunks": "Documents & ingestion",
    "entities": "Documents & ingestion",
    "ingestion_jobs": "Documents & ingestion",
    # Watched folders
    "watched_folders": "Watched folders",
    "watched_files": "Watched folders",
    # Email / IMAP ingest
    "mail_accounts": "Email (IMAP)",
    "watched_labels": "Email (IMAP)",
    "watched_messages": "Email (IMAP)",
    "imap_command_log": "Email (IMAP)",
    # Identity and access
    "users": "Users & auth",
    "api_keys": "Users & auth",
    "oauth_clients": "Users & auth",
    "oauth_codes": "Users & auth",
    "oauth_tokens": "Users & auth",
    # Conversational surfaces
    "conversations": "Chat & research",
    "chat_messages": "Chat & research",
    "research_state": "Chat & research",
    # Corpus-level analysis
    "corpus_topics": "Corpus analysis",
    "corpus_topics_meta": "Corpus analysis",
    # Operational
    "audit_log": "Operations & audit",
    "api_request_log": "Operations & audit",
    "model_settings": "Operations & audit",
    "schema_metadata": "Operations & audit",
    # Legacy upload API, retained for non-interactive ingest
    "uploads": "Legacy uploads",
    "upload_sessions": "Legacy uploads",
}

GROUP_ORDER = (
    "Documents & ingestion",
    "Watched folders",
    "Email (IMAP)",
    "Chat & research",
    "Users & auth",
    "Corpus analysis",
    "Operations & audit",
    "Legacy uploads",
)


def load_metadata():
    import harbor_clerk.models  # noqa: F401 - registers every mapper
    from harbor_clerk.models.base import Base

    return Base.metadata


def _key_columns(table, limit: int = 6) -> str:
    """Primary keys, foreign keys, and other notable columns — never all of them."""
    parts: list[str] = []
    for column in table.columns:
        if column.primary_key:
            parts.append(f"`{column.name}` (PK)")
        elif column.foreign_keys:
            target = next(iter(column.foreign_keys)).column.table.name
            parts.append(f"`{column.name}` → `{target}`")
    for column in table.columns:
        if len(parts) >= limit:
            break
        type_name = type(column.type).__name__.lower()
        if type_name in {"vector", "tsvector"} and f"`{column.name}`" not in " ".join(parts):
            parts.append(f"`{column.name}` ({type_name})")
    return ", ".join(parts[:limit]) or "—"


def generate() -> str:
    metadata = load_metadata()
    grouped: dict[str, list[str]] = defaultdict(list)
    for name in metadata.tables:
        grouped[TABLE_GROUPS.get(name, "Ungrouped")].append(name)

    lines = [f"**{len(metadata.tables)} tables.** Generated from SQLAlchemy metadata.", ""]
    ordered = [*GROUP_ORDER, *sorted(set(grouped) - set(GROUP_ORDER))]
    for group in ordered:
        if group not in grouped:
            continue
        lines += [f"### {group}", "", "| Table | Key columns |", "|---|---|"]
        for name in sorted(grouped[group]):
            lines.append(f"| `{name}` | {_key_columns(metadata.tables[name])} |")
        lines.append("")
    return "\n".join(lines).rstrip()
