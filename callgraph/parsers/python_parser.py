"""
Python source parser using the built-in `ast` module.
Handles functions, async functions, methods, classes, and call relationships.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Optional

from ..config import Config
from ..models import CallRelationship, FunctionDef, Language, Parameter, ResolutionConfidence, VariableDef
from .base import BaseParser


def _annotation_to_str(node: Optional[ast.expr]) -> Optional[str]:
    """Convert an AST annotation node to a readable string."""
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _args_to_str(node: ast.Call) -> list[str]:
    args = []
    try:
        for arg in node.args:
            args.append(ast.unparse(arg))
        for kw in node.keywords:
            key = kw.arg or "**"
            args.append(f"{key}={ast.unparse(kw.value)}")
    except Exception:
        pass
    return args


def _extract_call_name(func_node: ast.expr) -> Optional[str]:
    """Extract the function name from a Call node's func attribute."""
    if isinstance(func_node, ast.Name):
        return func_node.id
    if isinstance(func_node, ast.Attribute):
        # obj.method — return "method" (we can't statically resolve 'obj' type)
        return func_node.attr
    if isinstance(func_node, ast.Subscript):
        return _extract_call_name(func_node.value)
    return None


class _FunctionContext:
    """Stack frame tracking the current function being visited."""

    def __init__(
        self,
        node_id: str,
        name: str,
        qualified_name: str,
        parent_class: Optional[str],
        file_path: str,
    ) -> None:
        self.node_id = node_id
        self.name = name
        self.qualified_name = qualified_name
        self.parent_class = parent_class
        self.file_path = file_path
        self.var_assignments: dict[str, str] = {}
        self.variables: list[VariableDef] = []
        self.global_names: set[str] = set()


