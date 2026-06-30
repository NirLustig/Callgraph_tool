"""
Dead-variable / liveness analysis — shared engine.

This module owns the *language-agnostic verdict logic* (`classify_verdict`) plus the
**C/C++ tree-sitter** evidence collector (`detect_dead_variables_c`). The Python and
MATLAB parsers build their own evidence (each language has a very different surface) but
feed the **same** `classify_verdict` so every language maps to one shared vocabulary of
categories and confidence levels.

Categories (`dead_category`):
  - ``unused``        — declared, never read, never written.
  - ``dead_store``    — written at least once, never read (a wasted store).
  - ``unused_param``  — a parameter never referenced in the body.
  - ``dead_alloc``    — a pointer assigned from an allocator and (at most) ``free``d,
                        never dereferenced — the allocation serves no purpose.
  - ``unused_value``  — initialised from a side-effecting call, then never read
                        (the call may matter; the *variable* does not).

Confidence (`dead_confidence`): ``high`` | ``medium`` | ``low``. MATLAB is always
``low`` (regex, best-effort); C/C++ and Python are ``high`` unless an aliasing /
ambiguity downgrades them.

Design goals: single O(n) pass per function, conservative (when unsure → alive, never a
false positive), and case-sensitive scope binding so unrelated same-named symbols (a
local ``Foo`` vs a call to ``foo()``, a struct field ``count`` vs a local ``count``)
never keep each other alive.
"""
from __future__ import annotations

import re
from typing import Optional

# Names whose presence as an initialiser RHS marks a pointer write as an allocation.
_ALLOC_RE = re.compile(r"\b(?:malloc|calloc|realloc|aligned_alloc|strdup|g_malloc|g_new)\b")
# Free-ish sinks: a pointer used only here (plus an alloc) is a dead allocation.
_FREE_FUNCS = {"free", "delete", "g_free", "kfree", "vfree"}

# Intentional-unused name patterns (case-insensitive, besides a leading underscore).
_INTENTIONAL_UNUSED_NAMES = {
    "unused", "reserved", "dummy", "pad", "padding", "ignore", "ignored", "_",
}


def is_intentional_unused_name(name: str) -> bool:
    """True if the *name itself* signals an intentional throwaway (suppress detection)."""
    if not name:
        return False
    low = name.lower()
    if low in _INTENTIONAL_UNUSED_NAMES:
        return True
    # leading underscore is the near-universal "I know it's unused" convention
    if name.startswith("_"):
        return True
    return False


def classify_verdict(
    *,
    reads: int,
    writes: int,
    is_param: bool = False,
    is_pointer: bool = False,
    alloc_write: bool = False,
    deref_reads: int = 0,
    non_free_reads: Optional[int] = None,
    addr_taken: bool = False,
    is_volatile: bool = False,
    side_effect_init: bool = False,
    low_confidence: bool = False,
) -> Optional[tuple[str, str]]:
    """Map read/write evidence to a ``(category, confidence)`` or ``None`` (alive).

    Conservative by construction: ``volatile`` and address-taken variables are never
    flagged (they may be used through side effects / aliases we cannot see).
    """
    def cap(conf: str) -> str:
        return "low" if low_confidence else conf

    # volatile reads/writes have observable side effects — never dead.
    if is_volatile:
        return None
    # address escaped → may be read/written through an alias; stay safe.
    if addr_taken:
        return None

    if non_free_reads is None:
        non_free_reads = reads

    # ── parameters ───────────────────────────────────────────────────────────
    if is_param:
        if reads == 0 and writes == 0:
            return ("unused_param", cap("high"))
        return None

    # ── dead allocation (pointer churned but never used) ─────────────────────
    if is_pointer and alloc_write and deref_reads == 0 and non_free_reads == 0:
        return ("dead_alloc", cap("medium"))

    # ── truly unused ─────────────────────────────────────────────────────────
    if reads == 0 and writes == 0:
        return ("unused", cap("high"))

    # ── write-only (dead store) ──────────────────────────────────────────────
    if reads == 0 and writes >= 1:
        if side_effect_init and writes == 1:
            # the initialising call may have side effects worth keeping
            return ("unused_value", cap("medium"))
        return ("dead_store", cap("high"))

    return None


