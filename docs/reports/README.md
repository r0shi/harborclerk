# Reports

Dated outputs of loops and field work: eval runs, acceptance passes, model
surveys, benchmark results, field reports. One file per run, named
`YYYY-MM-DD-<kind>-<slug>.md`. Reports are evidence; the public claim ladder
and known limitations they support live in [`docs/evaluation.md`](../evaluation.md),
and numeric results stay here rather than in the README.

Every loop-generated report opens with a header block so a reader can
reproduce it or discount it:

- Commit, machine (chip, RAM), deployment (macOS native or Docker), OS
- Corpus, and whether the database was fresh or upgraded
- Models under test, judge model, run id, eval workdir
- Cloud spend in USD and the cap in force
- Method: scoring, and whether captures were reused or refreshed

Reports summarise. Raw artifacts (responses, verdicts, `metrics.csv`) stay in
the eval workdir on the machine that produced them.

Design specs and implementation plans are not reports; they live under
[`docs/superpowers/`](../superpowers/). Decisions live in [`docs/adr/`](../adr/).
