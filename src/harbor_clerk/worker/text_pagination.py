"""Synthetic-page splitting for long extracted text.

Shared between the Tika/plain-text path in :mod:`harbor_clerk.worker.stages.extract`
and the Markdown path in :mod:`harbor_clerk.worker.markdown_extract`. Lives in its
own module so neither side has to import the other for this one pure utility —
the previous arrangement (definition in ``stages/extract`` + lazy import from
``markdown_extract``) created an import cycle that worked at runtime but tripped
static-analysis tools.
"""


def paginate_text(text: str, target: int) -> list[tuple[int, str]]:
    """Split a long text into synthetic pages at paragraph boundaries.

    Returns ``[(page_num, text)]`` with 1-based page numbers. Tries to break at
    a paragraph boundary (``\\n\\n``) in the upper half of the target window;
    falls back to a single newline; finally hard-cuts at ``target`` chars.
    """
    if not text or target <= 0:
        return [(1, text)]

    if len(text) <= target:
        return [(1, text)]

    pages: list[tuple[int, str]] = []
    start = 0
    page_num = 1
    text_len = len(text)

    while start < text_len:
        end = min(start + target, text_len)

        if end < text_len:
            # Try to break at a paragraph boundary (double newline)
            para = text.rfind("\n\n", start, end)
            if para > start + target // 2:
                end = para + 2  # include the double newline
            else:
                # Fall back to single newline
                nl = text.rfind("\n", start, end)
                if nl > start + target // 2:
                    end = nl + 1

        pages.append((page_num, text[start:end]))
        page_num += 1
        start = end

    return pages