def derive_legacy_reason(category: Optional[str]) -> Optional[str]:
    """Back-compat ``dead_reason`` text from the new category vocabulary."""
    return {
        "unused": "declared but never used",
        "dead_store": "value never read (dead store)",
        "unused_param": "unused parameter",
        "dead_alloc": "allocated but never used",
        "unused_value": "result never read",
    }.get(category or "")


# ======================================================================== #
# C / C++ tree-sitter evidence collector                                    #
# ======================================================================== #

_SCOPE_NODES = {"compound_statement", "for_statement"}
# Statement nodes that can directly hold an `if (T x = ...)` style declaration.
_COND_SCOPE_NODES = {"if_statement", "while_statement", "switch_statement"}


class _Decl:
    """Mutable evidence accumulator for one declared name in one scope."""
    __slots__ = (
        "name", "line", "is_param", "is_pointer", "is_volatile",
        "reads", "writes", "deref_reads", "free_reads", "addr_taken",
        "alloc_write", "side_effect_init", "name_node_ids",
    )

    def __init__(self, name: str, line: int, is_param: bool,
                 is_pointer: bool, is_volatile: bool):
        self.name = name
        self.line = line
        self.is_param = is_param
        self.is_pointer = is_pointer
        self.is_volatile = is_volatile
        self.reads: list[int] = []
        self.writes: list[int] = []
        self.deref_reads = 0
        self.free_reads = 0
        self.addr_taken = False
        self.alloc_write = False
        self.side_effect_init = False
        self.name_node_ids: set[int] = set()


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _declarator_name_node(decl_or_declarator):
    """Return the innermost identifier node naming a declarator, or None."""
    n = decl_or_declarator
    # Unwrap pointer/array/reference/parenthesized/init declarators down to the name.
    guard = 0
    while n is not None and guard < 12:
        guard += 1
        t = n.type
        if t in ("identifier", "field_identifier"):
            return n
        if t == "init_declarator":
            # name is the part before '='
            target = None
            for ch in n.children:
                if ch.type == "=":
                    break
                if ch.is_named:
                    target = ch
            n = target
            continue
        if t in ("pointer_declarator", "array_declarator",
                 "reference_declarator", "parenthesized_declarator"):
            nxt = None
            for ch in n.children:
                if ch.is_named:
                    nxt = ch
                    break
            n = nxt
            continue
        return None
    return None


def _is_pointer_declarator(decl_node) -> bool:
    """True if any declarator in this declaration is a pointer declarator."""
    stack = list(decl_node.children)
    while stack:
        ch = stack.pop()
        if ch.type == "pointer_declarator":
            return True
        if ch.type == "init_declarator":
            stack.extend(ch.children)
    return False


def _register_declaration(decl_node, scope: dict, all_decls: list, source: bytes,
                          is_param: bool = False) -> None:
    """Find declared names in a `declaration`/parameter and add them to `scope`."""
    type_text = ""
    for ch in decl_node.children:
        if ch.type in ("primitive_type", "type_identifier", "sized_type_specifier",
                        "type_qualifier", "struct_specifier", "qualified_identifier"):
            type_text += " " + _node_text(ch, source)
    is_volatile = "volatile" in type_text

    # Iterate declarators (skip function declarators — those aren't variables).
    for ch in decl_node.children:
        if ch.type == "function_declarator":
            return
        if ch.type not in ("identifier", "init_declarator", "pointer_declarator",
                           "array_declarator", "reference_declarator",
                           "parenthesized_declarator", "field_identifier"):
            continue
        name_node = _declarator_name_node(ch)
        if name_node is None:
            continue
        name = _node_text(name_node, source)
        if not name:
            continue
        is_ptr = ch.type == "pointer_declarator" or (
            ch.type == "init_declarator" and any(
                c.type == "pointer_declarator" for c in ch.children
            )
        )
        di = _Decl(name, name_node.start_point[0] + 1, is_param, is_ptr, is_volatile)
        di.name_node_ids.add(name_node.id)
        # init_declarator with a call initialiser → side-effect init; alloc?
        if ch.type == "init_declarator":
            rhs = None
            seen_eq = False
            for c in ch.children:
                if c.type == "=":
                    seen_eq = True
                    continue
                if seen_eq and c.is_named:
                    rhs = c
            if rhs is not None:
                di.writes.append(name_node.start_point[0] + 1)
                rhs_text = _node_text(rhs, source)
                if rhs.type in ("call_expression", "new_expression"):
                    di.side_effect_init = True
                if _ALLOC_RE.search(rhs_text) or rhs.type == "new_expression":
                    di.alloc_write = True
        scope[name] = di
        all_decls.append(di)


