"""
#include extraction + cycle detection for C / C++ source and header files.

Public surface:
    build_include_graph(files, *, project_root, build_info=None, follow_system=False) -> IncludeGraph

This is a *regex* extractor with a light, preprocessor-aware conditional pass (INC-1).
Local includes ("...") are resolved using, in order:
    1. same directory as the including file
    2. project-wide basename index built from the discovered file list
    3. include paths from BuildInfo.units[file].includes (compile_commands.json)
    4. global include paths from BuildInfo.global_includes (.vcxproj merged)

System includes (<...>) are tagged is_system=True. Unless follow_system=True, they
are still recorded but the UI hides them by default.

INC-1 (preprocessor-aware include resolution): the scanner tracks
``#if/#ifdef/#ifndef/#elif/#else/#endif`` nesting and evaluates the guards against
the ``-D`` defines captured in :class:`BuildInfo` (per-unit + global). Evaluation is
*three-valued* (true / false / unknown) and deliberately conservative: an include is
only dropped when its guard is *definitely* false (e.g. ``#if 0`` or the inactive
side of an ``#ifdef`` whose macro is known-defined). When a guard mentions a macro we
do not know about (the common case for compiler builtins, or when no build metadata is
available), the branch is treated as *active* so behaviour matches the old
"include everything" default — no regressions. Dropped includes are recorded in
``IncludeGraph.excluded`` with the controlling guard.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

from .models import BuildInfo, IncludeEdge, IncludeGraph


_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*([<"])([^">]+)[">]', re.MULTILINE)
_INCLUDE_LINE_RE = re.compile(r'^\s*#\s*include\s*([<"])([^">]+)[">]')
_COND_RE = re.compile(r'^[ \t]*#[ \t]*(ifdef|ifndef|if|elif|else|endif)\b[ \t]*(.*?)[ \t]*$')
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")

_C_EXTS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inl", ".tpp"}


# ------------------------------------------------------------------ #
# INC-1: three-valued preprocessor condition evaluator               #
# ------------------------------------------------------------------ #
# A "tri-state" is one of 'T' (definitely true), 'F' (definitely false) or
# 'U' (unknown — not enough information). Includes are dropped ONLY under a
# definite-false branch, which guarantees no regression versus the previous
# unconditional behaviour when defines are unknown/empty.

_PP_DEFINED_RE = re.compile(r'\bdefined\b')
_PP_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<num>0[xX][0-9a-fA-F]+|\d+)[uUlL]*"
    r"|(?P<id>[A-Za-z_]\w*)"
    r"|(?P<op><<|>>|<=|>=|==|!=|&&|\|\||[()!~+\-*/%<>&|^])"
    r")"
)


def _macro_int(env: dict, name: str):
    """Return the integer value of a known macro, or None if unknown / non-integer.

    A bare ``-DFOO`` (value ``None``) is treated as ``1`` per the usual compiler
    convention, matching how ``#if FOO`` behaves under such a define.
    """
    if name not in env:
        return None
    val = env[name]
    if val is None:
        return 1
    try:
        return int(str(val).strip(), 0)
    except (ValueError, TypeError):
        return None


def _eval_pp_expr(expr: str, env: dict) -> str:
    """Evaluate a ``#if`` / ``#elif`` expression to a tri-state ('T'/'F'/'U')."""
    try:
        val = _PPExprParser(expr, env).parse()
    except Exception:
        return 'U'
    if val is None:
        return 'U'
    return 'T' if val != 0 else 'F'


