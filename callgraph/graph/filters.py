"""
Pure filter functions that operate on CallGraph data.
All functions are side-effect-free: they return new filtered structures.
"""
from __future__ import annotations

import fnmatch
from collections import deque
from pathlib import Path

from ..config import Config
from ..models import CallGraph, CallRelationship, FunctionDef


def _matches_patterns(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def filter_functions_by_name(
    functions: dict[str, FunctionDef], config: Config
) -> dict[str, FunctionDef]:
    """Apply include/exclude function name patterns from config."""
    result = {}
    inc = config.filter.include_functions
    exc = config.filter.exclude_functions

    for node_id, fn in functions.items():
        if exc and _matches_patterns(fn.name, exc):
            continue
        if inc and not _matches_patterns(fn.name, inc):
            continue
        result[node_id] = fn

    return result


def filter_functions_by_file(
    functions: dict[str, FunctionDef], config: Config
) -> dict[str, FunctionDef]:
    """Apply include/exclude file/directory patterns from config."""
    result = {}
    exc_files = config.filter.exclude_files
    inc_files = config.filter.include_files
    exc_dirs = config.filter.exclude_dirs
    inc_dirs = config.filter.include_dirs

    for node_id, fn in functions.items():
        path = Path(fn.file_path)
        name = path.name
        parts = path.parts

        # Check directory exclusions against all path segments
        if exc_dirs and any(_matches_patterns(part, exc_dirs) for part in parts):
            continue
        if inc_dirs:
            if not any(_matches_patterns(part, inc_dirs) for part in parts):
                continue

        # Check file exclusions
        if exc_files and _matches_patterns(name, exc_files):
            continue
        if inc_files and not _matches_patterns(name, inc_files):
            continue

        result[node_id] = fn

    return result


def filter_external(
    graph: CallGraph, config: Config
) -> tuple[dict[str, FunctionDef], list[CallRelationship]]:
    """Remove external (stub) functions and their edges if show_external is False."""
    if config.filter.show_external:
        return graph.functions, graph.calls

    kept_functions = {k: v for k, v in graph.functions.items() if not v.is_external}
    kept_ids = set(kept_functions.keys())
    kept_calls = [
        c for c in graph.calls
        if c.caller_id in kept_ids and (c.callee_id is None or c.callee_id in kept_ids)
    ]
    return kept_functions, kept_calls


def bfs_depth_filter(
    functions: dict[str, FunctionDef],
    calls: list[CallRelationship],
    entry_point_names: list[str],
    max_depth: int | None,
) -> tuple[dict[str, FunctionDef], list[CallRelationship]]:
    """
    BFS from entry point functions up to max_depth.
    Returns only reachable functions and calls.
    If entry_points is empty, returns everything unchanged.
    If max_depth is None, traverses the entire reachable subgraph.
    """
    if not entry_point_names:
        return functions, calls

    # Build adjacency: caller_id → [callee_id]
    adj: dict[str, list[str]] = {}
    for call in calls:
        if call.callee_id and call.is_resolved:
            adj.setdefault(call.caller_id, []).append(call.callee_id)

    # Find entry point node_ids by matching function names
    entry_ids: set[str] = set()
    for nid, fn in functions.items():
        if fn.name in entry_point_names or fn.qualified_name in entry_point_names:
            entry_ids.add(nid)

    if not entry_ids:
        # No matching entry points found — return all (warn handled in CLI)
        return functions, calls

    # BFS
    visited: dict[str, int] = {}   # node_id → depth
    queue: deque[tuple[str, int]] = deque()
    for eid in entry_ids:
        queue.append((eid, 0))

    while queue:
        nid, depth = queue.popleft()
        if nid in visited:
            continue
        visited[nid] = depth
        if max_depth is not None and depth >= max_depth:
            continue
        for neighbor in adj.get(nid, []):
            if neighbor not in visited:
                queue.append((neighbor, depth + 1))

    reachable = set(visited.keys())
    filtered_fns = {k: v for k, v in functions.items() if k in reachable}
    filtered_calls = [
        c for c in calls
        if c.caller_id in reachable and (not c.callee_id or c.callee_id in reachable)
    ]
    return filtered_fns, filtered_calls


def apply_node_cap(
    functions: dict[str, FunctionDef],
    calls: list[CallRelationship],
    max_nodes: int,
) -> tuple[dict[str, FunctionDef], list[CallRelationship], bool]:
    """
    If functions exceed max_nodes, trim to the cap (prioritise non-external).
    Returns (functions, calls, was_capped).
    """
    if len(functions) <= max_nodes:
        return functions, calls, False

    # Prefer internal functions
    internal = [(k, v) for k, v in functions.items() if not v.is_external]
    external = [(k, v) for k, v in functions.items() if v.is_external]

    kept_items = (internal + external)[:max_nodes]
    kept = dict(kept_items)
    kept_ids = set(kept.keys())
    kept_calls = [c for c in calls if c.caller_id in kept_ids and
                  (not c.callee_id or c.callee_id in kept_ids)]
    return kept, kept_calls, True
