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


def test_summary_handles_a_first_sentence_that_wraps_lines() -> None:
    """Docstrings wrap at 120 chars, so the opening sentence spans source lines.

    Reading only the first physical line truncates mid-sentence and emits a
    fragment with no terminal punctuation. This shipped once: kb_find_all
    rendered as "...deduped by document, with" in README.
    """
    from scripts.gen_docs.generators.mcp_tools import _summary

    wrapped = "Enumerate documents matching a query — deduped by document, with\noptional filtering. Use this when asked to LIST."
    assert _summary(wrapped) == "Enumerate documents matching a query — deduped by document, with optional filtering."


def test_summary_does_not_cut_at_an_abbreviation() -> None:
    from scripts.gen_docs.generators.mcp_tools import _summary

    assert _summary("Pass a filter, e.g. a doc_id, to narrow results. Then read.") == (
        "Pass a filter, e.g. a doc_id, to narrow results."
    )


def test_summary_escapes_table_delimiters() -> None:
    from scripts.gen_docs.generators.mcp_tools import _summary

    assert "\\|" in _summary("Accepts a|b as input. More text.")


def test_every_generated_tool_summary_is_a_complete_sentence() -> None:
    """Systemic guard: byte-stability alone cannot detect malformed content.

    `--check` only compares committed bytes against a fresh render, so a
    deterministic formatting bug reproduces identically forever and passes.
    Content quality needs its own assertion.
    """
    from scripts.gen_docs.generators.mcp_tools import _load_tools, _summary

    truncated = [t.name for t in _load_tools() if not _summary(t.description).endswith((".", "!", "?"))]
    assert not truncated, f"tool summaries truncated mid-sentence: {truncated}"


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


# --- Content-quality assertions -------------------------------------------
#
# Byte-stability (--check) proves a doc matches its generator; it says nothing
# about whether the generator is correct. A deterministic formatting bug
# reproduces identically forever and passes --check. PR #541 shipped exactly
# that. Each generator therefore needs assertions about its *content*.


def test_every_table_has_an_explicit_group() -> None:
    """The one hand-maintained element in db_tables cannot rot silently.

    Module grouping is ~1:1 with tables and FK clustering collapses to one
    component, so the group map is explicit. This test is what stops it from
    drifting: add a table without a group and the build fails.
    """
    from scripts.gen_docs.generators.db_tables import TABLE_GROUPS, load_metadata

    tables = set(load_metadata().tables)
    ungrouped = sorted(tables - set(TABLE_GROUPS))
    stale = sorted(set(TABLE_GROUPS) - tables)
    assert not ungrouped, f"tables with no group assignment in TABLE_GROUPS: {ungrouped}"
    assert not stale, f"TABLE_GROUPS references tables that no longer exist: {stale}"


def test_db_block_lists_every_table() -> None:
    from scripts.gen_docs.generators.db_tables import generate, load_metadata

    output = generate()
    missing = [t for t in load_metadata().tables if f"`{t}`" not in output]
    assert not missing, f"tables absent from the generated block: {missing}"


def test_pipeline_block_matches_worker_config() -> None:
    """Would have caught #537: the docs claimed two queues; the code has three."""
    from harbor_clerk.worker.entry import QUEUE_STAGES
    from harbor_clerk.worker.pipeline import STAGE_CONFIG
    from scripts.gen_docs.generators.pipeline import generate

    output = generate()
    for stage in STAGE_CONFIG:
        assert f"`{stage.value}`" in output, f"stage {stage.value} missing from generated block"
    for queue in QUEUE_STAGES:
        assert f"`{queue}`" in output, f"queue {queue} missing from generated block"
    assert "does **not** gate" in output, "summarize's background role must be stated explicitly"


def test_compose_block_lists_every_service() -> None:
    from scripts.gen_docs.generators.compose import generate, load_services

    output = generate()
    missing = [s for s in load_services() if f"`{s}`" not in output]
    assert not missing, f"services absent from the generated block: {missing}"


def test_rest_blocks_cover_every_operation() -> None:
    from scripts.gen_docs.generators.rest import _operations, generate_full

    ops = _operations()
    assert ops, "no REST operations discovered"
    assert all(path.startswith("/") for _, _, path, _, _ in ops)
    assert all(summary for _, _, _, summary, _ in ops), "every operation should have an OpenAPI summary"

    full = generate_full()
    missing = [f"{m} {p}" for _, m, p, _, _ in ops if f"`{p}`" not in full]
    assert not missing, f"operations absent from the full table: {missing[:5]}"


def test_cli_mcp_parity() -> None:
    """The CLI is a thin shell over MCP, so the surfaces must match exactly.

    Folded in from what was planned as its own PR: parity is currently perfect
    at 19/19, so this locks it rather than documenting exceptions.
    """
    from scripts.gen_docs.generators.cli_commands import load_subcommands
    from scripts.gen_docs.generators.mcp_tools import _load_tools

    mcp = {t.name.removeprefix("kb_").replace("_", "-") for t in _load_tools()}
    cli = set(load_subcommands())
    assert not mcp - cli, f"MCP tools with no CLI subcommand: {sorted(mcp - cli)}"
    assert not cli - mcp, f"CLI subcommands with no MCP tool: {sorted(cli - mcp)}"


def test_compose_roles_are_never_bare_flags() -> None:
    """A Role cell must name a command, not a flag.

    llama-server's compose command is a list of bare flags with no executable
    (the entrypoint is in the image), and minio's is a plain string rather than
    a list. Taking token zero blindly rendered "--host" as a role and dropped
    minio's entirely — a deterministic bug that --check would never notice.
    """
    from scripts.gen_docs.generators.compose import _role, load_services

    for name, service in load_services().items():
        role = _role(service or {})
        assert not role.startswith("`-"), f"{name} role is a bare flag: {role}"
        command = (service or {}).get("command")
        if isinstance(command, str):
            assert role != "—", f"{name} has a string command but rendered no role"


def test_rest_access_gates_are_derived_for_every_operation() -> None:
    """Authorization must be derived, never guessed.

    The hand-written table annotated "(admin)" by hand; those annotations were
    lost when it was generated. _operations() raises if a route's gate cannot be
    resolved unambiguously, so this asserts the join covers the whole surface
    and that known-destructive endpoints are still marked.
    """
    from scripts.gen_docs.generators.rest import _operations

    ops = _operations()
    by_path = {(path, method): access for _, method, path, _, access in ops}
    for path in (
        "/api/system/delete-all-documents",
        "/api/system/resummarize-all",
        "/api/system/reprocess-all",
    ):
        assert by_path.get((path, "POST")) == "admin", f"{path} must be marked admin-gated"
    assert any(a == "human only" for a in by_path.values()), "human-only gate should appear"


def test_every_entry_point_is_described() -> None:
    """The one hand-written element of entry_points cannot go stale.

    Console scripts carry no description of their own, so DESCRIPTIONS is
    hand-maintained — but it must cover [project.scripts] exactly in both
    directions, so a new script cannot ship undocumented and a removed one
    cannot linger.
    """
    from scripts.gen_docs.generators.entry_points import DESCRIPTIONS, load_scripts

    scripts = set(load_scripts())
    undescribed = sorted(scripts - set(DESCRIPTIONS))
    stale = sorted(set(DESCRIPTIONS) - scripts)
    assert not undescribed, f"console scripts with no description: {undescribed}"
    assert not stale, f"DESCRIPTIONS references scripts that no longer exist: {stale}"
