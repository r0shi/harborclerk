# AGENTS.md — `src/harbor_clerk`

Backend invariants. Everything here is verified against source; stage
assignments, timeouts, and queue membership are published as a generated table
in `docs/architecture.md#ingestion-pipeline-stages`.

## Ingestion pipeline

- **Every stage is idempotent** and guarded by a row-level lock on
  `(doc_id, stage)` via `SELECT ... FOR UPDATE SKIP LOCKED`.
- **Always call `mark_stage_done` on every exit path, including early returns.**
  An early `return` on a pipeline-seq race once skipped it, leaving the job row
  `running` until the reaper fired 20 minutes later — and re-enqueue is
  suppressed while a row is `running`, so the document simply stalled.
- **Sequential prefix:** `extract` → `ocr` → `chunk`. Then `entities` and
  `embed` fan out and **gate `finalize`**.
- **`summarize` is a background stage.** It runs on the `llm` queue and does
  **not** gate `finalize`; a document is searchable before its summary exists.
  See the rationale comment in `worker/pipeline.py`.
- **Three queues: `io`, `cpu`, `llm`.** A new stage must be assigned to one, and
  every queue must have a subscriber — a queue nobody listens to silently
  swallows work (this shipped once; `tests/test_docker_compose_queues.py` now
  guards it).
- **OCR is conditional** and runs in-process (pypdfium2 + Tesseract). It never
  calls Tika.

## Extraction

Three paths, not two. Plain-text formats decode directly via
`file_types.PLAIN_TEXT_EXTENSIONS`; markdown has its own branch preserving
heading structure (`worker/markdown_extract.py`); everything else goes through
Tika. Don't invent a fourth.

## Storage

All storage access goes through the `StorageBackend` ABC — never touch MinIO or
the filesystem directly. `STORAGE_BACKEND` selects the implementation.

Watched-folder documents are read in place from `source_path` and are never
written to. IMAP messages and attachments are the one case where content is
fetched and stored, because there is no file on disk to reference.

## Retrieval

