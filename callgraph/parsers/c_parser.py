"""
C source parser using Tree-sitter.

A conservative `#define` macro-expansion pre-pass (idea C-1, see
``macro_expand.py``) runs before parsing when ``config.parser.expand_macros`` is
enabled (default), so call sites wrapped in simple macros resolve to the real
callee. Function definitions emitted entirely via macros (e.g.
``DECLARE_HANDLER(foo, int)``) are still not detected; for heavily macro-driven
codebases consider a libclang backend instead.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from ..config import Config
from ..models import CallRelationship, FunctionDef, Language, Parameter, ResolutionConfidence, VariableDef
from .base import BaseParser
from .macro_expand import expand_c_macros

try:
    import tree_sitter_c as _tsc
    from tree_sitter import Language as TSLanguage, Parser as TSParser
    _TS_AVAILABLE = True
except ImportError:
    _TS_AVAILABLE = False


def _check_available() -> None:
    if not _TS_AVAILABLE:
        raise ImportError(
            "tree-sitter-c is not installed.\n"
            "Run: pip install tree-sitter tree-sitter-c"
        )


# ==============================================================================
# CUSTOM INPUT FUNCTION TEMPLATE — EDIT HERE
# ==============================================================================
# Detect calls to functions that write external data into a variable.
#
# Pattern A — custom_input (lugasi / lugasian family):
#   lugasi(&DEST, "SOURCE", WOW_NA)    — C++ class method or free function
#   lugasian(&DEST, "SOURCE", WOW_LI)  — C++ class method or free function
#   lugasi2(&DEST, "SOURCE", WOW_NA)   — C-style free function variant
#   lugasian2(&DEST, "SOURCE", WOW_LI) — C-style free function variant
#
#   Arg[0] (&DEST)    — destination variable written by address (& stripped)
#   Arg[1] ("SOURCE") — quoted source name; becomes the upstream VF source node
#   Arg[2] (WOW_NA)   — classifier shown as a badge on the destination block
#
# Pattern B — connect free function (connect2 family):
#   connect2(DEST_PTR, "path/input_name", ctx)
#
#   Arg[0] (DEST_PTR) — destination variable (pointer; & or * stripped)
#   Arg[1] ("path/input_name") — path string; last segment = input source name
#
# Detected via field_expression (.connect) or direct call (CUSTOM_INPUT / CONNECT_FREE).
# Both C and C++ parsers use these lists — add names in the case used in source.

CUSTOM_INPUT_FUNC_NAMES: list[str] = [
    # C++ class method form (lowercase, called as obj.lugasi(...) or bare lugasi(...))
    "lugasi",
    "lugasian",
    # C-style free function form (lowercase "2" variants)
    "lugasi2",
    "lugasian2",
    # Uppercase macro/legacy forms
    "LUGASI",
    "LUGASIAN",
    "LUGASI2",
    "LUGASIAN2",
]
CUSTOM_INPUT_ARG_DEST     = 0   # Index of destination variable argument (with &)
CUSTOM_INPUT_ARG_SOURCE   = 1   # Index of quoted source/input name argument
CUSTOM_INPUT_ARG_CLASSIFY = 2   # Index of source classifier/type argument
# Extra arguments beyond index 2 are ignored.

# Free-function connect pattern: connect2(dest_ptr, "path/input_name", ctx)
CONNECT_FREE_FUNC_NAMES: list[str] = [
    "connect2",
    "CONNECT2",
]
CONNECT_FREE_ARG_DEST = 0   # Index of destination variable argument (pointer)
CONNECT_FREE_ARG_PATH = 1   # Index of quoted path/input_name argument

# Method names matched by _extract_connect_call (field_expression / dot-call form).
# Add variants here — e.g. "Connect" for PascalCase codebases.
CONNECT_METHOD_NAMES: list[str] = [
    "connect",
    "Connect",
]
# ==============================================================================
# END CUSTOM INPUT FUNCTION TEMPLATE
# ==============================================================================


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


_STATEMENT_NODE_TYPES = frozenset({
    "declaration",
    "expression_statement",
    "field_declaration",
    "init_declarator",
    "assignment_expression",
    "return_statement",
    "if_statement",
    "for_statement",
    "while_statement",
})


def _full_statement_text(node, source: bytes, cap: int = 200) -> Optional[str]:
    """Return the complete source statement enclosing `node`, whitespace-collapsed.

    Walks up to the nearest statement-like ancestor (declaration / expression
    statement) so the SOURCE row shows the whole line of code — e.g.
    ``raw_speed = lugasi(pThis, s_strc, NULL);`` rather than just the RHS.
    A trailing ``;`` is preserved; interior runs of whitespace/newlines are
    collapsed to single spaces so multi-line statements read on one line.
    """
    if node is None:
        return None
    stmt = node
    # Prefer the enclosing expression_statement / declaration so we capture the
    # left-hand side + terminator, not just the assignment/initializer subtree.
    cur = node
    hops = 0
    while cur is not None and hops < 6:
        if cur.type in ("expression_statement", "declaration", "field_declaration"):
            stmt = cur
            break
        cur = cur.parent
        hops += 1
    text = _node_text(stmt, source)
    # Collapse all whitespace runs (incl. newlines) to single spaces.
    text = " ".join(text.split()).strip()
    if not text:
        return None
    if len(text) > cap:
        text = text[: cap - 1].rstrip() + "…"
    return text


# ------------------------------------------------------------------ #
# VF-10: adjacent intent-comment extraction                            #
# ------------------------------------------------------------------ #
# Covers four common patterns:
#   1. Inline right-side:  float speed;  // raw speed from sensor
#   2. Line immediately above:  // Raw speed\n  float speed;
#   3. Multiple // lines above (treated as one block)
#   4. /* ... */ block comment above (multi-line doc style), even with
#      one blank line between the comment and the declaration.
#
# A single blank line between the comment and the declaration is still
# allowed (the programmer sometimes adds breathing room).  Two or more
# blank lines are treated as "not related".
# ---------------------------------------------------------------------------

def _inline_comment_from_line(line_text: str) -> Optional[str]:
    """Return the text of a // or /* */ comment on the right side of a source line."""
    # Strip string and char literals so // inside a string is not mistaken
    stripped = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line_text)
    stripped = re.sub(r"'(?:[^'\\]|\\.)*'", "''", stripped)
    m = re.search(r'//+\s*(.+)', stripped)
    if m:
        return m.group(1).strip()
    m = re.search(r'/\*+\s*(.*?)\s*\*+/', stripped)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return None


