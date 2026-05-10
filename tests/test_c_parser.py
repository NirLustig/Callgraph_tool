"""Tests for the C Tree-sitter parser."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import tree_sitter_c  # noqa: F401
    TS_AVAILABLE = True
except ImportError:
    TS_AVAILABLE = False

pytestmark = pytest.mark.skipif(not TS_AVAILABLE, reason="tree-sitter-c not installed")

from callgraph.config import Config
from callgraph.models import Language
from callgraph.parsers.c_parser import CParser

SAMPLE_DIR = Path(__file__).parent / "example_project" / "c_sample"


@pytest.fixture
def parser():
    return CParser(Config())


def test_discovers_functions(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "processor.c")
    names = {f.name for f in fns}
    assert "process" in names
    assert "helper_c" in names
    assert "validate_input" in names
    assert "copy_buffer" in names


def test_function_parameters(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "processor.c")
    process_fn = next(f for f in fns if f.name == "process")
    param_names = {p.name for p in process_fn.parameters}
    assert "data" in param_names
    assert "out_buf" in param_names
    assert "buf_size" in param_names


def test_return_type(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "processor.c")
    process_fn = next(f for f in fns if f.name == "process")
    assert process_fn.return_type is not None
    assert "int" in process_fn.return_type


def test_call_detection(parser):
    _, calls = parser.parse_file(SAMPLE_DIR / "processor.c")
    callee_names = {c.callee_name for c in calls}
    assert "validate_input" in callee_names or "snprintf" in callee_names


def test_language_tag(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "processor.c")
    for fn in fns:
        assert fn.language == Language.C


def test_line_numbers(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "processor.c")
    for fn in fns:
        assert fn.line_start > 0
        assert fn.line_end >= fn.line_start


def test_main_file(parser):
    fns, calls = parser.parse_file(SAMPLE_DIR / "main.c")
    names = {f.name for f in fns}
    assert "main" in names
    assert "init" in names
    assert "run" in names
    callee_names = {c.callee_name for c in calls}
    assert "process" in callee_names or "helper_c" in callee_names


def test_header_file(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "processor.h")
    # headers may have declarations not definitions — just ensure no crash
    assert isinstance(fns, list)
