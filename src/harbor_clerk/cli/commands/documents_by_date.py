"""harbor-clerk documents-by-date — date-sorted document lookup."""

from __future__ import annotations

import argparse
import json as _json
import sys

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.commands import _common_parser
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.help import load
from harbor_clerk.cli.output import render, resolve_mode


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    description = load("documents-by-date") or "Documents sorted by their effective date."
    p = subparsers.add_parser(
        "documents-by-date",
        help="Documents sorted by their effective date (earliest / latest)",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_parser()],
    )
    p.add_argument(
        "--direction",
        choices=["earliest", "latest"],
        default="earliest",
        help="Sort direction (default: earliest)",
    )
    p.add_argument(
        "--query",
        help="Optional FTS filter (no vector ranking). Narrows the set, then sorts by date.",
    )
    p.add_argument(
        "--metadata-filter",
        help='JSON string of {"namespace.key": value} pairs (e.g. \'{"sidecar.vendor": "Pinnacle"}\')',
    )
    p.add_argument("--after", help="YYYY-MM-DD or ISO 8601 datetime; lower bound on effective date")
    p.add_argument("--before", help="YYYY-MM-DD or ISO 8601 datetime; upper bound on effective date")
    p.add_argument(
        "--date-field",
        choices=["tika.created_at", "frontmatter.date", "sidecar.date", "ingest"],
        help="Override auto-discovery; pin sort to a single source field",
    )
    p.add_argument("-l", "--limit", type=int, default=10, help="Max results (default: 10)")
    p.set_defaults(_handler=run)


def run(args) -> int:
    arguments: dict = {
        "direction": args.direction,
        "limit": args.limit,
    }
    if args.query:
        arguments["query"] = args.query
    if args.after:
        arguments["after"] = args.after
    if args.before:
        arguments["before"] = args.before
    if args.date_field:
        arguments["date_field"] = args.date_field
    if args.metadata_filter:
        try:
            arguments["metadata_filter"] = _json.loads(args.metadata_filter)
        except _json.JSONDecodeError as exc:
            sys.stderr.write(f"harbor-clerk: --metadata-filter must be valid JSON: {exc}\n")
            return 1

    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_documents_by_date", arguments)
    render(payload, mode=mode, command="documents-by-date")
    return 0
