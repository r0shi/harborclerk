---
name: harbor-clerk
description: Search and read documents from a local Harbor Clerk knowledge base. Use when the user references their personal documents, contracts, notes, emails, or asks "what did I store about X?"
---

# Harbor Clerk — extended memory for agents

Harbor Clerk is a local document corpus with hybrid FTS+vector search and citation-preserving reads. This skill exposes it via the `harbor-clerk` CLI.

## First step: discover the surface
Run `harbor-clerk --help` for the full command list, then `harbor-clerk <cmd> --help` for any specific command. The help is comprehensive — JSON return shapes, examples, common mistakes are all there.

## Three patterns you'll use most

1. **Search → expand**: `harbor-clerk search "..."` then `harbor-clerk expand-context <chunk_id> -n 3` on the best hit.
2. **Read a known document**: `harbor-clerk read-document <doc_id>` for full text with pagination.
3. **Check ingest status before searching for new content**: `harbor-clerk ingest-status <doc_id>` returns per-stage state.

## What you can trust
- Every search result includes a `citation` field — quote it back to the user.
- `possible_conflict: true` means top hits disagree across documents; surface both sources.
- The CLI exits non-zero on failure. Exit code 3 specifically means an admin has disabled CLI access — tell the user.
