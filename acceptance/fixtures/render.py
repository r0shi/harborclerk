"""Render the acceptance fixtures from their plain-text sources.

Only text is committed. At run time each source is rendered into the document
type the pipeline claims to handle: a PDF with a text layer, an image-only PDF
that forces OCR, a DOCX for the Tika path, an email with an attachment, and
plain copies. Everything here is hand-rolled on the standard library and
Pillow so the suite adds no dependency; `groundtruth.yaml` beside the sources
says what each rendered file must be.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import yaml
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SOURCES = Path(__file__).resolve().parent / "sources"


@dataclass(frozen=True)
class Fixture:
    name: str
    path: Path
    spec: dict[str, Any]

    @property
    def language(self) -> str | None:
        return self.spec.get("language")

    @property
    def unsupported(self) -> bool:
        return bool(self.spec.get("unsupported"))


def load_groundtruth() -> dict[str, Any]:
    return yaml.safe_load((SOURCES / "groundtruth.yaml").read_text(encoding="utf-8"))


def source_text(name: str) -> str:
    return (SOURCES / name).read_text(encoding="utf-8")


def materialize(dest: Path) -> list[Fixture]:
    """Render every fixture in `groundtruth.yaml` into `dest` and describe them."""
    dest.mkdir(parents=True, exist_ok=True)
    gt = load_groundtruth()
    out: list[Fixture] = []
    for name, spec in gt["fixtures"].items():
        path = dest / name
        kind = spec["render"]
        if kind == "copy":
            path.write_bytes((SOURCES / spec["source"]).read_bytes())
        elif kind == "text-pdf":
            write_text_pdf(source_text(spec["source"]), path)
        elif kind == "image-pdf":
            write_image_pdf(source_text(spec["source"]), path)
        elif kind == "docx":
            write_docx(source_text(spec["source"]), path)
        elif kind == "eml":
            write_eml(
                path,
                gt["email"],
                body=source_text(spec["source"]),
                attachment=(gt["email"]["attachment"], (SOURCES / spec["attachment_source"]).read_bytes()),
            )
        else:
            raise ValueError(f"{name}: unknown render kind {kind!r}")
        out.append(Fixture(name=name, path=path, spec=spec))
    return out


# ── PDF with a text layer ───────────────────────────────────────────────────

_PAGE_W, _PAGE_H = 595, 842  # A4 in points
_MARGIN, _FONT_SIZE, _LEADING = 72, 11, 14
_LINES_PER_PAGE = (_PAGE_H - 2 * _MARGIN) // _LEADING


def _pdf_string(line: str) -> bytes:
    """A PDF literal string in WinAnsi, which covers the French fixtures."""
    escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return b"(" + escaped.encode("cp1252", errors="replace") + b")"


def write_text_pdf(text: str, path: Path) -> None:
    lines = text.splitlines() or [""]
    pages = [lines[i : i + _LINES_PER_PAGE] for i in range(0, len(lines), _LINES_PER_PAGE)]

    objects: list[bytes] = []  # index + 1 is the object number

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # placeholder, filled once the pages object exists
    pages_obj = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    page_ids: list[int] = []
    for page_lines in pages:
        stream = b"BT /F1 %d Tf %d %d Td %d TL\n" % (_FONT_SIZE, _MARGIN, _PAGE_H - _MARGIN, _LEADING)
        stream += b"".join(_pdf_string(line) + b" Tj T*\n" for line in page_lines)
        stream += b"ET"
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R "
            b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (pages_obj, _PAGE_W, _PAGE_H, content, font)
        )
        page_ids.append(page)

    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj
    kids = b" ".join(b"%d 0 R" % p for p in page_ids)
    objects[pages_obj - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref)
    path.write_bytes(bytes(out))


# ── Image-only PDF (forces OCR) ─────────────────────────────────────────────

_DPI = 150
_IMG_W, _IMG_H = int(8.27 * _DPI), int(11.69 * _DPI)  # A4 at 150 DPI
_IMG_MARGIN, _IMG_FONT, _IMG_LEADING = 110, 26, 36
_IMG_LINES_PER_PAGE = (_IMG_H - 2 * _IMG_MARGIN) // _IMG_LEADING


def write_image_pdf(text: str, path: Path) -> None:
    """Render the text to page images and save them as a PDF with no text
    layer, so the pipeline must OCR it. A light blur keeps it scan-like without
    defeating Tesseract."""
    font = ImageFont.load_default(size=_IMG_FONT)
    lines = text.splitlines() or [""]
    images: list[Image.Image] = []
    for start in range(0, len(lines), _IMG_LINES_PER_PAGE):
        img = Image.new("L", (_IMG_W, _IMG_H), color=255)
        draw = ImageDraw.Draw(img)
        y = _IMG_MARGIN
        for line in lines[start : start + _IMG_LINES_PER_PAGE]:
            draw.text((_IMG_MARGIN, y), line, fill=0, font=font)
            y += _IMG_LEADING
        images.append(img.filter(ImageFilter.GaussianBlur(0.4)))
    first, rest = images[0], images[1:]
    first.save(path, "PDF", resolution=float(_DPI), save_all=bool(rest), append_images=rest)


# ── DOCX (minimal OOXML package) ────────────────────────────────────────────

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)


def write_docx(text: str, path: Path) -> None:
    paragraphs = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>' for line in text.splitlines()
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paragraphs}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", document)


# ── Email with an attachment ────────────────────────────────────────────────


def write_eml(path: Path, headers: dict[str, Any], *, body: str, attachment: tuple[str, bytes]) -> None:
    msg = EmailMessage()
    msg["From"] = f"{headers['from_name']} <{headers['from_address']}>"
    msg["To"] = ", ".join(headers["to_addresses"])
    if headers.get("cc_addresses"):
        msg["Cc"] = ", ".join(headers["cc_addresses"])
    msg["Subject"] = headers["subject"]
    msg["Date"] = format_datetime(parsedate_to_datetime(headers["date"]))
    msg["Message-ID"] = headers["message_id"]
    msg.set_content(body)
    filename, data = attachment
    msg.add_attachment(data, maintype="text", subtype="plain", filename=filename)
    path.write_bytes(msg.as_bytes())
