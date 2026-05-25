"""Subcommand registrations for harbor-clerk CLI."""

from __future__ import annotations

import argparse
import sys


def _common_parser() -> argparse.ArgumentParser:
    """Return a parent parser carrying the shared global flags.

    Each subcommand's add_parser() passes ``parents=[_common_parser()]`` so
    that ``--url``, ``--api-key``, ``--insecure``, ``--json``, and ``--format``
    are recognised on the command line.  These flags MUST appear AFTER the
    subcommand name (e.g. ``harbor-clerk search --json "foo"``).  Placing them
    before the subcommand will produce a clear argparse error rather than
    silently being dropped.
    """
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--url", help="Override $HARBOR_CLERK_URL")
    p.add_argument("--api-key", help="Override $HARBOR_CLERK_API_KEY")
    p.add_argument("--insecure", action="store_true", help="Allow self-signed TLS")
    output_group = p.add_mutually_exclusive_group()
    output_group.add_argument("--json", action="store_true", help="Force JSON output")
    output_group.add_argument("--format", choices=["text", "json"], help="Output format")
    return p


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
    "verify-identifier",
    "documents-by-date",
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
        except ModuleNotFoundError:
            p = subparsers.add_parser(name, help=f"[stub] {name}")
            p.set_defaults(_handler=_stub_handler(name))
            continue
        module.add_parser(subparsers)


def _stub_handler(name: str):
    def _h(_args) -> int:
        print(f"harbor-clerk: {name} not yet implemented", file=sys.stderr)
        return 2

    return _h