def _classify_identifier(node, parent, source: bytes):
    """Return (role, meta) for an identifier reference.

    role ∈ {'read','write','rw','addr'} ; meta carries deref/free hints.
    """
    if parent is None:
        return ("read", {})
    pt = parent.type

    # address-of: &x  → parent is pointer_expression whose operator child is '&'
    if pt == "pointer_expression":
        op = parent.children[0].type if parent.children else ""
        if op == "&":
            return ("addr", {})
        if op == "*":
            # *x  → dereference read of x
            return ("read", {"deref": True})

    # assignment LHS
    if pt == "assignment_expression":
        kids = [c for c in parent.children]
        # first named child is the LHS target
        named = [c for c in kids if c.is_named]
        op = ""
        for c in kids:
            if c.type in ("=", "+=", "-=", "*=", "/=", "%=", "&=", "|=",
                           "^=", "<<=", ">>="):
                op = c.type
                break
        if named and named[0].id == node.id:
            if op == "=":
                return ("write", {})
            return ("rw", {})  # compound assignment reads then writes

    # ++ / --
    if pt == "update_expression":
        return ("rw", {})

    # field base:  x.f / x->f  → x is read (and deref if '->')
    if pt == "field_expression":
        named = [c for c in parent.children if c.is_named]
        if named and named[0].id == node.id:
            arrow = any(c.type == "->" for c in parent.children)
            return ("read", {"deref": arrow})

    # subscript base:  x[i] → read of x (deref)
    if pt == "subscript_expression":
        named = [c for c in parent.children if c.is_named]
        if named and named[0].id == node.id:
            return ("read", {"deref": True})

    return ("read", {})


def _walk_collect(node, parent, scope_stack, all_decls, source, free_call_args):
    """Recursive pre-order walk: register decls, then classify references."""
    opened = False
    t = node.type

    if t in _SCOPE_NODES or t in _COND_SCOPE_NODES:
        scope_stack.append({})
        opened = True

    # register declarations into the current scope before descending
    if t == "declaration":
        _register_declaration(node, scope_stack[-1], all_decls, source)

    # detect free()/delete argument identifiers (to recognise free-only pointers)
    if t == "call_expression":
        callee = node.children[0] if node.children else None
        if callee is not None and callee.type == "identifier":
            if _node_text(callee, source) in _FREE_FUNCS:
                arglist = None
                for c in node.children:
                    if c.type == "argument_list":
                        arglist = c
                        break
                if arglist is not None:
                    for a in arglist.children:
                        if a.type == "identifier":
                            free_call_args.add(a.id)

    if t == "identifier":
        # is this a declarator name? (skip — it's the declaration itself)
        is_decl_name = any(node.id in d.name_node_ids for d in all_decls)
        if not is_decl_name:
            name = _node_text(node, source)
            di = None
            for scope in reversed(scope_stack):
                if name in scope:
                    di = scope[name]
                    break
            if di is not None:
                role, meta = _classify_identifier(node, parent, source)
                line = node.start_point[0] + 1
                if role == "addr":
                    di.addr_taken = True
                elif role == "write":
                    di.writes.append(line)
                elif role == "rw":
                    di.writes.append(line)
                    di.reads.append(line)
                else:  # read
                    di.reads.append(line)
                    if meta.get("deref"):
                        di.deref_reads += 1
                    if node.id in free_call_args:
                        di.free_reads += 1

    for ch in node.children:
        _walk_collect(ch, node, scope_stack, all_decls, source, free_call_args)

    if opened:
        scope_stack.pop()


