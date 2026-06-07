# Evaluation and release claims

Harbor Clerk has evaluation tooling for retrieval quality, cited answers, and
agent-memory workflows. The current public claim should be evidence-backed but
conservative:

> Harbor Clerk provides local document search, cited local AI answers, and
> controlled MCP/CLI access for external models and agent harnesses.

It is reasonable to say curated local AI models can be useful over local
document corpora. It is not reasonable to claim frontier-model parity,
complete legal review, or proven agent productivity gains without current
results that support those stronger claims.

## What is evaluated

The eval harness under [`scripts/test_corpora/`](../scripts/test_corpora/)
supports three related views of quality:

- **Retrieval eval**: whether Harbor Clerk search retrieves the documents and
  passages that should ground an answer.
- **Answer eval**: whether a model using Harbor Clerk tools answers correctly,
  with grounded citations and adequate completeness.
- **Agent-memory eval**: whether external agent harnesses can use Harbor Clerk
  as a cited memory layer through MCP or the CLI.

The harness writes artifacts under the configured test-corpora workdir, commonly
`~/Library/Application Support/Harbor Clerk/test-corpora/`. Reports should
include the Harbor Clerk commit, corpus, model, run label, scoring method, and
whether captures were reused or refreshed.

## Current claim posture

Safe claims:

- Harbor Clerk combines OCR, full-text search, semantic search, reranking, and
  citations in a local-first document system.
- Curated local AI models can produce useful cited answers for some document
  tasks.
- Quality varies by model, corpus, and task.
- Cloud LLMs connected through MCP can use Harbor Clerk as a cited retrieval
  tool.
- CLI-capable local agents can search and read Harbor Clerk through the same
  scoped API-key model used by MCP.
- Citations let users verify important outputs against source documents.

Conditional claims:

- Harbor Clerk can be positioned for research, legal, operations, and other
  document-heavy teams when examples and limitations are nearby.
- "Enterprise-grade retrieval tools" is acceptable if defined concretely as
  OCR, hybrid search, embeddings, reranking, citations, scoped API keys, audit
  logs, MCP, and CLI access.
- "Easy Mac setup" is acceptable for the native app path, but advanced MCP,
  CLI, cloud, Docker, and agent workflows still require operator judgment.

Avoid for now:

- Frontier-model parity.
- Fully automated office assistant.
- Guaranteed complete eDiscovery, contract review, or compliance review.
- Proven OpenClaw or agent productivity improvement.
- Numeric benchmark claims in README or launch copy unless they come from a
  current, reproducible run.

## Local model quality

Local models are a real feature, not just a demo. The strongest curated models
can answer useful questions with citations over contracts and structured
research-style corpora. They are still weaker than frontier cloud models on
broad synthesis, long-tail completeness, and messy email enumeration.

Use qualitative tiers in user-facing UI and docs:

- Best local research.
- Balanced local research.
- Lightweight lookup.
- Smallest usable.

If numeric scores are published, keep them in dated eval reports with corpus,
commit, model, and methodology details.

## Cloud model boundary

Cloud LLMs are part of the release story through MCP and connector-style access:
they can call scoped Harbor Clerk tools and receive cited snippets. Direct
provider-backed Ask/Research inside the Harbor Clerk app is a different surface
and should remain a fast-follow unless the checklist in
[Cloud model boundaries](cloud-model-boundaries.md) is satisfied.

This keeps the public claim honest: local AI runs locally, MCP/cloud connectors
receive scoped tool responses, and future in-app cloud mode would send selected
prompt/context to the chosen provider.

## Agent memory and OpenClaw

The current release-safe agent claim is:

> Harbor Clerk can act as a private, cited memory layer for agents through MCP
> and CLI.

OpenClaw is worth testing because it is a recognizable agentic harness and
fits Harbor Clerk's CLI/MCP surface. Stronger public claims should follow the
claim ladder and wall-time estimate in [Agent memory eval plan](agent-memory-eval.md).

Do not claim Harbor Clerk improves OpenClaw outcomes until the eval shows
successful cited traces across multiple task types, and ideally a with/without
comparison for outcome improvement.

## Known limitations

- Local AI quality varies by model, corpus, prompt, and task.
- Messy email corpora and exhaustive "find every instance" questions remain
  difficult, especially when completeness matters.
- OCR quality depends on scan quality, document layout, and installed language
  packs.
- Citations ground answers, but users should still verify important outputs.
- Harbor Clerk is local-first and single-tenant, not a hosted multi-tenant
  enterprise platform.
- Advanced integrations such as MCP, CLI, cloud LLMs, OpenClaw, and Docker
  require operator setup.
- Direct cloud-backed Ask/Research inside the app is not part of the initial
  release unless citation preservation, provider-key storage, disclosure, path
  policy, and failure-mode tests are stable.
- Backup and restore are documented but not yet a polished in-app workflow.
- The verifier/revision loop should not be marketed as a default quality
  improvement. Display-only citation support may become useful, but it should
  be framed as a grounding check, not an answer-correctness score.

## Reproducing evals

Start with the destructive warning in
[`scripts/test_corpora/README.md`](../scripts/test_corpora/README.md) and the
runbooks in [`scripts/test_corpora/runbooks/`](../scripts/test_corpora/runbooks/).
The sweep tooling can wipe and re-ingest corpora, so do not point it at a
Harbor Clerk instance containing documents you care about.

Useful entry points:

- [`scripts/test_corpora/README.md`](../scripts/test_corpora/README.md): setup,
  sweep modes, and safety notes.
- [`scripts/test_corpora/RUNBOOK.md`](../scripts/test_corpora/RUNBOOK.md):
  operational runbook for the corpus sweep.
- [`scripts/test_corpora/runbooks/single.md`](../scripts/test_corpora/runbooks/single.md):
  single-machine wall-time plan.
- [`scripts/test_corpora/runbooks/parallel-twins.md`](../scripts/test_corpora/runbooks/parallel-twins.md):
  split-machine wall-time plan.
- [`docs/agent-memory-eval.md`](agent-memory-eval.md): OpenClaw and agent-memory
  claim ladder.

Verifier validation runs should enable `research_verifier_enabled` while
leaving `research_verifier_revision_enabled` off. The sweep harness preserves
per-citation verifier verdicts in response artifacts and aggregates them in
`metrics.csv` so the release decision can be made from observed grounding
signal quality rather than from the existence of the mechanism alone.
Use `scripts.test_corpora.runner.verifier_report` to turn those columns into a
Markdown/JSON validation artifact for the citation-support default-on decision.

Public reports should summarize results instead of dumping raw partial sweeps
into the README. Raw artifacts are useful for technical readers, but the
release page should explain what was tested, what passed, what failed, and
which claims the evidence supports.