class _PPExprParser:
    """Tiny recursive-descent evaluator for C-preprocessor #if expressions.

    Values are Python ``int`` or ``None`` (unknown). Operators propagate ``None``
    except where short-circuiting yields a definite result (``0 && x`` -> 0,
    ``1 || x`` -> 1).
    """

    def __init__(self, expr: str, env: dict):
        self.env = env
        self.toks = self._tokenize(expr)
        self.i = 0

    def _tokenize(self, expr: str):
        toks = []
        pos = 0
        n = len(expr)
        while pos < n:
            m = _PP_TOKEN_RE.match(expr, pos)
            if not m or m.end() == pos:
                if expr[pos].isspace():
                    pos += 1
                    continue
                raise ValueError("bad token")
            pos = m.end()
            if m.group('num') is not None:
                toks.append(('num', int(m.group('num'), 0)))
            elif m.group('id') is not None:
                toks.append(('id', m.group('id')))
            else:
                toks.append(('op', m.group('op')))
        return toks

    def _peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def _next(self):
        t = self._peek()
        self.i += 1
        return t

    def parse(self):
        v = self._parse_or()
        return v

    # Precedence ladder (low -> high)
    def _parse_or(self):
        v = self._parse_and()
        while self._peek() == ('op', '||'):
            self._next()
            r = self._parse_and()
            if (v is not None and v != 0) or (r is not None and r != 0):
                v = 1
            elif v is None or r is None:
                v = None
            else:
                v = 0
        return v

    def _parse_and(self):
        v = self._parse_bitor()
        while self._peek() == ('op', '&&'):
            self._next()
            r = self._parse_bitor()
            if v == 0 or r == 0:
                v = 0
            elif v is None or r is None:
                v = None
            else:
                v = 1
        return v

    def _parse_bitor(self):
        v = self._parse_bitxor()
        while self._peek() in (('op', '|'),):
            self._next()
            r = self._parse_bitxor()
            v = None if (v is None or r is None) else (v | r)
        return v

    def _parse_bitxor(self):
        v = self._parse_bitand()
        while self._peek() == ('op', '^'):
            self._next()
            r = self._parse_bitand()
            v = None if (v is None or r is None) else (v ^ r)
        return v

    def _parse_bitand(self):
        v = self._parse_eq()
        while self._peek() == ('op', '&'):
            self._next()
            r = self._parse_eq()
            v = None if (v is None or r is None) else (v & r)
        return v

    def _parse_eq(self):
        v = self._parse_rel()
        while self._peek() in (('op', '=='), ('op', '!=')):
            op = self._next()[1]
            r = self._parse_rel()
            if v is None or r is None:
                v = None
            else:
                v = int((v == r) if op == '==' else (v != r))
        return v

    def _parse_rel(self):
        v = self._parse_shift()
        while self._peek() in (('op', '<'), ('op', '>'), ('op', '<='), ('op', '>=')):
            op = self._next()[1]
            r = self._parse_shift()
            if v is None or r is None:
                v = None
            else:
                v = int({'<': v < r, '>': v > r, '<=': v <= r, '>=': v >= r}[op])
        return v

    def _parse_shift(self):
        v = self._parse_add()
        while self._peek() in (('op', '<<'), ('op', '>>')):
            op = self._next()[1]
            r = self._parse_add()
            v = None if (v is None or r is None) else (v << r if op == '<<' else v >> r)
        return v

    def _parse_add(self):
        v = self._parse_mul()
        while self._peek() in (('op', '+'), ('op', '-')):
            op = self._next()[1]
            r = self._parse_mul()
            v = None if (v is None or r is None) else (v + r if op == '+' else v - r)
        return v

    def _parse_mul(self):
        v = self._parse_unary()
        while self._peek() in (('op', '*'), ('op', '/'), ('op', '%')):
            op = self._next()[1]
            r = self._parse_unary()
            if v is None or r is None:
                v = None
            elif op == '*':
                v = v * r
            elif r == 0:
                v = None
            else:
                v = int(v / r) if op == '/' else (v - int(v / r) * r)
        return v

    def _parse_unary(self):
        t = self._peek()
        if t == ('op', '!'):
            self._next()
            v = self._parse_unary()
            return None if v is None else int(v == 0)
        if t == ('op', '~'):
            self._next()
            v = self._parse_unary()
            return None if v is None else (~v)
        if t == ('op', '-'):
            self._next()
            v = self._parse_unary()
            return None if v is None else (-v)
        if t == ('op', '+'):
            self._next()
            return self._parse_unary()
        return self._parse_primary()

    def _parse_primary(self):
        t = self._peek()
        if t == ('op', '('):
            self._next()
            v = self._parse_or()
            if self._peek() == ('op', ')'):
                self._next()
            return v
        if t[0] == 'num':
            self._next()
            return t[1]
        if t[0] == 'id':
            self._next()
            name = t[1]
            if name == 'defined':
                return self._parse_defined()
            # Unknown identifier -> None (not 0): our define set is incomplete, so
            # we cannot assume the macro is undefined. A known macro expands to its
            # integer value (or 1 for a bare -DFOO).
            return _macro_int(self.env, name)
        # Unexpected token -> unknown
        raise ValueError("unexpected token")

    def _parse_defined(self):
        # Either  defined NAME  or  defined ( NAME )
        t = self._peek()
        paren = False
        if t == ('op', '('):
            self._next()
            paren = True
            t = self._peek()
        if t[0] != 'id':
            raise ValueError("defined needs a name")
        name = self._next()[1]
        if paren and self._peek() == ('op', ')'):
            self._next()
        # Known-defined -> 1; unknown -> None (we cannot prove it is undefined).
        return 1 if name in self.env else None


