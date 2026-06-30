"""
Core data models. Every other module depends on these — change carefully.

All new (post-v9.2) fields are optional with defaults, so existing callers and tests
continue to work unchanged. Never reorder or remove fields.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Optional


class Language(Enum):
    PYTHON = auto()
    C = auto()
    CPP = auto()
    MATLAB = auto()

    def display_name(self) -> str:
        return {
            Language.PYTHON: "Python",
            Language.C: "C",
            Language.CPP: "C++",
            Language.MATLAB: "MATLAB",
        }[self]

    def color(self) -> str:
        return {
            Language.PYTHON: "#4A90D9",
            Language.C:      "#E8832A",
            Language.CPP:    "#27AE60",
            Language.MATLAB: "#8E44AD",
        }[self]


@dataclass
class Parameter:
    name: str
    type_hint: Optional[str] = None
    is_dead: bool = False   # True if parameter is never referenced in the function body
    # Dead-variable engine (parallel to VariableDef's fields; set for unused params)
    dead_category: Optional[str] = None    # 'unused_param'
    dead_confidence: Optional[str] = None  # 'high' | 'medium' | 'low'
    read_lines: list[int] = field(default_factory=list)
    write_lines: list[int] = field(default_factory=list)
    is_suppressed: bool = False
    suppress_reason: Optional[str] = None

    def __str__(self) -> str:
        if self.type_hint:
            return f"{self.type_hint} {self.name}"
        return self.name


@dataclass
class VariableDef:
    """A variable visible from, or declared inside, a function."""

    name: str
    scope: str                 # UI kind: local/static/global/dynamic/field/environment/constant
    type_hint: Optional[str] = None
    value: Optional[str] = None
    line: int = 0
    file_path: Optional[str] = None
    context: Optional[str] = None
    source_kind: Optional[str] = None     # e.g. "hard-coded number", "function call"
    source_detail: Optional[str] = None   # e.g. "malloc", "os.getenv", "literal"
    is_dead: bool = False                 # True if declared but never used
    dead_reason: Optional[str] = None    # e.g. "declared but never used"
    connect_path: Optional[str] = None   # Full path from variable.connect("PATH/name", this)
    connect_input_name: Optional[str] = None  # Last segment of connect_path (input var name)
    custom_input_func: Optional[str] = None        # Function name matched by custom input template
    custom_input_classifier: Optional[str] = None  # Third argument (source type/classifier)
    parent_name: Optional[str] = None              # For source_kind="member_access": the parent
                                                   # object/struct expression (e.g. "var" in "var.x")
    assign_src: Optional[str] = None              # VFI-3: base var name of the RHS when this var is
                                                   # directly assigned from another identifier
                                                   # (e.g. "x" for `y = x;` or `y = fn(x);`)
    doc_comment: Optional[str] = None             # VF-10: adjacent intent comment (above or inline)
    full_source: Optional[str] = None             # Complete source statement line (e.g. "x = fn(a, b);")
                                                   # used for the block's SOURCE row + modal display
    # ── Dead-variable detection engine (read/write + scope liveness) ──────────
    dead_category: Optional[str] = None     # 'unused' | 'dead_store' | 'unused_param'
                                            # | 'dead_alloc' | 'unused_value'
    dead_confidence: Optional[str] = None   # 'high' | 'medium' | 'low'
    read_lines: list[int] = field(default_factory=list)   # evidence: lines that read the var
    write_lines: list[int] = field(default_factory=list)  # evidence: lines that write the var
    is_suppressed: bool = False             # True = intentional-unused, never flag (listed apart)
    suppress_reason: Optional[str] = None   # e.g. "(void) discard", "[[maybe_unused]]", "_-prefixed"


@dataclass
class FunctionDef:
    """A single function or method definition extracted from source code."""

    name: str
    qualified_name: str        # e.g. "MyClass::render" or "module.ClassName.method"
    language: Language
    file_path: str             # Absolute path
    line_start: int
    line_end: int
    parameters: list[Parameter] = field(default_factory=list)
    return_type: Optional[str] = None
    parent: Optional[str] = None   # Class name, namespace, or Python module
    is_external: bool = False      # Called but not defined in project
    is_method: bool = False
    is_virtual: bool = False        # C++ virtual/override method (set by parser + finalised by builder)
    func_type: Optional[str] = None   # "function"|"method"|"constructor"|"destructor"|"local function"|"nested function"|"async function"|"script"|"operator"
    docstring: Optional[str] = None
    tracked_vars: dict[str, str] = field(default_factory=dict)  # {var_name: value_repr}
    variables: list[VariableDef] = field(default_factory=list)

    @property
    def node_id(self) -> str:
        safe_path = re.sub(r"[^\w/\\.-]", "_", self.file_path)
        return f"{safe_path}::{self.qualified_name}::{self.line_start}"

    def signature(self, show_params: bool = True, show_return: bool = True) -> str:
        parts = []
        if show_return and self.return_type:
            parts.append(self.return_type)
        parts.append(self.name)
        if show_params:
            param_str = ", ".join(str(p) for p in self.parameters)
            parts[-1] = f"{parts[-1]}({param_str})"
        else:
            parts[-1] = f"{parts[-1]}(...)"
        return " ".join(parts) if show_return and self.return_type else parts[-1]


class ResolutionConfidence(str, Enum):
    EXACT = "EXACT"
    HEURISTIC = "HEURISTIC"
    UNRESOLVED = "UNRESOLVED"


@dataclass
class ClassInfo:
    """C++ class/struct hierarchy record collected by the parser.

    Aggregated across all files (a class may be declared in a header and defined
    in a .cpp), then consumed by the call-graph builder to resolve virtual /
    override method calls to every possible implementation (idea CPP-1).
    """
    name: str                                       # qualified name, e.g. "ns::Circle"
    bases: set = field(default_factory=set)         # base class names as written in source
    virtual_methods: set = field(default_factory=set)  # simple names declared virtual/override/final


class RenderLevel(str, Enum):
    """Abstraction level at which the graph is rendered."""
    FUNCTION  = "function"
    SCRIPT    = "script"      # one node per file
    FOLDER    = "folder"
    MODULE    = "module"
    LIBRARY   = "library"     # = .vcxproj / .sln project
    NAMESPACE = "namespace"   # namespace or class
    INCLUDE   = "include"     # used only for Include-Graph mode


# Visual confidence categories used by the HTML renderer for edge styling.
# Stored as plain strings on CallRelationship.confidence_category so they
# survive JSON round-trips without needing custom encoders.
CONFIDENCE_CATEGORIES = (
    "exact",
    "heuristic",
    "unresolved",
    "external",
    "aggregated",
    "violation",
)


@dataclass
class CallRelationship:
    """A directed edge: caller invokes callee at a specific source location."""

    caller_id: str
    callee_name: str                          # Raw name as written in source
    call_file: str
    call_line: int
    callee_id: Optional[str] = None           # Resolved FunctionDef.node_id
    call_args: list[str] = field(default_factory=list)
    is_resolved: bool = False
    resolution_confidence: ResolutionConfidence = ResolutionConfidence.UNRESOLVED
    # --- Extended metadata (post-v9.2) -----------------------------------
    resolution_reason: str = ""               # "exact qualified match", "same-file fallback", ...
    confidence_category: str = "unresolved"   # see CONFIDENCE_CATEGORIES
    underlying_count: int = 1                 # >1 only on aggregated edges
    sample_call_sites: list[tuple[str, int]] = field(default_factory=list)
    resolution_hint: str = ""                 # parser-set hint preserved in resolution_reason (post-v9.4)


# ---------------------------------------------------------------- #
# Build / compile-commands metadata                                 #
# ---------------------------------------------------------------- #

@dataclass
class CompileUnit:
    """One entry from compile_commands.json (per source file)."""
    source_file: str                                  # absolute, normalised
    directory: str                                    # working directory
    command: str = ""                                 # raw command string (if provided)
    arguments: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)   # -I / -isystem / /I
    defines: dict[str, Optional[str]] = field(default_factory=dict)
    extra_flags: list[str] = field(default_factory=list)


@dataclass
class BuildInfo:
    """Aggregate build metadata attached to the CallGraph."""
    source: str = "folder"
    # "folder" | "sln" | "compile_commands" | "compile_commands+sln"
    compile_commands_path: Optional[str] = None
    units: dict[str, CompileUnit] = field(default_factory=dict)   # key = abs source path
    configuration: Optional[str] = None
    platform: Optional[str] = None
    projects: list[str] = field(default_factory=list)             # .vcxproj names (in order)
    project_files: dict[str, list[str]] = field(default_factory=dict)  # project -> [abs paths]
    files_not_in_compile_commands: list[str] = field(default_factory=list)
    cc_files_not_found: list[str] = field(default_factory=list)
    global_defines: dict[str, Optional[str]] = field(default_factory=dict)
    global_includes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- #
# Architecture / modules                                            #
# ---------------------------------------------------------------- #

@dataclass
class MatlabProjectInfo:
    """G10: MATLAB project / search-path metadata (the MATLAB analogue of
    :class:`BuildInfo`).  Collected purely from the file system — ``+package`` and
    ``@class`` folders, ``.prj`` project files, and ``addpath``/``pathdef.m``
    search-path directives — without any parser changes.
    """
    source: str = "filesystem"          # "filesystem" | "prj" | "pathdef" | "mixed"
    project_files: list[str] = field(default_factory=list)    # discovered .prj paths
    project_name: Optional[str] = None                        # name from a .prj, if any
    packages: list[str] = field(default_factory=list)         # dotted names (+a/+b -> "a.b")
    class_folders: list[str] = field(default_factory=list)    # @Class folder class names
    addpath_dirs: list[str] = field(default_factory=list)     # dirs from addpath()/pathdef.m
    warnings: list[str] = field(default_factory=list)

@dataclass
class ModuleDef:
    """A logical module — group of source files inferred or user-defined."""
    name: str
    files: set[str] = field(default_factory=set)
    inferred_from: str = "folder"       # "folder" | "project" | "namespace" | "config"
    project: Optional[str] = None       # .vcxproj name (when inferred_from="project")


@dataclass
class ArchitectureRule:
    """A single rule in the architecture rule engine."""
    kind: str                           # "forbidden" | "allowed_only" | "required" | "layer"
    from_module: str = ""               # supports "*" wildcard
    to_module: str = ""                 # supports "*" wildcard
    reason: str = ""
    # For kind=="allowed_only": targets the caller may legally reach. Other targets are violations.
    allowed_targets: list[str] = field(default_factory=list)
    # For kind=="layer": ordered layer names; an edge from layers[i] to layers[j>=i] is allowed.
    layers: list[str] = field(default_factory=list)


@dataclass
class ArchitectureViolation:
    """A rule violation discovered during validate()."""
    rule_kind: str
    from_module: str
    to_module: str
    reason: str = ""
    sample_edges: list[tuple[str, str]] = field(default_factory=list)   # (caller_id, callee_id)


# ---------------------------------------------------------------- #
# Include graph                                                     #
# ---------------------------------------------------------------- #

@dataclass
class IncludeEdge:
    from_file: str
    to_file: str                        # resolved absolute path, or raw_target if unresolved
    is_system: bool = False             # <stdio.h> style
    resolved: bool = True
    raw_target: str = ""                # original text inside the quotes/angle brackets
    line: int = 0
    guard: str = ""                     # INC-1: controlling #if/#ifdef condition, "" = unconditional


@dataclass
class IncludeGraph:
    files: dict[str, list[IncludeEdge]] = field(default_factory=dict)
    unresolved: list[IncludeEdge] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)
    most_included: list[tuple[str, int]] = field(default_factory=list)
    # INC-1: includes dropped because a preprocessor guard evaluated to a
    # definite-false branch (e.g. the inactive side of an #ifdef whose macro is
    # known-defined, or an #if 0 block). Each edge's ``guard`` records why.
    excluded: list[IncludeEdge] = field(default_factory=list)


# ---------------------------------------------------------------- #
# CallGraph (extended)                                              #
# ---------------------------------------------------------------- #

@dataclass
class CallGraph:
    """Assembled graph: all functions (nodes) and all calls (edges)."""

    functions: dict[str, FunctionDef] = field(default_factory=dict)
    calls: list[CallRelationship] = field(default_factory=list)
    total_files_parsed: int = 0
    parse_errors: list[str] = field(default_factory=list)
    # --- Extended metadata (post-v9.2) -----------------------------------
    build_info: Optional[BuildInfo] = None
    include_graph: Optional[IncludeGraph] = None
    modules: dict[str, ModuleDef] = field(default_factory=dict)
    violations: list[ArchitectureViolation] = field(default_factory=list)
    render_level: RenderLevel = RenderLevel.FUNCTION
    project_root: Optional[str] = None      # absolute project root if known
    matlab_project: Optional[MatlabProjectInfo] = None   # G10: MATLAB path/project metadata

    def add_function(self, fn: FunctionDef) -> None:
        self.functions[fn.node_id] = fn

    def add_call(self, call: CallRelationship) -> None:
        self.calls.append(call)

    def stats(self) -> dict:
        resolved = sum(1 for c in self.calls if c.is_resolved)
        external = sum(1 for f in self.functions.values() if f.is_external)
        return {
            "functions": len(self.functions),
            "calls": len(self.calls),
            "resolved_calls": resolved,
            "external_functions": external,
            "files_parsed": self.total_files_parsed,
            "parse_errors": len(self.parse_errors),
        }
