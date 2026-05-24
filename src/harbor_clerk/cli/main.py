"""harbor-clerk CLI entry point."""

from __future__ import annotations

import argparse
import sys

from harbor_clerk.cli import __version__
from harbor_clerk.cli.client import McpClientError
from harbor_clerk.cli.commands import register_all
from harbor_clerk.cli.errors import EXIT_USAGE, map_client_error_to_exit, write_error


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that exits with code 1 (not 2) on argument errors."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="harbor-clerk",
        description=(
            "Query a Harbor Clerk knowledge base from the shell. "
            "Run `harbor-clerk <command> --help` for command details."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"harbor-clerk-cli/{__version__}",
    )
    parser.add_argument("--url", help="Override $HARBOR_CLERK_URL")
    parser.add_argument("--api-key", help="Override $HARBOR_CLERK_API_KEY")
    parser.add_argument("--insecure", action="store_true", help="Allow self-signed TLS")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--json", action="store_true", help="Force JSON output")
    output_group.add_argument("--format", choices=["text", "json"], help="Output format")

    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    register_all(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        return handler(args)
    except McpClientError as err:
        json_mode = bool(args.json) or args.format == "json"
        write_error(err, json_mode=json_mode)
        return map_client_error_to_exit(err)
    except ValueError as err:
        # e.g. resolve_config — missing API key
        sys.stderr.write(f"harbor-clerk: {err}\n")
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
