"""Tests for the C++ Tree-sitter parser."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import tree_sitter_cpp  # noqa: F401
    TS_AVAILABLE = True
except ImportError:
    TS_AVAILABLE = False

pytestmark = pytest.mark.skipif(not TS_AVAILABLE, reason="tree-sitter-cpp not installed")

from callgraph.config import Config
from callgraph.models import Language
from callgraph.parsers.cpp_parser import CppParser

SAMPLE_DIR = Path(__file__).parent / "example_project" / "cpp_sample"


@pytest.fixture
def parser():
    return CppParser(Config())


def test_discovers_class_methods(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "renderer.cpp")
    names = {f.name for f in fns}
    assert "draw" in names
    assert "clear" in names
    assert "resize" in names
    assert "set_pixel" in names
    assert "get_pixel" in names


def test_is_method_flag(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "renderer.cpp")
    draw_fn = next(f for f in fns if f.name == "draw")
    assert draw_fn.is_method is True


def test_namespace_context(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "renderer.cpp")
    draw_fn = next(f for f in fns if f.name == "draw")
    assert draw_fn.parent is not None
    assert "graphics" in draw_fn.parent or "Renderer" in draw_fn.parent


def test_language_tag(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "renderer.cpp")
    for fn in fns:
        assert fn.language == Language.CPP


def test_call_detection(parser):
    _, calls = parser.parse_file(SAMPLE_DIR / "renderer.cpp")
    callee_names = {c.callee_name for c in calls}
    assert "clear" in callee_names or "set_pixel" in callee_names or "draw_rect" in callee_names


def test_line_numbers(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "renderer.cpp")
    for fn in fns:
        assert fn.line_start > 0
        assert fn.line_end >= fn.line_start


def test_main_file(parser):
    fns, calls = parser.parse_file(SAMPLE_DIR / "main.cpp")
    names = {f.name for f in fns}
    assert "main" in names
    assert "build_scene" in names
    assert "run_demo" in names


def test_destructor(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "renderer.cpp")
    names = {f.name for f in fns}
    # Destructor ~Renderer should be captured
    assert any("Renderer" in n or "~" in n for n in names)
