"""Type-graph builder (Type Nodes Mode, phase B).

Consumes the raw :class:`TypeDef` records collected by the parsers' type
registry and produces a resolved, deduped :class:`TypeGraph`:

* **B1 canonical resolution** — normalise each member's referenced type token
  through the project alias table to a concrete ``type_id`` (or leave ``None``
  for primitives / external types).
* **B2 edge construction + main-structure ranking** — build containment / alias
  edges (collapsing repeated members of the same type), then rank the
  architectural root types.
* **B3 usage links** — record which functions reference each type in their
  signatures / locals (``used_by_functions``).
* **B4 header dedup** — merge duplicate definitions of the same type so a struct
  defined in a header shared by many TUs is one node.

Pure and deterministic: no renderer, no globals, fully unit-testable.
"""
from __future__ import annotations

import os
from typing import Optional

from ..models import CallGraph, FunctionDef, TypeDef, TypeEdge, TypeGraph

# Base type name suggesting a real architectural root (used as a ranking nudge).
_ROOT_HINT_TOKENS = (
    "config", "context", "ctx", "state", "manager", "system", "engine",
    "device", "session", "server", "app", "core", "registry", "table", "info",
)

# ── path helpers ─────────────────────────────────────────────────────────────

def _relpath(path: str, root: Optional[str]) -> str:
    if not path:
        return path
    try:
        if root:
            rel = os.path.relpath(path, root)
        else:
            rel = path
        return rel.replace("\\", "/")
    except ValueError:
        return os.path.basename(path).replace("\\", "/")


def _norm_id(type_id: str, root: Optional[str]) -> str:
    """Recompute a provisional ``<abs>::<key>`` id to ``<relpath>::<key>``."""
    if "::" not in type_id:
        return type_id
    file_part, _, rest = type_id.partition("::")
    return f"{_relpath(file_part, root)}::{rest}"


# ── the builder ──────────────────────────────────────────────────────────────

def build_type_graph(
    type_registry: dict,
    functions: Optional[dict] = None,
    project_root: Optional[str] = None,
) -> TypeGraph:
    """Assemble the resolved :class:`TypeGraph`."""
    functions = functions or {}

    # --- B4: normalise ids/paths + dedup identical definitions ---------------
    types: dict[str, TypeDef] = {}
    for old_id, td in type_registry.items():
        new_id = _norm_id(td.type_id, project_root)
        td.type_id = new_id
        td.file = _relpath(td.file, project_root)
        td.parent_type_id = _norm_id(td.parent_type_id, project_root) if td.parent_type_id else None
        for m in td.members:
            if m.anon_child_id:
                m.anon_child_id = _norm_id(m.anon_child_id, project_root)
        existing = types.get(new_id)
        if existing is None:
            types[new_id] = td
        else:
            _merge_typedef(existing, td)

    # --- B1: alias table + member resolution ---------------------------------
    alias_table = _build_alias_table(types)
    file_index = _build_file_index(types)
    for td in types.values():
        for m in td.members:
            if m.ref_type:
                m.canonical_type = _resolve_ref(m.ref_type, td.file, alias_table, file_index)

    # --- B2: edge construction ----------------------------------------------
    edges = _build_edges(types, alias_table, file_index)

    # --- B3: usage links -----------------------------------------------------
    _attach_usage_links(types, functions)

    # --- B2: main-structure ranking -----------------------------------------
    roots = _rank_roots(types, edges)

    tg = TypeGraph(types=types, edges=edges, roots=roots)
    tg.stats = _compute_stats(types, edges, roots, functions)
    return tg


def _merge_typedef(dst: TypeDef, src: TypeDef) -> None:
    """B4: fold a duplicate definition into the canonical one."""
    for a in src.aliases:
        if a not in dst.aliases:
            dst.aliases.append(a)
    if not dst.members and src.members:
        dst.members = src.members
    if not dst.enum_values and src.enum_values:
        dst.enum_values = src.enum_values
    if not dst.doc_comment and src.doc_comment:
        dst.doc_comment = src.doc_comment


# ── B1: alias resolution ─────────────────────────────────────────────────────

