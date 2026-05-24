"""harbor-clerk list-recent — recently ingested documents."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.commands import _common_parser
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.output import render, resolve_mode

_HELP_PATH = Path(__file__).parent.parent / "help" / "list-recent.txt"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    description = _HELP_PATH.read_text() if _HELP_PATH.exists() else "Recently ingested documents."
    p = subparsers.add_parser(
        "list-recent",
        help="Recently ingested documents",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_parser()],
    )
    p.add_argument("--limit", type=int, default=20, help="Maximum number of documents to return (default: 20)")
    p.set_defaults(_handler=run)


def run(args) -> int:
    arguments: dict = {
        "limit": args.limit,
    }

    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_list_recent", arguments)
    render(payload, mode=mode, command="list-recent")
    return 0
