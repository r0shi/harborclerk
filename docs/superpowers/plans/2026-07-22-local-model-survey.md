# Local Model Survey — 2026-07-22

**Status:** Report only. No changes made.
**Window surveyed:** ~2026-06-20 → 2026-07-22.
**Method:** Hugging Face Hub REST API for release dates, file sizes, GGUF
architecture/context metadata and chat-template commit history; GitHub API for
llama.cpp commits. Web search used only to cross-check. No model was run.

---

## Headline

Three findings, in order of value:

1. **Two live bugs exist today**, both verified against this repo — the Gemma 4
   context window is recorded at half its real value, and the pinned llama.cpp
   predates the tool-call grammar fixes.
2. **Both Qwen entries are two generations stale.** They point at May 2025
   repos; Qwen is on 3.5/3.6.
3. **The Gemma 4 publisher missed two upstream tool-calling template fixes.**
   Repointing is a pure drop-in.

There is one genuinely new contender worth evaluating (Agents-A1), one clear
tier-gap filler (Gemma 4 12B), and several hyped releases that are disqualified.

---

## Verified against this repo (not from the survey)

### `context_window=128000` for Gemma 4 26B-A4B is wrong

`llm/models.py:105`. GGUF metadata and Google's model card both give **262144**
(Google: 128K for E2B/E4B, **256K** for 12B / 26B-A4B / 31B). The app is
advertising and budgeting half the available context on its heavy-tier model.

Note `gpt-oss-20b` at `context_window=128000` (line 115) **is** correct.

### llama.cpp is pinned to `b9018`, built 2026-05-04 — ~11 weeks behind

`macos/scripts/build-llama.sh:7`. That predates every tool-calling fix below.
This affects **all five models**, not one, which makes it the highest-leverage
item in this report:

| PR | date | why it matters here |
|---|---|---|
| `#24869` | 06-21 | AC parser for stricter grammar generation — explicitly fixes **models escaping the grammar** during structured/tool output |
| `#24835` / `#24839` | 06-20 | json-schema-to-grammar spacing; PEG grammar refactor |
| `#24674` | 07-13 | fixes **reasoning leak** with force-opened bare `<think>` templates — affects every thinking model in the set |
| `#23116` | 07-13 | per-request `reasoning_budget_tokens` — a direct lever for the wall-time work |

Also missed: the entire speculative-decoding maturation (EAGLE3 for Qwen3.5/3.6
`#24593`, Gemma 4 MTP `#23398`, sidecar auto-download `#25811`, type inference
`#25989`). unsloth now ships matching MTP drafters for models already curated.
**No Metal/Apple-Silicon performance leap in the window** — upgrade for tool-call
reliability and speculative decoding, not for raw speed.

---

## Current set, per model

| id | state | verdict |
|---|---|---|
| `qwen3-8b` | `Qwen/Qwen3-8B-GGUF`, last modified **2025-05-21**, ctx 40960 | Stale 2 generations → Qwen3.5-9B |
| `qwen3-4b` | `Qwen/Qwen3-4B-GGUF`, last modified **2025-05-21**, ctx 40960 | Stale 2 generations → Qwen3.5-4B |
| `gemma4-26b-a4b` | bartowski, template last touched **2026-05-03** | Missing 2 template fixes → repoint to unsloth |
| `gpt-oss-20b` | unsloth, weights unchanged since **2025-08-21** | Orphaned — no successor exists (verified) |
| `qwen36-35b-a3b` | unsloth, 2026-04-16 | **Current.** Keep. |

### The Gemma 4 template gap

Google's own repo shipped chat-template fixes on 2026-04-28, **2026-05-18**
(tool-response content-parts), **2026-07-15** (null handling, reasoning
preservation, turn-tag balance) and 2026-07-20 (response_template).

The curated bartowski repo's last template commit is **2026-05-03** — it
predates two of those, both touching tool calling. unsloth rebuilt on
**2026-07-17** with "Added Gemma official chat template update".

| | bartowski (curated) | unsloth |
|---|---|---|
| Q4_K_M | 17.04 GB | **16.95 GB** (UD) |
| file date | 2026-05-03 | **2026-07-17** |
| template current | no | yes |
| 30-day downloads | 72K | **1.46M** |

Same arch, same context, Apache-2.0. Pure drop-in.

---

## New contenders

### Agents-A1 — the one worth evaluating

`InternScience/Agents-A1`, released 2026-06-22, vendor GGUF 2026-07-01.

- 35B MoE / 3B active, GGUF arch **`qwen35moe` — the same family already
  shipping** as Qwen3.6-35B-A3B, so llama.cpp support is proven in this stack.
- **21.17 GB** at Q4_K_M — *smaller* than the 22.13 GB incumbent it would replace.
- 262,144 context. Apache-2.0. Vendor-published GGUF.
- Chat template carries 21 `tool_call` references with Qwen-style `<tool_call>`
  tags — same parser path as the existing Qwen models.
- **Trained for long-horizon search, instruction following and tool calling** —
  the only in-window release whose training objective matches Research
  (plan → search → read → synthesize).

Caveats: single Q4_K_M quant, no imatrix file, self-reported benchmarks, no
French-specific evaluation.

### Gemma 4 12B — fills the real tier gap

