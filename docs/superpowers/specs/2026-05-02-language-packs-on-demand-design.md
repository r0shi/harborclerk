# Language packs on demand — design spec

**Date:** 2026-05-02
**Status:** Spec — implementation deferred
**Related:** PR #258 (TMPDIR fix that exposed the missing `fra.traineddata`); the on-demand model download pattern already used for LLM models in `models_dir`

## Goal

Build a System Settings → Languages page where each non-English language is opt-in. Enabling a language triggers automatic download of the right model artifacts for every tool that supports it (Tesseract OCR, spaCy NER, anything else added later). Disabling unloads them and reclaims disk. Default behaviour for a fresh install is English-only — matches today's bundle, no surprise downloads.

## Why this is needed

The current bundle ships English-only OCR data (`eng.traineddata` + `osd.traineddata`). The OCR worker calls Tesseract with `-l eng+fra` despite `fra.traineddata` not being present — after PR #258, this is a clean "Failed loading language fra" warning, but the worker proceeds with English-only OCR even on French content. **This is a silent recall regression for any French documents in the corpus.**

The right fix isn't to bundle more languages (every additional traineddata file is ~25 MB on average and most users don't need most of them). It's to let the user choose which languages they care about and pull only those.

The embedder (multilingual-e5-small) is genuinely multilingual and works for 100+ languages out of the box — no per-language download needed. NER (spaCy), OCR (Tesseract), and any future topic-specific tooling do need per-language artifacts.

## Design philosophy alignment

This project is a textbook fit for the established Harbor Clerk design philosophy:

- **AAA tools for lay users** — Tesseract + spaCy are the industry-standard professional choices; the language-pack scheme makes them easy to enable
- **Bundle, don't burden** — English ships in the bundle (no setup needed for the common case); other languages still feel "bundled" in the sense that the user picks from a curated list with one click, no hunting for download URLs
- **Graceful degradation with nudges** — French documents OCR'd before the user enables French still produce English-only output (graceful), but the UI surfaces a clear "you have French documents but no French language pack" prompt (active nudge)

## User-visible behaviour

### System Settings → Languages page

A new admin route at `/admin/languages`. Lists every supported language with:

- Language name (e.g. "French")
- Locale code (e.g. `fr`)
- Per-tool checkboxes / status indicators showing what's available:
  - OCR (Tesseract): `Available` / `Download (~25 MB)` / `Downloading 35%` / `Failed`
  - NER (spaCy): `Available` / `Download (~50 MB)` / `Downloading 35%` / `Failed`
  - Embedder: always `Built-in` (multilingual model handles all)
