"""harbor-clerk search — hybrid FTS + vector search."""

from __future__ import annotations

import argparse
import sys

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.commands import _common_parser
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.help import load
from harbor_clerk.cli.output import render, resolve_mode


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
    p.add_argument("-d", "--detail", choices=["full", "brief"], default="full")
    p.add_argument("--brief-chars", type=int, default=None, help="When --detail=brief, chars per snippet")
    p.add_argument("--doc-id", help="Restrict search to one document UUID")
    p.add_argument("--doc-ids", help="Comma-separated doc UUIDs")
    p.add_argument("--after", help="YYYY-MM-DD; only docs ingested after this date")
    p.add_argument("--before", help="YYYY-MM-DD; only docs ingested before this date")
    p.add_argument("--language", choices=["en", "fr"])
    p.add_argument("--mime-type", help="Restrict by MIME type (e.g. application/pdf)")
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
        arguments["language"] = args.language
    if args.mime_type:
        arguments["mime_type"] = args.mime_type
    if args.faceted:
        arguments["faceted"] = True

    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_search", arguments)
    render(payload, mode=mode, command="search")
    return 0
