"""Output rendering for the harbor-clerk CLI."""

from __future__ import annotations

import enum
import json
import sys
from collections.abc import Callable
from typing import Any, TextIO


class OutputMode(enum.Enum):
    JSON = "json"
    TEXT = "text"


def resolve_mode(*, force_json: bool, fmt: str | None, isatty: bool) -> OutputMode:
    if fmt == "json" or force_json:
        return OutputMode.JSON
    if fmt == "text":
        return OutputMode.TEXT
    return OutputMode.TEXT if isatty else OutputMode.JSON


# Per-command text pretty-printers. Unknown commands fall back to JSON.
_TEXT_RENDERERS: dict[str, Callable[[Any, TextIO], None]] = {}


def register_text_renderer(command: str):
    def deco(fn: Callable[[Any, TextIO], None]):
        _TEXT_RENDERERS[command] = fn
        return fn

    return deco


def render(payload: Any, *, mode: OutputMode, command: str, stream: TextIO | None = None) -> None:
    stream = stream or sys.stdout
    if mode == OutputMode.JSON:
        json.dump(payload, stream, indent=2, ensure_ascii=False, default=str)
        stream.write("\n")
        return

    renderer = _TEXT_RENDERERS.get(command)
    if renderer is None:
        json.dump(payload, stream, indent=2, ensure_ascii=False, default=str)
        stream.write("\n")
        return
    renderer(payload, stream)


# --- Search results pretty-printer ---


@register_text_renderer("search")
def _render_search(payload: Any, stream: TextIO) -> None:
    if not isinstance(payload, dict):
        stream.write(repr(payload) + "\n")
        return
    results = payload.get("results", [])
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        citation = r.get("citation", "")
        score = r.get("score")
        snippet = (r.get("snippet") or "").strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "?"
        stream.write(f"{i}. {title}  [{score_str}]  {citation}\n")
        stream.write(f"   {snippet}\n\n")
    if payload.get("possible_conflict"):
        stream.write("⚠  possible_conflict=true — top hits disagree across documents\n")
