"""
Configuration loading and validation.
Supports YAML and JSON config files; all settings have sensible defaults.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class DisplayConfig:
    show_parameters: bool = True
    show_return_types: bool = True
    show_filenames: bool = True
    show_line_numbers: bool = False
    show_classes: bool = True


@dataclass
class FilterConfig:
    exclude_dirs: list[str] = field(default_factory=lambda: [
        ".git", ".svn", "__pycache__", "node_modules", "vendor",
        ".venv", "venv", "env", "build", "dist", ".eggs",
    ])
    include_dirs: list[str] = field(default_factory=list)
    exclude_files: list[str] = field(default_factory=list)
    include_files: list[str] = field(default_factory=list)
    include_functions: list[str] = field(default_factory=list)   # fnmatch patterns
    exclude_functions: list[str] = field(default_factory=lambda: [
        "__init__", "__repr__", "__str__", "__del__",
    ])
    max_depth: Optional[int] = None
    entry_points: list[str] = field(default_factory=list)
    show_external: bool = False


@dataclass
class VariableConfig:
    track: bool = False
    names: list[str] = field(default_factory=list)


@dataclass
class OutputConfig:
    formats: list[str] = field(default_factory=lambda: ["html"])
    layout: str = "force"             # "force" (free drag) | "hierarchical"
    max_nodes: int = 3000
    node_size_scale: float = 1.0


@dataclass
class Config:
    display: DisplayConfig = field(default_factory=DisplayConfig)
    filter: FilterConfig = field(default_factory=FilterConfig)
    variables: VariableConfig = field(default_factory=VariableConfig)
    output: OutputConfig = field(default_factory=OutputConfig)


def _merge_dict(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _merge_dict(result[key], val)
        else:
            result[key] = val
    return result


def _dict_to_config(data: dict) -> Config:
    cfg = Config()

    if "display" in data:
        d = data["display"]
        cfg.display = DisplayConfig(
            show_parameters=d.get("show_parameters", cfg.display.show_parameters),
            show_return_types=d.get("show_return_types", cfg.display.show_return_types),
            show_filenames=d.get("show_filenames", cfg.display.show_filenames),
            show_line_numbers=d.get("show_line_numbers", cfg.display.show_line_numbers),
            show_classes=d.get("show_classes", cfg.display.show_classes),
        )

    if "filter" in data:
        f = data["filter"]
        cfg.filter = FilterConfig(
            exclude_dirs=f.get("exclude_dirs", cfg.filter.exclude_dirs),
            include_dirs=f.get("include_dirs", cfg.filter.include_dirs),
            exclude_files=f.get("exclude_files", cfg.filter.exclude_files),
            include_files=f.get("include_files", cfg.filter.include_files),
            include_functions=f.get("include_functions", cfg.filter.include_functions),
            exclude_functions=f.get("exclude_functions", cfg.filter.exclude_functions),
            max_depth=f.get("max_depth", cfg.filter.max_depth),
            entry_points=f.get("entry_points", cfg.filter.entry_points),
            show_external=f.get("show_external", cfg.filter.show_external),
        )

    if "variables" in data:
        v = data["variables"]
        cfg.variables = VariableConfig(
            track=v.get("track", cfg.variables.track),
            names=v.get("names", cfg.variables.names),
        )

    if "output" in data:
        o = data["output"]
        cfg.output = OutputConfig(
            formats=o.get("formats", cfg.output.formats),
            layout=o.get("layout", cfg.output.layout),
            max_nodes=o.get("max_nodes", cfg.output.max_nodes),
            node_size_scale=o.get("node_size_scale", cfg.output.node_size_scale),
        )

    return cfg


def load_config(path: Optional[str] = None) -> Config:
    """Load config from a YAML or JSON file. Returns defaults if path is None."""
    if path is None:
        return Config()

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(config_path, "r", encoding="utf-8") as fh:
        if config_path.suffix.lower() in (".yaml", ".yml"):
            data = yaml.safe_load(fh) or {}
        elif config_path.suffix.lower() == ".json":
            data = json.load(fh)
        else:
            raise ValueError(f"Unsupported config format: {config_path.suffix} (use .yaml or .json)")

    return _dict_to_config(data)
