"""
Architecture: module inference + dependency rule engine.

Public surface:
    build_modules(files, mapping_cfg, build_info=None, project_root=None) -> dict[name, ModuleDef]
    file_to_module(modules) -> dict[abs_file_path, module_name]
    validate(graph, rules, modules) -> list[ArchitectureViolation]

`mapping_cfg` is the `architecture.modules` mapping (name -> [glob, ...]).
User globs win. Files unmatched by user globs fall back to:
    - .vcxproj project name (when BuildInfo has project_files), else
    - first folder under `src/` if it exists, else top-level folder.

`rules` is a list of plain dicts from YAML; we normalise into ArchitectureRule objects here.
"""
from __future__ import annotations

import fnmatch
import re
from collections import defaultdict
from pathlib import Path, PurePath
from typing import Iterable, Optional

from .models import (
    ArchitectureRule,
    ArchitectureViolation,
    BuildInfo,
    CallGraph,
    ModuleDef,
)


# ------------------------------------------------------------------ #
# Module inference                                                    #
# ------------------------------------------------------------------ #

def build_modules(
    files: Iterable[Path | str],
    mapping_cfg: Optional[dict[str, list[str]]] = None,
    build_info: Optional[BuildInfo] = None,
    project_root: Optional[Path] = None,
) -> dict[str, ModuleDef]:
    """Group source files into ModuleDefs."""
    mapping_cfg = mapping_cfg or {}
    abs_files: list[str] = [_norm(p) for p in files]
    root = Path(project_root).resolve() if project_root else None

    project_files_index: dict[str, str] = {}
    if build_info is not None:
        for proj, plist in build_info.project_files.items():
            for f in plist:
                project_files_index[_norm(f)] = proj

    out: dict[str, ModuleDef] = {}

    def _add(mod_name: str, src_path: str, inferred_from: str, project: Optional[str] = None) -> None:
        md = out.get(mod_name)
        if md is None:
            md = ModuleDef(name=mod_name, files=set(), inferred_from=inferred_from, project=project)
            out[mod_name] = md
        md.files.add(src_path)

    for f in abs_files:
        rel_str = _project_relative(f, root)
        # 1) User globs (first match wins, preserve mapping_cfg insertion order)
        matched_user = False
        for mod_name, patterns in mapping_cfg.items():
            for pat in patterns:
                if _glob_match(rel_str, pat):
                    _add(mod_name, f, "config")
                    matched_user = True
                    break
            if matched_user:
                break
        if matched_user:
            continue

        # 2) Project name from .sln
        proj = project_files_index.get(f)
        if proj:
            _add(proj, f, "project", project=proj)
            continue

        # 3) Folder fallback (src/<name>/ or top-level)
        folder = _folder_module_name(rel_str)
        _add(folder, f, "folder")

    return out


def file_to_module(modules: dict[str, ModuleDef]) -> dict[str, str]:
    """Reverse-index: abs file path -> module name."""
    idx: dict[str, str] = {}
    for name, mod in modules.items():
        for f in mod.files:
            idx[_norm(f)] = name
    return idx


def _norm(p: Path | str) -> str:
    try:
        return str(Path(p).resolve())
    except OSError:
        return str(Path(p))


def _project_relative(abs_path: str, root: Optional[Path]) -> str:
    p = Path(abs_path)
    if root is not None:
        try:
            return str(p.resolve().relative_to(root).as_posix())
        except (ValueError, OSError):
            return p.as_posix()
    return p.as_posix()


def _glob_match(rel_path: str, pattern: str) -> bool:
    """Match a project-relative path against a glob.

    Supports ``**`` to mean 'zero or more directories'. fnmatch alone does not.
    """
    pat = pattern.replace("\\", "/")
    rel = rel_path.replace("\\", "/")
    if "**" not in pat:
        return fnmatch.fnmatch(rel, pat)
    # Convert glob to regex manually: escape regex meta, then turn ** -> .*,
    # * -> [^/]*, ? -> [^/]. This avoids depending on fnmatch.translate's
    # cross-version output format.
    out: list[str] = []
    i = 0
    while i < len(pat):
        ch = pat[i]
        if ch == "*" and i + 1 < len(pat) and pat[i + 1] == "*":
            out.append(".*")
            i += 2
            # Consume an optional trailing slash so 'foo/**' matches 'foo' too.
            if i < len(pat) and pat[i] == "/":
                i += 1
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    regex = "^" + "".join(out) + "$"
    return re.match(regex, rel) is not None


def _folder_module_name(rel_path: str) -> str:
    """Return the module name implied by the folder structure."""
    parts = PurePath(rel_path).parts
    if not parts:
        return "<root>"
    if parts[0].lower() == "src" and len(parts) >= 2:
        return parts[1]
    return parts[0]


# ------------------------------------------------------------------ #
# Rule normalisation                                                  #
# ------------------------------------------------------------------ #

_RULE_KINDS = {"forbidden", "allowed_only", "required", "layer"}


