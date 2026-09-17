# Model survey — 2026-09-17

- **Run id:** `ix-1` · commit `v0.9.2-15-g88a963c` · window: releases since 2026-07-22 · 27 vendors watched
- **Policy:** Q4_K_M at most 24 GB, context at least 32768, tool calling in the chat template, permissive licence, GGUF from the vendor or unsloth, ggml-org, bartowski, lmstudio-community (`scripts/model_survey/watchlist.yaml`)
- **llama.cpp:** pinned `b9018` (126 architectures); upstream latest `v0.4.1` (2026-09-14, 152 architectures at head)
- **Method:** Hugging Face Hub API and GitHub, unauthenticated; no model was run. Judge model: n/a · cloud spend: n/a

## Reading

**Headline.** `Qwen/Qwen3.8-27B` ranks first by a wide margin (65.2 against 24.7 for the runner-up), and it is
the only Qwen3.8 release this product can use. Apache-2.0, 16.5 GB at Q4_K_M, 262K native context, tool calls in the
chat template, 15,500 likes and 8.2M GGUF downloads in six weeks. Its architecture is `qwen35`, which the pinned
llama.cpp already loads, so it can be tried today without touching the pin. The other two Qwen3.8 releases are out of
reach: Flash-Next is 119.6 GB under a non-permissive licence and the 2.4T flagship is a datacentre model. There is no
vendor Qwen3.8 below 27B. The 2B, 4B and 9B "Qwen3.8" builds on the Hub are third-party distills, not candidates.

