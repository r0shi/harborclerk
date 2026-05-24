"""harbor-clerk expand-context — fetch N chunks before/after a given chunk."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.commands import _common_parser
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.output import render, resolve_mode

_HELP_PATH = Path(__file__).parent.parent / "help" / "expand-context.txt"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    description = _HELP_PATH.read_text() if _HELP_PATH.exists() else "Fetch N chunks before/after a given chunk."
    p = subparsers.add_parser(
        "expand-context",
        help="Fetch N chunks before/after a given chunk",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_parser()],
    )
    p.add_argument("chunk_id", help="Chunk UUID to expand around")
    p.add_argument("-n", "--n", type=int, default=2, help="Chunks to include before/after (default: 2)")
    p.set_defaults(_handler=run)


def run(args) -> int:
    arguments: dict = {"chunk_id": args.chunk_id, "n": args.n}

    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_expand_context", arguments)
    render(payload, mode=mode, command="expand-context")
    return 0
