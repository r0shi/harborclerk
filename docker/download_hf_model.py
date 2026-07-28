"""Download a Hugging Face model snapshot with retry/backoff for CI builds."""

from __future__ import annotations

import argparse
import random
import sys
import time
from collections.abc import Sequence

from huggingface_hub import snapshot_download


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--local-dir", required=True)
    parser.add_argument("--ignore-pattern", action="append", default=[])
    parser.add_argument("--attempts", type=int, default=6)
    parser.add_argument("--base-delay", type=float, default=8.0)
    return parser.parse_args()


def download(args: argparse.Namespace) -> None:
    ignore_patterns: Sequence[str] | None = args.ignore_pattern or None
    for attempt in range(1, args.attempts + 1):
        try:
            snapshot_download(
                repo_id=args.repo_id,
                local_dir=args.local_dir,
                ignore_patterns=ignore_patterns,
            )
            return
        except Exception as exc:
            if attempt >= args.attempts:
                raise

            delay = args.base_delay * (2 ** (attempt - 1)) + random.uniform(0, args.base_delay)
            # Renders the {type}: {message} shape inline on purpose: this is a
            # standalone build script that runs before the harbor_clerk package
            # exists in the image, so it cannot import error_text.describe_error.
            print(
                f"Hugging Face download failed on attempt {attempt}/{args.attempts}: {exc.__class__.__name__}: {exc}",
                file=sys.stderr,
            )
            print(f"Retrying in {delay:.1f}s...", file=sys.stderr)
            time.sleep(delay)


def main() -> None:
    download(parse_args())


if __name__ == "__main__":
    main()
