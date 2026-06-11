"""
Call graph assembly: deduplication, name resolution, filtering, NetworkX construction.
"""
from __future__ import annotations

from pathlib import Path

import networkx as nx

from ..config import Config
from ..models import CallGraph, CallRelationship, FunctionDef, Language, ResolutionConfidence
from .filters import (
    apply_node_cap,
    bfs_depth_filter,
    filter_external,
    filter_functions_by_file,
    filter_functions_by_name,
)


# ------------------------------------------------------------------ #
# Name resolution index                                               #
# ------------------------------------------------------------------ #

class _ResolutionIndex:
    """
    Multi-tier lookup index for resolving raw callee names to FunctionDef node_ids.
    """

    def __init__(self, functions: dict[str, FunctionDef]) -> None:
        # simple_name → [node_id, ...]
        self._by_simple: dict[str, list[str]] = {}
        # qualified_name → node_id (unique when qualified)
        self._by_qualified: dict[str, str] = {}

        for node_id, fn in functions.items():
            self._by_simple.setdefault(fn.name, []).append(node_id)
            self._by_qualified[fn.qualified_name] = node_id

    def resolve(
        self,
        callee_name: str,
        caller_file: str,
    ) -> tuple[str | None, ResolutionConfidence, str]:
        """
        Return (node_id, confidence, reason) using multi-tier matching.
        """
        # Tier 1: exact qualified match
        if callee_name in self._by_qualified:
            return (
                self._by_qualified[callee_name],
                ResolutionConfidence.EXACT,
                "exact qualified match",
            )

        # Tier 2: last segment of qualified match
        simple = callee_name.split("::")[-1].split(".")[-1]
        candidates = self._by_simple.get(simple, [])

        if not candidates:
            return None, ResolutionConfidence.UNRESOLVED, "no matching function in project"

        if len(candidates) == 1:
            return candidates[0], ResolutionConfidence.HEURISTIC, "unique name match"

        # Tier 3: prefer same file
        caller_dir = str(Path(caller_file).parent)
        same_file = [c for c in candidates if caller_file in c]
        if same_file:
            return same_file[0], ResolutionConfidence.HEURISTIC, "same-file fallback"

        # Tier 4: prefer same directory
        same_dir = [c for c in candidates if caller_dir in c]
        if same_dir:
            return same_dir[0], ResolutionConfidence.HEURISTIC, "same-directory fallback"

        # Tier 5: any match — return first
        return candidates[0], ResolutionConfidence.HEURISTIC, f"ambiguous ({len(candidates)} candidates)"


# ------------------------------------------------------------------ #
# Deduplication                                                        #
# ------------------------------------------------------------------ #

def _dedup_functions(raw: list[FunctionDef]) -> dict[str, FunctionDef]:
    """
    Deduplicate by node_id. On collision (e.g. same .h parsed twice),
    keep the one with more parameter information.
    """
    seen: dict[str, FunctionDef] = {}
    for fn in raw:
        existing = seen.get(fn.node_id)
        if existing is None:
            seen[fn.node_id] = fn
        elif len(fn.parameters) > len(existing.parameters):
            seen[fn.node_id] = fn
    return seen


# ------------------------------------------------------------------ #
# External stub creation                                              #
# ------------------------------------------------------------------ #

_STDLIB_PREFIXES = {
    "printf", "fprintf", "sprintf", "snprintf", "scanf", "malloc", "free",
    "calloc", "realloc", "memcpy", "memset", "memmove", "strlen", "strcpy",
    "strncpy", "strcmp", "strncmp", "strcat", "strtol", "strtod", "atoi",
    "exit", "abort", "assert", "fopen", "fclose", "fread", "fwrite",
    "std", "cout", "cin", "endl", "vector", "map", "set", "string",
    "make_shared", "make_unique", "move", "forward", "begin", "end",
    "print", "len", "range", "list", "dict", "set", "tuple", "type",
    "isinstance", "getattr", "setattr", "hasattr", "super",
    "open", "close", "read", "write", "append",
}


def _is_likely_stdlib(name: str) -> bool:
    base = name.split("::")[-1].split(".")[-1]
    return base in _STDLIB_PREFIXES or base.startswith("__")


def _make_external_stub(
    callee_name: str, lang: Language, call_file: str, call_line: int
) -> FunctionDef:
    from ..models import Parameter
    return FunctionDef(
        name=callee_name.split("::")[-1].split(".")[-1],
        qualified_name=callee_name,
        language=lang,
        file_path="<external>",
        line_start=0,
        line_end=0,
        is_external=True,
    )


# ------------------------------------------------------------------ #
# Main build function                                                  #
# ------------------------------------------------------------------ #

