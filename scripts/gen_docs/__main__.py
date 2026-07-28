"""Regenerate (or verify) generated documentation blocks.

    uv run python -m scripts.gen_docs            # rewrite blocks in place
    uv run python -m scripts.gen_docs --check    # exit 1 if anything is stale

`--check` is what CI runs. It is the forcing function: change a tool, forget to
regenerate, red build. Documentation cannot drift from source.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

from harbor_clerk.error_text import describe_error
from scripts.gen_docs.blocks import BlockError, find_blocks, replace_block
from scripts.gen_docs.registry import GENERATORS, REPO_ROOT, TARGETS


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _render(path: Path) -> tuple[str, str, list[str]]:
    """Return (original, regenerated, block names) for one target file."""
    original = path.read_text()
    updated = original
    names = [b.name for b in find_blocks(original)]

    for name in names:
        generator = GENERATORS.get(name)
        if generator is None:
            raise BlockError(
                f"{_rel(path)} contains block {name!r} but no generator is registered for it. "
                f"Known generators: {sorted(GENERATORS)}"
            )
        try:
            content = generator()
        except Exception as exc:  # noqa: BLE001 - surface which generator failed
            raise RuntimeError(f"generator {name!r} failed: {describe_error(exc)}") from exc
        updated = replace_block(updated, name, content)

    return original, updated, names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gen_docs", description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without writing; exit 1 if stale")
    args = parser.parse_args(argv)

    seen: set[str] = set()
    stale: list[str] = []
    written: list[str] = []

    for target in TARGETS:
        if not target.exists():
            print(f"error: target {_rel(target)} does not exist", file=sys.stderr)
            return 2
        try:
            original, updated, names = _render(target)
        except (BlockError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        seen.update(names)

        if original == updated:
            continue
        if args.check:
            stale.append(_rel(target))
            diff = difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"{_rel(target)} (committed)",
                tofile=f"{_rel(target)} (regenerated)",
            )
            sys.stderr.writelines(diff)
        else:
            target.write_text(updated)
            written.append(_rel(target))

    unused = sorted(set(GENERATORS) - seen)
    if unused:
        print(
            f"error: generator(s) {unused} are registered but no target file contains their block. "
            "Add the marker block or remove the generator.",
            file=sys.stderr,
        )
        return 2

    if args.check:
        if stale:
            print(f"\nerror: {len(stale)} file(s) out of date: {', '.join(stale)}", file=sys.stderr)
            print("Run: uv run python -m scripts.gen_docs", file=sys.stderr)
            return 1
        print(f"docs up to date ({len(seen)} block(s) checked)")
        return 0

    if written:
        print(f"regenerated: {', '.join(written)}")
    else:
        print(f"docs already up to date ({len(seen)} block(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
