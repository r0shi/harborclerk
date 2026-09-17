# Acceptance run — 2026-09-17 — ix

- **Suite commit:** `10166d7`
- **Instance:** http://localhost:8100 · build `e500116` · macOS native
- **Machine:** Mac16,10, Apple M4, 32 GB · macOS 27.0
- **Corpus:** acceptance fixtures rendered into one watched folder (folder-scoped run); instance database as found
- **Model under test:** gpt-oss-20b · judge model: n/a · cloud spend: n/a
- **Run id:** `live16` · duration 344 s · 47 passed, 3 xfailed
- **Method:** `uv run pytest acceptance/ -m acceptance`, API tier only; folder-scoped; every search scoped; fresh captures

| Area | Result | Checks |
| --- | --- | --- |
| Startup and onboarding | pass | a1 ✓, a2 ✓, a3 ✓ |
| Ingest and Status | pass | b1 ✓, b2 ✓, b3 ✓, b4 ✓, b5 ✓, b6 ✓, b7 ✓, h2 ✓ |
| Search and Find All | partial | c1 ✓, c2 ✓, c3 ✓, c4 ✓, c5 ✓, c6 ✓, c7 ✓, c8 ✓, h1 ✓, h1c xfail, h1d xfail, h1b ✓ |
| Documents and citations | partial | d1 ✓, d2 ✓, d3 ✓, d4 ✓, d5 ✓, d6 xfail, d7 ✓ |
| Ask and Research | pass | e1 ✓, e2 ✓ |
| MCP and CLI | pass | f1[search] ✓, f1[read] ✓, f1[full] ✓, f2 ✓, f3 ✓, f4 ✓, f5 ✓, f6 ✓, f7 ✓ |
| API key scope and audit | pass | g1 ✓, g2 ✓, g3 ✓, g4 ✓, g5 ✓, g6 ✓, g7 ✓, g8 ✓, g9 ✓ |
| Recovery and backup docs | not run | native tier (Status UI, recovery actions, backup docs) |
| Release docs and boundaries | not run | docs review; manual |

## Failing checks

- none

## Skipped or expected to fail

- **d6** (xfailed): #646: API-key tier is enforced only in MCP; REST read routes admit any key
- **h1c** (xfailed): #621 follow-up (v0.9.2 known limitation): a deleted chunk is still readable by id for unscoped principals
- **h1d** (xfailed): #621 follow-up (v0.9.2 known limitation): a deleted chunk is still readable by id for unscoped principals