def detect_dead_variables_c(fn, body_node, source: bytes,
                            params_node=None) -> None:
    """Liveness post-pass for a C/C++ function. Mutates `fn.variables`/`fn.parameters`.

    Two passes are needed because free()-argument node ids are discovered during the
    walk; we collect everything in one structural walk and resolve free-only pointers
    afterward (the free set is filled lazily but identifiers are processed after their
    enclosing call is entered, so a single pre-order walk suffices).
    """
    all_decls: list[_Decl] = []
    func_scope: dict[str, _Decl] = {}

    # seed parameters into the function's top scope
    for param in fn.parameters:
        if not param.name or param.name in ("self", "cls", "this", "void", ""):
            continue
        di = _Decl(param.name, fn.line_start, is_param=True,
                   is_pointer=False, is_volatile=False)
        func_scope[param.name] = di
        all_decls.append(di)

    free_call_args: set[int] = set()
    _walk_collect(body_node, None, [func_scope], all_decls, source, free_call_args)

    # index decls by (name, line) for matching back to VariableDef
    by_key: dict[tuple[str, int], _Decl] = {}
    param_decls: dict[str, _Decl] = {}
    for d in all_decls:
        if d.is_param:
            param_decls[d.name] = d
        else:
            by_key[(d.name, d.line)] = d

    # ── parameters ───────────────────────────────────────────────────────────
    for param in fn.parameters:
        d = param_decls.get(param.name)
        if d is None:
            continue
        suppressed = is_intentional_unused_name(param.name)
        verdict = classify_verdict(
            reads=len(d.reads), writes=len(d.writes), is_param=True,
        )
        if suppressed:
            param.is_suppressed = True
            param.suppress_reason = "intentional-unused name"
            param.is_dead = False
            continue
        if verdict:
            cat, conf = verdict
            param.dead_category = cat
            param.dead_confidence = conf
            param.read_lines = list(d.reads)
            param.write_lines = list(d.writes)
            param.is_dead = True

    # ── locals ───────────────────────────────────────────────────────────────
    _TRACKED_SCOPES = {"local", "static", "dynamic", "field"}
    _SYNTHETIC = {"custom_input", "input_file_connect", "member_access"}
    for var in fn.variables:
        if (var.scope or "").lower() not in _TRACKED_SCOPES:
            continue
        if (var.source_kind or "") in _SYNTHETIC:
            continue
        d = by_key.get((var.name, var.line))
        if d is None:
            continue

        # suppression: intentional-unused name or volatile
        if is_intentional_unused_name(var.name):
            var.is_suppressed = True
            var.suppress_reason = "intentional-unused name"
            continue
        if d.is_volatile:
            var.is_suppressed = True
            var.suppress_reason = "volatile (side effects)"
            continue

        non_free_reads = len(d.reads) - d.free_reads
        verdict = classify_verdict(
            reads=len(d.reads),
            writes=len(d.writes),
            is_pointer=d.is_pointer,
            alloc_write=d.alloc_write,
            deref_reads=d.deref_reads,
            non_free_reads=non_free_reads,
            addr_taken=d.addr_taken,
            is_volatile=d.is_volatile,
            side_effect_init=d.side_effect_init,
        )
        if verdict:
            cat, conf = verdict
            var.dead_category = cat
            var.dead_confidence = conf
            var.read_lines = list(d.reads)
            var.write_lines = list(d.writes)
            var.is_dead = True
            var.dead_reason = derive_legacy_reason(cat)


# ======================================================================== #
# Python ast evidence collector                                            #
# ======================================================================== #

_PY_DYNAMIC_ESCAPE = {"locals", "eval", "exec", "vars", "globals"}
_PY_NESTED_SCOPE = (
    "FunctionDef", "AsyncFunctionDef", "Lambda", "ClassDef",
    "ListComp", "SetComp", "DictComp", "GeneratorExp",
)


