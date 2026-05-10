"""Tests for the MATLAB regex parser."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from callgraph.config import Config
from callgraph.models import Language
from callgraph.parsers.matlab_parser import MatlabParser

SAMPLE_DIR = Path(__file__).parent / "example_project" / "matlab_sample"


@pytest.fixture
def parser():
    return MatlabParser(Config())


def test_discovers_functions(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "signal_processor.m")
    names = {f.name for f in fns}
    assert "process_signal" in names
    assert "apply_filter" in names
    assert "normalize_signal" in names
    assert "extract_features" in names
    assert "compute_rms" in names
    assert "find_dominant_freq" in names


def test_function_parameters(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "signal_processor.m")
    ps_fn = next(f for f in fns if f.name == "process_signal")
    param_names = {p.name for p in ps_fn.parameters}
    assert "data" in param_names
    assert "fs" in param_names


def test_return_type(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "signal_processor.m")
    ps_fn = next(f for f in fns if f.name == "process_signal")
    assert ps_fn.return_type == "result"


def test_multi_output_return_type(parser):
    src = """
function [a, b, c] = multi_out(x)
    a = x + 1;
    b = x + 2;
    c = x + 3;
end
"""
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".m", delete=False, mode="w") as tmp:
        tmp.write(src)
        tmp_path = tmp.name
    try:
        fns, _ = parser.parse_file(Path(tmp_path))
        fn = next(f for f in fns if f.name == "multi_out")
        assert fn.return_type is not None
        assert "a" in fn.return_type and "b" in fn.return_type
    finally:
        os.unlink(tmp_path)


def test_language_tag(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "signal_processor.m")
    for fn in fns:
        assert fn.language == Language.MATLAB


def test_line_numbers(parser):
    fns, _ = parser.parse_file(SAMPLE_DIR / "signal_processor.m")
    for fn in fns:
        assert fn.line_start > 0
        assert fn.line_end >= fn.line_start


def test_call_detection(parser):
    _, calls = parser.parse_file(SAMPLE_DIR / "signal_processor.m")
    callee_names = {c.callee_name for c in calls}
    assert "apply_filter" in callee_names
    assert "normalize_signal" in callee_names
    assert "extract_features" in callee_names


def test_keywords_not_captured_as_calls(parser):
    _, calls = parser.parse_file(SAMPLE_DIR / "signal_processor.m")
    callee_names = {c.callee_name for c in calls}
    # MATLAB keywords should not appear as function calls
    for kw in ("if", "for", "while", "end", "function"):
        assert kw not in callee_names


def test_main_pipeline(parser):
    fns, calls = parser.parse_file(SAMPLE_DIR / "main_pipeline.m")
    names = {f.name for f in fns}
    assert "run_pipeline" in names
    assert "load_data" in names
    assert "build_summary" in names
    assert "save_results" in names
    assert "log_message" in names
    callee_names = {c.callee_name for c in calls}
    assert "process_signal" in callee_names
    assert "load_data" in callee_names


def test_call_line_numbers(parser):
    _, calls = parser.parse_file(SAMPLE_DIR / "signal_processor.m")
    for call in calls:
        assert call.call_line > 0


def test_comment_stripping(parser):
    """Functions inside comment blocks should not be detected."""
    import tempfile, os
    src = """
%{
function fake_func(x)
    result = x;
end
%}
function real_func(x)
    y = x + 1;
end
"""
    with tempfile.NamedTemporaryFile(suffix=".m", delete=False, mode="w") as tmp:
        tmp.write(src)
        tmp_path = tmp.name
    try:
        fns, _ = parser.parse_file(Path(tmp_path))
        names = {f.name for f in fns}
        assert "real_func" in names
        assert "fake_func" not in names
    finally:
        os.unlink(tmp_path)


def test_script_file_has_implicit_function(parser):
    """A .m file with no function keyword should produce a script-level node."""
    import tempfile, os
    src = "x = 5;\ny = x + 1;\ndisp(y);\n"
    with tempfile.NamedTemporaryFile(suffix=".m", delete=False, mode="w") as tmp:
        tmp.write(src)
        tmp_path = tmp.name
    try:
        fns, _ = parser.parse_file(Path(tmp_path))
        assert len(fns) == 1
        assert fns[0].name == Path(tmp_path).stem
    finally:
        os.unlink(tmp_path)


def test_variable_tracking():
    import tempfile, os
    cfg = Config()
    cfg.variables.track = True
    cfg.variables.names = ["cutoff", "rng"]
    src = """
function out = myfunc(data, fs)
    cutoff = fs / 4;
    rng = max(data) - min(data);
    out = data / rng;
end
"""
    with tempfile.NamedTemporaryFile(suffix=".m", delete=False, mode="w") as tmp:
        tmp.write(src)
        tmp_path = tmp.name
    try:
        p = MatlabParser(cfg)
        fns, _ = p.parse_file(Path(tmp_path))
        fn = next(f for f in fns if f.name == "myfunc")
        assert "cutoff" in fn.tracked_vars
        assert "rng" in fn.tracked_vars
    finally:
        os.unlink(tmp_path)
