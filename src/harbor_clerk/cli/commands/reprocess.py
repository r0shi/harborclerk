"""harbor-clerk reprocess — re-run the pipeline for a document."""

from __future__ import annotations

import argparse
import sys

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.commands import _common_parser
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.help import load
from harbor_clerk.cli.output import render, resolve_mode


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    description = load("reprocess") or "Re-run the pipeline for a document."
    p = subparsers.add_parser(
        "reprocess",
        help="Re-run the pipeline for a document",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_parser()],
    )
    p.add_argument("doc_id", help="Document UUID to reprocess")
    p.set_defaults(_handler=run)


def run(args) -> int:
    arguments: dict = {"doc_id": args.doc_id}

    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_reprocess", arguments)
    render(payload, mode=mode, command="reprocess")
    return 0