def detect_dead_variables_python(fn, func_node) -> None:
    """Liveness post-pass for a Python function using exact ``ast`` Load/Store/Del.

    `func_node` is the ``ast.FunctionDef``/``AsyncFunctionDef`` node. Writes are taken
    from the function's *direct* scope only; reads are collected from the whole subtree
    (including nested functions/lambdas/comprehensions) so closures and free-variable
    captures conservatively keep their referents alive.
    """
    import ast

    reads: dict[str, int] = {}
    writes: dict[str, int] = {}
    read_lines: dict[str, list[int]] = {}
    write_lines: dict[str, list[int]] = {}
    side_effect_writes: dict[str, bool] = {}
    global_nonlocal: set[str] = set()
    dynamic_escape = [False]

    def add_read(name, line):
        reads[name] = reads.get(name, 0) + 1
        read_lines.setdefault(name, []).append(line)

    def add_write(name, line, from_call=False):
        writes[name] = writes.get(name, 0) + 1
        write_lines.setdefault(name, []).append(line)
        if from_call:
            side_effect_writes[name] = True

    def note_call_escape(node):
        fnnode = getattr(node, "func", None)
        if isinstance(fnnode, ast.Name) and fnnode.id in _PY_DYNAMIC_ESCAPE:
            dynamic_escape[0] = True

    def walk(node, direct: bool):
        tname = type(node).__name__

        if isinstance(node, ast.Call):
            note_call_escape(node)

        if tname in ("Global", "Nonlocal"):
            global_nonlocal.update(node.names)

        counted_target = None
        # assignment-from-call detection (side-effect init) in direct scope
        if direct and isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            val = node.value
            single = (len(targets) == 1 and isinstance(targets[0], ast.Name))
            if single and isinstance(val, ast.Call):
                add_write(targets[0].id, node.lineno, from_call=True)
                counted_target = targets[0]

        if isinstance(node, ast.Name):
            line = getattr(node, "lineno", fn.line_start)
            if isinstance(node.ctx, ast.Store):
                if direct:
                    add_write(node.id, line)
            elif isinstance(node.ctx, ast.Del):
                add_read(node.id, line)  # deletion counts as a use
            else:  # Load
                add_read(node.id, line)

        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            if direct:
                add_write(node.target.id, node.lineno)
                add_read(node.target.id, node.lineno)

        for child in ast.iter_child_nodes(node):
            if child is counted_target:
                continue  # already counted as a side-effect write; don't double-count
            # nested scopes flip `direct` off (their Stores aren't our writes,
            # but their Loads still count as reads of our locals = closures)
            child_direct = direct and tname not in _PY_NESTED_SCOPE
            walk(child, child_direct)

    for stmt in getattr(func_node, "body", []):
        walk(stmt, direct=True)

    if dynamic_escape[0]:
        return  # dynamic introspection — keep everything alive

    # parameters to skip: self/cls already excluded earlier; skip *args/**kwargs
    skip_params: set[str] = set()
    args = getattr(func_node, "args", None)
    if args is not None:
        if args.vararg:
            skip_params.add(args.vararg.arg)
        if args.kwarg:
            skip_params.add(args.kwarg.arg)

    for param in fn.parameters:
        if param.name in skip_params or is_intentional_unused_name(param.name):
            if is_intentional_unused_name(param.name):
                param.is_suppressed = True
                param.suppress_reason = "intentional-unused name"
            continue
        if param.name in global_nonlocal:
            continue
        verdict = classify_verdict(
            reads=reads.get(param.name, 0),
            writes=writes.get(param.name, 0),
            is_param=True,
        )
        if verdict:
            cat, conf = verdict
            param.dead_category = cat
            param.dead_confidence = conf
            param.read_lines = list(read_lines.get(param.name, []))
            param.write_lines = list(write_lines.get(param.name, []))
            param.is_dead = True

    _PY_TRACKED = {"local", "dynamic", "environment", "constant"}
    for var in fn.variables:
        if (var.scope or "").lower() not in _PY_TRACKED:
            continue
        if var.name in global_nonlocal:
            continue
        if is_intentional_unused_name(var.name):
            var.is_suppressed = True
            var.suppress_reason = "intentional-unused name"
            continue
        r = reads.get(var.name, 0)
        w = writes.get(var.name, 0)
        verdict = classify_verdict(
            reads=r,
            writes=w,
            side_effect_init=side_effect_writes.get(var.name, False),
        )
        if verdict:
            cat, conf = verdict
            var.dead_category = cat
            var.dead_confidence = conf
            var.read_lines = list(read_lines.get(var.name, []))
            var.write_lines = list(write_lines.get(var.name, []))
            var.is_dead = True
            var.dead_reason = derive_legacy_reason(cat)


