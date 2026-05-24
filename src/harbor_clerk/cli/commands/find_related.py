"""harbor-clerk find-related — find documents similar to a given doc_id."""

from __future__ import annotations

import argparse
import sys

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.commands import _common_parser
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.help import load
from harbor_clerk.cli.output import render, resolve_mode


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    description = load("find-related") or "Find documents similar to a given doc_id."
    p = subparsers.add_parser(
        "find-related",
        help="Find documents similar to a given doc_id",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_parser()],
    )
    p.add_argument("doc_id", help="Document UUID to find related documents for")
    p.add_argument("-k", "--k", type=int, default=5, help="Number of related documents to return (default: 5)")
    p.set_defaults(_handler=run)


def run(args) -> int:
    arguments: dict = {
        "doc_id": args.doc_id,
        "k": args.k,
    }

    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_find_related", arguments)
    render(payload, mode=mode, command="find-related")
    return 0
