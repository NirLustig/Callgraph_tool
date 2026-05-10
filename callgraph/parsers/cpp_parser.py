"""
C++ source parser using Tree-sitter.
Extends the C parser with class/namespace/method handling and qualified identifiers.

Limitations (inherited from Tree-sitter):
  - Preprocessor macros are not expanded.
  - Template instantiations are captured by name but parameters are opaque strings.
  - Virtual dispatch cannot be resolved statically.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import Config
from ..models import CallRelationship, FunctionDef, Language, Parameter, ResolutionConfidence
from .base import BaseParser
from .c_parser import (
    _check_available,
    _dedup_variables,
    _extract_assignment_variable,
    _extract_call_args,
    _extract_parameters,
    _extract_preproc_constant,
    _extract_variables_from_declaration,
    _find_child_by_type,
    _find_children_by_type,
    _node_text,
    ts_get_parser,
    get_language,
)


def _extract_qualified_name(node, source: bytes) -> Optional[str]:
    """Handle qualified_identifier nodes like Foo::Bar::method."""
    if node.type == "qualified_identifier":
        parts = []
        for child in node.children:
            if child.type not in ("::",):
                text = _node_text(child, source)
                if text.strip():
                    parts.append(text)
        return "::".join(parts)
    if node.type == "identifier":
        return _node_text(node, source)
    if node.type == "destructor_name":
        return _node_text(node, source)
    if node.type == "operator_name":
        return _node_text(node, source)
    return None


def _extract_cpp_call_name(call_node, source: bytes) -> Optional[str]:
    """Extract the function name from a C++ call_expression."""
    for child in call_node.children:
        if child.type in ("identifier", "qualified_identifier"):
            name = _extract_qualified_name(child, source)
            if name:
                return name.split("::")[-1]   # simple name for resolution
        if child.type == "field_expression":
            field_id = _find_child_by_type(child, "field_identifier")
            if field_id:
                return _node_text(field_id, source)
        if child.type == "template_function":
            name_node = _find_child_by_type(child, "identifier")
            if name_node:
                return _node_text(name_node, source)
    return None


def _extract_cpp_return_type(fn_node, source: bytes) -> Optional[str]:
    """Extract return type from C++ function_definition, handling auto."""
    parts = []
    for child in fn_node.children:
        if child.type in ("type_specifier", "primitive_type", "sized_type_specifier",
                           "type_qualifier", "storage_class_specifier",
                           "qualified_identifier", "template_type",
                           "auto"):
            parts.append(_node_text(child, source))
        elif child.type in ("function_declarator", "pointer_declarator",
                             "reference_declarator", "identifier"):
            break
    return " ".join(parts).strip() or None


class CppParser(BaseParser):
    LANGUAGE_NAME = "cpp"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        _check_available()
        self._ts_lang = get_language(self.LANGUAGE_NAME)
        self._ts_parser = ts_get_parser(self.LANGUAGE_NAME)

    def parse_file(
        self, path: Path
    ) -> tuple[list[FunctionDef], list[CallRelationship]]:
        source_bytes = path.read_bytes()
        tree = self._ts_parser.parse(source_bytes)

        functions: list[FunctionDef] = []
        calls: list[CallRelationship] = []
        file_variables = []

        self._walk(
            tree.root_node, source_bytes, str(path),
            functions, calls,
            current_func=None,
            class_stack=[],
            namespace_stack=[],
            file_variables=file_variables,
        )
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
        class_stack: list[str],
        namespace_stack: list[str],
        file_variables: list,
    ) -> None:
        node_type = node.type

        context_parts = namespace_stack + class_stack
        context = "::".join(context_parts) if context_parts else None

        if node_type in ("preproc_def", "preproc_function_def") and current_func is None:
            constant = _extract_preproc_constant(node, source, file_path)
            if constant:
                file_variables.append(constant)

        if node_type in ("declaration", "field_declaration"):
            context_parts = namespace_stack + class_stack
            variables = _extract_variables_from_declaration(
                node,
                source,
                "field" if node_type == "field_declaration" else ("local" if current_func else "global"),
                file_path,
                context=context,
            )
            if current_func:
                current_func.variables.extend(variables)
            else:
                file_variables.extend(variables)

        if node_type == "assignment_expression" and current_func:
            assigned = _extract_assignment_variable(
                node,
                source,
                file_path,
                "local",
                context=current_func.qualified_name,
            )
            if assigned:
                current_func.variables.append(assigned)

        if node_type == "namespace_definition":
            ns_name = _find_child_by_type(node, "namespace_identifier") or _find_child_by_type(node, "identifier")
            name_str = _node_text(ns_name, source) if ns_name else "anonymous"
            namespace_stack.append(name_str)
            ns_body = _find_child_by_type(node, "declaration_list")
            if ns_body:
                for child in ns_body.children:
                    self._walk(child, source, file_path, functions, calls,
                               current_func, class_stack, namespace_stack,
                               file_variables)
            namespace_stack.pop()
            return

        if node_type in ("class_specifier", "struct_specifier"):
            class_name = _find_child_by_type(node, "type_identifier")
            name_str = _node_text(class_name, source) if class_name else "anonymous"
            class_stack.append(name_str)
            body = _find_child_by_type(node, "field_declaration_list")
            if body:
                for child in body.children:
                    self._walk(child, source, file_path, functions, calls,
                               current_func, class_stack, namespace_stack,
                               file_variables)
            class_stack.pop()
            return

        if node_type == "function_definition":
            fn = self._extract_function(node, source, file_path,
                                         class_stack, namespace_stack)
            if fn:
                functions.append(fn)
                body = _find_child_by_type(node, "compound_statement")
                if body:
                    self._walk(body, source, file_path, functions, calls,
                               fn, class_stack, namespace_stack,
                               file_variables)
            return

        if node_type == "call_expression" and current_func:
            name = _extract_cpp_call_name(node, source)
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
                       current_func, class_stack, namespace_stack,
                       file_variables)

    def _extract_function(
        self,
        node,
        source: bytes,
        file_path: str,
        class_stack: list[str],
        namespace_stack: list[str],
    ) -> Optional[FunctionDef]:
        # Find function_declarator (may be nested in pointer/reference declarator)
        declarator = self._find_function_declarator(node)
        if declarator is None:
            return None

        # Extract the function name (handles qualified names like Foo::bar)
        name_child = None
        for child in declarator.children:
            if child.type in ("identifier", "qualified_identifier",
                               "destructor_name", "operator_name"):
                name_child = child
                break
            if child.type == "field_identifier":
                name_child = child
                break

        if name_child is None:
            return None

        full_name = _extract_qualified_name(name_child, source)
        if not full_name:
            return None

        # Simple name (last segment) and qualified name (namespace + class + name)
        simple_name = full_name.split("::")[-1]
        context_parts = namespace_stack + class_stack
        if "::" in full_name:
            # Already qualified in source (out-of-class definition like Foo::bar)
            qualified = full_name
            parent = "::".join(full_name.split("::")[:-1]) or None
        else:
            qualified = "::".join(context_parts + [simple_name]) if context_parts else simple_name
            parent = "::".join(context_parts) if context_parts else None

        params_node = _find_child_by_type(declarator, "parameter_list")
        params = _extract_parameters(params_node, source)
        return_type = _extract_cpp_return_type(node, source)

        is_method = bool(class_stack) or ("::" in full_name and not full_name.startswith("::"))

        # Determine function type
        qparts = full_name.split("::")
        if simple_name.startswith("~"):
            ftype = "destructor"
        elif simple_name.startswith("operator"):
            ftype = "operator"
        elif (class_stack and simple_name == class_stack[-1]) or \
             (len(qparts) >= 2 and qparts[-1] == qparts[-2]):
            ftype = "constructor"
        elif is_method:
            ftype = "method"
        else:
            ftype = "function"

        return FunctionDef(
            name=simple_name,
            qualified_name=qualified,
            language=Language.CPP,
            file_path=file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            parameters=params,
            return_type=return_type,
            parent=parent,
            is_external=False,
            is_method=is_method,
            func_type=ftype,
        )

    def _find_function_declarator(self, node):
        """Recursively find function_declarator inside pointer/reference wrappers."""
        for child in node.children:
            if child.type == "function_declarator":
                return child
            if child.type in ("pointer_declarator", "reference_declarator",
                               "abstract_pointer_declarator"):
                result = self._find_function_declarator(child)
                if result:
                    return result
        return None
