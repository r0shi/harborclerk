# Search Workbench and Find All Parity - Design Spec

**Date:** 2026-06-03
**Status:** Accepted for planning
**Scope:** Search page promotion, Find All mode, friendly filters, raw metadata filters, result shape, and parity across REST/MCP/CLI/local tools.

## Overview

Search should become Harbor Clerk's default working surface for a populated corpus. To earn that role, it needs to expose more of the retrieval power that already exists in MCP and local tools. The product currently has a mismatch: agents can use richer filters and enumeration tools than the human UI and REST search surface expose. That undercuts both the user experience and the product story.

This spec defines a Search Workbench with two modes:

- **Search:** find the most relevant passages.
- **Find All:** enumerate matching documents with stable filters and pagination.

The initial release does not need every result-set action, but it should make the core retrieval surface feel serious, inspectable, and aligned with agent tooling.

## Goals

- Add a clear Search / Find All mode toggle.
- Expose friendly filters for the major search axes.
- Provide raw metadata JSON as a power-user escape hatch.
- Bring REST and UI closer to MCP/local tool capabilities.
- Add CLI parity for `kb_find_all`, because CLI parity is release-blocking for agent harness credibility.
- Keep result behavior consistent across Search and Find All: users see the matching passage/snippet first, with the same path into document context.
- Let users click from chunk to document context.
- Design result-set actions now, but do not require implementation before release.

## Non-goals

- No saved searches or named collections in this scope.
- No full result-set action implementation for launch.
- No API permission model changes. These are search filters, not access-control rules.
- No attempt to make Search a general analytics/query builder.
- No relevance-ranking rewrite.

## Mode model

### Search mode

Intent: "Show me the best matching passages."

Default result shape:

- Passage/chunk row.
- Citation.
- Snippet with matched text.
- Document title and folder label.
- Score/confidence if already exposed, but avoid making the UI feel like a debug panel.
- Button/link to open the document at the chunk context.

### Find All mode

Intent: "List every document that matches these conditions up to a cap."

Default result shape:

- Matching passage/chunk row, using the same interaction model as Search.
- Clear indication that the row came from Find All enumeration rather than top-K relevance search.
- Citation/source identity.
- Folder label and relative path when useful.
- Matching metadata summary.
- Total matches, returned count, offset/page controls, and truncation state.

Find All should lean on `kb_find_all` semantics: server-side enumeration, document dedupe, `max_results`, `offset`, `sort_by`, `text_contains`, date filters, MIME/language filters, and metadata filters.

Implementation note: the UI should feel like Search even if the backing tool enumerates document-deduped matches. Each result still needs a concrete best matching passage/snippet so the user can inspect why it matched and click into document context. If later work adds a document/grouped view, it should be an alternate view, not the initial default.

## Filter set

Filters are search filters only. They do not change API key permissions or user access.

Friendly filters:

- Folder scope.
- Date range.
- MIME type / file type.
- Language.
- Email from.
- Email to.
- Email cc.
- Email subject.
- Exact text contains.
- Document id, where appropriate for advanced users.
- Summary state: has summary / missing summary.
- Pipeline state: ready / processing / failed.
- Ingest issue state, with at least a general "has ingest issue" filter and finer filters where practical:
  - Entity extraction skipped or failed.
  - OCR skipped or failed.
  - Summary missing, skipped, queued, running, or failed.
  - Stale/completed-status-cleanup state surfaced by System Status.

Power-user filter:

- Raw metadata JSON, modeled after a friendly policy builder: UI filters first, raw JSON second.

The UI should show raw JSON as an optional drawer or panel, not as the default way to filter. Friendly filters should generate the raw object where possible so users can learn the shape without being forced to hand-write it.

Before allowing editable raw JSON, answer the validation and safety questions explicitly:

- What schema is allowed?
- Which metadata keys are legal?
- Which operators are legal?
- How are type mismatches reported?
- Is the JSON compiled into parameterized SQL only?
- Are wildcard, regex, nested-object, and array cases bounded?
- Are error messages safe and non-leaky?

If those questions cannot be answered quickly and tested well, the release version should make raw JSON view-only: users can inspect the generated metadata filter from the friendly controls, but cannot submit arbitrary JSON.

## REST/API requirements

`POST /api/search` should accept the same practical filter axes as MCP search/find-all:

