"""Synthetic bilingual small-business corpus generator.

Generates ~300 documents for fictional company ``Marbledock & Associates``
across 10 document types using Sonnet 4.6. Each doc gets a JSON sidecar
with structured ground-truth facts (vendor, dates, signatories, totals)
authored at generation time.

A configurable subset is rendered to PDF at low DPI with noise/rotation
to force OCR. The rest are written as plain text + bilingual variants
where applicable.

Idempotent via marker file ``.acquired``. Resuming a partial generation
relies on per-doc filenames being content-addressed (numeric prefix +
type), so a re-run only fills in the missing slots.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import anthropic

from .manifest import CorpusManifest

# The spend estimate prices generation by this name, so it is stated once.
GENERATION_MODEL = "claude-sonnet-4-6"

DOC_COUNTS_DEFAULT = {
    "invoice": 60,
    "onboarding_letter": 40,
    "board_minutes": 30,
    "vendor_contract": 30,
    "internal_memo": 30,
    "policy_doc": 30,
    "marketing_brief": 20,
    "employee_handbook": 20,
    "quarterly_report": 20,
}

OCR_SUBSET_COUNT_DEFAULT = 20

COMPANY = {
    "name": "Marbledock & Associates",
    "founded": 2009,
    "industry": "professional services",
    "employees": 42,
    "locations": ["Toronto", "Montréal"],
    "key_people": {
        "ceo": "Margaret Holvan",
        "cfo": "Pierre Dubois",
        "head_of_ops": "Aiyana Park",
    },
    "key_clients": ["Acme Corp", "Northwind Partners", "Polestar Industries"],
    "key_vendors": ["Globex Supplies", "Initech Software", "Cyberdyne IT"],
}

PROMPT_TEMPLATES = {
    "invoice": (
        "Generate a realistic invoice from a vendor of {company_name} dated {date}. "
        "Use ONE of the company's known vendors. Total between $1000 and $50000. "
        "Return a JSON object with keys: text (the full invoice as plain text) and "
        "facts (object with vendor, invoice_number, date, total_usd, line_items). "
        "Output JSON only, no prose."
    ),
    "onboarding_letter": (
        "Generate an employee onboarding letter for {company_name} dated {date}. "
        "Mix English and French naturally for a Canadian company (about 60/40). "
        "Return JSON with keys: text and facts (object with employee_name, role, "
        "start_date, languages_used, signing_manager). Output JSON only."
    ),
    "board_minutes": (
        "Generate board meeting minutes for {company_name} for {date}. "
        "Include 4-6 agenda items, attendees from key_people, and concrete decisions. "
        "Return JSON with keys: text and facts (object with date, attendees, "
        "decisions, lang). Output JSON only."
    ),
    "vendor_contract": (
        "Generate a vendor service contract between {company_name} and one of its "
        "vendors, dated {date}. Include term length, payment terms, governing law. "
        "Return JSON with keys: text and facts (object with vendor, term_months, "
        "monthly_fee_usd, governing_law, signatures). Output JSON only."
    ),
    "internal_memo": (
        "Generate an internal company memo for {company_name} dated {date} on a "
        "policy or operational topic. Return JSON with keys: text and facts "
        "(object with from, to, subject, lang). Output JSON only."
    ),
    "policy_doc": (
        "Generate a company policy document (HR, security, etc.) for {company_name} "
        "dated {date}. Include version number. Return JSON with keys: text and "
        "facts (object with policy_name, version, effective_date, owner). Output JSON only."
    ),
    "marketing_brief": (
        "Generate a marketing brief for {company_name} dated {date} for a campaign. "
        "Return JSON with keys: text and facts (object with campaign_name, target, "
        "budget_usd, owner). Output JSON only."
    ),
    "employee_handbook": (
        "Generate an excerpt from the {year} employee handbook of {company_name} "
        "with parallel English and French sections. Return JSON with keys: text "
        "and facts (object with year, sections, lang_split). Output JSON only."
    ),
    "quarterly_report": (
        "Generate a quarterly report excerpt for {company_name} for Q{q} {year}. "
        "Return JSON with keys: text and facts (object with quarter, year, "
        "revenue_usd, key_initiatives). Output JSON only."
    ),
}


def _make_client():
    from scripts.test_corpora.runner import spend

    return spend.anthropic_client("synthetic_doc")


def _doc_names(doc_counts: dict[str, int] | None = None) -> list[tuple[str, str]]:
    """(doc_type, file stem) for every document of the corpus, in generation order."""
    names, seq = [], 0
    for doc_type, n in (doc_counts or DOC_COUNTS_DEFAULT).items():
        for _ in range(n):
            seq += 1
            names.append((doc_type, f"{seq:04d}_{doc_type}"))
    return names


def _is_generated(ingest_dir: Path, base: str) -> bool:
    # The facts file is written last, so it marks a finished document. The .txt may since have become a .pdf.
    return (ingest_dir / f"{base}.json").exists() and any(
        (ingest_dir / f"{base}{s}").exists() for s in (".txt", ".pdf")
    )


def planned_generation_count(workdir: Path, doc_counts: dict[str, int] | None = None) -> int:
    """How many documents `acquire` would generate, for the pre-run spend estimate: none once the corpus is
    acquired, otherwise the ones a stopped generation has not written yet."""
    ingest_dir = Path(workdir) / "ingest"
    if (ingest_dir / ".acquired").exists() and any(d.suffix in {".txt", ".pdf"} for d in ingest_dir.glob("*")):
        return 0
    return sum(1 for _, base in _doc_names(doc_counts) if not _is_generated(ingest_dir, base))


def _draw_prompt(doc_type: str, rng: random.Random) -> str:
    """The prompt for the next document. Apart from `_generate_one` so that a resumed generation can draw
    (and discard) the prompts of documents it already has, and leave the later ones as they would have been."""
    template = PROMPT_TEMPLATES[doc_type]
    # Pick a deterministic-ish date in 2025
    date = f"2025-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
    return template.format(
        company_name=COMPANY["name"],
        date=date,
        year=2025,
        q=rng.randint(1, 4),
    )


def _generate_one(
    client: anthropic.Anthropic,
    doc_type: str,
    rng: random.Random,
) -> dict:
    prompt = _draw_prompt(doc_type, rng)
    msg = client.messages.create(
        model=GENERATION_MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    from scripts.test_corpora.runner import spend

    spend.get_meter().count_unit("synthetic_doc")
    text = msg.content[0].text
    # Extract the first JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"no JSON in response for {doc_type}: {text[:200]}")
    return json.loads(text[start : end + 1])


def _render_to_pdf_with_noise(text: str, out_path: Path, rng: random.Random) -> None:
    """Render plain text to a PDF at 150 DPI with slight noise/rotation."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1275, 1650), "white")  # 8.5x11 at 150 DPI
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
    except OSError:
        font = ImageFont.load_default()
    y = 50
    for line in text.split("\n"):
        draw.text((60, y), line, fill="black", font=font)
        y += 18
        if y > 1600:
            break
    # Rotate by ±2° and add noise
    img = img.rotate(rng.uniform(-2, 2), fillcolor="white")
    # Save as PDF (Pillow can do this directly)
    img.save(out_path, "PDF", resolution=150.0)


