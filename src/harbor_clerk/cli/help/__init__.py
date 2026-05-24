"""Loader for per-subcommand long-help text files."""

from __future__ import annotations

from pathlib import Path


def load(command: str) -> str:
    """Load the help text for a command, falling back to an empty string."""
    path = Path(__file__).parent / f"{command}.txt"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")
