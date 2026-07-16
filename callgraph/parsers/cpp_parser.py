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
from ..models import CallRelationship, ClassInfo, FunctionDef, Language, Parameter, ResolutionConfidence
from .base import BaseParser
from .c_parser import (
    _check_available,
    _attach_referenced_globals,
    _collect_fp_assignments,
    _dedup_variables,
    _detect_dead_variables,
    _extract_assignment_variable,
    _extract_call_args,
    _extract_connect_call,
    _extract_declarator_ident,
    _extract_member_access,
    _extract_connect_free_func_call,
    _extract_custom_input_call,
    _extract_memory_op,
    _extract_parameters,
    _extract_preproc_constant,
    _extract_variables_from_declaration,
    _find_child_by_type,
    _find_children_by_type,
    _maybe_expand_macros,
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
    # field_identifier is used for inline class method names inside field_declaration_list
    if node.type in ("identifier", "field_identifier"):
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


# ---------------------------------------------------------------------------- #
# Class hierarchy capture (idea CPP-1 — virtual / override resolution)          #
# ---------------------------------------------------------------------------- #

def _has_virtual_marker(member_node, source: bytes) -> bool:
    """True if a class member (field_declaration / function_definition) is marked
    ``virtual`` or carries an ``override`` / ``final`` specifier.

    Searches the member node but never descends into the function body
    (``compound_statement``), so call sites inside the body can't trigger a
    false positive.
    """
    stack = list(member_node.children)
    while stack:
        c = stack.pop()
        if c.type == "compound_statement":
            continue
        if c.type in ("virtual", "virtual_function_specifier", "virtual_specifier"):
            return True
        stack.extend(c.children)
    return False


def _member_method_name(member_node, source: bytes) -> Optional[str]:
    """Return the declared method simple name from a class-member declaration or
    definition, or ``None`` if the member is not a function."""
    declarator = _find_child_by_type(member_node, "function_declarator")
    if declarator is None:
        for child in member_node.children:
            if child.type in ("pointer_declarator", "reference_declarator"):
                declarator = _find_child_by_type(child, "function_declarator")
                if declarator:
                    break
    if declarator is None:
        return None
    for child in declarator.children:
        if child.type in ("identifier", "field_identifier"):
            return _node_text(child, source).strip()
        if child.type == "qualified_identifier":
            return _node_text(child, source).split("::")[-1].strip()
        if child.type == "destructor_name":
            return _node_text(child, source).strip()
    return None


def _extract_base_classes(class_node, source: bytes) -> set:
    """Collect base-class names from a class_specifier's ``base_class_clause``."""
    bases: set = set()
    clause = _find_child_by_type(class_node, "base_class_clause")
    if clause is None:
        return bases
    for child in clause.children:
        if child.type in ("type_identifier", "qualified_identifier", "template_type"):
            text = _node_text(child, source).strip()
            if text:
                bases.add(text)
    return bases


def _collect_virtual_method_names(class_node, source: bytes) -> set:
    """Simple names of every method in a class body marked virtual/override/final."""
    names: set = set()
    body = _find_child_by_type(class_node, "field_declaration_list")
    if body is None:
        return names
    for member in body.children:
        if member.type not in ("field_declaration", "function_definition",
                                "declaration", "template_declaration"):
            continue
        target = member
        if member.type == "template_declaration":
            target = _find_child_by_type(member, "function_definition") or \
                     _find_child_by_type(member, "field_declaration") or member
        if _has_virtual_marker(target, source):
            name = _member_method_name(target, source)
            if name:
                names.add(name.lstrip("~"))
    return names


class CppParser(BaseParser):
    LANGUAGE_NAME = "cpp"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        _check_available()
        self._ts_lang = get_language(self.LANGUAGE_NAME)
        # Tree-sitter Parser is not thread-safe — give each worker thread its own.
        import threading
        self._tls = threading.local()
        # Aggregated class hierarchy (idea CPP-1). Populated per-file, merged here
        # under a lock so the builder can resolve virtual/override dispatch across
        # files (class declared in a header, methods defined in a .cpp).
        self.class_registry: dict[str, ClassInfo] = {}
        self._registry_lock = threading.Lock()
        # Type Nodes Mode: shared struct/union/enum/typedef/class registry.
        self.type_registry: dict = {}
        self._type_lock = threading.Lock()

    def _get_parser(self):
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
        file_variables = []
        self._tls.file_classes = {}   # per-file class hierarchy capture

        self._walk(
            tree.root_node, source_bytes, str(path),
            functions, calls,
            current_func=None,
            class_stack=[],
            namespace_stack=[],
            file_variables=file_variables,
        )
        _attach_referenced_globals(functions, file_variables, source_bytes)

        # Merge this file's class records into the shared registry.
        file_classes = getattr(self._tls, "file_classes", {})
        if file_classes:
            with self._registry_lock:
                for qual, info in file_classes.items():
                    master = self.class_registry.get(qual)
                    if master is None:
                        self.class_registry[qual] = ClassInfo(
                            name=qual,
                            bases=set(info.bases),
                            virtual_methods=set(info.virtual_methods),
                        )
                    else:
                        master.bases.update(info.bases)
                        master.virtual_methods.update(info.virtual_methods)
        self._tls.file_classes = {}

        # Type Nodes Mode: collect struct/union/enum/typedef/class definitions.
        if getattr(self.config.output, "type_mode", True):
            try:
                from ._type_extract import extract_types_from_tree
                type_defs = extract_types_from_tree(
                    tree.root_node, source_bytes, str(path), self.LANGUAGE_NAME
                )
                if type_defs:
                    with self._type_lock:
                        for td in type_defs:
                            self.type_registry.setdefault(td.type_id, td)
            except Exception:
                pass   # type extraction never breaks the parse (graceful degradation)

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
            # Record class hierarchy for virtual/override resolution (idea CPP-1).
            body = _find_child_by_type(node, "field_declaration_list")
            if body is not None and name_str != "anonymous":
                class_qual = "::".join(namespace_stack + class_stack)
                reg = getattr(self._tls, "file_classes", None)
                if reg is not None:
                    info = reg.get(class_qual)
                    if info is None:
                        info = ClassInfo(name=class_qual)
                        reg[class_qual] = info
                    info.bases.update(_extract_base_classes(node, source))
                    info.virtual_methods.update(_collect_virtual_method_names(node, source))
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
                    fn._fp_targets = _collect_fp_assignments(body, source)  # type: ignore[attr-defined]
                    self._walk(body, source, file_path, functions, calls,
                               fn, class_stack, namespace_stack,
                               file_variables)
                    _detect_dead_variables(fn, body, source)
            return

        if node_type == "call_expression" and current_func:
            name = _extract_cpp_call_name(node, source)
            if name:
                fp_targets = getattr(current_func, "_fp_targets", {})
                if name in fp_targets:
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

        # Member-access tracking (mirrors CParser._walk; see c_parser.py for the
        # dedup rationale).
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
            is_virtual=_has_virtual_marker(node, source),
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
