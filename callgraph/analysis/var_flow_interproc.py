"""Interprocedural variable-flow (VFI-2).

When a tracked variable is passed as a call argument to a project function, a
flow edge is synthesised into the callee's matching (positional) parameter,
reusing the already-resolved call edges in the graph. The pass is seeded from
every occurrence of the root variable -- including variables *created by*
custom-input functions (``lugasi`` / ``lugasian`` / ...), which are detected
and tracked exactly like ordinary variables.

This is the canonical, browser-free reference implementation of the BFS that
``_vfBuildFlowChain`` performs at render time in ``html_renderer.py``. The two
implementations share the same algorithm (argument base/full-name matching,
positional argument->parameter mapping, same-name fallback when the callee has
no parameter metadata) so this module can be unit-tested in CI without a
headless browser, and so the renderer behaviour stays pinned by tests.

Key robustness point (struct-member custom-input destinations): a destination
such as ``lugasi(&cfg.speed, ...)`` is recorded under the variable name
``cfg.speed``. Matching only the *base* name (``cfg``) would sever the flow when
``cfg.speed`` is later passed by value to a renamed parameter, so call arguments
are matched against BOTH the base name and the full member path.
"""
from __future__ import annotations

import re
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from callgraph.models import CallGraph

_CAST_RE = re.compile(r"^\([^)]+\)\s*")
_LEAD_PTR_RE = re.compile(r"^[&*]+")
_INDEX_RE = re.compile(r"\[.*$")
_TRAILING_FIELD_RE = re.compile(r"\.\w+$")


def _strip_common(expr: str) -> str:
    """Strip a leading cast, leading address-of/deref, and any array index."""
    s = (expr or "").strip()
    s = _CAST_RE.sub("", s)        # (Type) cast
    s = _LEAD_PTR_RE.sub("", s)    # & *
    s = _INDEX_RE.sub("", s)       # [idx]
    s = _LEAD_PTR_RE.sub("", s)    # & * again, after cast removal
    return s.strip()


def extract_full_var_name(expr: str) -> str:
    """Base variable expression keeping the ``.field`` member path.

    ``&cfg.speed`` -> ``cfg.speed``; ``(int)x`` -> ``x``; ``arr[i]`` -> ``arr``.
    Mirrors the JS ``_extractFullVarName``.
    """
    return _strip_common(expr)


def extract_base_var_name(expr: str) -> str:
    """Base variable name with any trailing ``.field`` access removed.

    ``&cfg.speed`` -> ``cfg``; ``imu.x_acc`` -> ``imu``. Mirrors the JS
    ``_extractBaseVarName``.
    """
    return _TRAILING_FIELD_RE.sub("", _strip_common(expr)).strip()


def _node_param_names(graph: "CallGraph") -> dict:
    names: dict = {}
    for node_id, fn in graph.functions.items():
        params = getattr(fn, "parameters", None)
        if params:
            names[node_id] = [(getattr(p, "name", "") or "") for p in params]
    return names


def scope_identity(occ: dict) -> str:
    """Stable scope-group id for an occurrence (VFI-1).

    Function-scoped occurrences (locals / parameters / heap) carry no explicit
    ``scope_id`` and fall back to their ``function_id`` -- so two same-named but
    unrelated locals in different functions are *distinct* identities. Broader
    scopes (globals -> ``"global"``, statics/consts -> ``"f:<file>"``, struct
    members -> ``"t:<type>"``) carry an explicit ``scope_id`` and therefore merge
    across the functions that share them.
    """
    return occ.get("scope_id") or occ.get("function_id") or ""