- Total disk used per language (sum of all enabled tools' artifacts)
- "Enable" button (downloads all enabled-by-default tools for that language)
- "Disable" button (removes the artifacts and unloads from running services)

### Per-language packs

A "language pack" for a given language is the set of model artifacts the user has enabled for that language. Enabling French at the default level might pull both `fra.traineddata` and `fr_core_news_sm`, totalling ~75 MB. Power users can toggle individual tool artifacts (e.g. enable French OCR only, not French NER).

### Default behaviour

Fresh install: only English is enabled. No downloads happen on first run. Onboarding wizard adds an optional "Languages" step where the user can pick additional languages before ingesting their first document.

### Migration story

Existing English-OCR'd documents are NOT auto-reprocessed when a new language is added — that's too expensive on a multi-thousand-document corpus. Instead:

- New documents OCR with the current language list (English + any enabled languages)
- A per-document `OCR languages used` tag is recorded so the operator can see what got applied
- The doc detail page gains a "Reprocess with French" affordance once French is enabled

This keeps the default cheap (no surprise reprocess storms) while making the upgrade path explicit and discoverable.

## Tool capability matrix

The cornerstone of the implementation is a single source of truth mapping `(language code, tool) → artifact spec`. Each spec carries:

- Source URL (HuggingFace, official Tesseract repo, official spaCy CDN, etc.)
- Expected SHA256
- Approximate size for the UI
- Where to install on disk

### Tesseract OCR

Tesseract supports ~100 languages via traineddata files from the [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) repo. Each is roughly 1–25 MB.

The full list comes from `tesseract --list-langs` after pulling the tessdata index. The Languages page presents the most common ~30 by default with an "Show all 100+ languages" expander.

Install location: `~/Library/Application Support/Harbor Clerk/lang-packs/<lang>/tesseract/<lang>.traineddata`. Tesseract's `TESSDATA_PREFIX` env var is configured to look at the bundle's tessdata path **and** the user's lang-packs path (concatenated via the `:` separator on Unix).

### spaCy NER

spaCy publishes named-entity models for ~25 languages, each ~10–500 MB depending on the model. We use the `_sm` variants (smallest, English-equivalent quality) for parity with the bundled `en_core_web_sm`.

Naming convention varies by language family — `fr_core_news_sm`, `de_core_news_sm`, `es_core_news_sm`, `zh_core_web_sm`, etc. The capability map encodes the right artifact name per language.

Install location: spaCy's standard package directory (managed via `pip install --target` or the spaCy `download` subcommand pointed at the lang-packs directory).

### Tika

Mostly language-agnostic for extraction. AutoDetect handles content-type from bytes; OCR is delegated to Tesseract. No per-language artifacts needed for Tika itself.

### Embedder (multilingual-e5-small)

Already multilingual. No per-language download needed. Documented on the Languages page as "Built-in: handles all 100+ languages".

### Topics / clustering

Uses embeddings, which are language-agnostic. No per-language artifacts needed.

### Future tools

The capability map is the extension point. When a new tool with per-language artifacts is added, add an entry in the map and the Languages page picks it up automatically.

## Architecture

### Single source of truth

A new module at `src/harbor_clerk/languages.py`:

```python
from dataclasses import dataclass
from enum import Enum

class Tool(Enum):
    OCR = "ocr"        # Tesseract
    NER = "ner"        # spaCy
    # extension point — add new tools here

@dataclass(frozen=True)
class ArtifactSpec:
    url: str
    sha256: str
    size_bytes: int
    install_subpath: str  # relative to lang-packs/<lang>/

@dataclass(frozen=True)
class LanguageSpec:
    code: str          # ISO 639-1, e.g. "fr"
    display_name: str  # "French"
    artifacts: dict[Tool, ArtifactSpec]

LANGUAGES: dict[str, LanguageSpec] = {
    "en": LanguageSpec(...),  # English — bundled, all artifacts ship in the app
    "fr": LanguageSpec(
        code="fr",
        display_name="French",
        artifacts={
            Tool.OCR: ArtifactSpec(
                url="https://github.com/tesseract-ocr/tessdata_fast/raw/main/fra.traineddata",
                sha256="...",
                size_bytes=25_000_000,
                install_subpath="tesseract/fra.traineddata",
            ),
            Tool.NER: ArtifactSpec(
                url="https://github.com/explosion/spacy-models/releases/download/.../fr_core_news_sm-3.8.0-py3-none-any.whl",
                sha256="...",
                size_bytes=50_000_000,
                install_subpath="ner/fr_core_news_sm-3.8.0-py3-none-any.whl",
            ),
        },
    ),
    # ... ~25 more for the curated default list
}
```

Language code scheme: ISO 639-1 (`en`, `fr`, `de`) for the user-facing identifier, with per-tool naming translation handled inside `ArtifactSpec.install_subpath` and the tool wiring code. Avoids the confusion of mixing Tesseract codes (`eng`, `fra`) with spaCy model names (`en_core_web_sm`, `fr_core_news_sm`).

### Download manager

A new module at `src/harbor_clerk/lang_packs/manager.py` providing:

- `start_download(lang_code: str, tool: Tool) -> DownloadHandle` — kicks off a fetch in a background worker, returns a handle for progress polling
- `download_status(lang_code: str, tool: Tool) -> DownloadStatus` — poll for `not_started | downloading(progress, total) | verifying | installed | failed(error)`
- `verify_artifact(lang_code: str, tool: Tool) -> bool` — SHA256 check on disk
- `remove_artifact(lang_code: str, tool: Tool) -> None` — disable / reclaim disk
- `installed_languages() -> list[str]` — what's available right now
- `installed_tools_for(lang_code: str) -> set[Tool]` — what specifically is available for this language

Storage layout:

```
~/Library/Application Support/Harbor Clerk/lang-packs/
├── fr/
│   ├── tesseract/
│   │   └── fra.traineddata
│   └── ner/
│       └── fr_core_news_sm-3.8.0-py3-none-any.whl
└── de/
    └── tesseract/
        └── deu.traineddata
```

For Tesseract: write `<TESSDATA_PREFIX>:<USER_LANG_PACKS_PATH>` so the engine looks at both. For spaCy: install the wheel with `pip install --target=<USER_LANG_PACKS_PATH>/<lang>/ner` and prepend that path to `sys.path` at worker startup.

Reuse pattern: where it makes sense, lean on the existing `models_dir` LLM download mechanism rather than rolling our own — same progress-reporting UI patterns, same disk-checkpoint integrity model.

### Preferences storage

Per-user preference indicating which languages are enabled. Stored in `users.preferences` JSON column under `enabled_languages: list[str]`. Default is `["en"]`. Admin-level setting (per-tenant in a single-tenant appliance: it's just one global list).

### Dynamic per-job language selection

Replaces the hardcoded `TESSERACT_LANG = "eng+fra"` in `worker/stages/ocr.py`.

At job-claim time, the worker reads:

1. `documents.ocr_languages_used` (existing, may be NULL for legacy docs) — if set, respect it (deterministic re-runs)
2. Otherwise read the global `enabled_languages` preference and build the Tesseract `-l` arg as `"+".join(sorted(enabled))` in the Tesseract code-format

For NER (`worker/stages/entities.py`): per-chunk language detection (already exists via `langdetect`) — pick the spaCy model matching the chunk's detected language. If the language pack isn't installed, fall back to no-NER for that chunk and log a warning.

### Per-doc auto-detect (deferred bonus)

Two-pass scheme: run OCR with `osd.traineddata` only to detect script + run-length, then call the right pack. Behind a default-off "auto-detect document language" toggle. Doesn't ship in the initial cut.

## Implementation phases (rough sequencing)

1. **Inventory & static map.** Build `src/harbor_clerk/languages.py` with the `LANGUAGES` dict for the curated default ~25 languages. Static, no downloads. Lock down the language code scheme (ISO 639-1).
2. **Download manager.** API + worker subroutines for fetch + progress + integrity check. Reuse the LLM download UI patterns where possible.
3. **System Settings UI page.** `/admin/languages` route, list of supported languages with download buttons + size + status indicators.
4. **Wire OCR to read from preferences.** Replace the hardcoded `eng+fra` with the dynamic per-doc lang list. Capture per-doc `ocr_languages_used` so re-runs are deterministic.
5. **Wire NER to read from preferences.** spaCy model load on demand (lazy import per language); fall back to no-NER if pack absent.
6. **(Defer) Per-doc auto-detect.** Bonus that makes multi-lang corpora actually convenient.

Each phase produces independently shippable behaviour:

- After phase 1: nothing changes for users; foundation is in place
- After phase 2 + 3: users can enable/disable languages from the UI, but the worker still hardcodes English; useful for testing the download path
- After phase 4: French OCR actually works for users who enabled French. **This is the ship-blocker for closing the silent French recall regression.**
- After phase 5: French NER works
- After phase 6: French auto-detection works

## Default behaviour

Fresh install:
- `enabled_languages = ["en"]`
- No surprise downloads on first run
- Onboarding wizard adds a "Languages" step with checkboxes; user can pick additional languages before ingesting their first document
- The corpus banner ("0 documents, drop a folder to start") gains a small "Add languages" link

Upgrade from pre-language-packs install:
- Existing `enabled_languages` migration: defaults to `["en"]` (since no language packs were ever installed)
- A one-time post-upgrade modal explains that French OCR was effectively English-only on prior versions and offers to enable French now (with download confirmation)

## Migration / cleanup

Existing documents OCR'd as English-only do NOT auto-reprocess when a new language is added. Reasons:

- Multi-thousand-doc corpora make this prohibitively expensive
- Many documents in the corpus may genuinely be English-only — re-OCR is wasted work
- Auto-reprocess would burn user trust ("why is the system thrashing?")

Instead:
- Per-doc `ocr_languages_used` column on `documents` records what was applied
- Doc detail page shows the languages used and offers a "Reprocess with [enabled languages]" button per-doc
- Admin maintenance page exposes a "Reprocess all docs OCR'd before language X was enabled" batch action with a clear progress indicator and time estimate

## Open questions for implementation

These are decisions to make at planning time, not now:

1. **Curated default list size.** ~25 languages covers most common needs. Should the UI show a "show all 100+" expander? How is "all 100+" surfaced when the master list comes from a remote tessdata index?
2. **Network failure UX.** What if `github.com/tesseract-ocr/tessdata_fast` is down when the user clicks Enable? Retry behaviour? Offline cache?
3. **Custom CDN.** Do we mirror the artifacts on our own CDN for reliability? Probably yes for the curated default list; pass-through for the long tail.
4. **Removal semantics.** When the user disables French, do we delete the artifacts immediately or move to a "trash" / "recently disabled" state for 30 days in case they re-enable?
5. **Embedder rotation.** If we ever swap the embedder for a non-multilingual one, the language-pack story for embedder needs to expand. Out of scope for this spec but worth noting.
6. **Telemetry.** What anonymous usage signals (if any) do we want from "which languages are people enabling"? Out of scope, but the static map makes it possible to add later.

## Out of scope for this design

- Translation. Harbor Clerk does not translate documents; it ingests them in their original language and retrieval works because the embedder is multilingual.
- Custom user-supplied artifacts. Power users who want to plug in a custom traineddata file can drop it into the lang-packs directory manually; a UI for this is not in scope.
- Per-folder language preferences. Watched folders all share the same `enabled_languages` setting. Folder-level overrides could come later.
- Apple Vision OCR or other macOS-native OCR engines. Tesseract is the AAA tool; sticking with it keeps Linux/Docker users at parity.

## Testing strategy

The static map is unit-testable: given a `(language_code, tool)` pair, assert the spec parses, the SHA is hex, the size is positive, the URL is parseable.

The download manager is integration-testable against a local HTTP server serving small fixture artifacts (use `pytest-httpserver`); SHA mismatch produces `failed`, partial download then resume works, etc.

The OCR/NER wiring needs a synthetic French document fixture (a small text image with French content) and assertions that the right Tesseract `-l` arg is passed and that the resulting OCR text contains the expected accented characters.

The reprocess-on-language-add affordance needs an end-to-end test: ingest a French doc with English-only enabled (assert garbage OCR), enable French, click reprocess, assert the doc now has clean French OCR.

## Companion documents

- `docs/release-notes/2026-05-stage-3-caller-changes.md` — Stage 3 release notes (caller-facing changes, including the OCR-French-fallback note)
- Memory note: `~/.claude/projects/-Users-alex-mcp-gateway/memory/project_language_packs_on_demand.md` — original brainstorm capturing the user's stated vision
