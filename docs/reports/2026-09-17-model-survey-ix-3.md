# Model survey — 2026-09-17

- **Run id:** `ix-3` · commit `v0.9.2-21-gba6e55c` · window: releases since 2026-07-22 · 27 vendors watched
- **Previous survey:** none · 0 release(s) carried from its lists, 0 examined again (the rest were already in the window, or are gone)
- **Policy:** Q4_K_M at most 24 GB, context at least 32768, tool calling in the chat template, permissive licence, GGUF from the vendor or unsloth, ggml-org, bartowski, lmstudio-community (`scripts/model_survey/watchlist.yaml`)
- **Coverage:** 51 releases examined · 159 left out by name or task · 0 over the cap of 12 per vendor · all three are listed below, with a count per vendor · ⚠ the successor and gap search saw only the newest 500 repos of `google`, back to 2024-02-13
- **llama.cpp:** pinned `b9018` (126 architectures); latest stable release `v0.4.1` (2026-09-14, 151); master 152
- **Method:** Hugging Face Hub API (unauthenticated) and GitHub; no model was run, so machine, deployment and corpus do not apply. Judge model: n/a · cloud spend: n/a

## Reading

**Headline.** `Qwen/Qwen3.8-27B` ranks first by a wide margin (65.2 against 24.7 for the runner-up), and it is
the only Qwen3.8 release this product can use. Apache-2.0, 16.5 GB at Q4_K_M, 262K native context, tool calls in the
chat template, 15,500 likes and 8.2M GGUF downloads in six weeks. Its architecture is `qwen35`, which the pinned
llama.cpp already loads, so it can be tried today without touching the pin. The other two Qwen3.8 releases are out of
reach: Flash-Next is 119.6 GB under a non-permissive licence and the 2.4T flagship is a datacentre model. There is no
vendor Qwen3.8 below 27B. The 2B, 4B and 9B "Qwen3.8" builds on the Hub are third-party distills, not candidates.

