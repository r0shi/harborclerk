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


def _walk_routes(obj, seen: set[int] | None = None):
    """Yield every APIRoute, descending through FastAPI's router wrappers."""
    from fastapi.routing import APIRoute

    seen = seen if seen is not None else set()
    for route in getattr(obj, "routes", []) or []:
        if id(route) in seen:
            continue
        seen.add(id(route))
        if isinstance(route, APIRoute):
            yield route
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _walk_routes(original, seen)
        elif not isinstance(route, APIRoute):
            yield from _walk_routes(route, seen)


def _access_of(route) -> str:
    """Derive the authorization gate from the route's dependency tree.

    The read/write axis in this codebase is human-vs-API-key, not admin-vs-user:
    `require_human_user` rejects API keys but accepts any human, while
    `require_admin` is the genuinely narrower gate. The old hand-written table
    annotated these by hand; deriving them means they cannot fall out of date.
    """
    names: set[str] = set()

    def walk(dependant) -> None:
        name = getattr(dependant.call, "__name__", None)
        if name:
            names.add(name)
        for sub in dependant.dependencies:
            walk(sub)

    walk(route.dependant)
    if "require_admin" in names:
        return "admin"
    if "require_human_user" in names:
        return "human only"
    return ""


def _access_map() -> list[tuple[frozenset[str], str, str]]:
    from harbor_clerk.api.app import app

    return [(frozenset(r.methods), r.path, _access_of(r)) for r in _walk_routes(app)]


def _operations() -> list[tuple[str, str, str, str, str]]:
    """Return (tag, METHOD, path, summary, access), sorted stably.

    Paths come from the OpenAPI schema (fully prefixed); the access gate comes
    from the route objects, whose paths are router-relative. They are joined on
    method plus path suffix, and an ambiguous or missing join raises rather
    than silently mislabelling an endpoint's authorization.
    """
    from harbor_clerk.api.app import app

    routes = _access_map()
    spec = app.openapi()
    ops: list[tuple[str, str, str, str, str]] = []
    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            if method.lower() not in _METHODS or not isinstance(operation, dict):
                continue
            upper = method.upper()
            candidates = [
                (route_path, access)
                for verbs, route_path, access in routes
                if upper in verbs and path.endswith(route_path)
            ]
            if not candidates:
                raise RuntimeError(
                    f"no route matched {upper} {path}; cannot determine its authorization gate. Refusing to guess."
                )
            # Router paths are relative, so several can be suffixes of one spec
            # path (e.g. "/stats" and "/system/stats" both end "/api/system/stats").
            # The longest suffix is the most specific route and the correct match.
            longest = max(len(route_path) for route_path, _ in candidates)
            best = {access for route_path, access in candidates if len(route_path) == longest}
            if len(best) != 1:
                raise RuntimeError(
                    f"ambiguous authorization gate for {upper} {path}: "
                    f"{len(best)} equally-specific routes disagree. Refusing to guess."
                )
            tag = (operation.get("tags") or ["untagged"])[0]
            ops.append((tag, upper, path, (operation.get("summary") or "").strip(), best.pop()))
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
    by_tag: dict[str, list[tuple[str, str, str, str, str]]] = defaultdict(list)
    for op in ops:
        by_tag[op[0]].append(op)

    lines = [
        f"**{len(ops)} operations across {len(by_tag)} groups.** "
        "Full reference: [docs/architecture.md](docs/architecture.md#rest-api).",
        "",
        "| Group | Operations | Admin-only | Base path |",
        "|---|---|---|---|",
    ]
    for tag in sorted(by_tag, key=lambda t: (-len(by_tag[t]), t)):
        entries = by_tag[tag]
        admin = sum(1 for e in entries if e[4] == "admin")
        lines.append(f"| `{tag}` | {len(entries)} | {admin or '—'} | `{_common_prefix([e[2] for e in entries])}` |")
    return "\n".join(lines)


def generate_full() -> str:
    ops = _operations()
    by_tag: dict[str, list[tuple[str, str, str, str, str]]] = defaultdict(list)
    for op in ops:
        by_tag[op[0]].append(op)

    gated = sum(1 for op in ops if op[4])
    lines = [
        f"**{len(ops)} operations**, {gated} of them access-gated. "
        "Generated from the FastAPI OpenAPI schema; the Access column is derived "
        "from each route's dependency tree.",
        "",
    ]
    for tag in sorted(by_tag):
        lines += [f"### `{tag}`", "", "| Method | Path | Access | Summary |", "|---|---|---|---|"]
        for _, method, path, summary, access in by_tag[tag]:
            cell = f"**{access}**" if access else "—"
            lines.append(f"| `{method}` | `{path}` | {cell} | {summary.replace('|', chr(92) + '|')} |")
        lines.append("")
    return "\n".join(lines).rstrip()