def _comment_above(start_line: int, lines: list) -> Optional[str]:
    """Walk upward from start_line collecting adjacent comment text.

    *lines* is source.split(b'\\n').  Allows exactly one blank line gap.
    """
    i = start_line
    if i < 0 or i >= len(lines):
        return None
    txt = lines[i].decode("utf-8", errors="replace").strip()
    # Allow one blank line between comment and declaration
    if not txt:
        i -= 1
        if i < 0 or i >= len(lines):
            return None
        txt = lines[i].decode("utf-8", errors="replace").strip()
        if not txt:
            return None  # two blank lines → not related
    # --- Consecutive // comment lines ----
    if txt.startswith("//"):
        collected: list[str] = []
        j = i
        while j >= 0:
            t = lines[j].decode("utf-8", errors="replace").strip()
            if t.startswith("//"):
                clean = re.sub(r'^//+\s*', '', t).strip()
                if clean:
                    collected.append(clean)
                j -= 1
            else:
                break
        if collected:
            collected.reverse()
            return " ".join(collected[:5])  # cap at 5 merged lines
    # --- Block comment /* ... */ ending on this line ---
    if "*/" in txt:
        block: list[str] = []
        j = i
        while j >= 0:
            t = lines[j].decode("utf-8", errors="replace").strip()
            # Strip common doc-comment delimiters / doxygen prefixes
            clean = re.sub(r'^/?\*+\s*', '', t).rstrip("*/").strip()
            # Skip doxygen @ / \ directives
            if clean.startswith(("@", "\\")):
                j -= 1
                continue
            if clean:
                block.append(clean)
            if t.startswith("/*") or t.startswith("/**") or t.startswith("/*!"):
                break
            j -= 1
        block = [b for b in block if b]
        if block:
            block.reverse()
            return " ".join(block[:5])
    return None


def _extract_adjacent_comment(line_0: int, source: bytes) -> Optional[str]:
    """Extract the best adjacent intent comment for a declaration at *line_0* (0-based).

    Priority order: inline right-side → comments above.
    """
    lines = source.split(b'\n')
    if line_0 < 0 or line_0 >= len(lines):
        return None
    # Case 1: inline right-side
    decl_text = lines[line_0].decode("utf-8", errors="replace")
    inline = _inline_comment_from_line(decl_text)
    if inline:
        return inline
    # Cases 2-4: above
    return _comment_above(line_0 - 1, lines)


def _find_children_by_type(node, type_name: str) -> list:
    return [c for c in node.children if c.type == type_name]


def _find_child_by_type(node, type_name: str):
    for c in node.children:
        if c.type == type_name:
            return c
    return None


_TYPE_NODE_TYPES = {
    "type_specifier", "primitive_type", "sized_type_specifier",
    "type_qualifier", "storage_class_specifier", "struct_specifier",
    "enum_specifier", "union_specifier", "type_identifier",
    "qualified_identifier", "template_type", "auto",
    "placeholder_type_specifier",
}

_DECLARATOR_NODE_TYPES = {
    "identifier", "field_identifier", "init_declarator",
    "pointer_declarator", "reference_declarator", "array_declarator",
    "parenthesized_declarator",
}


def _has_descendant_type(node, type_name: str) -> bool:
    if node.type == type_name:
        return True
    return any(_has_descendant_type(child, type_name) for child in node.children)


def _extract_storage_class(decl_node, source: bytes) -> Optional[str]:
    storage = _find_child_by_type(decl_node, "storage_class_specifier")
    return _node_text(storage, source).strip() if storage else None


def _extract_declaration_type(decl_node, source: bytes) -> Optional[str]:
    parts: list[str] = []
    for child in decl_node.children:
        if child.type in _DECLARATOR_NODE_TYPES or child.type == "function_declarator":
            break
        if child.type == "storage_class_specifier":
            continue
        if child.type in _TYPE_NODE_TYPES:
            text = _node_text(child, source).strip()
            if text:
                parts.append(text)
    return " ".join(parts).strip() or None


def _extract_variable_name(declarator_node, source: bytes) -> Optional[str]:
    if declarator_node is None or declarator_node.type == "function_declarator":
        return None

    if declarator_node.type in ("identifier", "field_identifier"):
        return _node_text(declarator_node, source)

    if declarator_node.type == "init_declarator":
        for child in declarator_node.children:
            if child.type == "=":
                break
            if child.is_named:
                name = _extract_variable_name(child, source)
                if name:
                    return name
        return None

    if declarator_node.type in (
        "pointer_declarator", "reference_declarator", "array_declarator",
        "parenthesized_declarator",
    ):
        for child in declarator_node.children:
            name = _extract_variable_name(child, source)
            if name:
                return name

    return None


def _extract_initializer_value(init_node, source: bytes) -> Optional[str]:
    value_node = _extract_initializer_node(init_node)
    if value_node is None:
        return None
    return _node_text(value_node, source).strip()


def _extract_initializer_node(init_node):
    if init_node.type != "init_declarator":
        return None

    after_equals = False
    value_node = None
    for child in init_node.children:
        if child.type == "=":
            after_equals = True
            continue
        if after_equals and child.is_named:
            value_node = child
            break

    return value_node


def _type_with_declarator_suffix(type_hint: Optional[str], declarator_node, source: bytes) -> Optional[str]:
    if not type_hint:
        return None

    text = _node_text(declarator_node, source).strip() if declarator_node else ""
    suffix = ""
    if text.startswith("*"):
        suffix = "*"
    elif text.startswith("&"):
        suffix = "&"
    return f"{type_hint} {suffix}".strip()


def _is_constant_name(name: str) -> bool:
    letters = [ch for ch in name if ch.isalpha()]
    return bool(letters) and name.upper() == name


def _is_const_declaration(decl_node, source: bytes) -> bool:
    return any(
        child.type == "type_qualifier" and _node_text(child, source).strip() == "const"
        for child in decl_node.children
    )


def _called_name_from_call(call_node, source: bytes) -> Optional[str]:
    for child in call_node.children:
        if child.type in ("identifier", "qualified_identifier", "template_function"):
            return _node_text(child, source).strip()
        if child.type == "field_expression":
            field_id = _find_child_by_type(child, "field_identifier")
            if field_id:
                return _node_text(field_id, source)
    return None


def _find_call_names(node, source: bytes) -> list[str]:
    names: list[str] = []
    if node.type == "call_expression":
        name = _called_name_from_call(node, source)
        if name:
            names.append(name)
    for child in node.children:
        if child.is_named:
            names.extend(_find_call_names(child, source))
    return names


def _extract_assign_src(value_node, source: bytes) -> Optional[str]:
    """Return the source variable name for cross-var assignment edges (VFI-3).

    Returns the base name when the RHS is:
    - a direct identifier:   ``y = x``    -> ``"x"``
    - a single-identifier call: ``y = fn(x)`` -> ``"x"``

    Returns None for literals, multi-arg calls, and complex expressions.
    """
    if value_node is None:
        return None
    if value_node.type == "identifier":
        name = _node_text(value_node, source).strip()
        return name if name else None
    if value_node.type == "call_expression":
        args_node = _find_child_by_type(value_node, "argument_list")
        if args_node:
            id_args = [c for c in args_node.children
                       if c.is_named and c.type == "identifier"]
            if len(id_args) == 1:
                name = _node_text(id_args[0], source).strip()
                return name if name else None
    return None


