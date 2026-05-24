"""harbor-clerk read-passages — fetch passages by chunk_id."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.commands import _common_parser
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.output import render, resolve_mode

_HELP_PATH = Path(__file__).parent.parent / "help" / "read-passages.txt"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    description = _HELP_PATH.read_text() if _HELP_PATH.exists() else "Fetch passages by chunk_id."
    p = subparsers.add_parser(
        "read-passages",
        help="Fetch passages by chunk_id",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_parser()],
    )
    p.add_argument("chunk_ids", nargs="+", help="One or more chunk UUIDs")
    p.add_argument("--include-meta", action="store_true", help="Include chunk metadata in response")
    p.set_defaults(_handler=run)


def run(args) -> int:
    arguments: dict = {"chunk_ids": list(args.chunk_ids)}
    if args.include_meta:
        arguments["include_meta"] = True

    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_read_passages", arguments)
    render(payload, mode=mode, command="read-passages")
    return 0
