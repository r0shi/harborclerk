"""harbor-clerk system-health — daemon + worker health snapshot."""

from __future__ import annotations

import argparse
import sys

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.commands import _common_parser
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.help import load
from harbor_clerk.cli.output import render, resolve_mode


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    description = load("system-health") or "Daemon + worker health snapshot."
    p = subparsers.add_parser(
        "system-health",
        help="Daemon + worker health snapshot",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_parser()],
    )
    p.set_defaults(_handler=run)


def run(args) -> int:
    arguments: dict = {}

    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_system_health", arguments)
    render(payload, mode=mode, command="system-health")
    return 0
