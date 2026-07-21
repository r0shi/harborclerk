# UI Chrome and Information Architecture Refresh - Design Spec

**Date:** 2026-06-03
**Status:** Accepted for planning
**Scope:** Navigation, first-run routing, admin/settings consolidation, and the UX audit needed before promoting Search and Find All. This spec does not redesign every page.

## Overview

Harbor Clerk's current UI has accumulated several top-level surfaces: Ask, Research, Search, Documents, Folders, Observatory, Models, Integrations, API Keys, Health/System pages, and operational controls. For release, the top-level chrome should communicate the product's core workflow:

1. Add folders.
2. Search and browse the corpus.
3. Ask or research with citations.
4. Connect models, agents, and admin controls when needed.

The default landing page should become Search once the corpus exists. During onboarding or empty state, the app should drop the user into Folders because no other mode can be useful until a corpus exists.

## Goals

- Make Search the default working surface for a populated corpus.
- Send empty/onboarding users to Folders.
- Keep Documents, Ask, Research, Folders, and Observatory visible at top level for the initial release.
- Move Models, Integrations, API Keys, Health, Preferences, and low-level system controls into a unified Settings/Admin area.
- Pair Search promotion with a UI audit of the document list, document detail, result rows, citations, and chunk-context behavior.
- Keep Observatory visible because the visualizations are appealing and help the product feel tangible, even if they are not core productivity tooling.

## Non-goals

- Do not remove Ask or Research from top-level navigation for the initial release.
- Do not hide Observatory outright.
- Do not ship a full Collections/Projects system as part of the IA pass.
- Do not redesign every settings page in detail before the release-critical routing and grouping work lands.
- Do not turn the app into a landing page. The first screen should be a working surface.

## Proposed top-level navigation

Top-level:

- Search
- Documents
- Ask
- Research
- Folders
- Explore, pending the audit below
- Observatory
- Settings

Settings children:

- Models
- Integrations
- API Keys
- Status
- Diagnostics
- Preferences
- Security / Users, if present for the deployment

The exact labels can shift, but the important move is that model/admin/health pages stop competing with corpus work in the primary nav.

## Routing behavior

### First launch or empty corpus

If no active watched folder exists, or no indexed documents exist after setup, route the user to Folders. The empty state should make the next action obvious: add or choose a folder, then watch ingest progress.

### Populated corpus

Default route should be Search. Search is the strongest neutral starting point because it works for technical and less technical users, does not require model selection, and teaches the citation/result model before the user asks an LLM to synthesize.

### Existing deep links

Existing routes should keep working. Redirect only the app's default route and nav grouping. Do not break links to `/models`, `/integrations`, or `/admin/api-keys` during the transition; either keep aliases or redirect into Settings.

## Search promotion and related UX audit

Promoting Search means its current rough edges become first-impression issues. The audit should cover:

- Search result row density, scannability, and citation clarity.
- Chunk-first behavior: the user sees the matching chunk first, with a clear way to open the full document at that context.
- Document-deduped behavior in Find All mode, where appropriate.
- The document list expansion/turndown content compared with the document detail page.
- Existing Explorer UI patterns and whether any should be reused in Search Workbench.
- Whether metadata, summary state, email headers, folder labels, and ingestion status appear in the right place.
- How failed or partial ingestion appears in Documents, Search, and Status.
- Mobile/narrow-window behavior for filter rows and result rows.

The deliverable for this audit should be a short annotated plan before broad UI editing starts.

## Explorer placement

The existing Explorer tab is not just legacy navigation. It currently provides corpus discovery through people, places, organizations, topic clusters, timeline, related topics, entity-topic cross references, and filtered document sub-panes. That overlaps with Search and Documents, but it is a different mode:

- Search is targeted retrieval: "find the best matching passages" or "find all matches for this condition."
- Documents is corpus inventory and document inspection.
- Explorer is browse/discovery: "show me what is in this archive and let me pivot through facets."

Recommendation for the release IA:

- Do not fold Explorer wholesale into Search before the Search Workbench exists.
- Audit Explorer's UI elements and reuse the good ones where they fit: facet chips, topic/entity pivots, related-topic chips, filtered document sub-panes, compact result tables, and timeline-as-filter ideas.
- Keep Explorer as a candidate top-level page for release if nav space allows after admin pages move under Settings.
- If nav becomes too crowded, move Explorer under Observatory or Documents before deleting it. It is closer to corpus discovery than to system administration.
- Revisit consolidation after Search Workbench ships. At that point, decide whether Explorer remains a separate discovery surface, becomes a Search side-panel/discovery mode, or merges with Observatory.

This keeps the useful and fun parts of Explorer alive without overloading the default Search screen.

## Documents page decision points

The Documents page remains top-level, but it needs a tighter job:

- Browse the corpus by document.
- Inspect ingest state, summaries, metadata, and source details.
- Open the document detail view.
- Filter for failed, missing, or stale documents.

The current list turndown and detail page should be compared. Information should not be duplicated just because both surfaces happen to exist. The rough rule:

- List row and turndown: status, identity, summary preview, folder, date, top metadata, and quick actions.
- Detail page: full metadata, chunks/passages, citations, source path policy, related entities, email header detail, and operational actions.

## Ask and Research placement

Ask and Research stay top-level for now:

- Ask is useful for quick cited lookup and follow-up questions.
- Research is useful for structured, longer-running corpus exploration.
- They should not be the default landing surface because they depend on corpus health, model health, and user understanding of citations.

Future possibility: Ask and Research could become sub-modes under a broader "AI" or "Work" area, but that should wait until the release has real usage feedback.

## Observatory placement

Observatory remains visible. The name is acceptable for now. "Playground" is a possible alternative if future visualization work becomes more exploratory, but the current name has charm and does not block release.

Possible future additions should be evaluated separately:

- More interactive corpus map views.
- Topic clustering drill-down.
- Entity network exploration.
- Ingest performance visualizations.
- Search/retrieval quality visualizations.

This spec does not start that work.

## Settings and Status distinction

Settings should be the umbrella. Status should be the user-facing operational health area inside it.

- Status: "what needs attention and what can I do?"
- Diagnostics: logs, raw service health, advanced traces, and developer/operator detail.
- Preferences: app behavior and local options.
- Models: model download/selection/health.
- Integrations: MCP, CLI, OpenClaw, Codex, Claude Code, ChatGPT/Claude connector guidance.
- API Keys: key creation, scoping, audit links, and tool access disclosure.

## Release-blocking requirements

- Default route chooses Folders for empty/onboarding and Search for populated corpus.
- Top-level nav no longer exposes every admin/system page as a peer of Search/Documents.
- Existing routes remain reachable.
- Search page is visibly ready to be the default working surface.
- Status replaces low-level Health as the normal user-facing operational label.
- "Show logs in console" is removed if it is stale or misleading.

## Testing

- Frontend route tests for empty vs populated default route, if current test infrastructure supports it.
- Manual browser verification on initial setup, empty corpus, and populated corpus.
- Manual narrow-window verification for the nav and Search filters.
- Regression check that deep links to old admin pages still reach the right destination.

## Open questions

- Whether Settings should be a page with left-side subnav or a top-level route with cards. Recommendation: left-side subnav once inside Settings, because this is an operational app and predictable navigation matters.
- Whether API Keys belongs under Settings or Integrations. Recommendation: Settings primary, with Integrations linking into it when setup docs require key creation.
- Whether Observatory should be renamed before release. Recommendation: no.
