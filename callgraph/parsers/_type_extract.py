"""Type extraction (Type Nodes Mode, phase A).

Walks a Tree-sitter C/C++ tree and produces raw :class:`TypeDef` records for
every ``struct`` / ``union`` / ``enum`` / ``typedef`` definition, capturing each
member's verbatim declared type text plus the structured facts the type builder
needs to resolve links (pointer depth, array dims, bitfield width, function
pointers, anonymous nested children).

Design rules (see Obsidian ``Features/Modes/Type Nodes Mode``):
- ``type_text`` is stored *verbatim as written* — a resolution miss degrades to
  an unlinked chip, never a wrong label.
- ``type_id`` here is provisional (``<file>::<key>``); the builder normalises the
  file part to a root-relative path and dedups duplicate header definitions.
- Only body-bearing specifiers become nodes; a bare ``struct Foo *p`` reference
  (no body) is a *usage*, not a definition, and is skipped.

The module is parser-agnostic (no import from ``c_parser`` to avoid a cycle);
``c_parser`` and, later, ``cpp_parser`` call :func:`extract_types_from_tree`.
"""
from __future__ import annotations

from typing import Optional

from ..models import TypeDef, TypeMember


# ── tiny local AST helpers (kept independent of c_parser) ────────────────────

def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _first_child(node, type_name: str):
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _find_descendant(node, type_name: str):
    """First descendant (pre-order) of the given type, or None."""
    for c in node.children:
        if c.type == type_name:
            return c
        found = _find_descendant(c, type_name)
        if found is not None:
            return found
    return None


def _count_descendant(node, type_name: str) -> int:
    """Number of descendants (including direct children) of the given type."""
    n = 0
    for c in node.children:
        if c.type == type_name:
            n += 1
        n += _count_descendant(c, type_name)
    return n


# Nodes that make up the *type prefix* of a field / typedef (everything before
# the declarator name).
_TYPE_PREFIX = frozenset({
    "primitive_type", "sized_type_specifier", "type_identifier",
    "struct_specifier", "union_specifier", "enum_specifier",
    "type_qualifier", "storage_class_specifier", "macro_type_specifier",
    "sized_type_specifier",
})

_DECLARATOR_TYPES = frozenset({
    "field_identifier", "identifier", "pointer_declarator",
    "array_declarator", "function_declarator", "parenthesized_declarator",
})

# For peeling nested declarators, a typedef alias name can be a ``type_identifier``
# (e.g. ``*PFoo``); include it here but NOT in the member-field scan above, where
# a bare ``type_identifier`` is the base type, not a declarator.
_INNER_DECL_TYPES = _DECLARATOR_TYPES | {"type_identifier"}


# ── declarator analysis ──────────────────────────────────────────────────────

def _unwrap_declarator(node, source: bytes) -> dict:
    """Peel a declarator to (name, pointer_depth, array_dims, func-ptr info)."""
    info = {
        "name": None, "pointer": 0, "array_dims": [],
        "is_func_ptr": False, "func_sig": None, "is_method": False,
    }
    cur = node
    while cur is not None:
        t = cur.type
        if t in ("field_identifier", "identifier", "type_identifier"):
            info["name"] = _text(cur, source)
            break
        if t == "pointer_declarator":
            info["pointer"] += 1
            cur = _inner_declarator(cur)
            continue
        if t == "array_declarator":
            dim = _first_child(cur, "number_literal") or _first_child(cur, "identifier")
            info["array_dims"].append(_text(dim, source) if dim else "")
            cur = _inner_declarator(cur)
            continue
        if t == "function_declarator":
            inner = _inner_declarator(cur)
            fid = _find_descendant(cur, "field_identifier") or _find_descendant(cur, "identifier")
            if inner is not None and inner.type == "parenthesized_declarator":
                # genuine function-pointer member: int (*cb)(args)
                info["is_func_ptr"] = True
                info["func_sig"] = _text(node, source)
                info["name"] = _text(fid, source) if fid else None
            else:
                # C++ member method declaration (void foo();) — not a data member
                info["is_method"] = True
                info["name"] = _text(fid, source) if fid else None
            break
        if t == "parenthesized_declarator":
            cur = _inner_declarator(cur)
            continue
        break
    return info