def _eval_directive(kind: str, arg: str, env: dict) -> str:
    """Evaluate one conditional directive to a tri-state ('T'/'F'/'U')."""
    if kind == 'ifdef':
        name = arg.split()[0] if arg.split() else ''
        return 'T' if name in env else 'U'
    if kind == 'ifndef':
        name = arg.split()[0] if arg.split() else ''
        return 'F' if name in env else 'U'
    # 'if' / 'elif'
    return _eval_pp_expr(arg, env)



def build_include_graph(
    files: Iterable[Path],
    *,
    project_root: Optional[Path] = None,
    build_info: Optional[BuildInfo] = None,
    follow_system: bool = False,
) -> IncludeGraph:
    """Build an IncludeGraph for the given C/C++ files."""
    file_list = [Path(p) for p in files if Path(p).suffix.lower() in _C_EXTS]
    abs_paths = {str(p.resolve()): p for p in file_list}

    # basename index for cheap project-wide resolution
    by_basename: dict[str, list[str]] = {}
    for abs_str in abs_paths:
        name = Path(abs_str).name.lower()
        by_basename.setdefault(name, []).append(abs_str)

    # global include paths from build_info
    global_includes: list[Path] = []
    if build_info is not None:
        for inc in build_info.global_includes:
            global_includes.append(Path(inc))

    files_by_path: dict[str, list[IncludeEdge]] = {}
    unresolved: list[IncludeEdge] = []
    excluded: list[IncludeEdge] = []
    in_degree: dict[str, int] = {}

    for abs_str, p in abs_paths.items():
        edges, dropped = _scan_includes(
            p,
            abs_str,
            abs_paths=abs_paths,
            by_basename=by_basename,
            project_includes=_unit_includes(build_info, abs_str),
            global_includes=global_includes,
            follow_system=follow_system,
            defines=_define_env(build_info, abs_str),
        )
        files_by_path[abs_str] = edges
        excluded.extend(dropped)
        for e in edges:
            if not e.resolved:
                unresolved.append(e)
            else:
                in_degree[e.to_file] = in_degree.get(e.to_file, 0) + 1

    cycles = _find_cycles(files_by_path)

    most_included = sorted(in_degree.items(), key=lambda kv: kv[1], reverse=True)[:25]

    return IncludeGraph(
        files=files_by_path,
        unresolved=unresolved,
        cycles=cycles,
        most_included=most_included,
        excluded=excluded,
    )


