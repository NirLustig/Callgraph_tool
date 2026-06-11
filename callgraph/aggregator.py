"""
Render-level aggregation: collapse the function-level CallGraph to file / folder /
module / library / namespace abstractions.

Public surface:
    aggregate(graph, level, *, project_root=None, modules=None, folder_depth=2) -> CallGraph

The output is a *new* CallGraph whose nodes are synthetic FunctionDef objects with
qualified_name = f"<{level}>::{key}" so their node_ids never collide with real
function nodes. Edges carry confidence_category="aggregated" (unless a violation
takes over via architecture.validate) and a non-trivial underlying_count.

Backward compat: the original collapse_to_files() in graph/builder.py still works;
it now delegates to aggregate(level=SCRIPT) internally.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path, PurePath
from typing import Optional

from .models import (
    CallGraph,
    CallRelationship,
    FunctionDef,
    Language,
    ModuleDef,
    RenderLevel,
    ResolutionConfidence,
)


# ------------------------------------------------------------------ #
# Public entry point                                                  #
# ------------------------------------------------------------------ #

def aggregate(
    graph: CallGraph,
    level: RenderLevel,
    *,
    project_root: Optional[str] = None,
    modules: Optional[dict[str, ModuleDef]] = None,
    folder_depth: int = 2,
) -> CallGraph:
    """Collapse `graph` to the requested render level. Returns a new CallGraph."""
    if level == RenderLevel.FUNCTION:
        return graph

    fn_to_key, label_for_key = _key_extractor(
        graph,
        level,
        project_root=project_root,
        modules=modules,
        folder_depth=folder_depth,
    )

    return _build_aggregated_graph(graph, fn_to_key, label_for_key, level)


# ------------------------------------------------------------------ #
# Key extractors                                                      #
# ------------------------------------------------------------------ #

def _key_extractor(
    graph: CallGraph,
    level: RenderLevel,
    *,
    project_root: Optional[str],
    modules: Optional[dict[str, ModuleDef]],
    folder_depth: int,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (fn_node_id -> key, key -> display_label)."""
    fn_to_key: dict[str, str] = {}
    label_for_key: dict[str, str] = {}

    if level == RenderLevel.SCRIPT:
        for nid, fn in graph.functions.items():
            if fn.is_external or fn.file_path == "<external>":
                continue
            fn_to_key[nid] = fn.file_path
            label_for_key.setdefault(fn.file_path, Path(fn.file_path).name)

    elif level == RenderLevel.FOLDER:
        root = Path(project_root).resolve() if project_root else None
        for nid, fn in graph.functions.items():
            if fn.is_external or fn.file_path == "<external>":
                continue
            key = _folder_key(fn.file_path, root, folder_depth)
            fn_to_key[nid] = key
            label_for_key.setdefault(key, key)

    elif level == RenderLevel.MODULE:
        if not modules:
            return {}, {}
        file_to_module: dict[str, str] = {}
        for name, mod in modules.items():
            for f in mod.files:
                file_to_module[_norm(f)] = name
        for nid, fn in graph.functions.items():
            if fn.is_external or fn.file_path == "<external>":
                continue
            mod_name = file_to_module.get(_norm(fn.file_path))
            if not mod_name:
                continue
            fn_to_key[nid] = mod_name
            label_for_key.setdefault(mod_name, mod_name)

    elif level == RenderLevel.LIBRARY:
        # Use BuildInfo.project_files when available, else fall back to top folder.
        project_files: dict[str, str] = {}    # abs file path -> project name
        if graph.build_info is not None:
            for proj_name, files in graph.build_info.project_files.items():
                for f in files:
                    project_files[_norm(f)] = proj_name
        root = Path(project_root).resolve() if project_root else None
        for nid, fn in graph.functions.items():
            if fn.is_external or fn.file_path == "<external>":
                continue
            key = project_files.get(_norm(fn.file_path)) or _folder_key(fn.file_path, root, 1)
            fn_to_key[nid] = key
            label_for_key.setdefault(key, key)

    elif level == RenderLevel.NAMESPACE:
        for nid, fn in graph.functions.items():
            if fn.is_external or fn.file_path == "<external>":
                continue
            key = fn.parent or "<global>"
            fn_to_key[nid] = key
            label_for_key.setdefault(key, key)

    return fn_to_key, label_for_key


def _norm(p: str) -> str:
    try:
        return str(Path(p).resolve())
    except OSError:
        return str(Path(p))


def _folder_key(file_path: str, project_root: Optional[Path], depth: int) -> str:
    """Return the first `depth` path components under project_root (or absolute if root unknown)."""
    p = Path(file_path)
    if project_root is not None:
        try:
            rel = p.resolve().relative_to(project_root)
        except (ValueError, OSError):
            rel = p
    else:
        rel = p
    parts = list(rel.parts)
    if not parts:
        return str(rel)
    # If the path is a file, drop the filename — we want the folder.
    if rel.suffix:
        parts = parts[:-1]
    if not parts:
        return "<root>"
    return str(PurePath(*parts[: max(1, depth)]).as_posix())


