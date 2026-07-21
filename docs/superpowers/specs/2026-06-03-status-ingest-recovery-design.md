# Status, Ingest, and Recovery UX - Design Spec

**Date:** 2026-06-03
**Status:** Accepted for planning
**Scope:** User-facing operational health, failed ingest visibility, plain recovery actions, diagnostics, and minimal backup guidance. This spec is not the full backup/restore design.

## Overview

Harbor Clerk is a local system with several moving parts: watched folders, database, Tika, workers, embeddings, reranker, local model server, and the Mac menu app. Users should not need to think in those terms during normal operation. The release UI should expose a Status area that answers:

- Is the system ready?
- Is anything stuck or failed?
- What can I do about it?
- Where do I go for deeper diagnostics?

"Health" and raw service controls are useful to developers and advanced operators, but they should not be the main recovery interface.

## Goals

- Rename or reframe the normal operational surface as Status.
- Add an advanced Diagnostics section for logs, raw service health, and low-level details.
- Show failed/needs-attention documents in both Documents and Status.
- Provide plain recovery actions instead of raw process controls.
- Remove stale or misleading "show logs in console" controls.
- Include simple backup guidance in docs for initial release.
- Add an always-visible global status signal that links into the fuller Status surface.
- Note future self-help/docs RAG as a possible recovery aid.

## Non-goals

- No full backup/restore product flow in this scope.
- No time-machine style versioning.
- No remote monitoring or alerting.
- No exposing every subprocess control in the main UI.
- No large ingest pipeline redesign.

## Status page model

Status should be organized around user questions:

### Ready

Examples:

- Search index ready.
- Folders watching.
- Local AI ready or not configured.
- No failed documents.

### Needs attention

Examples:

- Documents failed ingest.
- Folder access lost.
- Local AI server stopped.
- Search services unavailable.
- Summaries pending or failed, if user-visible.

### Recent activity

Examples:

- Recently indexed documents.
- Currently processing documents.
- Retry history.

### Advanced diagnostics

Examples:

- Raw service status.
- Logs.
- Worker queues.
- Configuration detail.

Advanced diagnostics can be in the Status page behind an advanced section or in a separate Settings -> Diagnostics route. The user-facing label should still be Status.

## Global status signal

Recommendation: add a global status pill in the app chrome, with a fuller Status page/tray as drill-down.

The pill should be compact and boring:

- Ready
- Processing
- Needs attention
- Local AI unavailable
- Folder access issue

Clicking the pill should open Status, or a small tray that links to Status. The pill is better than a full always-open tray for release because it keeps the primary app surfaces calm while still making system state visible.

Use the existing queue tray where it helps, but keep the concepts distinct:

- Queue tray: current/recent processing activity.
- Status pill/page: readiness, failures, recovery actions, and operational needs.

If the pill becomes too dense, the next step is a small tray or popover. The default should not be a large persistent panel.

## Recovery actions

Use task-level actions:

- Restart search services.
- Retry failed documents.
- Restart local AI.
- Repair folder access.
- Rescan folder.
- Recompute summaries, if summary failure is user-visible.

Avoid primary buttons like:

- Restart container.
- Restart postgres.
- Kill worker.
- Show logs in console.

Raw actions may remain under Diagnostics if truly needed.

## Failed ingest visibility

Failed documents should appear in two places:

### Documents

- Filter or chip for failed documents.
- Row state that makes failure visible without opening detail.
- Retry action when available.

### Status

- Needs-attention queue summarizing failed documents.
- Bulk retry action.
- Link to filtered Documents view.

This dual placement matters because Documents is where users inspect the corpus, while Status is where they resolve operational problems.

## Known escalation note: entity extraction outages

2026-06-04 dev-system observation: a macOS bundle could have spaCy installed but be missing the packaged spaCy NER model packages. The expected safe behavior when NER is unavailable is that the `entities` stage is skipped with `spacy_unavailable` and existing entity rows are left alone. Existing entity rows should disappear only when a document is intentionally reprocessed or chunks are rebuilt, or if `run_entities()` executes and replaces prior rows with an empty extraction result.

Do not chase this further on a noisy dev system unless it recurs. Escalate if any of these happen in a cleaner environment:

