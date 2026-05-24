"""Exit-code mapping + structured error output for the harbor-clerk CLI."""

from __future__ import annotations

import json
import sys
from typing import TextIO

from harbor_clerk.cli.client import McpClientError

EXIT_OK = 0
EXIT_USAGE = 1  # argparse default for bad usage
EXIT_CONNECTION = 2
EXIT_CLI_DISABLED = 3
EXIT_AUTH = 4
EXIT_HTTP = 5
EXIT_PROTOCOL = 5  # protocol failures collapsed into the HTTP bucket


_EXIT_FOR_KIND = {
    "connection": EXIT_CONNECTION,
    "cli_disabled": EXIT_CLI_DISABLED,
    "auth": EXIT_AUTH,
    "http": EXIT_HTTP,
    "protocol": EXIT_PROTOCOL,
}


def map_client_error_to_exit(err: McpClientError) -> int:
    return _EXIT_FOR_KIND.get(err.kind, EXIT_HTTP)


def write_error(err: McpClientError, *, json_mode: bool, stream: TextIO | None = None) -> None:
    stream = stream or sys.stderr
    if json_mode:
        json.dump(
            {
                "error_kind": err.kind,
                "message": err.message,
                "status_code": err.status_code,
                "body": err.body,
            },
            stream,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
        stream.write("\n")
    else:
        stream.write(f"harbor-clerk: {err.message}\n")
