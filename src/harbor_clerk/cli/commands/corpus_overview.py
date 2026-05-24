"""harbor-clerk corpus-overview — aggregate corpus statistics."""

from __future__ import annotations

import argparse
import sys

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.commands import _common_parser
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.help import load
from harbor_clerk.cli.output import render, resolve_mode


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    description = (
        load("corpus-overview") or "Aggregate corpus stats (doc/chunk/page counts, language + MIME mix, date range)."
    )
    p = subparsers.add_parser(
        "corpus-overview",
        help="Aggregate corpus stats (doc/chunk/page counts, language + MIME mix, date range)",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_parser()],
    )
    p.add_argument("--limit", type=int, default=50, help="Maximum number of documents to return (default: 50)")
    p.set_defaults(_handler=run)


def run(args) -> int:
    arguments: dict = {
        "limit": args.limit,
    }

    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_corpus_overview", arguments)
    render(payload, mode=mode, command="corpus-overview")
    return 0