- `scope`
- `after`
- `before`
- `language`
- `mime_type`
- `doc_id`
- `doc_ids`
- `metadata_filter`
- `text_contains`
- `summary_state`
- `pipeline_status`
- `job_stage`
- `job_status`
- `job_issue`

Add a REST endpoint or request mode for Find All. Options:

1. `POST /api/search/find-all`
2. `POST /api/search` with `mode: "search" | "find_all"`

Recommendation: separate endpoint for clearer behavior and response schema. Search and enumeration are conceptually different enough that overloading one route may create UI and schema awkwardness.

## MCP/local tool requirements

MCP already has `kb_find_all`. Local chat mirror exists as `find_all_documents`. Keep those surfaces aligned with the Search Workbench semantics and `SourceRef` contract.

Tool descriptions should continue to distinguish:

- Use search for top relevant passages.
- Use find-all for enumerate/list every/all documents.

## CLI requirements

CLI parity is required for the release posture around agent harnesses.

Add:

- `harbor-clerk find-all`
- help text under `src/harbor_clerk/cli/help/find-all.txt`
- JSON output preserving the MCP response shape
- friendly/table output for humans
- support for `--query`, `--text-contains`, `--max-results`, `--offset`, `--sort-by`, date filters, MIME/language filters, document filters, and raw `--metadata-filter-json`

The CLI should not expose absolute paths by default.

## Result-set actions

Design now, implement later unless release timing allows:

- Ask over these results.
- Research over these results.
- Export results.
- Copy citations.
- Copy result JSON.

These actions are a fast-follow because they are meaningful product work, not just buttons. They need scoped retrieval semantics, citation preservation, and possibly saved transient result sets.

## Strategic release timing

If the OpenClaw/agentic harness eval has meaningful wall time, Search Workbench implementation is a good candidate to run in parallel or fill waiting periods. At minimum, the release gate should include the mode toggle and core Find All UI. This should be treated as a strategic scheduling opportunity, not a reason to defer the Search work indefinitely.

## UI conventions

- Mode toggle should be visible and plain. "Advanced" checkbox is the wrong model.
- Filters should collapse gracefully on narrow windows without hiding active constraints.
- Active filters should render as chips with clear remove controls.
- Raw JSON should validate inline and show friendly errors.
- Result rows should not resize unpredictably when snippets expand.
- Chunk preview should have a stable expand/open-in-document affordance.

## Relationship to Explorer

The existing Explorer page already has useful corpus-discovery UI: entity/topic pivots, related-topic chips, filtered document sub-panes, timeline views, compact document rows, and lightweight sort/filter controls. Search Workbench should inspect and reuse those patterns where they help.

Do not merge Explorer wholesale into Search for the first Search Workbench pass. Search should stay focused on targeted retrieval and enumeration. Explorer can remain the browse/discovery surface while Search borrows the interaction patterns that make sense for filters and result exploration.

## Release-blocking requirements

- Search is good enough to be the default page for populated corpora.
- UI exposes the major filters already available to tools.
- REST search accepts metadata and exact-text filters.
- Find All has a human UI path or a clearly imminent implementation path. Recommendation: include it before release if feasible.
- CLI has `find-all`, so MCP and CLI are no longer mismatched on the most visible enumeration tool.
- Search and Find All results use the new `SourceRef`/citation contract or are ready for it.

## Tests

- Backend tests for `metadata_filter` and `text_contains` through REST search.
- Backend tests for summary-state filters, including has-summary and missing-summary.
- Backend tests for pipeline/job issue filters, including failed entity extraction and at least one general ingest-error queue state.
- Backend tests for Find All REST endpoint if added.
- CLI tests for `find-all` argument parsing and JSON passthrough.
- Frontend tests for filter serialization where practical.
- Manual UI verification with PDF, email, attachment, and plain-text corpora.

## Open questions

- Whether Search Workbench ships with Find All UI in the first release or only REST/CLI parity. Decision: at least the mode toggle and core Find All UI are a minimum release gate.
- Whether Find All shows chunks or documents by default. Decision: Find All should use the same passage/snippet-first behavior as Search, with document/grouped views left as possible alternates.
- Whether raw JSON is edited directly or through a drawer. Decision: use a drawer/panel. Editable raw JSON is allowed only after schema validation and SQL-safety questions are answered; otherwise raw JSON is view-only for release.
