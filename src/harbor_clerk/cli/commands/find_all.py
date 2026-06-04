"""harbor-clerk find-all - enumerate matching documents."""

from __future__ import annotations

import argparse
import json as _json
import sys

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.commands import _common_parser
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.help import load
from harbor_clerk.cli.output import render, resolve_mode

_LANGUAGE_ALIASES = {
    "en": "english",
    "fr": "french",
    "english": "english",
    "french": "french",
}


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    description = load("find-all") or "Enumerate documents matching a query."
    p = subparsers.add_parser(
        "find-all",
        help="Enumerate documents matching a query",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_parser()],
    )
    p.add_argument("query_arg", nargs="?", help="Relevance query")
    p.add_argument("--query", dest="query", help="Relevance query; alternative to positional query")
    p.add_argument("--text-contains", help="Require an exact substring in matching chunk text")
    p.add_argument("--max-results", type=int, default=100, help="Max documents returned (default: 100)")
    p.add_argument("-o", "--offset", type=int, default=0, help="Pagination offset (default: 0)")
    p.add_argument("--presentation", choices=["brief", "full"], default="brief", help="Result detail (default: brief)")
    p.add_argument(
        "--sort-by",
        choices=["relevance", "date_desc", "date_asc"],
        default="relevance",
        help="Sort order (default: relevance)",
    )
    p.add_argument("--doc-id", help="Restrict enumeration to one document UUID")
    p.add_argument("--doc-ids", help="Comma-separated doc UUIDs")
    p.add_argument("--after", help="YYYY-MM-DD or ISO 8601 datetime; only docs after this date")
    p.add_argument("--before", help="YYYY-MM-DD or ISO 8601 datetime; only docs before this date")
    p.add_argument("--language", choices=sorted(_LANGUAGE_ALIASES))
    p.add_argument("--mime-type", help="Restrict by MIME type (e.g. application/pdf)")
    p.add_argument(
        "--metadata-filter-json",
        "--metadata-filter",
        dest="metadata_filter",
        help='JSON string of {"namespace.key": value} pairs (e.g. \'{"email.from_address": "alice@example.com"}\')',
    )
    p.set_defaults(_handler=run)


def run(args) -> int:
    query = args.query or args.query_arg
    if args.query and args.query_arg:
        sys.stderr.write("harbor-clerk: provide query either positionally or with --query, not both\n")
        return 1
    if not query:
        sys.stderr.write("harbor-clerk: find-all requires a query or --query\n")
        return 1

    arguments: dict = {
        "query": query,
        "max_results": args.max_results,
        "offset": args.offset,
        "presentation": args.presentation,
        "sort_by": args.sort_by,
    }
    if args.text_contains:
        arguments["text_contains"] = args.text_contains
    if args.doc_id:
        arguments["doc_id"] = args.doc_id
    if args.doc_ids:
        arguments["doc_ids"] = [d.strip() for d in args.doc_ids.split(",") if d.strip()]
    if args.after:
        arguments["after"] = args.after
    if args.before:
        arguments["before"] = args.before
    if args.language:
        arguments["language"] = _LANGUAGE_ALIASES[args.language]
    if args.mime_type:
        arguments["mime_type"] = args.mime_type
    if args.metadata_filter:
        try:
            metadata_filter = _json.loads(args.metadata_filter)
        except _json.JSONDecodeError as exc:
            sys.stderr.write(f"harbor-clerk: --metadata-filter-json must be valid JSON: {exc}\n")
            return 1
        if not isinstance(metadata_filter, dict):
            sys.stderr.write("harbor-clerk: --metadata-filter-json must be a JSON object\n")
            return 1
        arguments["metadata_filter"] = metadata_filter

    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_find_all", arguments)
    render(payload, mode=mode, command="find-all")
    return 0
