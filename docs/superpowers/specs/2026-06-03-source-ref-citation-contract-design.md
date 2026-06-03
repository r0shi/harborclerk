# SourceRef and Citation Contract - Design Spec

**Date:** 2026-06-03
**Status:** Accepted for planning
**Scope:** Common source/citation object across REST, MCP, CLI, local chat/research, and UI. This spec is about response contracts and citation policy, not retrieval ranking.

## Overview

Harbor Clerk's strongest product promise is that search and AI answers are grounded in inspectable sources. That promise gets weaker if each surface invents its own citation string, omits chunk identifiers, exposes filesystem paths inconsistently, or handles email differently from documents.

This spec introduces a shared `SourceRef` contract: responses should include both a structured `source` object and a derived human-readable `citation` string. The structured object gives agents and UI code stable fields; the string gives cloud LLMs and humans a compact citation they can quote directly.

## Goals

- One citation/source schema used by REST search, MCP tools, CLI commands, local chat tools, Ask, Research, and document UI where practical.
- Include `chunk_id` whenever the result is chunk-backed.
- Include a derived `citation` string in tool/API responses, not only in UI rendering.
- Give email and email attachments first-class citation formats.
- Avoid exposing absolute local filesystem paths to MCP/cloud/agent responses.
- Allow folder aliases/labels and relative paths to be exposed as contextual source identity when policy permits.
- Make citation generation testable and centrally maintained.

## Non-goals

- No rewrite of the retrieval algorithm.
- No guarantee that every legacy endpoint immediately returns the new object in the first PR.
- No source-file download policy change.
- No cryptographic citation verification.
- No full provenance graph for derived summaries.

## SourceRef schema

Initial shape:

```json
{
  "doc_id": "uuid",
  "doc_title": "string",
  "chunk_id": "uuid|null",
  "pages": "4-5|null",
  "section": "string|null",
  "source_kind": "document|email|attachment|unknown",
  "source_label": "string",
  "folder_label": "string|null",
  "relative_path": "string|null",
  "citation": "string"
}
```

Field semantics:

- `doc_id`: stable Harbor Clerk document identifier.
- `doc_title`: display title for the document.
- `chunk_id`: stable chunk/passage identifier when the result is chunk-backed. Null for document-level results without a specific passage.
- `pages`: normalized page or page range when known, such as `"4"` or `"4-5"`.
- `section`: heading, email part label, or other structural context when known.
- `source_kind`: broad source category.
- `source_label`: concise human-readable source name. For normal documents this is usually the title. For email it may include sender/subject/date.
- `folder_label`: watched-folder alias/name, not an absolute path.
- `relative_path`: path relative to the watched-folder root when policy permits.
- `citation`: human-readable citation derived from the same fields.

## Citation formats

### Documents

- `Contract A, p. 4`
- `Contract A, pp. 4-5`
- `Contract A`

Use `p.` for one page and `pp.` for a range. If pages are unknown, omit page text rather than inventing location.

### Email bodies

- `Email from Jane Doe, "Budget follow-up", Mar 7, 2025`
- `Email from Jane Doe to Sam Lee, "Budget follow-up", Mar 7, 2025`

The short form should prefer sender, subject, and date. Include recipient when it materially disambiguates or when email metadata is central to the result.

For `.eml` files on disk, default to email-native metadata. If parsing failed or key email fields are missing, fall back to file metadata rather than producing a broken email citation. Example fallback order:

1. Parsed email sender/subject/date.
2. Document title plus whatever email metadata is available.
3. File name or relative path plus page/section context if no usable email metadata exists.

### Email attachments

- `Attachment "invoice.pdf" to Email from Jane Doe, "Budget follow-up", Mar 7, 2025`
- `Attachment "contract.pdf", p. 2, to Email from Jane Doe, "Budget follow-up", Mar 7, 2025`

Attachments should keep both identities: the attachment as the retrieved document and the parent email as provenance.

### Unknown or partial source data

When metadata is missing, degrade gracefully:

- `Untitled document`
- `Email "Budget follow-up"`
- `Attachment "invoice.pdf"`

Do not emit placeholders like `None`, `unknown.pdf`, or `page null`.

## Path and security policy