**It is not a drop-in replacement for the heavy tier.** It is dense: every token reads all 16.5 GB. On the mini (M4,
120 GB/s) that caps decode near 7 tokens per second. The incumbent `qwen36-35b-a3b` activates 3B parameters per token
and has a ceiling several times higher. Ask is bounded by 25 tool rounds rather than by time, and the acceptance run
already recorded a 19-minute Ask on the incumbent, so wall time per answered question has to be measured beside
accuracy. The likely outcome is a quality tier next to the mixture-of-experts model, aimed at Macs with more memory
bandwidth, not a swap. Speculative decoding (carried from #551) is what could make it practical on the mini, and the
drafter architectures (`eagle3`, `dflash`, `gemma4-assistant`) exist only past the pin.

**Tier by tier.**

- `qwen3-8b` to `Qwen3.5-9B`, and `qwen3-4b` to `Qwen3.5-4B`. Both have been available since 2026-02-27. Same vendor,
  same licence, an architecture the pin loads, and 262K native context in place of 32K plus YaRN, for 0.7 and 0.2 GB
  more. Wiring them is mechanical. Making either the default still needs its own baseline (#551). This is the largest
  unclaimed gain for the least risk, and it goes first.
- `qwen36-35b-a3b` to `Qwen3.8-27B`: needs evaluation, as above. Add beside, do not replace blind.
- `gemma4-26b-a4b` and `gpt-oss-20b`: no newer generation exists. gpt-oss remains orphaned.
- Ladder gaps: `gemma-4-12B-it` (7.1 GB) fills the 5 to 11.6 GB gap. #551 found it by hand in July; the tool now
  finds it by rule. `gemma-4-31B-it` (18.3 GB) is dense with the same bandwidth cost as Qwen3.8-27B and less reason
  to pay it. Not recommended.

**Wrong today, fixable without an evaluation.**

- #548 is confirmed: the registry gives Gemma 128000 tokens where the GGUF carries 262144.
- #550 needs a ten-minute check before anyone acts. bartowski's Gemma repo changed on 2026-07-27, after the issue was
  filed. Its chat template still differs from unsloth's (18,681 against 18,922 characters), so the update did not
  close the issue, but the claim that it is two fixes behind should be re-read against a diff.
- Docker Compose runs the floating `ghcr.io/ggml-org/llama.cpp:server` while macOS pins `b9018`. The two deployments
  run different llama.cpp versions, and Compose changes without a commit. Pin both to one release when #549 lands.

**The pin.** It blocks one ranked candidate, `Ling-3.0-tiny` (`bailingmoe3`). It is four and a half months old, and
upstream now cuts stable semver releases, so #549 should target `v0.4.1` rather than another rolling `b` build. It
lands first because it re-baselines tool-call grammar for every model and unlocks the drafters.

**Other candidates.** `MiniCPM5-2B` (1.6 GB, Apache-2.0, loads today) is a possible tier below `qwen3-4b` for 8 GB
Macs. Models that small are usually weakest at tool calling, so expect evaluation to decide against it, but the test
is cheap. Granite 4.2 (Apache-2.0, vendor GGUF, 131K): the 8b competes directly with Qwen3.5-9B and earns one run;
the 3b and 30b do not displace anything. `Ling-3.0-tiny` waits for #549. `swiss-ai/Apertus-v1.5-8B` is multilingual
with French and permissively licensed but has no trusted GGUF yet: watch. Outside the watchlist,
`Edge0/Edge0-35B-A3B-preview` drew 3,284 likes in nine days from an unknown publisher. Read its card before adding
the org; popularity alone is not a reason. `ukisai/Swift-Qwen3.8-27b` is a third-party finetune.

**Carried forward from #551, still unevaluated.** `InternScience/Agents-A1` (35B-A3B, 21.2 GB, trained for search
and tool calling, still the only release whose training objective matches Research), Gemma 4 12B, speculative
decoding, and retiring `gpt-oss-20b` if Gemma 4 12B evaluates well.

**Recommended order.** (1) #549 to `v0.4.1`, pin Compose to match, re-baseline the current five. (2) #548, and the
#550 check. (3) Evaluate Qwen3.5-9B and 4B as replacements, Gemma 4 12B as a new tier, then Qwen3.8-27B against
Agents-A1 and the incumbent with wall time recorded. Everything named here is under the 24 GB ceiling, so the whole
evaluation can run on the mini. Stage 3 waits on two pieces that do not exist yet: the benchmark skill with its
enforced spend cap, and a memory requirement on `ModelInfo` (#556).

**Not verified.** No model was run. Tool calling under llama-server, French quality, throughput and every vendor
benchmark are unverified. The 7 tokens per second figure is a bandwidth ceiling worked out from file size, not a
measurement. Popularity is a signal that a model works for someone, not that it works here.

## Candidates, ranked

| # | Release | Released | Licence | Params | GGUF | Q4 GB | Context | Arch | Score | Why |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `Qwen/Qwen3.8-27B` | 2026-08-05 | apache-2.0 | 27.8B | `unsloth/Qwen3.8-27B-GGUF` | 16.5 | 262144 | qwen35 | 65.2 | successor of a curated family +40; popularity +20; recency +5.2 |
| 2 | `openbmb/MiniCPM5-2B` | 2026-09-06 | apache-2.0 | 2.5B | `openbmb/MiniCPM5-2B-GGUF` | 1.6 | 131072 | llama | 24.7 | popularity +15.9; recency +8.8 |
| 3 | `inclusionAI/Ling-3.0-tiny` | 2026-08-10 | mit | 7.9B | `inclusionAI/Ling-3.0-tiny-GGUF` | 4.8 | 131072 | bailingmoe3 | 19.1 | popularity +13.3; recency +5.8. ⚠ architecture 'bailingmoe3' needs a llama.cpp upgrade past the pin. |
| 4 | `ibm-granite/granite-4.2-30b` | 2026-08-07 | apache-2.0 | 29.3B | `ibm-granite/granite-4.2-30b-GGUF` | 17.7 | 131072 | granite | 15.8 | popularity +10.4; recency +5.4 |
| 5 | `ibm-granite/granite-4.2-3b` | 2026-08-07 | apache-2.0 | 3.7B | `ibm-granite/granite-4.2-3b-GGUF` | 2.2 | 131072 | granite | 15.3 | popularity +9.9; recency +5.4 |
| 6 | `ibm-granite/granite-4.2-8b` | 2026-08-07 | apache-2.0 | 8.8B | `ibm-granite/granite-4.2-8b-GGUF` | 5.3 | 131072 | granite | 14.9 | popularity +9.5; recency +5.4 |

## The curated set

| Model | GGUF repo | Registry ctx | GGUF ctx | Repo last modified | Findings |
|---|---|---|---|---|---|
| `qwen3-8b` | `Qwen/Qwen3-8B-GGUF` | 32768 | 40960 | 2025-05-21 | size-matched successor: Qwen/Qwen3.5-9B |
| `qwen3-4b` | `Qwen/Qwen3-4B-GGUF` | 32768 | 40960 | 2025-05-21 | size-matched successor: Qwen/Qwen3.5-4B |
| `gemma4-26b-a4b` | `bartowski/google_gemma-4-26B-A4B-it-GGUF` | 128000 | 262144 | 2026-07-27 | registry context_window 128000 leaves the GGUF's 262144 unused |
| `gpt-oss-20b` | `unsloth/gpt-oss-20b-GGUF` | 128000 | 131072 | 2025-12-19 | none |
| `qwen36-35b-a3b` | `unsloth/Qwen3.6-35B-A3B-GGUF` | 262144 | 262144 | 2026-04-20 | size-matched successor: Qwen/Qwen3.8-27B |

## What replaces each tier

Size-matched successors from the vendor's whole catalogue, not only the window.

| Curated | Successor | Released | Licence | GGUF | Q4 GB | Context | Arch |
|---|---|---|---|---|---|---|---|
| `qwen3-8b` (5.0 GB) | `Qwen/Qwen3.5-9B` | 2026-02-27 | apache-2.0 | `unsloth/Qwen3.5-9B-GGUF` | 5.7 | 262144 | qwen35 |
| `qwen3-4b` (2.5 GB) | `Qwen/Qwen3.5-4B` | 2026-02-27 | apache-2.0 | `unsloth/Qwen3.5-4B-GGUF` | 2.7 | 262144 | qwen35 |
| `qwen36-35b-a3b` (22.1 GB) | `Qwen/Qwen3.8-27B` | 2026-08-05 | apache-2.0 | `unsloth/Qwen3.8-27B-GGUF` | 16.5 | 262144 | qwen35 |

## Gaps in the size ladder

Members of a curated family that would sit in a gap of the ladder wider than 4 GB.

| Release | Released | Licence | Gap | GGUF | Q4 GB | Context | Arch |
|---|---|---|---|---|---|---|---|
| `google/gemma-4-12B-it` | 2026-05-23 | apache-2.0 | 5 to 11.6 GB | `unsloth/gemma-4-12b-it-GGUF` | 7.1 | 262144 | gemma4 |
| `google/gemma-4-31B-it` | 2026-03-11 | apache-2.0 | 17 to 22.1 GB | `unsloth/gemma-4-31B-it-GGUF` | 18.3 | 262144 | gemma4 |

## llama.cpp

Architectures upstream can load that the pin cannot: `bailingmoe3`, `cohere2moe`, `deepseek32`, `deepseek4`, `dflash`, `dots3note`, `eagle3`, `gemma4-assistant`, `granite_swa`, `graniteswitch`, `hrm_text`, `hy_v3`, `hy_v4`, `kimi-k3`, `laguna`, `maple`, `mellum`, `minimax-01`, `minimax-m3`, `muse-glimmer`, `nanbeige`, `pockettts`, `qwen3tts`, `qwen4exp`, `spark2_5`, `talkie`.

## Screened out

| Release | Released | Likes | Why not |
|---|---|---|---|
| `Qwen/Qwen3.8-Flash-Next` | 2026-08-24 | 5354 | licence is other; Q4_K_M is 119.6 GB, over the 24 GB ceiling |
| `deepseek-ai/DeepSeek-V4-Flash-0731` | 2026-07-31 | 3975 | no Q4_K_M file in unsloth/DeepSeek-V4-Flash-0731-GGUF |
| `deepseek-ai/DeepSeek-V4.1-Flash` | 2026-09-10 | 2968 | no GGUF from the vendor or a trusted publisher |
| `zai-org/GLM-5.3-Flash` | 2026-08-25 | 2417 | no Q4_K_M file in unsloth/GLM-5.3-Flash-GGUF; llama.cpp cannot load architecture 'glm5next', even at head |
| `zai-org/GLM-5.3` | 2026-08-25 | 1858 | licence is other; no Q4_K_M file in unsloth/GLM-5.3-GGUF |
| `Qwen/Qwen3.8-2.4T-A95B` | 2026-08-08 | 1230 | licence is other; no Q4_K_M file in unsloth/Qwen3.8-2.4T-A95B-GGUF |
| `deepseek-ai/DeepSeek-V4-Flash-Vision-Exp` | 2026-08-31 | 918 | no Q4_K_M file in unsloth/DeepSeek-V4-Flash-Vision-Exp-GGUF |
| `deepseek-ai/DeepSeek-V4-Pro-0813` | 2026-08-13 | 837 | no Q4_K_M file in unsloth/DeepSeek-V4-Pro-0813-GGUF |
| `LiquidAI/LFM2.5-2.6B` | 2026-07-28 | 767 | licence is other |
| `tencent/Hy4-preview` | 2026-08-27 | 480 | no GGUF from the vendor or a trusted publisher |
| `microsoft/Mage-VL` | 2026-07-25 | 405 | no GGUF from the vendor or a trusted publisher |
| `LiquidAI/LFM2.5-VL-3B` | 2026-08-11 | 209 | licence is other |
| `CohereLabs/North-Micro-Vision-Instruct` | 2026-08-10 | 142 | no GGUF from the vendor or a trusted publisher |
| `inclusionAI/Ling-3.0-flash-Fin` | 2026-09-03 | 98 | Q4_K_M is 77.8 GB, over the 24 GB ceiling |
| `inclusionAI/Ling-3.0-flash-VL` | 2026-09-04 | 96 | no GGUF from the vendor or a trusted publisher |
| `swiss-ai/Apertus-v1.5-8B` | 2026-07-24 | 92 | no GGUF from the vendor or a trusted publisher |
| `swiss-ai/Apertus-v1.5-70B` | 2026-07-24 | 90 | no GGUF from the vendor or a trusted publisher |
| `openbmb/MiniCPM5-2B-DSpark` | 2026-09-06 | 38 | no Q4_K_M file in openbmb/MiniCPM5-2B-DSpark-GGUF |
| `LiquidAI/LFM2.5-8B-A1B-DSpark` | 2026-08-10 | 36 | licence is other |
| `LiquidAI/LFM2.5-2.6B-DSpark` | 2026-08-10 | 35 | licence is other |
| `LiquidAI/LFM2.5-1.2B-Instruct-DSpark` | 2026-08-10 | 32 | licence is other |
| `inclusionAI/LLaDA2.2-mini` | 2026-09-05 | 27 | no GGUF from the vendor or a trusted publisher |
| `openbmb/MiniCPM5-2B-SFT` | 2026-08-27 | 24 | no GGUF from the vendor or a trusted publisher |
| `tencent/ContextPilot-14B` | 2026-08-27 | 22 | licence is other |
| `openbmb/MiniCPM5-2B-Midtrain` | 2026-09-01 | 19 | no GGUF from the vendor or a trusted publisher |
| `inclusionAI/Ling-3.0-flash-dspark` | 2026-08-09 | 17 | licence is other; no GGUF from the vendor or a trusted publisher |
| `nvidia/NVIDIA-Nemotron-Labs-Teacher-General-Reasoning` | 2026-08-14 | 14 | licence is other; no GGUF from the vendor or a trusted publisher |
| `tencent/ContextPilot-8B` | 2026-08-27 | 10 | licence is other; no GGUF from the vendor or a trusted publisher |
| `CohereLabs/tiny-aya-l2-thinker` | 2026-09-02 | 10 | licence is cc-by-nc-4.0; no GGUF from the vendor or a trusted publisher |
| `nvidia/NVIDIA-NemotronLabs-AI-for-Media-Sports-Tennis` | 2026-09-01 | 9 | licence is other; no GGUF from the vendor or a trusted publisher |

## Trending outside the watchlist

Not screened; listed so a new publisher is noticed. Add the org to the watchlist to bring it in.

- `Edge0/Edge0-35B-A3B-preview` · 2026-09-08 · 3284 likes
- `TokenRhythm/NeoHorse-1-4B` · 2026-09-05 · 2259 likes
- `XHToken/Spark-X2.5-4B` · 2026-08-24 · 1258 likes
- `nex-agi/Nex-N2.5-mini` · 2026-09-08 · 830 likes
- `TokenRhythm/NeoHorse-1-9B` · 2026-09-05 · 818 likes
- `nex-agi/Nex-N2.5-Pro` · 2026-09-08 · 660 likes
- `ornith-ai/Ornith-1.5-35B-A3B` · 2026-08-18 · 621 likes
- `ukisai/Swift-Qwen3.8-27b` · 2026-09-08 · 370 likes
- `IFM/K2-Horizon-MoVA-36B-A4B` · 2026-09-01 · 331 likes
- `sensenova/SenseNova-U1.5-8B-MoT` · 2026-08-19 · 248 likes
- `harshatheg/Qwen-2.5-1B-RLCD` · 2026-09-16 · 237 likes
- `Agnes-AI/Agnes-3.0-Flash` · 2026-09-11 · 213 likes
