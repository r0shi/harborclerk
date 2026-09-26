import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.test_corpora.corpora import synthetic


def test_synthetic_acquire_writes_doc_and_sidecar(tmp_path: Path):
    fake_anthropic = MagicMock()
    # Mock returns a templated invoice
    fake_anthropic.messages.create.return_value.content = [
        MagicMock(
            text='{"text": "INVOICE\\nVendor: Acme\\nTotal: $12,500", "facts": {"vendor": "Acme", "total_usd": 12500}}'
        )
    ]

    with patch.object(synthetic, "_make_client", return_value=fake_anthropic):
        m = synthetic.acquire(
            workdir=tmp_path / "synth",
            doc_counts={"invoice": 2},  # only 2 docs to keep the test fast
            ocr_subset_count=0,
        )
    assert m.doc_count == 2
    docs = sorted(m.ingest_dir.glob("*.txt"))
    assert len(docs) == 2
    # Each doc has a JSON sidecar with ground-truth facts, beside the watched folder and never in it (#726)
    assert sorted(m.ingest_dir.glob("*.json")) == []
    sidecars = sorted(synthetic.facts_dir_for(tmp_path / "synth").glob("*.json"))
    assert [p.stem for p in sidecars] == [d.stem for d in docs]
    facts = json.loads(sidecars[0].read_text())
    assert facts["vendor"] == "Acme"


def test_sidecars_an_earlier_generation_left_in_the_watched_folder_are_moved_out_and_not_bought_again(tmp_path: Path):
    """#726: the generation before the fix wrote the answer key into ingest/. acquire keeps that generation (it
    cost money) and moves the sidecars to facts/, so a re-ingest indexes only the documents."""
    workdir = tmp_path / "synth"
    ingest_dir = workdir / "ingest"
    ingest_dir.mkdir(parents=True)
    for base in ("0001_invoice", "0002_invoice"):
        (ingest_dir / f"{base}.txt").write_text("INVOICE")
        (ingest_dir / f"{base}.json").write_text(json.dumps({"vendor": "Acme", "total_usd": 1}))
    (ingest_dir / ".acquired").write_text("acquired")
    # One sidecar already has a copy in facts/: the stray is dropped, not duplicated
    facts_dir = synthetic.facts_dir_for(workdir)
    facts_dir.mkdir()
    (facts_dir / "0002_invoice.json").write_text(json.dumps({"vendor": "Acme", "total_usd": 2}))

    assert synthetic.planned_generation_count(workdir, {"invoice": 2}) == 0
    fake_anthropic = MagicMock()
    with patch.object(synthetic, "_make_client", return_value=fake_anthropic):
        m = synthetic.acquire(workdir=workdir, doc_counts={"invoice": 2}, ocr_subset_count=0)

    fake_anthropic.messages.create.assert_not_called()
    assert m.doc_count == 2
    assert sorted(ingest_dir.glob("*.json")) == []
    assert sorted(p.name for p in facts_dir.glob("*.json")) == ["0001_invoice.json", "0002_invoice.json"]
    assert json.loads((facts_dir / "0002_invoice.json").read_text())["total_usd"] == 2


def test_a_stopped_generation_resumes_from_the_sidecars_in_facts(tmp_path: Path):
    """The resume path reads the facts of a bought document from facts/ and generates only the missing one."""
    workdir = tmp_path / "synth"
    ingest_dir = workdir / "ingest"
    facts_dir = synthetic.facts_dir_for(workdir)
    ingest_dir.mkdir(parents=True)
    facts_dir.mkdir()
    (ingest_dir / "0001_invoice.txt").write_text("INVOICE one")
    (facts_dir / "0001_invoice.json").write_text(json.dumps({"vendor": "Acme", "total_usd": 1}))
    assert synthetic.planned_generation_count(workdir, {"invoice": 2}) == 1

    fake_anthropic = MagicMock()
    fake_anthropic.messages.create.return_value.content = [
        MagicMock(text='{"text": "INVOICE two", "facts": {"vendor": "Beta", "total_usd": 2}}')
    ]
    with patch.object(synthetic, "_make_client", return_value=fake_anthropic):
        m = synthetic.acquire(workdir=workdir, doc_counts={"invoice": 2}, ocr_subset_count=0)

    assert fake_anthropic.messages.create.call_count == 1
    assert m.doc_count == 2
    assert json.loads((facts_dir / "0002_invoice.json").read_text())["vendor"] == "Beta"
    assert sorted(ingest_dir.glob("*.json")) == []


def test_synthetic_marker_without_files_re_acquires(tmp_path: Path):
    """Regression for the stale-marker bug: marker present but ingest_dir
    has no .txt/.pdf docs → must regenerate. Without this guard, Phase 0
    returns a manifest with doc_count=0 and Phase 4 hits a 120s
    ingest-watcher timeout."""
    fake_anthropic = MagicMock()
    fake_anthropic.messages.create.return_value.content = [
        MagicMock(text='{"text": "INVOICE\\nVendor: Acme", "facts": {"vendor": "Acme", "total_usd": 100}}')
    ]

    workdir = tmp_path / "synth"
    ingest_dir = workdir / "ingest"
    ingest_dir.mkdir(parents=True)
    (ingest_dir / ".acquired").write_text("acquired")
    # No .txt/.pdf files — only the marker. Stale-cache scenario.

    with patch.object(synthetic, "_make_client", return_value=fake_anthropic):
        m = synthetic.acquire(
            workdir=workdir,
            doc_counts={"invoice": 2},
            ocr_subset_count=0,
        )

    assert m.doc_count == 2
    assert sorted(m.ingest_dir.glob("*.txt"))