def _inner_declarator(node):
    """The child of a wrapping declarator that is itself a declarator."""
    for c in node.children:
        if c.type in _INNER_DECL_TYPES:
            return c
    return None


# ── member extraction ────────────────────────────────────────────────────────

def _base_type_text(field_node, first_decl_start: int, source: bytes) -> str:
    """Verbatim base-type text (span before the first declarator)."""
    txt = source[field_node.start_byte:first_decl_start].decode("utf-8", errors="replace")
    return " ".join(txt.split()).strip()


def _ref_from_prefix(field_node, source: bytes):
    """(ref_type_token, anon_specifier_node) for the field's declared type.

    ref_type_token is the tag / named type the member refers to (for the builder
    to resolve); anon_specifier_node is a body-bearing struct/union/enum with no
    tag → an anonymous nested type to synthesize.
    """
    for c in field_node.children:
        if c.type in ("struct_specifier", "union_specifier", "enum_specifier",
                      "class_specifier"):
            tag = _first_child(c, "type_identifier")
            has_body = (_first_child(c, "field_declaration_list") is not None
                        or _first_child(c, "enumerator_list") is not None)
            if tag is not None:
                return _text(tag, source), None
            if has_body:
                return None, c        # anonymous nested definition
            return None, None
        if c.type == "type_identifier":
            return _text(c, source), None
    return None, None


def _extract_members(body, source: bytes, out: list,
                     parent_key: str, file_path: str, language: str) -> list:
    """All members of a struct/union body. Anonymous nested definitions are
    appended directly to ``out`` (the top-level type list)."""
    members: list[TypeMember] = []
    anon_counter = 0
    for field in body.children:
        if field.type != "field_declaration":
            continue
        # locate declarators (skip the type prefix + punctuation)
        decls = [c for c in field.children if c.type in _DECLARATOR_TYPES]
        bitfield = _first_child(field, "bitfield_clause")
        bf_width = None
        if bitfield is not None:
            num = _first_child(bitfield, "number_literal")
            if num is not None:
                try:
                    bf_width = int(_text(num, source))
                except ValueError:
                    bf_width = None

        ref_type, anon_node = _ref_from_prefix(field, source)

        # anonymous nested struct/union/enum → synthesize a child TypeDef
        anon_child_id = None
        if anon_node is not None:
            anon_counter += 1
            anon_child_id = f"{parent_key}::<anon{anon_counter}@{anon_node.start_point[0] + 1}>"
            _build_anon_child(anon_node, source, out, anon_child_id, parent_key,
                              file_path, language)

        if not decls:
            # e.g. an unnamed embedded struct with no member instance name
            if anon_child_id is not None:
                members.append(TypeMember(
                    name="", type_text=_base_type_text(field, field.end_byte, source),
                    ref_type=None, anon_child_id=anon_child_id,
                    line=field.start_point[0] + 1,
                ))
            continue

        first_start = min(d.start_byte for d in decls)
        base_text = _base_type_text(field, first_start, source)

        for d in decls:
            u = _unwrap_declarator(d, source)
            if u.get("is_method"):
                continue   # C++ member method — not a data member (v1)
            if not u["name"]:
                continue
            type_text = base_text
            if u["is_func_ptr"] and u["func_sig"]:
                type_text = f"{base_text} {u['func_sig']}".strip()
            members.append(TypeMember(
                name=u["name"],
                type_text=type_text,
                ref_type=ref_type,
                is_pointer=u["pointer"],
                array_dims=list(u["array_dims"]),
                bitfield_width=bf_width,
                is_func_ptr=u["is_func_ptr"],
                func_ptr_signature=u["func_sig"],
                anon_child_id=anon_child_id,
                line=field.start_point[0] + 1,
            ))
    return members