def build_interprocedural_flow(
    graph: "CallGraph",
    root_var: str,
    var_flow_data: Optional[dict] = None,
    seed_scope_id: Optional[str] = None,
    direction: str = "forward",
) -> dict:
    """Compute the interprocedural flow chain for ``root_var``.

    Returns ``{"entries": [...], "edges": [...]}`` where:
      * each *entry* is ``{function_id, function_name, local_name, orig_name,
        source_kind}`` -- an occurrence reached by the flow, with ``local_name``
        being the (possibly renamed) name the variable carries in that function;
      * each *edge* is ``{from_fn, from_var, to_fn, to_var, arg_index}`` -- an
        argument->parameter link across a resolved call site.

    ``root_var`` is matched case-insensitively. Custom-input destinations
    (``lugasi``/``lugasian`` etc.) participate as ordinary roots.

    When ``seed_scope_id`` is given (VFI-1 "split by scope"), only occurrences
    whose :func:`scope_identity` matches it seed the BFS; the search still
    expands across resolved call edges, so genuine interprocedural flow is
    preserved while unrelated same-named scopes are not merged in.

    ``direction`` selects the traversal (VFI-7):
      * ``"forward"`` (default) -- follow argument->parameter mappings into
        callees (downstream def-use, the VFI-2 behaviour).
      * ``"backward"`` -- when the tracked variable is a callee *parameter*, walk
        each caller's positional argument expression back to its originating
        variable (upstream "where did this come from?"). Edge orientation stays
        source->sink; only the discovery direction differs.
    """
    if var_flow_data is None:
        # Imported lazily to avoid a hard import cycle at module load time.
        from callgraph.renderers.html_renderer import _build_var_flow_data
        var_flow_data = _build_var_flow_data(graph)

    backward = (direction == "backward")
    norm_key = (root_var or "").lower()
    param_names = _node_param_names(graph)

    # Index resolved call edges by caller (forward) and by callee (backward).
    edges_by_caller: dict = {}
    edges_by_callee: dict = {}
    for c in graph.calls:
        if not getattr(c, "is_resolved", False):
            continue
        to_id = getattr(c, "callee_id", None)
        if not to_id:
            continue
        args = getattr(c, "call_args", None) or []
        if not args:
            continue
        edges_by_caller.setdefault(c.caller_id, []).append((to_id, args))
        edges_by_callee.setdefault(to_id, []).append((c.caller_id, args))

    entries: list = []
    flow_edges: list = []
    visited: set = set()

    def fn_display(fid: str) -> str:
        fn = graph.functions.get(fid)
        if not fn:
            return fid
        return fn.qualified_name or fn.name or fid

    def add_occs(fn_id: str, var_name: str, orig_name: str) -> None:
        key = fn_id + "::" + var_name.lower()
        if key in visited:
            return
        visited.add(key)
        for occ in var_flow_data.get(var_name.lower(), []):
            if occ.get("function_id") == fn_id:
                entries.append({
                    "function_id": fn_id,
                    "function_name": occ.get("function_name", fn_display(fn_id)),
                    "local_name": var_name,
                    "orig_name": orig_name,
                    "source_kind": occ.get("source_kind", ""),
                })

    root_occs = var_flow_data.get(norm_key, [])
    if seed_scope_id is not None:
        root_occs = [o for o in root_occs if scope_identity(o) == seed_scope_id]
    root_fns: list = []
    seen_root = set()
    for occ in root_occs:
        fid = occ.get("function_id")
        if fid not in seen_root:
            seen_root.add(fid)
            root_fns.append(fid)
            add_occs(fid, norm_key, norm_key)

    queue = [(fid, norm_key, norm_key) for fid in root_fns]
    qi = 0
    while qi < len(queue):
        fn_id, var_name, orig_name = queue[qi]
        qi += 1
        vlow = var_name.lower()

        if backward:
            # VFI-7: if var_name is a parameter of fn_id, walk each caller's
            # positional argument expression back to its originating variable.
            my_parms = param_names.get(fn_id)
            if not my_parms:
                continue
            pk = next((i for i, p in enumerate(my_parms)
                       if p and p.lower() == vlow), -1)
            if pk < 0:
                continue  # not a parameter -> origin reached on this path
            for from_id, args in edges_by_callee.get(fn_id, []):
                if pk >= len(args):
                    continue
                arg = args[pk]
                full = extract_full_var_name(arg).lower()
                base = extract_base_var_name(arg).lower()
                src_name = full if (full and full in var_flow_data) else base
                if not src_name:
                    continue
                if not any(
                    fe["from_fn"] == from_id and fe["to_fn"] == fn_id
                    and fe["from_var"].lower() == src_name
                    and fe["to_var"].lower() == vlow
                    for fe in flow_edges
                ):
                    flow_edges.append({
                        "from_fn": from_id, "from_var": src_name,
                        "to_fn": fn_id, "to_var": var_name,
                        "arg_index": pk,
                    })
                to_key = from_id + "::" + src_name
                if to_key not in visited:
                    add_occs(from_id, src_name, orig_name)
                    queue.append((from_id, src_name, orig_name))
            continue

        for to_id, args in edges_by_caller.get(fn_id, []):
            for j, arg in enumerate(args):
                base = extract_base_var_name(arg).lower()
                full = extract_full_var_name(arg).lower()
                if base != vlow and full != vlow:
                    continue
                callee_parms = param_names.get(to_id)
                if callee_parms and j < len(callee_parms) and callee_parms[j]:
                    param_name = callee_parms[j]
                    if not any(
                        fe["from_fn"] == fn_id and fe["to_fn"] == to_id
                        and fe["from_var"].lower() == vlow
                        and fe["to_var"].lower() == param_name.lower()
                        for fe in flow_edges
                    ):
                        flow_edges.append({
                            "from_fn": fn_id, "from_var": var_name,
                            "to_fn": to_id, "to_var": param_name,
                            "arg_index": j,
                        })
                    to_key = to_id + "::" + param_name.lower()
                    if to_key not in visited:
                        add_occs(to_id, param_name, orig_name)
                        queue.append((to_id, param_name, orig_name))
                elif not callee_parms:
                    # No parameter metadata -> fall back to same-name tracking.
                    to_key2 = to_id + "::" + vlow
                    if to_key2 not in visited:
                        add_occs(to_id, var_name, orig_name)
                        flow_edges.append({
                            "from_fn": fn_id, "from_var": var_name,
                            "to_fn": to_id, "to_var": var_name,
                            "arg_index": j,
                        })
                        queue.append((to_id, var_name, orig_name))
                break  # one match per call site is enough

    # VFI-3: Cross-variable assignment edges.
    # After the main BFS, scan every reached entry for same-function assignment links.
    # Build a reverse index: src_var_key → [dst_var_key] per function.
    assign_dst_index: dict = {}
    for dst_key2, occs_list in var_flow_data.items():
        for occ2 in occs_list:
            src2 = (occ2.get("assign_src") or "").lower()
            if src2:
                idx_k = occ2.get("function_id", "") + "::" + src2
                assign_dst_index.setdefault(idx_k, []).append({"dst_key": dst_key2, "occ": occ2})

    entry_snap = list(entries)  # snapshot before add_occs may grow entries
    assign_seen: set = set()

    def emit_assign_edge(from_fn: str, from_var: str, to_fn: str, to_var: str) -> None:
        k = from_fn + "::" + from_var.lower() + "→" + to_fn + "::" + to_var.lower()
        if k in assign_seen:
            return
        assign_seen.add(k)
        flow_edges.append({
            "from_fn": from_fn, "from_var": from_var,
            "to_fn": to_fn,     "to_var": to_var,
            "link_kind": "cross_var_assign",
        })

    for ent in entry_snap:
        fn_id2 = ent["function_id"]
        var_key2 = ent["local_name"].lower()
        # Upstream: find if any occurrence in this function has assign_src pointing
        # to another tracked variable.
        for occ3 in var_flow_data.get(var_key2, []):
            if occ3.get("function_id") != fn_id2:
                continue
            src3 = (occ3.get("assign_src") or "").lower()
            if not src3 or src3 not in var_flow_data:
                continue
            vis_key3 = fn_id2 + "::" + src3
            if vis_key3 not in visited:
                add_occs(fn_id2, src3, src3)
            emit_assign_edge(fn_id2, src3, fn_id2, var_key2)
        # Downstream: other tracked vars assigned FROM this variable in the same function.
        for item2 in assign_dst_index.get(fn_id2 + "::" + var_key2, []):
            dst_key3 = item2["dst_key"]
            vis_key4 = fn_id2 + "::" + dst_key3
            if vis_key4 not in visited:
                add_occs(fn_id2, dst_key3, dst_key3)
            emit_assign_edge(fn_id2, var_key2, fn_id2, dst_key3)

    return {"entries": entries, "edges": flow_edges}


