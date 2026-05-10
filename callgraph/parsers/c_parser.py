"""
C source parser using Tree-sitter.

Known limitation: preprocessor macros are not expanded. Functions defined
entirely via macros (e.g. DECLARE_HANDLER(foo, int)) will not be detected.
For macro-heavy codebases, consider the libclang backend instead.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import Config
from ..models import CallRelationship, FunctionDef, Language, Parameter, ResolutionConfidence, VariableDef
from .base import BaseParser

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


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


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
) -> VariableDef:
    source_kind, source_detail, inferred_type = _classify_value_node(value_node, source)
    return VariableDef(
        name=name,
        scope=kind,
        type_hint=type_hint or inferred_type,
        value=value,
        line=line,
        file_path=file_path,
        context=context,
        source_kind=source_kind,
        source_detail=source_detail,
    )


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


class CParser(BaseParser):
    LANGUAGE_NAME = "c"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        _check_available()
        self._ts_parser = ts_get_parser(self.LANGUAGE_NAME)

    def parse_file(
        self, path: Path
    ) -> tuple[list[FunctionDef], list[CallRelationship]]:
        source_bytes = path.read_bytes()
        tree = self._ts_parser.parse(source_bytes)

        functions: list[FunctionDef] = []
        calls: list[CallRelationship] = []
        file_variables: list[VariableDef] = []

        self._walk(tree.root_node, source_bytes, str(path), functions, calls,
                   current_func=None, namespace_stack=[],
                   file_variables=file_variables)

        for fn in functions:
            fn.variables = _dedup_variables(file_variables + fn.variables)

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
                    self._walk(body, source, file_path, functions, calls,
                               current_func=fn, namespace_stack=namespace_stack,
                               file_variables=file_variables)
                return

        elif node.type == "call_expression" and current_func:
            name = _extract_call_name(node, source)
            if name:
                calls.append(CallRelationship(
                    caller_id=current_func.node_id,
                    callee_name=name,
                    call_file=file_path,
                    call_line=node.start_point[0] + 1,
                    call_args=_extract_call_args(node, source),
                    resolution_confidence=ResolutionConfidence.UNRESOLVED,
                ))

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