def _build_anon_child(spec_node, source: bytes, out: list, type_id: str,
                      parent_key: str, file_path: str, language: str) -> None:
    kind = {"struct_specifier": "struct", "union_specifier": "union",
            "enum_specifier": "enum", "class_specifier": "class"}.get(spec_node.type)
    if kind is None:
        return
    td = TypeDef(
        type_id=type_id, kind=kind, tag_name=None, aliases=[],
        file=file_path, line_start=spec_node.start_point[0] + 1,
        line_end=spec_node.end_point[0] + 1, is_anonymous=True,
        parent_type_id=parent_key, language=language,
    )
    out.append(td)
    if kind == "enum":
        elist = _first_child(spec_node, "enumerator_list")
        if elist is not None:
            td.enum_values = _extract_enum_values(elist, source)
    else:
        body = _first_child(spec_node, "field_declaration_list")
        if body is not None:
            td.members = _extract_members(body, source, out, type_id,
                                          file_path, language)


def _extract_enum_values(elist, source: bytes) -> list:
    out = []
    for e in elist.children:
        if e.type != "enumerator":
            continue
        name_node = _first_child(e, "identifier")
        if name_node is None:
            continue
        name = _text(name_node, source)
        val = None
        eq = False
        for c in e.children:
            if c.type == "=":
                eq = True
                continue
            if eq and c.type not in (",",):
                val = _text(c, source)
                break
        out.append((name, val))
    return out


# ── top-level definition handlers ────────────────────────────────────────────

def _key_for(file_path: str, tag: Optional[str], aliases: list) -> str:
    base = tag or (aliases[0] if aliases else "<anon>")
    return f"{file_path}::{base}"


def _handle_struct_union(spec_node, source: bytes, file_path: str, out: list,
                         aliases: list, language: str) -> Optional[str]:
    if spec_node.type == "class_specifier":
        kind = "class"
    elif spec_node.type == "union_specifier":
        kind = "union"
    else:
        kind = "struct"
    tag_node = _first_child(spec_node, "type_identifier")
    tag = _text(tag_node, source) if tag_node is not None else None
    body = _first_child(spec_node, "field_declaration_list")
    if body is None:
        return None   # a reference, not a definition
    key = _key_for(file_path, tag, aliases)
    td = TypeDef(
        type_id=key, kind=kind, tag_name=tag, aliases=list(aliases),
        file=file_path, line_start=spec_node.start_point[0] + 1,
        line_end=spec_node.end_point[0] + 1, language=language,
        bases=_extract_bases(spec_node, source),
    )
    parent = getattr(spec_node, "parent", None)
    if parent is not None and parent.type == "template_declaration":
        tpl = _first_child(parent, "template_parameter_list")
        if tpl is not None:
            td.attributes["template"] = _text(tpl, source)
    out.append(td)
    td.members = _extract_members(body, source, out, key, file_path, language)
    return key


def _extract_bases(spec_node, source: bytes) -> list:
    """C++ base classes from a ``base_class_clause`` (names only, deduped)."""
    clause = _first_child(spec_node, "base_class_clause")
    if clause is None:
        return []
    names: list[str] = []
    for c in clause.children:
        if c.type == "type_identifier":
            names.append(_text(c, source))
        elif c.type in ("qualified_identifier", "template_type"):
            tid = _find_descendant(c, "type_identifier")
            if tid is not None:
                names.append(_text(tid, source))
    seen: set = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _handle_alias_declaration(node, source: bytes, file_path: str, out: list,
                              language: str) -> None:
    """C++ ``using Name = Underlying;`` → a typedef node (alias_of edge)."""
    name_node = _first_child(node, "type_identifier")
    if name_node is None:
        return
    name = _text(name_node, source)
    td_node = _first_child(node, "type_descriptor")
    target = None
    depth = 0
    if td_node is not None:
        depth = _count_descendant(td_node, "abstract_pointer_declarator")
        base = (_find_descendant(td_node, "type_identifier")
                or _find_descendant(td_node, "primitive_type")
                or _find_descendant(td_node, "sized_type_specifier"))
        target = _text(base, source) if base is not None else _text(td_node, source).strip()
    key = f"{file_path}::{name}"
    out.append(TypeDef(
        type_id=key, kind="typedef", tag_name=None, aliases=[name],
        file=file_path, line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1, language=language,
        alias_target=target, alias_pointer_depth=depth,
    ))