def _define_env(build_info: Optional[BuildInfo], abs_path: str) -> dict:
    """Merge global + per-unit ``-D`` defines into a single name->value map.

    Per-unit defines win over global ones. Returns an empty dict when no build
    metadata is available, which makes every guard evaluate to 'unknown' and so
    preserves the legacy "include everything" behaviour (INC-1, no regression).
    """
    if build_info is None:
        return {}
    env: dict = dict(build_info.global_defines)
    unit = build_info.units.get(abs_path)
    if unit is not None:
        env.update(unit.defines)
    return env


def _unit_includes(build_info: Optional[BuildInfo], abs_path: str) -> list[Path]:
    if build_info is None:
        return []
    unit = build_info.units.get(abs_path)
    if unit is None:
        return []
    return [Path(p) for p in unit.includes]


def _strip_comments(text: str) -> str:
    text = _BLOCK_COMMENT_RE.sub("", text)
    text = _LINE_COMMENT_RE.sub("", text)
    return text


def _strip_comments_keep_lines(text: str) -> str:
    """Strip comments while preserving line breaks so directive/line tracking stays
    accurate for the INC-1 conditional walk."""
    text = _BLOCK_COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    text = _LINE_COMMENT_RE.sub("", text)
    return text


def _scan_includes(
    file_path: Path,
    abs_str: str,
    *,
    abs_paths: dict[str, Path],
    by_basename: dict[str, list[str]],
    project_includes: list[Path],
    global_includes: list[Path],
    follow_system: bool,
    defines: Optional[dict] = None,
) -> tuple[list[IncludeEdge], list[IncludeEdge]]:
    """Return ``(active_edges, excluded_edges)``.

    ``excluded_edges`` are includes that sit inside a preprocessor branch the
    build defines prove is *not* taken (INC-1). They carry a ``guard`` string.
    """
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []
    env = defines or {}
    cleaned = _strip_comments_keep_lines(text)

    edges: list[IncludeEdge] = []
    dropped: list[IncludeEdge] = []

    # Conditional nesting stack. Each frame:
    #   parent_active   : is the enclosing context emitting includes?
    #   definite_true   : a prior branch in this group was definitely taken
    #   uncertain       : a prior branch was active-but-uncertain
    #   active          : is the current branch emitting includes?
    #   label           : raw directive text controlling the current branch
    stack: list[dict] = []

    def _ctx_active() -> bool:
        return all(f["active"] for f in stack) if stack else True

    def _guard_label() -> str:
        for f in reversed(stack):
            if f.get("label"):
                return f["label"]
        return ""

    for ix, raw_line in enumerate(cleaned.split("\n")):
        line_no = ix + 1
        cm = _COND_RE.match(raw_line)
        if cm:
            kind, arg = cm.group(1), cm.group(2)
            if kind in ("if", "ifdef", "ifndef"):
                parent_active = _ctx_active()
                cond = _eval_directive(kind, arg, env)
                active = parent_active and cond != 'F'
                frame = {
                    "parent_active": parent_active,
                    "definite_true": parent_active and cond == 'T',
                    "uncertain": active and not (parent_active and cond == 'T'),
                    "active": active,
                    "label": ("#" + kind + (" " + arg if arg else "")).strip(),
                }
                stack.append(frame)
            elif kind in ("elif", "else") and stack:
                fr = stack[-1]
                if kind == "elif":
                    cond = _eval_pp_expr(arg, env)
                    fr["label"] = ("#elif " + arg).strip()
                else:  # else: taken iff no prior branch was (definitely) taken
                    if fr["definite_true"]:
                        cond = 'F'
                    elif fr["uncertain"]:
                        cond = 'U'
                    else:
                        cond = 'T'
                    fr["label"] = "#else"
                reachable = fr["parent_active"] and not fr["definite_true"]
                active = reachable and cond != 'F'
                becomes_def = reachable and cond == 'T' and not fr["uncertain"]
                if becomes_def:
                    fr["definite_true"] = True
                elif active:
                    fr["uncertain"] = True
                fr["active"] = active
            elif kind == "endif" and stack:
                stack.pop()
            continue

        im = _INCLUDE_LINE_RE.match(raw_line)
        if not im:
            continue
        delim = im.group(1)
        target = im.group(2).strip()
        is_system = delim == "<"

        resolved_path = _resolve(
            target,
            file_path,
            abs_paths=abs_paths,
            by_basename=by_basename,
            project_includes=project_includes,
            global_includes=global_includes,
            is_system=is_system,
        )

        guard = _guard_label()
        if resolved_path is None:
            edge = IncludeEdge(
                from_file=abs_str, to_file=target, is_system=is_system,
                resolved=False, raw_target=target, line=line_no, guard=guard,
            )
        else:
            edge = IncludeEdge(
                from_file=abs_str, to_file=resolved_path, is_system=is_system,
                resolved=True, raw_target=target, line=line_no, guard=guard,
            )

        if _ctx_active():
            edges.append(edge)
        else:
            dropped.append(edge)

    return edges, dropped


