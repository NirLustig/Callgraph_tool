"""
MATLAB source parser (.m files) using regex-based line-by-line analysis.

Handles:
  - Script files (no function keyword at top)
  - Function files: function [out1,out2] = name(arg1, arg2)
  - Multiple local functions in one file
  - Classdef files (class name extracted; methods detected)
  - Function calls: name(...) patterns
  - Line and block comments stripped before analysis
  - Docstrings: first consecutive comment block after function definition

Limitations:
  - Cannot resolve MATLAB's array-indexing ambiguity: a(i) vs func(i).
    Calls are captured by name and resolved later by the graph builder.
  - No type information (MATLAB is dynamically typed).
  - Anonymous functions (@(x) ...) are noted but not deeply analyzed.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from ..config import Config
from ..models import CallRelationship, FunctionDef, Language, Parameter, ResolutionConfidence, VariableDef
from .base import BaseParser

# ------------------------------------------------------------------ #
# Regex patterns (all single-line, no re.MULTILINE)                  #
# ------------------------------------------------------------------ #

_FUNC_LINE_RE = re.compile(
    r'^[ \t]*function[ \t]+'
    r'(?:'
        r'(?:\[([^\]]*)\]|([A-Za-z_]\w*))[ \t]*=[ \t]*'  # outputs
    r')?'
    r'([A-Za-z_]\w*)'                                      # function name
    r'(?:[ \t]*\(([^)]*)\))?'                              # optional params
    ,
    re.IGNORECASE,
)

_CLASS_LINE_RE = re.compile(
    r'^[ \t]*classdef[ \t]+([A-Za-z_]\w*)',
    re.IGNORECASE,
)

_CALL_RE = re.compile(r'\b([A-Za-z_]\w*)\s*\(')

_KEYWORDS = frozenset({
    'if', 'elseif', 'else', 'end', 'for', 'while', 'switch', 'case',
    'otherwise', 'break', 'continue', 'return', 'try', 'catch', 'rethrow',
    'function', 'classdef', 'properties', 'methods', 'events', 'enumeration',
    'global', 'persistent', 'import', 'parfor', 'spmd',
})


def _strip_comments(source: str) -> str:
    """Return source with comments replaced by blank lines (preserving line count)."""
    lines = source.splitlines()
    cleaned: list[str] = []
    in_block = False
    for line in lines:
        if in_block:
            if line.strip() == '%}':
                in_block = False
            cleaned.append('')
            continue
        if line.strip().startswith('%{'):
            in_block = True
            cleaned.append('')
            continue
        pct_pos = _find_comment_start(line)
        cleaned.append(line[:pct_pos] if pct_pos >= 0 else line)
    return '\n'.join(cleaned)


def _find_comment_start(line: str) -> int:
    """Position of the first % not inside a string literal. Returns -1 if none."""
    in_str = False
    str_char = ''
    for i, ch in enumerate(line):
        if in_str:
            if ch == str_char:
                in_str = False
        else:
            if ch in ('"', "'"):
                in_str = True
                str_char = ch
            elif ch == '%':
                return i
    return -1


def _extract_docstring(raw_lines: list[str], func_start_0idx: int) -> Optional[str]:
    """
    Extract MATLAB docstring: the first consecutive block of % comment lines
    immediately following the function definition line.
    Blank lines are skipped up to the first comment; once comments start,
    any non-comment non-blank line ends the docstring.
    """
    doc_parts: list[str] = []
    found_first = False
    for j in range(func_start_0idx + 1, min(func_start_0idx + 40, len(raw_lines))):
        stripped = raw_lines[j].strip()
        if stripped.startswith('%'):
            doc_parts.append(stripped.lstrip('%').strip())
            found_first = True
        elif stripped == '' and not found_first:
            continue        # blank line before docstring — keep looking
        elif stripped == '' and found_first:
            break            # blank line after docstring started — stop
        else:
            break            # code line — stop
    return '\n'.join(doc_parts) if doc_parts else None


def _parse_params(params_str: Optional[str]) -> list[Parameter]:
    if not params_str:
        return []
    params = []
    for part in params_str.split(','):
        name = part.strip().lstrip('~')
        if name and re.match(r'^[A-Za-z_]\w*$', name):
            params.append(Parameter(name=name, type_hint=None))
    return params


def _parse_return_type(out1: Optional[str], out2: Optional[str]) -> Optional[str]:
    if out1 is not None:
        parts = [p.strip() for p in out1.split(',') if p.strip()]
        return '[' + ', '.join(parts) + ']' if parts else None
    if out2 is not None:
        return out2.strip() or None
    return None


class MatlabParser(BaseParser):
    def parse_file(
        self, path: Path
    ) -> tuple[list[FunctionDef], list[CallRelationship]]:
        source = path.read_text(encoding="utf-8", errors="replace")
        raw_lines = source.splitlines()          # original lines for docstring extraction
        cleaned = _strip_comments(source)
        cleaned_lines = cleaned.splitlines()

        class_name: Optional[str] = None
        for line in cleaned_lines:
            cm = _CLASS_LINE_RE.match(line)
            if cm:
                class_name = cm.group(1)
                break

        functions = self._extract_functions(cleaned_lines, raw_lines, str(path), class_name)
        calls = self._extract_calls(cleaned_lines, str(path), functions)
        self._extract_variables(cleaned_lines, str(path), functions)

        if self.config.variables.track and self.config.variables.names:
            self._track_variables(cleaned_lines, functions)

        return functions, calls

    # ---------------------------------------------------------------- #

    def _extract_functions(
        self,
        lines: list[str],
        raw_lines: list[str],
        file_path: str,
        class_name: Optional[str],
    ) -> list[FunctionDef]:
        func_positions: list[tuple[int, re.Match]] = []
        for i, line in enumerate(lines):
            m = _FUNC_LINE_RE.match(line)
            if m:
                func_positions.append((i + 1, m))  # 1-indexed line number

        if not func_positions:
            # Script file — create a single implicit node
            stem = Path(file_path).stem
            return [FunctionDef(
                name=stem, qualified_name=stem,
                language=Language.MATLAB, file_path=file_path,
                line_start=1, line_end=len(lines),
                parameters=[], return_type=None,
                parent=None, is_external=False, is_method=False,
                func_type="script",
            )]

        functions: list[FunctionDef] = []
        n_lines = len(lines)

        for j, (line_num, m) in enumerate(func_positions):
            end_line = func_positions[j + 1][0] - 1 if j + 1 < len(func_positions) else n_lines

            out1 = m.group(1)
            out2 = m.group(2)
            name = m.group(3)
            params_raw = m.group(4)

            return_type = _parse_return_type(out1, out2)
            params = _parse_params(params_raw)
            docstring = _extract_docstring(raw_lines, line_num - 1)  # 0-indexed

            if class_name:
                func_type = "method"
            elif j == 0:
                func_type = "function"
            else:
                func_type = "local function"

            parent = class_name
            qualified = f"{class_name}.{name}" if class_name else name

            functions.append(FunctionDef(
                name=name,
                qualified_name=qualified,
                language=Language.MATLAB,
                file_path=file_path,
                line_start=line_num,
                line_end=end_line,
                parameters=params,
                return_type=return_type,
                parent=parent,
                is_external=False,
                is_method=bool(class_name),
                func_type=func_type,
                docstring=docstring,
            ))

        return functions

    # ---------------------------------------------------------------- #

    def _extract_calls(
        self,
        lines: list[str],
        file_path: str,
        functions: list[FunctionDef],
    ) -> list[CallRelationship]:
        if not functions:
            return []

        fn_ranges = [(fn.line_start, fn.line_end, fn) for fn in functions]
        calls: list[CallRelationship] = []

        for i, line in enumerate(lines):
            line_num = i + 1
            caller = self._find_owning_function(line_num, fn_ranges)
            if caller is None:
                continue
            for m in _CALL_RE.finditer(line):
                callee = m.group(1)
                if callee in _KEYWORDS:
                    continue
                calls.append(CallRelationship(
                    caller_id=caller.node_id,
                    callee_name=callee,
                    call_file=file_path,
                    call_line=line_num,
                    resolution_confidence=ResolutionConfidence.UNRESOLVED,
                ))

        return calls

    def _find_owning_function(
        self,
        line: int,
        ranges: list[tuple[int, int, FunctionDef]],
    ) -> Optional[FunctionDef]:
        """Return the innermost (latest-starting) function that contains this line."""
        best: Optional[FunctionDef] = None
        best_start = -1
        for start, end, fn in ranges:
            if start <= line <= end and start > best_start:
                best = fn
                best_start = start
        return best

    def _track_variables(
        self,
        lines: list[str],
        functions: list[FunctionDef],
    ) -> None:
        target_names = set(self.config.variables.names)
        assign_re = re.compile(
            r'^[ \t]*(' + '|'.join(re.escape(n) for n in target_names) + r')[ \t]*=[ \t]*(.+?)[ \t]*;?$',
        )
        fn_ranges = [(fn.line_start, fn.line_end, fn) for fn in functions]
        for i, line in enumerate(lines, start=1):
            m = assign_re.match(line)
            if not m:
                continue
            var_name = m.group(1)
            var_value = m.group(2).strip().rstrip(';').strip()
            owner = self._find_owning_function(i, fn_ranges)
            if owner:
                owner.tracked_vars[var_name] = var_value[:80]

    def _extract_variables(
        self,
        lines: list[str],
        file_path: str,
        functions: list[FunctionDef],
    ) -> None:
        fn_ranges = [(fn.line_start, fn.line_end, fn) for fn in functions]
        assign_re = re.compile(
            r'^[ \t]*(?:\[([^\]]+)\]|([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?))[ \t]*=[ \t]*(.+?)[ \t]*;?$'
        )
        global_re = re.compile(r'^[ \t]*global[ \t]+(.+)$', re.IGNORECASE)
        persistent_re = re.compile(r'^[ \t]*persistent[ \t]+(.+)$', re.IGNORECASE)

        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.lower().startswith("function "):
                continue

            owner = self._find_owning_function(i, fn_ranges)
            if owner is None:
                continue

            global_match = global_re.match(line)
            if global_match:
                for name in _split_matlab_names(global_match.group(1)):
                    owner.variables.append(VariableDef(
                        name=name,
                        scope="global",
                        line=i,
                        file_path=file_path,
                        context=owner.qualified_name,
                        source_kind="global declaration",
                        source_detail="global",
                    ))
                continue

            persistent_match = persistent_re.match(line)
            if persistent_match:
                for name in _split_matlab_names(persistent_match.group(1)):
                    owner.variables.append(VariableDef(
                        name=name,
                        scope="static",
                        line=i,
                        file_path=file_path,
                        context=owner.qualified_name,
                        source_kind="persistent declaration",
                        source_detail="persistent",
                    ))
                continue

            assign_match = assign_re.match(line)
            if not assign_match:
                continue
            lhs_multi, lhs_single, rhs = assign_match.groups()
            names = _split_matlab_names(lhs_multi) if lhs_multi else [lhs_single]
            for name in names:
                if name and name not in _KEYWORDS:
                    rhs_clean = rhs.strip().rstrip(";").strip()
                    source_kind, source_detail, inferred_type, dynamic = _classify_matlab_value(rhs_clean)
                    kind = "field" if "." in name else "local"
                    if source_kind == "environment lookup":
                        kind = "environment"
                    elif dynamic:
                        kind = "dynamic"
                    elif _is_matlab_constant_name(name) and source_kind and source_kind.startswith("hard-coded"):
                        kind = "constant"
                    owner.variables.append(VariableDef(
                        name=name,
                        scope=kind,
                        type_hint=inferred_type,
                        value=rhs_clean[:120],
                        line=i,
                        file_path=file_path,
                        context=owner.qualified_name,
                        source_kind=source_kind,
                        source_detail=source_detail,
                    ))

        for fn in functions:
            fn.variables = _dedup_variables(fn.variables)


def _split_matlab_names(raw: str) -> list[str]:
    names: list[str] = []
    for part in re.split(r'[\s,]+', raw.strip()):
        name = part.strip().lstrip("~")
        if re.match(r'^[A-Za-z_]\w*$', name):
            names.append(name)
    return names


def _is_matlab_constant_name(name: str) -> bool:
    base = name.split(".")[-1]
    letters = [ch for ch in base if ch.isalpha()]
    return bool(letters) and base.upper() == base


def _classify_matlab_value(value: str) -> tuple[Optional[str], Optional[str], Optional[str], bool]:
    text = value.strip()
    lower = text.lower()

    if re.fullmatch(r'[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?', text):
        return "hard-coded number", "literal", "double", False
    if (text.startswith("'") and text.endswith("'")) or (text.startswith('"') and text.endswith('"')):
        return "hard-coded string", "literal", "char", False
    if lower in {"true", "false"}:
        return "hard-coded boolean", "literal", "logical", False
    if lower.startswith("getenv("):
        return "environment lookup", "getenv", "char", False
    call_match = re.match(r'^([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*\(', text)
    if call_match:
        callee = call_match.group(1)
        if callee in {"zeros", "ones", "cell", "struct", "containers.Map", "table", "string"}:
            return "dynamic allocation", callee, None, True
        return "function call", callee, None, False
    if any(op in text for op in ["+", "-", "*", "/", "^"]):
        return "calculated expression", None, None, False
    return "expression", None, None, False


def _dedup_variables(variables: list[VariableDef]) -> list[VariableDef]:
    seen: set[tuple[str, str, int, Optional[str]]] = set()
    result: list[VariableDef] = []
    for var in variables:
        key = (var.scope, var.name, var.line, var.context)
        if key in seen:
            continue
        seen.add(key)
        result.append(var)
    return result
