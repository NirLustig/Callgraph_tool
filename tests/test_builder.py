"""Tests for the call graph builder and filters."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from callgraph.config import Config
from callgraph.graph.builder import build_call_graph, to_networkx
from callgraph.models import (
    CallRelationship,
    FunctionDef,
    Language,
    Parameter,
    ResolutionConfidence,
)


def _make_fn(name: str, file: str = "a.py", line: int = 1, lang=Language.PYTHON) -> FunctionDef:
    return FunctionDef(
        name=name,
        qualified_name=name,
        language=lang,
        file_path=file,
        line_start=line,
        line_end=line + 5,
    )


def _make_call(caller: FunctionDef, callee_name: str, line: int = 10) -> CallRelationship:
    return CallRelationship(
        caller_id=caller.node_id,
        callee_name=callee_name,
        call_file=caller.file_path,
        call_line=line,
    )


# ── Basic assembly ─────────────────────────────────────────────────

def test_build_basic_graph():
    fn_main = _make_fn("main", line=1)
    fn_parse = _make_fn("parse", line=10)
    call = _make_call(fn_main, "parse")

    cfg = Config()
    cfg.filter.exclude_functions = []
    graph = build_call_graph([fn_main, fn_parse], [call], [], cfg)

    assert "main" in {f.name for f in graph.functions.values()}
    assert "parse" in {f.name for f in graph.functions.values()}


def test_resolution_exact_match():
    fn_a = _make_fn("foo", file="a.py", line=1)
    fn_b = _make_fn("foo", file="b.py", line=1)
    caller = _make_fn("bar", file="a.py", line=20)
    call = _make_call(caller, "foo")

    cfg = Config()
    cfg.filter.exclude_functions = []
    graph = build_call_graph([fn_a, fn_b, caller], [call], [], cfg)

    resolved = [c for c in graph.calls if c.is_resolved]
    assert len(resolved) >= 1


def test_deduplication():
    fn1 = _make_fn("parse", file="a.py", line=5)
    fn2 = _make_fn("parse", file="a.py", line=5)  # exact duplicate
    cfg = Config()
    cfg.filter.exclude_functions = []
    graph = build_call_graph([fn1, fn2], [], [], cfg)
    parse_fns = [f for f in graph.functions.values() if f.name == "parse"]
    assert len(parse_fns) == 1


def test_networkx_conversion():
    fn_main = _make_fn("main", line=1)
    fn_parse = _make_fn("parse", line=10)
    call = _make_call(fn_main, "parse")

    cfg = Config()
    cfg.filter.exclude_functions = []
    graph = build_call_graph([fn_main, fn_parse], [call], [], cfg)
    G = to_networkx(graph)
    assert len(G.nodes) == len(graph.functions)


# ── Filters ─────────────────────────────────────────────────────

def test_exclude_function_filter():
    fn_main = _make_fn("main", line=1)
    fn_helper = _make_fn("helper", line=10)

    cfg = Config()
    cfg.filter.exclude_functions = ["helper"]
    graph = build_call_graph([fn_main, fn_helper], [], [], cfg)
    names = {f.name for f in graph.functions.values()}
    assert "helper" not in names
    assert "main" in names


def test_include_function_filter():
    fns = [_make_fn("parse"), _make_fn("render"), _make_fn("_internal")]
    cfg = Config()
    cfg.filter.include_functions = ["parse", "render"]
    cfg.filter.exclude_functions = []
    graph = build_call_graph(fns, [], [], cfg)
    names = {f.name for f in graph.functions.values()}
    assert "_internal" not in names
    assert "parse" in names


def test_depth_filter():
    fn_a = _make_fn("a", line=1)
    fn_b = _make_fn("b", line=10)
    fn_c = _make_fn("c", line=20)
    call_ab = _make_call(fn_a, "b")
    call_bc = _make_call(fn_b, "c")

    cfg = Config()
    cfg.filter.entry_points = ["a"]
    cfg.filter.max_depth = 1
    cfg.filter.exclude_functions = []
    graph = build_call_graph([fn_a, fn_b, fn_c], [call_ab, call_bc], [], cfg)
    names = {f.name for f in graph.functions.values()}
    assert "a" in names
    assert "b" in names
    # c is at depth 2, should be excluded
    assert "c" not in names


def test_external_hidden_by_default():
    caller = _make_fn("main")
    cfg = Config()
    cfg.filter.show_external = False
    cfg.filter.exclude_functions = []
    call = _make_call(caller, "printf")  # unresolved → would become stub
    graph = build_call_graph([caller], [call], [], cfg)
    external = [f for f in graph.functions.values() if f.is_external]
    assert len(external) == 0


def test_external_shown_when_enabled():
    caller = _make_fn("main")
    cfg = Config()
    cfg.filter.show_external = True
    cfg.filter.exclude_functions = []
    # Use a name that is not in the stdlib list
    call = _make_call(caller, "custom_lib_func")
    graph = build_call_graph([caller], [call], [], cfg)
    external = [f for f in graph.functions.values() if f.is_external]
    assert len(external) >= 1


def test_node_cap():
    from callgraph.graph.filters import apply_node_cap
    fns = {f"id_{i}": _make_fn(f"fn_{i}") for i in range(20)}
    calls = []
    kept, _, capped = apply_node_cap(fns, calls, max_nodes=10)
    assert capped is True
    assert len(kept) == 10