def _classify_value_node(value_node, source: bytes) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if value_node is None:
        return None, None, None

    text = _node_text(value_node, source).strip()
    node_type = value_node.type

    if node_type in ("number_literal", "true", "false"):
        return "hard-coded number", "literal", _infer_type_from_literal_text(text)
    if node_type in ("string_literal", "char_literal", "concatenated_string"):
        return "hard-coded string", "literal", "string"
    if node_type == "null":
        return "null literal", "literal", "null"
    if node_type == "new_expression":
        return "heap allocation", "new", None
    if node_type == "initializer_list":
        return "initializer list", "literal", None
    if node_type == "call_expression":
        callee = _called_name_from_call(value_node, source)
        base = (callee or "").split("::")[-1].split(".")[-1].split("<")[0]
        if base in {"malloc", "calloc", "realloc", "aligned_alloc", "new", "make_unique", "make_shared"}:
            return "heap allocation", callee, None
        if base in {"getenv", "_wgetenv", "secure_getenv"}:
            return "environment lookup", callee, "string"
        return "function call", callee, None

    call_names = _find_call_names(value_node, source)
    if call_names:
        bases = [name.split("::")[-1].split(".")[-1].split("<")[0] for name in call_names]
        for name, base in zip(call_names, bases):
            if base in {"malloc", "calloc", "realloc", "aligned_alloc", "new", "make_unique", "make_shared"}:
                return "heap allocation", name, None
            if base in {"getenv", "_wgetenv", "secure_getenv"}:
                return "environment lookup", name, "string"
        return "expression with function call", call_names[0], None
    return "expression", None, None


def _infer_type_from_literal_text(text: str) -> Optional[str]:
    stripped = text.strip().lower()
    if stripped in {"true", "false"}:
        return "bool"
    if any(ch in stripped for ch in (".", "e")):
        return "float"
    return "int" if stripped else None


def _choose_variable_kind(
    name: str,
    default_scope: str,
    decl_node,
    storage: Optional[str],
    value_node,
    source: bytes,
) -> str:
    source_kind, _, _ = _classify_value_node(value_node, source)
    if source_kind == "environment lookup":
        return "environment"
    if source_kind == "heap allocation":
        return "dynamic"
    if _is_const_declaration(decl_node, source) or _is_constant_name(name):
        return "constant"
    if decl_node.type == "field_declaration":
        return "static" if storage == "static" else "field"
    if storage == "static":
        return "static"
    if storage == "extern":
        return "global"
    return default_scope


def _build_variable_def(
    name: str,
    kind: str,
    type_hint: Optional[str],
    value: Optional[str],
    value_node,
    line: int,
    file_path: str,
    context: Optional[str],
    source: bytes,
    doc_comment: Optional[str] = None,
    full_source: Optional[str] = None,
) -> VariableDef:
    source_kind, source_detail, inferred_type = _classify_value_node(value_node, source)
    vdef = VariableDef(
        name=name,
        scope=kind,
        type_hint=type_hint or inferred_type,
        value=value,
        line=line,
        file_path=file_path,
        context=context,
        source_kind=source_kind,
        source_detail=source_detail,
        doc_comment=doc_comment or None,
        full_source=full_source or None,
    )
    # VFI-3: record the source variable name for cross-variable assignment edges.
    assign_src = _extract_assign_src(value_node, source)
    if assign_src and assign_src.lower() != name.lower():
        vdef.assign_src = assign_src
    return vdef


def _extract_variables_from_declaration(
    decl_node,
    source: bytes,
    default_scope: str,
    file_path: str,
    context: Optional[str] = None,
) -> list[VariableDef]:
    if _has_descendant_type(decl_node, "function_declarator"):
        return []

    storage = _extract_storage_class(decl_node, source)

    base_type = _extract_declaration_type(decl_node, source)
    variables: list[VariableDef] = []

    for child in decl_node.children:
        if child.type not in _DECLARATOR_NODE_TYPES:
            continue
        name = _extract_variable_name(child, source)
        if not name:
            continue
        value_node = _extract_initializer_node(child)
        value = _extract_initializer_value(child, source)
        kind = _choose_variable_kind(name, default_scope, decl_node, storage, value_node, source)
        doc_comment = _extract_adjacent_comment(child.start_point[0], source)
        variables.append(_build_variable_def(
            name=name,
            kind=kind,
            type_hint=_type_with_declarator_suffix(base_type, child, source),
            value=value,
            value_node=value_node,
            line=child.start_point[0] + 1,
            file_path=file_path,
            context=context,
            source=source,
            doc_comment=doc_comment,
            full_source=_full_statement_text(child, source),
        ))

    return variables


def _extract_preproc_constant(node, source: bytes, file_path: str) -> Optional[VariableDef]:
    if node.type not in ("preproc_def", "preproc_function_def"):
        return None
    name_node = _find_child_by_type(node, "identifier")
    if not name_node:
        return None
    arg_node = _find_child_by_type(node, "preproc_arg")
    value = _node_text(arg_node, source).strip() if arg_node else None
    type_hint = None
    if value:
        if value.startswith(("\"", "'")):
            type_hint = "string"
        else:
            type_hint = _infer_type_from_literal_text(value)
    return VariableDef(
        name=_node_text(name_node, source),
        scope="constant",
        type_hint=type_hint,
        value=value,
        line=node.start_point[0] + 1,
        file_path=file_path,
        source_kind="preprocessor define",
        source_detail="#define",
    )


def _extract_assignment_variable(
    node,
    source: bytes,
    file_path: str,
    default_scope: str,
    context: Optional[str],
) -> Optional[VariableDef]:
    if node.type != "assignment_expression":
        return None

    named_children = [child for child in node.children if child.is_named]
    if len(named_children) < 2:
        return None

    target = named_children[0]
    value_node = named_children[-1]
    target_text = _node_text(target, source).strip()
    value = _node_text(value_node, source).strip()

    if target.type == "field_expression":
        field = _find_child_by_type(target, "field_identifier")
        name = target_text if not field else target_text
        kind = "field"
    elif target.type == "identifier":
        name = target_text
        kind = default_scope
    else:
        return None

    source_kind, _, _ = _classify_value_node(value_node, source)
    if source_kind == "environment lookup" and kind != "field":
        kind = "environment"
    elif source_kind == "heap allocation" and kind != "field":
        kind = "dynamic"
    elif _is_constant_name(name):
        kind = "constant"

    return _build_variable_def(
        name=name,
        kind=kind,
        type_hint=None,
        value=value,
        value_node=value_node,
        line=node.start_point[0] + 1,
        file_path=file_path,
        context=context,
        source=source,
        full_source=_full_statement_text(node, source),
    )


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


_IDENT_TOKEN_RE = re.compile(r"[A-Za-z_]\w*")


def _identifier_tokens_in_range(text_lines: list[str], start: int, end: int) -> set[str]:
    """Set of identifier tokens appearing in source lines [start, end] (1-indexed)."""
    names: set[str] = set()
    lo = max(start - 1, 0)
    hi = max(end, lo)
    for line in text_lines[lo:hi]:
        names.update(_IDENT_TOKEN_RE.findall(line))
    return names


