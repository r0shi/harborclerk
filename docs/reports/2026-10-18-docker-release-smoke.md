# Harbor Clerk Release Smoke - 2026-10-18 (Docker, pre-1.0)

- Commit: local integration branch, not a release candidate. `origin/main` at
  1875deab (with #870) merged with the open PRs #851, #861 (head b1b1595a) and
  #891 (head 8126d26f, plus the `PGDATA` pin that #891 now carries for #894).
  Re-run on `main` once those merge, before tagging 1.0.
- App version (health `version`): `0.9.3+unstamped`, build `dev`
- Deployment: Docker Compose, project `hcsmoke`
- Machine: Apple M4, 32 GB RAM (host)
- Cloud spend: USD 0 (all local)
- OS: macOS host, colima 0.10.3 (vz, aarch64, 4 CPU), Docker 29.8.1, Compose 5.0.2.
  VM memory 8 GB for the first pass, 12 GB from the Ask step on.
- Corpus: two generated PDFs in `./data/watch/smoke` (a text-native memo, an
  image-only "scanned" invoice with no text layer) and one uploaded `.txt`
- Fresh DB or upgraded DB: fresh volumes
- Local model: Qwen3.5-4B-Q4_K_M (`LLM_MODEL_FILE` default), context 8192
- MCP/CLI key scope: one search-tier API key, MCP only

| Area                        | Result  | Notes |
| --------------------------- | ------- | ----- |
| Startup and onboarding      | partial | Fresh `up --build` fails on `main` (blockers 1 and 2); passes with #891. Admin created through `POST /api/setup`. The web UI was not exercised: the in-app browser refused the self-signed certificate, so the run used the API through the Caddy gateway. |
| Ingest and Status           | pass    | Both watched PDFs `ready` about 21 s after the copy; the scanned one OCR'd (`worker-cpu` reading the read-only `/data/watch` mount). Upload confirm stored the file on the `originals` volume and indexed it. |
| Search and Find All         | pass    | Phrases from both PDFs, including OCR-only text, ranked first. Find All not run. |
| Documents and citations     | pass    | With `ALLOW_SOURCE_DOWNLOAD=true`, both watched PDFs and the upload download byte-identical (sha256 match). |
| Ask and Research            | pass at 12 GB, fail at 8 GB | 8 GB: the kernel OOM-killed llama-server on the first Ask (blocker 3). 12 GB: the model was picked up without a restart; Ask answered correctly with a citation in about 2 minutes on CPU; a light Research run produced a cited report naming both documents and stating that they are unrelated. |
| MCP and CLI                 | partial | MCP `initialize` plus `kb_search` with a search-tier key returned cited hits. CLI not run. |
| API key scope and audit     | not run | |
| Recovery and backup docs    | not run | |
| Release docs and boundaries | fail    | README's Docker memory figure is wrong (blocker 3). |

## Blockers

1. **#888: the `minio/minio` image can no longer be pulled.** A fresh install
   stops before any Harbor Clerk container starts. Fixed by #891 (filesystem
   storage volume, MinIO opt-in only), verified here.
2. **#894: Postgres refuses to start on the current `pgvector/pgvector:pg18`
   image** while the volume is mounted at `/var/lib/postgresql/data` with
   `PGDATA` unset. Fixed by pinning `PGDATA` (in #891), verified here on a fresh
   volume.
3. **#895: 8 GB cannot run local AI.** With the default model loaded the stack
   uses about 7.6 GB and llama-server (about 3.2 GB RSS) is OOM-killed. The
   README says "4 GB minimum (8 GB recommended)". 12 GB worked. The Models page's
   "May not fit in this container's memory" warning and Ask's "stopped answering
   … may need more memory" message were both accurate.

## Fast-Follows

- **#896: image size.** embedder 9.35 GB, reranker 12.3 GB, each Python service
  3.25 GB; the layers include CUDA-only files (`triton/backends/nvidia`). A
  parallel build ran out of disk on a 98 GB VM disk; `COMPOSE_PARALLEL_LIMIT=1`
  after a cache prune got through.
- **#886: a redundant trailing chunk** showed up in search results here (a
  second, overlapping chunk of the memo).
- Re-run the UI half of the checklist in a browser that accepts the self-signed
  certificate: onboarding routing, the Docker-mode Folders page and guide link,
  and the Models page under Compose.