def build_call_graph(
    raw_functions: list[FunctionDef],
    raw_calls: list[CallRelationship],
    parse_errors: list[str],
    config: Config,
) -> CallGraph:
    graph = CallGraph(
        total_files_parsed=0,   # updated by caller if needed
        parse_errors=parse_errors,
    )

    # Step 1: dedup
    functions = _dedup_functions(raw_functions)

    # Step 2: build resolution index
    index = _ResolutionIndex(functions)

    # Step 3: resolve calls
    resolved_calls: list[CallRelationship] = []
    stubs: dict[str, FunctionDef] = {}

    for call in raw_calls:
        callee_id, confidence, reason = index.resolve(call.callee_name, call.call_file)

        if callee_id:
            call.callee_id = callee_id
            call.is_resolved = True
            call.resolution_confidence = confidence
            reason_full = reason + (f"; {call.resolution_hint}" if call.resolution_hint else "")
            call.resolution_reason = reason_full
            call.confidence_category = (
                "exact" if confidence == ResolutionConfidence.EXACT else "heuristic"
            )
        elif config.filter.show_external and not _is_likely_stdlib(call.callee_name):
            # Create external stub
            caller_fn = functions.get(call.caller_id)
            lang = caller_fn.language if caller_fn else Language.C
            stub = _make_external_stub(call.callee_name, lang, call.call_file, call.call_line)
            if stub.node_id not in stubs:
                stubs[stub.node_id] = stub
            call.callee_id = stub.node_id
            call.is_resolved = True
            call.resolution_confidence = ResolutionConfidence.UNRESOLVED
            call.resolution_reason = "external stub"
            call.confidence_category = "external"
        else:
            call.resolution_reason = reason
            call.confidence_category = "unresolved"

        resolved_calls.append(call)

    # Merge stubs into functions
    functions.update(stubs)

    # Step 4: apply filters
    functions = filter_functions_by_name(functions, config)
    functions = filter_functions_by_file(functions, config)

    # Remove calls whose caller was filtered out
    caller_ids = set(functions.keys())
    resolved_calls = [c for c in resolved_calls if c.caller_id in caller_ids]

    # Remove external if not wanted
    functions, resolved_calls = filter_external(
        CallGraph(functions=functions, calls=resolved_calls), config
    )

    # BFS depth filter
    if config.filter.entry_points or config.filter.max_depth is not None:
        functions, resolved_calls = bfs_depth_filter(
            functions, resolved_calls,
            config.filter.entry_points,
            config.filter.max_depth,
        )

    # Node cap
    functions, resolved_calls, _capped = apply_node_cap(
        functions, resolved_calls, config.output.max_nodes
    )

    # Step 5: populate CallGraph
    for fn in functions.values():
        graph.add_function(fn)
    for call in resolved_calls:
        graph.add_call(call)

    graph.total_files_parsed = len(set(
        fn.file_path for fn in functions.values() if fn.file_path != "<external>"
    ))

    return graph


def collapse_to_files(graph: CallGraph) -> CallGraph:
    """
    Derive a file-level summary graph: one synthetic FunctionDef per source file,
    plus one CallRelationship per (src_file, dst_file) pair that had at least one
    cross-file call in the original. Same-file calls are dropped.

    Used by --summary-by-file to make 15K-function .sln projects navigable —
    typically collapses ~hundreds of file-cards instead of 15K function nodes.
    Per-function variable_flow data is lost in the summary (the per-function modal
    is still useful via the existing Script Mode card → function pop-out).
    """
    from collections import Counter, defaultdict

    file_to_fns: dict[str, list[FunctionDef]] = defaultdict(list)
    for fn in graph.functions.values():
        if fn.is_external or fn.file_path == "<external>":
            continue
        file_to_fns[fn.file_path].append(fn)

    def _file_node_id(path: str) -> str:
        import re as _re
        safe = _re.sub(r"[^\w/\\.-]", "_", path)
        return f"{safe}::<file>::0"

    new_functions: dict[str, FunctionDef] = {}
    file_id: dict[str, str] = {}
    for fpath, fns in file_to_fns.items():
        from pathlib import Path as _P
        langs = [fn.language for fn in fns]
        dom_lang = Counter(langs).most_common(1)[0][0] if langs else Language.C
        nid = _file_node_id(fpath)
        file_id[fpath] = nid
        name = _P(fpath).name or fpath
        synth = FunctionDef(
            name=name,
            qualified_name=fpath,
            language=dom_lang,
            file_path=fpath,
            line_start=0,
            line_end=0,
        )
        # The synthetic node_id must equal `nid` we computed above; FunctionDef.node_id
        # is a property, so we mirror its derivation by giving it matching field values.
        # (FunctionDef.node_id uses file_path + qualified_name + line_start.)
        new_functions[nid] = synth

    # Build cross-file edges from the resolved calls.
    fn_to_file: dict[str, str] = {fn.node_id: fn.file_path for fn in graph.functions.values()
                                  if not fn.is_external and fn.file_path != "<external>"}
    seen_pairs: set[tuple[str, str]] = set()
    new_calls: list[CallRelationship] = []
    for call in graph.calls:
        if not call.is_resolved or call.callee_id is None:
            continue
        src_file = fn_to_file.get(call.caller_id)
        dst_file = fn_to_file.get(call.callee_id)
        if not src_file or not dst_file or src_file == dst_file:
            continue
        src_id = file_id[src_file]
        dst_id = file_id[dst_file]
        pair = (src_id, dst_id)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        new_calls.append(CallRelationship(
            caller_id=src_id,
            callee_name=dst_id,
            call_file=src_file,
            call_line=0,
            callee_id=dst_id,
            is_resolved=True,
            resolution_confidence=ResolutionConfidence.EXACT,
        ))

    out = CallGraph(
        total_files_parsed=len(new_functions),
        parse_errors=list(graph.parse_errors),
    )
    for fn in new_functions.values():
        out.add_function(fn)
    for c in new_calls:
        out.add_call(c)
    return out


def to_networkx(graph: CallGraph) -> nx.DiGraph:
    """Convert a CallGraph to a NetworkX directed graph for algorithm use."""
    G = nx.DiGraph()
    for node_id, fn in graph.functions.items():
        G.add_node(node_id, fn=fn)
    for call in graph.calls:
        if call.callee_id and call.callee_id in graph.functions:
            G.add_edge(
                call.caller_id,
                call.callee_id,
                call=call,
                dashed=(call.resolution_confidence == ResolutionConfidence.HEURISTIC),
            )
    return G