def _resolve(
    target: str,
    including_file: Path,
    *,
    abs_paths: dict[str, Path],
    by_basename: dict[str, list[str]],
    project_includes: list[Path],
    global_includes: list[Path],
    is_system: bool,
) -> Optional[str]:
    target_norm = target.replace("\\", "/")
    # 1. Same directory (only for local includes; system includes skip this)
    if not is_system:
        candidate = (including_file.parent / target_norm).resolve()
        if str(candidate) in abs_paths:
            return str(candidate)

    # 2. Basename index (project-wide)
    matches = by_basename.get(Path(target_norm).name.lower(), [])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Prefer one ending with the full target path.
        tail = target_norm.lower()
        for m in matches:
            if m.replace("\\", "/").lower().endswith(tail):
                return m

    # 3 + 4. Project / global include paths
    for inc_root in list(project_includes) + list(global_includes):
        candidate = (inc_root / target_norm).resolve()
        if str(candidate) in abs_paths:
            return str(candidate)
    return None


# ------------------------------------------------------------------ #
# Cycle detection                                                     #
# ------------------------------------------------------------------ #

def _find_cycles(files_by_path: dict[str, list[IncludeEdge]]) -> list[list[str]]:
    """Iterative DFS that captures simple back-edge cycles. Returns deduplicated cycle paths."""
    visited: set[str] = set()
    on_stack: set[str] = set()
    cycles_set: set[tuple[str, ...]] = set()

    def _normalise_cycle(seq: list[str]) -> tuple[str, ...]:
        # Rotate so the lexicographically smallest element is first; preserves order.
        if not seq:
            return tuple()
        m = min(range(len(seq)), key=lambda i: seq[i])
        return tuple(seq[m:] + seq[:m])

    for start in files_by_path:
        if start in visited:
            continue
        # Iterative DFS
        stack: list[tuple[str, int, list[str]]] = [(start, 0, [start])]
        path_index: dict[str, int] = {start: 0}
        on_stack.add(start)
        while stack:
            node, ei, path = stack[-1]
            edges = [e for e in files_by_path.get(node, []) if e.resolved and not e.is_system]
            if ei >= len(edges):
                stack.pop()
                on_stack.discard(node)
                path_index.pop(node, None)
                visited.add(node)
                continue
            stack[-1] = (node, ei + 1, path)
            nxt = edges[ei].to_file
            if nxt in on_stack:
                # Found cycle: slice path from where nxt appears
                start_idx = path_index.get(nxt)
                if start_idx is not None:
                    cyc = path[start_idx:] + [nxt]
                    cycles_set.add(_normalise_cycle(cyc[:-1]))  # drop dup tail
                continue
            if nxt in visited:
                continue
            on_stack.add(nxt)
            new_path = path + [nxt]
            path_index[nxt] = len(new_path) - 1
            stack.append((nxt, 0, new_path))

    return [list(c) for c in sorted(cycles_set, key=lambda t: (len(t), t))]