**It is not a drop-in replacement for the heavy tier.** It is dense: every token reads all 16.5 GB. On the mini (M4,
120 GB/s) that caps decode near 7 tokens per second. The incumbent `qwen36-35b-a3b` activates 3B parameters per token
and has a ceiling several times higher. Ask is bounded by 25 tool rounds rather than by time, and acceptance work on
the mini has already seen one Ask run for 19 minutes (noted in `acceptance/hc_client.py`; the model was not
recorded, and no curated model is as slow as a dense 27B would be), so wall time per answered question has to be
measured beside accuracy. The likely outcome is a quality tier next to the mixture-of-experts model, aimed at Macs with more memory
bandwidth, not a swap. Speculative decoding (carried from #551) is what could make it practical on the mini, and the
drafter architectures (`eagle3`, `dflash`, `gemma4-assistant`) are past the pin but in the stable release `v0.4.1`.

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

- Nothing is missing: all five registered files are in their repos at the registered sizes.
- #548 is confirmed: the registry gives Gemma 128000 tokens where the GGUF carries 262144.
- #550 needs a ten-minute check before anyone acts. bartowski's Gemma repo changed on 2026-07-27, after the issue was
  filed. Its chat template still differs from unsloth's (18,681 against 18,922 characters), so the update did not
  close the issue, but the claim that it is two fixes behind should be re-read against a diff.
- Docker Compose runs the floating `ghcr.io/ggml-org/llama.cpp:server` while macOS pins `b9018`. The two deployments
  run different llama.cpp versions, and Compose changes without a commit. Pin both to one release when #549 lands.

**The pin.** It blocks one ranked candidate, `Ling-3.0-tiny` (`bailingmoe3`), which `v0.4.1` carries. The pin is four
and a half months old, and upstream now cuts stable semver releases, so #549 should target `v0.4.1` rather than
another rolling `b` build. Only one architecture (`hrm_text`) is on master and in no stable release, and nothing here
needs it. It
lands first because it re-baselines tool-call grammar for every model and unlocks the drafters.

**Other candidates.** `MiniCPM5-2B` (1.6 GB, Apache-2.0, loads today) is a possible tier below `qwen3-4b` for 8 GB
Macs. Models that small are usually weakest at tool calling, so expect evaluation to decide against it, but the test
is cheap. Granite 4.2 (Apache-2.0, vendor GGUF, 131K): the 8b competes directly with Qwen3.5-9B and earns one run;
the 3b and 30b do not displace anything. `Ling-3.0-tiny` waits for #549. `swiss-ai/Apertus-v1.5-8B` is multilingual with French and
permissively licensed but has no trusted GGUF yet. It is on the waiting list below, so the next survey examines it again. That list
also holds a few untagged research artefacts (a climate model, a question generator) that are examined because Mistral
publishes real releases without a task tag; they cost a few requests per run and drop off after 120 days. Outside the watchlist,
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

**Coverage.** 51 releases examined, none over the per-vendor cap. Eleven watched vendors published nothing in the
window, Mistral and OpenAI among them; the Vendors section shows each listing was read and was not empty. Google's
catalogue is longer than the 500 repos the successor search reads, reaching back to 2024-02-13, which does not
affect Gemma 4.

**Not verified.** No model was run. Tool calling under llama-server, French quality, throughput and every vendor
benchmark are unverified. The 7 tokens per second figure is a bandwidth ceiling worked out from file size, not a
measurement. Popularity is a signal that a model works for someone, not that it works here.

## Candidates, ranked

| # | Release | Released | Licence | Params | GGUF | Q4 GB | Context | Arch | Score | Why |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `Qwen/Qwen3.8-27B` | 2026-08-05 | apache-2.0 | 27.8B | `unsloth/Qwen3.8-27B-GGUF` | 16.5 | 262144 | qwen35 | 65.2 | successor of a curated family +40; popularity +20; recency +5.2 |
| 2 | `openbmb/MiniCPM5-2B` | 2026-09-06 | apache-2.0 | 2.5B | `openbmb/MiniCPM5-2B-GGUF` | 1.6 | 131072 | llama | 24.7 | popularity +15.9; recency +8.8 |
| 3 | `inclusionAI/Ling-3.0-tiny` | 2026-08-10 | mit | 7.9B | `inclusionAI/Ling-3.0-tiny-GGUF` | 4.8 | 131072 | bailingmoe3 | 19.1 | popularity +13.3; recency +5.8. ⚠ architecture 'bailingmoe3' needs a llama.cpp upgrade past the pin (v0.4.1 has it). |
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

Size-matched successors from the vendor's whole catalogue, not only the window, put through the same screen.

| Curated | Successor | Released | Licence | GGUF | Q4 GB | Context | Arch | Screen |
|---|---|---|---|---|---|---|---|---|
| `qwen3-8b` (5.0 GB) | `Qwen/Qwen3.5-9B` | 2026-02-27 | apache-2.0 | `unsloth/Qwen3.5-9B-GGUF` | 5.7 | 262144 | qwen35 | passes |
| `qwen3-4b` (2.5 GB) | `Qwen/Qwen3.5-4B` | 2026-02-27 | apache-2.0 | `unsloth/Qwen3.5-4B-GGUF` | 2.7 | 262144 | qwen35 | passes |
| `qwen36-35b-a3b` (22.1 GB) | `Qwen/Qwen3.8-27B` | 2026-08-05 | apache-2.0 | `unsloth/Qwen3.8-27B-GGUF` | 16.5 | 262144 | qwen35 | passes |

## Gaps in the size ladder

Members of a curated family that would sit in a gap of the ladder wider than 4 GB, put through the same screen.

| Release | Released | Licence | Gap | GGUF | Q4 GB | Context | Arch | Screen |
|---|---|---|---|---|---|---|---|---|
| `google/gemma-4-12B-it` | 2026-05-23 | apache-2.0 | 5 to 11.6 GB | `unsloth/gemma-4-12b-it-GGUF` | 7.1 | 262144 | gemma4 | passes |
| `google/gemma-4-31B-it` | 2026-03-11 | apache-2.0 | 17 to 22.1 GB | `unsloth/gemma-4-31B-it-GGUF` | 18.3 | 262144 | gemma4 | passes |

## llama.cpp

In the stable release `v0.4.1` but not in the pin: `bailingmoe3`, `cohere2moe`, `deepseek32`, `deepseek4`, `dflash`, `dots3note`, `eagle3`, `gemma4-assistant`, `granite_swa`, `graniteswitch`, `hy_v3`, `hy_v4`, `kimi-k3`, `laguna`, `maple`, `mellum`, `minimax-01`, `minimax-m3`, `muse-glimmer`, `nanbeige`, `pockettts`, `qwen3tts`, `qwen4exp`, `spark2_5`, `talkie`.

Only on master, in no stable release yet: `hrm_text`.

## Screened out

Every release that was examined and failed the screen, with every reason.

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
| `nvidia/NVIDIA-NemotronLabs-VoiceChat-11B` | 2026-07-29 | 482 | licence is openmdw-1.1; no GGUF from the vendor or a trusted publisher |
| `tencent/Hy4-preview` | 2026-08-27 | 480 | no GGUF from the vendor or a trusted publisher |
| `inclusionAI/Ling-3.0-flash` | 2026-08-02 | 414 | Q4_K_M is 77.0 GB, over the 24 GB ceiling |
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
| `google/gnm-v3` | 2026-09-01 | 16 | no GGUF from the vendor or a trusted publisher |
| `nvidia/NVIDIA-Nemotron-Labs-Teacher-General-Reasoning` | 2026-08-14 | 14 | licence is other; no GGUF from the vendor or a trusted publisher |
| `tencent/ContextPilot-8B` | 2026-08-27 | 10 | licence is other; no GGUF from the vendor or a trusted publisher |
| `CohereLabs/tiny-aya-l2-thinker` | 2026-09-02 | 10 | licence is cc-by-nc-4.0; no GGUF from the vendor or a trusted publisher |

And 13 with under 10 likes: `nvidia/NVIDIA-NemotronLabs-AI-for-Media-Sports-Tennis` (licence is other, no GGUF from the vendor or a trusted publisher); `CohereLabs/tiny-aya-en-thinker` (licence is cc-by-nc-4.0, no GGUF from the vendor or a trusted publisher); `CohereLabs/North-Mini-Code-1.0-eagle` (no GGUF from the vendor or a trusted publisher); `nvidia/NVIDIA-Nemotron-Labs-Teacher-STEM` (licence is other, no GGUF from the vendor or a trusted publisher); `tencent/Simple-Attention-Sparsification` (licence is unknown, no GGUF from the vendor or a trusted publisher); `tencent/ContextPilot-E4B` (licence is other, no GGUF from the vendor or a trusted publisher); `nvidia/NVIDIA-Nemotron-Labs-Teacher-Competition-Coding` (licence is other, no GGUF from the vendor or a trusted publisher); `microsoft/SQuadGen` (no GGUF from the vendor or a trusted publisher); `nvidia/NVIDIA-Nemotron-Labs-Teacher-Instruction-Following` (licence is other, no GGUF from the vendor or a trusted publisher); `allenai/SamudrACE-E3SMv3` (no GGUF from the vendor or a trusted publisher); `tencent/WeVisDoc-4B` (no GGUF from the vendor or a trusted publisher); `tencent/WeVisDoc-2B` (no GGUF from the vendor or a trusted publisher); `nvidia/NVIDIA-Nemotron-Labs-Teacher-Chat` (licence is other, no GGUF from the vendor or a trusted publisher).

## Waiting for a GGUF

Screened only because no trusted Q4_K_M build exists yet. The next survey reads this list and examines these again although they will be older than its window. A release drops off 120 days after it was published.

- `deepseek-ai/DeepSeek-V4-Flash-0731` · 2026-07-31
- `deepseek-ai/DeepSeek-V4.1-Flash` · 2026-09-10
- `deepseek-ai/DeepSeek-V4-Flash-Vision-Exp` · 2026-08-31
- `deepseek-ai/DeepSeek-V4-Pro-0813` · 2026-08-13
- `tencent/Hy4-preview` · 2026-08-27
- `microsoft/Mage-VL` · 2026-07-25
- `CohereLabs/North-Micro-Vision-Instruct` · 2026-08-10
- `inclusionAI/Ling-3.0-flash-VL` · 2026-09-04
- `swiss-ai/Apertus-v1.5-8B` · 2026-07-24
- `swiss-ai/Apertus-v1.5-70B` · 2026-07-24
- `openbmb/MiniCPM5-2B-DSpark` · 2026-09-06
- `inclusionAI/LLaDA2.2-mini` · 2026-09-05
- `openbmb/MiniCPM5-2B-SFT` · 2026-08-27
- `openbmb/MiniCPM5-2B-Midtrain` · 2026-09-01
- `google/gnm-v3` · 2026-09-01
- `CohereLabs/North-Mini-Code-1.0-eagle` · 2026-07-24
- `microsoft/SQuadGen` · 2026-08-27
- `allenai/SamudrACE-E3SMv3` · 2026-08-04
- `tencent/WeVisDoc-4B` · 2026-09-16
- `tencent/WeVisDoc-2B` · 2026-09-16

## Left out before examination

### Over the per-vendor cap

The least liked of a vendor's eligible releases, past the 12 that were examined. The next survey reads this list and examines them.

- none

### By name or task

This is how fine-tuning artefacts, quantised duplicates and non-chat models are kept out. A model wrongly listed here means a fragment in the policy is too greedy.

- `CohereLabs`: `tiny-aya-base-32K` (name contains '-base'); `North-Small-Translate-1.0-w4a16` (name contains 'translate'); `North-Small-Translate-1.0` (name contains 'translate'); `North-Small-Translate-1.0-fp8` (name contains 'fp8')
- `LiquidAI`: `LFM2.5-VL-3B-MLX-bf16` (name contains 'mlx'); `LFM2.5-VL-3B-MLX-8bit` (name contains 'mlx'); `LFM2.5-VL-3B-MLX-5bit` (name contains 'mlx'); `LFM2.5-VL-3B-MLX-6bit` (name contains 'mlx'); `LFM2.5-VL-3B-MLX-4bit` (name contains 'mlx'); `LFM2.5-VL-3B-ONNX` (name contains 'onnx'); `LFM2.5-2.6B-MLX-nvfp4` (name contains 'fp4'); `LFM2.5-2.6B-MLX-mxfp4` (name contains 'fp4'); `LFM2.5-2.6B-MLX-mxfp8` (name contains 'fp8'); `LFM2.5-2.6B-MLX-4bit` (name contains 'mlx'); `LFM2.5-2.6B-MLX-5bit` (name contains 'mlx'); `LFM2.5-2.6B-MLX-6bit` (name contains 'mlx'); `LFM2.5-2.6B-MLX-8bit` (name contains 'mlx'); `LFM2.5-2.6B-MLX-bf16` (name contains 'mlx'); `LFM2.5-2.6B-MLX` (name contains 'mlx'); `LFM2.5-2.6B-ONNX` (name contains 'onnx'); `LFM2.5-2.6B-Base` (name contains '-base'); `LFM2.5-Encoder-230M` (task is fill-mask); `LFM2.5-Encoder-350M` (task is fill-mask)
- `MiniMaxAI`: `MiniMax-Music3` (task is text-to-audio); `MiniMax-H3` (task is image-text-to-video)
- `Qwen`: `Qwen-Drive-1.0-4B` (name contains 'drive'); `Qwen3.8-Flash-Next-FP8` (name contains 'fp8'); `Qwen3.8-27B-FP8` (name contains 'fp8'); `Qwen3.8-2.4T-A95B-FP8` (name contains 'fp8')
- `ServiceNow-AI`: `gemma4-sft-experiments` (name contains 'experiment')
- `allenai`: `ACE2S-SHiELD-plus-supplemental-checkpoints` (name contains 'checkpoint')
- `google`: `timesfm-3.0-pytorch` (task is time-series-forecasting); `tipsv1-g14-lowres` (task is zero-shot-image-classification); `tipsv1-g14` (task is zero-shot-image-classification); `tipsv1-so400m14` (task is zero-shot-image-classification); `tipsv1-l14` (task is zero-shot-image-classification); `tipsv1-b14` (task is zero-shot-image-classification); `tipsv1-s14` (task is zero-shot-image-classification)
- `ibm-granite`: `granite-4.2-30b-bf16-mlx` (name contains 'mlx'); `granite-4.2-30b-q8-mlx` (name contains 'mlx'); `granite-4.2-8b-bf16-mlx` (name contains 'mlx'); `granite-4.2-3b-bf16-mlx` (name contains 'mlx'); `granite-4.2-30b-q4-mlx` (name contains 'mlx'); `granite-4.2-3b-q8-mlx` (name contains 'mlx'); `granite-4.2-8b-q8-mlx` (name contains 'mlx'); `granite-4.2-3b-q4-mlx` (name contains 'mlx'); `granite-4.2-8b-q4-mlx` (name contains 'mlx'); `granite-speech-5.0-470m-turboctc-nc` (task is automatic-speech-recognition); `granite-4.2-8b-nvfp4` (name contains 'fp4'); `granite-4.2-8b-mxfp4` (name contains 'fp4'); `granite-4.2-8b-fp8` (name contains 'fp8'); `granite-4.2-3b-nvfp4` (name contains 'fp4'); `granite-4.2-3b-mxfp4` (name contains 'fp4'); `granite-4.2-3b-fp8` (name contains 'fp8'); `granite-4.2-30b-nvfp4` (name contains 'fp4'); `granite-4.2-30b-mxfp4` (name contains 'fp4'); `granite-4.2-30b-fp8` (name contains 'fp8'); `granite-timeseries-patchtst-fm-r2` (task is time-series-forecasting); `granite-speech-5.0-470m-turboctc` (task is automatic-speech-recognition)
- `inclusionAI`: `Step-3.7-Flash-singprobe` (name contains 'probe'); `Qwen3.8-27B-singprobe` (name contains 'probe'); `Qwen3.5-397B-A17B-singprobe` (name contains 'probe'); `gpt-oss-120b-singprobe` (name contains 'probe'); `MiniMax-M2.7-singprobe` (name contains 'probe'); `GLM-5.3-singprobe` (name contains 'probe'); `Qwen3.6-35B-A3B-singprobe` (name contains 'probe'); `Qwen3.6-27B-singprobe` (name contains 'probe'); `Qwen3.5-122B-A10B-singprobe` (name contains 'probe'); `Qwen3.5-35B-A3B-singprobe` (name contains 'probe'); `Qwen3.5-27B-singprobe` (name contains 'probe'); `Qwen3.5-9B-singprobe` (name contains 'probe'); `Qwen3.5-4B-singprobe` (name contains 'probe'); `Qwen3.5-2B-singprobe` (name contains 'probe'); `Qwen3.5-0.8B-singprobe` (name contains 'probe'); `Qwen3-8B-singprobe` (name contains 'probe'); `Qwen3-4B-Instruct-2507-singprobe` (name contains 'probe'); `Qwen3-0.6B-singprobe` (name contains 'probe'); `Llama-3.2-1B-Instruct-singprobe` (name contains 'probe'); `Llama-3.1-8B-Instruct-singprobe` (name contains 'probe'); `Hy3-singprobe` (name contains 'probe'); `gpt-oss-20b-singprobe` (name contains 'probe'); `GLM-5.2-singprobe` (name contains 'probe'); `gemma-4-E4B-it-singprobe` (name contains 'probe'); `gemma-4-31B-it-singprobe` (name contains 'probe'); `gemma-4-26B-A4B-it-singprobe` (name contains 'probe'); `DeepSeek-V4-Flash-0731-singprobe` (name contains 'probe'); `Ling-3.0-flash-Fin-fp4` (name contains 'fp4'); `Ling-3.0-flash-Fin-int4` (name contains 'int4'); `Ling-3.0-flash-Fin-fp8` (name contains 'fp8'); `Ling-3.0-flash-VL-int4` (name contains 'int4'); `Ling-3.0-flash-VL-fp4` (name contains 'fp4'); `Ling-3.0-flash-VL-fp8` (name contains 'fp8'); `LLaDA-UI` (name contains '-ui'); `LLaDA-Image-Turbo-FP8` (name contains 'fp8'); `LLaDA-Image-FP8` (name contains 'fp8'); `Ling-3.0-tiny-singprobe` (name contains 'probe'); `Ling-3.0-flash-singprobe` (name contains 'probe'); `LLaDA-Image-Turbo` (name contains 'image'); `LLaDA-Image` (name contains 'image'); `UI-Venus-2-9B` (name contains 'ui-'); `ArmorOCR` (name contains 'ocr'); `Ling-3.0-tiny-base` (name contains '-base'); `Ling-3.0-tiny-base-30T` (name contains '-base'); `Ling-3.0-flash-base` (name contains '-base'); `Ling-3.0-flash-base-30T` (name contains '-base'); `Ling-3.0-flash-base-midtrain` (name contains '-base'); `Ling-3.0-tiny-base-midtrain` (name contains '-base'); `Ling-3.0-tiny-int4` (name contains 'int4'); `Ling-3.0-tiny-fp8` (name contains 'fp8'); `Ling-3.0-flash-fp4` (name contains 'fp4'); `Ling-3.0-flash-int4` (name contains 'int4'); `Ling-3.0-flash-fp8` (name contains 'fp8')
- `microsoft`: `VibeVoice-ASR-Streaming-1.5B` (name contains 'asr'); `VibeVoice-ASR-Streaming-7B` (name contains 'asr'); `Mage-ViT` (task is image-feature-extraction); `VibeVoice-ASR-BitNet` (name contains 'asr')
- `nvidia`: `DeepSeek-V4.1-Flash-NVFP4` (name contains 'fp4'); `c-foundationstereo-s` (task is depth-estimation); `foundationpose` (task is robotics); `DeepSeek-V4-Pro-0813-nvfp4-DSpark` (name contains 'fp4'); `Qwen3.8-27B-NVFP4` (name contains 'fp4'); `GLM-5.3-Flash-NVFP4` (name contains 'fp4'); `Nemotron-3-Labs-Ultra-Math-RL` (name contains 'math'); `Nemotron-3-Labs-Ultra-Math-SFT` (name contains 'math'); `Qwen3.8-Flash-Next-NVFP4` (name contains 'fp4'); `Muse-Glimmer-30B-NVFP4` (name contains 'fp4'); `DeepSeek-V4-Pro-0813-NVFP4` (name contains 'fp4'); `Qwen3.8-2.4T-A95B-NVFP4` (name contains 'fp4'); `Nemotron-3-Diarization-preview` (task is voice-activity-detection); `DeepSeek-V4-Flash-0731-NVFP4` (name contains 'fp4'); `Kimi-K3-NVFP4` (name contains 'fp4'); `cmd` (task is image-to-video); `NVIDIA-Nemotron-3.5-Lightning-30B-A3B-Base-BF16` (name contains 'bf16'); `NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4-DSpark` (name contains 'fp4'); `NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4-DFlash` (name contains 'fp4'); `NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4` (name contains 'fp4'); `NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16` (name contains 'bf16'); `DeepSeek-V4-Pro-nvfp4-DSpark` (name contains 'fp4'); `DeepSeek-V4-Flash-nvfp4-DSpark` (name contains 'fp4')
- `openbmb`: `MiniCPM5-2B-GPTQ` (name contains 'gptq'); `JustRL-II-base-model` (name contains '-base'); `UltraData-Code-L2-Classifier` (name contains 'classifier'); `MiniCPM5-2B-MLX` (name contains 'mlx'); `MiniCPM5-2B-Base` (name contains '-base'); `MathForm-8B` (name contains 'math')
- `tencent`: `EVIE-8B` (task is visual-document-retrieval); `EVIE-4.5B` (task is visual-document-retrieval); `Hy4-preview-FP8` (name contains 'fp8'); `WeMM-Embedding-9B` (name contains 'embedding'); `WeMM-Embedding-4B` (name contains 'embedding'); `WeMM-Embedding-2B` (name contains 'embedding'); `AuK-Flash` (task is text-to-speech); `AuK` (task is text-to-speech); `EVIE-Preview-4.5B` (task is visual-document-retrieval); `UI-Mate-democua-27B` (name contains 'ui-'); `UI-Mate-9B` (name contains 'ui-'); `UI-Mate-27B` (name contains 'ui-')
- `zai-org`: `GLM-5.3-Flash-BF16` (name contains 'bf16'); `GLM-5.3-BF16` (name contains 'bf16')

## Vendors

What each watched vendor's listing held. The next survey reads this list: a vendor that is not on it gets a first-run window of 90 days.

- `Qwen` · 465 listed · 7 since 2026-07-22 · 3 examined
- `google` · 500 listed · 8 since 2026-07-22 · 1 examined
- `openai` · 39 listed · 0 since 2026-07-22 · 0 examined
- `mistralai` · 75 listed · 0 since 2026-07-22 · 0 examined
- `microsoft` · 500 listed · 6 since 2026-07-22 · 2 examined
- `ibm-granite` · 241 listed · 24 since 2026-07-22 · 3 examined
- `meta-llama` · 70 listed · 0 since 2026-07-22 · 0 examined
- `deepseek-ai` · 105 listed · 4 since 2026-07-22 · 4 examined
- `zai-org` · 154 listed · 4 since 2026-07-22 · 2 examined
- `moonshotai` · 19 listed · 0 since 2026-07-22 · 0 examined
- `nvidia` · 500 listed · 30 since 2026-07-22 · 7 examined
- `allenai` · 500 listed · 2 since 2026-07-22 · 1 examined
- `HuggingFaceTB` · 84 listed · 0 since 2026-07-22 · 0 examined
- `LiquidAI` · 174 listed · 24 since 2026-07-22 · 5 examined
- `tencent` · 160 listed · 19 since 2026-07-22 · 7 examined
- `baidu` · 36 listed · 0 since 2026-07-22 · 0 examined
- `InternScience` · 15 listed · 0 since 2026-07-22 · 0 examined
- `inclusionAI` · 208 listed · 59 since 2026-07-22 · 6 examined
- `MiniMaxAI` · 21 listed · 2 since 2026-07-22 · 0 examined
- `stepfun-ai` · 51 listed · 0 since 2026-07-22 · 0 examined
- `ServiceNow-AI` · 11 listed · 1 since 2026-07-22 · 0 examined
- `arcee-ai` · 204 listed · 0 since 2026-07-22 · 0 examined
- `NousResearch` · 126 listed · 0 since 2026-07-22 · 0 examined
- `CohereLabs` · 55 listed · 8 since 2026-07-22 · 4 examined
- `swiss-ai` · 24 listed · 2 since 2026-07-22 · 2 examined
- `apple` · 142 listed · 0 since 2026-07-22 · 0 examined
- `openbmb` · 173 listed · 10 since 2026-07-22 · 4 examined

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
