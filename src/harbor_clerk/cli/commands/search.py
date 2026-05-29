"""harbor-clerk search — hybrid FTS + vector search."""

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
    description = load("search") or "Hybrid FTS + vector search."
    p = subparsers.add_parser(
        "search",
        help="Hybrid FTS + vector search across the corpus",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_parser()],
    )
    p.add_argument("query", help="The search query")
    p.add_argument("-k", "--k", type=int, default=10, help="Number of results (default: 10, max: 50)")
    p.add_argument("-o", "--offset", type=int, default=0, help="Pagination offset (default: 0)")
    p.add_argument("-d", "--detail", choices=["full", "brief", "compact"], default="full")
    p.add_argument("--brief-chars", type=int, default=None, help="When --detail=brief, chars per hit text")
    p.add_argument("--doc-id", help="Restrict search to one document UUID")
    p.add_argument("--doc-ids", help="Comma-separated doc UUIDs")
    p.add_argument("--after", help="YYYY-MM-DD; only docs ingested after this date")
    p.add_argument("--before", help="YYYY-MM-DD; only docs ingested before this date")
    p.add_argument("--language", choices=sorted(_LANGUAGE_ALIASES))
    p.add_argument("--mime-type", help="Restrict by MIME type (e.g. application/pdf)")
    p.add_argument(
        "--metadata-filter",
        help='JSON string of {"namespace.key": value} pairs (e.g. \'{"email.from": "alice@example.com"}\')',
    )
    p.add_argument("--text-contains", help="Require an exact substring in the chunk text")
    p.add_argument("--faceted", action="store_true", help="Include facet counts in response")
    p.set_defaults(_handler=run)


def run(args) -> int:
    arguments: dict = {
        "query": args.query,
        "k": args.k,
        "offset": args.offset,
        "detail": args.detail,
    }
    if args.brief_chars is not None:
        arguments["brief_chars"] = args.brief_chars
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
            sys.stderr.write(f"harbor-clerk: --metadata-filter must be valid JSON: {exc}\n")
            return 1
        if not isinstance(metadata_filter, dict):
            sys.stderr.write("harbor-clerk: --metadata-filter must be a JSON object\n")
            return 1
        arguments["metadata_filter"] = metadata_filter
    if args.text_contains:
        arguments["text_contains"] = args.text_contains
    if args.faceted:
        arguments["faceted"] = True

    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_search", arguments)
    render(payload, mode=mode, command="search")
    return 0
