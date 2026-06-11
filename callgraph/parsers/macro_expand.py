"""
Simple C/C++ `#define` macro-expansion pre-pass (idea C-1).

Many call sites are wrapped in object-like or function-like macros, e.g.

    #define LOG(msg)   real_log(msg)
    #define HANDLER    do_handle

    LOG("hi");      // Tree-sitter sees a call to LOG, not real_log
    HANDLER();      // Tree-sitter sees a call to HANDLER, not do_handle

Without expansion these resolve to a non-existent ``LOG`` / ``HANDLER`` target
and land in the ``unresolved`` bucket. This module performs a conservative
textual expansion of *simple* macros before the source is handed to Tree-sitter
so the real callee name is parsed instead.

Design constraints (see Obsidian/Rules.md):
  * **Line count is preserved exactly.** Node ids depend on line numbers, so any
    newlines consumed by a multi-line macro invocation are re-emitted after the
    expansion. Object-like bodies are collapsed to a single line.
  * **Conservative.** Macros using token-paste/stringize (``#`` / ``##``),
    variadic macros (``...`` / ``__VA_ARGS__``), and self-referential macros are
    left untouched. Strings, char literals, comments and ``#`` directive lines
    are never expanded.
  * **Pure text, offline.** No external preprocessor is invoked.
"""
from __future__ import annotations

import re

_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_MAX_PASSES = 8


def _is_simple_body(body: str, name: str) -> bool:
    """Reject bodies we can't safely expand textually."""
    if "#" in body:            # stringize / token-paste
        return False
    if "__VA_ARGS__" in body:
        return False
    # direct self-reference would loop (the real preprocessor blocks it too)
    if re.search(r"\b" + re.escape(name) + r"\b", body):
        return False
    return True


def _collect_macros(source: str) -> tuple[dict[str, str], dict[str, tuple[list[str], str]]]:
    """Return (object_like, function_like) macro tables from ``#define`` lines."""
    obj: dict[str, str] = {}
    func: dict[str, tuple[list[str], str]] = {}

    raw = source.split("\n")
    n = len(raw)
    i = 0
    while i < n:
        # Join backslash line-continuations into one logical line.
        cont = raw[i]
        while cont.endswith("\\") and i + 1 < n:
            cont = cont[:-1] + " " + raw[i + 1]
            i += 1
        i += 1

        stripped = cont.lstrip()
        if not stripped.startswith("#"):
            continue
        after_hash = stripped[1:].lstrip()
        if not after_hash.startswith("define"):
            continue
        rest = after_hash[len("define"):]
        if rest[:1] not in (" ", "\t"):
            continue
        _parse_define(rest.strip(), obj, func)

    return obj, func


def _parse_define(rest: str,
                   obj: dict[str, str],
                   func: dict[str, tuple[list[str], str]]) -> None:
    m = _IDENT_RE.match(rest)
    if not m:
        return
    name = m.group(0)
    after = rest[m.end():]          # NOTE: not stripped — a leading "(" with no
                                    # space means function-like; a space means
                                    # object-like body that happens to start "(".
    if after.startswith("("):
        close = after.find(")")
        if close == -1:
            return
        params_str = after[1:close]
        body = after[close + 1:].strip()
        params = [p.strip() for p in params_str.split(",") if p.strip()]
        if any(("..." in p or "__VA_ARGS__" in p) for p in params):
            return
        if not _is_simple_body(body, name):
            return
        func[name] = (params, body)
    else:
        body = after.strip()
        if not _is_simple_body(body, name):
            return
        obj[name] = body


