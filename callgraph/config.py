"""
Configuration loading and validation.
Supports YAML and JSON config files; all settings have sensible defaults.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


_VALID_RENDER_LEVELS = ("function", "script", "folder", "module", "library", "namespace")


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
    parallel: Optional[int] = None    # parser worker threads; None = auto-detect
    summary_by_file: bool = False     # collapse 1-node-per-function to 1-node-per-file


@dataclass
class BuildConfig:
    """compile_commands.json + .sln configuration metadata."""
    compile_commands: Optional[str] = None     # explicit path; if None, auto-detect
    auto_detect: bool = True                   # search common locations when path is empty
    configuration: Optional[str] = None        # active .sln configuration (Debug / Release)
    platform: Optional[str] = None             # active .sln platform (x64 / Win32)


@dataclass
class SelectionConfig:
    """Restrict analysis to a subset of the project (used by GUI .sln picker + CLI flags)."""
    projects: list[str] = field(default_factory=list)   # .vcxproj names (substring)
    modules:  list[str] = field(default_factory=list)
    folders:  list[str] = field(default_factory=list)   # glob patterns relative to root
    files:    list[str] = field(default_factory=list)   # glob patterns relative to root
    languages: list[str] = field(default_factory=list)  # ["c","cpp","python","matlab"]


@dataclass
class RenderConfig:
    """Render-slot configuration for the HTML view."""
    view_slot_1: str = "function"
    view_slot_2: str = "script"
    render_level: str = "function"        # shorthand override for DOT/SVG (single image)
    folder_depth: int = 2                  # how many path components define a "folder"


@dataclass
class IncludeGraphConfig:
    enabled: bool = False
    follow_system: bool = False            # render <stdio.h>-style system includes


@dataclass
class ParserConfig:
    """Parser-level options shared across language parsers."""
    expand_macros: bool = True             # C/C++: expand simple #define macros before parsing (idea C-1)


@dataclass
class ArchitectureConfig:
    # module_name -> list of glob patterns relative to project root (first match wins)
    modules: dict[str, list[str]] = field(default_factory=dict)
    rules:   list[dict[str, Any]] = field(default_factory=list)
    report:  Optional[str] = None          # JSON violation report path (CLI --architecture-report)


@dataclass
class Config:
    display: DisplayConfig = field(default_factory=DisplayConfig)
    filter: FilterConfig = field(default_factory=FilterConfig)
    variables: VariableConfig = field(default_factory=VariableConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    build: BuildConfig = field(default_factory=BuildConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    include_graph: IncludeGraphConfig = field(default_factory=IncludeGraphConfig)
    architecture: ArchitectureConfig = field(default_factory=ArchitectureConfig)
    parser: ParserConfig = field(default_factory=ParserConfig)


def _merge_dict(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _merge_dict(result[key], val)
        else:
            result[key] = val
    return result


def _validate_render_level(value: str, where: str) -> str:
    v = (value or "function").lower()
    if v not in _VALID_RENDER_LEVELS:
        raise ValueError(
            f"Invalid {where} value: {value!r}. Expected one of: {', '.join(_VALID_RENDER_LEVELS)}"
        )
    return v


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
            parallel=o.get("parallel", cfg.output.parallel),
            summary_by_file=o.get("summary_by_file", cfg.output.summary_by_file),
        )

    if "build" in data:
        b = data["build"]
        cfg.build = BuildConfig(
            compile_commands=b.get("compile_commands", cfg.build.compile_commands),
            auto_detect=b.get("auto_detect", cfg.build.auto_detect),
            configuration=b.get("configuration", cfg.build.configuration),
            platform=b.get("platform", cfg.build.platform),
        )

    if "selection" in data:
        s = data["selection"]
        cfg.selection = SelectionConfig(
            projects=s.get("projects", cfg.selection.projects),
            modules=s.get("modules", cfg.selection.modules),
            folders=s.get("folders", cfg.selection.folders),
            files=s.get("files", cfg.selection.files),
            languages=s.get("languages", cfg.selection.languages),
        )

    if "render" in data:
        r = data["render"]
        cfg.render = RenderConfig(
            view_slot_1=_validate_render_level(
                r.get("view_slot_1", cfg.render.view_slot_1), "render.view_slot_1"
            ),
            view_slot_2=_validate_render_level(
                r.get("view_slot_2", cfg.render.view_slot_2), "render.view_slot_2"
            ),
            render_level=_validate_render_level(
                r.get("render_level", cfg.render.render_level), "render.render_level"
            ),
            folder_depth=int(r.get("folder_depth", cfg.render.folder_depth)),
        )

    if "include_graph" in data:
        ig = data["include_graph"]
        cfg.include_graph = IncludeGraphConfig(
            enabled=ig.get("enabled", cfg.include_graph.enabled),
            follow_system=ig.get("follow_system", cfg.include_graph.follow_system),
        )

    if "parser" in data:
        p = data["parser"]
        cfg.parser = ParserConfig(
            expand_macros=p.get("expand_macros", cfg.parser.expand_macros),
        )

    if "architecture" in data:
        a = data["architecture"]
        modules_raw = a.get("modules") or {}
        if not isinstance(modules_raw, dict):
            raise ValueError("architecture.modules must be a mapping of name -> [glob, ...]")
        normalised: dict[str, list[str]] = {}
        for name, patterns in modules_raw.items():
            if isinstance(patterns, str):
                normalised[name] = [patterns]
            else:
                normalised[name] = list(patterns or [])
        rules_raw = a.get("rules") or []
        if not isinstance(rules_raw, list):
            raise ValueError("architecture.rules must be a list of rule dicts")
        cfg.architecture = ArchitectureConfig(
            modules=normalised,
            rules=[dict(r) for r in rules_raw],
            report=a.get("report", cfg.architecture.report),
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
