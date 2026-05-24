"""harbor-clerk read-document — read a full document with pagination."""

from __future__ import annotations

import argparse
import sys

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.commands import _common_parser
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.help import load
from harbor_clerk.cli.output import render, resolve_mode


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    description = load("read-document") or "Read a full document with pagination."
    p = subparsers.add_parser(
        "read-document",
        help="Read a full document (paginated)",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_parser()],
    )
    p.add_argument("doc_id", help="Document UUID")
    p.add_argument(
        "--page-start", type=int, default=None, help="First page to return (1-based; default: server default)"
    )
    p.add_argument(
        "--page-end", type=int, default=None, help="Last page to return (inclusive; default: server default)"
    )
    p.add_argument(
        "--max-chars", type=int, default=None, help="Max characters to return (default: server default of 50000)"
    )
    p.set_defaults(_handler=run)


def run(args) -> int:
    arguments: dict = {"doc_id": args.doc_id}
    if args.page_start is not None:
        arguments["page_start"] = args.page_start
    if args.page_end is not None:
        arguments["page_end"] = args.page_end
    if args.max_chars is not None:
        arguments["max_chars"] = args.max_chars

    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_read_document", arguments)
    render(payload, mode=mode, command="read-document")
    return 0
