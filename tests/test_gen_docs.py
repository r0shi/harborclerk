"""Tests for the generated-documentation machinery.

The point of this system is that documentation cannot silently drift from
source. That guarantee rests on two behaviours: regeneration is byte-stable
(so `--check` doesn't produce false alarms), and `--check` actually fails when
a generated block is edited or goes out of date.
"""

from __future__ import annotations

import pytest

from scripts.gen_docs.blocks import BlockError, find_blocks, render_body, replace_block
from scripts.gen_docs.registry import GENERATORS, TARGETS

SIMPLE = """# Title

Prose before.

<!-- BEGIN GENERATED: alpha -->
old content
<!-- END GENERATED: alpha -->

Prose after.
"""


def test_finds_block_and_preserves_surrounding_prose() -> None:
    blocks = find_blocks(SIMPLE)
    assert [b.name for b in blocks] == ["alpha"]
    assert blocks[0].body(SIMPLE).strip() == "old content"

    updated = replace_block(SIMPLE, "alpha", "new content")
    assert "new content" in updated
    assert "old content" not in updated
    assert updated.startswith("# Title")
    assert updated.rstrip().endswith("Prose after.")


def test_replacement_is_idempotent() -> None:
    once = replace_block(SIMPLE, "alpha", "stable")
    twice = replace_block(once, "alpha", "stable")
    assert once == twice, "regeneration must be byte-stable or --check produces false alarms"


def test_render_body_normalises_whitespace() -> None:
    assert render_body("x") == render_body("\n\n  x  \n\n")


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("<!-- BEGIN GENERATED: a -->\n", "unterminated"),
        ("<!-- END GENERATED: a -->\n", "unopened"),
        ("<!-- BEGIN GENERATED: a -->\n<!-- END GENERATED: b -->\n", "name mismatch"),
        (
            "<!-- BEGIN GENERATED: a -->\n<!-- END GENERATED: a -->\n"
            "<!-- BEGIN GENERATED: a -->\n<!-- END GENERATED: a -->\n",
            "duplicate name",
        ),
    ],
)
def test_malformed_markers_are_rejected(text: str, reason: str) -> None:
    with pytest.raises(BlockError):
        find_blocks(text)


def test_replacing_unknown_block_is_an_error() -> None:
    with pytest.raises(BlockError):
        replace_block(SIMPLE, "nonexistent", "x")


def test_every_registered_generator_is_deterministic() -> None:
    """Two calls must agree, or CI would flap between pass and fail."""
    for name, generator in GENERATORS.items():
        assert generator() == generator(), f"generator {name!r} is not deterministic"


def test_every_block_in_targets_has_a_generator() -> None:
    for target in TARGETS:
        for block in find_blocks(target.read_text()):
            assert block.name in GENERATORS, f"{target.name} contains block {block.name!r} with no registered generator"


def test_every_generator_has_a_home() -> None:
    placed = {b.name for t in TARGETS for b in find_blocks(t.read_text())}
    assert not (set(GENERATORS) - placed), "registered generator with no marker block in any target"


def test_mcp_tools_block_lists_every_registered_tool() -> None:
    from scripts.gen_docs.generators.mcp_tools import _load_tools, generate

    output = generate()
    for tool in _load_tools():
        assert f"`{tool.name}`" in output, f"{tool.name} missing from the generated table"
    assert f"**{len(_load_tools())} tools.**" in output


def test_committed_docs_are_up_to_date() -> None:
    """The CI gate, as a test: committed generated blocks match a fresh render."""
    from scripts.gen_docs.__main__ import _render

    for target in TARGETS:
        original, regenerated, _ = _render(target)
        assert original == regenerated, (
            f"{target.name} has stale generated content. Run: uv run python -m scripts.gen_docs"
        )