def _attach_referenced_globals(
    functions: list[FunctionDef],
    file_variables: list[VariableDef],
    source_bytes: bytes,
) -> None:
    """Attach file-scope (global) variables to each function (G8).

    Previously *every* file-scope global was prepended to *every* function's
    ``variables`` list, which is O(functions x globals) in both time and memory
    (a 500-function file with 1,000 globals produced ~500,000 records).  Instead
    we attach a global to a function only when the function body actually
    references that identifier — which is both far cheaper and more correct
    (variable-flow no longer shows globals a function never touches).
    """
    if not file_variables:
        for fn in functions:
            fn.variables = _dedup_variables(fn.variables)
        return

    from collections import defaultdict
    by_name: dict[str, list[VariableDef]] = defaultdict(list)
    for var in file_variables:
        by_name[var.name].append(var)

    text_lines = source_bytes.decode("utf-8", "replace").splitlines()
    for fn in functions:
        used = _identifier_tokens_in_range(
            text_lines, fn.line_start, fn.line_end or fn.line_start
        )
        relevant: list[VariableDef] = []
        for name in used:
            bucket = by_name.get(name)
            if bucket:
                relevant.extend(bucket)
        relevant.sort(key=lambda v: v.line)
        fn.variables = _dedup_variables(relevant + fn.variables)


# ------------------------------------------------------------------ #
# Dead-variable detection (post-pass after function body is walked)   #
# ------------------------------------------------------------------ #

def _collect_identifiers_in_subtree(node, source: bytes) -> list[tuple[str, int]]:
    """Collect all (name, line) from identifier and field_identifier nodes (iterative)."""
    result: list[tuple[str, int]] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("identifier", "field_identifier"):
            result.append((_node_text(n, source).strip(), n.start_point[0] + 1))
        stack.extend(reversed(n.children))
    return result


def _detect_dead_variables(fn: "FunctionDef", body_node, source: bytes) -> None:
    """
    Post-pass: classify dead variables/parameters using the shared read/write +
    lexical-scope liveness engine (``_liveness.detect_dead_variables_c``).

    Replaces the former name-based heuristic. The new engine resolves each
    reference to its nearest enclosing declaration (case-sensitive), distinguishes
    reads from writes, reasons about pointers/allocation, and suppresses
    intentional-unused names — populating the richer ``dead_category`` /
    ``dead_confidence`` / ``read_lines`` / ``write_lines`` fields while still
    deriving the legacy ``is_dead`` / ``dead_reason`` flags for back-compat.
    """
    from . import _liveness

    _liveness.detect_dead_variables_c(fn, body_node, source)


# ------------------------------------------------------------------ #
# Helpers: unwrap casts / address-of from a dest-argument expression  #
# ------------------------------------------------------------------ #

# Matches a C/C++ cast prefix like `(char*)`, `(unsigned long *)`, `(MyType **)`,
# `(uint8_t * const)`. Required: starts with identifier, contains at least one
# `*` or `&` (pointer/ref marker — the distinguishing feature of a cast), then
# optional trailing qualifier (`const`, `volatile`, `restrict`, more `*`/`&`).
_CAST_RE = re.compile(
    r"^\s*\(\s*"                      # opening paren
    r"[A-Za-z_][\w\s:<>,]*"           # type name (allows templates / namespaces)
    r"[*&][\w\s:<>,*&]*"              # at least one * or & somewhere inside
    r"\)\s*"                          # closing paren
)


def _strip_cast_and_addr(text: str) -> str:
    """
    Strip C/C++ casts, wrapping parens, address-of `&` and dereference `*`
    operators from the front of an expression, leaving the bare destination.
    Shared by `_unwrap_dest_arg` (simple-identifier callers) and
    `_unwrap_dest_full` (member-aware callers).
    """
    s = (text or "").strip()
    changed = True
    while changed:
        changed = False
        m = _CAST_RE.match(s)
        if m:
            s = s[m.end():].lstrip()
            changed = True
            continue
        if s.startswith("(") and s.endswith(")"):
            depth = 0
            balanced_outer = True
            for i, ch in enumerate(s):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0 and i != len(s) - 1:
                        balanced_outer = False
                        break
            if balanced_outer and depth == 0:
                s = s[1:-1].strip()
                changed = True
                continue
    s = s.lstrip("&* ").strip()
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        balanced_outer = True
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    balanced_outer = False
                    break
        if not balanced_outer:
            break
        s = s[1:-1].strip()
    return s


def _unwrap_dest_full(text: str) -> tuple[str, str]:
    """
    Like `_unwrap_dest_arg` but PRESERVES member chains. Returns
    `(full_dest_expression, head_identifier)`.

    The full expression is suitable for use as the tracked variable name
    when the dest is something like `IMU.x_acc()`. The head identifier
    is the parent object (or the bare variable name when there is no
    member access) — useful for parent_name and as the .isidentifier()
    validation check.

    Examples (input -> (full, head)):
        "&x"                          -> ("x",           "x")
        "&m_Force"                    -> ("m_Force",     "m_Force")
        "(char*)&m_Force"             -> ("m_Force",     "m_Force")
        "&IMU.x_acc()"                -> ("IMU.x_acc",   "IMU")
        "(char*)&IMU.x_acc()"         -> ("IMU.x_acc",   "IMU")
        "&IMU->y_acc()"               -> ("IMU->y_acc",  "IMU")
        "&s.field"                    -> ("s.field",     "s")
        "&buf[i]"                     -> ("buf",         "buf")
    """
    s = _strip_cast_and_addr(text)
    # Drop trailing method-call parens like `()` (and accidental whitespace before them)
    while s.endswith(")"):
        # If the dest ends with `()` (empty parens after a member), strip them.
        # Be conservative: only strip empty `()`, never something like `f(x)`.
        # That keeps a non-method-call form like `f(x)` intact (which isn't a
        # valid LUGASI dest anyway and will be caught by the head .isidentifier()
        # check below).
        idx_open = s.rfind("(")
        if idx_open == -1:
            break
        inside = s[idx_open + 1 : -1].strip()
        if inside != "":
            break
        s = s[:idx_open].rstrip()
    # Drop trailing array index `[...]`
    if s.endswith("]"):
        idx_open = s.rfind("[")
        if idx_open != -1:
            s = s[:idx_open].rstrip()
    full = s
    # Head identifier = the first segment before any member separator or index
    head = full.split("->")[0].split(".")[0].split("[")[0].strip()
    return full, head


def _unwrap_dest_arg(text: str) -> str:
    """
    Strip C/C++ casts, wrapping parens, address-of and dereference operators
    from the destination-argument text of a LUGASI / connect2-style call.
    Returns ONLY the head identifier — for member-aware callers that need
    to preserve the full member expression, use `_unwrap_dest_full`.

    Examples (input -> output):
        "&x"                     -> "x"
        "&m_Force"               -> "m_Force"
        "(char*)&m_Force"        -> "m_Force"
        "(unsigned long *)&v"    -> "v"
        "((char*)&m_Force)"      -> "m_Force"
        "(some_type **) & x"     -> "x"
        "&s.field"               -> "s"     (parent of member access)
    """
    _full, head = _unwrap_dest_full(text)
    return head


# ------------------------------------------------------------------ #
# .connect("PATH/input_var", ...) pattern detection                   #
# ------------------------------------------------------------------ #

