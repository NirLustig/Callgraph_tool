"""
#include extraction + cycle detection for C / C++ source and header files.

Public surface:
    build_include_graph(files, *, project_root, build_info=None, follow_system=False) -> IncludeGraph

This is a *regex* extractor — no real preprocessor. Local includes ("...") are resolved
using, in order:
    1. same directory as the including file
    2. project-wide basename index built from the discovered file list
    3. include paths from BuildInfo.units[file].includes (compile_commands.json)
    4. global include paths from BuildInfo.global_includes (.vcxproj merged)

System includes (<...>) are tagged is_system=True. Unless follow_system=True, they
are still recorded but the UI hides them by default.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

from .models import BuildInfo, IncludeEdge, IncludeGraph


_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*([<"])([^">]+)[">]', re.MULTILINE)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")

_C_EXTS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inl", ".tpp"}


def build_include_graph(
    files: Iterable[Path],
    *,
    project_root: Optional[Path] = None,
    build_info: Optional[BuildInfo] = None,
    follow_system: bool = False,
) -> IncludeGraph:
    """Build an IncludeGraph for the given C/C++ files."""
    file_list = [Path(p) for p in files if Path(p).suffix.lower() in _C_EXTS]
    abs_paths = {str(p.resolve()): p for p in file_list}

    # basename index for cheap project-wide resolution
    by_basename: dict[str, list[str]] = {}
    for abs_str in abs_paths:
        name = Path(abs_str).name.lower()
        by_basename.setdefault(name, []).append(abs_str)

    # global include paths from build_info
    global_includes: list[Path] = []
    if build_info is not None:
        for inc in build_info.global_includes:
            global_includes.append(Path(inc))

    files_by_path: dict[str, list[IncludeEdge]] = {}
    unresolved: list[IncludeEdge] = []
    in_degree: dict[str, int] = {}

    for abs_str, p in abs_paths.items():
        edges = _scan_includes(
            p,
            abs_str,
            abs_paths=abs_paths,
            by_basename=by_basename,
            project_includes=_unit_includes(build_info, abs_str),
            global_includes=global_includes,
            follow_system=follow_system,
        )
        files_by_path[abs_str] = edges
        for e in edges:
            if not e.resolved:
                unresolved.append(e)
            else:
                in_degree[e.to_file] = in_degree.get(e.to_file, 0) + 1

    cycles = _find_cycles(files_by_path)

    most_included = sorted(in_degree.items(), key=lambda kv: kv[1], reverse=True)[:25]

    return IncludeGraph(
        files=files_by_path,
        unresolved=unresolved,
        cycles=cycles,
        most_included=most_included,
    )


def _unit_includes(build_info: Optional[BuildInfo], abs_path: str) -> list[Path]:
    if build_info is None:
        return []
    unit = build_info.units.get(abs_path)
    if unit is None:
        return []
    return [Path(p) for p in unit.includes]


def _strip_comments(text: str) -> str:
    text = _BLOCK_COMMENT_RE.sub("", text)
    text = _LINE_COMMENT_RE.sub("", text)
    return text


def _scan_includes(
    file_path: Path,
    abs_str: str,
    *,
    abs_paths: dict[str, Path],
    by_basename: dict[str, list[str]],
    project_includes: list[Path],
    global_includes: list[Path],
    follow_system: bool,
) -> list[IncludeEdge]:
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    cleaned = _strip_comments(text)

    edges: list[IncludeEdge] = []
    for m in _INCLUDE_RE.finditer(cleaned):
        delim = m.group(1)
        target = m.group(2).strip()
        is_system = delim == "<"
        # Approximate line number by counting newlines up to match.
        line = cleaned.count("\n", 0, m.start()) + 1

        resolved_path = _resolve(
            target,
            file_path,
            abs_paths=abs_paths,
            by_basename=by_basename,
            project_includes=project_includes,
            global_includes=global_includes,
            is_system=is_system,
        )

        if resolved_path is None:
            edges.append(IncludeEdge(
                from_file=abs_str,
                to_file=target,
                is_system=is_system,
                resolved=False,
                raw_target=target,
                line=line,
            ))
        else:
            edges.append(IncludeEdge(
                from_file=abs_str,
                to_file=resolved_path,
                is_system=is_system,
                resolved=True,
                raw_target=target,
                line=line,
            ))
    return edges


def _resolve(
    target: str,
    including_file: Path,
    *,
    abs_paths: dict[str, Path],
    by_basename: dict[str, list[str]],
    project_includes: list[Path],
    global_includes: list[Path],
    is_system: bool,
) -> Optional[str]:
    target_norm = target.replace("\\", "/")
    # 1. Same directory (only for local includes; system includes skip this)
    if not is_system:
        candidate = (including_file.parent / target_norm).resolve()
        if str(candidate) in abs_paths:
            return str(candidate)

    # 2. Basename index (project-wide)
    matches = by_basename.get(Path(target_norm).name.lower(), [])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Prefer one ending with the full target path.
        tail = target_norm.lower()
        for m in matches:
            if m.replace("\\", "/").lower().endswith(tail):
                return m

    # 3 + 4. Project / global include paths
    for inc_root in list(project_includes) + list(global_includes):
        candidate = (inc_root / target_norm).resolve()
        if str(candidate) in abs_paths:
            return str(candidate)
    return None


# ------------------------------------------------------------------ #
# Cycle detection                                                     #
# ------------------------------------------------------------------ #

def _find_cycles(files_by_path: dict[str, list[IncludeEdge]]) -> list[list[str]]:
    """Iterative DFS that captures simple back-edge cycles. Returns deduplicated cycle paths."""
    visited: set[str] = set()
    on_stack: set[str] = set()
    cycles_set: set[tuple[str, ...]] = set()

    def _normalise_cycle(seq: list[str]) -> tuple[str, ...]:
        # Rotate so the lexicographically smallest element is first; preserves order.
        if not seq:
            return tuple()
        m = min(range(len(seq)), key=lambda i: seq[i])
        return tuple(seq[m:] + seq[:m])

    for start in files_by_path:
        if start in visited:
            continue
        # Iterative DFS
        stack: list[tuple[str, int, list[str]]] = [(start, 0, [start])]
        path_index: dict[str, int] = {start: 0}
        on_stack.add(start)
        while stack:
            node, ei, path = stack[-1]
            edges = [e for e in files_by_path.get(node, []) if e.resolved and not e.is_system]
            if ei >= len(edges):
                stack.pop()
                on_stack.discard(node)
                path_index.pop(node, None)
                visited.add(node)
                continue
            stack[-1] = (node, ei + 1, path)
            nxt = edges[ei].to_file
            if nxt in on_stack:
                # Found cycle: slice path from where nxt appears
                start_idx = path_index.get(nxt)
                if start_idx is not None:
                    cyc = path[start_idx:] + [nxt]
                    cycles_set.add(_normalise_cycle(cyc[:-1]))  # drop dup tail
                continue
            if nxt in visited:
                continue
            on_stack.add(nxt)
            new_path = path + [nxt]
            path_index[nxt] = len(new_path) - 1
            stack.append((nxt, 0, new_path))

    return [list(c) for c in sorted(cycles_set, key=lambda t: (len(t), t))]
