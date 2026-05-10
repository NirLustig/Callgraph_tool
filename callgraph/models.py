"""
Core data models. Every other module depends on these — change carefully.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, auto
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


@dataclass
class CallGraph:
    """Assembled graph: all functions (nodes) and all calls (edges)."""

    functions: dict[str, FunctionDef] = field(default_factory=dict)
    calls: list[CallRelationship] = field(default_factory=list)
    total_files_parsed: int = 0
    parse_errors: list[str] = field(default_factory=list)

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