def _handle_enum(spec_node, source: bytes, file_path: str, out: list,
                 aliases: list, language: str) -> Optional[str]:
    tag_node = _first_child(spec_node, "type_identifier")
    tag = _text(tag_node, source) if tag_node is not None else None
    elist = _first_child(spec_node, "enumerator_list")
    if elist is None:
        return None
    key = _key_for(file_path, tag, aliases)
    td = TypeDef(
        type_id=key, kind="enum", tag_name=tag, aliases=list(aliases),
        file=file_path, line_start=spec_node.start_point[0] + 1,
        line_end=spec_node.end_point[0] + 1, language=language,
    )
    td.enum_values = _extract_enum_values(elist, source)
    out.append(td)
    return key


def _handle_typedef(node, source: bytes, file_path: str, out: list,
                    language: str) -> None:
    """``typedef <underlying> Name, *PtrName, ...;``"""
    # underlying type node = first non-'typedef' child that is a type prefix
    underlying = None
    for c in node.children:
        if c.type == "typedef":
            continue
        if c.type in ("struct_specifier", "union_specifier", "enum_specifier",
                      "type_identifier", "primitive_type", "sized_type_specifier"):
            underlying = c
            break

    # alias declarators come after the underlying type
    alias_names: list[str] = []       # plain (value) aliases
    ptr_aliases: list[tuple[str, int]] = []
    seen_underlying = False
    for c in node.children:
        if c is underlying:
            seen_underlying = True
            continue
        if not seen_underlying:
            continue
        if c.type == "type_identifier":
            alias_names.append(_text(c, source))
        elif c.type == "pointer_declarator":
            u = _unwrap_declarator(c, source)
            if u["name"]:
                ptr_aliases.append((u["name"], u["pointer"] or 1))

    body_bearing = underlying is not None and underlying.type in (
        "struct_specifier", "union_specifier", "enum_specifier"
    ) and (
        _first_child(underlying, "field_declaration_list") is not None
        or _first_child(underlying, "enumerator_list") is not None
    )

    if body_bearing:
        # define the struct/union/enum with the value aliases attached
        if underlying.type == "enum_specifier":
            key = _handle_enum(underlying, source, file_path, out, alias_names, language)
        else:
            key = _handle_struct_union(underlying, source, file_path, out, alias_names, language)
        target_ref = None
        if out and key:
            td = out[-1] if out[-1].type_id == key else None
            # find the just-added def to expose its tag/alias as pointer target
            for t in reversed(out):
                if t.type_id == key:
                    target_ref = t.tag_name or (t.aliases[0] if t.aliases else None)
                    break
    else:
        # pure alias: typedef <name/prim> Alias;
        target_ref = _text(underlying, source).strip() if underlying is not None else None
        for name in alias_names:
            key = f"{file_path}::{name}"
            out.append(TypeDef(
                type_id=key, kind="typedef", tag_name=None, aliases=[name],
                file=file_path, line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1, language=language,
                alias_target=target_ref, alias_pointer_depth=0,
            ))

    # pointer typedefs (e.g. *PFoo) become their own typedef nodes → alias_of
    for pname, depth in ptr_aliases:
        key = f"{file_path}::{pname}"
        out.append(TypeDef(
            type_id=key, kind="typedef", tag_name=None, aliases=[pname],
            file=file_path, line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1, language=language,
            alias_target=target_ref, alias_pointer_depth=depth,
        ))


# ── public entry point ───────────────────────────────────────────────────────

def extract_types_from_tree(root, source: bytes, file_path: str,
                            language: str = "c") -> list[TypeDef]:
    """Collect all top-level type definitions from a parsed tree."""
    out: list[TypeDef] = []
    _collect(root, source, file_path, out, language)
    return out


def _collect(node, source: bytes, file_path: str, out: list, language: str) -> None:
    t = node.type
    if t == "type_definition":
        _handle_typedef(node, source, file_path, out, language)
        return
    if t == "alias_declaration":
        _handle_alias_declaration(node, source, file_path, out, language)
        return
    if t in ("struct_specifier", "union_specifier", "class_specifier"):
        if _first_child(node, "field_declaration_list") is not None:
            _handle_struct_union(node, source, file_path, out, [], language)
            return
    if t == "enum_specifier":
        if _first_child(node, "enumerator_list") is not None:
            _handle_enum(node, source, file_path, out, [], language)
            return
    for c in node.children:
        _collect(c, source, file_path, out, language)