def _value_to_str(node: Optional[ast.AST]) -> Optional[str]:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _attribute_chain(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _attribute_chain(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _call_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Call):
        return _attribute_chain(node.func)
    return None


def _is_os_environ(node: ast.AST) -> bool:
    return _attribute_chain(node) == "os.environ"


def _is_environment_value(node: Optional[ast.AST]) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
        return True
    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name in {"os.getenv", "getenv"}:
            return True
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            return _is_os_environ(node.func.value)
    return False


def _is_environment_target(node: ast.AST) -> bool:
    return isinstance(node, ast.Subscript) and _is_os_environ(node.value)


def _iter_assignment_targets(target: ast.AST) -> list[tuple[str, Optional[str]]]:
    if isinstance(target, ast.Name):
        return [(target.id, None)]
    if isinstance(target, ast.Attribute):
        name = _value_to_str(target)
        return [(name, "field")] if name else []
    if _is_environment_target(target):
        name = _value_to_str(target)
        return [(name, "environment")] if name else []
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[tuple[str, Optional[str]]] = []
        for elt in target.elts:
            names.extend(_iter_assignment_targets(elt))
        return names
    return []


def _is_literal_value(node: Optional[ast.AST]) -> bool:
    return isinstance(node, ast.Constant)


def _is_constant_name(name: str) -> bool:
    base = name.split(".")[-1]
    letters = [ch for ch in base if ch.isalpha()]
    return bool(letters) and base.upper() == base


def _is_final_annotation(type_hint: Optional[str]) -> bool:
    return bool(type_hint and "Final" in type_hint)


def _infer_python_type(node: Optional[ast.AST], type_hint: Optional[str]) -> Optional[str]:
    if type_hint:
        return type_hint
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return "bool"
        if isinstance(node.value, int):
            return "int"
        if isinstance(node.value, float):
            return "float"
        if isinstance(node.value, str):
            return "str"
        if node.value is None:
            return "None"
        return type(node.value).__name__
    if isinstance(node, ast.List):
        return "list"
    if isinstance(node, ast.Dict):
        return "dict"
    if isinstance(node, ast.Set):
        return "set"
    if isinstance(node, ast.Tuple):
        return "tuple"
    if isinstance(node, ast.Call):
        name = _call_name(node) or ""
        simple = name.split(".")[-1]
        if simple and simple[0].isupper():
            return simple
    return None


def _classify_python_value(node: Optional[ast.AST]) -> tuple[Optional[str], Optional[str], bool]:
    if node is None:
        return "declaration", None, False
    if _is_environment_value(node):
        return "environment lookup", _call_name(node) or "os.environ", False
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return "hard-coded boolean", "literal", False
        if isinstance(node.value, (int, float, complex)):
            return "hard-coded number", "literal", False
        if isinstance(node.value, str):
            return "hard-coded string", "literal", False
        if node.value is None:
            return "null literal", "literal", False
        return "literal", "literal", False
    if isinstance(node, (ast.List, ast.Dict, ast.Set, ast.Tuple, ast.ListComp, ast.DictComp, ast.SetComp)):
        return "dynamic allocation", type(node).__name__, True
    if isinstance(node, ast.Call):
        name = _call_name(node)
        simple = (name or "").split(".")[-1]
        alloc_like = {"list", "dict", "set", "tuple", "bytearray", "bytes", "object"}
        if simple in alloc_like or (simple and simple[0].isupper()):
            return "dynamic allocation", name, True
        return "function call", name, False
    if isinstance(node, ast.Subscript):
        return "lookup", _attribute_chain(node.value), False
    if isinstance(node, ast.Attribute):
        return "field/property access", _attribute_chain(node), False
    if isinstance(node, (ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp)):
        return "calculated expression", type(node).__name__, False
    return "expression", type(node).__name__, False


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, file_path: str, config: Config) -> None:
        self.file_path = file_path
        self.config = config
        self.functions: list[FunctionDef] = []
        self.calls: list[CallRelationship] = []
        self._class_stack: list[str] = []
        self._func_stack: list[_FunctionContext] = []
        self._module_name = Path(file_path).stem
        self._module_variables: list[VariableDef] = []
        self._class_variables: dict[str, list[VariableDef]] = {}

    # ------------------------------------------------------------------ #
    # Class and function entry/exit                                        #
    # ------------------------------------------------------------------ #

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter_function(node)

    def _enter_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parent_class = self._class_stack[-1] if self._class_stack else None
        parent_module = self._module_name

        if parent_class:
            qualified = f"{parent_class}.{node.name}"
        elif self._func_stack:
            qualified = f"{self._func_stack[-1].qualified_name}.{node.name}"
        else:
            qualified = node.name

        params = self._extract_params(node)
        return_type = _annotation_to_str(node.returns)
        docstring = ast.get_docstring(node)

        if isinstance(node, ast.AsyncFunctionDef):
            ftype = "async function"
        elif node.name == "__init__":
            ftype = "constructor"
        elif node.name == "__del__":
            ftype = "destructor"
        elif node.name.startswith("__") and node.name.endswith("__"):
            ftype = "special method"
        elif bool(parent_class):
            ftype = "method"
        elif self._func_stack:
            ftype = "nested function"
        else:
            ftype = "function"

        fn = FunctionDef(
            name=node.name,
            qualified_name=qualified,
            language=Language.PYTHON,
            file_path=self.file_path,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            parameters=params,
            return_type=return_type,
            parent=parent_class or parent_module,
            is_external=False,
            is_method=bool(parent_class),
            func_type=ftype,
            docstring=docstring,
        )
        self.functions.append(fn)

        ctx = _FunctionContext(
            node_id=fn.node_id,
            name=node.name,
            qualified_name=qualified,
            parent_class=parent_class,
            file_path=self.file_path,
        )
        self._func_stack.append(ctx)
        self.generic_visit(node)
        self._func_stack.pop()
        fn.variables.extend(ctx.variables)

        # Apply tracked variables collected during visit
        if self.config.variables.track and ctx.var_assignments:
            for fn_def in self.functions:
                if fn_def.node_id == fn.node_id:
                    fn_def.tracked_vars = {
                        k: v for k, v in ctx.var_assignments.items()
                        if k in self.config.variables.names
                    }
                    break

    def _extract_params(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[Parameter]:
        params = []
        args = node.args
        all_args = args.posonlyargs + args.args + args.kwonlyargs
        if args.vararg:
            all_args.append(args.vararg)
        if args.kwarg:
            all_args.append(args.kwarg)

        for arg in all_args:
            if arg.arg in ("self", "cls"):
                continue
            params.append(Parameter(
                name=arg.arg,
                type_hint=_annotation_to_str(arg.annotation),
            ))
        return params

    # ------------------------------------------------------------------ #
    # Call detection                                                       #
    # ------------------------------------------------------------------ #

    def visit_Call(self, node: ast.Call) -> None:
        if self._func_stack:
            callee_name = _extract_call_name(node.func)
            if callee_name:
                call = CallRelationship(
                    caller_id=self._func_stack[-1].node_id,
                    callee_name=callee_name,
                    call_file=self.file_path,
                    call_line=node.lineno,
                    call_args=_args_to_str(node),
                    resolution_confidence=ResolutionConfidence.UNRESOLVED,
                )
                self.calls.append(call)
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        if self._func_stack:
            ctx = self._func_stack[-1]
            ctx.global_names.update(node.names)
            for name in node.names:
                ctx.variables.append(VariableDef(
                    name=name,
                    scope="global",
                    line=node.lineno,
                    file_path=self.file_path,
                    context=self._module_name,
                    source_kind="global declaration",
                    source_detail="global",
                ))
        self.generic_visit(node)

    # ------------------------------------------------------------------ #
    # Variable tracking (single-scope assignment, best-effort)            #
    # ------------------------------------------------------------------ #

    def visit_Assign(self, node: ast.Assign) -> None:
        self._record_assignment(
            node.targets,
            value=node.value,
            line=node.lineno,
            type_hint=None,
        )
        if self._func_stack and self.config.variables.track:
            ctx = self._func_stack[-1]
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in self.config.variables.names:
                    try:
                        ctx.var_assignments[target.id] = ast.unparse(node.value)
                    except Exception:
                        pass
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_assignment(
            [node.target],
            value=node.value,
            line=node.lineno,
            type_hint=_annotation_to_str(node.annotation),
        )
        if self._func_stack and self.config.variables.track and node.value:
            ctx = self._func_stack[-1]
            if isinstance(node.target, ast.Name) and node.target.id in self.config.variables.names:
                try:
                    ctx.var_assignments[node.target.id] = ast.unparse(node.value)
                except Exception:
                    pass
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        op = type(node.op).__name__.replace("Add", "+").replace("Sub", "-")
        value = _value_to_str(node.value)
        self._record_assignment(
            [node.target],
            value=node.value,
            line=node.lineno,
            type_hint=None,
            value_repr=f"{op}= {value}" if value else None,
        )
        self.generic_visit(node)

    def _record_assignment(
        self,
        targets: list[ast.AST],
        value: Optional[ast.AST],
        line: int,
        type_hint: Optional[str],
        value_repr: Optional[str] = None,
    ) -> None:
        value_text = value_repr if value_repr is not None else _value_to_str(value)
        for target in targets:
            for name, target_kind in _iter_assignment_targets(target):
                source_kind, source_detail, is_dynamic = _classify_python_value(value)
                inferred_type = _infer_python_type(value, type_hint)
                if self._func_stack:
                    ctx = self._func_stack[-1]
                    base_scope = "global" if name in ctx.global_names else "local"
                    scope = target_kind or base_scope
                    if scope not in {"field", "environment"}:
                        if source_kind == "environment lookup":
                            scope = "environment"
                        elif is_dynamic:
                            scope = "dynamic"
                        elif _is_final_annotation(type_hint) or (_is_constant_name(name) and _is_literal_value(value)):
                            scope = "constant"
                    context = self._module_name if scope == "global" else ctx.qualified_name
                    ctx.variables.append(VariableDef(
                        name=name,
                        scope=scope,
                        type_hint=inferred_type,
                        value=value_text,
                        line=line,
                        file_path=self.file_path,
                        context=context,
                        source_kind=source_kind,
                        source_detail=source_detail,
                    ))
                elif self._class_stack:
                    cls = self._class_stack[-1]
                    scope = target_kind or "static"
                    if scope not in {"field", "environment"}:
                        if source_kind == "environment lookup":
                            scope = "environment"
                        elif is_dynamic:
                            scope = "dynamic"
                        elif _is_final_annotation(type_hint) or (_is_constant_name(name) and _is_literal_value(value)):
                            scope = "constant"
                    self._class_variables.setdefault(cls, []).append(VariableDef(
                        name=name,
                        scope=scope,
                        type_hint=inferred_type,
                        value=value_text,
                        line=line,
                        file_path=self.file_path,
                        context=cls,
                        source_kind=source_kind,
                        source_detail=source_detail,
                    ))
                else:
                    scope = target_kind or "global"
                    if scope not in {"field", "environment"}:
                        if source_kind == "environment lookup":
                            scope = "environment"
                        elif is_dynamic:
                            scope = "dynamic"
                        elif _is_final_annotation(type_hint) or (_is_constant_name(name) and _is_literal_value(value)):
                            scope = "constant"
                    self._module_variables.append(VariableDef(
                        name=name,
                        scope=scope,
                        type_hint=inferred_type,
                        value=value_text,
                        line=line,
                        file_path=self.file_path,
                        context=self._module_name,
                        source_kind=source_kind,
                        source_detail=source_detail,
                    ))

    def finalize_variables(self) -> None:
        for fn in self.functions:
            variables = list(self._module_variables)
            if fn.is_method and fn.parent:
                variables.extend(self._class_variables.get(fn.parent, []))
            variables.extend(fn.variables)
            fn.variables = _dedup_variables(variables)


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


class PythonParser(BaseParser):
    def parse_file(
        self, path: Path
    ) -> tuple[list[FunctionDef], list[CallRelationship]]:
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise ValueError(f"Syntax error: {exc}") from exc

        visitor = _PythonVisitor(str(path), self.config)
        visitor.visit(tree)
        visitor.finalize_variables()
        return visitor.functions, visitor.calls