# ── #732: the prompts name the entities the questions ask about ──


def test_every_prompt_that_speaks_of_a_vendor_client_or_campaign_names_a_declared_one():
    import random

    rng = random.Random(7)
    seen_vendors, seen_campaigns, seen_clients = set(), set(), set()
    for _ in range(40):
        for doc_type in ("invoice", "vendor_contract"):
            prompt = synthetic._draw_prompt(doc_type, rng)
            named = [v for v in synthetic.COMPANY["key_vendors"] if v in prompt]
            assert len(named) == 1, prompt
            seen_vendors.update(named)
        prompt = synthetic._draw_prompt("marketing_brief", rng)
        named = [c for c in synthetic.COMPANY["campaigns"] if c in prompt]
        assert len(named) == 1 and any(c in prompt for c in synthetic.COMPANY["key_clients"]), prompt
        seen_campaigns.update(named)
        for doc_type in ("board_minutes", "quarterly_report"):
            prompt = synthetic._draw_prompt(doc_type, rng)
            named = [c for c in synthetic.COMPANY["key_clients"] if c in prompt]
            assert len(named) == 1, prompt
            seen_clients.update(named)
        assert synthetic.COMPANY["key_people"]["ceo"] in synthetic._draw_prompt("board_minutes", rng)
    # Over a corpus's worth of draws every declared name is asked for at least once.
    assert seen_vendors == set(synthetic.COMPANY["key_vendors"])
    assert seen_campaigns == set(synthetic.COMPANY["campaigns"])
    assert seen_clients == set(synthetic.COMPANY["key_clients"])
    assert "Skylight" in synthetic.COMPANY["campaigns"], "the campaign questions/synthetic.yaml asks about"


def test_missing_declared_entities_names_what_no_document_mentions(tmp_path: Path):
    ingest, facts = tmp_path / "ingest", tmp_path / "facts"
    ingest.mkdir()
    facts.mkdir()
    (ingest / "0001_invoice.txt").write_text("INVOICE from Globex Supplies to Marbledock. Acme Corp is cc'd.")
    (ingest / "0002_board_minutes.txt").write_text("Skylight campaign approved.")
    # A document the OCR step turned into a PDF: its name survives in its facts only.
    (facts / "0003_vendor_contract.json").write_text('{"vendor": "Initech Software"}')
    (ingest / "0003_vendor_contract.pdf").write_bytes(b"%PDF-1.4")
    # A document whose text is still there: its facts do not count, the app never sees them (#726).
    (ingest / "0004_invoice.txt").write_text("INVOICE from the usual supplier.")
    (facts / "0004_invoice.json").write_text('{"vendor": "Cyberdyne IT"}')
    missing = synthetic.missing_declared_entities(ingest, facts)
    assert missing == ["Northwind Partners", "Polestar Industries", "Cyberdyne IT", "Northstar", "Harbour Lights"]
    for name in missing:
        (ingest / f"x_{name}.txt").write_text(name.lower())  # case does not matter
    assert synthetic.missing_declared_entities(ingest, facts) == []


def test_acquire_says_which_declared_names_the_corpus_does_not_contain(tmp_path: Path, caplog):
    """The model was asked for the names and did not use them: the manifest and the log both say so, so the
    run's report can read the questions that name them as negatives instead of lookups."""
    fake_anthropic = MagicMock()
    fake_anthropic.messages.create.return_value.content = [
        MagicMock(text='{"text": "INVOICE from Nobody Inc.", "facts": {"vendor": "Nobody Inc."}}')
    ]
    with patch.object(synthetic, "_make_client", return_value=fake_anthropic), caplog.at_level("WARNING"):
        m = synthetic.acquire(workdir=tmp_path / "synth", doc_counts={"invoice": 2}, ocr_subset_count=0)
    assert m.notes.endswith("not in the corpus: " + ", ".join(synthetic.DECLARED_ENTITIES))
    assert "contains none of: Acme Corp" in caplog.text and "#732" in caplog.text

    # The same corpus, met again through its marker: the same note.
    with patch.object(synthetic, "_make_client", return_value=fake_anthropic):
        again = synthetic.acquire(workdir=tmp_path / "synth", doc_counts={"invoice": 2}, ocr_subset_count=0)
    assert again.notes == m.notes


def test_a_corpus_that_contains_every_declared_name_gets_a_plain_note_and_no_warning(tmp_path: Path, caplog):
    fake_anthropic = MagicMock()
    everything = " ".join(synthetic.DECLARED_ENTITIES)
    fake_anthropic.messages.create.return_value.content = [
        MagicMock(text=json.dumps({"text": f"INVOICE mentioning {everything}", "facts": {"vendor": "Globex Supplies"}}))
    ]
    with patch.object(synthetic, "_make_client", return_value=fake_anthropic), caplog.at_level("WARNING"):
        m = synthetic.acquire(workdir=tmp_path / "synth", doc_counts={"invoice": 1}, ocr_subset_count=0)
    assert m.notes == "synthetic bilingual small-business"
    assert "contains none of" not in caplog.text
