"""Generate the MCP tool table from the live server registration.

Source of truth: the `@mcp.tool()`-decorated functions in
`harbor_clerk.mcp_server`. Hand-maintained copies of this table have already
drifted once (the count and the tool list appeared in both README and
CLAUDE.md), which is what this generator exists to prevent.
"""

from __future__ import annotations

import asyncio
import re


def _load_tools() -> list:
    from harbor_clerk.mcp_server import mcp

    manager = getattr(mcp, "_tool_manager", None)
    tools = manager.list_tools() if manager is not None else mcp.list_tools()
    if asyncio.iscoroutine(tools):
        tools = asyncio.run(tools)
    return sorted(tools, key=lambda t: t.name)


# Terminators that end an abbreviation rather than a sentence. Without this,
# "Use e.g. a doc_id" would be cut at "Use e.g."
_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.", "cf.", "approx.", "Inc.", "Ltd.")

_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def _summary(description: str | None) -> str:
    """First sentence of the docstring, flattened into a single table cell.

    Whitespace is normalised across the *whole* description before looking for
    a sentence end. Docstrings wrap at 120 characters, so an opening sentence
    frequently spans several source lines; reading only the first physical line
    truncates mid-sentence and emits a fragment with no terminal punctuation.
    """
    if not description:
        return ""
    text = " ".join(description.split())
    for match in _SENTENCE_END.finditer(text):
        candidate = text[: match.end()]
        if any(candidate.endswith(abbr) for abbr in _ABBREVIATIONS):
            continue
        text = candidate
        break
    return text.replace("|", "\\|")


def generate() -> str:
    tools = _load_tools()
    lines = [
        f"**{len(tools)} tools.** Authenticate with `Authorization: Bearer <api_key>`.",
        "",
        "| Tool | Description |",
        "|---|---|",
    ]
    lines.extend(f"| `{tool.name}` | {_summary(tool.description)} |" for tool in tools)
    return "\n".join(lines)
