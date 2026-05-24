"""Phase 2 dependency smoke test — confirms markdown-it-py and pyyaml import and work."""


def test_markdown_it_importable_and_parses():
    from markdown_it import MarkdownIt

    md = MarkdownIt()
    tokens = md.parse("# Hello\n")
    # The parser must produce a heading_open token for ATX `#`.
    assert any(t.type == "heading_open" for t in tokens)


def test_pyyaml_importable_and_parses():
    import yaml

    data = yaml.safe_load("title: Hello\ntags: [a, b]\n")
    assert data == {"title": "Hello", "tags": ["a", "b"]}