def acquire(
    workdir: Path,
    doc_counts: dict[str, int] | None = None,
    ocr_subset_count: int = OCR_SUBSET_COUNT_DEFAULT,
) -> CorpusManifest:
    workdir = Path(workdir)
    ingest_dir = workdir / "ingest"
    marker = ingest_dir / ".acquired"
    counts = doc_counts or DOC_COUNTS_DEFAULT

    if marker.exists():
        docs = sorted(ingest_dir.glob("*"))
        doc_count = sum(1 for d in docs if d.suffix in {".txt", ".pdf"})
        # Marker without files is a stale-cache sentinel — fall through to
        # the generate path so the marker doesn't trick the caller into
        # thinking a corpus exists when its files are gone.
        if doc_count > 0:
            return CorpusManifest(
                corpus_id="synthetic",
                ingest_dir=ingest_dir,
                doc_count=doc_count,
                total_size_bytes=sum(d.stat().st_size for d in docs if d.is_file()),
                license="generated",
                notes="synthetic bilingual small-business",
            )
        marker.unlink()  # remove stale marker so future runs don't keep tripping it

    ingest_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(42)
    client = _make_client()

    all_docs: list[tuple[str, dict]] = []  # (type, generated dict)
    for doc_type, base in _doc_names(counts):
        if _is_generated(ingest_dir, base):
            # Bought by an earlier, stopped generation (the spend cap, Ctrl-C). Not bought again. One the OCR
            # step already turned into a PDF has no text left to read, and needs none: that step skips it.
            _draw_prompt(doc_type, rng)
            txt = ingest_dir / f"{base}.txt"
            gen = {
                "text": txt.read_text() if txt.exists() else None,
                "facts": json.loads((ingest_dir / f"{base}.json").read_text()),
            }
            all_docs.append((doc_type, gen))
            continue
        try:
            gen = _generate_one(client, doc_type, rng)
        except Exception as exc:
            # Skip individual failures rather than abort the whole sweep
            gen = {"text": f"[generation failed: {exc}]", "facts": {"_error": str(exc)}}
        (ingest_dir / f"{base}.txt").write_text(gen["text"])
        (ingest_dir / f"{base}.json").write_text(json.dumps(gen.get("facts", {}), indent=2))
        all_docs.append((doc_type, gen))

    # OCR subset: pick N docs randomly, render to PDF, replace .txt
    if ocr_subset_count > 0 and all_docs:
        ocr_indices = rng.sample(range(len(all_docs)), min(ocr_subset_count, len(all_docs)))
        for idx in ocr_indices:
            doc_type, gen = all_docs[idx]
            seq_num = idx + 1
            base = f"{seq_num:04d}_{doc_type}"
            txt_path = ingest_dir / f"{base}.txt"
            pdf_path = ingest_dir / f"{base}.pdf"
            if gen["text"] is None:
                continue  # rendered by the generation this one resumes
            _render_to_pdf_with_noise(gen["text"], pdf_path, rng)
            txt_path.unlink(missing_ok=True)

    marker.write_text("acquired")
    return CorpusManifest(
        corpus_id="synthetic",
        ingest_dir=ingest_dir,
        doc_count=sum(1 for d in ingest_dir.glob("*") if d.suffix in {".txt", ".pdf"}),
        total_size_bytes=sum(d.stat().st_size for d in ingest_dir.glob("*") if d.is_file()),
        license="generated",
        notes="synthetic bilingual small-business",
    )
