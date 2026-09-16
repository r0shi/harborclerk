"""The acceptance fixtures must be what `groundtruth.yaml` says they are.

These run offline in CI. They guard the renderer (a PDF that claims a text
layer has one; the scan does not; the DOCX is a valid package; the email has
the headers and attachment the live checks filter on) and the ground truth's
consistency with the sources (an exact phrase really is in its document, the
Find All phrase is in exactly the documents listed). A live check that fails
because a fixture drifted would otherwise look like a product regression.
"""

from __future__ import annotations

import email
import email.policy
import xml.etree.ElementTree as ET
import zipfile
from email.utils import parseaddr
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from acceptance.fixtures import render
from acceptance.fixtures.render import Fixture, load_groundtruth, materialize, source_text


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Fixture]:
    dest = tmp_path_factory.mktemp("fixtures")
    return {f.name: f for f in materialize(dest)}


def _pdf_text(path: Path) -> str:
    pdf = pdfium.PdfDocument(str(path))
    try:
        return "\n".join(page.get_textpage().get_text_range() for page in pdf)
    finally:
        pdf.close()


def test_every_groundtruth_fixture_is_rendered(rendered: dict[str, Fixture]) -> None:
    gt = load_groundtruth()
    assert set(rendered) == set(gt["fixtures"])
    for f in rendered.values():
        assert f.path.is_file() and f.path.stat().st_size > 0, f.name


def test_text_pdf_has_a_text_layer_with_the_exact_phrase(rendered: dict[str, Fixture]) -> None:
    text = _pdf_text(rendered["lease-agreement.pdf"].path)
    gt = load_groundtruth()
    assert gt["queries"]["known_answer"]["exact_phrase"] in text
    assert "Eleanor Marlowe" in text
    # Parentheses and quotes must survive PDF string escaping.
    assert '(the "Lease")' in text


def test_text_pdf_keeps_french_accents(tmp_path: Path) -> None:
    out = tmp_path / "fr.pdf"
    render.write_text_pdf(source_text("bail-commercial.txt"), out)
    text = _pdf_text(out)
    assert "DURÉE" in text and "échéance" in text


def test_text_pdf_paginates_long_text(tmp_path: Path) -> None:
    out = tmp_path / "long.pdf"
    render.write_text_pdf("\n".join(f"line {i}" for i in range(200)), out)
    pdf = pdfium.PdfDocument(str(out))
    try:
        assert len(pdf) >= 4
    finally:
        pdf.close()


def test_image_pdf_has_no_text_layer_but_visible_ink(rendered: dict[str, Fixture]) -> None:
    path = rendered["lease-agreement-scan.pdf"].path
    assert _pdf_text(path).strip() == "", "the scan fixture must force OCR; it has a text layer"
    pdf = pdfium.PdfDocument(str(path))
    try:
        image = pdf[0].render(scale=0.5).to_pil().convert("L")
    finally:
        pdf.close()
    darkest, lightest = image.getextrema()
    assert darkest < 128 < lightest, "page rendered blank or solid"


def test_docx_is_a_valid_package_with_the_text(rendered: dict[str, Fixture]) -> None:
    with zipfile.ZipFile(rendered["handbook.docx"].path) as z:
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= set(z.namelist())
        document = z.read("word/document.xml")
    root = ET.fromstring(document)  # ampersands in "Quill & Tern" must be escaped
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    text = "\n".join(t.text or "" for t in root.iterfind(".//w:t", ns))
    assert "STAFF HANDBOOK" in text and "Quill & Tern Bookbinders" in text


def test_eml_headers_body_and_attachment_match_groundtruth(rendered: dict[str, Fixture]) -> None:
    gt = load_groundtruth()["email"]
    msg = email.message_from_bytes(rendered["invoice-thread.eml"].path.read_bytes(), policy=email.policy.default)
    assert parseaddr(msg["From"]) == (gt["from_name"], gt["from_address"])
    assert [a for _, a in email.utils.getaddresses([msg["To"]])] == gt["to_addresses"]
    assert [a for _, a in email.utils.getaddresses([msg["Cc"]])] == gt["cc_addresses"]
    assert msg["Subject"] == gt["subject"]
    assert msg["Message-ID"] == gt["message_id"]
    assert email.utils.parsedate_to_datetime(msg["Date"]) == email.utils.parsedate_to_datetime(gt["date"])
    body = msg.get_body(preferencelist=("plain",))
    assert body is not None and "standing order" in body.get_content()
    attachments = [p.get_filename() for p in msg.iter_attachments()]
    assert attachments == [gt["attachment"]]


def test_minutes_pair_differ_by_exactly_one_line() -> None:
    a = source_text("meeting-minutes.txt").splitlines()
    b = source_text("meeting-minutes-v2.txt").splitlines()
    assert len(a) == len(b)
    assert sum(x != y for x, y in zip(a, b, strict=True)) == 1


def test_groundtruth_is_consistent_with_the_sources() -> None:
    gt = load_groundtruth()
    fixtures = gt["fixtures"]
    by_name = {name: source_text(spec["source"]) for name, spec in fixtures.items() if not spec.get("unsupported")}
    by_name["invoice-thread.eml"] += source_text(fixtures["invoice-thread.eml"]["attachment_source"])

    q = gt["queries"]
    for key in ("known_answer", "french"):
        assert q[key]["expected_top"] in fixtures, key
    assert q["known_answer"]["exact_phrase"] in by_name[q["known_answer"]["expected_top"]]
    assert q["french"]["query"] in by_name[q["french"]["expected_top"]]
    assert set(q["conflict"]["expected_docs"]) <= set(fixtures)

    phrase = q["find_all"]["phrase"]
    containing = {name for name, text in by_name.items() if phrase.lower() in text.lower()}
    # The scan is the lease rendered as an image, so it never adds a new document to any phrase set.
    assert containing == set(q["find_all"]["expected_docs"]), (
        f"Find All phrase {phrase!r} is in {sorted(containing)}, ground truth lists {q['find_all']['expected_docs']}"
    )

    everything = "\n".join(by_name.values())
    for person in gt["people"]:
        assert person in everything, person
    for org in gt["organisations"]:
        assert org in everything, org


def test_languages_and_flags_are_declared(rendered: dict[str, Fixture]) -> None:
    for f in rendered.values():
        if f.unsupported:
            assert f.language is None
            continue
        assert f.language in ("en", "fr"), f.name
        assert f.spec["source_kind"] in ("document", "email"), f.name
    assert "é" in rendered["bail-commercial.txt"].path.read_text(encoding="utf-8")
