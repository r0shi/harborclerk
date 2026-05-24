"""Subcommand registrations for harbor-clerk CLI."""

from __future__ import annotations

import argparse
import sys

_COMMAND_NAMES = [
    "search",
    "batch-search",
    "read-passages",
    "expand-context",
    "read-document",
    "get-document",
    "list-recent",
    "corpus-overview",
    "document-outline",
    "find-related",
    "entity-search",
    "entity-overview",
    "entity-cooccurrence",
    "ingest-status",
    "reprocess",
    "system-health",
]


def register_all(subparsers: argparse._SubParsersAction) -> None:
    """Register every subcommand parser. Imports done lazily to keep startup fast.

    For commands whose impl module doesn't exist yet (during incremental rollout),
    a stub is registered so `harbor-clerk --help` still lists all commands.
    """
    for name in _COMMAND_NAMES:
        module_name = name.replace("-", "_")
        try:
            module = __import__(
                f"harbor_clerk.cli.commands.{module_name}",
                fromlist=["add_parser"],
            )
        except ImportError:
            p = subparsers.add_parser(name, help=f"[stub] {name}")
            p.set_defaults(_handler=_stub_handler(name))
            continue
        module.add_parser(subparsers)


def _stub_handler(name: str):
    def _h(_args) -> int:
        print(f"harbor-clerk: {name} not yet implemented", file=sys.stderr)
        return 2

    return _h
