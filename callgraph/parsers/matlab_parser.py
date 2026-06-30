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

G1 — Call-vs-index disambiguation (two-pass):
  `parse_files` overrides the base implementation to run a second pass over all
  extracted calls once the full project function-name set is known.  A call is
  dropped when the callee appears **only** as a local/field variable in the calling
  function and is **not** a project-defined function (most likely array indexing).
  Calls whose callee matches a project function are upgraded to HEURISTIC confidence.

G2 — Variable scope awareness:
  `detect_dead_variables_matlab` (in _liveness.py) now excludes lines that belong to
  nested inner functions when computing read/write evidence for the outer function.
  For flat (non-nested) functions without a dynamic-escape call the dead-variable
  confidence is upgraded from ``low`` to ``medium``.

Limitations:
  - No type information (MATLAB is dynamically typed).
  - Anonymous functions (@(x) ...) are noted but not deeply analyzed.
  - Confidence for MATLAB dead variables is at most ``medium`` (regex, not AST).
"""
from __future__ import annotations

import dataclasses
import re
import threading
from pathlib import Path
from typing import Any, Optional

from ..config import Config
from ..models import CallRelationship, ClassInfo, FunctionDef, Language, Parameter, ResolutionConfidence, VariableDef
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
    r'^[ \t]*classdef\b[ \t]*'
    r'(?:\([^)]*\)[ \t]*)?'                       # optional (Attributes)
    r'([A-Za-z_]\w*)'                              # 1: class name
    r'(?:[ \t]*<[ \t]*(.+?))?'                     # 2: optional superclass list
    r'[ \t]*$',
    re.IGNORECASE,
)

# Block keywords inside a classdef that open an `end`-terminated region.
_ML_OPENERS = frozenset({
    'function', 'if', 'for', 'parfor', 'while', 'switch', 'try', 'spmd',
    'properties', 'methods', 'events', 'enumeration', 'classdef',
})
_ML_OPENER_RE = re.compile(r'^[ \t]*([A-Za-z_]\w*)\b')
_ML_END_RE = re.compile(r'^[ \t]*end[ \t]*;?[ \t]*$')

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


def _ml_inline_comment(raw_line: str) -> Optional[str]:
    """G9: text of an inline ``%`` comment on the same line (None if absent)."""
    pos = _find_comment_start(raw_line)
    if pos < 0:
        return None
    text = raw_line[pos + 1:].strip().lstrip('%').strip()
    return text or None


def _ml_comment_above(raw_lines: list[str], idx0: int) -> Optional[str]:
    """G9: contiguous ``%`` comment block immediately above line ``idx0`` (0-indexed).

    Mirrors the VF-10 C/C++ logic: a single blank-line gap between the comment and
    the declaration is tolerated; ``%{``/``%}`` block delimiters are not harvested.
    """
    j = idx0 - 1
    if j >= 0 and raw_lines[j].strip() == '':
        j -= 1            # tolerate one blank line between comment and declaration
    parts: list[str] = []
    while j >= 0:
        s = raw_lines[j].strip()
        if s.startswith('%') and not s.startswith('%{') and s != '%}':
            body = s.lstrip('%').strip()
            if body:
                parts.append(body)
            j -= 1
        else:
            break
    if not parts:
        return None
    parts.reverse()
    return ' '.join(parts)


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


@dataclasses.dataclass
class _ClassdefInfo:
    """G7: parsed structure of a MATLAB ``classdef`` file."""
    name: str
    bases: list[str] = dataclasses.field(default_factory=list)
    methods: list[tuple[int, int]] = dataclasses.field(default_factory=list)      # (start,end) 1-indexed
    properties: list[tuple[int, int]] = dataclasses.field(default_factory=list)
    events: list[tuple[int, int]] = dataclasses.field(default_factory=list)


def _parse_classdef_blocks(lines: list[str]) -> dict[str, list[tuple[int, int]]]:
    """G7: locate ``methods`` / ``properties`` / ``events`` block ranges.

    Tracks MATLAB ``end`` matching with a keyword stack so that the matching
    ``end`` for each block is found even though method bodies contain their own
    ``if``/``for``/``function`` blocks.  Ranges are 1-indexed and inclusive of the
    opening keyword line and its closing ``end``.
    """
    blocks: dict[str, list[tuple[int, int]]] = {
        'methods': [], 'properties': [], 'events': [],
    }
    stack: list[tuple[str, int]] = []
    for idx, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if _ML_END_RE.match(s):
            if stack:
                kw, start = stack.pop()
                if kw in blocks:
                    blocks[kw].append((start + 1, idx + 1))
            continue
        m = _ML_OPENER_RE.match(line)
        if not m:
            continue
        kw = m.group(1).lower()
        if kw not in _ML_OPENERS:
            continue
        # For section keywords, disambiguate block-vs-expression: `methods = x`
        # (assignment) and `methods(obj)` (call) are NOT section blocks, whereas
        # `methods` / `methods (Static)` are. Control-flow openers always push so
        # the `end` stack stays balanced (e.g. `if(x)` is still a block).
        if kw in ('properties', 'methods', 'events', 'enumeration'):
            nxt = s[len(m.group(1)):len(m.group(1)) + 1]
            if nxt in ('=', '('):
                continue
        stack.append((kw, idx))
    return blocks


def _block_member_names(
    lines: list[str], ranges: list[tuple[int, int]]
) -> list[tuple[str, int]]:
    """G7: first identifier on each declaration line inside the given blocks."""
    out: list[tuple[str, int]] = []
    for start, end in ranges:
        for ln in range(start + 1, end):       # skip the header and the closing `end`
            text = lines[ln - 1].strip()
            if not text:
                continue
            m = re.match(r'^([A-Za-z_]\w*)', text)
            if not m:
                continue
            nm = m.group(1)
            if nm.lower() in _KEYWORDS:
                continue
            out.append((nm, ln))
    return out


class MatlabParser(BaseParser):
    def __init__(self, config: Config) -> None:
        super().__init__(config)
        # G7: aggregated classdef hierarchy (class name → ClassInfo). Consumed
        # generically by cli.py via getattr(parser, "class_registry", None), then
        # by the call-graph builder's class-hierarchy resolver.
        self.class_registry: dict[str, ClassInfo] = {}
        self._registry_lock = threading.Lock()

    def parse_file(
        self, path: Path
    ) -> tuple[list[FunctionDef], list[CallRelationship]]:
        source = path.read_text(encoding="utf-8-sig", errors="replace")
        raw_lines = source.splitlines()          # original lines for docstring extraction
        cleaned = _strip_comments(source)
        cleaned_lines = cleaned.splitlines()

        classdef = self._scan_classdef(cleaned_lines)   # G7

        functions = self._extract_functions(cleaned_lines, raw_lines, str(path), classdef)
        # Extract variables FIRST so _extract_calls can use local-var context.
        self._extract_variables(cleaned_lines, raw_lines, str(path), functions)
        calls = self._extract_calls(cleaned_lines, str(path), functions)

        # G6: infer parameter types from arguments blocks / validators / isa checks.
        _infer_param_types(cleaned_lines, functions)

        # G7: register the class hierarchy and attach class members to methods.
        if classdef is not None:
            self._register_class(classdef)
            self._attach_class_members(classdef, cleaned_lines, raw_lines,
                                       functions, str(path))

        # Best-effort dead-variable detection (at most medium confidence after G2).
        try:
            from . import _liveness
            _liveness.detect_dead_variables_matlab(functions, cleaned_lines)
        except Exception:
            pass

        if self.config.variables.track and self.config.variables.names:
            self._track_variables(cleaned_lines, functions)

        return functions, calls

    # ---------------------------------------------------------------- #
    # G7 — classdef / OOP support                                       #
    # ---------------------------------------------------------------- #

    def _scan_classdef(self, lines: list[str]) -> Optional[_ClassdefInfo]:
        name: Optional[str] = None
        bases: list[str] = []
        for line in lines:
            cm = _CLASS_LINE_RE.match(line)
            if cm:
                name = cm.group(1)
                if cm.group(2):
                    bases = [b.strip() for b in cm.group(2).split('&') if b.strip()]
                break
        if name is None:
            return None
        blocks = _parse_classdef_blocks(lines)
        return _ClassdefInfo(
            name=name, bases=bases,
            methods=blocks['methods'],
            properties=blocks['properties'],
            events=blocks['events'],
        )

    def _register_class(self, info: _ClassdefInfo) -> None:
        ci = ClassInfo(name=info.name, bases=set(info.bases))
        with self._registry_lock:
            master = self.class_registry.get(info.name)
            if master is None:
                self.class_registry[info.name] = ci
            else:
                master.bases.update(ci.bases)

    def _attach_class_members(
        self,
        info: _ClassdefInfo,
        lines: list[str],
        raw_lines: list[str],
        functions: list[FunctionDef],
        file_path: str,
    ) -> None:
        """Attach `properties`/`events` declarations as members visible to methods."""
        members: list[tuple[str, int, str, str]] = []
        for nm, ln in _block_member_names(lines, info.properties):
            members.append((nm, ln, "property", "property declaration"))
        for nm, ln in _block_member_names(lines, info.events):
            members.append((nm, ln, "event", "event declaration"))
        if not members:
            return

        methods = [fn for fn in functions if fn.is_method]
        for fn in methods:
            for nm, ln, scope_kind, src_kind in members:
                raw_line = raw_lines[ln - 1] if ln - 1 < len(raw_lines) else ""
                doc = _ml_inline_comment(raw_line) or _ml_comment_above(raw_lines, ln - 1)
                fn.variables.append(VariableDef(
                    name=nm,
                    scope=scope_kind,
                    line=ln,
                    file_path=file_path,
                    context=fn.qualified_name,
                    source_kind=src_kind,
                    source_detail=info.name,
                    doc_comment=doc,
                    full_source=raw_line.strip() or None,
                ))
            fn.variables = _dedup_variables(fn.variables)

    def parse_files(
        self,
        paths: list[Path],
        progress_task: Optional[tuple[Any, Any]] = None,
    ) -> tuple[list[FunctionDef], list[CallRelationship], list[str]]:
        """Two-pass parse for G1 call-vs-index disambiguation.

        Pass 1 (delegated to BaseParser): parse every .m file independently.
        Pass 2: now that we know all project function names, reclassify each call:
          - callee is a known project function  → keep; upgrade to HEURISTIC.
          - callee is a local/field variable in the caller AND NOT a function → drop
            (almost certainly array/struct indexing).
          - callee is unknown (built-in, external)          → keep as UNRESOLVED.
        """
        all_fns, all_calls, errors = super().parse_files(paths, progress_task)

        # Build project-wide function-name index (simple name + qualified name).
        project_func_names: set[str] = set()
        for fn in all_fns:
            project_func_names.add(fn.name)
            project_func_names.add(fn.qualified_name)

        # Build per-caller variable-name lookup: node_id → frozenset of var names.
        caller_var_names: dict[str, frozenset[str]] = {
            fn.node_id: frozenset(v.name for v in fn.variables)
            for fn in all_fns
        }

        filtered: list[CallRelationship] = []
        for call in all_calls:
            callee = call.callee_name
            if callee in project_func_names:
                # Confirmed project function → upgrade confidence.
                filtered.append(dataclasses.replace(
                    call,
                    resolution_hint="matlab_func_match",
                    resolution_confidence=ResolutionConfidence.HEURISTIC,
                ))
            else:
                local_vars = caller_var_names.get(call.caller_id, frozenset())
                if callee in local_vars:
                    # Callee is a local variable — almost certainly array/struct
                    # indexing (e.g. result(i)).  Drop to reduce graph noise.
                    continue
                # Unknown callee (MATLAB built-in or external dependency).
                filtered.append(call)

        return all_fns, filtered, errors

    # ---------------------------------------------------------------- #

    def _extract_functions(
        self,
        lines: list[str],
        raw_lines: list[str],
        file_path: str,
        classdef: Optional[_ClassdefInfo],
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

        class_name = classdef.name if classdef else None
        method_ranges = classdef.methods if classdef else []

        def _is_method(line_num: int) -> bool:
            # A function is a method when it lives inside a `methods ... end` block.
            # If the class has no detected methods blocks (defensive fallback) treat
            # every function in the classdef file as a method (legacy behaviour).
            if not class_name:
                return False
            if not method_ranges:
                return True
            return any(s <= line_num <= e for s, e in method_ranges)

        for j, (line_num, m) in enumerate(func_positions):
            end_line = func_positions[j + 1][0] - 1 if j + 1 < len(func_positions) else n_lines

            out1 = m.group(1)
            out2 = m.group(2)
            name = m.group(3)
            params_raw = m.group(4)

            return_type = _parse_return_type(out1, out2)
            params = _parse_params(params_raw)
            docstring = _extract_docstring(raw_lines, line_num - 1)  # 0-indexed

            is_method = _is_method(line_num)
            if is_method:
                func_type = "method"
                parent = class_name
                qualified = f"{class_name}.{name}"
            elif class_name:
                # Helper function outside any methods block (local function after
                # the classdef `end`): not a method of the class.
                func_type = "local function"
                parent = None
                qualified = name
            elif j == 0:
                func_type = "function"
                parent = None
                qualified = name
            else:
                func_type = "local function"
                parent = None
                qualified = name

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
                is_method=is_method,
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
        raw_lines: list[str],
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

            # G9: harvest the adjacent intent comment (inline wins over above) and
            # the full raw source statement for this declaration line.
            raw_line = raw_lines[i - 1] if i - 1 < len(raw_lines) else ""
            doc_comment = _ml_inline_comment(raw_line) or _ml_comment_above(raw_lines, i - 1)
            full_source = raw_line.strip() or None

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
                        doc_comment=doc_comment,
                        full_source=full_source,
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
                        doc_comment=doc_comment,
                        full_source=full_source,
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
                        doc_comment=doc_comment,
                        full_source=full_source,
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


# ------------------------------------------------------------------ #
# G6 — parameter type inference                                       #
# ------------------------------------------------------------------ #
# MATLAB is dynamically typed, so there is never a declared type. We infer a soft
# type from three sources, all of which are *optional* developer-supplied hints:
#   1. an `arguments ... end` validation block (R2019b+): `name (size) type {valid}`
#   2. `validateattributes(name, {'type', ...}, ...)` — first class string
#   3. `mustBe*` validators and `isa(name, 'Type')` checks.

_ARGS_BLOCK_START_RE = re.compile(r"^\s*arguments\b(?:\s*\([^)]*\))?\s*$", re.IGNORECASE)
# A single declaration line inside an arguments block:
#   name(.field)? (dims)? type? {validators}? (= default)?
_ARG_DECL_RE = re.compile(
    r"^\s*([A-Za-z_]\w*)"                     # 1: parameter name
    r"(?:\.[A-Za-z_]\w*)?"                    #    optional .field (struct arg) — ignored
    r"(?:\s*\([^)]*\))?"                      #    optional (size) spec
    r"\s*([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)?"  # 2: optional type (possibly dotted)
)
_VALIDATEATTR_RE = re.compile(
    r"\bvalidateattributes\s*\(\s*([A-Za-z_]\w*)\s*,\s*\{\s*['\"]([A-Za-z_]\w*)['\"]",
    re.IGNORECASE,
)
_ISA_RE = re.compile(
    r"\bisa\s*\(\s*([A-Za-z_]\w*)\s*,\s*['\"]([A-Za-z_]\w*(?:\.\w+)*)['\"]",
    re.IGNORECASE,
)
_MUSTBEA_RE = re.compile(
    r"\bmustBeA\s*\(\s*([A-Za-z_]\w*)\s*,\s*['\"]([A-Za-z_]\w*(?:\.\w+)*)['\"]",
    re.IGNORECASE,
)
# Simple `mustBe<Type>(name)` validators that map directly to a soft type word.
_MUSTBE_SIMPLE = {
    "mustbenumeric": "numeric",
    "mustbereal": "real",
    "mustbeinteger": "integer",
    "mustbefloat": "float",
    "mustbetext": "text",
    "mustbetextscalar": "text",
    "mustbestring": "string",
    "mustbelogical": "logical",
}
_MUSTBE_SIMPLE_RE = re.compile(
    r"\b(mustBe[A-Za-z]+)\s*\(\s*([A-Za-z_]\w*)", re.IGNORECASE
)
# Tokens that are NOT real types when they appear in the type slot of an arg decl.
_ARG_NON_TYPES = {"arguments", "end", "function", "if", "for", "while", "switch"}


def _infer_param_types(lines: list[str], functions: list[FunctionDef]) -> None:
    """Populate ``Parameter.type_hint`` for MATLAB params from validation hints.

    All inferred types are *soft* (MATLAB has no declared types). The first source
    that yields a type for a given parameter wins, in priority order:
    arguments-block > validateattributes > mustBeA/isa > simple mustBe* validator.
    Never overwrites an already-set type_hint.
    """
    for fn in functions:
        if not fn.parameters:
            continue
        param_names = {p.name for p in fn.parameters}
        inferred: dict[str, str] = {}

        start = max(fn.line_start, 1)
        end = fn.line_end or fn.line_start
        body = lines[start:end] if end > start else []

        in_args = False
        for raw in body:
            line = raw.split("%", 1)[0]
            stripped = line.strip()
            if not stripped:
                continue

            # ── arguments block tracking ──
            if not in_args and _ARGS_BLOCK_START_RE.match(line):
                in_args = True
                continue
            if in_args:
                if stripped.lower() == "end":
                    in_args = False
                    continue
                m = _ARG_DECL_RE.match(line)
                if m:
                    pname, ptype = m.group(1), m.group(2)
                    if (pname in param_names and ptype
                            and ptype.lower() not in _ARG_NON_TYPES
                            and pname not in inferred):
                        inferred[pname] = ptype
                continue

            # ── validateattributes(name, {'type', ...}) ──
            for vm in _VALIDATEATTR_RE.finditer(line):
                pname, ptype = vm.group(1), vm.group(2)
                if pname in param_names and pname not in inferred:
                    inferred[pname] = ptype

            # ── mustBeA(name, 'Type') / isa(name, 'Type') ──
            for rx in (_MUSTBEA_RE, _ISA_RE):
                for am in rx.finditer(line):
                    pname, ptype = am.group(1), am.group(2)
                    if pname in param_names and pname not in inferred:
                        inferred[pname] = ptype

            # ── simple mustBe<Type>(name) validators ──
            for sm in _MUSTBE_SIMPLE_RE.finditer(line):
                fn_name, pname = sm.group(1).lower(), sm.group(2)
                mapped = _MUSTBE_SIMPLE.get(fn_name)
                if mapped and pname in param_names and pname not in inferred:
                    inferred[pname] = mapped

        if inferred:
            for p in fn.parameters:
                if p.type_hint is None and p.name in inferred:
                    p.type_hint = inferred[p.name]

