# Semantic sweep diagnostic: would a concept-query sweep have reached the clauses the frontier models missed?

Date: 2026-10-15. Instance: the disposable Mac mini at http://localhost:8100 (build a40dbe9), re-ingested with the CUAD
sample through the harness's guarded path (`_refuse_to_wipe_a_real_instance` -> `_ingest_corpus`): 80 documents,
6408 chunks, all embedded (768-dim, unit-norm). No cloud call was made; the analysis used the local embedder
(:8101), the local reranker (:8201) and read-only SELECTs on the instance's Postgres.

## Method

For each of the six keyed list questions (cuad-key-15..20) I wrote 4-5 concept queries a model might send (the clause type in plain words, a legal phrasing, a layman's phrasing, the exact CUAD category name), embedded them with `task=query`, and computed the exact cosine similarity against every chunk embedding pulled from Postgres (no ANN index involved). Documents were ranked by their best chunk. For every positive in the key I report the document's rank among the 80, its best chunk's rank among the 6,408 chunks, and the rank of the chunk that actually holds the CUAD-annotated clause (located by matching the span text from `master_clauses.csv` to chunk text by word 4-gram coverage; coverage is reported and is 1.0 for most items). Three reranker configurations were measured with the best concept query (the one that recovers all positives soonest under cosine): (A) the per-document best cosine chunk, all 80 documents, through the reranker; (B) the top-200 cosine chunks through the reranker, then per-document best; and (C) an upper bound: every one of the 6,408 chunks through the reranker (about 7 minutes per query), per-document best. For (C) two queries were run per concept-only question so a union could be measured. Finally, the two frontier runs' search-type tool calls (kb_find_all / kb_search / kb_batch_search) were replayed with their original arguments against the re-ingested instance to see whether each missed positive was ever returned, and whether the returned chunk was the annotated clause; 'listed' uses the harness's own scorer (`answer_key.named_contracts`).

Caveats. The concept queries are mine, not the models'; I chose them before seeing results except that q4 of key-16 ('damages the reputation or goodwill') is close to the Innerscope wording, which is flagged where it matters. The replay runs against a fresh ingest with new ids, so calls scoped to old doc_ids were not replayed (they were follow-ups inside already-surfaced documents). Transcript result summaries are capped at 600 characters, so what the model actually saw is inferred from the replay.

## Headline numbers

Documents a sweep must read, in rank order, to recover every positive (k_all) and every item both models missed (k_miss), out of 80. 'Best F1' is the best precision/recall trade-off anywhere on the curve, i.e. the best any cutoff could do. The last two columns are what the models actually scored on the same question (harness scorer), with the number of documents whose chunk text they saw.

| Question | Positives | Exact cosine, best query: k_all (prec) / k_miss / best F1 | Per-doc best chunk + reranker (the proposed design): k_all / k_miss / best F1 | Top-200 chunks + reranker: k_all / k_miss / best F1 | Every chunk reranked (upper bound), per query: k_all / k_miss / best F1 | Fable F1 (docs seen) | Astra F1 (docs seen) |
|---|---|---|---|---|---|---|---|
| cuad-key-15 No-Solicit Of Customers | 8 | 39 (0.21) / 30 / 0.38 | 72 (0.11) / 62 / 0.35 | 50 (0.16) / 49 / 0.29 | q0: 56 (0.14) / 43 / 0.42 ; q1: 51 (0.16) / 51 / 0.42 | 0.62 (33) | 0.82 (50) |
| cuad-key-16 Non-Disparagement | 9 | 60 (0.15) / 51 / 0.36 | 73 (0.12) / 65 / 0.22 | not in pool / 48 / 0.28 | q0: 55 (0.16) / 55 / 0.57 ; q4: 53 (0.17) / 41 / 0.29 | 0.67 (30) | 0.67 (39) |
| cuad-key-17 Irrevocable Or Perpetual License | 10 | 46 (0.22) / - / 0.56 | 62 (0.16) / - / 0.29 | not in pool / - / 0.75 | not run (no concept-only misses) | 1.00 (38) | 1.00 (42) |
| cuad-key-18 Rofr/Rofo/Rofn | 16 | 53 (0.3) / 53 / 0.59 | 77 (0.21) / 77 / 0.53 | 55 (0.29) / 55 / 0.52 | q0: 56 (0.29) / 56 / 0.76 ; q1: 67 (0.24) / 41 / 0.49 | 0.67 (37) | 0.74 (57) |
| cuad-key-19 Insurance | 21 | 35 (0.6) / - / 0.85 | 54 (0.39) / - / 0.79 | 43 (0.49) / - / 0.84 | not run (no concept-only misses) | 1.00 (40) | 0.98 (44) |
| cuad-key-20 Governing Law = Delaware | 7 | 54 (0.13) / - / 0.71 | 63 (0.11) / - / 0.8 | 8 (0.88) / - / 0.93 | not run (no concept-only misses) | 1.00 (51) | 1.00 (38) |

Reading of the table: on the three concept-only questions (15, 16, 18) no ranking puts the positives at the top. Exact cosine over every chunk needs 39, 60 and 53 documents read to reach full recall (precision 0.21, 0.15, 0.30), and 30, 51 and 53 to reach the items both models missed. The design under consideration (per-document best chunk, then the reranker) is worse than raw cosine on every question: 72, 73 and 77 documents, because the per-document best cosine chunk is almost never the clause, so the reranker is handed the wrong passage and demotes the document. Even the upper bound that no sweep would run, every one of 6,408 chunks through the reranker, needs 51-67 documents for full recall and 41-56 for the both-missed items. The two keyword-rich questions (19, 20) behave differently: the reranker over the top-200 cosine chunks gets Delaware governing law to 7/7 at k=8 (precision 0.88) and insurance to 20/21 at k=30.

## cuad-key-15: No-Solicit Of Customers (8 positives; literal keyword `solicit`)

Concept queries (doc-level k to recover all positives under exact cosine; k to recover all items both models missed):

- q0 (best): "clause prohibiting solicitation of the other party's customers" -> k_all=39, k_missed=30, mean positive doc rank 19.6
- q1: "shall not solicit, contact or divert the customers or clients of the other party" -> k_all=49, k_missed=38, mean positive doc rank 21.8
- q2: "promise not to go after the other side's customers" -> k_all=71, k_missed=36, mean positive doc rank 29.1
- q3: "No-Solicit Of Customers" -> k_all=43, k_missed=13, mean positive doc rank 15.9
- q4: "non-solicitation of customers covenant" -> k_all=63, k_missed=35, mean positive doc rank 22.1

| Positive | Missed by | Keyword in span / doc | Span cov. | Cosine doc rank (best q) | Best-chunk rank | Annotated-chunk rank | Min doc rank over queries | Rerank A doc rank | Rerank B doc rank | Full rerank q0 doc rank (annot. chunk rank) | Full rerank q1 doc rank (annot. chunk rank) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| DigitalCinemaDestinationsCorp |  | yes / yes | 0.781 | 6 | 7 | 7 (=best) | 2 | 3 | 6 | 11 (15) | 10 (11) |
| **GentechHoldingsInc** | both | no / no | 1.0 | 18 | 27 | 278 | 13 | 49 | 20 | 43 (516) | 22 (31) |
| InvendaCorp |  | yes / yes | 1.0 | 29 | 51 | 232 | 6 | 72 | 34 | 26 (972) | 48 (1288) |
| LegacyEducationAllianceInc |  | yes / yes | 0.669 | 15 | 21 | 1627 | 15 | 9 | 16 | 7 (8) | 3 (3) |
| **RandWorldwideInc** | both | no / no | 1.0 | 30 | 54 | 1994 | 3 | 62 | 49 | 29 (2104) | 51 (1618) |
| **RitterPharmaceuticalsInc** | both | no / yes | 1.0 | 9 | 10 | 2699 | 2 | 5 | 9 | 9 (2069) | 11 (1291) |
| SteelVaultCorp |  | yes / yes | 1.0 | 39 | 75 | 952 | 30 | 66 | 50 | 4 (5) | 4 (4) |
| **UsioInc** | Fable | no / no | 1.0 | 11 | 14 | 537 | 4 | 63 | 46 | 56 (227) | 33 (56) |

Ranks are 1-based; document ranks are out of 80, chunk ranks out of 6,408. Rerank A = per-document best cosine chunk (80 passages) reranked; Rerank B = top-200 cosine chunks reranked, per-document best ('>N' = the document had no chunk in the top 200, N docs in pool); Full rerank = every chunk reranked, per-document best, with the annotated clause chunk's rank among all chunks in parentheses.

Recall-at-k over documents read in rank order (positives 8; items both models missed 3):

| Method | recall@10 | @20 | @30 | @40 | @50 | k for all positives (precision there) | k for all both-missed items | best F1 (k, recall) |
|---|---|---|---|---|---|---|---|---|
| exact cosine, best query | 2/8 | 5 | 7 | 8 | 8 | 39 (0.21) | 30 | 0.38 (k=18, 5/8) |
| exact cosine, max over all concept queries | 3/8 | 4 | 5 | 5 | 8 | 46 (0.17) | 41 | 0.38 (k=13, 4/8) |
| rerank A (per-doc best chunk) | 3/8 | 3 | 3 | 3 | 4 | 72 (0.11) | 62 | 0.35 (k=9, 3/8) |
| rerank B (top-200 chunks) | 2/8 | 4 | 4 | 5 | 8 | 50 (0.16) | 49 | 0.29 (k=20, 4/8) |
| full rerank, q0 | 3/8 | 4 | 6 | 6 | 7 | 56 (0.14) | 43 | 0.42 (k=11, 4/8) |
| full rerank, q1 | 3/8 | 4 | 5 | 6 | 7 | 51 (0.16) | 51 | 0.42 (k=11, 4/8) |
| full rerank, max over the two queries | 3/8 | 4 | 5 | 6 | 7 | 51 (0.16) | 51 | 0.42 (k=11, 4/8) |

Cutoff check, cosine (best query): positives' doc similarity min 0.8736 / median 0.8804; negatives' max 0.8991 / median 0.8717; 31 negatives score at or above the weakest positive. Spread of all 80 doc scores: 0.8285-0.8991.
Cutoff check, full rerank q0: positives' doc score min 0.0182 / median 0.0677; negatives' max 0.8010 / median 0.0260; 48 negatives score at or above the weakest positive.
Cutoff check, full rerank q1: positives' doc score min 0.0956 / median 0.3210; negatives' max 0.7890 / median 0.1305; 43 negatives score at or above the weakest positive.

What the models actually did (replayed against the re-ingested corpus):

| Model | Tool calls | Search calls replayed (skipped: scoped to old ids) | Read-tool calls | Docs whose chunk text the model saw | Docs seen title-only | Named (true positives / false positives) |
|---|---|---|---|---|---|---|
| claude-fable-5-1 | 7 | 5 (1) | 1 | 33 | 35 | 5 (4 / 1) |
| gpt-6-astra | 25 | 7 (3) | 5 | 50 | 0 | 9 (7 / 2) |

- claude-fable-5-1 did not list **GentechHoldingsInc**: annotated clause chunk returned with text, not listed.
- claude-fable-5-1 did not list **RandWorldwideInc**: returned title-only (find_all brief), never read.
- claude-fable-5-1 did not list **RitterPharmaceuticalsInc**: other chunk(s) of the doc returned with text, not listed.
- claude-fable-5-1 did not list **UsioInc**: returned title-only (find_all brief), never read.
- claude-fable-5-1 false positives: StampscomInc.
- gpt-6-astra did not list **RitterPharmaceuticalsInc**: other chunk(s) of the doc returned with text, not listed.
- gpt-6-astra false positives: StampscomInc, WaterNowInc.

## cuad-key-16: Non-Disparagement (9 positives; literal keyword `disparag`)

Concept queries (doc-level k to recover all positives under exact cosine; k to recover all items both models missed):

- q0: "non-disparagement clause" -> k_all=68, k_missed=68, mean positive doc rank 40.0
- q1: "shall not make any disparaging or derogatory statements about the other party" -> k_all=68, k_missed=62, mean positive doc rank 37.7
- q2: "promise not to say bad things about the other company or hurt its reputation" -> k_all=63, k_missed=63, mean positive doc rank 25.6
- q3: "Non-Disparagement" -> k_all=68, k_missed=60, mean positive doc rank 35.1
- q4 (best): "shall not do anything that damages the reputation or goodwill of the other party" -> k_all=60, k_missed=51, mean positive doc rank 32.7

| Positive | Missed by | Keyword in span / doc | Span cov. | Cosine doc rank (best q) | Best-chunk rank | Annotated-chunk rank | Min doc rank over queries | Rerank A doc rank | Rerank B doc rank | Full rerank q0 doc rank (annot. chunk rank) | Full rerank q4 doc rank (annot. chunk rank) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| BerkshireHillsBancorpInc |  | yes / yes | 0.778 | 60 | 267 | 2160 | 47 | 53 | >52 | 10 (23) | 53 (3805) |
| CybergyHoldingsInc |  | no / yes | 0.98 | 58 | 248 | 2185 | 14 | 73 | >52 | 4 (6) | 34 (94) |
| DigitalCinemaDestinationsCorp |  | no / yes | 1.0 | 36 | 84 | 705 | 27 | 27 | 7 | 14 (1831) | 11 (1180) |
| **InnerscopeHearingTechnologiesInc** | both | no / no | 1.0 | 51 | 172 | 1321 | 43 | 65 | 48 | 55 (2951) | 3 (6) |
| PapaJohnsInternationalInc |  | yes / yes | 1.0 | 16 | 23 | 1320 | 5 | 23 | 20 | 3 (14) | 37 (580) |
| PelicanDeliversInc |  | yes / yes | 1.0 | 1 | 1 | 16 | 1 | 34 | 10 | 1 (1) | 21 (31) |
| **PerformanceSportsBrandsInc** | both | no / no | 1.0 | 2 | 2 | 43 | 2 | 63 | 19 | 42 (251) | 41 (82) |
| **SmartRxSystemsInc** | both | no / no | 1.0 | 20 | 30 | 1624 | 4 | 35 | 36 | 16 (506) | 32 (578) |
| StaarSurgicalCompany |  | yes / yes | 1.0 | 50 | 171 | 3311 | 1 | 45 | 43 | 5 (7) | 52 (1109) |

Ranks are 1-based; document ranks are out of 80, chunk ranks out of 6,408. Rerank A = per-document best cosine chunk (80 passages) reranked; Rerank B = top-200 cosine chunks reranked, per-document best ('>N' = the document had no chunk in the top 200, N docs in pool); Full rerank = every chunk reranked, per-document best, with the annotated clause chunk's rank among all chunks in parentheses.

Recall-at-k over documents read in rank order (positives 9; items both models missed 3):

| Method | recall@10 | @20 | @30 | @40 | @50 | k for all positives (precision there) | k for all both-missed items | best F1 (k, recall) |
|---|---|---|---|---|---|---|---|---|
| exact cosine, best query | 2/9 | 4 | 4 | 5 | 6 | 60 (0.15) | 51 | 0.36 (k=2, 2/9) |
| exact cosine, max over all concept queries | 3/9 | 4 | 5 | 5 | 6 | 63 (0.14) | 57 | 0.4 (k=6, 3/9) |
| rerank A (per-doc best chunk) | 0/9 | 0 | 2 | 4 | 5 | 73 (0.12) | 65 | 0.22 (k=73, 9/9) |
| rerank B (top-200 chunks) | 2/9 | 4 | 4 | 5 | 7 | not within pool of 52 | 48 | 0.28 (k=20, 4/9) |
| full rerank, q0 | 5/9 | 7 | 7 | 7 | 8 | 55 (0.16) | 55 | 0.57 (k=5, 4/9) |
| full rerank, q4 | 1/9 | 2 | 3 | 6 | 7 | 53 (0.17) | 41 | 0.29 (k=53, 9/9) |
| full rerank, max over the two queries | 3/9 | 6 | 6 | 7 | 8 | 54 (0.17) | 43 | 0.43 (k=19, 6/9) |

Cutoff check, cosine (best query): positives' doc similarity min 0.8727 / median 0.8814; negatives' max 0.8967 / median 0.8808; 51 negatives score at or above the weakest positive. Spread of all 80 doc scores: 0.8065-0.9119.
Cutoff check, full rerank q0: positives' doc score min 0.0027 / median 0.0435; negatives' max 0.8972 / median 0.0036; 46 negatives score at or above the weakest positive.
Cutoff check, full rerank q4: positives' doc score min 0.0787 / median 0.1634; negatives' max 0.8851 / median 0.1223; 44 negatives score at or above the weakest positive.

What the models actually did (replayed against the re-ingested corpus):

| Model | Tool calls | Search calls replayed (skipped: scoped to old ids) | Read-tool calls | Docs whose chunk text the model saw | Docs seen title-only | Named (true positives / false positives) |
|---|---|---|---|---|---|---|
| claude-fable-5-1 | 9 | 8 (0) | 0 | 30 | 40 | 9 (6 / 3) |
| gpt-6-astra | 21 | 8 (0) | 2 | 39 | 0 | 9 (6 / 3) |

- claude-fable-5-1 did not list **InnerscopeHearingTechnologiesInc**: annotated clause chunk returned with text, not listed.
- claude-fable-5-1 did not list **PerformanceSportsBrandsInc**: returned title-only (find_all brief), never read.
- claude-fable-5-1 did not list **SmartRxSystemsInc**: returned title-only (find_all brief), never read.
- claude-fable-5-1 false positives: BizzingoInc, CreditcardscomInc, RandWorldwideInc.
- gpt-6-astra did not list **InnerscopeHearingTechnologiesInc**: never returned.
- gpt-6-astra did not list **PerformanceSportsBrandsInc**: other chunk(s) of the doc returned with text, not listed.
- gpt-6-astra did not list **SmartRxSystemsInc**: never returned.
- gpt-6-astra false positives: BizzingoInc, CreditcardscomInc, RandWorldwideInc.

## cuad-key-17: Irrevocable Or Perpetual License (10 positives; literal keyword `perpetual`)

Concept queries (doc-level k to recover all positives under exact cosine; k to recover all items both models missed):

- q0: "irrevocable or perpetual license grant" -> k_all=48, k_missed=-, mean positive doc rank 16.5
- q1: "hereby grants a perpetual, irrevocable, royalty-free license" -> k_all=52, k_missed=-, mean positive doc rank 21.4
- q2: "a license that lasts forever and cannot be taken back" -> k_all=49, k_missed=-, mean positive doc rank 20.8
- q3 (best): "Irrevocable Or Perpetual License" -> k_all=46, k_missed=-, mean positive doc rank 15.7

| Positive | Missed by | Keyword in span / doc | Span cov. | Cosine doc rank (best q) | Best-chunk rank | Annotated-chunk rank | Min doc rank over queries | Rerank A doc rank | Rerank B doc rank |
|---|---|---|---|---|---|---|---|---|---|
| AimmuneTherapeuticsInc |  | yes / yes | 1.0 | 4 | 7 | 29 | 2 | 23 | 2 |
| ClickstreamCorp |  | yes / yes | 1.0 | 46 | 339 | 547 | 20 | 61 | >32 |
| CoherusBiosciencesInc |  | yes / yes | 1.0 | 5 | 9 | 168 | 1 | 8 | 5 |
| FuelcellEnergyInc |  | yes / yes | 1.0 | 10 | 23 | 141 | 8 | 14 | 6 |
| LifewayFoodsInc |  | yes / yes | 1.0 | 30 | 180 | 329 | 26 | 37 | 23 |
| LiquidmetalTechnologiesInc |  | yes / yes | 1.0 | 15 | 82 | 273 | 15 | 57 | 19 |
| NlsPharmaceuticsLtd |  | yes / yes | 1.0 | 3 | 5 | 51 | 3 | 30 | 1 |
| PapaJohnsInternationalInc |  | yes / yes | 1.0 | 14 | 78 | 78 (=best) | 1 | 1 | 4 |
| RevolutionMedicinesInc |  | yes / yes | 1.0 | 8 | 19 | 114 | 5 | 38 | 3 |
| ZogenixInc |  | yes / yes | 1.0 | 22 | 146 | 1170 | 22 | 62 | 31 |

Ranks are 1-based; document ranks are out of 80, chunk ranks out of 6,408. Rerank A = per-document best cosine chunk (80 passages) reranked; Rerank B = top-200 cosine chunks reranked, per-document best ('>N' = the document had no chunk in the top 200, N docs in pool); Full rerank = every chunk reranked, per-document best, with the annotated clause chunk's rank among all chunks in parentheses.

Recall-at-k over documents read in rank order (positives 10; items both models missed 0):

| Method | recall@10 | @20 | @30 | @40 | @50 | k for all positives (precision there) | k for all both-missed items | best F1 (k, recall) |
|---|---|---|---|---|---|---|---|---|
| exact cosine, best query | 5/10 | 7 | 9 | 9 | 10 | 46 (0.22) | n/a | 0.56 (k=15, 7/10) |
| exact cosine, max over all concept queries | 5/10 | 7 | 8 | 9 | 10 | 44 (0.23) | n/a | 0.55 (k=12, 6/10) |
| rerank A (per-doc best chunk) | 2/10 | 3 | 5 | 7 | 7 | 62 (0.16) | n/a | 0.29 (k=38, 7/10) |
| rerank B (top-200 chunks) | 6/10 | 7 | 8 | 9 | 9 | not within pool of 32 | n/a | 0.75 (k=6, 6/10) |

Cutoff check, cosine (best query): positives' doc similarity min 0.8640 / median 0.8826; negatives' max 0.9007 / median 0.8647; 37 negatives score at or above the weakest positive. Spread of all 80 doc scores: 0.8152-0.9007.

What the models actually did (replayed against the re-ingested corpus):

| Model | Tool calls | Search calls replayed (skipped: scoped to old ids) | Read-tool calls | Docs whose chunk text the model saw | Docs seen title-only | Named (true positives / false positives) |
|---|---|---|---|---|---|---|
| claude-fable-5-1 | 25 | 7 (17) | 0 | 38 | 0 | 10 (10 / 0) |
| gpt-6-astra | 24 | 9 (2) | 2 | 42 | 31 | 10 (10 / 0) |

- Every positive was listed by both models.

## cuad-key-18: Rofr/Rofo/Rofn (16 positives; literal keyword `first refusal`)

Concept queries (doc-level k to recover all positives under exact cosine; k to recover all items both models missed):

- q0: "right of first refusal, right of first offer or right of first negotiation" -> k_all=61, k_missed=51, mean positive doc rank 28.7
- q1 (best): "shall first offer the other party the opportunity to negotiate before offering to any third party" -> k_all=53, k_missed=53, mean positive doc rank 20.6
- q2: "the other side gets the first chance to buy or negotiate before anyone else does" -> k_all=54, k_missed=54, mean positive doc rank 27.8
- q3: "Rofr/Rofo/Rofn" -> k_all=65, k_missed=65, mean positive doc rank 33.3
- q4: "exclusive right to negotiate for a period before dealing with third parties" -> k_all=61, k_missed=45, mean positive doc rank 25.9

| Positive | Missed by | Keyword in span / doc | Span cov. | Cosine doc rank (best q) | Best-chunk rank | Annotated-chunk rank | Min doc rank over queries | Rerank A doc rank | Rerank B doc rank | Full rerank q0 doc rank (annot. chunk rank) | Full rerank q1 doc rank (annot. chunk rank) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **AimmuneTherapeuticsInc** | both | no / no | 0.881 | 7 | 9 | 3976 | 7 | 6 | 11 | 25 (918) | 7 (2721) |
| **ConformisInc** | both | no / no | 0.788 | 44 | 120 | 2674 | 19 | 11 | 12 | 51 (5619) | 18 (1256) |
| EdietsComInc |  | yes / yes | 1.0 | 8 | 11 | 5190 | 8 | 14 | 23 | 9 (16) | 38 (3844) |
| EmbarkComInc |  | no / yes | 0.682 | 24 | 40 | 88 | 3 | 32 | 31 | 3 (3) | 54 (240) |
| FuelcellEnergyInc |  | yes / yes | 1.0 | 1 | 1 | 4663 | 1 | 4 | 9 | 8 (4976) | 16 (6241) |
| IbioInc |  | no / no | 1.0 | 14 | 22 | 206 | 12 | 1 | 2 | 11 (19) | 2 (2) |
| LinkPlusCorp |  | no / no | 1.0 | 51 | 151 | 1782 | 21 | 40 | 42 | 7 (14) | 67 (910) |
| **LiquidmetalTechnologiesInc** | both | no / no | 1.0 | 26 | 45 | 2337 | 2 | 77 | 55 | 56 (1482) | 41 (4710) |
| MusclepharmCorp |  | yes / no | 1.0 | 18 | 28 | 1791 | 6 | 26 | 36 | 10 (17) | 62 (2156) |
| **NakedBrandGroupInc** | both | no / no | 1.0 | 53 | 161 | 161 (=best) | 4 | 5 | 10 | 30 (56) | 17 (27) |
| **PhasebioPharmaceuticalsInc** | both | no / no | 0.99 | 9 | 12 | 3149 | 5 | 42 | 15 | 13 (612) | 21 (1845) |
| RevolutionMedicinesInc |  | no / yes | 1.0 | 11 | 17 | 5601 | 2 | 3 | 1 | 4 (160) | 1 (5) |
| RitterPharmaceuticalsInc |  | yes / yes | 1.0 | 5 | 5 | 7 | 1 | 13 | 3 | 1 (1) | 5 (8) |
| **TubeMediaCorp** | Fable | yes / no | 1.0 | 15 | 23 | 973 | 13 | 33 | 39 | 6 (9) | 14 (2126) |
| UsioInc |  | yes / yes | 1.0 | 6 | 8 | 8 (=best) | 2 | 51 | 46 | 2 (5) | 66 (1268) |
| **WaterNowInc** | both | no / no | 1.0 | 38 | 97 | 1598 | 14 | 28 | 37 | 34 (1829) | 27 (59) |

Ranks are 1-based; document ranks are out of 80, chunk ranks out of 6,408. Rerank A = per-document best cosine chunk (80 passages) reranked; Rerank B = top-200 cosine chunks reranked, per-document best ('>N' = the document had no chunk in the top 200, N docs in pool); Full rerank = every chunk reranked, per-document best, with the annotated clause chunk's rank among all chunks in parentheses.

Recall-at-k over documents read in rank order (positives 16; items both models missed 6):

| Method | recall@10 | @20 | @30 | @40 | @50 | k for all positives (precision there) | k for all both-missed items | best F1 (k, recall) |
|---|---|---|---|---|---|---|---|---|
| exact cosine, best query | 6/16 | 10 | 12 | 13 | 14 | 53 (0.3) | 53 | 0.59 (k=18, 10/16) |
| exact cosine, max over all concept queries | 5/16 | 8 | 12 | 14 | 16 | 50 (0.32) | 50 | 0.57 (k=33, 14/16) |
| rerank A (per-doc best chunk) | 5/16 | 8 | 10 | 13 | 14 | 77 (0.21) | 77 | 0.53 (k=14, 8/16) |
| rerank B (top-200 chunks) | 5/16 | 8 | 9 | 13 | 15 | 55 (0.29) | 55 | 0.52 (k=15, 8/16) |
| full rerank, q0 | 9/16 | 11 | 13 | 14 | 14 | 56 (0.29) | 56 | 0.76 (k=13, 11/16) |
| full rerank, q1 | 4/16 | 8 | 10 | 11 | 12 | 67 (0.24) | 41 | 0.49 (k=21, 9/16) |
| full rerank, max over the two queries | 7/16 | 11 | 14 | 15 | 16 | 47 (0.34) | 47 | 0.71 (k=15, 11/16) |

Cutoff check, cosine (best query): positives' doc similarity min 0.8634 / median 0.8774; negatives' max 0.8934 / median 0.8649; 37 negatives score at or above the weakest positive. Spread of all 80 doc scores: 0.8224-0.8940.
Cutoff check, full rerank q0: positives' doc score min 0.0071 / median 0.2328; negatives' max 0.7092 / median 0.0086; 42 negatives score at or above the weakest positive.
Cutoff check, full rerank q1: positives' doc score min 0.0060 / median 0.0769; negatives' max 0.5793 / median 0.0192; 51 negatives score at or above the weakest positive.

What the models actually did (replayed against the re-ingested corpus):

| Model | Tool calls | Search calls replayed (skipped: scoped to old ids) | Read-tool calls | Docs whose chunk text the model saw | Docs seen title-only | Named (true positives / false positives) |
|---|---|---|---|---|---|---|
| claude-fable-5-1 | 22 | 17 (2) | 2 | 37 | 0 | 8 (8 / 0) |
| gpt-6-astra | 41 | 20 (3) | 5 | 57 | 0 | 11 (10 / 1) |

- claude-fable-5-1 did not list **AimmuneTherapeuticsInc**: other chunk(s) of the doc returned with text, not listed.
- claude-fable-5-1 did not list **ConformisInc**: other chunk(s) of the doc returned with text, not listed.
- claude-fable-5-1 did not list **LiquidmetalTechnologiesInc**: never returned.
- claude-fable-5-1 did not list **NakedBrandGroupInc**: annotated clause chunk returned with text, not listed.
- claude-fable-5-1 did not list **PhasebioPharmaceuticalsInc**: other chunk(s) of the doc returned with text, not listed.
- claude-fable-5-1 did not list **RevolutionMedicinesInc**: other chunk(s) of the doc returned with text, not listed.
- claude-fable-5-1 did not list **TubeMediaCorp**: never returned.
- claude-fable-5-1 did not list **WaterNowInc**: never returned.
- gpt-6-astra did not list **AimmuneTherapeuticsInc**: other chunk(s) of the doc returned with text, not listed.
- gpt-6-astra did not list **ConformisInc**: other chunk(s) of the doc returned with text, not listed.
- gpt-6-astra did not list **LiquidmetalTechnologiesInc**: annotated clause chunk returned with text, not listed.
- gpt-6-astra did not list **NakedBrandGroupInc**: other chunk(s) of the doc returned with text, not listed.
- gpt-6-astra did not list **PhasebioPharmaceuticalsInc**: other chunk(s) of the doc returned with text, not listed.
- gpt-6-astra did not list **WaterNowInc**: other chunk(s) of the doc returned with text, not listed.
- gpt-6-astra false positives: ElPolloLocoHoldingsInc.

## cuad-key-19: Insurance (21 positives; literal keyword `insurance`)

Concept queries (doc-level k to recover all positives under exact cosine; k to recover all items both models missed):

- q0: "requirement that a party maintain insurance" -> k_all=51, k_missed=-, mean positive doc rank 20.5
- q1: "shall maintain commercial general liability insurance coverage with limits of not less than" -> k_all=48, k_missed=-, mean positive doc rank 18.8
- q2 (best): "the company has to keep an insurance policy in force" -> k_all=35, k_missed=-, mean positive doc rank 13.8
- q3: "Insurance" -> k_all=49, k_missed=-, mean positive doc rank 18.0

| Positive | Missed by | Keyword in span / doc | Span cov. | Cosine doc rank (best q) | Best-chunk rank | Annotated-chunk rank | Min doc rank over queries | Rerank A doc rank | Rerank B doc rank |
|---|---|---|---|---|---|---|---|---|---|
| AimmuneTherapeuticsInc |  | yes / yes | 1.0 | 12 | 21 | 21 (=best) | 1 | 7 | 14 |
| BizzingoInc |  | yes / yes | 1.0 | 23 | 52 | 52 (=best) | 13 | 15 | 18 |
| DigitalCinemaDestinationsCorp |  | yes / yes | 1.0 | 26 | 63 | 195 | 24 | 24 | 22 |
| ElPolloLocoHoldingsInc |  | yes / yes | 1.0 | 3 | 3 | 96 | 3 | 14 | 15 |
| EtonPharmaceuticalsInc |  | yes / yes | 1.0 | 17 | 36 | 36 (=best) | 8 | 17 | 20 |
| FuseMedicalInc |  | yes / yes | 1.0 | 16 | 29 | 127 | 16 | 5 | 12 |
| GridironBionutrientsInc |  | yes / yes | 1.0 | 13 | 22 | 38 | 7 | 9 | 7 |
| LegacyEducationAllianceInc |  | yes / yes | 1.0 | 35 | 105 | 458 | 14 | 54 | 43 |
| LiquidmetalTechnologiesInc |  | yes / yes | 1.0 | 4 | 4 | 4 (=best) | 2 | 13 | 9 |
| MusclepharmCorp |  | yes / yes | 1.0 | 9 | 12 | 13 | 5 | 22 | 13 |
| PapaJohnsInternationalInc |  | yes / yes | 1.0 | 14 | 24 | 25 | 13 | 28 | 2 |
| PerformanceSportsBrandsInc |  | yes / yes | 1.0 | 7 | 10 | 10 (=best) | 7 | 1 | 1 |
| PharmagenInc |  | yes / yes | 1.0 | 25 | 61 | 61 (=best) | 11 | 39 | 24 |
| PhasebioPharmaceuticalsInc |  | yes / yes | 1.0 | 2 | 2 | 176 | 1 | 12 | 5 |
| RevolutionMedicinesInc |  | yes / yes | 1.0 | 10 | 14 | 14 (=best) | 9 | 3 | 8 |
| RitterPharmaceuticalsInc |  | yes / yes | 1.0 | 15 | 26 | 561 | 2 | 4 | 10 |
| ScansourceInc |  | yes / yes | 1.0 | 20 | 45 | 45 (=best) | 4 | 6 | 11 |
| SmartRxSystemsInc |  | yes / yes | 0.842 | 19 | 43 | 49 | 19 | 2 | 3 |
| StaarSurgicalCompany |  | yes / yes | 1.0 | 6 | 8 | 32 | 6 | 11 | 16 |
| TubeMediaCorp |  | yes / yes | 1.0 | 5 | 6 | 6 (=best) | 5 | 27 | 27 |
| ZogenixInc |  | yes / yes | 1.0 | 8 | 11 | 11 (=best) | 2 | 10 | 17 |

Ranks are 1-based; document ranks are out of 80, chunk ranks out of 6,408. Rerank A = per-document best cosine chunk (80 passages) reranked; Rerank B = top-200 cosine chunks reranked, per-document best ('>N' = the document had no chunk in the top 200, N docs in pool); Full rerank = every chunk reranked, per-document best, with the annotated clause chunk's rank among all chunks in parentheses.

Recall-at-k over documents read in rank order (positives 21; items both models missed 0):

| Method | recall@10 | @20 | @30 | @40 | @50 | k for all positives (precision there) | k for all both-missed items | best F1 (k, recall) |
|---|---|---|---|---|---|---|---|---|
| exact cosine, best query | 9/21 | 17 | 20 | 21 | 21 | 35 (0.6) | n/a | 0.85 (k=26, 20/21) |
| exact cosine, max over all concept queries | 7/21 | 14 | 18 | 19 | 20 | 55 (0.38) | n/a | 0.77 (k=26, 18/21) |
| rerank A (per-doc best chunk) | 9/21 | 15 | 19 | 20 | 20 | 54 (0.39) | n/a | 0.79 (k=17, 15/21) |
| rerank B (top-200 chunks) | 8/21 | 17 | 20 | 20 | 21 | 43 (0.49) | n/a | 0.84 (k=24, 19/21) |

Cutoff check, cosine (best query): positives' doc similarity min 0.8699 / median 0.8907; negatives' max 0.9148 / median 0.8627; 14 negatives score at or above the weakest positive. Spread of all 80 doc scores: 0.8187-0.9148.

What the models actually did (replayed against the re-ingested corpus):

| Model | Tool calls | Search calls replayed (skipped: scoped to old ids) | Read-tool calls | Docs whose chunk text the model saw | Docs seen title-only | Named (true positives / false positives) |
|---|---|---|---|---|---|---|
| claude-fable-5-1 | 10 | 4 (2) | 3 | 40 | 0 | 21 (21 / 0) |
| gpt-6-astra | 33 | 5 (1) | 3 | 44 | 0 | 22 (21 / 1) |

- gpt-6-astra false positives: PrudentialBancorpInc.
- Every positive was listed by both models.

## cuad-key-20: Governing Law = Delaware (7 positives; literal keyword `delaware`)

Concept queries (doc-level k to recover all positives under exact cosine; k to recover all items both models missed):

- q0 (best): "governed by the laws of the State of Delaware" -> k_all=54, k_missed=-, mean positive doc rank 12.1
- q1: "governing law Delaware" -> k_all=63, k_missed=-, mean positive doc rank 14.6
- q2: "this agreement shall be governed by and construed in accordance with the laws of the State of Delaware" -> k_all=71, k_missed=-, mean positive doc rank 15.7
- q3: "which state's law applies to this contract: Delaware" -> k_all=56, k_missed=-, mean positive doc rank 12.3
- q4: "Governing Law" -> k_all=75, k_missed=-, mean positive doc rank 32.9

| Positive | Missed by | Keyword in span / doc | Span cov. | Cosine doc rank (best q) | Best-chunk rank | Annotated-chunk rank | Min doc rank over queries | Rerank A doc rank | Rerank B doc rank |
|---|---|---|---|---|---|---|---|---|---|
| CreditcardscomInc |  | yes / yes | 1.0 | 54 | 109 | 132 | 54 | 63 | 4 |
| DigitalCinemaDestinationsCorp |  | yes / yes | 1.0 | 1 | 1 | 4 | 1 | 5 | 3 |
| EtonPharmaceuticalsInc |  | yes / yes | 1.0 | 5 | 6 | 6 (=best) | 3 | 4 | 8 |
| ImpresseCorp |  | yes / yes | 1.0 | 4 | 5 | 11 | 4 | 6 | 1 |
| PapaJohnsInternationalInc |  | yes / yes | 1.0 | 2 | 2 | 2 (=best) | 1 | 2 | 6 |
| PhasebioPharmaceuticalsInc |  | yes / yes | 1.0 | 9 | 10 | 22 | 5 | 8 | 5 |
| RitterPharmaceuticalsInc |  | yes / yes | 1.0 | 10 | 12 | 12 (=best) | 4 | 3 | 7 |

Ranks are 1-based; document ranks are out of 80, chunk ranks out of 6,408. Rerank A = per-document best cosine chunk (80 passages) reranked; Rerank B = top-200 cosine chunks reranked, per-document best ('>N' = the document had no chunk in the top 200, N docs in pool); Full rerank = every chunk reranked, per-document best, with the annotated clause chunk's rank among all chunks in parentheses.

Recall-at-k over documents read in rank order (positives 7; items both models missed 0):

| Method | recall@10 | @20 | @30 | @40 | @50 | k for all positives (precision there) | k for all both-missed items | best F1 (k, recall) |
|---|---|---|---|---|---|---|---|---|
| exact cosine, best query | 6/7 | 6 | 6 | 6 | 6 | 54 (0.13) | n/a | 0.71 (k=10, 6/7) |
| exact cosine, max over all concept queries | 5/7 | 5 | 6 | 6 | 6 | 71 (0.1) | n/a | 0.77 (k=6, 5/7) |
| rerank A (per-doc best chunk) | 6/7 | 6 | 6 | 6 | 6 | 63 (0.11) | n/a | 0.8 (k=8, 6/7) |
| rerank B (top-200 chunks) | 7/7 | 7 | 7 | 7 | 7 | 8 (0.88) | n/a | 0.93 (k=8, 7/7) |

Cutoff check, cosine (best query): positives' doc similarity min 0.8280 / median 0.8661; negatives' max 0.8842 / median 0.8305; 47 negatives score at or above the weakest positive. Spread of all 80 doc scores: 0.8021-0.9059.

What the models actually did (replayed against the re-ingested corpus):

| Model | Tool calls | Search calls replayed (skipped: scoped to old ids) | Read-tool calls | Docs whose chunk text the model saw | Docs seen title-only | Named (true positives / false positives) |
|---|---|---|---|---|---|---|
| claude-fable-5-1 | 14 | 7 (4) | 2 | 51 | 0 | 7 (7 / 0) |
| gpt-6-astra | 16 | 3 (0) | 4 | 38 | 0 | 7 (7 / 0) |

- Every positive was listed by both models.

## Why the missed clauses rank where they do

The annotated clause chunks of the items both models missed rank, under exact cosine with the best concept query, at 278 (Gentech), 1,994 (Rand Worldwide), 2,699 (Ritter, key-15); 1,321 (Innerscope), 43 (Performance Sports Brands), 1,624 (SmartRx, key-16); 3,976 (Aimmune), 2,674 (Conformis), 2,337 (Liquidmetal), 161 (Naked Brand), 3,149 (Phasebio), 1,598 (WaterNow, key-18) out of 6,408. Only two of the twelve are inside the top 200 that a cosine-then-rerank sweep would hand to the reranker; the rest are invisible to it whatever the cutoff. Three causes are visible in the chunk text:

- The clause is one sentence inside a ~1,000-character chunk about something else. Innerscope's 'harmful to its honor, goodwill or reputation' sits in a list of distributor obligations (sales restrictions, export); Gentech's 'shall not contact any of Distributor's Customers' sits in a territory clause. The chunk embedding is dominated by the surrounding text.
- The clause is written in deal-specific terms with no clause-type vocabulary at all: Aimmune's and Conformis's rights attach to abandoning patent prosecution; Phasebio's is a consent right over 'Licensing Transactions'; Liquidmetal's is an 'option to purchase the Optioned Assets'; Ritter's no-solicit is a carve-out for 'Qualigen Retained Customers'. The CUAD annotators labelled these by legal judgment; neither a bi-encoder nor the reranker recognises them from a category-level query.
- The embedder's similarity range is compressed. Across every question the 80 document-best cosines span about 0.80-0.91 and the positives' median is within 0.01 of the negatives' median (key-16: 0.8814 vs 0.8808). 31-51 negatives score at or above the weakest positive on every concept question. There is no calibrated cutoff to find: the scores do not separate.

The reranker is sharper but query-bound. Given a near-paraphrase of the Innerscope sentence (q4, 'shall not do anything that damages the reputation or goodwill of the other party') it ranks that chunk 6th of 6,408 and the document 3rd; given the plain 'non-disparagement clause' (q0) it ranks the same chunk 2,951st. Conversely q0 finds 7 of 9 positives by k=20 (the six that contain 'disparag' plus SmartRx, via a chunk that is not the annotated one) and q4 finds two. The union of the two queries still needs 54 documents for full recall (43 for the both-missed items). A sweep would have to guess the annotator's paraphrase to reach these clauses, and it would reach them one query at a time.

## What the models actually did, and where the misses came from

Both models enumerated by literal `kb_find_all` sweeps and then read hits; Fable saw chunk text from 30-51 documents per question and Astra from 38-57, so each was already reading roughly half the corpus. Replaying their search calls against the re-ingested corpus classifies the misses:

- Returned title-only and never read (Fable only; 4 items: Rand Worldwide and Usio on key-15, Performance Sports Brands and SmartRx on key-16). These came back from Fable's one semantic `kb_find_all` without `text_contains` (67 and 70 documents in `brief` presentation, which is title and score only). Fable did not read them; it read the 22 and 10 documents its literal filters returned. A brief-presentation list of 70 titles is not a sweep a model can act on.
- The annotated clause chunk was returned with its text and the model did not list the document (Fable: Gentech, Innerscope, Naked Brand; Astra: Liquidmetal). These are judgment rejections, not retrieval failures: Fable had the 'honor, goodwill or reputation' sentence in front of it from its own `kb_batch_search` paraphrases and decided it was not a non-disparagement clause. Fable's key-15 answer also lists Ritter and Naked Brand explicitly under 'M&A no-shop / exclusivity on offers', i.e. it read them and classified them out.
- Another chunk of the document was returned, not the clause (Astra: Aimmune, Conformis, Naked Brand, Phasebio, WaterNow, Performance Sports Brands, Ritter; Fable: Aimmune, Conformis, Phasebio, Ritter). The document was surfaced, the clause was not. Astra saw chunk text from 57 documents on key-18 and still missed six, because the chunk it saw was not the one the annotator meant.
- Never returned by any call (Fable: Liquidmetal, TubeMedia, WaterNow on key-18; Astra: Innerscope, SmartRx on key-16).

## Surprises

- Two keyword-bearing positives were missed for whitespace reasons, not judgment. TubeMedia says 'right of first refusal' three times, but the PDF text carries non-breaking spaces ('first\u00a0refusal'), so Fable's `text_contains: "first refusal"` (eight sweeps, 7 hits each time) never matched it; Fable noticed the same effect on MusclePharm and said so, but did not re-sweep with a NBSP. WaterNow's 'first\nopportunity' is split by a newline, so Astra's `text_contains: "first opportunity"` returned 0; Astra then tried 'first\noffer' and 'first\nnegotiation' but not 'first\nopportunity'. `text_contains` is a literal substring on raw chunk text; a whitespace-normalised match would have surfaced both for free.
- Ritter on key-15 is listed in the brief as 'keyword present'. The word 'solicit' does occur twice in the document, but not in the annotated span, which is 'Sekisui shall not market, rent or sell any Products to the Qualigen Retained Customers'. Both models read Ritter (its 'solicit' chunk came back from their literal sweeps) and rejected it; Fable filed it under no-shop. The key is scoring a judgment call the annotators made.
- Both models' 'false positives' on key-16 are the same three documents (Bizzingo, Creditcards.com, Rand Worldwide), each containing the word 'disparaging' in a clause about use of the other party's marks ('agrees not to use the Licensed Materials in any manner that is disparaging'). CUAD did not label these as Non-Disparagement. Likewise Stamps.com on key-15 ('agrees not to use any Customer Data to solicit any MBE-Generated Customers') is unlabelled. On these two questions the key penalises the keyword-faithful reading and rewards the annotator's. That caps the F1 any method can reach on them; it is not a retrieval problem.
- Conformis is listed in the brief as 'keyword present' for key-18; the document contains 'first right' twice (an abandonment-of-patent right), none of the three named rights by name. Fable's `text_contains: "first right"` sweep returned it (10 hits) and Fable read another chunk of it, not the annotated one.
- The harness scorer counts Fable's key-18 answer as naming 8 of 16, not 9: Fable discussed Revolution Medicines under a heading the scorer treats as excluded (a near-miss / not-counted section). The brief's list of seven Fable misses on key-18 is therefore eight by the key's own method. It does not change the conclusion.
- The per-document-best-chunk design is actively harmful. On every one of the six questions, reranking the 80 per-document best cosine chunks ranked the positives worse than cosine alone (k_all 72/73/62/77/54/63 vs 39/60/46/53/35/54). The document's most query-similar chunk is usually a boilerplate passage that merely shares vocabulary with the query (limitation of liability, dispute resolution, confidentiality); the reranker correctly scores it low and the document sinks. Top-200 chunks then rerank is better, and is the configuration that makes Delaware law work, but the clause chunks of the missed items are not in any top 200.

## Verdict

No. A semantic sweep as specified (exact cosine over every chunk, per-document best chunk, reranker over those, calibrated cutoff) would not have recovered the missed items at an acceptable read count. On the three concept-only questions the sweep ordering needs 72, 73 and 77 of 80 documents read for full recall, worse than cosine alone (39, 60, 53) and far worse than what the models already did (Fable read text from 33, 30 and 37 documents and Astra from 50, 39 and 57, landing at F1 0.62/0.67/0.67 and 0.82/0.67/0.74). The best F1 any cutoff on the sweep's own ordering could reach is 0.35, 0.22 and 0.53 (cosine alone: 0.38, 0.36, 0.59). The twelve both-missed items specifically sit at document ranks 5-77 (median 38) under the proposed ordering, with 62, 65 and 77 documents needed to collect all of them per question, and their clause chunks rank 43-3,976 of 6,408 under cosine (ten of the twelve beyond 160), so they are not in any pool the reranker would see. Even the upper bound that reranks all 6,408 chunks needs 41-56 documents to reach them and offers no separating cutoff (negatives' max score 0.58-0.90 against positives' min 0.003-0.10). The misses are of two kinds a sweep cannot fix: clauses written without clause-type vocabulary, buried in chunks about other things, where the only retrievable signal is a paraphrase of the specific sentence; and clauses the models did surface and rejected on judgment. What the replay does point at, cheaply: a whitespace-normalised `text_contains` (NBSP and newline) would have added TubeMedia and WaterNow; a `kb_find_all` without `text_contains` that returns 70 titles in `brief` is a list the model does not read, so if semantic enumeration is to mean anything it must return the best passage per document, not a title; and the reranker over a bounded chunk pool is worth keeping for keyword-rich clause types (Delaware law 7/7 at k=8), where it is precise, not for concept clauses, where it is not.

## Files

All in this directory: `analyze.py` (cosine + reranker A/B, writes `results.json`), `full_rerank.py` (every-chunk reranker, writes `full_rerank-<qid>-q<i>.json`), `replay.py` (tool-call replay via the local MCP endpoint, writes `replay.json`), `build_report.py` + `assemble.py` (this report), `ingest_cuad.py` (the guarded re-ingest). The instance holds CUAD (80 documents, 6,408 embedded chunks) and was left that way.
