# Agent Memory Eval Plan

This plan defines what Harbor Clerk needs to test before making stronger public
claims about agentic memory, especially with OpenClaw.

Safe release claim today:

> Harbor Clerk can act as a private, cited memory layer for agents through MCP
> and CLI.

Do not claim measurable productivity, success-rate, or quality improvement
until the eval results support it.

## Claim Ladder

| Level | Public claim | Required evidence |
| --- | --- | --- |
| 0 | Harbor Clerk exposes your local archive to agents through MCP and CLI, with citations. | Tool parity, setup docs, citation consistency, and boundary disclosure. |
| 1 | Harbor Clerk works with OpenClaw as a private, cited document memory layer. | Fresh setup test, at least one realistic task, successful source retrieval, troubleshooting notes. |
| 2 | Harbor Clerk helps OpenClaw workflows find, cite, and reuse facts from local archives. | Multiple task types, correct citations, logged successes and failures. |
| 3 | Harbor Clerk improves OpenClaw task success or reduces manual retrieval work. | With/without Harbor Clerk comparison, stable scoring, enough tasks to avoid anecdote. |

Initial release should target Level 0 by default. Level 1 is achievable quickly
if setup and smoke testing pass. Level 2 is the more valuable launch claim if
wall time stays reasonable. Level 3 should wait.

## Minimum Credible OpenClaw Eval

Goal: decide whether Harbor Clerk can be described as a working OpenClaw memory
layer, not whether it universally improves agent performance.

Shape:

- 10 to 15 tasks.
- OpenClaw primary.
- Codex and Claude Code secondary only if setup is fast.
- Use at least two corpora, ideally one contract-heavy corpus and one email or
  mixed-office corpus.
- Capture traces, tool calls, final answers, cited sources, and operator
  interventions.
- Include both success and failure examples in the result note.

Required task types:

- Find all relevant documents matching a fact pattern.
- Answer a contract question with cited passages.
- Trace an email thread, sender, recipient, or attachment context.
- Reuse local project or research memory across a multi-step task.
- Diagnose missing expected sources by checking ingest or scope state.

Scoring:

- **Task success**: pass, partial, fail.
- **Citation quality**: correct source, plausible but weak source, wrong or
  missing source.
- **Tool behavior**: chose useful Harbor Clerk tools, needed nudging, or ignored
  available tools.
- **Intervention count**: number of operator corrections required.
- **Boundary quality**: no unexpected absolute paths or full-corpus exposure.

Minimum pass criteria:

- The agent retrieves relevant Harbor Clerk sources when the task requires
  corpus facts.
- Final answers cite the right documents or passages.
- The workflow succeeds from documented setup steps.
- Failures are captured honestly and are not hidden in summary copy.

## Suggested Task Set

| ID | Corpus | Task | Expected signal |
| --- | --- | --- | --- |
| OC-01 | CUAD/contracts | Find all agreements that mention renewal, extension, or termination notice terms. | Uses `find-all`; cites multiple documents. |
| OC-02 | CUAD/contracts | Compare indemnification scope across three relevant contracts. | Searches, expands context, cites passages. |
| OC-03 | CUAD/contracts | Identify a contract with a worldwide territory clause and cite it. | Finds exact supporting passage. |
| OC-04 | Enron/email | Find messages involving a named person and summarize the thread with citations. | Uses email metadata and citations. |
| OC-05 | Enron/email | Trace who received a specific topic update and cite the message. | Uses sender/recipient fields. |
| OC-06 | Mixed synthetic | Find invoices or vendor notes related to a project and list source documents. | Enumerates matching docs. |
| OC-07 | Mixed synthetic | Answer a bilingual or OCR-heavy document question. | Handles OCR/language variance. |
| OC-08 | Project memory | Locate prior notes/specs for a feature and summarize open decisions. | Works as coding/research memory. |
| OC-09 | Project memory | Given a stale claim, verify whether local docs contradict it. | Uses search plus read/expand. |
| OC-10 | Scope/ingest | Explain why an expected document might not appear. | Checks scope or ingest status. |

Add five more variations if the first ten are too easy or if failures cluster
around one tool-selection pattern.

## Wall-Time Estimate

These are estimates until the first dry run is complete.

| Eval size | Evidence level | Wall time | Operator time | Notes |
| --- | --- | --- | --- | --- |
| Setup smoke | Level 1 candidate | 2-4 hours | 1-2 hours | Fresh key, CLI/MCP setup, one OpenClaw task, troubleshooting notes. |
| Minimum credible | Level 2 candidate | 1-2 days if corpora are already ingested; 2-3 days if ingestion must be rebuilt. | 6-10 hours | 10-15 OpenClaw tasks, trace capture, manual scoring. |
| Fuller comparative | Stronger Level 2, maybe early Level 3 | 4-7 days | 18-30 hours | OpenClaw with/without Harbor Clerk, plus Codex or Claude Code secondary checks. |
| Outcome claim | Level 3 | 7-14+ days | 30+ hours | More tasks, repeated runs, stronger scoring, and cleaner statistical caution. |

If release timing is tight, run setup smoke plus the minimum credible eval and
publish only the claim level the evidence supports.

## Runbook

1. Start with a clean Harbor Clerk build and a known corpus state.
2. Create a dedicated OpenClaw API key with explicit scope, rate limits, and
   expiry.
3. Verify CLI access:

```bash
harbor-clerk search "contract renewal" --json
harbor-clerk find-all "renewal" --presentation full --json
```

4. Copy `skills/harbor-clerk/SKILL.md` into the OpenClaw skill location.
5. Run OC-01 as a dry run. Fix setup problems before scoring.
6. Run tasks one at a time, saving:
   - prompt,
   - trace/tool log,
   - final answer,
   - cited sources,
   - manual score,
   - operator interventions,
   - failure notes.
7. Summarize results in a dated report with commit hash, corpus state, and
   public-claim recommendation.

## Result Template

```markdown
# Agent Memory Eval Results - YYYY-MM-DD

- Harbor Clerk commit:
- OpenClaw version:
- Corpus:
- API key scope:
- Surface: CLI / MCP

| Task | Success | Citation quality | Tool behavior | Interventions | Notes |
| --- | --- | --- | --- | --- | --- |
| OC-01 | pass/partial/fail | correct/weak/wrong | good/nudged/ignored | 0 | |

## Supported Claim

Recommended public claim:

## Failures To Disclose

## Follow-Ups
```

## Release Decision

OpenClaw can be mentioned in release copy at Level 0 with current documented
tool parity and setup guidance. Promote to Level 1 only after a clean smoke
test. Promote to Level 2 only after the minimum credible eval produces multiple
successful, cited traces across task types.