Q4_K_M **7.12 GB** (unsloth, 2026-07-17); Google QAT q4_0 6.98 GB. 256K context,
Apache-2.0, native function calling, 35+ languages out of the box / 140+
pretrained — the strongest multilingual story available for the French corpora.

### Agents-A1-4B

2026-07-13, **2.71 GB**, ctx 262144, Apache-2.0. Agentic-tuned alternative to
Qwen3.5-4B. Only worth testing if Agents-A1 35B proves out.

### Screened out

| model | why not |
|---|---|
| **Qwen-AgentWorld-35B-A3B** | Despite the name and 698K downloads, it is a **world model that simulates environments** for training agents. It is the environment, not the agent. |
| **Ornith-1.0 9B/35B** | Agentic **coding** models (Terminal-Bench, SWE-bench). Card states twice that the Qwen chat template **must be modified** — a direct tool-calling risk out of the box. |
| **GLM-5.2** | 753B params (verified from GGUF metadata). ~400 GB at Q4. |
| Kimi K2.x, Hy3 295B, Solar-Open2-250B, Leanstral-119B | Far beyond the 24 GB ceiling. |
| Microsoft Fara1.5 | Computer-use/GUI agents, no GGUF. |
| **Meta** | Nothing since 2025-04 — effectively exited open weights. |
| **OpenAI** | Nothing since gpt-oss 2025-08-04. |

---

## Tier gaps

Current ladder: 2.5 / 5.0 / 11.6 / 17 / 22.1 GB.

- **The real gap is 5.0 → 11.6 GB.** Gemma 4 12B at 7.12 GB lands squarely in
  it. This is the most valuable *addition*.
- **11.6 → 17 GB is not worth filling** — the candidates there (Qwen3.6-27B
  dense 16.82 GB, Gemma 4 31B 18.32 GB) are dense and would be markedly slower
  than the 26B-A4B MoE already at 17 GB.
- **Sub-3 GB:** Qwen3.5-4B (2.74 GB) is a straight upgrade — 256K vs 40K context
  for +0.24 GB.
- **Heavy tier is well served.** Agents-A1 vs Qwen3.6-35B-A3B is a question of
  which behaves better, not of a gap.

---

## Recommended actions

### (a) Low-risk — no new evaluation strictly required

1. **Fix `context_window` for `gemma4-26b-a4b`: 128000 → 262144.** A one-line
   correction to a value that is simply wrong.
2. **Upgrade llama.cpp past `b9018`** to ≥ 2026-06-21 for the grammar/tool-call
   fixes, ideally ≥ 2026-07-17 for the reasoning-leak fix and speculative
   sidecar auto-download. Affects all five models.
3. **Repoint Gemma 4 to `unsloth/gemma-4-26B-A4B-it-GGUF`
   (`...-UD-Q4_K_M.gguf`, 16.95 GB)** for the two missed template fixes.
4. **`qwen3-8b` → `unsloth/Qwen3.5-9B-GGUF`** (5.68 GB): 40K → 262K native
   context, 119 → 201 languages, better tool/agent benchmarks, +0.68 GB. Arch
   moves `qwen3` → `qwen35`; the `qwen35moe` family already runs here.
5. **`qwen3-4b` → `unsloth/Qwen3.5-4B-GGUF`** (2.74 GB), same reasoning.

### (b) Needs evaluation before adoption

1. **Agents-A1** head-to-head against Qwen3.6-35B-A3B on the Research harness.
   If it wins it *replaces* the heavy tier rather than adding to it.
2. **Gemma 4 12B** for the 5→11.6 GB gap.
3. **Speculative decoding** via MTP/EAGLE3 drafters — not a model change, but
   the largest available wall-clock win, and now nearly config-free.
4. **`gpt-oss-20b` retirement** — orphaned and its 11.6 GB slot is contested by
   Gemma 4 12B at 7.12 GB. Do not drop blind; retire only if Gemma 4 12B
   evaluates well.

---

## The framing that matters most

**The local-model quality baseline is ~8 weeks old.** The last full local
multi-model sweep was `2026-05-28-local-sweep` (8 models, three since culled);
the last local answer-eval was the v3 sweep on 2026-05-31. Everything after that
— verifier A/B, prompt-tightening, the #449 validation — was cloud-model or
single-purpose.

So there is **no current baseline to compare any candidate against**. Adopting
anything from section (b) means running a sweep, not making a swap. The existing
per-model tuning demonstrates why: `qwen3-4b` sits at `-np 1` and `gpt-oss-20b`
has hand-sized KV cache because sweeps found those, not because a spec sheet
said so. A new model arrives with none of that tuning.

The section (a) items do not have this problem — a wrong context value, an
outdated llama.cpp pin, and a stale publisher are defects regardless of
benchmark position.

---

## Not verified

- **Tool-calling reliability under llama-server for any candidate.** Everything
  above is architecture, template inspection and vendor claims. Arch match makes
  support near-certain; *behaviour* is unproven.
- **French quality specifically.** Only aggregate multilingual claims. Agents-A1
  and Ornith publish no multilingual evaluation at all.
- Whether the Agents-A1 vendor Q4_K_M is imatrix-calibrated (no imatrix file,
  single quant offered).
- Whether Agents-A1's shipped template drives llama.cpp's Qwen tool parser
  correctly — contents look right, untested.
- Real throughput on M2/M4 for any candidate.
- All vendor benchmark tables are self-reported and were not independently
  corroborated.