# ------------------------------------------------------------------ #
# Aggregated graph builder                                            #
# ------------------------------------------------------------------ #

def _build_aggregated_graph(
    graph: CallGraph,
    fn_to_key: dict[str, str],
    label_for_key: dict[str, str],
    level: RenderLevel,
) -> CallGraph:
    keys = set(fn_to_key.values())
    new_functions: dict[str, FunctionDef] = {}
    id_for_key: dict[str, str] = {}    # key -> synthetic node_id

    # Track dominant language per aggregated node for colour consistency.
    lang_counter: dict[str, Counter] = defaultdict(Counter)
    files_per_key: dict[str, set[str]] = defaultdict(set)
    funcs_per_key: dict[str, list[str]] = defaultdict(list)
    # Drill-down hierarchy: per aggregated key, list of (file_path, [(fn_id, fn_label), ...])
    # consumed by the HTML Module View to render Module → File → Function.
    fns_per_file: dict[str, dict[str, list[tuple[str, str]]]] = defaultdict(lambda: defaultdict(list))

    for nid, key in fn_to_key.items():
        fn = graph.functions[nid]
        lang_counter[key][fn.language] += 1
        files_per_key[key].add(fn.file_path)
        funcs_per_key[key].append(fn.qualified_name or fn.name)
        fns_per_file[key][fn.file_path].append((nid, fn.qualified_name or fn.name))

    for key in sorted(keys):
        dom_lang = (
            lang_counter[key].most_common(1)[0][0]
            if lang_counter[key]
            else Language.C
        )
        # `file_path` is set to a synthetic marker so the computed `node_id`
        # is stable and unique across render levels. (FunctionDef.node_id =
        # safe(file_path) + qualified_name + line_start.) Using the actual
        # first underlying file caused the safe_path to vary and would have
        # collided with real function node_ids.
        synth_marker = f"<{level.value}>::{key}"
        synth = FunctionDef(
            name=label_for_key.get(key, key),
            qualified_name=synth_marker,
            language=dom_lang,
            file_path=synth_marker,
            line_start=0,
            line_end=0,
            parent=None,
            func_type=level.value,
        )
        # Stash member metadata on tracked_vars (compact and free dict slot).
        synth.tracked_vars["__members__"] = ",".join(sorted(files_per_key[key])[:50])
        synth.tracked_vars["__function_count__"] = str(len(funcs_per_key[key]))
        # Drill-down hierarchy for the HTML Module View. Encoded as JSON so the
        # JS side can parse it directly. Format:
        #   [{"file": "abs/path", "fns": [["fn_node_id", "label"], ...]}, ...]
        import json as _json
        synth.tracked_vars["__hierarchy__"] = _json.dumps([
            {"file": fp, "fns": fns_per_file[key][fp]}
            for fp in sorted(fns_per_file[key].keys())
        ], separators=(",", ":"))
        id_for_key[key] = synth.node_id
        new_functions[synth.node_id] = synth

    # Edges: aggregate by (src_key, dst_key) pair.
    edge_buckets: dict[tuple[str, str], list[CallRelationship]] = defaultdict(list)
    for call in graph.calls:
        if not call.is_resolved or call.callee_id is None:
            continue
        src_key = fn_to_key.get(call.caller_id)
        dst_key = fn_to_key.get(call.callee_id)
        if not src_key or not dst_key or src_key == dst_key:
            continue
        edge_buckets[(src_key, dst_key)].append(call)

    new_calls: list[CallRelationship] = []
    for (src_key, dst_key), underlying in edge_buckets.items():
        src_id = id_for_key[src_key]
        dst_id = id_for_key[dst_key]
        cats = Counter(c.confidence_category or "exact" for c in underlying)
        reason = (
            f"aggregated {len(underlying)} underlying calls "
            f"({', '.join(f'{n} {k}' for k, n in cats.most_common())})"
        )
        # Promote to "violation" if *any* underlying edge was marked as one
        # (e.g. by architecture.validate). Otherwise stay "aggregated" so the
        # UI styles it as a thick blue collapsed edge.
        category = "violation" if cats.get("violation", 0) else "aggregated"
        samples: list[tuple[str, int]] = []
        for c in underlying[:5]:
            samples.append((c.call_file, c.call_line))
        new_calls.append(CallRelationship(
            caller_id=src_id,
            callee_name=dst_id,
            call_file=underlying[0].call_file,
            call_line=underlying[0].call_line,
            callee_id=dst_id,
            is_resolved=True,
            resolution_confidence=ResolutionConfidence.EXACT,
            resolution_reason=reason,
            confidence_category=category,
            underlying_count=len(underlying),
            sample_call_sites=samples,
        ))

    out = CallGraph(
        total_files_parsed=len(new_functions),
        parse_errors=list(graph.parse_errors),
        build_info=graph.build_info,
        include_graph=graph.include_graph,
        modules=graph.modules,
        violations=graph.violations,
        render_level=level,
        project_root=graph.project_root,
    )
    for fn in new_functions.values():
        out.add_function(fn)
    for c in new_calls:
        out.add_call(c)

    return out
