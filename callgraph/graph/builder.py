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
    ) -> tuple[str | None, ResolutionConfidence]:
        """
        Return (node_id, confidence) using multi-tier matching.
        """
        # Tier 1: exact qualified match
        if callee_name in self._by_qualified:
            return self._by_qualified[callee_name], ResolutionConfidence.EXACT

        # Tier 2: last segment of qualified match
        simple = callee_name.split("::")[-1].split(".")[-1]
        candidates = self._by_simple.get(simple, [])

        if not candidates:
            return None, ResolutionConfidence.UNRESOLVED

        if len(candidates) == 1:
            return candidates[0], ResolutionConfidence.HEURISTIC

        # Tier 3: prefer same file
        caller_dir = str(Path(caller_file).parent)
        same_file = [c for c in candidates if caller_file in c]
        if same_file:
            return same_file[0], ResolutionConfidence.HEURISTIC

        # Tier 4: prefer same directory
        same_dir = [c for c in candidates if caller_dir in c]
        if same_dir:
            return same_dir[0], ResolutionConfidence.HEURISTIC

        # Tier 5: any match — return first
        return candidates[0], ResolutionConfidence.HEURISTIC


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
        callee_id, confidence = index.resolve(call.callee_name, call.call_file)

        if callee_id:
            call.callee_id = callee_id
            call.is_resolved = True
            call.resolution_confidence = confidence
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
