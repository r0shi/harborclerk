---
name: harbor-clerk
description: Search and read documents from a local Harbor Clerk knowledge base. Use when the user references their personal documents, contracts, notes, emails, or asks "what did I store about X?"
---

# Harbor Clerk - extended memory for agents

Harbor Clerk is a local document corpus with hybrid FTS+vector search and citation-ready reads. This skill exposes it via the `harbor-clerk` CLI.

## First step: discover the surface
Run `harbor-clerk --help` for the full command list, then `harbor-clerk <cmd> --help` for any specific command. The help is comprehensive: JSON return shapes, examples, and common mistakes are all there.

## Four patterns you'll use most

1. **Search → expand**: `harbor-clerk search "..."` then `harbor-clerk expand-context <chunk_id> -n 3` on the best hit.
2. **Find every matching document**: `harbor-clerk find-all "..." --text-contains "literal phrase" --presentation full` when the task asks to list or enumerate.
3. **Read a known document**: `harbor-clerk read-document <doc_id>` for full text with pagination.
4. **Check ingest status before searching for new content**: `harbor-clerk ingest-status <doc_id>` returns per-stage state.

## What you can trust
- Search returns top-level `.hits[]`; Find All returns top-level `.results[]`. Result-bearing commands include `source`, `citation`, and `chunk_id` when available. Use those fields as the citation trail, then call `read-passages` when you need exact surrounding text.
- `possible_conflict: true` means top hits span multiple similarly scored documents; inspect the relevant sources before answering.
- `ingest-status`, `read-document`, and entity commands may require a Full-tier API key. If access is denied, continue with search and passage reads, and tell the user the key may be scoped too narrowly for that command.
- The CLI exits non-zero on failure. Exit code 3 specifically means an admin has disabled CLI access; tell the user.