def build_scope_groups(
    graph: "CallGraph",
    root_var: str,
    var_flow_data: Optional[dict] = None,
) -> list:
    """Partition a variable name into distinct scope identities (VFI-1).

    Returns a list of groups (ordered by first appearance), one per distinct
    :func:`scope_identity` among ``root_var``'s occurrences::

        [{"scope_id": str, "label": str, "seed_count": int,
          "flow": {"entries": [...], "edges": [...]}}, ...]

    ``label`` is human-readable: ``"global"`` for the program scope, otherwise
    the enclosing function name (for function-scoped locals) or the raw scope id.
    Each group's ``flow`` is the scope-seeded interprocedural chain, so a local
    that is genuinely passed to other functions still shows its real downstream
    flow while unrelated same-named locals stay in separate groups.
    """
    if var_flow_data is None:
        from callgraph.renderers.html_renderer import _build_var_flow_data
        var_flow_data = _build_var_flow_data(graph)

    norm_key = (root_var or "").lower()
    occs = var_flow_data.get(norm_key, [])

    order: list = []
    by_scope: dict = {}
    for occ in occs:
        sid = scope_identity(occ)
        if sid not in by_scope:
            by_scope[sid] = []
            order.append(sid)
        by_scope[sid].append(occ)

    def _label(sid: str, sample: dict) -> str:
        if sid == "global":
            return "global"
        if sid.startswith("m:"):
            return "member " + sid[2:]
        if sid.startswith("f:"):
            return "file " + sid[2:].rsplit("/", 1)[-1]
        if sid.startswith("t:"):
            return "type " + sid[2:]
        fn = graph.functions.get(sid)
        if fn:
            return fn.qualified_name or fn.name or sample.get("function_name", sid)
        return sample.get("function_name", sid)

    groups = []
    for sid in order:
        members = by_scope[sid]
        flow = build_interprocedural_flow(
            graph, norm_key, var_flow_data=var_flow_data, seed_scope_id=sid)
        groups.append({
            "scope_id": sid,
            "label": _label(sid, members[0]),
            "seed_count": len(members),
            "flow": flow,
        })
    return groups
