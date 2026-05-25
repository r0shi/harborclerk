"""harbor-clerk verify-identifier — verify an identifier resolves to one doc."""

from __future__ import annotations

import argparse
import sys

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.commands import _common_parser
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.help import load
from harbor_clerk.cli.output import render, resolve_mode


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    description = load("verify-identifier") or "Verify a document identifier."
    p = subparsers.add_parser(
        "verify-identifier",
        help="Verify an identifier resolves to exactly one document",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_parser()],
    )
    p.add_argument(
        "identifier",
        help="The identifier string to check against title, filename, and identifier-like metadata fields",
    )
    p.set_defaults(_handler=run)


def run(args) -> int:
    arguments = {"identifier": args.identifier}
    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_verify_identifier", arguments)
    render(payload, mode=mode, command="verify-identifier")
    return 0
