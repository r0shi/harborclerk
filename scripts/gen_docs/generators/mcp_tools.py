"""Generate the MCP tool table from the live server registration.

Source of truth: the `@mcp.tool()`-decorated functions in
`harbor_clerk.mcp_server`. Hand-maintained copies of this table have already
drifted once (the count and the tool list appeared in both README and
CLAUDE.md), which is what this generator exists to prevent.
"""

from __future__ import annotations

import asyncio


def _load_tools() -> list:
    from harbor_clerk.mcp_server import mcp

    manager = getattr(mcp, "_tool_manager", None)
    tools = manager.list_tools() if manager is not None else mcp.list_tools()
    if asyncio.iscoroutine(tools):
        tools = asyncio.run(tools)
    return sorted(tools, key=lambda t: t.name)


def _summary(description: str | None) -> str:
    """First sentence of the docstring, collapsed to a single table cell."""
    if not description:
        return ""
    first_line = next((line.strip() for line in description.strip().splitlines() if line.strip()), "")
    # Keep it to the leading sentence; tool docstrings often continue with
    # usage guidance that belongs in tools/list, not in a summary table.
    for terminator in (". ", "! ", "? "):
        if terminator in first_line:
            first_line = first_line.split(terminator, 1)[0] + terminator.strip()
            break
    return first_line.replace("|", "\\|")


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
