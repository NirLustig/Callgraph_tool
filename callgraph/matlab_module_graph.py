"""
MATLAB module / file-dependency graph (roadmap gap G5).

MATLAB has no ``#include`` mechanism, so unlike C/C++ there is no textual dependency
to extract. Instead we synthesise a file-dependency graph from two sources:

  1. **Cross-file calls** — file A depends on file B when a function in A calls a
     function *defined* in B (resolved by simple-name lookup against the project's
     MATLAB function table). This is the primary, most reliable signal.
  2. **Explicit path / run directives** — ``addpath('dir')``, ``run('file')`` and
     ``import pkg.*`` statements are parsed as soft edges. ``run`` targets a file;
     ``addpath`` targets a directory (recorded as an unresolved soft edge unless a
     matching project directory is found).

The result reuses the existing :class:`IncludeGraph` / :class:`IncludeEdge` model so the
HTML renderer's include-graph view works unchanged. Cycle detection reuses
``include_graph._find_cycles``.

Design constraints (Obsidian/Rules.md):
  * Pure regex, offline, conservative — a missing edge is preferred over a wrong one.
  * Only ``.m`` files participate; mixed C/MATLAB projects keep the two graphs separate
    and the CLI merges them into one ``IncludeGraph`` for display.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

from .include_graph import _find_cycles
from .models import CallRelationship, FunctionDef, IncludeEdge, IncludeGraph, Language

_M_EXTS = {".m"}

# addpath('path') / addpath "path" / addpath path
_ADDPATH_RE = re.compile(r"^\s*addpath\s*\(?\s*['\"]?([^'\")\n;]+)['\"]?\s*\)?", re.IGNORECASE)
# run('file') / run "file" / run file.m
_RUN_RE = re.compile(r"^\s*run\s*\(?\s*['\"]?([^'\")\n;]+\.m)['\"]?\s*\)?", re.IGNORECASE)
# import pkg.Class / import pkg.*
_IMPORT_RE = re.compile(r"^\s*import\s+([A-Za-z_]\w*(?:\.\w+|\.\*)*)", re.IGNORECASE)


def build_matlab_module_graph(
    files: Iterable[Path],
    functions: list[FunctionDef],
    calls: list[CallRelationship],
    *,
    project_root: Optional[Path] = None,
) -> IncludeGraph:
    """Build a file-dependency :class:`IncludeGraph` for MATLAB ``.m`` files.

    ``functions`` and ``calls`` are the already-parsed project entities (MATLAB ones
    are used; others are ignored). File nodes are keyed by resolved absolute path so
    they line up with the C/C++ include graph's keys when merged.
    """
    file_list = [Path(p) for p in files if Path(p).suffix.lower() in _M_EXTS]
    abs_by_path = {str(p.resolve()): p for p in file_list}

    # Map every MATLAB function's defining file (absolute) by simple name.
    func_file_by_name: dict[str, set[str]] = {}
    node_to_file: dict[str, str] = {}
    for fn in functions:
        if fn.language != Language.MATLAB:
            continue
        abs_file = str(Path(fn.file_path).resolve())
        node_to_file[fn.node_id] = abs_file
        func_file_by_name.setdefault(fn.name, set()).add(abs_file)
        if fn.qualified_name != fn.name:
            func_file_by_name.setdefault(fn.qualified_name, set()).add(abs_file)

    # basename index for run()/addpath resolution
    by_basename: dict[str, list[str]] = {}
    for abs_str in abs_by_path:
        by_basename.setdefault(Path(abs_str).name.lower(), []).append(abs_str)

    files_by_path: dict[str, list[IncludeEdge]] = {a: [] for a in abs_by_path}
    seen_edges: set[tuple[str, str]] = set()
    in_degree: dict[str, int] = {}
    unresolved: list[IncludeEdge] = []

    def _add_edge(from_file: str, to_file: str, raw: str, line: int, resolved: bool):
        if from_file == to_file:
            return  # self-dependency carries no information
        key = (from_file, to_file)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edge = IncludeEdge(
            from_file=from_file,
            to_file=to_file,
            is_system=False,
            resolved=resolved,
            raw_target=raw,
            line=line,
        )
        files_by_path.setdefault(from_file, []).append(edge)
        if resolved:
            in_degree[to_file] = in_degree.get(to_file, 0) + 1
        else:
            unresolved.append(edge)

    # ── 1. cross-file call dependencies ──────────────────────────────────────
    for call in calls:
        from_file = node_to_file.get(call.caller_id)
        if from_file is None:
            continue
        targets = func_file_by_name.get(call.callee_name)
        if not targets:
            continue
        for to_file in targets:
            _add_edge(from_file, to_file, call.callee_name, call.call_line, resolved=True)

    # ── 2. explicit addpath / run / import directives ────────────────────────
    for abs_str, p in abs_by_path.items():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.split("%", 1)[0]
            if not line.strip():
                continue

            run_m = _RUN_RE.match(line)
            if run_m:
                target = run_m.group(1).strip().replace("\\", "/")
                resolved = _resolve_run_target(target, p, abs_by_path, by_basename)
                if resolved:
                    _add_edge(abs_str, resolved, Path(target).name, lineno, resolved=True)
                else:
                    _add_edge(abs_str, target, target, lineno, resolved=False)
                continue

            add_m = _ADDPATH_RE.match(line)
            if add_m:
                # addpath targets a directory, not a file — record as a soft
                # (unresolved) edge so the dependency is visible without a node.
                target = add_m.group(1).strip().replace("\\", "/")
                _add_edge(abs_str, f"addpath:{target}", target, lineno, resolved=False)
                continue

            imp_m = _IMPORT_RE.match(line)
            if imp_m:
                target = imp_m.group(1).strip()
                _add_edge(abs_str, f"import:{target}", target, lineno, resolved=False)

    cycles = _find_cycles(files_by_path)
    most_included = sorted(in_degree.items(), key=lambda kv: kv[1], reverse=True)[:25]

    return IncludeGraph(
        files=files_by_path,
        unresolved=unresolved,
        cycles=cycles,
        most_included=most_included,
    )


def _resolve_run_target(
    target: str,
    including_file: Path,
    abs_paths: dict[str, Path],
    by_basename: dict[str, list[str]],
) -> Optional[str]:
    """Resolve a ``run('file.m')`` target to an absolute project path, or None."""
    # 1. relative to the including file's directory
    candidate = (including_file.parent / target).resolve()
    if str(candidate) in abs_paths:
        return str(candidate)
    # 2. project-wide basename index
    matches = by_basename.get(Path(target).name.lower(), [])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        tail = target.lower()
        for m in matches:
            if m.replace("\\", "/").lower().endswith(tail):
                return m
    return None


def merge_into(base: Optional[IncludeGraph], extra: IncludeGraph) -> IncludeGraph:
    """Merge ``extra`` (e.g. the MATLAB module graph) into ``base`` (e.g. the C/C++
    include graph). Returns a combined :class:`IncludeGraph`. If ``base`` is None the
    ``extra`` graph is returned unchanged.
    """
    if base is None:
        return extra
    merged_files = dict(base.files)
    for f, edges in extra.files.items():
        merged_files.setdefault(f, [])
        existing = {(e.from_file, e.to_file) for e in merged_files[f]}
        for e in edges:
            if (e.from_file, e.to_file) not in existing:
                merged_files[f].append(e)

    # rebuild in-degree across the merged set
    in_degree: dict[str, int] = {}
    for edges in merged_files.values():
        for e in edges:
            if e.resolved:
                in_degree[e.to_file] = in_degree.get(e.to_file, 0) + 1
    most_included = sorted(in_degree.items(), key=lambda kv: kv[1], reverse=True)[:25]

    return IncludeGraph(
        files=merged_files,
        unresolved=list(base.unresolved) + list(extra.unresolved),
        cycles=list(base.cycles) + list(extra.cycles),
        most_included=most_included,
    )