- Full-text search is **bilingual**: query `fts_en` and `fts_fr`, each through
  `search.websearch_tsquery`, which folds whitespace as the columns do (#825).
- The reranker is on by default (`reranker_enabled`) and **degrades gracefully**
  — if it is unreachable, the merged FTS+vector score is the final ordering.
  Never let a reranker failure fail a search.
- Every result carries citations.

## Data model

- The `documents` model is **flat**. There is no version table; reprocessing
  updates the row in place. Same SHA on a known path is a no-op; a changed SHA
  reprocesses.
- `chunks.embedding` is `vector(768)` (Granite-R2). `schema_metadata` records
  the embedding model and dimension, and startup refuses a mismatch — changing
  the model requires a migration, not just a config edit.
- No `tenant_id`. Anywhere.

## The model registry states memory, and Swift mirrors it

Every `ModelInfo` in `llm/models.py` carries `kv_bytes_per_token` (and
`kv_fixed_bytes` for sliding-window or linear-attention layers), read from the
GGUF header and checked against the file by `tests/test_llm_models.py` when it
is downloaded. `memory_bytes`, `min_ram_gb` and `max_context` derive from them;
the API reports them, activation refuses a model whose weights alone do not fit
(no override: the launcher would refuse it too), and the macOS launcher clamps
`-c` to what fits or refuses to launch. `context_window` is what the app passes
as `-c`, so raising it is a memory change, not a documentation fix. Chat,
research and summarize budget prompts through `context_budget()`, never the
registry window: it is per request, `-c` divided by `-np`, because a slot
divides the context rather than adding memory (every model runs one slot).
A context is sized to leave a tenth of the Mac free (`FREE_MEMORY_DIVISOR`): the
headroom is what the host *uses*, and filling the rest gave the mini's 35B a
context Metal refused (#684). The margin never refuses a model; a tight fit runs
at `TIGHT_FIT_CONTEXT`. **A pinned context is a claim until the model has been
loaded at it.** The prompt cache (`--cache-ram`) gets what is left, up to 8 GiB:
margin, then context, then cache (`prompt_cache_mib`, `MemoryBudget.promptCacheMiB`). That bound is
safe only because the pin skips a state larger than the limit; a pin bump
re-reads `server_prompt_cache::alloc` (`PROMPT_CACHE_SEMANTICS_READ_AT`). Context
checkpoints (copies of a sliding-window cache or recurrent state, 32 per slot
by default) are bounded at `LLAMA_CTX_CHECKPOINTS` and budgeted:
`fixed_bytes(model)` is what every memory figure uses, never `kv_fixed_bytes`
alone. A new model states its `checkpoint_bytes`, in the registry and in Swift. The Swift mirror
(`Settings.swift`: filenames, slots, windows, `kvBytesPerToken`,
`kvFixedBytes`, `MemoryBudget`) is held to the registry by a test; a new model
needs all of them. `HC_GGUF_LIVE=1` checks headers from the Hub for models not
downloaded.

## API keys over REST

The tier is enforced on both surfaces from one table, `REST_ROUTE_TOOLS`
(`api/scope.py`): each key-reachable REST route maps to the MCP tool whose data
it exposes, and `require_read_access` admits a key iff that tool is in its
effective set. A route using the dependency without an entry is closed to keys;
`tests/test_rest_api_key_tier.py` (add a `KEY_ROUTES` row) and `gen_docs` fail
on drift. Reads with no MCP counterpart (jobs, stats, watched folders, chat,
research) are `require_human_user`; there is no "any authenticated principal"
dependency, and `tests/test_gen_docs.py` refuses a blank gate unless public.
Keys never receive `source_path` / `watch_source_path`: wrap document-shaped
responses in `redact_paths_for_principal`; register a new path field in
`ABSOLUTE_PATH_FIELDS`, free text that may quote one (pipeline `error` text
does) in `ABSOLUTE_PATH_TEXT_FIELDS`. Snippet cap (#823): all chunk or page text
in a key-reachable payload goes through `passage_cap.py` (`cap_passage_text`,
`cap_text`, `snippet_cap`): each passage's first N characters (per passage, not
per document), never a window within it; null at 0; a stored negative is 0.
Titles, summaries, metadata, headings, entity strings and `text_contains`
matches are not passage text (a match tells a capped key the literal is in the
chunk). New surface: a row in `tests/test_key_snippet_cap.py`.

## The chat token estimate is raw; the bounds are predicted

`llm/chat.py` estimates tokens by characters over `_CHARS_PER_TOKEN`, a prose
figure that runs at about half the tokenizer's count on tool results (#805).
Every bound (the context-budget stop, the history trim, the forced call's
outer deadline) compares against the next prompt as **predicted**
(`_predict_prompt_tokens`): the server's exact `usage.prompt_tokens` of the
last counted prompt, plus the messages appended since, weighted per message
(`_predict_messages_tokens`: tool results and their calls at the model's tool
ratio, prose and the schema at the model's prose ratio); with no count yet,
the whole prompt weighted the same way. Reading prose at the tool ratio
trimmed a 16K history to 4 exchanges of 20 where the server takes 13; a
constant prose weight let a 1.8x (CJK) history be refused on every turn. Both
ratios are kept per model id for the life of the process (`_estimate_ratios`,
`_prose_ratios`). A refused first prompt is where a dense history is met: the
loop records the prose density from the refusal's count and retries the turn
once, trimmed against that count and the slot the refusal states; a refusal
once the turn has searched ends the search like the context budget, and the
forced call's retry shrinks the results (the history trim would keep the
latest tool row as the question); a first prompt refused twice is the error.
An increment under `_INCREMENT_FLOOR_TOKENS` raw tokens is not measured.
The prose table is per model while density is per conversation, so alternating
languages oscillate it (EN/CJK/EN measured 1.20, 1.64, 1.72, 1.46, 1.62);
per-conversation keying is #810.
The tool ratio is measured only as the **increment** between two counted prompts
of one turn's loop (the result and its call, nothing else): never from a
tool-less prompt, never from a turn's first prompt even when the history holds
results (diluted), never from the forced call (it drops the tools); blended at
half weight from the default, never replaced.
The prose ratio is measured over every prompt with no tool row, accepted or
refused (seed 1.2, replaced by the first measurement, blended after); once a
conversation has searched, its later first prompts are tool-bearing and
measure neither ratio. A count under
`_ESTIMATE_RATIO_MIN` times the estimate is distrusted, in the loop and in the
forced call. The one figure read bare, deliberately, is the per-result
truncation cap `tool_result_max_chars` (a retrieval-quality knob, not a
bound). llama-server (pin v0.4.1) sends usage only when the request carries
`stream_options: {"include_usage": true}`, in a last chunk whose `choices` is
an **empty array**: `chunk["choices"][0]` raises on it; read the last usage a
stream carries, once, after it. A new estimate-derived figure takes the
prediction or the weights; a new streamed call asks for usage, records it and
tolerates the empty-choices chunk (`research.py`'s streaming reader does not
ask for usage and still indexes `choices[0]`: asking without fixing the reader
crashes it).

## Paths over MCP

The rule on both surfaces is the same, and keyed on one test: `principal.type
== "user"` is the signed-in human — a web-app request over REST, or the chat and
research tools, which run the `kb_*` functions in-process under
`Principal(type="user")` (`llm/tools.py`). The only other MCP principal types
are `api_key` and `oauth`, and both are external agents: they keep the MCP
contract of never receiving a path component (`tests/test_mcp_source_ref.py`),
while the human keeps the folder leaf in chat citations and the whole error text
in a tool chip, exactly as the web app shows them. The test describes the
credential, not the client: any JWT holder on the MCP port is the human, so a
harness that wants the agent view (the eval runner, a scripted client) must
authenticate with an API key — `answer_eval.py` warns when it falls back to a
login.

Concretely, in `mcp_server.py`: `_human_session()` is the predicate; every tool
loads its SourceRef context through `_source_context()`, which passes
`folder_label_from_path=_human_session()` to `load_source_ref_context` (never
call the loader directly from a tool); and pipeline error text goes through
`_error_text()` — `redact_path_tokens` from `api/scope.py` for an agent,
unchanged for the human (`kb_get_document` and `kb_ingest_status` today). The
REST search, find-all and passage routes pass `folder_label_from_path=
principal.type == "user"`, the test `documents.py` applies to `folder_name`.
`kb_get_document` reports a stage failure as `pipeline_error`, present only when
one failed: the chat tools read a top-level `error` key as "the call failed"
and render it verbatim with no citation (`llm/tools.py::summarize_tool_result`,
`llm/citations.py`), so no healthy payload may carry a key of that name.
Acceptance D5 checks the agent branch against a live instance with a document
whose extract stage failed on a path-quoting OSError.

## Async gotchas

- `enqueue_stage()` uses a **sync** session. Calling it in a loop from an async
  endpoint blocks the event loop — wrap it in `run_in_executor`.
- SQLAlchemy enum members must be **lowercase** to match Postgres values: both
  `postgresql.ENUM` and `sa.Enum` send `.name`, not `.value`.

## Legacy upload storage

The legacy upload path stores originals in the `originals` bucket under
`originals/<doc_id>/<original_filename>`. Watched-folder and IMAP documents do
**not** use it — watched files are read in place from `source_path`, and mail
content is stored via the normal document path.
