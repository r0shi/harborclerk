# Watched-Folder-First Refactor — Stage 2 Stub

> **Status:** brainstorm done, full spec not yet written. Pick this up after Stage 1 ships.
> **Predecessor:** [Stage 1 design](2026-04-30-watched-folder-first-stage-1-design.md) — must ship first (Docker users need watched folders to be possible before we can hide upload from them).

## Goal

Remove the direct-upload UI from the web app entirely; commit fully to the watched-folder model as the only user-facing ingest path.

## Decisions already locked in (during 2026-04-30 brainstorm)

- **Direct upload deprecation is "soft"** at the code level. The `/api/uploads/*` REST endpoints stay alive — they're a tested code path we may want for non-user-facing sources (e.g., future email ingestion). The UI is what goes away, not the code path.
- **No web upload affordance at all.** Not even a "drop into folder" proxy widget. We're not a filestore any more. Adding a file means going to Finder (or whatever the user's filesystem-side tool is). The web UI is purely for browsing, searching, chatting, and folder management.
- **Onboarding flow is "pick at least one folder to watch."** When a user reaches the app and has zero watched folders, they get a wizard / empty state that walks them through adding one. The wizard uses the same `pickFolder()` JS bridge on macOS and the docs link on Docker.
- **No migration of legacy direct-uploaded docs.** No real users have significant legacy uploads. Existing direct-uploaded docs (where `document_versions.source_path IS NULL` and there's no `watched_files` row) stay queryable but become unfoldered. We do not write retroactive folder-assignment code, do not auto-create a "Legacy" folder, do not hide them in the UI.
- **`/upload` route is unlinked from nav in Stage 1**, but the page itself stays mounted until Stage 2 deletes it.
- **The folder filter on chat/search (originally task #134) is NOT shipped here.** It folds into the Projects/Collections work (task #97) where a "project" picks a set of folders and chat/search inherits scope from that. The infrastructure already exists (`KeyScope.scope_folder_ids`, `apply_key_scope` in `src/harbor_clerk/api/scope.py`); Projects/Collections will expose the same dimension to interactive users.

## Scope (for the Stage 2 spec when it gets written)

In:
- Onboarding wizard / empty state on first reach if no watched folders exist.
- Remove `frontend/src/pages/UploadPage.tsx` (or equivalent) entirely.
- Remove the `/upload` route from `frontend/src/App.tsx`.
- Remove any "Upload" links elsewhere in the UI (e.g., empty-state CTAs on Documents, links from settings, etc.).
- Documents page becomes the primary content surface: empty state on first run links to the wizard rather than to upload.
- API surface: keep `/api/uploads/*` mounted, but consider whether anything user-facing still calls it. Probably not after this work — only tests + future email path.
- Onboarding wizard behaviour matrix:
  - macOS, no folders: walk user through `pickFolder` bridge call → POST `/api/watch/folders`.
  - Docker, no folders: show the operator docs link prominently. User cannot self-serve here — they have to mount and rely on auto-discovery.

Out:
- Anything related to email/automation ingestion paths (separate work; these reuse `/api/uploads/*`).
- The flatten-document-model work (Stage 3).

## Open questions for the Stage 2 spec

- Where does the wizard live as a UI element? Modal? Dedicated `/welcome` route? In-page empty state on Documents/Folders?
- Do we want a single-folder onboarding ("just pick one to start") or guided multi-folder ("pick all your common categories")?
- What if a user disables every watched folder after onboarding — does the wizard re-trigger, or just show a "no active folders" empty state?
- Should we surface a one-time toast or banner explaining what changed for users upgrading from a pre-Stage-2 install? (Probably not given the no-real-users situation, but flag it.)

## Out-of-band reminders

- The original plan had four pending tasks: #134 (folder filter), #135 (upload-to-folder flow on macOS), #136 (Docker watched folders), #137 (deprecate direct upload). Stage 2 covers the spirit of #135 + #137. #134 is folded into Projects/Collections. #136 is Stage 1.