# ======================================================================== #
# MATLAB regex evidence collector (best-effort, always low confidence)      #
# ======================================================================== #

_ML_WORD = re.compile(r"[A-Za-z_]\w*")
_ML_ASSIGN = re.compile(
    r"^[ \t]*(?:\[([^\]]+)\]|([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?))[ \t]*=[ \t]*(.+?)[ \t]*$"
)
_ML_ESCAPE_CALLS = re.compile(r"\b(?:eval|feval|evalin|assignin|evalc)\b")
_ML_SKIP_NAMES = {
    "nargin", "nargout", "varargin", "varargout", "end", "true", "false",
}
_ML_TRACKED_SCOPES = {"local", "dynamic", "environment", "constant"}


def _ml_base_name(lhs: str) -> str:
    """Base variable for an LHS target like ``a`` / ``a.b`` / ``a(i)`` → ``a``."""
    lhs = lhs.strip().lstrip("~").strip()
    m = _ML_WORD.match(lhs)
    return m.group(0) if m else ""


def _ml_inner_line_ranges(fn, all_functions) -> set[int]:
    """Return a set of 1-indexed line numbers that belong to NESTED inner functions
    of *fn*.  In MATLAB, a nested function is any other function whose ``line_start``
    and ``line_end`` fall entirely within *fn*'s range (strictly inside, so the
    signature line of *fn* itself is excluded).  Lines in inner functions must be
    excluded from *fn*'s read/write evidence scan to avoid cross-contaminating the
    liveness counts.
    """
    inner_lines: set[int] = set()
    for other in all_functions:
        if other is fn:
            continue
        if (fn.line_start < other.line_start and
                other.line_end <= fn.line_end):
            inner_lines.update(range(other.line_start, other.line_end + 1))
    return inner_lines