def normalise_rules(raw_rules: list[dict]) -> list[ArchitectureRule]:
    rules: list[ArchitectureRule] = []
    for raw in raw_rules:
        kind = str(raw.get("kind", "")).lower().strip()
        if kind not in _RULE_KINDS:
            continue
        rule = ArchitectureRule(
            kind=kind,
            from_module=str(raw.get("from", "")),
            to_module=str(raw.get("to", "")),
            reason=str(raw.get("reason", "")),
        )
        if kind == "allowed_only":
            rule.allowed_targets = list(raw.get("allowed_targets", []) or [])
        if kind == "layer":
            rule.layers = list(raw.get("layers", []) or [])
        rules.append(rule)
    return rules


# ------------------------------------------------------------------ #
# Validation                                                          #
# ------------------------------------------------------------------ #

def validate(
    graph: CallGraph,
    raw_rules: list[dict],
    modules: dict[str, ModuleDef],
) -> list[ArchitectureViolation]:
    """Evaluate every rule against the graph and return a list of violations.

    Also stamps `confidence_category = "violation"` on offending edges so the renderer
    can colour them red regardless of original confidence.
    """
    rules = normalise_rules(raw_rules)
    if not rules or not modules:
        return []

    f2m = file_to_module(modules)
    fn_to_module = _function_to_module(graph, f2m)
    pair_edges = _edges_by_module_pair(graph, fn_to_module)

    violations: list[ArchitectureViolation] = []

    # Build a quick layer-index map for layer rules.
    for rule in rules:
        if rule.kind == "forbidden":
            for (src, dst), edges in pair_edges.items():
                if src == dst:
                    continue
                if _wildcard(rule.from_module, src) and _wildcard(rule.to_module, dst):
                    _mark_violation(edges)
                    violations.append(ArchitectureViolation(
                        rule_kind="forbidden",
                        from_module=src,
                        to_module=dst,
                        reason=rule.reason or f"{src} must not call {dst}",
                        sample_edges=[(c.caller_id, c.callee_id or "") for c in edges[:5]],
                    ))
        elif rule.kind == "allowed_only":
            allowed = set(rule.allowed_targets)
            for (src, dst), edges in pair_edges.items():
                if src == dst:
                    continue
                if not _wildcard(rule.from_module, src):
                    continue
                if dst in allowed:
                    continue
                _mark_violation(edges)
                violations.append(ArchitectureViolation(
                    rule_kind="allowed_only",
                    from_module=src,
                    to_module=dst,
                    reason=rule.reason or f"{src} may only call {sorted(allowed)}",
                    sample_edges=[(c.caller_id, c.callee_id or "") for c in edges[:5]],
                ))
        elif rule.kind == "required":
            sources_with_required: set[str] = set()
            for (src, dst), _edges in pair_edges.items():
                if _wildcard(rule.from_module, src) and _wildcard(rule.to_module, dst):
                    sources_with_required.add(src)
            for src in sorted({m for m in modules if _wildcard(rule.from_module, m)}):
                if src not in sources_with_required:
                    violations.append(ArchitectureViolation(
                        rule_kind="required",
                        from_module=src,
                        to_module=rule.to_module,
                        reason=rule.reason or f"{src} must call {rule.to_module}",
                        sample_edges=[],
                    ))
        elif rule.kind == "layer":
            layer_index = {name: i for i, name in enumerate(rule.layers)}
            for (src, dst), edges in pair_edges.items():
                if src == dst:
                    continue
                si = layer_index.get(src)
                di = layer_index.get(dst)
                if si is None or di is None:
                    continue
                if di < si:  # callee is in a higher (earlier) layer than caller
                    _mark_violation(edges)
                    violations.append(ArchitectureViolation(
                        rule_kind="layer",
                        from_module=src,
                        to_module=dst,
                        reason=rule.reason or f"{src} (layer {si}) must not call {dst} (layer {di})",
                        sample_edges=[(c.caller_id, c.callee_id or "") for c in edges[:5]],
                    ))

    return violations


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _function_to_module(graph: CallGraph, f2m: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for nid, fn in graph.functions.items():
        if fn.is_external or fn.file_path == "<external>":
            continue
        mod = f2m.get(_norm(fn.file_path))
        if mod:
            out[nid] = mod
    return out


def _edges_by_module_pair(graph: CallGraph, fn_to_module: dict[str, str]) -> dict[tuple[str, str], list]:
    buckets: dict[tuple[str, str], list] = defaultdict(list)
    for call in graph.calls:
        if not call.is_resolved or call.callee_id is None:
            continue
        src = fn_to_module.get(call.caller_id)
        dst = fn_to_module.get(call.callee_id)
        if not src or not dst:
            continue
        buckets[(src, dst)].append(call)
    return buckets


def _wildcard(pattern: str, name: str) -> bool:
    if pattern == "" or pattern == "*":
        return True
    return fnmatch.fnmatch(name, pattern)


def _mark_violation(edges: list) -> None:
    for c in edges:
        c.confidence_category = "violation"