def _build_alias_table(types: dict) -> dict:
    """token → list of (file, type_id) for every name a type can be referenced by.

    Covers struct/union/enum tags, their typedef aliases, and pure typedef names
    (resolved transitively to the underlying struct where possible).
    """
    table: dict[str, list] = {}

    def add(token: str, file: str, tid: str) -> None:
        if not token:
            return
        table.setdefault(token, []).append((file, tid))

    # direct: tags + struct aliases
    for tid, td in types.items():
        if td.kind in ("struct", "union", "enum", "class"):
            if td.tag_name:
                add(td.tag_name, td.file, tid)
            for a in td.aliases:
                add(a, td.file, tid)

    # pure typedefs → resolve transitively to a concrete type where possible
    for tid, td in types.items():
        if td.kind != "typedef":
            continue
        for a in td.aliases:
            resolved = _follow_typedef(td, types, seen=set())
            add(a, td.file, resolved or tid)
    return table


def _follow_typedef(td: TypeDef, types: dict, seen: set) -> Optional[str]:
    """Chase a typedef's alias_target chain to a concrete struct/union/enum id."""
    if td.type_id in seen:
        return None
    seen.add(td.type_id)
    target = td.alias_target
    if not target:
        return None
    # find a type whose tag or alias equals the target token
    for tid, cand in types.items():
        if cand.type_id == td.type_id:
            continue
        if cand.kind in ("struct", "union", "enum", "class"):
            if cand.tag_name == target or target in cand.aliases:
                return tid
        elif cand.kind == "typedef" and target in cand.aliases:
            return _follow_typedef(cand, types, seen)
    return None


def _build_file_index(types: dict) -> dict:
    idx: dict[str, set] = {}
    for tid, td in types.items():
        idx.setdefault(td.file, set()).add(tid)
    return idx


def _resolve_ref(token: str, from_file: str, alias_table: dict, file_index: dict) -> Optional[str]:
    """Resolve a referenced type token to a type_id.

    Preference (mirrors CallIndex.resolve tiers): same-file match > unique
    project-wide match > None (ambiguous or external).
    """
    cands = alias_table.get(token)
    if not cands:
        return None
    # same-file first
    same = [tid for (f, tid) in cands if f == from_file]
    if len(same) == 1:
        return same[0]
    if same:
        return sorted(same)[0]   # deterministic tie-break
    uniq = {tid for (_f, tid) in cands}
    if len(uniq) == 1:
        return next(iter(uniq))
    return sorted(uniq)[0]       # ambiguous → deterministic pick


# ── B2: edges ────────────────────────────────────────────────────────────────

def _build_edges(types: dict, alias_table: dict, file_index: dict) -> list:
    """Containment + alias edges, collapsing repeated members of the same type."""
    # (src, dst, kind) -> [member_names]
    agg: dict[tuple, list] = {}

    for tid, td in types.items():
        # structs/unions: member containment
        for m in td.members:
            # anonymous nested type → value containment to the synthesized child
            if m.anon_child_id and m.anon_child_id in types:
                agg.setdefault((tid, m.anon_child_id, "contains_value"), []).append(m.name or "<anon>")
                continue
            dst = m.canonical_type
            if not dst:
                continue
            if m.array_dims:
                kind = "array_of"
            elif m.is_pointer:
                kind = "contains_pointer"
            else:
                kind = "contains_value"
            # skip self value/array containment (illegal in C); allow self pointer
            # so linked-list / tree self-references render as a self-loop.
            if dst == tid and kind != "contains_pointer":
                continue
            agg.setdefault((tid, dst, kind), []).append(m.name)
        # typedef alias edges
        if td.kind == "typedef" and td.alias_target:
            dst = _resolve_ref(td.alias_target, td.file, alias_table, file_index)
            if dst and dst != tid:
                agg.setdefault((tid, dst, "alias_of"), []).append(td.aliases[0] if td.aliases else "")
        # C++ inheritance edges (derived → base)
        for base in getattr(td, "bases", None) or []:
            dst = _resolve_ref(base, td.file, alias_table, file_index)
            if dst and dst != tid:
                agg.setdefault((tid, dst, "inherits"), []).append(base)

    edges = []
    for (src, dst, kind), names in sorted(agg.items()):
        clean = [n for n in names if n]
        edges.append(TypeEdge(
            src_type_id=src, dst_type_id=dst, kind=kind,
            member_names=clean, count=len(names),
        ))
    return edges


# ── B3: usage links ──────────────────────────────────────────────────────────