Absolute paths are sensitive because MCP and cloud LLMs may receive them. They can reveal usernames, project names, client names, folder structure, or mounted-volume details.

Policy:

- MCP responses: no absolute paths.
- CLI responses: no absolute paths by default. Provide an explicit local-only flag later if needed.
- REST responses used by the web UI: may include source-path data only when the endpoint already requires human auth and the UI needs local reveal/open behavior.
- Cloud LLM bridge, if built: no absolute paths in model-visible payloads.
- Folder labels and relative paths may be exposed if they are deliberately user-facing. This is still mildly sensitive, so docs should state it clearly.

For watched folders, prefer:

```json
{
  "folder_label": "Enron-ingest",
  "relative_path": "lay/2001/forwarded/foo.eml"
}
```

over:

```json
{
  "path": "/Users/alex/Documents/private-client/lay/2001/forwarded/foo.eml"
}
```

### Fast-follow: path disclosure scope

Path disclosure should become an explicit API-key/tool policy rather than a one-size-fits-all default. Some local-agent use cases may already grant an agent broad filesystem access, making absolute paths acceptable and useful. Cloud-facing keys should remain conservative.

Possible enum:

- `filename`: expose only source/file label.
- `relative_path`: expose folder label plus path relative to the watched-folder root.
- `absolute_path`: expose full local path.

Default recommendation:

- Cloud/MCP keys: `relative_path` at most, possibly `filename` for conservative setups.
- CLI/local-agent keys: `relative_path` by default, `absolute_path` opt-in.
- Human UI: can use absolute paths for local reveal/open affordances where already authenticated and expected.

## Surface matrix

### REST search

Search hits should include:

- `source`
- top-level `citation` alias, if compatibility requires it
- `chunk_id`
- existing snippet/score fields

The frontend should render citations from `source.citation` rather than recreating page strings ad hoc.

### REST document detail

Document detail can expose richer local metadata because it is a human-authenticated UI endpoint. It should still separate:

- `source` fields safe for citations.
- local file fields used for reveal/open/download affordances.

### MCP

Every result-bearing tool should use the same source shape where it returns passages or documents:

- `kb_search`
- `kb_batch_search`
- `kb_find_all`
- `kb_read_passages`
- `kb_expand_context`
- `kb_get_document`
- `kb_document_outline`
- `kb_find_related`
- `kb_documents_by_date`
- `kb_verify_identifier`

Tools that return operational data, such as health or ingest status, do not need `SourceRef` unless they include document rows.

### CLI

CLI JSON output should preserve the same structure as MCP. Friendly/table output should render citation strings and keep full source objects available under `--json`.

This is important for agent harnesses that shell out to CLI and parse output.

### Ask and Research

Local chat and research tool results should preserve `SourceRef` internally. Final answers should cite using `citation` strings and carry enough metadata for UI citation chips to open the correct document/chunk.

### UI citation component

`CitedMarkdown` and result components should accept normalized citation/source data instead of parsing strings wherever practical. String parsing is acceptable only as a backwards-compatibility shim.

## Implementation notes

- Add a central citation builder in Python.
- Add a TypeScript type mirroring `SourceRef`.
- Keep old fields temporarily if existing UI code depends on them.
- Avoid a large single PR if the surface is too broad. Recommended order:
  1. Shared backend type and builder.
  2. REST search plus UI search result rendering.
  3. MCP/CLI result normalization.
  4. Ask/Research final answer citation chips.
  5. Legacy cleanup.

## Tests

- Unit tests for citation string generation, including page ranges, missing pages, email, and attachments.
- Contract tests for MCP/CLI JSON shape.
- REST schema tests for search hits.
- Frontend render tests for citation chips if existing test harness supports it.
- Security regression test that MCP/CLI result payloads do not include absolute paths unless an explicitly local-only mode is introduced.

## Open questions

- Whether emails on disk (`.eml`) should get both a document citation and an email citation. Recommendation: the primary citation should be email-native; the source object can still include relative path and document title.
- Whether `relative_path` should always be included in MCP payloads. Recommendation: include only when the folder label is user-facing and the path is useful for disambiguation; otherwise omit.
- Whether `source_label` and `citation` are redundant. Decision: keep both. `source_label` is identity; `citation` is a formatted quote target. Extra source signal is worth preserving.
