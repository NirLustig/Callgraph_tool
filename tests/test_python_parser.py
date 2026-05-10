"""Tests for the Python AST parser."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from callgraph.config import Config
from callgraph.parsers.python_parser import PythonParser

SAMPLE_DIR = Path(__file__).parent / "example_project" / "python_sample"


@pytest.fixture
def parser():
    return PythonParser(Config())


def test_discovers_module_functions(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "utils.py")
    names = {f.name for f in fns}
    assert "tokenize" in names
    assert "parse" in names
    assert "_clean_tokens" in names
    assert "_build_result" in names


def test_discovers_class_methods(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "utils.py")
    methods = {f.name for f in fns if f.is_method}
    assert "render" in methods
    assert "_add_header" in methods
    assert "_flush" in methods


def test_function_parameters(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "utils.py")
    tokenize_fn = next(f for f in fns if f.name == "tokenize")
    assert len(tokenize_fn.parameters) == 1
    assert tokenize_fn.parameters[0].name == "text"
    assert tokenize_fn.parameters[0].type_hint == "str"


def test_return_type(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "utils.py")
    tokenize_fn = next(f for f in fns if f.name == "tokenize")
    assert tokenize_fn.return_type == "list[str]"


def test_parent_class(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "utils.py")
    render_fn = next(f for f in fns if f.name == "render" and f.is_method)
    assert render_fn.parent == "Renderer"


def test_docstring_extraction(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "utils.py")
    parse_fn = next(f for f in fns if f.name == "parse")
    assert parse_fn.docstring and "parse" in parse_fn.docstring.lower()


def test_call_detection(parser):
    _, calls = parser.parse_file(SAMPLE_DIR / "utils.py")
    callee_names = {c.callee_name for c in calls}
    assert "tokenize" in callee_names
    assert "_build_result" in callee_names


def test_call_has_line_number(parser):
    _, calls = parser.parse_file(SAMPLE_DIR / "utils.py")
    for call in calls:
        assert call.call_line > 0


def test_main_file(parser):
    fns, calls = parser.parse_file(SAMPLE_DIR / "main.py")
    names = {f.name for f in fns}
    assert "main" in names
    assert "process_input" in names
    callee_names = {c.callee_name for c in calls}
    assert "parse" in callee_names
    assert "process_input" in callee_names


def test_line_numbers(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "utils.py")
    tokenize_fn = next(f for f in fns if f.name == "tokenize")
    assert tokenize_fn.line_start > 0
    assert tokenize_fn.line_end >= tokenize_fn.line_start


def test_language_tag(parser):
    from callgraph.models import Language
    fns, _ = parser.parse_file(SAMPLE_DIR / "utils.py")
    for fn in fns:
        assert fn.language == Language.PYTHON


def test_qualified_name_method(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "utils.py")
    render_fn = next(f for f in fns if f.name == "render" and f.is_method)
    assert "Renderer" in render_fn.qualified_name


def test_variable_tracking(tmp_path):
    """Variable tracking records assignments for configured names."""
    from callgraph.config import Config
    cfg = Config()
    cfg.variables.track = True
    cfg.variables.names = ["mode", "result"]

    src = tmp_path / "tracked.py"
    src.write_text("""
def process(data):
    mode = "strict"
    result = {"ok": True}
    return result
""")
    p = PythonParser(cfg)
    fns, _ = p.parse_file(src)
    process_fn = next(f for f in fns if f.name == "process")
    assert "mode" in process_fn.tracked_vars
    assert "result" in process_fn.tracked_vars