- Corpus or document entity counts drop after a bundle/model outage without an explicit reprocess.
- Documents that previously had entity rows show zero entities and their latest `entities` job is only a skipped `spacy_unavailable`.
- Documents reach ready state after model availability is restored but still have no entity rows and no visible recovery path.

First read-only checks:

- Inspect `ingestion_jobs` for `stage='entities'`, `metrics.reason='spacy_unavailable'`, timestamps, and retry/reprocess history.
- Compare whether `chunks` were deleted or recreated for affected docs.
- Compare entity row counts by document before and after the suspected outage if a backup or previous snapshot exists.

Potential product follow-ups:

- Show "entity extraction skipped" as a recoverable status distinct from a hard ingest failure.
- Add an entity-stage-only retry that does not rebuild chunks or embeddings.
- Include NER model availability in the global Status/Diagnostics surface.

## Folder access recovery

For macOS watched folders, Status should be able to tell the user when a folder is missing, moved, or permission-denied. Recovery should use normal user language:

- "Repair folder access"
- "Choose folder again"
- "Reveal folder"
- "Remove folder"

The UI should not make the user infer folder state from low-level watcher errors.

## Local AI recovery

If Ask/Research cannot reach the local model server:

- Status shows local AI as needing attention.
- Ask/Research show a local inline banner with a link to Status or Models.
- Recovery action says "Restart local AI."
- If no model is configured, action says "Choose a model" instead of restart.

## Logs and diagnostics

Logs should be available, but not as primary UX.

Decision:

- Remove "show logs in console" if no logs actually go there or if that path is deprecated.
- Keep log access under Diagnostics.
- Prefer in-app log viewing/export if implemented later.
- Make clear that Diagnostics is for advanced troubleshooting.

## Backup guidance for initial release

Full backup/restore is a separate spec. For initial release docs, include simple guidance:

- Quit Harbor Clerk.
- Copy `~/Library/Application Support/Harbor Clerk/` to backup storage.
- Keep watched-folder source files backed up separately, because Harbor Clerk reads them in place.
- Restore should be documented cautiously and tested before being presented as a polished workflow.

This is basic, but better than no guidance.

## Future: self-help documentation RAG

The Status/recovery UX will likely require comprehensive docs. A future feature could let Harbor Clerk answer questions about itself by indexing its own documentation and using a local model to provide guided troubleshooting.

Potential value:

- Users can ask "why is local AI unavailable?" or "how do I back this up?" in natural language.
- Answers can cite Harbor Clerk docs, keeping the same product pattern of inspectable sources.
- This could reduce support burden without pretending every user should understand the internals.

Constraints:

- Keep this out of the initial repair loop unless it is very stable.
- It must not replace clear buttons for common recovery actions.
- It should cite docs and distinguish product guidance from live system diagnosis.
- It should avoid sending local config, logs, or sensitive paths into model prompts unless explicitly scoped.

Recommendation: document this as a future self-help layer after the core Status page, diagnostics, and backup docs exist.

## Release-blocking requirements

- Status exists as the normal user-facing health/recovery label.
- A global status pill or equivalent signal makes needs-attention state visible outside Settings.
- Failed documents are discoverable from Documents and Status.
- Plain recovery actions exist for the most common failure modes.
- "Show logs in console" is removed if stale.
- Backup docs include the simple Application Support folder guidance.

## Tests

- Backend/API tests for failed-ingest summaries if a new endpoint is added.
- Frontend tests for Status needs-attention rendering where practical.
- Manual failure scenarios:
  - Stop local AI.
  - Add a file that fails ingest.
  - Remove or deny access to a watched folder.
  - Restart search services.
- Manual verification that logs/diagnostics are still reachable for advanced operators.

## Open questions

- Whether Status should be top-level or under Settings. Decision: Status lives inside Settings and also appears as a global status pill or equivalent chrome signal.
- Whether the queue tray and Status page should share components. Recommendation: yes where possible, but do not let component reuse dictate the user story.
- Whether backup guidance should appear in-app or docs only. Recommendation: docs only for the initial release.
- Whether Harbor Clerk should answer questions about its own documentation. Recommendation: promising fast-follow/future self-help layer, not a release blocker.
