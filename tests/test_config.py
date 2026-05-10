"""Tests for config loading."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from callgraph.config import Config, load_config


def test_default_config():
    cfg = load_config(None)
    assert cfg.display.show_parameters is True
    assert cfg.filter.show_external is False
    assert "html" in cfg.output.formats
    assert cfg.output.max_nodes == 300


def test_yaml_config(tmp_path):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("""
display:
  show_parameters: false
  show_line_numbers: true

filter:
  max_depth: 5
  entry_points:
    - main

output:
  formats:
    - html
    - svg
  layout: force
""")
    cfg = load_config(str(yaml_file))
    assert cfg.display.show_parameters is False
    assert cfg.display.show_line_numbers is True
    assert cfg.filter.max_depth == 5
    assert "main" in cfg.filter.entry_points
    assert "svg" in cfg.output.formats
    assert cfg.output.layout == "force"


def test_json_config(tmp_path):
    json_file = tmp_path / "config.json"
    json_file.write_text('{"display": {"show_return_types": false}}')
    cfg = load_config(str(json_file))
    assert cfg.display.show_return_types is False


def test_missing_config_raises():
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/path/config.yaml")


def test_invalid_extension_raises(tmp_path):
    bad_file = tmp_path / "config.toml"
    bad_file.write_text("[display]")
    with pytest.raises(ValueError):
        load_config(str(bad_file))


def test_partial_config_uses_defaults(tmp_path):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("display:\n  show_filenames: false\n")
    cfg = load_config(str(yaml_file))
    assert cfg.display.show_filenames is False
    # Other display fields should retain defaults
    assert cfg.display.show_parameters is True
    assert cfg.output.max_nodes == 300
