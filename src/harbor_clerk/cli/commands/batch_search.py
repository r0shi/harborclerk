"""harbor-clerk batch-search — run multiple searches in one call."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.commands import _common_parser
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.output import render, resolve_mode

_HELP_PATH = Path(__file__).parent.parent / "help" / "batch-search.txt"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    description = _HELP_PATH.read_text() if _HELP_PATH.exists() else "Run multiple searches in one call."
    p = subparsers.add_parser(
        "batch-search",
        help="Run multiple searches in one call",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_parser()],
    )
    p.add_argument("queries", nargs="+", help="One or more search query strings")
    p.add_argument("-k", "--k", type=int, default=5, help="Number of results per query (default: 5)")
    p.add_argument("-d", "--detail", choices=["full", "brief"], default="brief")
    p.set_defaults(_handler=run)


def run(args) -> int:
    arguments: dict = {
        "queries": list(args.queries),
        "k": args.k,
        "detail": args.detail,
    }

    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_batch_search", arguments)
    render(payload, mode=mode, command="batch-search")
    return 0