def _extract_connect_call(
    call_node,
    source: bytes,
    file_path: str,
    current_func: "FunctionDef",
) -> Optional[VariableDef]:
    """
    Detect: variable.connect("PATH/input_var_name", ...) calls.
    Returns a VariableDef representing the local variable bound to the input source,
    or None if this call does not match the pattern.
    """
    # Must be a field_expression call (.method syntax)
    func_part = None
    for child in call_node.children:
        if child.type == "field_expression":
            func_part = child
            break
    if func_part is None:
        return None

    field_id = _find_child_by_type(func_part, "field_identifier")
    if field_id is None or _node_text(field_id, source).strip() not in CONNECT_METHOD_NAMES:
        return None

    # Receiver is the expression before .connect / ->connect. Tree-sitter's
    # field_expression exposes it via the 'argument' named field; fall back to
    # the first named child that isn't the field_identifier for older grammars.
    # Supported receiver shapes (raw text -> stored name):
    #   var.connect(...)              -> "var"
    #   module.signal.connect(...)    -> "module.signal"
    #   this->sensor.connect(...)     -> "this->sensor"
    #   obj->field.connect(...)       -> "obj->field"
    #   IMU.x_acc().connect(...)      -> "IMU.x_acc"
    # _unwrap_dest_full strips casts / & / trailing () / [] using the same
    # normalization the LUGASI extractor applies to member-chain dests, so a
    # .connect block on `IMU.x_acc` keys the same VAR_FLOW_DATA record as a
    # LUGASI dest on `IMU.x_acc`.
    receiver_node = None
    try:
        receiver_node = func_part.child_by_field_name("argument")
    except Exception:
        receiver_node = None
    if receiver_node is None:
        for ch in func_part.children:
            if ch.is_named and ch is not field_id:
                receiver_node = ch
                break
    if receiver_node is None:
        return None
    receiver, _head = _unwrap_dest_full(_node_text(receiver_node, source))
    if not receiver:
        return None

    # First argument must be a string literal
    arg_list = _find_child_by_type(call_node, "argument_list")
    if arg_list is None:
        return None
    first_arg = None
    for child in arg_list.children:
        if child.type not in (",", "(", ")") and child.is_named:
            first_arg = child
            break
    if first_arg is None or first_arg.type not in ("string_literal", "concatenated_string"):
        return None

    path_raw = _node_text(first_arg, source).strip()
    # Strip surrounding quotes
    path_str = path_raw
    if path_str.startswith('"') and path_str.endswith('"'):
        path_str = path_str[1:-1]
    elif path_str.startswith("'") and path_str.endswith("'"):
        path_str = path_str[1:-1]
    if not path_str:
        return None

    input_name = path_str.split("/")[-1] if "/" in path_str else path_str

    return VariableDef(
        name=receiver,
        scope="local",
        line=call_node.start_point[0] + 1,
        file_path=file_path,
        context=current_func.qualified_name,
        source_kind="input_file_connect",
        source_detail=path_str,
        connect_path=path_str,
        connect_input_name=input_name,
        value=_node_text(call_node, source).strip()[:120],
        doc_comment=_extract_adjacent_comment(call_node.start_point[0], source),
        full_source=_full_statement_text(call_node, source),
    )


# ------------------------------------------------------------------ #
# Free-function connect pattern (connect2 family)                     #
# connect2(dest_ptr, "path/input_name", ctx)                         #
# ------------------------------------------------------------------ #

def _extract_connect_free_func_call(
    call_node,
    source: bytes,
    file_path: str,
    current_func: "FunctionDef",
) -> Optional[VariableDef]:
    """
    Detect: connect2(dest_ptr, "path/input_name", ...) direct-call pattern.
    The destination variable is the first argument (pointer; & and * stripped).
    The path string is the second argument; the last path segment is the input name.
    Returns a VariableDef with source_kind='input_file_connect' and custom_input_func
    set to the matched function name so the badge displays correctly.
    """
    func_name = _extract_call_name(call_node, source)
    if not func_name or func_name not in CONNECT_FREE_FUNC_NAMES:
        return None

    arg_list = _find_child_by_type(call_node, "argument_list")
    if arg_list is None:
        return None

    args = [
        child for child in arg_list.children
        if child.type not in (",", "(", ")") and child.is_named
    ]

    max_needed = max(CONNECT_FREE_ARG_DEST, CONNECT_FREE_ARG_PATH)
    if len(args) <= max_needed:
        return None

    # --- Destination variable (preserve member chain; strip casts/parens/&) ---
    dest_node = args[CONNECT_FREE_ARG_DEST]
    dest_txt = _node_text(dest_node, source).strip()
    dest_full, dest_head = _unwrap_dest_full(dest_txt)
    if not dest_head.isidentifier():
        return None
    dest_name = dest_full
    dest_parent = dest_head if dest_full != dest_head else None

    # --- Path string (second arg must be a string literal) ---
    path_node = args[CONNECT_FREE_ARG_PATH]
    path_raw = _node_text(path_node, source).strip()
    if path_node.type not in ("string_literal", "concatenated_string"):
        return None
    path_str = path_raw
    if path_str.startswith('"') and path_str.endswith('"'):
        path_str = path_str[1:-1]
    elif path_str.startswith("'") and path_str.endswith("'"):
        path_str = path_str[1:-1]
    if not path_str:
        return None

    input_name = path_str.split("/")[-1] if "/" in path_str else path_str

    return VariableDef(
        name=dest_name,
        scope="local",
        line=call_node.start_point[0] + 1,
        file_path=file_path,
        context=current_func.qualified_name,
        source_kind="input_file_connect",
        source_detail=path_str,
        connect_path=path_str,
        connect_input_name=input_name,
        custom_input_func=func_name,
        parent_name=dest_parent,
        value=_node_text(call_node, source).strip()[:120],
        doc_comment=_extract_adjacent_comment(call_node.start_point[0], source),
        full_source=_full_statement_text(call_node, source),
    )


# ------------------------------------------------------------------ #
# Custom input function detection (LUGASI / LUGASIAN template)       #
# ------------------------------------------------------------------ #