def _read_args(source: str, k: int) -> tuple[list[str], int, bool]:
    """Parse a balanced ``(...)`` arg list starting at ``source[k] == '('``.

    Returns (args, end_index_after_closing_paren, ok).
    """
    n = len(source)
    depth = 0
    args: list[str] = []
    cur: list[str] = []
    i = k
    while i < n:
        c = source[i]
        if c in '"\'':
            q = c
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == q:
                    j += 1
                    break
                j += 1
            cur.append(source[i:j])
            i = j
            continue
        if c == "(":
            depth += 1
            if depth > 1:
                cur.append(c)
            i += 1
            continue
        if c == ")":
            depth -= 1
            if depth == 0:
                arg = "".join(cur).strip()
                if arg or args:        # ``FOO()`` → zero args, not one empty arg
                    args.append(arg)
                return args, i + 1, True
            cur.append(c)
            i += 1
            continue
        if c == "," and depth == 1:
            args.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    return [], k, False                # unbalanced — bail


def _subst(body: str, params: list[str], args: list[str]) -> str | None:
    """Substitute function-like params with args (whole-word). None on mismatch."""
    if len(params) != len(args):
        return None
    if not params:
        return body
    mapping = dict(zip(params, args))
    return _IDENT_RE.sub(lambda m: mapping.get(m.group(0), m.group(0)), body)


def _rewrite(source: str,
             obj: dict[str, str],
             func: dict[str, tuple[list[str], str]]) -> tuple[str, bool]:
    """One expansion pass over ``source``. Returns (new_source, changed)."""
    out: list[str] = []
    n = len(source)
    i = 0
    changed = False
    at_line_start = True

    while i < n:
        c = source[i]

        # Preprocessor directive line → copy the whole logical line verbatim.
        if at_line_start and c == "#":
            j = i
            while j < n:
                if source[j] == "\n":
                    if j > 0 and source[j - 1] == "\\":
                        j += 1
                        continue
                    break
                j += 1
            out.append(source[i:j])
            i = j
            continue

        if c == "\n":
            out.append(c)
            i += 1
            at_line_start = True
            continue
        if c in " \t\r":
            out.append(c)
            i += 1
            continue                    # keep at_line_start unchanged

        # Comments
        if c == "/" and i + 1 < n and source[i + 1] == "/":
            j = source.find("\n", i)
            if j == -1:
                j = n
            out.append(source[i:j])
            i = j
            at_line_start = False
            continue
        if c == "/" and i + 1 < n and source[i + 1] == "*":
            j = source.find("*/", i + 2)
            j = (j + 2) if j != -1 else n
            out.append(source[i:j])
            i = j
            at_line_start = False
            continue

        # String / char literals
        if c in '"\'':
            q = c
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == q:
                    j += 1
                    break
                j += 1
            out.append(source[i:j])
            i = j
            at_line_start = False
            continue

        # Identifier
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (source[j].isalnum() or source[j] == "_"):
                j += 1
            ident = source[i:j]
            at_line_start = False

            if ident in func:
                k = j
                while k < n and source[k] in " \t\r\n":
                    k += 1
                if k < n and source[k] == "(":
                    args, end, ok = _read_args(source, k)
                    if ok:
                        params, body = func[ident]
                        expanded = _subst(body, params, args)
                        if expanded is not None:
                            nl = source.count("\n", i, end)
                            out.append(expanded + ("\n" * nl))
                            changed = True
                            i = end
                            continue
                out.append(ident)
                i = j
                continue

            if ident in obj:
                out.append(obj[ident])
                changed = True
                i = j
                continue

            out.append(ident)
            i = j
            continue

        out.append(c)
        i += 1
        at_line_start = False

    return "".join(out), changed


def expand_c_macros(source: str, max_passes: int = _MAX_PASSES) -> str:
    """Expand simple object-like and function-like ``#define`` macros in ``source``.

    Line count is preserved so downstream line numbers / node ids stay stable.
    Returns ``source`` unchanged when there is nothing to expand.
    """
    obj, func = _collect_macros(source)
    if not obj and not func:
        return source

    cur = source
    for _ in range(max_passes):
        new, changed = _rewrite(cur, obj, func)
        if not changed:
            break
        cur = new
    return cur
