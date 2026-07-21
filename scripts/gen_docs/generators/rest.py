"""Generate REST API tables from FastAPI's own OpenAPI schema.

Two blocks, because the two audiences differ:

- `rest-summary` (README) — one row per tag. The front door should convey the
  shape of the API, not enumerate 141 operations.
- `rest-endpoints` (architecture.md) — every operation, grouped by tag, for
  someone who needs the actual reference.

Both derive from `app.openapi()`, so neither can drift.
"""

from __future__ import annotations

from collections import defaultdict

_METHODS = ("get", "post", "put", "patch", "delete")


def _operations() -> list[tuple[str, str, str, str]]:
    """Return (tag, METHOD, path, summary), sorted stably."""
    from harbor_clerk.api.app import app

    spec = app.openapi()
    ops: list[tuple[str, str, str, str]] = []
    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            if method.lower() not in _METHODS or not isinstance(operation, dict):
                continue
            tag = (operation.get("tags") or ["untagged"])[0]
            ops.append((tag, method.upper(), path, (operation.get("summary") or "").strip()))
    return sorted(ops, key=lambda o: (o[0], o[2], o[1]))


def _common_prefix(paths: list[str]) -> str:
    """Longest shared path prefix, trimmed to a whole segment."""
    if not paths:
        return ""
    segments = [p.strip("/").split("/") for p in paths]
    shared: list[str] = []
    for parts in zip(*segments, strict=False):
        if len({*parts}) != 1 or parts[0].startswith("{"):
            break
        shared.append(parts[0])
    return "/" + "/".join(shared) if shared else "(various)"


def generate_summary() -> str:
    ops = _operations()
    by_tag: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for op in ops:
        by_tag[op[0]].append(op)

    lines = [
        f"**{len(ops)} operations across {len(by_tag)} groups.** "
        "Full reference: [docs/architecture.md](docs/architecture.md#rest-api).",
        "",
        "| Group | Operations | Base path |",
        "|---|---|---|",
    ]
    for tag in sorted(by_tag, key=lambda t: (-len(by_tag[t]), t)):
        entries = by_tag[tag]
        lines.append(f"| `{tag}` | {len(entries)} | `{_common_prefix([e[2] for e in entries])}` |")
    return "\n".join(lines)


def generate_full() -> str:
    ops = _operations()
    by_tag: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for op in ops:
        by_tag[op[0]].append(op)

    lines = [f"**{len(ops)} operations.** Generated from the FastAPI OpenAPI schema.", ""]
    for tag in sorted(by_tag):
        lines += [f"### `{tag}`", "", "| Method | Path | Summary |", "|---|---|---|"]
        for _, method, path, summary in by_tag[tag]:
            lines.append(f"| `{method}` | `{path}` | {summary.replace('|', chr(92) + '|')} |")
        lines.append("")
    return "\n".join(lines).rstrip()