def _attach_usage_links(types: dict, functions: dict) -> None:
    """Record, per type, the function node_ids that reference it in signatures
    or local declarations (used_by_functions)."""
    if not functions:
        return
    # token → type_id for every user-referenceable name
    token_to_id: dict[str, str] = {}
    for tid, td in types.items():
        for tok in _referenceable_tokens(td):
            token_to_id.setdefault(tok, tid)

    import re
    ident_re = re.compile(r"[A-Za-z_]\w*")
    for fn in functions.values():
        strings = []
        if getattr(fn, "return_type", None):
            strings.append(fn.return_type)
        for p in getattr(fn, "parameters", []) or []:
            if p.type_hint:
                strings.append(p.type_hint)
        for v in getattr(fn, "variables", []) or []:
            if v.type_hint:
                strings.append(v.type_hint)
        hit_ids = set()
        for s in strings:
            for tok in ident_re.findall(s):
                tid = token_to_id.get(tok)
                if tid:
                    hit_ids.add(tid)
        for tid in hit_ids:
            types[tid].used_by_functions.append(fn.node_id)

    # deterministic ordering
    for td in types.values():
        td.used_by_functions = sorted(set(td.used_by_functions))


def _referenceable_tokens(td: TypeDef) -> list:
    toks = []
    if td.tag_name:
        toks.append(td.tag_name)
    toks.extend(td.aliases)
    return toks


# ── B2: main-structure ranking ───────────────────────────────────────────────

def _rank_roots(types: dict, edges: list) -> list:
    """Rank architectural roots: types with no incoming value-containment,
    ordered by transitive containment size, usage, and pointer fan-in."""
    value_in: dict[str, int] = {tid: 0 for tid in types}
    ptr_in: dict[str, int] = {tid: 0 for tid in types}
    children: dict[str, set] = {tid: set() for tid in types}
    for e in edges:
        if e.kind in ("contains_value", "array_of"):
            value_in[e.dst_type_id] = value_in.get(e.dst_type_id, 0) + 1
            children.setdefault(e.src_type_id, set()).add(e.dst_type_id)
        elif e.kind == "contains_pointer":
            ptr_in[e.dst_type_id] = ptr_in.get(e.dst_type_id, 0) + 1

    def transitive_size(tid: str) -> int:
        seen = set()
        stack = [tid]
        while stack:
            cur = stack.pop()
            for ch in children.get(cur, ()):
                if ch not in seen:
                    seen.add(ch)
                    stack.append(ch)
        return len(seen)

    candidates = []
    for tid, td in types.items():
        if td.is_anonymous or td.kind in ("typedef", "enum"):
            continue
        if value_in.get(tid, 0) != 0:
            continue   # embedded by value somewhere → not a root
        size = transitive_size(tid)
        usage = len(td.used_by_functions)
        hint = 1 if any(h in td.display_name.lower() for h in _ROOT_HINT_TOKENS) else 0
        candidates.append((tid, size, usage, ptr_in.get(tid, 0), hint))

    # sort by (size, usage, hint, ptr_in) desc, then id for determinism
    candidates.sort(key=lambda c: (-c[1], -c[2], -c[4], -c[3], c[0]))
    return [c[0] for c in candidates]


def _compute_stats(types: dict, edges: list, roots: list, functions: dict) -> dict:
    kinds: dict[str, int] = {}
    for td in types.values():
        kinds[td.kind] = kinds.get(td.kind, 0) + 1
    edge_kinds: dict[str, int] = {}
    for e in edges:
        edge_kinds[e.kind] = edge_kinds.get(e.kind, 0) + 1
    return {
        "types": len(types),
        "edges": len(edges),
        "roots": len(roots),
        "members": sum(len(t.members) for t in types.values()),
        "anonymous": sum(1 for t in types.values() if t.is_anonymous),
        "by_kind": kinds,
        "by_edge_kind": edge_kinds,
        "linked_functions": sum(len(t.used_by_functions) for t in types.values()),
    }


def build_type_graph_from_callgraph(
    graph: CallGraph,
    type_registry: dict,
) -> TypeGraph:
    """Convenience wrapper used by the CLI: builds and attaches the type graph."""
    tg = build_type_graph(type_registry, graph.functions, graph.project_root)
    graph.type_graph = tg
    return tg