def detect_dead_variables_matlab(functions, lines) -> None:
    """Best-effort MATLAB dead-variable detection (at most ``medium`` confidence).

    G2 improvements over the original "always low" engine:
    - Lines belonging to **nested inner functions** are excluded from the outer
      function's read/write evidence scan, eliminating false cross-function reads.
    - For functions with **clear scope isolation** (no dynamic-escape call AND no
      nested inner functions), the verdict confidence is capped at ``medium`` instead
      of forced to ``low``.  This gives actionable results for the common case of a
      flat, self-contained MATLAB function.

    `lines` is the comment-stripped source as a list (0-indexed). Writes without a
    trailing ``;`` display the value → counted as a use. Dynamic-escape calls
    (``eval``/``feval``/``assignin``/…) keep the whole function alive.
    Conservative by construction — biased toward "alive".
    """
    for fn in functions:
        start = max(fn.line_start, 1)
        end = fn.line_end or fn.line_start
        body = lines[start:end] if end > start else []

        # G2: build the set of 1-indexed lines owned by nested inner functions.
        inner_line_set = _ml_inner_line_ranges(fn, functions)
        has_inner = bool(inner_line_set)

        # Output variables (LHS of the `function [a,b] = name(...)` signature) escape
        # via the return — never flag them as dead.
        output_names: set[str] = set()
        sig = lines[start - 1] if 0 <= start - 1 < len(lines) else ""
        sig_m = re.match(r"^\s*function\s+(.+?)=\s*[A-Za-z_]\w*\s*\(", sig)
        if sig_m:
            for tok in _ML_WORD.findall(sig_m.group(1)):
                output_names.add(tok)

        reads: dict[str, int] = {}
        writes: dict[str, list[int]] = {}
        displayed: set[str] = set()
        escaped = [False]

        def add_read(name):
            if name and name not in _ML_SKIP_NAMES:
                reads[name] = reads.get(name, 0) + 1

        for offset, raw in enumerate(body):
            lineno = start + offset + 1  # 1-indexed

            # G2: skip lines owned by an inner nested function.
            if lineno in inner_line_set:
                continue

            line = raw.split("%", 1)[0].rstrip()
            stripped = line.strip()
            if not stripped or stripped.lower().startswith(("function ", "global ",
                                                            "persistent ")):
                if _ML_ESCAPE_CALLS.search(stripped):
                    escaped[0] = True
                continue
            if _ML_ESCAPE_CALLS.search(stripped):
                escaped[0] = True

            m = _ML_ASSIGN.match(line)
            lhs_part = line.split("=", 1)[0] if "=" in line else ""
            is_eq_test = lhs_part.endswith(("=", "<", ">", "~", "!"))
            if m and not is_eq_test:
                lhs_multi, lhs_single, rhs = m.groups()
                terminated = line.rstrip().endswith(";")
                if lhs_multi:
                    targets = [_ml_base_name(p) for p in re.split(r"[,\s]+", lhs_multi)]
                else:
                    targets = [_ml_base_name(lhs_single)]
                for nm in targets:
                    if not nm:
                        continue
                    writes.setdefault(nm, []).append(lineno)
                    if not terminated:
                        displayed.add(nm)
                for tok in _ML_WORD.findall(rhs):
                    add_read(tok)
                if lhs_single and "(" in (lhs_single or ""):
                    add_read(_ml_base_name(lhs_single))
            else:
                for tok in _ML_WORD.findall(stripped):
                    add_read(tok)

        if escaped[0]:
            continue  # dynamic eval/assignin — keep all vars alive

        # G2: for isolated flat functions (no escape, no nested inner functions that
        # could contaminate evidence), use medium confidence instead of always-low.
        # For functions with inner functions, the outer scope evidence is less
        # reliable → stay low to remain conservative.
        isolated = not has_inner  # escaped already short-circuited above

        def _cap_conf(conf: str) -> str:
            """Map a classify_verdict confidence to the MATLAB-appropriate level.
            MATLAB is regex-based so we never claim 'high'. Isolated functions get
            'medium'; functions with nested inner scopes stay 'low'.
            """
            if not isolated:
                return "low"
            # high → medium (regex, not AST); medium stays medium; low stays low.
            return "medium" if conf == "high" else conf

        for param in fn.parameters:
            if param.name in _ML_SKIP_NAMES or is_intentional_unused_name(param.name):
                continue
            verdict = classify_verdict(
                reads=reads.get(param.name, 0),
                writes=len(writes.get(param.name, [])),
                is_param=True,
                low_confidence=not isolated,
            )
            if verdict:
                cat, conf = verdict
                param.dead_category = cat
                param.dead_confidence = _cap_conf(conf)
                param.is_dead = True

        for var in fn.variables:
            if (var.scope or "").lower() not in _ML_TRACKED_SCOPES:
                continue
            if var.name in _ML_SKIP_NAMES:
                continue
            if var.name in output_names:
                continue  # function output — escapes via return
            if is_intentional_unused_name(var.name):
                var.is_suppressed = True
                var.suppress_reason = "intentional-unused name"
                continue
            if var.name in displayed:
                continue  # value displayed (no trailing ';') → a use
            verdict = classify_verdict(
                reads=reads.get(var.name, 0),
                writes=len(writes.get(var.name, [])),
                low_confidence=not isolated,
            )
            if verdict:
                cat, conf = verdict
                var.dead_category = cat
                var.dead_confidence = _cap_conf(conf)
                var.write_lines = list(writes.get(var.name, []))
                var.is_dead = True
                var.dead_reason = derive_legacy_reason(cat)