def _extract_custom_input_call(
    call_node,
    source: bytes,
    file_path: str,
    current_func: "FunctionDef",
) -> Optional[VariableDef]:
    """
    Detect calls matching the custom input function template:
        LUGASI(&YALLA,   "BALLA", WOW_NA, ...optional_extra_args...)
        LUGASI(&YALLA,   "BALLA", WOW_LI, ...optional_extra_args...)
        LUGASIAN(&YALLA, "BALLA", WOW_NA, ...optional_extra_args...)
        LUGASIAN(&YALLA, "BALLA", WOW_LI, ...optional_extra_args...)

    Returns a VariableDef for the destination variable YALLA with the source
    name "BALLA" stored in connect_input_name (so Variable Flow Mode can
    render it as an upstream source node), or None if no match.

    Edit CUSTOM_INPUT_FUNC_NAMES / CUSTOM_INPUT_ARG_* above to configure.
    """
    func_name = _extract_call_name(call_node, source)
    if not func_name or func_name not in CUSTOM_INPUT_FUNC_NAMES:
        return None

    arg_list = _find_child_by_type(call_node, "argument_list")
    if arg_list is None:
        return None

    args = [
        child for child in arg_list.children
        if child.type not in (",", "(", ")") and child.is_named
    ]

    # Need at least dest + source_name + classifier
    max_needed = max(CUSTOM_INPUT_ARG_DEST, CUSTOM_INPUT_ARG_SOURCE, CUSTOM_INPUT_ARG_CLASSIFY)
    if len(args) <= max_needed:
        return None

    # --- Destination variable (preserve member chain; strip casts/parens/&) ---
    dest_node = args[CUSTOM_INPUT_ARG_DEST]
    dest_txt = _node_text(dest_node, source).strip()
    dest_full, dest_head = _unwrap_dest_full(dest_txt)
    # Validation is on the HEAD identifier (the parent var must be a valid name).
    # `dest_full` may be a member chain like "IMU.x_acc" — that's fine and is
    # what we want to expose as the tracked variable name.
    if not dest_head.isidentifier():
        return None
    dest_name = dest_full
    # Only set parent_name when the dest is actually a member access.
    dest_parent = dest_head if dest_full != dest_head else None

    # --- Quoted source/input name ---
    src_node = args[CUSTOM_INPUT_ARG_SOURCE]
    src_raw = _node_text(src_node, source).strip()
    if src_node.type not in ("string_literal", "concatenated_string"):
        return None
    src_name = src_raw
    if src_name.startswith('"') and src_name.endswith('"'):
        src_name = src_name[1:-1]
    elif src_name.startswith("'") and src_name.endswith("'"):
        src_name = src_name[1:-1]
    if not src_name:
        return None

    # --- Classifier/type (third arg, raw text) ---
    cls_node = args[CUSTOM_INPUT_ARG_CLASSIFY]
    classifier = _node_text(cls_node, source).strip()

    call_text = _node_text(call_node, source).strip()[:120]

    return VariableDef(
        name=dest_name,
        scope="local",
        line=call_node.start_point[0] + 1,
        file_path=file_path,
        context=current_func.qualified_name,
        source_kind="custom_input",
        source_detail=f"{func_name}(\"{src_name}\", {classifier})",
        connect_input_name=src_name,
        connect_path=src_name,
        custom_input_func=func_name,
        custom_input_classifier=classifier,
        parent_name=dest_parent,
        value=call_text,
        doc_comment=_extract_adjacent_comment(call_node.start_point[0], source),
        full_source=_full_statement_text(call_node, source),
    )


# ------------------------------------------------------------------ #
# Member access tracking — var.x / var.x() / var->x                   #
# ------------------------------------------------------------------ #

# Identifiers we never want to emit as a tracked member (operator overloads,
# common stdlib method names that would swamp the data without insight).
_MEMBER_SKIP_NAMES: frozenset[str] = frozenset({
    "operator", "operator=", "operator()", "operator[]", "operator->",
})


def _extract_member_access(
    field_expr,
    source: bytes,
    file_path: str,
    current_func: "FunctionDef",
) -> Optional[VariableDef]:
    """
    Detect a `var.x` / `var->x` access (whether part of a call like `var.x()`
    or a bare read like `printf("%d", var.x)`) and return a VariableDef whose
    `name` is the member identifier `x` and whose `parent_name` is the parent
    expression `var`. Returns None for cases that should not be tracked
    (namespace-qualified `std::endl`, operator overloads, unparseable parent).

    The caller is responsible for dedup-by (function_id, line, member_name).
    """
    if field_expr is None or field_expr.type != "field_expression":
        return None
    field_id = _find_child_by_type(field_expr, "field_identifier")
    if field_id is None:
        return None
    member_name = _node_text(field_id, source).strip()
    if not member_name or not member_name.isidentifier():
        return None
    if member_name in _MEMBER_SKIP_NAMES:
        return None

    # Parent expression = the field_expression text minus ".<member>" / "-><member>"
    full_text = _node_text(field_expr, source).strip()
    # Strip the member suffix off the end (handles both `.x` and `->x`)
    parent_text = full_text
    for sep in (f"->{member_name}", f".{member_name}"):
        if parent_text.endswith(sep):
            parent_text = parent_text[: -len(sep)]
            break
    parent_text = parent_text.strip()
    if not parent_text:
        return None

    # If the next non-whitespace byte after the field_expression is '(', this
    # is a method call (`var.x()`); show the parens to make the form obvious.
    # Source-text peek is robust across tree-sitter Python binding variations
    # in how `.parent` is exposed on freshly-walked nodes.
    is_call = False
    end = field_expr.end_byte
    while end < len(source) and source[end:end+1] in (b" ", b"\t", b"\r", b"\n"):
        end += 1
    if end < len(source) and source[end:end+1] == b"(":
        is_call = True
    access_expr = f"{full_text}()" if is_call else full_text

    return VariableDef(
        name=member_name,
        scope="member",
        line=field_expr.start_point[0] + 1,
        file_path=file_path,
        context=current_func.qualified_name,
        source_kind="member_access",
        source_detail=access_expr[:120],
        parent_name=parent_text[:80],
        value=access_expr[:80],
        full_source=_full_statement_text(field_expr, source),
    )


# ------------------------------------------------------------------ #
# memset / memcpy variable-flow tracking                              #
# ------------------------------------------------------------------ #

def _extract_memory_op(
    call_node,
    source: bytes,
    file_path: str,
    current_func: "FunctionDef",
) -> list[VariableDef]:
    """
    Detect memset/memcpy/memmove calls and create VariableDef entries
    representing the memory operation targets and sources.
    """
    name = _extract_call_name(call_node, source)
    if name not in ("memset", "memcpy", "memmove", "mempcpy", "wmemset", "wmemcpy"):
        return []

    arg_list = _find_child_by_type(call_node, "argument_list")
    if arg_list is None:
        return []

    args = [
        child for child in arg_list.children
        if child.type not in (",", "(", ")") and child.is_named
    ]

    line = call_node.start_point[0] + 1
    context = current_func.qualified_name
    call_text = _node_text(call_node, source).strip()[:120]

    def _base_name(expr_node) -> Optional[str]:
        """Strip deref/address-of/cast/array-index to get simple identifier."""
        txt = _node_text(expr_node, source).strip()
        # Handle pointer member: buf->field or obj.field — take root object
        txt = txt.split("->")[0].split(".")[0]
        txt = txt.lstrip("&*")
        txt = txt.split("[")[0].strip()
        # Strip C cast: (Type)expr
        if txt.startswith("("):
            close = txt.find(")")
            if close != -1:
                txt = txt[close + 1:].lstrip("&* ")
        if txt.isidentifier():
            return txt
        return None

    results: list[VariableDef] = []

    if name == "memset" and args:
        dest = _base_name(args[0])
        if dest:
            results.append(VariableDef(
                name=dest,
                scope="local",
                line=line,
                file_path=file_path,
                context=context,
                source_kind="memory initialization",
                source_detail=f"memset",
                value=call_text,
            ))

    elif name in ("memcpy", "memmove", "mempcpy", "wmemcpy") and len(args) >= 2:
        dest = _base_name(args[0])
        src  = _base_name(args[1])
        src_txt = _node_text(args[1], source).strip()[:60]

        if dest:
            results.append(VariableDef(
                name=dest,
                scope="local",
                line=line,
                file_path=file_path,
                context=context,
                source_kind="memory copy",
                source_detail=f"memcpy from {src_txt}",
                value=call_text,
            ))
        if src:
            dest_txt = _node_text(args[0], source).strip()[:60]
            results.append(VariableDef(
                name=src,
                scope="local",
                line=line,
                file_path=file_path,
                context=context,
                source_kind="memory copy source",
                source_detail=f"memcpy to {dest_txt}",
                value=call_text,
            ))

    return results


