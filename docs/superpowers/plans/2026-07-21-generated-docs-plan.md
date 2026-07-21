# Generated Documentation — Implementation Plan

**Date:** 2026-07-21
**Tracks:** #539 (architecture rewrite), momentum task 7 (CLI/MCP parity)
**Estimated:** 14–18 hours across 5 PRs

## Why

Docs about code drift silently and are then trusted with authority. An audit of
`docs/architecture.md` (#539) found the embedding model, queue topology, OCR
path, macOS service list, ports, auth semantics, and roughly half the data model
all wrong — plus a real production bug (#537/#538) hiding behind a diagram that
faithfully mirrored a broken compose file.

The lesson from that audit: **enumerations rot continuously and silently;
structure rots occasionally and traceably.** So generate every list, table,
count, and name from source; hand-write prose and diagrams.

The forcing function is a CI job that regenerates and fails on diff — the same
pattern as `ruff format --check`. Docs then cannot drift.

## Feasibility (verified 2026-07-21)

| Source | Access | Result |
|---|---|---|
| MCP tools | `harbor_clerk.mcp_server.mcp` → tool manager | 19 tools |
| REST endpoints | `harbor_clerk.api.app.app.openapi()` | 118 paths |
| DB tables | `harbor_clerk.models.base.Base.metadata` | 29 tables |
| Pipeline stages | `worker.pipeline.STAGE_CONFIG` | 7 stages |
| Queues | `worker.entry.QUEUE_STAGES` | io/cpu/llm |
| CLI subcommands | `cli.main.build_parser()` | argparse tree |
| Compose services | `docker-compose.yml` | 12 services |

All import cleanly without a database connection.

## Design

```
scripts/gen_docs/
  __main__.py          # python -m scripts.gen_docs [--check]
  blocks.py            # marker-block find/replace
  registry.py          # generator name -> callable
  generators/
    mcp_tools.py  rest_endpoints.py  db_tables.py
    pipeline.py   compose.py         cli_commands.py
```

**Marker blocks** let narrative wrap generated content in place:

```markdown
<!-- BEGIN GENERATED: mcp-tools -->
| Tool | Description |
...
<!-- END GENERATED: mcp-tools -->
```

**Runner contract**
- `python -m scripts.gen_docs` rewrites every registered block in every target.
- `--check` exits non-zero and prints a diff when any block is out of date.
- Unknown block name, or a block present in a file but absent from the registry,
  is an error — prevents silent no-ops.

**CI:** a `docs-generated` job runs `--check`. Change a tool and forget to
regenerate → red build. The tool-surface diff also becomes explicitly
reviewable, which delivers the CLI/MCP parity check as a side effect.

## Sequencing

### PR 1 — Generator core + MCP tools (~4h)
Block machinery, runner, `--check`, registry, first generator, README's 19-tool
table converted to a generated block, CI job, unit tests including a
"regenerating is idempotent" test and a "--check catches drift" test.

**Exit:** `python -m scripts.gen_docs --check` passes; deliberately editing a
generated block makes it fail.

### PR 2 — REST, DB, pipeline, compose, CLI generators (~5h)
Five more generators against the same machinery. REST table from OpenAPI (group
by tag/prefix; exclude internals). DB tables from metadata (name + purpose +
key columns). Pipeline from `STAGE_CONFIG` + `QUEUE_STAGES` — this one would
have caught the #537 bug. Compose from the YAML. CLI from `build_parser()`.

**Exit:** all six generators registered and passing `--check`.

### PR 3 — CLI/MCP parity check (~1h)
Assert every MCP tool has a CLI subcommand and vice versa, modulo an explicit
allowlist of intentional asymmetries. Closes momentum task 7.

### PR 4 — architecture.md rewrite (~4h)
Rewrite using generated blocks; fix every HIGH finding in #539; add the
subsystems currently missing entirely (Research, chat/LLM orchestration,
secrets, topics, language packs, metadata extractors, markdown extraction
branch); correct the auth section including OAuth 2.1; remove the staleness
banner. Keep and hand-maintain the mermaid diagrams — with the OCR→Tika edge
deleted and the summarize de-gating corrected.

**Exit:** #539 closed; banner gone.

### PR 5 — CLAUDE.md restructure, batches 1–2 (~2h)
Apply the approved constitution: preamble with the authority rule, identity,
non-negotiables (including the decomposed never-modify / copy-only-where-required
pair), working agreement, review policy with the unprimed-reviewer rule and the
new-subsystem trigger, and the knowledge-routing section. Delete the architecture
cluster, ingestion pipeline, ingestion flow, and retrieval sections. Create
`src/harbor_clerk/CLAUDE.md` with the audit-verified invariants.

Batches 3–5 of the restructure are **deliberately excluded** — the owner asked to
walk those through interactively.

## Decisions taken without consultation

Recorded because the owner is away; all are reversible.

1. **`scripts/gen_docs/` over a `tools/` or root script** — matches the existing
   `scripts/test_corpora/` precedent and keeps it out of the shipped package.
2. **Marker blocks over whole generated files** — narrative and generated
   content need to interleave in README and architecture.md.
3. **`--check` in CI rather than auto-commit** — auto-committing from CI hides
   contract changes that deserve review.
4. **Generated tables kept terse** — name + one-line purpose. Full detail stays
   in the source; the doc is an index, not a mirror.
5. **Diagrams stay hand-written.** Generated mermaid would lose editorial
   judgment (e.g. dashing the conditional OCR node) and churn noisily.

## Risks

- **Import side effects.** Generators import app modules; if one starts
  requiring a live DB, the CI job breaks confusingly. Mitigation: generators
  import lazily and the runner reports which generator failed.
- **OpenAPI churn.** 118 paths is a lot of table. Mitigation: group and filter to
  the documented public surface rather than dumping everything.
- **Over-generation.** Not everything benefits; prose explaining *why* a
  subsystem exists must stay hand-written.
