"""Tests for heading_parser — pure unit tests, no DB required."""

from harbor_clerk.worker.heading_parser import Heading, parse_headings_from_xhtml


def test_basic_headings():
    xhtml = "<html><body><h1>Introduction</h1><p>text</p><h2>Background</h2></body></html>"
    result = parse_headings_from_xhtml(xhtml)
    assert len(result) == 2
    assert result[0] == Heading(level=1, title="Introduction", position=result[0].position)
    assert result[1].level == 2
    assert result[1].title == "Background"


def test_namespaced_headings():
    """Tika often emits <html:h1> with a namespace prefix."""
    xhtml = "<html:body><html:h1>Title</html:h1><html:h2>Sub</html:h2></html:body>"
    result = parse_headings_from_xhtml(xhtml)
    assert len(result) == 2
    assert result[0].level == 1
    assert result[0].title == "Title"
    assert result[1].level == 2
    assert result[1].title == "Sub"


def test_empty_headings_skipped():
    xhtml = "<h1></h1><h2>Real</h2><h3>   </h3>"
    result = parse_headings_from_xhtml(xhtml)
    assert len(result) == 1
    assert result[0].title == "Real"


def test_no_headings():
    xhtml = "<html><body><p>Just a paragraph.</p></body></html>"
    result = parse_headings_from_xhtml(xhtml)
    assert result == []


def test_nested_inline_elements():
    """Headings containing <b>, <i>, <span> etc. should collect all text."""
    xhtml = "<h1><b>Bold</b> and <i>italic</i> title</h1>"
    result = parse_headings_from_xhtml(xhtml)
    assert len(result) == 1
    assert result[0].title == "Bold and italic title"


def test_heading_positions_increase():
    xhtml = "<p>Some intro text here.</p><h1>First</h1><p>More text.</p><h2>Second</h2>"
    result = parse_headings_from_xhtml(xhtml)
    assert len(result) == 2
    assert result[0].position < result[1].position


def test_all_heading_levels():
    xhtml = "".join(f"<h{i}>Level {i}</h{i}>" for i in range(1, 7))
    result = parse_headings_from_xhtml(xhtml)
    assert len(result) == 6
    for i, h in enumerate(result):
        assert h.level == i + 1
        assert h.title == f"Level {i + 1}"


def test_whitespace_normalization():
    xhtml = "<h1>  Multiple   spaces\n\tand\ttabs  </h1>"
    result = parse_headings_from_xhtml(xhtml)
    assert len(result) == 1
    assert result[0].title == "Multiple spaces and tabs"


def test_empty_input():
    assert parse_headings_from_xhtml("") == []