def _extract_function_name(declarator_node, source: bytes) -> Optional[str]:
    """
    Recursively unwrap a C declarator to find the function name.
    Handles: function_declarator → identifier / pointer_declarator → ...
    """
    if declarator_node is None:
        return None

    node_type = declarator_node.type

    if node_type == "function_declarator":
        inner = _find_child_by_type(declarator_node, "identifier")
        if inner:
            return _node_text(inner, source)
        inner = _find_child_by_type(declarator_node, "pointer_declarator")
        return _extract_function_name(inner, source)

    if node_type == "identifier":
        return _node_text(declarator_node, source)

    if node_type in ("pointer_declarator", "abstract_pointer_declarator"):
        for child in declarator_node.children:
            result = _extract_function_name(child, source)
            if result:
                return result

    return None


def _extract_parameters(params_node, source: bytes) -> list[Parameter]:
    if params_node is None:
        return []
    params = []
    for child in params_node.children:
        if child.type == "parameter_declaration":
            name_node = None
            type_parts = []
            for part in child.children:
                if part.type in ("type_specifier", "primitive_type", "sized_type_specifier",
                                 "type_qualifier", "struct_specifier", "enum_specifier",
                                 "union_specifier"):
                    type_parts.append(_node_text(part, source))
                elif part.type == "identifier":
                    name_node = part
                elif part.type in ("pointer_declarator", "abstract_pointer_declarator"):
                    inner_id = _find_child_by_type(part, "identifier")
                    if inner_id:
                        name_node = inner_id
                    type_parts.append("*")

            type_hint = " ".join(type_parts).strip() or None
            name = _node_text(name_node, source) if name_node else "_"
            if name not in ("void", "..."):
                params.append(Parameter(name=name, type_hint=type_hint))
    return params


def _extract_return_type(fn_node, source: bytes) -> Optional[str]:
    """Extract the type specifier(s) before the declarator."""
    parts = []
    for child in fn_node.children:
        if child.type in ("type_specifier", "primitive_type", "sized_type_specifier",
                           "type_qualifier", "storage_class_specifier"):
            parts.append(_node_text(child, source))
        elif child.type in ("function_declarator", "pointer_declarator", "identifier"):
            break
    return " ".join(parts).strip() or None


def _extract_call_name(call_node, source: bytes) -> Optional[str]:
    """Extract the function name from a call_expression node."""
    func_child = _find_child_by_type(call_node, "identifier")
    if func_child:
        return _node_text(func_child, source)
    field = _find_child_by_type(call_node, "field_expression")
    if field:
        field_id = _find_child_by_type(field, "field_identifier")
        if field_id:
            return _node_text(field_id, source)
    return None


def _extract_call_args(call_node, source: bytes) -> list[str]:
    args = []
    arg_list = _find_child_by_type(call_node, "argument_list")
    if arg_list:
        for child in arg_list.children:
            if child.type not in (",", "(", ")"):
                text = _node_text(child, source).strip()
                if text:
                    args.append(text)
    return args


# These are re-exported for cpp_parser to import
def _extract_declarator_ident(node, source: bytes) -> Optional[str]:
    """Find the innermost ``identifier`` name from any declarator subtree.

    Works for ``pointer_declarator``, ``parenthesized_declarator``,
    ``function_declarator``, ``reference_declarator``, ``array_declarator``, etc.
    Returns the bare variable name, not the full declarator text.
    """
    if node.type in ("identifier", "field_identifier"):
        return _node_text(node, source).strip()
    for child in node.children:
        if child.is_named:
            result = _extract_declarator_ident(child, source)
            if result:
                return result
    return None


def _collect_fp_assignments(body_node, source: bytes) -> dict[str, list[str]]:
    """Walk a function body and collect function-pointer variable assignments.

    Returns ``{var_name: [assigned_function_names]}`` for patterns like::

        void (*fp)(void) = target;      # init_declarator
        fp = target;                     # assignment_expression, both identifiers
        s.callback = target;             # assignment via field_expression

    Stops descent at nested ``function_definition`` nodes (lambdas / nested fns).
    This drives idea C-2 (pointer-target resolution).
    """
    result: dict[str, list[str]] = {}

    stack = [body_node]
    while stack:
        node = stack.pop()

        if node.type == "init_declarator":
            # Locate the '=' sign between declarator and value.
            eq_idx = next(
                (i for i, c in enumerate(node.children) if _node_text(c, source) == "="),
                -1,
            )
            if eq_idx != -1:
                decl_side = node.children[:eq_idx]
                val_side = node.children[eq_idx + 1:]
                var_name: Optional[str] = None
                for c in decl_side:
                    if c.is_named:
                        var_name = _extract_declarator_ident(c, source)
                        if var_name:
                            break
                for c in val_side:
                    if c.is_named and c.type == "identifier":
                        target = _node_text(c, source).strip()
                        if var_name and target and var_name != target:
                            result.setdefault(var_name, [])
                            if target not in result[var_name]:
                                result[var_name].append(target)
                        break

        elif node.type == "assignment_expression":
            named = [c for c in node.children if c.is_named]
            if len(named) >= 2:
                lhs, rhs = named[0], named[-1]
                if rhs.type == "identifier":
                    target = _node_text(rhs, source).strip()
                    if lhs.type == "identifier":
                        var = _node_text(lhs, source).strip()
                        if var and target and var != target:
                            result.setdefault(var, [])
                            if target not in result[var]:
                                result[var].append(target)
                    elif lhs.type == "field_expression":
                        field_id = _find_child_by_type(lhs, "field_identifier")
                        if field_id:
                            field_name = _node_text(field_id, source).strip()
                            if field_name and target and field_name != target:
                                result.setdefault(field_name, [])
                                if target not in result[field_name]:
                                    result[field_name].append(target)

        for child in node.children:
            if child.type != "function_definition":
                stack.append(child)

    return result


def get_language(lang_name: str) -> TSLanguage:
    if lang_name == "c":
        import tree_sitter_c as _tsc
        return TSLanguage(_tsc.language())
    elif lang_name == "cpp":
        import tree_sitter_cpp as _tscpp
        return TSLanguage(_tscpp.language())
    raise ValueError(f"Unknown language: {lang_name}")


def ts_get_parser(lang_name: str) -> TSParser:
    lang = get_language(lang_name)
    return TSParser(lang)


