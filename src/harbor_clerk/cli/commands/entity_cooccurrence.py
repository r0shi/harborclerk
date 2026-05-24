"""harbor-clerk entity-cooccurrence — entities appearing alongside a given entity."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.commands import _common_parser
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.output import render, resolve_mode

_HELP_PATH = Path(__file__).parent.parent / "help" / "entity-cooccurrence.txt"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    description = _HELP_PATH.read_text() if _HELP_PATH.exists() else "Entities appearing alongside a given entity."
    p = subparsers.add_parser(
        "entity-cooccurrence",
        help="Entities appearing alongside a given entity",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_parser()],
    )
    p.add_argument("entity", help="Entity name to find co-occurring entities for")
    p.add_argument("-k", "--k", type=int, default=20, help="Number of results to return (default: 20)")
    p.set_defaults(_handler=run)


def run(args) -> int:
    arguments: dict = {"entity_text": args.entity, "limit": args.k}

    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_entity_cooccurrence", arguments)
    render(payload, mode=mode, command="entity-cooccurrence")
    return 0