def _maybe_expand_macros(source_bytes: bytes, config: Config) -> bytes:
    """Run the simple #define macro-expansion pre-pass (idea C-1) when enabled.

    Decodes with latin-1 (lossless 1:1 byte mapping) so the expander operates on
    text, then re-encodes. Line count is preserved by the expander, keeping all
    downstream line numbers / node ids stable. Any failure degrades gracefully to
    the original bytes — parsing must never hard-crash.
    """
    if not getattr(getattr(config, "parser", None), "expand_macros", True):
        return source_bytes
    try:
        text = source_bytes.decode("latin-1")
        expanded = expand_c_macros(text)
        if expanded is text:
            return source_bytes
        return expanded.encode("latin-1")
    except Exception:
        return source_bytes


class CParser(BaseParser):
    LANGUAGE_NAME = "c"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        _check_available()
        # Tree-sitter Parser is not thread-safe — give each worker thread its own.
        import threading
        self._tls = threading.local()

    def _get_parser(self) -> "TSParser":
        p = getattr(self._tls, "parser", None)
        if p is None:
            p = ts_get_parser(self.LANGUAGE_NAME)
            self._tls.parser = p
        return p

    def parse_file(
        self, path: Path
    ) -> tuple[list[FunctionDef], list[CallRelationship]]:
        source_bytes = path.read_bytes()
        source_bytes = _maybe_expand_macros(source_bytes, self.config)
        tree = self._get_parser().parse(source_bytes)

        functions: list[FunctionDef] = []
        calls: list[CallRelationship] = []
        file_variables: list[VariableDef] = []

        self._walk(tree.root_node, source_bytes, str(path), functions, calls,
                   current_func=None, namespace_stack=[],
                   file_variables=file_variables)

        _attach_referenced_globals(functions, file_variables, source_bytes)

        return functions, calls

    def _walk(
        self,
        node,
        source: bytes,
        file_path: str,
        functions: list[FunctionDef],
        calls: list[CallRelationship],
        current_func: Optional[FunctionDef],
        namespace_stack: list[str],
        file_variables: list[VariableDef],
    ) -> None:
        context = "::".join(namespace_stack) if namespace_stack else None

        if node.type in ("preproc_def", "preproc_function_def") and current_func is None:
            constant = _extract_preproc_constant(node, source, file_path)
            if constant:
                file_variables.append(constant)

        if node.type in ("declaration", "field_declaration"):
            context = "::".join(namespace_stack) if namespace_stack else None
            variables = _extract_variables_from_declaration(
                node,
                source,
                "field" if node.type == "field_declaration" else ("local" if current_func else "global"),
                file_path,
                context=context,
            )
            if current_func:
                current_func.variables.extend(variables)
            else:
                file_variables.extend(variables)

        if node.type == "assignment_expression" and current_func:
            assigned = _extract_assignment_variable(
                node,
                source,
                file_path,
                "local",
                context=current_func.qualified_name,
            )
            if assigned:
                current_func.variables.append(assigned)

        if node.type == "function_definition":
            fn = self._extract_function(node, source, file_path, namespace_stack)
            if fn:
                functions.append(fn)
                body = _find_child_by_type(node, "compound_statement")
                if body:
                    fn._fp_targets = _collect_fp_assignments(body, source)  # type: ignore[attr-defined]
                    self._walk(body, source, file_path, functions, calls,
                               current_func=fn, namespace_stack=namespace_stack,
                               file_variables=file_variables)
                    _detect_dead_variables(fn, body, source)
                return

        elif node.type == "call_expression" and current_func:
            name = _extract_call_name(node, source)
            if name:
                fp_targets = getattr(current_func, "_fp_targets", {})
                if name in fp_targets:
                    # Emit one call per known pointer target (idea C-2)
                    for target in fp_targets[name]:
                        calls.append(CallRelationship(
                            caller_id=current_func.node_id,
                            callee_name=target,
                            call_file=file_path,
                            call_line=node.start_point[0] + 1,
                            call_args=_extract_call_args(node, source),
                            resolution_confidence=ResolutionConfidence.HEURISTIC,
                            resolution_hint=f"function-pointer target (via {name})",
                        ))
                else:
                    calls.append(CallRelationship(
                        caller_id=current_func.node_id,
                        callee_name=name,
                        call_file=file_path,
                        call_line=node.start_point[0] + 1,
                        call_args=_extract_call_args(node, source),
                        resolution_confidence=ResolutionConfidence.UNRESOLVED,
                    ))
            # .connect("PATH/input_var", ...) pattern — method-call form
            conn_var = _extract_connect_call(node, source, file_path, current_func)
            if conn_var:
                current_func.variables.append(conn_var)
            # connect2(...) — free-function form of .connect
            conn_free_var = _extract_connect_free_func_call(node, source, file_path, current_func)
            if conn_free_var:
                current_func.variables.append(conn_free_var)
            # lugasi / lugasian (and 2-variants) custom input function template
            custom_var = _extract_custom_input_call(node, source, file_path, current_func)
            if custom_var:
                current_func.variables.append(custom_var)
            # memset / memcpy tracking
            mem_vars = _extract_memory_op(node, source, file_path, current_func)
            current_func.variables.extend(mem_vars)

        # Member-access tracking (var.x / var.x() / var->x). Fires for every
        # field_expression inside a function body. We dedup by (line, member)
        # via an ad-hoc set attached to the FunctionDef so the same source
        # location doesn't yield multiple identical entries.
        if node.type == "field_expression" and current_func:
            seen = getattr(current_func, "_member_access_seen", None)
            if seen is None:
                seen = set()
                current_func._member_access_seen = seen   # type: ignore[attr-defined]
            field_id = _find_child_by_type(node, "field_identifier")
            if field_id is not None:
                m_name = _node_text(field_id, source).strip()
                key = (node.start_point[0] + 1, m_name)
                if m_name and key not in seen:
                    seen.add(key)
                    mv = _extract_member_access(node, source, file_path, current_func)
                    if mv:
                        current_func.variables.append(mv)

        for child in node.children:
            self._walk(child, source, file_path, functions, calls,
                       current_func=current_func, namespace_stack=namespace_stack,
                       file_variables=file_variables)

    def _extract_function(
        self, node, source: bytes, file_path: str, namespace_stack: list[str]
    ) -> Optional[FunctionDef]:
        declarator = _find_child_by_type(node, "function_declarator")
        if declarator is None:
            ptr = _find_child_by_type(node, "pointer_declarator")
            if ptr:
                declarator = _find_child_by_type(ptr, "function_declarator")
        if declarator is None:
            return None

        name = _extract_function_name(declarator, source)
        if not name:
            return None

        ns = "::".join(namespace_stack) if namespace_stack else None
        qualified = f"{ns}::{name}" if ns else name

        params_node = _find_child_by_type(declarator, "parameter_list")
        params = _extract_parameters(params_node, source)
        return_type = _extract_return_type(node, source)

        return FunctionDef(
            name=name,
            qualified_name=qualified,
            language=Language.C,
            file_path=file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            parameters=params,
            return_type=return_type,
            parent=ns,
            is_external=False,
            is_method=False,
            func_type="function",
        )
