"""
Interactive HTML renderer using Pyvis.
Generates a fully self-contained .html file (vis.js embedded inline — no internet needed).

Layout strategy:
  - Weakly-connected components are identified and laid out independently.
  - Components are sorted by dominant language, then placed in rows with generous spacing.
  - Within each component, a BFS-based hierarchical layout places callers above callees.
  - Physics is disabled; nodes are freely draggable in x and y.
  - Positions persist in browser localStorage (auto-saved on drag, restored on reload).
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from ..config import Config
from ..models import CallGraph, FunctionDef, Language, ResolutionConfidence
from .base import BaseRenderer

try:
    from pyvis.network import Network
    _PYVIS_AVAILABLE = True
except ImportError:
    _PYVIS_AVAILABLE = False


# ------------------------------------------------------------------ #
# Color constants                                                     #
# ------------------------------------------------------------------ #

LANG_COLORS: dict[Language, dict] = {
    Language.PYTHON: {
        "background": "#4A90D9", "border": "#2C6FAC",
        "highlight": {"background": "#74B3F7", "border": "#2C6FAC"},
        "hover":     {"background": "#5BA3EC", "border": "#2C6FAC"},
    },
    Language.C: {
        "background": "#E8832A", "border": "#B5601A",
        "highlight": {"background": "#F9B06E", "border": "#B5601A"},
        "hover":     {"background": "#F0944A", "border": "#B5601A"},
    },
    Language.CPP: {
        "background": "#27AE60", "border": "#1A7A43",
        "highlight": {"background": "#58D68D", "border": "#1A7A43"},
        "hover":     {"background": "#2ECC71", "border": "#1A7A43"},
    },
    Language.MATLAB: {
        "background": "#8E44AD", "border": "#6C3483",
        "highlight": {"background": "#BB8FCE", "border": "#6C3483"},
        "hover":     {"background": "#A569BD", "border": "#6C3483"},
    },
}

EXTERNAL_COLOR = {
    "background": "#95A5A6", "border": "#7F8C8D",
    "highlight": {"background": "#BDC3C7", "border": "#7F8C8D"},
    "hover":     {"background": "#AAB7B8", "border": "#7F8C8D"},
}
VAR_COLOR = {
    "background": "#F0E68C", "border": "#C8B400",
    "highlight": {"background": "#FFFACD", "border": "#C8B400"},
    "hover":     {"background": "#FFFFF0", "border": "#C8B400"},
}
ENTRY_BORDER = "#FF4444"

_LANG_ORDER = {Language.PYTHON: 0, Language.C: 1, Language.CPP: 2, Language.MATLAB: 3}


# ------------------------------------------------------------------ #
# Edge style by confidence category                                   #
# ------------------------------------------------------------------ #
_EDGE_STYLE: dict[str, dict] = {
    "exact":      {"color": "#6c8ebf", "dashes": False, "width": 1.5, "opacity": 0.90},
    "heuristic":  {"color": "#e08a00", "dashes": True,  "width": 1.5, "opacity": 0.85},
    "unresolved": {"color": "#888888", "dashes": True,  "width": 1.2, "opacity": 0.70},
    "external":   {"color": "#888888", "dashes": True,  "width": 1.2, "opacity": 0.70},
    "aggregated": {"color": "#4f8cff", "dashes": False, "width": 2.6, "opacity": 0.92},
    "violation":  {"color": "#e23b3b", "dashes": False, "width": 2.6, "opacity": 0.95},
}


_RENDER_LEVEL_LABELS: dict[str, str] = {
    "function":  "Function Nodes",
    "script":    "Script Nodes",
    "folder":    "Folder Nodes",
    "module":    "Module Nodes",
    "library":   "Library Nodes",
    "namespace": "Namespace Nodes",
}


# ------------------------------------------------------------------ #
# Multi-component layout                                              #
# ------------------------------------------------------------------ #

def _compute_layout(
    graph: CallGraph,
    h_sep: int = 420,
    v_sep: int = 340,
) -> dict[str, tuple[float, float]]:
    """
    1. Find weakly-connected components.
    2. Sort components by (dominant language, descending size).
    3. Lay each component out using BFS hierarchical positioning.
    4. Pack components into rows, starting a new row when language changes
       or the row would exceed MAX_ROW_W.
    """
    all_nodes = set(graph.functions.keys())
    if not all_nodes:
        return {}

    successors: dict[str, set[str]] = defaultdict(set)
    predecessors: dict[str, set[str]] = defaultdict(set)
    undirected: dict[str, set[str]] = defaultdict(set)

    for call in graph.calls:
        c, e = call.caller_id, call.callee_id
        if e and e in all_nodes and c in all_nodes and c != e:
            successors[c].add(e)
            predecessors[e].add(c)
            undirected[c].add(e)
            undirected[e].add(c)

    # ── Find weakly-connected components ──
    visited: set[str] = set()
    components: list[list[str]] = []
    for start in sorted(all_nodes):
        if start in visited:
            continue
        comp: list[str] = []
        q: deque[str] = deque([start])
        while q:
            n = q.popleft()
            if n in visited:
                continue
            visited.add(n)
            comp.append(n)
            for nb in undirected.get(n, set()):
                if nb not in visited:
                    q.append(nb)
        components.append(comp)

    def dom_lang(comp: list[str]) -> Language:
        langs = [graph.functions[n].language for n in comp if n in graph.functions]
        return max(set(langs), key=langs.count) if langs else Language.PYTHON

    components.sort(key=lambda c: (_LANG_ORDER.get(dom_lang(c), 99), -len(c)))

    # ── Lay out one component ──
    def _layout_one(comp: list[str]) -> dict[str, tuple[float, float]]:
        comp_set = set(comp)
        roots = [n for n in comp if not predecessors.get(n)]
        if not roots:
            roots = [comp[0]]

        # Longest-path BFS with a relax-budget. We keep extending levels when a
        # longer route is found, but cap the total visits per node so cyclic
        # call graphs (e.g. module-level aggregation where Drivers <-> UI
        # creates a 2-cycle) cannot livelock the layout. `len(comp_set) + 1`
        # is enough to assign each node its longest distance from any root
        # even if every node is revisited once per other node in the worst case.
        level: dict[str, int] = {}
        visit_count: dict[str, int] = defaultdict(int)
        max_visits = len(comp_set) + 1

        q2: deque[str] = deque()
        for r in sorted(roots):
            if r not in level:
                level[r] = 0
                q2.append(r)

        while q2:
            nd = q2.popleft()
            for s in sorted(successors.get(nd, set())):
                if s not in comp_set:
                    continue
                new_lvl = level[nd] + 1
                if s not in level or level[s] < new_lvl:
                    if visit_count[s] >= max_visits:
                        continue   # cycle guard
                    visit_count[s] += 1
                    level[s] = new_lvl
                    q2.append(s)

        max_lvl = max(level.values(), default=0)
        for n in comp:
            if n not in level:
                level[n] = max_lvl + 1

        by_lvl: dict[int, list[str]] = defaultdict(list)
        for n, lvl in level.items():
            by_lvl[lvl].append(n)

        pos: dict[str, tuple[float, float]] = {}
        for lvl in sorted(by_lvl.keys()):
            nodes_here = by_lvl[lvl]
            if lvl > 0:
                def _px(n: str) -> float:
                    preds = [p for p in predecessors.get(n, set()) if p in pos and p in comp_set]
                    return sum(pos[p][0] for p in preds) / len(preds) if preds else 0.0
                nodes_here = sorted(nodes_here, key=_px)
            count = len(nodes_here)
            total_w = (count - 1) * h_sep
            for i, n in enumerate(nodes_here):
                pos[n] = (float(i * h_sep - total_w / 2), float(lvl * v_sep))
        return pos

    # ── Pack components into a global canvas ──
    H_COMP_GAP = 600   # gap between components of the same language
    V_LANG_GAP = 700   # extra vertical gap between language groups
    V_COMP_GAP = 500   # vertical gap for row-wrap within same language
    PAD = 100           # padding added around each component bounding box
    MAX_ROW_W = 6000

    final: dict[str, tuple[float, float]] = {}
    curr_x = 0.0
    curr_y = 0.0
    row_h = 0.0
    prev_lang: Optional[Language] = None

    for comp in components:
        local_pos = _layout_one(comp)
        if not local_pos:
            continue

        xs = [p[0] for p in local_pos.values()]
        ys = [p[1] for p in local_pos.values()]
        mn_x, mx_x = min(xs), max(xs)
        mn_y, mx_y = min(ys), max(ys)
        comp_w = mx_x - mn_x + h_sep
        comp_h = mx_y - mn_y + v_sep

        cl = dom_lang(comp)

        if prev_lang is not None and cl != prev_lang:
            curr_y += row_h + V_LANG_GAP
            curr_x = 0.0
            row_h = 0.0
        elif curr_x > 0 and curr_x + comp_w + PAD > MAX_ROW_W:
            curr_y += row_h + V_COMP_GAP
            curr_x = 0.0
            row_h = 0.0

        off_x = curr_x - mn_x + PAD
        off_y = curr_y - mn_y + PAD
        for nid, (lx, ly) in local_pos.items():
            final[nid] = (lx + off_x, ly + off_y)

        curr_x += comp_w + H_COMP_GAP
        row_h = max(row_h, comp_h + 2 * PAD)
        prev_lang = cl

    return final


def _layout_key(graph: CallGraph) -> str:
    ids = "".join(sorted(graph.functions.keys()))
    digest = hashlib.md5(ids.encode("utf-8", errors="replace")).hexdigest()[:10]
    return f"cg_layout_{digest}"


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _sanitize(text: str) -> str:
    return re.sub(r'[<>&"\'\\]', lambda m: {
        "<": "&lt;", ">": "&gt;", "&": "&amp;",
        '"': "&quot;", "'": "&#39;", "\\": "&#92;",
    }[m.group()], text)


def _build_node_label(fn: FunctionDef, cfg: Config) -> str:
    lines = []
    if cfg.display.show_classes and fn.parent and fn.parent not in ("<external>", fn.name):
        lines.append(f"[{fn.parent}]")

    params = ", ".join(str(p) for p in fn.parameters) if (cfg.display.show_parameters and fn.parameters) else ""
    sig = f"{fn.name}({params})"
    if cfg.display.show_return_types and fn.return_type:
        sig = f"{fn.return_type} {sig}"
    lines.append(sig)

    if cfg.display.show_filenames and fn.file_path and fn.file_path != "<external>":
        fname = Path(fn.file_path).name
        lines.append(f"{fname}:{fn.line_start}" if cfg.display.show_line_numbers else fname)
    elif cfg.display.show_line_numbers and fn.line_start:
        lines.append(f"line {fn.line_start}")

    return "\n".join(lines)


# ------------------------------------------------------------------ #
# Arrow SVGs for legend                                               #
# ------------------------------------------------------------------ #

_ARROW_SOLID = (
    '<svg class="cg-edge-line" width="38" height="14" viewBox="0 0 38 14">'
    '<defs><marker id="arr-s" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">'
    '<path d="M0,0 L6,3 L0,6 Z" fill="#6c8ebf"/></marker></defs>'
    '<line x1="0" y1="7" x2="32" y2="7" stroke="#6c8ebf" stroke-width="2" marker-end="url(#arr-s)"/>'
    '</svg>'
)
_ARROW_DASHED = (
    '<svg class="cg-edge-line" width="38" height="14" viewBox="0 0 38 14">'
    '<defs><marker id="arr-d" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">'
    '<path d="M0,0 L6,3 L0,6 Z" fill="#888"/></marker></defs>'
    '<line x1="0" y1="7" x2="32" y2="7" stroke="#888" stroke-width="1.5"'
    ' stroke-dasharray="5,3" marker-end="url(#arr-d)"/>'
    '</svg>'
)
_ARROW_VAR = (
    '<svg class="cg-edge-line" width="38" height="14" viewBox="0 0 38 14">'
    '<line x1="0" y1="7" x2="32" y2="7" stroke="#C8B400" stroke-width="1.2"'
    ' stroke-dasharray="3,3"/>'
    '</svg>'
)


# ------------------------------------------------------------------ #
# Variable Flow data builder                                          #
# ------------------------------------------------------------------ #

def _enrich_var_flow_types(result: dict, graph: "CallGraph") -> None:
    """Post-process: fill 'unknown' data_type where type can be inferred."""
    # Build per-function parameter type map and return type map
    fn_param_types: dict[str, dict[str, str]] = {}
    for node_id, fn in graph.functions.items():
        ptypes: dict[str, str] = {}
        for p in fn.parameters:
            if p.name and p.type_hint:
                ptypes[p.name.lower()] = p.type_hint
        fn_param_types[node_id] = ptypes

    for norm_key, occs in result.items():
        # Pass 1: fill from parameter type in the same function
        for occ in occs:
            if not occ["data_type"] or occ["data_type"] == "unknown":
                param_t = fn_param_types.get(occ["function_id"], {}).get(norm_key)
                if param_t:
                    occ["data_type"] = param_t

        # Pass 2: collect known types and pick the most common one
        type_votes: dict[str, int] = {}
        for occ in occs:
            t = occ["data_type"]
            if t and t not in ("unknown", "external signal"):
                type_votes[t] = type_votes.get(t, 0) + 1

        if not type_votes:
            continue

        best_type = max(type_votes, key=lambda t: type_votes[t])

        # Pass 3: propagate best known type to remaining unknowns and "external signal" placeholders
        for occ in occs:
            if not occ["data_type"] or occ["data_type"] in ("unknown", "external signal"):
                occ["data_type"] = best_type


def _build_var_flow_data(graph: "CallGraph") -> dict:
    """Aggregate variable occurrences across functions for Variable Flow Mode."""
    from collections import defaultdict
    result: dict = defaultdict(list)

    _SCOPE_KINDS = {"constant", "static", "global", "field", "environment", "dynamic"}

    def _category(sc: str, kind: str) -> str:
        if sc in ("field",) or kind == "field":
            return "member"
        if sc == "global" or kind == "global":
            return "global"
        if sc == "static" or kind == "static":
            return "static"
        if sc in ("constant",) or kind == "constant":
            return "const"
        if sc == "environment" or kind == "environment":
            return "env"
        if sc == "dynamic" or kind == "dynamic":
            return "heap"
        return "local"

    for node_id, fn in graph.functions.items():
        fp_fn = (fn.file_path or "").replace("\\", "/")
        fn_display = fn.qualified_name or fn.name or node_id

        # Function parameters → category "argument"
        for param in fn.parameters:
            pname = param.name or ""
            if not pname or pname in ("self", "cls"):
                continue
            norm = pname.lower()
            is_dead = getattr(param, "is_dead", False)
            result[norm].append({
                "name": pname,
                "category": "argument",
                "data_type": param.type_hint or "unknown",
                "file_path": fp_fn,
                "file_name": Path(fp_fn).name if fp_fn else "",
                "function_name": fn_display,
                "function_id": node_id,
                "line": fn.line_start,
                "scope": "argument",
                "action": "argument",
                "snippet": "",
                "type_hint": param.type_hint or "",
                "source_kind": "argument",
                "value": "",
                "is_dead": is_dead,
                "dead_reason": "unused parameter" if is_dead else "",
                "connect_path": "",
                "connect_input_name": "",
                "sort_priority": 2,
                "custom_input_func": "",
                "custom_input_classifier": "",
            })

        for var in fn.variables:
            name = var.name or ""
            if not name or name in ("self", "cls"):
                continue
            norm = name.lower()
            sk = (var.source_kind or "").lower()
            sc = (var.scope or "").lower()
            kind = sk if sk in _SCOPE_KINDS else sc

            if sk == "member_access":
                action = "member_access"
            elif kind == "constant":
                action = "constant"
            elif kind == "static":
                action = "static"
            elif kind == "global":
                action = "global"
            elif kind == "field":
                action = "field"
            elif kind == "environment":
                action = "env"
            elif kind == "dynamic":
                action = "heap"
            else:
                action = "assign" if (var.value or var.source_detail) else "declare"

            fp = (var.file_path or fn.file_path or "").replace("\\", "/")
            _custom_classifier = getattr(var, "custom_input_classifier", "") or ""
            _custom_func       = getattr(var, "custom_input_func", "") or ""
            # sort_priority: 0=custom_input (highest), 1=connect, 2=everything else
            _sort_pri = 0 if sk == "custom_input" else (1 if sk == "input_file_connect" else 2)
            # Build the record dict, dropping empty/default fields to shrink JSON payload
            # (significant on large .sln projects where this can be 150K+ occurrences).
            _rec = {
                "name": name,
                "category": _category(sc, kind),
                "data_type": var.type_hint or "unknown",
                "file_path": fp,
                "file_name": Path(fp).name if fp else "",
                "function_name": fn_display,
                "function_id": node_id,
                "line": var.line or 0,
                "scope": var.scope or sk or "local",
                "action": action,
                "source_kind": sk,
                "sort_priority": _sort_pri,
            }
            _snippet = (var.source_detail or var.context or var.value or "")[:80]
            if _snippet:                       _rec["snippet"] = _snippet
            if var.type_hint:                  _rec["type_hint"] = var.type_hint
            _val = (var.value or "")[:120]
            if _val:                            _rec["value"] = _val
            if getattr(var, "is_dead", False): _rec["is_dead"] = True
            _dr = getattr(var, "dead_reason", "") or ""
            if _dr:                             _rec["dead_reason"] = _dr
            _cp = getattr(var, "connect_path", "") or ""
            if _cp:                             _rec["connect_path"] = _cp
            _cin = getattr(var, "connect_input_name", "") or ""
            if _cin:                            _rec["connect_input_name"] = _cin
            if _custom_func:                    _rec["custom_input_func"] = _custom_func
            if _custom_classifier:              _rec["custom_input_classifier"] = _custom_classifier
            _parent = getattr(var, "parent_name", "") or ""
            if _parent:                         _rec["parent_name"] = _parent
            result[norm].append(_rec)

            # NOTE: We deliberately do NOT create a synthetic "input_source" upstream block
            # for `.Connect(...)` variables. The `.Connect` block itself carries PATH and
            # INPUT inline (see node-body rendering in _SIDEBAR_JS), and the LUGASI/LUGASIAN
            # block (when present for the same variable in the same function) is the correct
            # parent via the sort_priority 0 → 1 intra-function chain edge.

    for key in result:
        # sort_priority: 0=custom_input first, 1=connect, 2=normal — then by file+line
        result[key].sort(key=lambda x: (x.get("sort_priority", 2), x["file_path"], x["line"]))
    result_dict = dict(result)
    _enrich_var_flow_types(result_dict, graph)
    return result_dict


# ------------------------------------------------------------------ #
# Injected CSS                                                        #
# ------------------------------------------------------------------ #

_SIDEBAR_CSS = """
<style id="callgraph-sidebar-css">
*, *::before, *::after { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0; height: 100%; overflow: hidden;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #334155 !important; color: #e0e0e0;
}
body { display: flex !important; flex-direction: row !important; }

/* ── Sidebar ── */
#cg-sidebar {
  width: 272px; min-width: 220px; flex-shrink: 0;
  display: flex; flex-direction: column;
  background: #23272e; border-right: 1px solid #2d3139;
  overflow-y: auto; overflow-x: hidden; z-index: 100; height: 100vh;
}
#cg-header {
  padding: 12px 14px 9px;
  background: linear-gradient(135deg, #1e3a5f 0%, #14213d 100%);
  border-bottom: 1px solid #2c4a6e; flex-shrink: 0;
  position: sticky; top: 0; z-index: 10;
}
#cg-header h1 { font-size: 13px; font-weight: 700; color: #74B3F7; margin: 0; }
#cg-header .sub { font-size: 10px; color: #8090a0; margin-top: 2px; }

.cg-section { padding: 9px 11px; border-bottom: 1px solid #2d3139; flex-shrink: 0; }
.cg-section > label {
  display: block; font-size: 10px; font-weight: 600;
  color: #8090a0; text-transform: uppercase; letter-spacing: 0.7px; margin-bottom: 5px;
}
.cg-input {
  width: 100%; padding: 5px 8px; border-radius: 4px;
  border: 1px solid #3d4451; background: #1a1d23; color: #e0e0e0;
  font-size: 12px; outline: none;
}
.cg-input:focus { border-color: #4A90D9; }
.cg-select {
  background: #1a1d23; color: #ccc; border: 1px solid #3d4451;
  padding: 4px 6px; font-size: 11px; border-radius: 3px; cursor: pointer;
}
.cg-hint { font-size: 10px; color: #6a7a8a; margin-top: 3px; }

.cg-btn-row { display: flex; gap: 5px; margin-top: 5px; }
.cg-btn {
  flex: 1; padding: 5px 0; font-size: 10px; font-weight: 600;
  border: 1px solid #3d4451; background: #1a1d23; color: #bbb;
  border-radius: 4px; cursor: pointer; transition: background 0.12s;
}
.cg-btn:hover  { background: #2d3139; color: #e0e0e0; }
.cg-btn.active { background: #1e3a5f; border-color: #4A90D9; color: #74B3F7; }
.cg-btn.flash  { background: #1a3a1a; border-color: #27AE60; color: #58D68D; }

.cg-row2 { display: flex; gap: 5px; align-items: center; margin-top: 5px; font-size: 10px; color: #8090a0; }
.cg-row2 > span { flex-shrink: 0; }
.cg-row2 > select { flex: 1; }

.cg-stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; }
.cg-stat-box { background: #1a1d23; border-radius: 4px; padding: 6px 8px; border: 1px solid #2d3139; }
.cg-stat-box .val { font-size: 17px; font-weight: 700; color: #74B3F7; }
.cg-stat-box .lbl { font-size: 10px; color: #8090a0; text-transform: uppercase; }
.cg-stat-err .val { color: #e74c3c !important; }

.cg-legend-item { display: flex; align-items: flex-start; gap: 6px; font-size: 11px; margin-bottom: 4px; line-height: 1.35; }
.cg-dot { width: 11px; height: 11px; border-radius: 2px; flex-shrink: 0; margin-top: 1px; }
.cg-dot-entry { width: 11px; height: 11px; border-radius: 2px; flex-shrink: 0; margin-top: 1px; background: transparent; border: 2px solid #FF4444; }
.cg-edge-line { flex-shrink: 0; margin-top: 3px; }
.cg-legend-note { font-size: 10px; color: #5a6a7a; margin-top: 4px; }

.cg-err-panel { display: none; max-height: 100px; overflow-y: auto; background: #1a1d23; border-radius: 4px; padding: 5px; border: 1px solid #3d1515; margin-top: 4px; }
.cg-err-item { font-size: 10px; color: #e07070; padding: 1px 0; border-bottom: 1px solid #2d1515; word-break: break-all; }
#cg-err-toggle { font-size: 10px; color: #e74c3c; cursor: pointer; margin-top: 4px; display: inline-block; }

/* ── Hover popup ── */
#cg-hover-popup {
  display: none; position: fixed; z-index: 500;
  background: #1e2530; border: 1px solid #4A90D9; border-radius: 8px;
  padding: 11px 13px; min-width: 230px; max-width: 360px;
  font-size: 12px; box-shadow: 0 8px 28px rgba(0,0,0,0.6);
  pointer-events: none; animation: cg-fadein 0.1s ease;
}
#cg-edge-popup {
  display: none; position: fixed; z-index: 650;
  background: #1e2530; border: 1px solid #F7D774; border-radius: 8px;
  padding: 13px 15px; width: min(360px, calc(100vw - 24px));
  font-size: 12px; box-shadow: 0 10px 30px rgba(0,0,0,0.65);
  animation: cg-fadein 0.1s ease;
}
.cg-edge-close {
  position: absolute; top: 7px; right: 9px; background: none; border: none;
  color: #8da0b0; cursor: pointer; font-size: 16px; line-height: 1; padding: 2px 5px;
}
.cg-edge-close:hover { color: #e0e0e0; }
.cg-edge-title {
  color: #F7D774; font-weight: 700; font-size: 13px;
  margin: 0 24px 8px 0; word-break: break-word;
}
.cg-edge-row { display: flex; gap: 8px; margin: 4px 0; align-items: flex-start; }
.cg-edge-label {
  color: #6a7a8a; min-width: 70px; flex-shrink: 0;
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.4px; padding-top: 1px;
}
.cg-edge-value { color: #ddd; font-family: monospace; line-height: 1.4; word-break: break-word; }
.cg-edge-args {
  margin-top: 8px; padding: 7px 9px; background: #1a1d23;
  border: 1px solid #2d3139; border-radius: 4px;
}
.cg-edge-arg {
  font-family: monospace; color: #ddd; padding: 3px 0; word-break: break-all;
  display: flex; gap: 8px; flex-wrap: wrap; align-items: baseline;
}
.cg-edge-arg-type { color: #74B3F7; font-size: 11px; }
.cg-edge-empty { color: #5a6a7a; font-size: 11px; font-style: italic; }
@keyframes cg-fadein { from { opacity:0; transform:translateY(3px); } to { opacity:1; transform:none; } }
.hp-badges { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 5px; }
.hp-badge {
  display: inline-block; padding: 1px 7px; border-radius: 3px;
  font-size: 10px; font-weight: 700; letter-spacing: 0.3px;
}
.hp-lang-python  { background:#1a3a5f; color:#74B3F7; }
.hp-lang-c       { background:#4a2a0a; color:#F9B06E; }
.hp-lang-cpp     { background:#0a3a1a; color:#58D68D; }
.hp-lang-matlab  { background:#3a1a4a; color:#BB8FCE; }
.hp-lang-ext     { background:#2a2a2a; color:#BDC3C7; }
.hp-ftype        { background:#2a2e38; color:#99aabb; }
.hp-name  { font-size: 13px; font-weight: 700; color: #74B3F7; margin-bottom: 6px; word-break: break-all; }
.hp-doc   { font-style: italic; font-size: 11px; color: #8da0b0; margin-bottom: 6px; line-height: 1.35; }
.hp-row   { display: flex; gap: 5px; margin-bottom: 2px; font-size: 11px; }
.hp-lbl   { color: #6a7a8a; min-width: 70px; flex-shrink: 0; font-size: 10px; text-transform: uppercase; letter-spacing: 0.4px; padding-top: 1px; }
.hp-val   { color: #ddd; font-family: monospace; word-break: break-all; line-height: 1.4; }
.hp-cat   { font-size: 9px; color: #6a7a8a; margin-left: 4px; }
.hp-divider { border-top: 1px solid #2d3139; margin: 5px 0; }
.hp-section-hdr { font-size: 10px; text-transform: uppercase; color: #6a7a8a; letter-spacing: 0.5px; margin-bottom: 3px; }

/* ── Detail side panel ── */
#cg-detail {
  position: fixed; top: 0; right: 0; width: 300px; height: 100vh;
  background: #23272e; border-left: 1px solid #333;
  transform: translateX(100%); transition: transform 0.2s ease;
  z-index: 300; overflow-y: auto; padding: 16px; font-size: 12px;
}
#cg-detail.open { transform: translateX(0); }
#cg-detail-close { float: right; cursor: pointer; font-size: 17px; color: #8090a0; line-height: 1; }
#cg-detail-close:hover { color: #e0e0e0; }
#cg-detail-title { font-size: 13px; font-weight: 700; color: #74B3F7; margin-bottom: 8px; margin-right: 24px; word-break: break-all; }
.cg-ds { margin-top: 9px; }
.cg-ds h3 { font-size: 10px; text-transform: uppercase; color: #8090a0; margin-bottom: 4px; letter-spacing: 0.7px; }
.cg-ds p  { color: #ccc; margin-bottom: 2px; font-family: monospace; font-size: 11px; }
.cg-ci    { padding: 2px 0; border-bottom: 1px solid #2d3139; color: #bbb; font-family: monospace; font-size: 11px; cursor: pointer; }
.cg-ci:hover { color: #74B3F7; }

/* ── Double-click detail modal ── */
#cg-modal {
  display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.78); z-index: 900;
  align-items: center; justify-content: center; padding: 20px;
}
#cg-modal-card {
  background: #23272e; border: 1px solid #4A90D9; border-radius: 12px;
  width: min(90vw, 720px); max-height: 87vh; overflow-y: auto;
  padding: 22px 26px 26px; box-shadow: 0 20px 60px rgba(0,0,0,0.85);
  position: relative; color: #e0e0e0; font-size: 12px;
}
#cg-modal-close {
  position: absolute; top: 12px; right: 16px;
  cursor: pointer; font-size: 22px; color: #8090a0; line-height: 1;
  background: none; border: none; padding: 2px 6px;
}
#cg-modal-close:hover { color: #e0e0e0; }
.cg-modal-title { font-size: 16px; font-weight: 700; color: #74B3F7; margin-bottom: 3px; margin-right: 40px; word-break: break-all; }
.cg-modal-qname { font-size: 11px; color: #8090a0; margin-bottom: 12px; font-family: monospace; word-break: break-all; }
.cg-modal-section { margin-top: 14px; padding-top: 14px; border-top: 1px solid #2d3139; }
.cg-modal-section h3 { font-size: 10px; text-transform: uppercase; color: #6a7a8a; letter-spacing: 0.8px; margin-bottom: 8px; }
.cg-modal-row { display: flex; gap: 8px; margin-bottom: 5px; font-size: 12px; align-items: flex-start; }
.cg-modal-lbl { color: #6a7a8a; min-width: 130px; flex-shrink: 0; font-size: 10px; text-transform: uppercase; letter-spacing: 0.4px; padding-top: 2px; }
.cg-modal-val { color: #ddd; font-family: monospace; word-break: break-all; line-height: 1.5; }
.cg-modal-param { background: #1a1d23; border-radius: 4px; padding: 5px 10px; margin-bottom: 4px; font-family: monospace; font-size: 11px; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.cg-modal-param-num { color: #6a7a8a; min-width: 20px; }
.cg-modal-param-name { color: #ddd; }
.cg-modal-param-type { color: #74B3F7; }
.cg-modal-var-group { margin: 8px 0 10px; }
.cg-modal-var-head { color: #8090a0; font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; margin: 8px 0 5px; }
.cg-modal-var-scope { min-width: 78px; color: #6a7a8a; text-transform: uppercase; font-size: 9px; }
.cg-modal-var-line { color: #5a6a7a; margin-left: auto; }
.cg-modal-var-source { color: #8da0b0; }
.cg-modal-var-empty { color: #4f5c68; font-size: 10px; padding: 4px 10px; background: #181b21; border-radius: 4px; }
.cg-modal-ci { padding: 5px 10px; margin-bottom: 4px; background: #1a1d23; border-radius: 4px; cursor: pointer; font-family: monospace; font-size: 11px; color: #bbb; border: 1px solid #2d3139; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.cg-modal-ci:hover { color: #74B3F7; border-color: #4A90D9; background: #1e2a3a; }
.cg-modal-ci-nocursor { cursor: default; }
.cg-modal-ci-nocursor:hover { color: #bbb; border-color: #2d3139; background: #1a1d23; }
.cg-modal-note { font-size: 10px; color: #5a6a7a; font-style: italic; margin-top: 6px; }
.cg-modal-doc { font-style: italic; color: #8da0b0; line-height: 1.55; background: #1a1d23; border-left: 2px solid #4A90D9; padding: 8px 12px; border-radius: 0 4px 4px 0; font-size: 11px; white-space: pre-wrap; word-break: break-word; }

/* ── Canvas ── Pyvis wraps #mynetwork in a Bootstrap .card div ── */
body > .card {
  flex: 1 !important; width: auto !important; height: 100vh !important;
  padding: 0 !important; margin: 0 !important; min-width: 0 !important;
  border: none !important; border-radius: 0 !important;
  background: transparent !important;
}
#mynetwork {
  width: 100% !important; height: 100% !important;
  background: #334155 !important;
  border: none !important; position: relative !important;
  flex: 1 !important; padding: 0 !important;
}

/* ── Script view graph canvas ── */
#cg-script-view {
  display: none; flex: 1; height: 100vh;
  overflow: hidden; background: #334155; position: relative;
}
#cg-sv-viewport {
  width: 100%; height: 100%; position: relative; overflow: hidden;
  cursor: grab; -webkit-user-select: none; user-select: none;
}
#cg-sv-viewport.sv-panning { cursor: grabbing; }
#cg-sv-canvas { position: absolute; top: 0; left: 0; transform-origin: 0 0; }
.cg-marquee-box {
  position: absolute; z-index: 9999; pointer-events: none;
  border: 1px solid #74b3f7; background: rgba(116,179,247,0.18);
  box-shadow: inset 0 0 0 1px rgba(116,179,247,0.25);
}
#cg-sv-edges  { position: absolute; top: 0; left: 0; overflow: visible; pointer-events: auto; }
.cg-sv-edge {
  pointer-events: stroke; cursor: pointer;
  transition: stroke-width 0.12s, opacity 0.12s, stroke 0.12s;
}
.cg-sv-edge:hover { stroke: #F7D774; opacity: 0.95; stroke-width: 3; }
.cg-sv-edge.sv-edge-active { stroke: #F7D774 !important; opacity: 1 !important; stroke-width: 4 !important; }
.cg-file-card {
  position: absolute; z-index: 10;
  background: #23272e; border: 1px solid #2d3139; border-radius: 8px;
  overflow: hidden; box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  transition: box-shadow 0.15s, opacity 0.15s;
}
.cg-file-card:hover { box-shadow: 0 6px 22px rgba(0,0,0,0.7); }
.cg-file-card.sv-dim { opacity: 0.18; }
.cg-file-card.sv-selected { border-color: #4A90D9; }
.cg-file-card.sv-multi-selected { border-color: #F7D774; box-shadow: 0 0 0 2px rgba(247,215,116,0.38); }
.cg-fc-header {
  display: flex; align-items: center; gap: 8px; padding: 8px 12px;
  background: #1e2530; border-bottom: 1px solid #2d3139; cursor: move;
}
.cg-fc-header:hover { background: #252c3a; }
.cg-fc-collapse-btn {
  flex-shrink: 0; width: 16px; height: 16px; border: none; background: none;
  color: #6a7a8a; font-size: 10px; cursor: pointer; padding: 0; line-height: 1;
  display: flex; align-items: center; justify-content: center; border-radius: 3px;
  transition: color 0.15s, background 0.15s;
}
.cg-fc-collapse-btn:hover { color: #c0c8d4; background: #2d3545; }
.cg-file-card.sv-collapsed .cg-fn-list { display: none; }
.cg-file-card.sv-collapsed .cg-fc-header { border-bottom: none; }
.cg-fc-fname { font-size: 13px; font-weight: 700; color: #e0e0e0; white-space: nowrap; }
.cg-fc-dir { font-size: 10px; color: #5a6a7a; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cg-fc-count { font-size: 10px; color: #8090a0; background: #1a1d23; border-radius: 10px; padding: 1px 7px; flex-shrink: 0; }
.cg-fn-list { padding: 2px 0; }
.cg-fn-row {
  padding: 5px 12px; border-bottom: 1px solid #2d3139; cursor: pointer;
  transition: background 0.1s, border-left 0.1s, opacity 0.15s;
}
.cg-fn-row:last-child { border-bottom: none; }
.cg-fn-row:hover { background: #1e2530; }
.cg-fn-row.sv-selected { background: #1a2a3a !important; border-left: 3px solid #4A90D9; padding-left: 9px; }
.cg-fn-row.sv-match    { background: #1a2a1a !important; border-left: 3px solid #27AE60; padding-left: 9px; }
.cg-fn-row.sv-dim      { opacity: 0.18; }
.cg-fn-row.sv-edge-muted { opacity: 0.34; }
.cg-fn-row.sv-edge-endpoint { background: #342f18 !important; border-left: 3px solid #F7D774; padding-left: 9px; opacity: 1 !important; }
.cg-fn-top { display: flex; align-items: baseline; gap: 5px; flex-wrap: wrap; }
.cg-fn-typebadge {
  font-size: 9px; font-weight: 700; padding: 1px 5px; border-radius: 3px;
  flex-shrink: 0; letter-spacing: 0.3px;
}
.cg-fn-nm { font-family: monospace; font-size: 12px; color: #e0e0e0; }
.cg-fn-nm-main { font-weight: 700; color: #74B3F7; }
.cg-fn-sig { font-family: monospace; font-size: 11px; color: #8090a0; }
.cg-fn-callees { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
.cg-cb {
  font-size: 10px; padding: 1px 6px; border-radius: 10px; cursor: pointer;
  font-family: monospace; max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  transition: background 0.1s;
}
.cg-cb-same  { background: #0a2a1a; color: #58D68D; border: 1px solid #1a4a2a; }
.cg-cb-cross { background: #2a1a0a; color: #F9B06E; border: 1px solid #4a2a0a; }
.cg-cb-ext   { background: #1a1a1a; color: #95A5A6; border: 1px solid #2d2d2d; cursor: default; }
.cg-cb-more  { background: #1a1d23; color: #5a6a7a; border: 1px solid #2d3139; cursor: default; }
.cg-cb-same:hover  { background: #0a3a2a; }
.cg-cb-cross:hover { background: #3a2a0a; }
.cg-fn-meta { font-size: 10px; color: #5a6a7a; margin-top: 3px; }

/* ── Variable Flow view ── */
#cg-varflow-view {
  display: none; flex: 1; height: 100vh;
  flex-direction: column; overflow: hidden;
  background: #334155; position: relative;
}
#cg-vf-topbar {
  padding: 12px 16px 11px; background: #23272e;
  border-bottom: 1px solid #2d3139; flex-shrink: 0;
}
#cg-vf-topbar h2 {
  font-size: 10px; font-weight: 600; color: #8090a0;
  text-transform: uppercase; letter-spacing: 0.7px; margin: 0 0 9px;
}
/* Row that contains the search bar + action buttons */
#cg-vf-topbar-row {
  display: flex; gap: 8px; align-items: center;
}
#cg-vf-search-wrap { position: relative; display: flex; gap: 6px; flex: 1; min-width: 0; }
#cg-vf-search-input {
  flex: 1; padding: 8px 12px; border: 1px solid #3d4451; border-radius: 5px;
  background: #1a1d23; color: #e0e0e0; font-size: 13px; outline: none;
}
#cg-vf-search-input:focus { border-color: #4A90D9; }
#cg-vf-search-clear {
  padding: 7px 11px; border: 1px solid #3d4451; border-radius: 5px;
  background: #1a1d23; color: #8090a0; cursor: pointer; font-size: 15px; line-height: 1;
}
#cg-vf-search-clear:hover { background: #2d3139; color: #e0e0e0; }
#cg-vf-dropdown {
  position: absolute; top: calc(100% + 4px); left: 0; right: 52px;
  background: #23272e; border: 1px solid #3d4451; border-radius: 5px;
  max-height: 220px; overflow-y: auto; z-index: 200; display: none;
  box-shadow: 0 8px 24px rgba(0,0,0,0.65);
}
.cg-vf-dd-item {
  padding: 8px 12px; cursor: pointer; color: #d0d0d0; font-size: 12px;
  display: flex; align-items: center; gap: 8px; border-bottom: 1px solid #2d3139;
}
.cg-vf-dd-item:last-child { border-bottom: none; }
.cg-vf-dd-item:hover { background: #1e3050; }
.cg-vf-dd-name { font-family: monospace; font-weight: 600; }
.cg-vf-dd-mark { color: #569cd6; }
.cg-vf-dd-count {
  margin-left: auto; font-size: 10px; color: #6a7a8a;
  background: #1a1d23; padding: 1px 7px; border-radius: 8px; flex-shrink: 0;
}
#cg-vf-graph-area { flex: 1; position: relative; overflow: hidden; }
#cg-vf-viewport {
  position: absolute; left: 0; top: 0; right: 0; bottom: 0;
  overflow: hidden; cursor: grab;
  -webkit-user-select: none; user-select: none;
}
#cg-vf-viewport.vf-panning { cursor: grabbing; }
#cg-vf-canvas { position: absolute; top: 0; left: 0; transform-origin: 0 0; }
#cg-vf-placeholder {
  position: absolute; left: 50%; top: 50%;
  transform: translate(-50%, -50%);
  text-align: center; color: #4a5560; font-size: 14px; line-height: 2.2;
  pointer-events: none; z-index: 5;
}

/* ── VarFlow block ── */
.cg-vf-node {
  position: absolute; background: #1e2129; border: 1.5px solid #3d4451;
  border-radius: 8px; width: 240px; cursor: grab; box-sizing: border-box;
  transition: border-color 0.15s, box-shadow 0.15s; overflow: hidden;
  -webkit-user-select: none; user-select: none; z-index: 10;
}
.cg-vf-node:hover { border-color: #4A90D9; box-shadow: 0 0 12px rgba(74,144,217,0.28); }
.cg-vf-node.vf-selected { border-color: #F7D774; box-shadow: 0 0 14px rgba(247,215,116,0.36); }
.cg-vf-node.vf-multi-selected { border-color: #74b3f7; box-shadow: 0 0 0 2px rgba(116,179,247,0.42); }
.cg-vf-node.vf-dragging { cursor: grabbing; opacity: 0.90; box-shadow: 0 8px 30px rgba(0,0,0,0.60); z-index: 999; }
.cg-vf-node-header {
  display: flex; align-items: center; gap: 5px; flex-wrap: wrap;
  padding: 6px 9px 5px; border-bottom: 1px solid #2a2f38; background: #23272e;
}
/* category badges */
.cg-vf-cat-badge {
  font-size: 9px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.5px; padding: 2px 7px; border-radius: 3px; flex-shrink: 0;
}
.cg-vfc-local    { background: #142030; color: #7eb8f7; border: 1px solid #2a3a5a; }
.cg-vfc-global   { background: #2a0a0a; color: #f47070; border: 1px solid #4a1a1a; }
.cg-vfc-static   { background: #1e1e0a; color: #dcdcaa; border: 1px solid #3a3a1a; }
.cg-vfc-argument { background: #0a2a1a; color: #4ec9b0; border: 1px solid #1a4a30; }
.cg-vfc-return   { background: #0a2810; color: #6dda8a; border: 1px solid #1a4824; }
.cg-vfc-member   { background: #2a0a2a; color: #c586c0; border: 1px solid #4a1a4a; }
.cg-vfc-const    { background: #2a1a0a; color: #ce9178; border: 1px solid #4a2a0a; }
.cg-vfc-env      { background: #0a1a2a; color: #9cdcfe; border: 1px solid #1a3a4a; }
.cg-vfc-heap         { background: #1a0a1a; color: #d7ba7d; border: 1px solid #3a1a2a; }
/* action badges */
.cg-vf-action-badge {
  font-size: 9px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.4px; padding: 2px 7px; border-radius: 3px; margin-left: auto; flex-shrink: 0;
}
.cg-vfa-declare  { background: #0a2a1a; color: #4ec9b0; border: 1px solid #2a5a3a; }
.cg-vfa-assign   { background: #0a1a2a; color: #569cd6; border: 1px solid #1a3a5a; }
.cg-vfa-argument { background: #0a2a18; color: #4ec9b0; border: 1px solid #1a4a30; }
.cg-vfa-field    { background: #2a0a2a; color: #c586c0; border: 1px solid #4a1a4a; }
.cg-vfa-constant { background: #2a1a0a; color: #ce9178; border: 1px solid #4a2a0a; }
.cg-vfa-global   { background: #2a0a0a; color: #f47070; border: 1px solid #4a1a1a; }
.cg-vfa-static   { background: #1a1a0a; color: #dcdcaa; border: 1px solid #3a3a1a; }
.cg-vfa-env      { background: #0a1a2a; color: #9cdcfe; border: 1px solid #1a3a4a; }
.cg-vfa-heap         { background: #1a0a1a; color: #d7ba7d; border: 1px solid #3a1a2a; }
/* block body */
.cg-vf-node-body { padding: 7px 10px 8px; }
.cg-vf-var-row { display: flex; align-items: baseline; gap: 5px; margin-bottom: 5px; }
.cg-vf-var-label { font-size: 9px; color: #4a5a6a; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; flex-shrink: 0; min-width: 30px; }
.cg-vf-var-name { font-size: 13px; font-weight: 700; color: #d4d4d4; font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cg-vf-info-row { display: flex; align-items: baseline; gap: 5px; margin-top: 3px; }
.cg-vf-info-label { font-size: 9px; color: #4a5a6a; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; flex-shrink: 0; min-width: 30px; }
.cg-vf-info-val { font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cg-vf-type-val  { color: #74B3F7; font-family: monospace; }
.cg-vf-fn-val    { color: #9090a0; }
.cg-vf-file-val  { color: #5a6a7a; font-size: 10px; }
.cg-vf-snippet {
  font-size: 10px; color: #9cdcfe; background: #141720; padding: 5px 9px;
  border-top: 1px solid #2a2f38; font-family: Consolas, monospace;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
/* edges — blue palette matching Function/Script mode */
.cg-vf-edge       { stroke: #6c8ebf; stroke-width: 2;   fill: none; opacity: 0.85; }
.cg-vf-edge-call  { /* inherits */ }
.cg-vf-edge-chain { /* inherits */ }
.cg-vf-edge-same  { stroke-dasharray: 5,4; stroke-width: 1.5; opacity: 0.65; }
/* selected edge turns yellow (matches Function-mode edge-click highlight) */
.cg-vf-edge.vf-edge-selected { stroke: #F7D774; stroke-width: 3.5; opacity: 1; }
.cg-vf-var-orig { font-size:10px; color:#8899aa; margin-left:3px; font-style:italic; }
/* dim for non-highlighted blocks */
.cg-vf-node.vf-dim { opacity: 0.28; }
/* branch-highlight (VF-2): merge point reached by >1 coloured branch */
.cg-vf-node.vf-merge { border-style: dashed !important; }
/* branch-highlight legend overlay */
#cg-vf-legend {
  position: absolute; top: 10px; right: 10px; z-index: 40;
  background: rgba(20,23,32,0.92); border: 1px solid #3d4451; border-radius: 8px;
  padding: 8px 10px; font-size: 11px; color: #c8d2dc; max-width: 220px;
  box-shadow: 0 4px 18px rgba(0,0,0,0.45); display: none;
}
#cg-vf-legend .cg-vf-legend-title { font-weight: 600; margin-bottom: 6px; color: #e6edf3; }
#cg-vf-legend .cg-vf-legend-row { display: flex; align-items: center; gap: 7px; margin: 3px 0; }
#cg-vf-legend .cg-vf-legend-swatch { width: 14px; height: 14px; border-radius: 3px; flex: 0 0 auto; }
#cg-vf-legend .cg-vf-legend-hint { margin-top: 7px; color: #7d8a98; font-size: 10px; line-height: 1.35; }
#cg-vf-legend .cg-vf-legend-clear {
  margin-top: 7px; width: 100%; cursor: pointer; background: #2a2f38; color: #c8d2dc;
  border: 1px solid #3d4451; border-radius: 5px; padding: 4px 0; font-size: 11px;
}
#cg-vf-legend .cg-vf-legend-clear:hover { background: #343b46; }

/* ── VarFlow details modal ── */
#cg-vf-modal {
  display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.74);
  z-index: 2000; align-items: center; justify-content: center;
}
#cg-vf-modal.open { display: flex; }
#cg-vf-modal-card {
  background: #23272e; border: 1px solid #3d4451; border-radius: 10px;
  width: 540px; max-width: 92vw; max-height: 82vh; overflow-y: auto;
  box-shadow: 0 20px 60px rgba(0,0,0,0.75); position: relative;
}
#cg-vf-modal-close {
  position: absolute; top: 12px; right: 14px; background: none; border: none;
  color: #8090a0; font-size: 22px; cursor: pointer; line-height: 1; z-index: 10;
}
#cg-vf-modal-close:hover { color: #e0e0e0; }
#cg-vf-modal-title {
  padding: 15px 44px 13px 18px; border-bottom: 1px solid #2d3139;
  font-size: 15px; font-weight: 700; color: #d4d4d4; font-family: monospace;
  background: #1e2129; border-radius: 10px 10px 0 0;
}
#cg-vf-modal-body {}
.cg-vf-modal-section { padding: 11px 18px; border-bottom: 1px solid #2a2f38; }
.cg-vf-modal-section:last-child { border-bottom: none; padding-bottom: 14px; }
.cg-vf-modal-section-title {
  font-size: 9px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.7px; color: #5a6a7a; margin-bottom: 8px;
}
.cg-vf-modal-row { display: flex; align-items: baseline; gap: 10px; margin-bottom: 5px; }
.cg-vf-modal-row:last-child { margin-bottom: 0; }
.cg-vf-modal-label {
  font-size: 10px; font-weight: 700; color: #5a6a7a; min-width: 68px;
  text-transform: uppercase; letter-spacing: 0.4px; flex-shrink: 0;
}
.cg-vf-modal-value { font-size: 12px; color: #c0c0d0; word-break: break-all; }
.cg-vf-modal-value.mono { font-family: monospace; color: #9cdcfe; }
.cg-vf-modal-action-desc { font-size: 12px; color: #9ab0c0; line-height: 1.5; }
.cg-vf-modal-code {
  background: #141720; border-radius: 5px; padding: 10px 12px; margin-top: 6px;
  font-family: Consolas, monospace; font-size: 12px; color: #9cdcfe;
  white-space: pre-wrap; word-break: break-all; border: 1px solid #2a2f38;
  max-height: 180px; overflow-y: auto;
}

/* ── Dead variable node styling ── */
.cg-vf-node.cg-vf-dead {
  border: 2px solid #e74c3c !important;
  box-shadow: 0 0 8px rgba(231, 76, 60, 0.45);
}
.cg-vf-dead-badge {
  display: inline-block; padding: 1px 6px; border-radius: 3px;
  font-size: 9px; font-weight: 700; text-transform: uppercase;
  background: #4a1010; color: #e74c3c; border: 1px solid #7a1a1a;
  margin-left: 4px;
}
/* .connect() receiver node — purple border */
.cg-vf-node.cg-vf-connect-input {
  border: 2px solid #9b59b6 !important;
  background: #1a0d28 !important;
}
/* member-access node (var.x / var.x()) — teal accent */
.cg-vf-node.cg-vf-member-access {
  border-left: 3px solid #1abc9c !important;
}
/* "connected" badge for .connect() blocks — purple */
.cg-vf-connect-badge {
  display: inline-block; padding: 1px 6px; border-radius: 3px;
  font-size: 9px; font-weight: 700; text-transform: uppercase;
  background: #2a1040; color: #c586c0; border: 1px solid #5a2080;
}
/* Memory op badges */
.cg-vf-memop-badge {
  display: inline-block; padding: 1px 6px; border-radius: 3px;
  font-size: 9px; font-weight: 700; text-transform: uppercase;
  background: #1a1a30; color: #c586c0; border: 1px solid #4a2060;
}
/* Custom-input classifier badge (WOW_NA / WOW_LI from LUGASI/LUGASIAN template) */
.cg-vf-classifier-badge {
  display: inline-block; padding: 1px 6px; border-radius: 3px;
  font-size: 9px; font-weight: 700; text-transform: uppercase;
  background: #0a1e1e; color: #4dc4c4; border: 1px solid #1a4a4a;
  margin-left: 2px;
}
/* LUGASI/LUGASIAN blocks — light-cyan/turquoise border */
.cg-vf-node.cg-vf-custom-input {
  border: 2px solid #4dc4c4 !important;
  background: #091e1e !important;
}
/* Note indicator on nodes */
.cg-vf-note-dot {
  position: absolute; top: 5px; right: 28px;
  width: 14px; height: 14px; border-radius: 50%;
  background: #f0a500; color: #1a1d23;
  font-size: 9px; font-weight: 700; line-height: 14px; text-align: center;
  cursor: pointer; z-index: 5; flex-shrink: 0;
  box-shadow: 0 1px 4px rgba(0,0,0,0.5);
}
.cg-vf-node { position: absolute; }
/* Context menu */
#cg-vf-ctx-menu {
  position: fixed; background: #23272e; border: 1px solid #3d4451;
  border-radius: 5px; padding: 4px 0; z-index: 9000; display: none;
  box-shadow: 0 6px 20px rgba(0,0,0,0.7); min-width: 140px;
}
.cg-vf-ctx-item {
  padding: 7px 14px; font-size: 12px; color: #d0d0d0; cursor: pointer;
}
.cg-vf-ctx-item:hover { background: #1e3050; color: #fff; }
.cg-vf-ctx-sep { height: 1px; background: #2d3139; margin: 3px 0; }
/* Annotation rectangles on VF canvas */
.cg-vf-annot {
  position: absolute; border: 2px solid rgba(70, 200, 100, 0.85);
  background: rgba(70, 200, 100, 0.25); border-radius: 4px;
  pointer-events: all; cursor: move; z-index: 1;
  min-width: 80px; min-height: 40px;
}
.cg-vf-annot-label {
  position: absolute; top: 6px; left: 9px; right: 28px;
  font-size: 16px; color: rgba(120, 240, 140, 0.9);
  pointer-events: none; white-space: pre-wrap; word-break: break-word;
  text-shadow: 0 1px 3px rgba(0,0,0,0.8); line-height: 1.3;
}
/* Annotation picker — font-size selector row */
.cg-ap-size-row { display: flex; align-items: center; gap: 6px; margin: 6px 0 4px; }
.cg-ap-size-lbl { font-size: 11px; color: #8090a0; flex-shrink: 0; }
.cg-ap-size-btn {
  font-size: 11px; font-weight: 600; padding: 2px 8px;
  border: 1px solid #3d4451; border-radius: 4px;
  cursor: pointer; color: #b0b8c8; background: #232a3a; user-select: none;
}
.cg-ap-size-btn.selected { border-color: #4ec9b0; color: #4ec9b0; background: #0e2535; }
.cg-vf-annot-del {
  position: absolute; top: 2px; right: 4px;
  font-size: 12px; color: rgba(200,200,200,0.5); cursor: pointer;
  background: none; border: none; line-height: 1; padding: 2px;
}
.cg-vf-annot-del:hover { color: #e74c3c; }
.cg-vf-annot-resize {
  position: absolute; bottom: 0; right: 0; width: 14px; height: 14px;
  cursor: se-resize; background: rgba(70,200,100,0.25); border-radius: 0 0 3px 0;
}
/* Dead vars panel button */
#cg-vf-dead-btn {
  padding: 4px 10px; font-size: 11px; font-weight: 600;
  border: 1px solid #7a1a1a; background: #1a1d23; color: #e74c3c;
  border-radius: 4px; cursor: pointer; white-space: nowrap; flex-shrink: 0;
}
#cg-vf-dead-btn.active { background: #4a1010; border-color: #e74c3c; }
#cg-vf-dead-btn:hover { background: #3a0e0e; }
/* Draw annotation button */
#cg-vf-annot-btn {
  padding: 4px 10px; font-size: 11px; font-weight: 600;
  border: 1px solid #3d4451; background: #1a1d23; color: #4ec980;
  border-radius: 4px; cursor: pointer; white-space: nowrap; flex-shrink: 0;
}
#cg-vf-annot-btn.active { background: #0e2a18; border-color: #4ec980; }
#cg-vf-annot-btn:hover { background: #112010; }
/* Annotation color-picker modal */
#cg-annot-picker {
  display: none; position: fixed; top: 40%; left: 50%;
  transform: translate(-50%, -50%);
  background: #1a1d23; border: 1px solid #3d4451; border-radius: 8px;
  padding: 14px; box-shadow: 0 8px 32px rgba(0,0,0,0.75);
  min-width: 230px; z-index: 9500;
}
#cg-annot-picker-title { color: #8090a0; font-size: 11px; margin-bottom: 8px; }
.cg-ap-swatches {
  display: grid; grid-template-columns: repeat(6, 28px); gap: 5px; margin-bottom: 10px;
}
.cg-ap-swatch {
  width: 28px; height: 20px; border-radius: 4px; cursor: pointer;
  border: 2px solid transparent; box-sizing: border-box;
}
.cg-ap-swatch.selected { border-color: #fff; box-shadow: 0 0 0 1px #fff; }
#cg-annot-picker-lbl {
  width: 100%; box-sizing: border-box;
  background: #23272e; border: 1px solid #3d4451; color: #d0d8e0;
  border-radius: 4px; padding: 5px 8px; font-size: 12px; outline: none;
  margin-bottom: 10px; display: block;
}
#cg-annot-picker-lbl:focus { border-color: #4ec980; }
.cg-ap-btns { display: flex; gap: 8px; justify-content: flex-end; }
.cg-ap-btn {
  padding: 4px 14px; font-size: 12px; border-radius: 4px; cursor: pointer;
  border: 1px solid; font-weight: 600;
}
.cg-ap-btn-ok { background: #0e2a18; color: #4ec980; border-color: #4ec980; }
.cg-ap-btn-ok:hover { background: #183820; }
.cg-ap-btn-cancel { background: #1a1d23; color: #8090a0; border-color: #3d4451; }
.cg-ap-btn-cancel:hover { background: #23272e; }
/* Pinned notes */
.cg-pin {
  position: absolute; min-width: 120px; min-height: 58px;
  background: rgba(247,215,116,0.92); border: 1px solid rgba(180,150,50,0.5);
  border-radius: 5px; box-shadow: 2px 3px 10px rgba(0,0,0,0.5);
  z-index: 5; overflow: hidden; font-family: inherit;
  pointer-events: auto;  /* override pointer-events:none on fn-mode overlay layer */
}
.cg-pin-header {
  background: rgba(200,170,60,0.7); padding: 3px 6px; cursor: move;
  display: flex; justify-content: space-between; align-items: center;
  font-size: 12px; line-height: 1.4; user-select: none;
}
.cg-pin-close {
  background: none; border: none; cursor: pointer;
  font-size: 14px; color: rgba(60,40,0,0.7); line-height: 1; padding: 0 2px;
}
.cg-pin-close:hover { color: #c00; }
.cg-pin-body {
  padding: 5px 7px; min-height: 36px; outline: none;
  color: #3a2c00; font-size: 12px; word-break: break-word; white-space: pre-wrap;
}
.cg-pin-resize {
  position: absolute; bottom: 0; right: 0; width: 12px; height: 12px;
  cursor: se-resize; background: rgba(180,150,50,0.4); border-radius: 0 0 4px 0;
}
/* Pin context menu */
#cg-pin-ctx-menu {
  display: none; position: fixed; z-index: 9000;
  background: #1a1d23; border: 1px solid #2d3139; border-radius: 6px;
  box-shadow: 0 6px 20px rgba(0,0,0,0.7); min-width: 140px;
}
/* Dead vars report modal */
#cg-dead-modal {
  display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.74);
  z-index: 3000; align-items: center; justify-content: center;
}
#cg-dead-modal.open { display: flex; }
#cg-dead-modal-card {
  background: #23272e; border: 1px solid #3d4451; border-radius: 10px;
  width: 680px; max-width: 95vw; max-height: 85vh; overflow-y: auto;
  box-shadow: 0 20px 60px rgba(0,0,0,0.75); position: relative;
  display: flex; flex-direction: column;
}
#cg-dead-modal-header {
  padding: 14px 46px 12px 18px; border-bottom: 1px solid #2d3139;
  font-size: 14px; font-weight: 700; color: #e74c3c;
  background: #1a1010; border-radius: 10px 10px 0 0; flex-shrink: 0;
}
#cg-dead-modal-close {
  position: absolute; top: 12px; right: 14px; background: none; border: none;
  color: #8090a0; font-size: 22px; cursor: pointer;
}
#cg-dead-modal-close:hover { color: #e0e0e0; }
#cg-dead-modal-body { padding: 10px 0; overflow-y: auto; }
.cg-dead-row {
  display: grid;
  grid-template-columns: 1fr 1fr 80px 90px 1fr;
  gap: 0 8px; padding: 6px 14px; font-size: 11px; border-bottom: 1px solid #1e2129;
  align-items: center;
}
.cg-dead-row:hover { background: #1e2530; }
.cg-dead-hdr {
  font-size: 10px; font-weight: 700; color: #5a6a7a; text-transform: uppercase;
  padding: 5px 14px 4px; border-bottom: 2px solid #2d3139; letter-spacing: 0.5px;
  display: grid; grid-template-columns: 1fr 1fr 80px 90px 1fr; gap: 0 8px;
}
.cg-dead-name { color: #e0e0e0; font-family: monospace; font-weight: 600; }
.cg-dead-fn   { color: #9ab0c0; }
.cg-dead-line { color: #6a7a8a; text-align: right; }
.cg-dead-type { color: #c586c0; }
.cg-dead-why  { color: #e74c3c; font-size: 10px; }

/* ── Main search dropdown (function / script modes) ── */
#cg-search-wrap { position: relative; }
#cg-search-dropdown {
  position: fixed; background: #23272e; border: 1px solid #3d4451;
  border-radius: 4px; max-height: 220px; overflow-y: auto;
  z-index: 600; display: none; box-shadow: 0 6px 20px rgba(0,0,0,0.65);
}
.cg-sd-item {
  padding: 7px 10px; cursor: pointer; display: flex; flex-direction: column;
  gap: 2px; border-bottom: 1px solid #2d3139;
}
.cg-sd-item:last-child { border-bottom: none; }
.cg-sd-item:hover { background: #1e3050; }
.cg-sd-name { font-family: monospace; font-size: 12px; color: #e0e0e0; }
.cg-sd-mark { color: #569cd6; }
.cg-sd-meta { font-size: 10px; color: #6a7a8a; }

/* ── Theme toggle button ── */
#cg-theme-btn {
  background: none; border: none; cursor: pointer;
  padding: 2px 5px; font-size: 15px; line-height: 1;
  color: #a8c8f0; border-radius: 4px; flex-shrink: 0; opacity: 0.82;
  transition: opacity 0.12s, background 0.12s;
}
#cg-theme-btn:hover { opacity: 1; background: rgba(255,255,255,0.13); }

/* ── Light theme overrides (body[data-theme="light"]) ── */
body[data-theme="light"],
body[data-theme="light"] #mynetwork { background: #dde3ea !important; color: #1a2535; }
body[data-theme="light"] #cg-sidebar { background: #f4f7fa; border-right-color: #c8d4de; }
body[data-theme="light"] #cg-header { background: linear-gradient(135deg,#1d4ed8 0%,#1e3a8a 100%); border-bottom-color: #1e4dc8; }
body[data-theme="light"] #cg-header .sub { color: #b0c8e8; }
body[data-theme="light"] .cg-section { border-bottom-color: #c8d4de; }
body[data-theme="light"] .cg-section > label { color: #4a6070; }
body[data-theme="light"] .cg-hint { color: #7a8898; }
body[data-theme="light"] .cg-input { background: #fff; border-color: #9eb4c8; color: #1a2535; }
body[data-theme="light"] .cg-select { background: #fff; border-color: #9eb4c8; color: #1a2535; }
body[data-theme="light"] .cg-btn { background: #edf2f7; border-color: #9eb4c8; color: #2c4455; }
body[data-theme="light"] .cg-btn:hover { background: #dde6f0; color: #1a2535; }
body[data-theme="light"] .cg-btn.active { background: #dbeafe; border-color: #3b82f6; color: #1d4ed8; }
body[data-theme="light"] .cg-btn.flash { background: #dcfce7; border-color: #22c55e; color: #15803d; }
body[data-theme="light"] .cg-row2 { color: #4a6070; }
body[data-theme="light"] .cg-stat-box { background: #edf2f7; border-color: #c8d4de; }
body[data-theme="light"] .cg-stat-box .lbl { color: #4a6070; }
body[data-theme="light"] .cg-err-panel { background: #fff5f5; border-color: #fecaca; }
body[data-theme="light"] .cg-err-item { color: #b91c1c; border-bottom-color: #fecaca; }
/* Hover popup / edge popup */
body[data-theme="light"] #cg-hover-popup { background: #fff; border-color: #9eb4c8; box-shadow: 0 8px 28px rgba(0,0,0,0.18); color: #1a2535; }
body[data-theme="light"] #cg-edge-popup { background: #fff; box-shadow: 0 10px 30px rgba(0,0,0,0.2); color: #1a2535; }
body[data-theme="light"] .cg-edge-close { color: #7a8898; }
body[data-theme="light"] .cg-edge-close:hover { color: #1a2535; }
body[data-theme="light"] .cg-edge-label { color: #7a8898; }
body[data-theme="light"] .cg-edge-value { color: #1a2535; }
body[data-theme="light"] .cg-edge-args { background: #edf2f7; border-color: #c8d4de; }
body[data-theme="light"] .cg-edge-arg { color: #1a2535; }
body[data-theme="light"] .cg-edge-empty { color: #9eb4c8; }
/* Hover popup internals */
body[data-theme="light"] .hp-name { color: #1d4ed8; }
body[data-theme="light"] .hp-doc { color: #4a6070; }
body[data-theme="light"] .hp-lbl { color: #7a8898; }
body[data-theme="light"] .hp-val { color: #1a2535; }
body[data-theme="light"] .hp-divider { border-top-color: #c8d4de; }
body[data-theme="light"] .hp-section-hdr { color: #7a8898; }
body[data-theme="light"] .hp-cat { color: #9eb4c8; }
body[data-theme="light"] .hp-lang-python { background:#dbeafe; color:#1d4ed8; }
body[data-theme="light"] .hp-lang-c { background:#fff7ed; color:#c2410c; }
body[data-theme="light"] .hp-lang-cpp { background:#f0fdf4; color:#15803d; }
body[data-theme="light"] .hp-lang-matlab { background:#faf5ff; color:#7e22ce; }
body[data-theme="light"] .hp-lang-ext { background:#f1f5f9; color:#64748b; }
body[data-theme="light"] .hp-ftype { background:#f1f5f9; color:#64748b; }
/* Detail panel */
body[data-theme="light"] #cg-detail { background: #f4f7fa; border-left-color: #c8d4de; color: #1a2535; }
body[data-theme="light"] #cg-detail-close { color: #7a8898; }
body[data-theme="light"] #cg-detail-close:hover { color: #1a2535; }
body[data-theme="light"] #cg-detail-title { color: #1d4ed8; }
body[data-theme="light"] .cg-ds h3 { color: #7a8898; }
body[data-theme="light"] .cg-ds p { color: #1a2535; }
body[data-theme="light"] .cg-ci { border-bottom-color: #c8d4de; color: #2c4455; }
body[data-theme="light"] .cg-ci:hover { color: #1d4ed8; }
/* Double-click modal */
body[data-theme="light"] #cg-modal { background: rgba(0,0,0,0.42); }
body[data-theme="light"] #cg-modal-card { background: #fff; border-color: #9eb4c8; box-shadow: 0 20px 60px rgba(0,0,0,0.25); color: #1a2535; }
body[data-theme="light"] #cg-modal-close { color: #7a8898; }
body[data-theme="light"] #cg-modal-close:hover { color: #1a2535; }
body[data-theme="light"] .cg-modal-title { color: #1d4ed8; }
body[data-theme="light"] .cg-modal-qname { color: #7a8898; }
body[data-theme="light"] .cg-modal-section { border-top-color: #c8d4de; }
body[data-theme="light"] .cg-modal-section h3 { color: #7a8898; }
body[data-theme="light"] .cg-modal-lbl { color: #7a8898; }
body[data-theme="light"] .cg-modal-val { color: #1a2535; }
body[data-theme="light"] .cg-modal-param { background: #edf2f7; color: #1a2535; }
body[data-theme="light"] .cg-modal-param-num { color: #7a8898; }
body[data-theme="light"] .cg-modal-param-name { color: #1a2535; }
body[data-theme="light"] .cg-modal-param-type { color: #1d4ed8; }
body[data-theme="light"] .cg-modal-ci { background: #edf2f7; border-color: #c8d4de; color: #2c4455; }
body[data-theme="light"] .cg-modal-ci:hover { color: #1d4ed8; border-color: #3b82f6; background: #dbeafe; }
body[data-theme="light"] .cg-modal-ci-nocursor:hover { color: #2c4455; border-color: #c8d4de; background: #edf2f7; }
body[data-theme="light"] .cg-modal-note { color: #9eb4c8; }
body[data-theme="light"] .cg-modal-doc { background: #edf2f7; border-left-color: #3b82f6; color: #4a6070; }
body[data-theme="light"] .cg-modal-var-empty { background: #edf2f7; color: #7a8898; }
body[data-theme="light"] .cg-modal-var-head { color: #7a8898; }
body[data-theme="light"] .cg-modal-var-scope { color: #7a8898; }
body[data-theme="light"] .cg-modal-var-line { color: #9eb4c8; }
body[data-theme="light"] .cg-modal-var-source { color: #4a6070; }
/* Script / function view */
body[data-theme="light"] #cg-script-view { background: #dde3ea; }
body[data-theme="light"] .cg-marquee-box { border-color: #3b82f6; background: rgba(59,130,246,0.1); }
body[data-theme="light"] .cg-file-card { background: #fff; border-color: #c8d4de; box-shadow: 0 4px 14px rgba(0,0,0,0.08); }
body[data-theme="light"] .cg-file-card.sv-selected { border-color: #3b82f6; }
body[data-theme="light"] .cg-file-card.sv-multi-selected { border-color: #f59e0b; box-shadow: 0 0 0 2px rgba(245,158,11,0.3); }
body[data-theme="light"] .cg-fc-header { background: #edf2f7; border-bottom-color: #c8d4de; }
body[data-theme="light"] .cg-fc-header:hover { background: #e2eaf2; }
body[data-theme="light"] .cg-fc-collapse-btn { color: #8090a0; }
body[data-theme="light"] .cg-fc-collapse-btn:hover { color: #2d3a4a; background: #d6e4f0; }
body[data-theme="light"] .cg-fc-fname { color: #1a2535; }
body[data-theme="light"] .cg-fc-dir { color: #7a8898; }
body[data-theme="light"] .cg-fc-count { background: #dde6f0; color: #4a6070; }
body[data-theme="light"] .cg-fn-row { border-bottom-color: #e5edf4; }
body[data-theme="light"] .cg-fn-row:hover { background: #f0f5fb; }
body[data-theme="light"] .cg-fn-row.sv-selected { background: #dbeafe !important; border-left-color: #3b82f6; }
body[data-theme="light"] .cg-fn-row.sv-match { background: #dcfce7 !important; border-left-color: #22c55e; }
body[data-theme="light"] .cg-fn-row.sv-edge-endpoint { background: #fef9c3 !important; border-left-color: #eab308; }
body[data-theme="light"] .cg-fn-nm { color: #1a2535; }
body[data-theme="light"] .cg-fn-nm-main { color: #1d4ed8; }
body[data-theme="light"] .cg-fn-sig { color: #7a8898; }
body[data-theme="light"] .cg-fn-meta { color: #9eb4c8; }
body[data-theme="light"] .cg-cb-ext { background: #edf2f7; color: #7a8898; border-color: #c8d4de; }
body[data-theme="light"] .cg-cb-more { background: #edf2f7; color: #9eb4c8; border-color: #c8d4de; }
/* Variable Flow */
body[data-theme="light"] #cg-varflow-view { background: #dde3ea; }
body[data-theme="light"] #cg-vf-topbar { background: #f4f7fa; border-bottom-color: #c8d4de; }
body[data-theme="light"] #cg-vf-topbar h2 { color: #7a8898; }
body[data-theme="light"] #cg-vf-search-input { background: #fff; border-color: #9eb4c8; color: #1a2535; }
body[data-theme="light"] #cg-vf-search-clear { background: #fff; border-color: #9eb4c8; color: #7a8898; }
body[data-theme="light"] #cg-vf-search-clear:hover { background: #edf2f7; color: #1a2535; }
body[data-theme="light"] #cg-vf-dropdown { background: #fff; border-color: #9eb4c8; box-shadow: 0 8px 24px rgba(0,0,0,0.15); }
body[data-theme="light"] .cg-vf-dd-item { color: #1a2535; border-bottom-color: #e5edf4; }
body[data-theme="light"] .cg-vf-dd-item:hover { background: #dbeafe; }
body[data-theme="light"] .cg-vf-dd-count { background: #edf2f7; color: #7a8898; }
body[data-theme="light"] #cg-vf-placeholder { color: #9eb4c8; }
body[data-theme="light"] .cg-vf-node { background: #fff !important; border-color: #c8d4de; }
body[data-theme="light"] .cg-vf-node:hover { border-color: #3b82f6 !important; box-shadow: 0 0 12px rgba(59,130,246,0.2); }
body[data-theme="light"] .cg-vf-node-header { background: #f4f7fa; border-bottom-color: #e5edf4; }
body[data-theme="light"] .cg-vf-var-label,
body[data-theme="light"] .cg-vf-info-label { color: #7a8898; }
body[data-theme="light"] .cg-vf-var-name { color: #1a2535; }
body[data-theme="light"] .cg-vf-fn-val { color: #7a8898; }
body[data-theme="light"] .cg-vf-file-val { color: #9eb4c8; }
body[data-theme="light"] .cg-vf-type-val { color: #1d4ed8; }
body[data-theme="light"] .cg-vf-snippet { background: #edf2f7; border-top-color: #e5edf4; color: #1e6da0; }
body[data-theme="light"] .cg-vf-node.cg-vf-connect-input { background: #f8f0ff !important; }
body[data-theme="light"] .cg-vf-node.cg-vf-custom-input { background: #f0fdfd !important; }
body[data-theme="light"] #cg-vf-legend { background: rgba(255,255,255,0.96); border-color: #c8d4de; color: #1a2535; box-shadow: 0 4px 18px rgba(0,0,0,0.12); }
body[data-theme="light"] #cg-vf-legend .cg-vf-legend-title { color: #1a2535; }
body[data-theme="light"] #cg-vf-legend .cg-vf-legend-hint { color: #7a8898; }
body[data-theme="light"] #cg-vf-legend .cg-vf-legend-clear { background: #edf2f7; border-color: #c8d4de; color: #2c4455; }
body[data-theme="light"] #cg-vf-legend .cg-vf-legend-clear:hover { background: #dde6f0; }
body[data-theme="light"] #cg-vf-modal { background: rgba(0,0,0,0.42); }
body[data-theme="light"] #cg-vf-modal-card { background: #fff; border-color: #c8d4de; box-shadow: 0 20px 60px rgba(0,0,0,0.2); }
body[data-theme="light"] #cg-vf-modal-close { color: #7a8898; }
body[data-theme="light"] #cg-vf-modal-close:hover { color: #1a2535; }
body[data-theme="light"] #cg-vf-modal-title { background: #f4f7fa; color: #1a2535; border-bottom-color: #c8d4de; }
body[data-theme="light"] .cg-vf-modal-section { border-bottom-color: #e5edf4; }
body[data-theme="light"] .cg-vf-modal-section-title { color: #7a8898; }
body[data-theme="light"] .cg-vf-modal-label { color: #7a8898; }
body[data-theme="light"] .cg-vf-modal-value { color: #1a2535; }
body[data-theme="light"] .cg-vf-modal-value.mono { color: #1e6da0; }
body[data-theme="light"] .cg-vf-modal-action-desc { color: #4a6070; }
body[data-theme="light"] .cg-vf-modal-code { background: #edf2f7; color: #1e6da0; border-color: #c8d4de; }
body[data-theme="light"] .cg-vf-ctx-menu { background: #fff; border-color: #c8d4de; box-shadow: 0 6px 20px rgba(0,0,0,0.15); }
body[data-theme="light"] .cg-vf-ctx-item { color: #1a2535; }
body[data-theme="light"] .cg-vf-ctx-item:hover { background: #dbeafe; color: #1a2535; }
body[data-theme="light"] .cg-vf-ctx-sep { background: #e5edf4; }
body[data-theme="light"] #cg-vf-dead-btn { background: #fff5f5; color: #dc2626; border-color: #fca5a5; }
body[data-theme="light"] #cg-vf-dead-btn:hover { background: #fee2e2; }
body[data-theme="light"] #cg-vf-annot-btn { background: #f0fdf4; color: #15803d; border-color: #86efac; }
body[data-theme="light"] #cg-vf-annot-btn:hover { background: #dcfce7; }
body[data-theme="light"] #cg-annot-picker { background: #fff; border-color: #c8d4de; box-shadow: 0 8px 32px rgba(0,0,0,0.18); }
body[data-theme="light"] #cg-annot-picker-title { color: #7a8898; }
body[data-theme="light"] #cg-annot-picker-lbl { background: #fff; border-color: #9eb4c8; color: #1a2535; }
body[data-theme="light"] .cg-ap-size-btn { border-color: #9eb4c8; color: #4a6070; background: #f4f7fa; }
body[data-theme="light"] .cg-ap-btn-cancel { background: #edf2f7; color: #7a8898; border-color: #c8d4de; }
body[data-theme="light"] .cg-ap-btn-cancel:hover { background: #dde6f0; }
/* VF category / action badges */
body[data-theme="light"] .cg-vfc-local { background:#dbeafe; color:#1d4ed8; border-color:#93c5fd; }
body[data-theme="light"] .cg-vfc-global { background:#fee2e2; color:#b91c1c; border-color:#fca5a5; }
body[data-theme="light"] .cg-vfc-static { background:#fef9c3; color:#854d0e; border-color:#fde047; }
body[data-theme="light"] .cg-vfc-argument { background:#d1fae5; color:#065f46; border-color:#6ee7b7; }
body[data-theme="light"] .cg-vfc-return { background:#dcfce7; color:#14532d; border-color:#86efac; }
body[data-theme="light"] .cg-vfc-member { background:#f3e8ff; color:#6b21a8; border-color:#d8b4fe; }
body[data-theme="light"] .cg-vfc-const { background:#fff7ed; color:#9a3412; border-color:#fdba74; }
body[data-theme="light"] .cg-vfc-env { background:#e0f2fe; color:#075985; border-color:#7dd3fc; }
body[data-theme="light"] .cg-vfc-heap { background:#fef3c7; color:#92400e; border-color:#fcd34d; }
body[data-theme="light"] .cg-vfa-declare { background:#d1fae5; color:#065f46; border-color:#6ee7b7; }
body[data-theme="light"] .cg-vfa-assign { background:#dbeafe; color:#1d4ed8; border-color:#93c5fd; }
body[data-theme="light"] .cg-vfa-argument { background:#d1fae5; color:#065f46; border-color:#6ee7b7; }
body[data-theme="light"] .cg-vfa-field { background:#f3e8ff; color:#6b21a8; border-color:#d8b4fe; }
body[data-theme="light"] .cg-vfa-constant { background:#fff7ed; color:#9a3412; border-color:#fdba74; }
body[data-theme="light"] .cg-vfa-global { background:#fee2e2; color:#b91c1c; border-color:#fca5a5; }
body[data-theme="light"] .cg-vfa-static { background:#fef9c3; color:#854d0e; border-color:#fde047; }
body[data-theme="light"] .cg-vfa-env { background:#e0f2fe; color:#075985; border-color:#7dd3fc; }
body[data-theme="light"] .cg-vfa-heap { background:#fef3c7; color:#92400e; border-color:#fcd34d; }
body[data-theme="light"] .cg-vf-connect-badge { background:#f3e8ff; color:#6b21a8; border-color:#d8b4fe; }
body[data-theme="light"] .cg-vf-dead-badge { background:#fee2e2; color:#b91c1c; border-color:#fca5a5; }
body[data-theme="light"] .cg-vf-memop-badge { background:#f3e8ff; color:#6b21a8; border-color:#d8b4fe; }
body[data-theme="light"] .cg-vf-classifier-badge { background:#ecfdf5; color:#065f46; border-color:#6ee7b7; }
/* Dead vars modal */
body[data-theme="light"] #cg-dead-modal { background: rgba(0,0,0,0.42); }
body[data-theme="light"] #cg-dead-modal-card { background: #fff; border-color: #c8d4de; box-shadow: 0 20px 60px rgba(0,0,0,0.2); }
body[data-theme="light"] #cg-dead-modal-header { background: #fff5f5; color: #dc2626; border-bottom-color: #fecaca; border-radius: 10px 10px 0 0; }
body[data-theme="light"] #cg-dead-modal-close { color: #7a8898; }
body[data-theme="light"] #cg-dead-modal-close:hover { color: #1a2535; }
body[data-theme="light"] .cg-dead-row { border-bottom-color: #e5edf4; }
body[data-theme="light"] .cg-dead-row:hover { background: #f4f7fa; }
body[data-theme="light"] .cg-dead-hdr { color: #7a8898; border-bottom-color: #c8d4de; }
body[data-theme="light"] .cg-dead-name { color: #1a2535; }
body[data-theme="light"] .cg-dead-fn { color: #4a6070; }
body[data-theme="light"] .cg-dead-line { color: #9eb4c8; }
/* Main search dropdown */
body[data-theme="light"] #cg-search-dropdown { background: #fff; border-color: #9eb4c8; box-shadow: 0 6px 20px rgba(0,0,0,0.15); }
body[data-theme="light"] .cg-sd-item { border-bottom-color: #e5edf4; }
body[data-theme="light"] .cg-sd-item:hover { background: #dbeafe; }
body[data-theme="light"] .cg-sd-name { color: #1a2535; }
body[data-theme="light"] .cg-sd-meta { color: #7a8898; }
/* Pin ctx menu */
body[data-theme="light"] #cg-pin-ctx-menu { background: #fff; border-color: #c8d4de; box-shadow: 0 6px 20px rgba(0,0,0,0.15); }
</style>
"""

_SIDEBAR_HTML = """\
<div id="cg-sidebar">
  <div id="cg-header">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:6px;">
      <h1 style="margin:0">CallGraph Analyzer</h1>
      <button id="cg-theme-btn" title="Switch to light mode">&#9728;</button>
    </div>
    <div class="sub">{title}</div>
  </div>

  <!-- View mode -->
  <div class="cg-section">
    <label>View Mode</label>
    <div class="cg-btn-row">
      <button class="cg-btn active" id="cg-btn-mode-fn">Function Nodes</button>
      <button class="cg-btn" id="cg-btn-mode-sv">Script Nodes</button>
      <button class="cg-btn" id="cg-btn-mode-vf">Var Flow</button>
    </div>
  </div>

  <!-- Search & focus -->
  <div class="cg-section">
    <label>Search &amp; Focus</label>
    <div id="cg-search-wrap">
      <input id="cg-search" class="cg-input" type="text" placeholder="Function name..." autocomplete="off"/>
      <div id="cg-search-dropdown"></div>
    </div>
    <div class="cg-hint" id="cg-search-hint">Click to browse &bull; type to filter</div>
    <div class="cg-btn-row">
      <button class="cg-btn" id="cg-btn-highlight" title="Select and highlight matching nodes">Highlight</button>
      <button class="cg-btn" id="cg-btn-center"    title="Center view on first match">Center</button>
    </div>
    <div class="cg-btn-row">
      <button class="cg-btn" id="cg-btn-isolate" title="Show only this function and its call tree">Isolate</button>
      <button class="cg-btn" id="cg-btn-expand"  title="Expand call tree without hiding others">Expand</button>
    </div>
    <div class="cg-row2">
      <span>Depth:</span>
      <select id="cg-focus-depth" class="cg-select">
        <option value="0">0 — selected only</option>
        <option value="1">1 — direct neighbors</option>
        <option value="2">2</option>
        <option value="3" selected>3</option>
        <option value="4">4</option>
        <option value="5">5</option>
        <option value="9999">All</option>
      </select>
    </div>
    <div class="cg-row2">
      <span>Show:</span>
      <select id="cg-focus-dir" class="cg-select">
        <option value="both"    selected>Callers &amp; callees</option>
        <option value="callees">Callees only (downstream)</option>
        <option value="callers">Callers only (upstream)</option>
      </select>
    </div>
    <div class="cg-btn-row" style="margin-top:6px">
      <button class="cg-btn" id="cg-btn-clearfocus" title="Show all nodes again">Clear isolation</button>
    </div>
  </div>

  <!-- View controls -->
  <div class="cg-section">
    <label>View controls</label>
    <div class="cg-btn-row">
      <button class="cg-btn" id="cg-btn-fit"         title="Zoom and pan to fit all nodes">Fit view</button>
      <button class="cg-btn" id="cg-btn-showall"     title="Show all hidden nodes">Show all</button>
    </div>
    <div class="cg-btn-row">
      <button class="cg-btn" id="cg-btn-savelayout"  title="Save current node positions to browser storage">&#128190; Save Layout</button>
    </div>
    <div class="cg-btn-row">
      <button class="cg-btn" id="cg-btn-resetlayout" title="Restore original computed layout">Reset layout</button>
      <button class="cg-btn" id="cg-btn-clearsaved"  title="Remove saved positions from browser storage">Clear saved</button>
    </div>
    <div class="cg-btn-row" id="cg-fn-layout-row">
      <button class="cg-btn" id="cg-btn-straight" title="Toggle straight vs curved edges">&#10231; Straight</button>
      <button class="cg-btn" id="cg-btn-hierarchical" title="Toggle layered top-down layout">&#8862; Layered</button>
    </div>
    <div class="cg-btn-row" id="cg-sv-layout-row" style="display:none">
      <button class="cg-btn" id="cg-btn-sv-straight" title="Toggle straight vs curved edges">&#10231; Straight</button>
      <button class="cg-btn" id="cg-btn-sv-spread" title="Toggle wider card spacing">&#8596; Spread</button>
    </div>
    <div class="cg-btn-row" id="cg-vf-layout-row" style="display:none">
      <button class="cg-btn" id="cg-btn-vf-straight" title="Toggle straight vs curved edges">&#10231; Straight</button>
    </div>
    <div class="cg-hint" style="margin-top:4px">Double-click any node for full details</div>
  </div>

  <!-- Large-graph performance warning (injected by JS when needed) -->
  <div id="cg-large-graph-warn" style="display:none;margin:6px 0 0;padding:6px 8px;background:#2a1a00;border:1px solid #7a4400;border-radius:5px;font-size:11px;color:#f0a040;line-height:1.4"></div>

  <!-- Statistics -->
  <div class="cg-section">
    <label>Statistics</label>
    <div class="cg-stat-grid">
      <div class="cg-stat-box"><div class="val">{stat_fn}</div><div class="lbl">Functions</div></div>
      <div class="cg-stat-box"><div class="val">{stat_calls}</div><div class="lbl">Calls</div></div>
      <div class="cg-stat-box"><div class="val">{stat_files}</div><div class="lbl">Files</div></div>
      <div class="cg-stat-box {err_class}"><div class="val">{stat_errors}</div><div class="lbl">Errors</div></div>
    </div>
    {errors_toggle}
    <div class="cg-err-panel" id="cg-err-panel">{errors_html}</div>
  </div>

  <!-- Legend: nodes -->
  <div class="cg-section">
    <label>Node colors</label>
    <div class="cg-legend-item"><div class="cg-dot" style="background:#4A90D9"></div>Python function</div>
    <div class="cg-legend-item"><div class="cg-dot" style="background:#E8832A"></div>C function</div>
    <div class="cg-legend-item"><div class="cg-dot" style="background:#27AE60"></div>C++ function</div>
    <div class="cg-legend-item"><div class="cg-dot" style="background:#8E44AD"></div>MATLAB function</div>
    <div class="cg-legend-item"><div class="cg-dot" style="background:#95A5A6"></div>External / library call</div>
    <div class="cg-legend-item"><div class="cg-dot" style="background:#F0E68C;border:1px solid #C8B400;transform:rotate(45deg)"></div>Tracked variable</div>
    <div class="cg-legend-item"><div class="cg-dot-entry"></div>Entry point (red border)</div>
  </div>

  <!-- Legend: edges (now interactive — checkboxes hide/show arrows by type) -->
  <div class="cg-section" id="cg-edge-types-section">
    <label>Edge types</label>
    <div class="cg-legend-item">
      <input type="checkbox" id="cg-et-cb-exact" class="cg-et-cb" checked title="Show / hide confirmed-call arrows"/>
      {arrow_solid}
      <span><b>Confirmed call</b>&nbsp;&mdash; function found exactly in this project</span>
    </div>
    <div class="cg-legend-item">
      <input type="checkbox" id="cg-et-cb-probable" class="cg-et-cb" checked title="Show / hide probable / heuristic / external arrows"/>
      {arrow_dashed}
      <span><b>Probable call</b>&nbsp;&mdash; heuristic, unresolved, or external</span>
    </div>
    <div class="cg-legend-item">
      <input type="checkbox" id="cg-et-cb-var" class="cg-et-cb" checked title="Show / hide variable-annotation arrows"/>
      {arrow_var}
      <span><b>Variable annotation</b>&nbsp;&mdash; tracked variable value</span>
    </div>
    <div id="cg-et-violation-row" class="cg-legend-item" style="display:none">
      <input type="checkbox" id="cg-et-cb-violation" class="cg-et-cb" checked title="Show / hide architecture-violation arrows"/>
      <svg class="cg-edge-line" width="38" height="14" viewBox="0 0 38 14">
        <defs><marker id="arr-v" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 Z" fill="#e23b3b"/></marker></defs>
        <line x1="0" y1="7" x2="32" y2="7" stroke="#e23b3b" stroke-width="2.4" marker-end="url(#arr-v)"/>
      </svg>
      <span><b>Architecture violation</b>&nbsp;&mdash; rule-flagged call</span>
    </div>
    <div class="cg-legend-note">Drag nodes to reposition. Click <b>Save Layout</b> to persist.</div>
  </div>

  <!-- Dead variables analysis -->
  <div class="cg-section" id="cg-dead-section">
    <label>Dead Variable Analysis</label>
    <div class="cg-stat-grid" style="margin-bottom:5px">
      <div class="cg-stat-box cg-stat-err"><div class="val" id="cg-dead-count">—</div><div class="lbl">Dead Vars</div></div>
      <div class="cg-stat-box cg-stat-err"><div class="val" id="cg-dead-param-count">—</div><div class="lbl">Unused Params</div></div>
    </div>
    <div class="cg-btn-row">
      <button class="cg-btn" id="cg-btn-dead-report" onclick="cgOpenDeadReport()" title="Show full dead variable report">Report</button>
    </div>
    <div class="cg-hint">Variables declared but never used in the codebase.</div>
  </div>

  <!-- Legend: Variable Flow badges (shown always for reference) -->
  <div class="cg-section" id="cg-vf-legend">
    <label>Var Flow — action types</label>
    <div class="cg-legend-item"><div class="cg-dot" style="background:#4ec9b0"></div><span class="cg-vfa-declare" style="padding:1px 5px;border-radius:3px;font-size:9px;font-weight:700">Declare</span>&nbsp;Variable declared</div>
    <div class="cg-legend-item"><div class="cg-dot" style="background:#569cd6"></div><span class="cg-vfa-assign" style="padding:1px 5px;border-radius:3px;font-size:9px;font-weight:700">Assign</span>&nbsp;Value assigned</div>
    <div class="cg-legend-item"><div class="cg-dot" style="background:#c586c0"></div><span class="cg-vfa-field" style="padding:1px 5px;border-radius:3px;font-size:9px;font-weight:700">Field</span>&nbsp;Struct / class field</div>
    <div class="cg-legend-item"><div class="cg-dot" style="background:#ce9178"></div><span class="cg-vfa-constant" style="padding:1px 5px;border-radius:3px;font-size:9px;font-weight:700">Const</span>&nbsp;Constant / #define</div>
    <div class="cg-legend-item"><div class="cg-dot" style="background:#f47070"></div><span class="cg-vfa-global" style="padding:1px 5px;border-radius:3px;font-size:9px;font-weight:700">Global</span>&nbsp;Global variable</div>
    <div class="cg-legend-item"><div class="cg-dot" style="background:#dcdcaa"></div><span class="cg-vfa-static" style="padding:1px 5px;border-radius:3px;font-size:9px;font-weight:700">Static</span>&nbsp;Static variable</div>
    <div class="cg-legend-item"><div class="cg-dot" style="background:#9cdcfe"></div><span class="cg-vfa-env" style="padding:1px 5px;border-radius:3px;font-size:9px;font-weight:700">Env Read</span>&nbsp;Environment var</div>
    <div class="cg-legend-item"><div class="cg-dot" style="background:#d7ba7d"></div><span class="cg-vfa-heap" style="padding:1px 5px;border-radius:3px;font-size:9px;font-weight:700">Heap Alloc</span>&nbsp;Dynamic allocation</div>
    <div class="cg-legend-note" style="margin-top:5px">Solid edge = call-flow &bull; Dashed = sequential in file</div>
  </div>
</div>

<div id="cg-hover-popup"></div>
<div id="cg-edge-popup"></div>
"""

# ------------------------------------------------------------------ #
# Sidebar JavaScript                                                  #
# ------------------------------------------------------------------ #

_SIDEBAR_JS = """
<script id="callgraph-sidebar-js">
(function() {
  /* ── Injected data ─────────────────────────────────────────── */
  var NODE_DATA     = CG_NODE_DATA;
  var EDGE_DATA     = CG_EDGE_DATA;
  var INITIAL_POS   = CG_INITIAL_POS;
  var LAYOUT_KEY    = CG_LAYOUT_KEY;
  var ALL_NODE_IDS  = CG_ALL_NODE_IDS;
  var VAR_PARENT    = CG_VAR_PARENT;
  var VAR_FLOW_DATA = CG_VAR_FLOW_DATA;
  var GRAPH_ID      = CG_GRAPH_ID;      /* unique hash per generated graph */
  var LARGE_GRAPH   = CG_LARGE_GRAPH;  /* true when node count >= LARGE threshold (2000) */
  var HUGE_GRAPH    = CG_HUGE_GRAPH;   /* true when node count >= HUGE threshold (default 8000) */
  var HUGE_THRESHOLD = CG_HUGE_THRESHOLD;
  var SV_LAYOUT_KEY = GRAPH_ID + ':cg_sv_layout_v1';
  var VF_LAYOUT_PFX = GRAPH_ID + ':cg_vf_layout_v1::';
  window.CGX_NODE_DATA = NODE_DATA;
  window.CGX_EDGE_DATA = EDGE_DATA;
  window.cgStraightEdges = false;

  /* ── Pre-indexes (computed once, used everywhere) ────────────── */
  /* EDGES_BY_FROM[nid] / EDGES_BY_TO[nid] — O(1) replacement for
     EDGE_DATA.filter(e => e.from===nid). Huge speedup on large graphs
     where every UI handler used to scan the full edge list. */
  var EDGES_BY_FROM = {};
  var EDGES_BY_TO   = {};
  EDGE_DATA.forEach(function(e) {
    (EDGES_BY_FROM[e.from] = EDGES_BY_FROM[e.from] || []).push(e);
    (EDGES_BY_TO[e.to]     = EDGES_BY_TO[e.to]     || []).push(e);
  });
  /* _VF_KEYS — cached Object.keys(VAR_FLOW_DATA); avoids recomputing per keystroke
     in the Variable Flow search dropdown. */
  var _VF_KEYS = Object.keys(VAR_FLOW_DATA);

  /* CONNECT_INDEX[lowercased connect_input_name] = [.Connect receiver occurrences]
     Used by _vfBuildFlowChain to link a LUGASI block to the .Connect block that
     consumes that input, even when they live in different functions and have
     different variable names. Match key is the source/input STRING, not the
     local variable name (LUGASI dest and .Connect receiver are usually different
     identifiers). */
  var CONNECT_INDEX = {};
  _VF_KEYS.forEach(function(k){
    VAR_FLOW_DATA[k].forEach(function(o){
      if (o.source_kind === 'input_file_connect' && o.connect_input_name) {
        var key = String(o.connect_input_name).toLowerCase().trim();
        if (key) (CONNECT_INDEX[key] = CONNECT_INDEX[key] || []).push(o);
      }
    });
  });

  /* ── State ─────────────────────────────────────────────────── */
  var lastMX = 0, lastMY = 0;
  var hoverTimer = null;
  var selectedNode = null;
  var wireAttempts = 0;
  var fittedOnce = false;
  var currentMode = 'fn';
  var _svBuilt = false;
  var _svSelectedNid = null;
  var _svPanX = 0, _svPanY = 0, _svZoom = 1.0;
  var _svViewDrag = null, _svCardDrag = null;
  var _svMarquee = null, _svMultiSel = {};
  /* Layout toggle state */
  var _fnStraightLines = false;
  var _fnHierarchical  = true;   /* comfortable default: layered function view */
  var _fnMarquee = null;
  var _fnMidPan  = null;   /* middle-mouse pan state for Function Nodes (vis.js) */
  var _fnGroupDrag = null;
  var _vfStraightLines = false;
  var _svStraightLines = false;
  var _svWideSpread    = true;   /* comfortable default: more readable script spacing */
  var _svCardGapX = 780, _svCardGapY = 360, _svCompGapX = 960, _svCompGapY = 560;
  var _edgeHighlightedEdgeId = null;
  var _edgeHighlightedNodes = [];
  var _vfNodeDrag = null;
  var _vfMarquee = null, _vfMultiSel = {};
  var _vfNodeOverrides = {};
  var _vfCurrentNodes = [];
  var _vfCurrentEdges = [];
  var _vfCurrentChainEdges = [];
  var _vfSelectedEdgeIdx = null;  /* index into _vfEdgeMeta — which edge is highlighted */
  var _vfEdgeMeta = [];           /* [{fromId, toId, type}] — indexed by hit-path data-eidx */
  /* ── Branch highlight (VF-2): click a node to colour each downstream path ──
   * Each immediate outgoing branch from the clicked node gets a distinct hue.
   * When a branch splits again further downstream, child hues are DERIVED from
   * the parent hue (rotated per sibling, shaded by depth) so lineage stays
   * readable instead of blending into mud. Nodes reached by >1 branch are
   * flagged as "merge" points. */
  var _vfBranchActive   = false;  /* is a branch highlight currently shown? */
  var _vfBranchOriginId = null;   /* node id the coloured branches emanate from */
  var _vfBranchNodeColor = {};    /* nodeId -> hsl() colour of its branch */
  var _vfBranchMerge     = {};    /* nodeId -> true when reached by >1 branch */

  /* ── Network accessor ─────────────────────────────────────── */
  function getNet() {
    try {
      var n = window.network;
      return (n && n.body) ? n : null;
    } catch(e) { return null; }
  }

  document.addEventListener('mousemove', function(e) { lastMX = e.clientX; lastMY = e.clientY; });

  /* ── DOM refs ─────────────────────────────────────────────── */
  var searchInput    = document.getElementById('cg-search');
  var searchHint     = document.getElementById('cg-search-hint');
  var focusDepth     = document.getElementById('cg-focus-depth');
  var focusDir       = document.getElementById('cg-focus-dir');
  var btnHighlight   = document.getElementById('cg-btn-highlight');
  var btnCenter      = document.getElementById('cg-btn-center');
  var btnIsolate     = document.getElementById('cg-btn-isolate');
  var btnExpand      = document.getElementById('cg-btn-expand');
  var btnClearFocus  = document.getElementById('cg-btn-clearfocus');
  var btnFit         = document.getElementById('cg-btn-fit');
  var btnShowAll     = document.getElementById('cg-btn-showall');
  var btnSaveLayout  = document.getElementById('cg-btn-savelayout');
  var btnResetLayout = document.getElementById('cg-btn-resetlayout');
  var btnClearSaved  = document.getElementById('cg-btn-clearsaved');
  var btnModeFn      = document.getElementById('cg-btn-mode-fn');
  var btnModeSv      = document.getElementById('cg-btn-mode-sv');
  var btnModeVf      = document.getElementById('cg-btn-mode-vf');
  var edgePopup      = document.getElementById('cg-edge-popup');

  /* ── Theme toggle (UX-1) ─────────────────────────────────── */
  var _CG_THEME_KEY = 'cg_theme_v1';
  function _applyTheme(t) {
    document.body.setAttribute('data-theme', t);
    var btn = document.getElementById('cg-theme-btn');
    if (btn) {
      btn.textContent = (t === 'light') ? '\u263D' : '\u2600';
      btn.title = (t === 'light') ? 'Switch to dark mode' : 'Switch to light mode';
    }
  }
  (function(){
    var saved = '';
    try { saved = localStorage.getItem(_CG_THEME_KEY) || ''; } catch(e) {}
    _applyTheme(saved === 'light' ? 'light' : 'dark');
  })();
  var _themeBtn = document.getElementById('cg-theme-btn');
  if (_themeBtn) {
    _themeBtn.addEventListener('click', function() {
      var cur = document.body.getAttribute('data-theme');
      var next = (cur === 'light') ? 'dark' : 'light';
      _applyTheme(next);
      try { localStorage.setItem(_CG_THEME_KEY, next); } catch(e) {}
    });
  }

  /* ── Utility ──────────────────────────────────────────────── */
  function esc(s) {
    return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
  function _hpRow(label, val) {
    return '<div class="hp-row"><span class="hp-lbl">' + label + '</span><span class="hp-val">' + val + '</span></div>';
  }
  function _flashBtn(btn, msg, ms) {
    if (!btn) return;
    var orig = btn.textContent;
    btn.textContent = msg; btn.classList.add('flash');
    setTimeout(function(){ btn.textContent = orig; btn.classList.remove('flash'); }, ms || 1000);
  }
  function _mkRect(a, b) {
    return {
      left: Math.min(a.x, b.x),
      top: Math.min(a.y, b.y),
      right: Math.max(a.x, b.x),
      bottom: Math.max(a.y, b.y),
    };
  }
  function _rectIntersects(r1, r2) {
    return !(r1.right < r2.left || r1.left > r2.right || r1.bottom < r2.top || r1.top > r2.bottom);
  }
  function _setBoxRect(box, r) {
    if (!box) return;
    box.style.left = r.left + 'px';
    box.style.top = r.top + 'px';
    box.style.width = Math.max(0, r.right - r.left) + 'px';
    box.style.height = Math.max(0, r.bottom - r.top) + 'px';
  }
  /* Expose rect helpers globally so _CGX_EXTRAS_JS can use them */
  window._mkRect = _mkRect;
  window._rectIntersects = _rectIntersects;
  window._setBoxRect = _setBoxRect;

  function _nodeMeta(nid) {
    var nd = NODE_DATA.find(function(n){ return n.id === nid; });
    return nd && nd.meta ? nd.meta : null;
  }

  function _nodeDisplayName(nid) {
    var m = _nodeMeta(nid);
    if (m) return m.qualified_name || m.name || nid;
    var map = window.CG_EDGE_NODE_NAMES;
    return (map && map[nid]) ? map[nid] : nid;
  }

  function _nodeShortName(nid) {
    var m = _nodeMeta(nid);
    if (m) return m.name || m.qualified_name || nid;
    var map = window.CG_EDGE_NODE_NAMES;
    return (map && map[nid]) ? map[nid] : nid;
  }

  function _edgeById(edgeId) {
    for (var i = 0; i < EDGE_DATA.length; i++) {
      if (String(EDGE_DATA[i].id) === String(edgeId)) return EDGE_DATA[i];
    }
    return null;
  }

  function _edgeBaseColor(edge) {
    return edge && edge.confidence === 'HEURISTIC' ? '#888888' : '#6c8ebf';
  }

  function _edgeBaseUpdate(edge) {
    return { id: edge.id, width: 1.5, color: { color: _edgeBaseColor(edge), opacity: 0.85 } };
  }

  function _clearScriptEdgeHighlight() {
    var svEl = document.getElementById('cg-script-view');
    if (!svEl) return;
    svEl.querySelectorAll('.cg-fn-row.sv-edge-endpoint, .cg-fn-row.sv-edge-muted').forEach(function(row) {
      row.classList.remove('sv-edge-endpoint', 'sv-edge-muted');
    });
    svEl.querySelectorAll('.cg-sv-edge.sv-edge-active').forEach(function(path) {
      path.classList.remove('sv-edge-active');
      path.setAttribute('marker-end', 'url(#sv-arr)');
    });
  }

  function _applyScriptEdgeHighlight(edge) {
    var svEl = document.getElementById('cg-script-view');
    if (!svEl || !edge) return;
    _clearScriptEdgeHighlight();
    svEl.querySelectorAll('.cg-fn-row').forEach(function(row) {
      if (row.dataset.nid === edge.from || row.dataset.nid === edge.to) {
        row.classList.remove('sv-edge-muted');
        row.classList.add('sv-edge-endpoint');
      } else {
        row.classList.add('sv-edge-muted');
        row.classList.remove('sv-edge-endpoint');
      }
    });
    var path = svEl.querySelector('.cg-sv-edge[data-eid="' + _cssEscape(edge.id) + '"]');
    if (path) {
      path.classList.add('sv-edge-active');
      path.setAttribute('marker-end', 'url(#sv-arr-active)');
    }
  }

  function _clearEdgeHighlight(keepPopup) {
    var hadEdgeHighlight = !!_edgeHighlightedEdgeId;
    var net = getNet();
    if (net) {
      var nodesDs = window.nodes || net.body.data.nodes;
      if (nodesDs && _edgeHighlightedNodes.length) {
        nodesDs.update(_edgeHighlightedNodes.map(function(id){ return { id: id, shadow: false }; }));
      }
      var prev = _edgeById(_edgeHighlightedEdgeId);
      var edgesDs = window.edges || net.body.data.edges;
      if (prev && edgesDs) edgesDs.update([_edgeBaseUpdate(prev)]);
      if (hadEdgeHighlight) {
        try {
          var selectedNodes = net.getSelectedNodes ? net.getSelectedNodes() : [];
          if (net.setSelection) net.setSelection({ nodes: selectedNodes, edges: [] });
          else if (!selectedNodes.length) net.unselectAll();
        } catch(e) {}
      }
      try { net.redraw(); } catch(e) {}
    }
    _clearScriptEdgeHighlight();
    _edgeHighlightedEdgeId = null;
    _edgeHighlightedNodes = [];
    if (!keepPopup && edgePopup) edgePopup.style.display = 'none';
  }

  function _applyEdgeHighlight(edge) {
    if (!edge) return;
    _clearEdgeHighlight(true);
    _edgeHighlightedEdgeId = edge.id;
    _edgeHighlightedNodes = edge.from === edge.to ? [edge.from] : [edge.from, edge.to];

    var net = getNet();
    if (net) {
      var nodesDs = window.nodes || net.body.data.nodes;
      var edgesDs = window.edges || net.body.data.edges;
      if (nodesDs) {
        nodesDs.update(_edgeHighlightedNodes.map(function(id) {
          return {
            id: id,
            shadow: { enabled: true, color: 'rgba(247,215,116,0.9)', size: 18, x: 0, y: 0 }
          };
        }));
      }
      if (edgesDs) {
        edgesDs.update([{ id: edge.id, width: 4, color: { color: '#F7D774', opacity: 1 } }]);
      }
      try { net.selectEdges([edge.id]); } catch(e) {}
      try { net.redraw(); } catch(e) {}
    }
    _applyScriptEdgeHighlight(edge);
  }

  function _edgeCallText(edge) {
    var args = Array.isArray(edge.args) ? edge.args : [];
    return (edge.callee_name || _nodeShortName(edge.to)) + '(' + args.join(', ') + ')';
  }

  function _literalArgType(expr) {
    var s = String(expr || '').trim();
    if (!s) return '';
    if (/^(['"`]).*\\1$/.test(s)) return 'string';
    if (/^[-+]?\\d+$/.test(s)) return 'integer';
    if (/^[-+]?(?:(?:\\d+\\.\\d*)|(?:\\.\\d+))(?:[eE][-+]?\\d+)?$/.test(s) ||
        /^[-+]?\\d+[eE][-+]?\\d+$/.test(s)) return 'number';
    if (/^(true|false)$/i.test(s)) return 'boolean';
    if (/^(none|null|nullptr|NULL|nan)$/i.test(s)) return 'null';
    if (/^\\[.*\\]$/.test(s)) return 'array/list';
    if (/^\\{.*\\}$/.test(s)) return 'object/map';
    return '';
  }

  function _splitKeywordArg(arg) {
    var s = String(arg || '').trim();
    var m = s.match(/^([A-Za-z_]\\w*)\\s*=\\s*(.+)$/);
    return m ? { name: m[1], value: m[2], raw: s } : { name: '', value: s, raw: s };
  }

  function _cleanArgName(expr) {
    var s = String(expr || '').trim();
    s = s.replace(/^[*&]+/, '').replace(/^\\(([^()]+)\\)$/, '$1').trim();
    if (/^[A-Za-z_]\\w*(?:\\.[A-Za-z_]\\w*|->[A-Za-z_]\\w*)*$/.test(s)) return s;
    return '';
  }

  function _findNamedType(meta, name) {
    if (!meta || !name) return '';
    var simple = name.replace(/->/g, '.').split('.').pop();
    if (meta.parameters) {
      for (var i = 0; i < meta.parameters.length; i++) {
        var p = meta.parameters[i];
        if (p && (p.name === name || p.name === simple) && p.type_hint) return p.type_hint;
      }
    }
    if (meta.variables) {
      for (var j = meta.variables.length - 1; j >= 0; j--) {
        var v = meta.variables[j];
        if (!v || !v.type_hint) continue;
        var vn = String(v.name || '').replace(/->/g, '.');
        if (vn === name.replace(/->/g, '.') || vn.split('.').pop() === simple) return v.type_hint;
      }
    }
    return '';
  }

  function _calleeExpectedArgType(edge, index, keyword) {
    var calleeMeta = _nodeMeta(edge.to);
    if (!calleeMeta || !calleeMeta.parameters) return '';
    if (keyword) {
      for (var i = 0; i < calleeMeta.parameters.length; i++) {
        var kp = calleeMeta.parameters[i];
        if (kp && kp.name === keyword && kp.type_hint) return kp.type_hint;
      }
    }
    var p = calleeMeta.parameters[index];
    return p && p.type_hint ? p.type_hint : '';
  }

  function _edgeArgType(edge, arg, index) {
    var parts = _splitKeywordArg(arg);
    var literalType = _literalArgType(parts.value);
    if (literalType) return literalType;

    var callerMeta = _nodeMeta(edge.from);
    var argName = _cleanArgName(parts.value);
    var callerType = _findNamedType(callerMeta, argName || parts.name);
    if (callerType) return callerType;

    var expectedType = _calleeExpectedArgType(edge, index, parts.name);
    if (expectedType) return 'expected ' + expectedType;

    if (/^[A-Za-z_]\\w*(?:\\.|->)?[A-Za-z_]\\w*\\s*\\(/.test(parts.value)) return 'function result';
    return 'unknown';
  }

  function _edgePopupHtml(edge) {
    var fromMeta = _nodeMeta(edge.from);
    var args = Array.isArray(edge.args) ? edge.args : [];
    var lineText = edge.line || '?';
    if (edge.call_file) {
      lineText = edge.call_file.replace(/.*[\\\\/]/, '') + ':' + lineText;
    } else if (!edge.line && edge.underlying) {
      lineText = 'aggregated (' + edge.underlying + ' underlying call(s))';
    }

    var h = '<button class="cg-edge-close" onclick="cgCloseEdgePopup()" title="Close">&#x2715;</button>';
    h += '<div class="cg-edge-title">' + esc(_nodeShortName(edge.from)) + ' calls ' + esc(_nodeShortName(edge.to)) + '</div>';
    h += '<div class="cg-edge-row"><span class="cg-edge-label">Caller</span><span class="cg-edge-value">' + esc(_nodeDisplayName(edge.from)) + '</span></div>';
    h += '<div class="cg-edge-row"><span class="cg-edge-label">Callee</span><span class="cg-edge-value">' + esc(_nodeDisplayName(edge.to)) + '</span></div>';
    h += '<div class="cg-edge-row"><span class="cg-edge-label">Line</span><span class="cg-edge-value">' + esc(lineText) + '</span></div>';
    h += '<div class="cg-edge-row"><span class="cg-edge-label">Call</span><span class="cg-edge-value">' + esc(_edgeCallText(edge)) + '</span></div>';
    if (edge.category) {
      h += '<div class="cg-edge-row"><span class="cg-edge-label">Category</span><span class="cg-edge-value">' + esc(edge.category) + '</span></div>';
    }
    if (edge.reason) {
      h += '<div class="cg-edge-row"><span class="cg-edge-label">Reason</span><span class="cg-edge-value">' + esc(edge.reason) + '</span></div>';
    }
    if (fromMeta && fromMeta.file_path && fromMeta.file_path !== '<external>') {
      h += '<div class="cg-edge-row"><span class="cg-edge-label">File</span><span class="cg-edge-value">' + esc(fromMeta.file_path) + '</span></div>';
    }
    h += '<div class="cg-edge-args"><div class="cg-edge-label" style="min-width:0;margin-bottom:3px">Arguments</div>';
    if (args.length) {
      args.forEach(function(arg, i) {
        var argType = _edgeArgType(edge, arg, i);
        h += '<div class="cg-edge-arg"><span>' + (i + 1) + '. ' + esc(arg) + '</span>';
        h += '<span class="cg-edge-arg-type">type: ' + esc(argType) + '</span></div>';
      });
    } else {
      h += '<div class="cg-edge-empty">none detected</div>';
    }
    h += '</div>';
    return h;
  }

  function _positionEdgePopup(evt) {
    if (!edgePopup) return;
    var mx = evt && evt.clientX != null ? evt.clientX : lastMX;
    var my = evt && evt.clientY != null ? evt.clientY : lastMY;
    var pw = edgePopup.offsetWidth || 340, ph = edgePopup.offsetHeight || 170;
    var vw = window.innerWidth, vh = window.innerHeight;
    var x = mx + 16, y = my + 12;
    if (x + pw > vw - 12) x = mx - pw - 12;
    if (y + ph > vh - 12) y = vh - ph - 12;
    if (x < 12) x = 12;
    if (y < 12) y = 12;
    edgePopup.style.left = x + 'px';
    edgePopup.style.top  = y + 'px';
  }

  function showEdgeDetails(edgeId, evt) {
    var edge = typeof edgeId === 'object' ? edgeId : _edgeById(edgeId);
    if (!edge || !edgePopup) return;
    hideHoverPopup();
    _applyEdgeHighlight(edge);
    edgePopup.innerHTML = _edgePopupHtml(edge);
    edgePopup.style.display = 'block';
    _positionEdgePopup(evt);
  }

  window.cgCloseEdgePopup = function() {
    _clearEdgeHighlight(false);
  };
  window.cgShowEdgeDetails = showEdgeDetails;

  /* ── Node visibility helpers ──────────────────────────────── */
  function _isolateNodes(visited) {
    _clearEdgeHighlight(false);
    var net = getNet(); if (!net) return;
    var nodesDs = window.nodes || net.body.data.nodes;
    nodesDs.update(ALL_NODE_IDS.map(function(id) {
      var parentId = VAR_PARENT[id];
      var visible = parentId ? !!visited[parentId] : !!visited[id];
      return { id: id, hidden: !visible };
    }));
    net.redraw();
    net.fit({ animation: { duration: 500 } });
  }

  function _showAllNodes() {
    _clearEdgeHighlight(false);
    var net = getNet(); if (!net) return;
    var nodesDs = window.nodes || net.body.data.nodes;
    nodesDs.update(ALL_NODE_IDS.map(function(id){ return {id:id, hidden:false}; }));
    net.redraw();
    net.unselectAll();
    net.fit({ animation: { duration: 500 } });
  }

  /* ── Search helpers ───────────────────────────────────────── */
  function findNodes(q) {
    q = (q || '').trim().toLowerCase();
    if (!q) return [];
    if (currentMode === 'inc' && window.cgxIncGetNodes) {
      var incNodes = window.cgxIncGetNodes();
      return incNodes.filter(function(n) {
        return (n.id || '').toLowerCase().indexOf(q) >= 0;
      }).map(function(n) { return { id: n.id, meta: { name: n.id } }; });
    }
    return NODE_DATA.filter(function(n) {
      if (!n.meta) return false;
      return (n.meta.name          || '').toLowerCase().indexOf(q) >= 0 ||
             (n.meta.qualified_name|| '').toLowerCase().indexOf(q) >= 0;
    });
  }

  function updateHint(matches) {
    if (!searchHint) return;
    var q = searchInput ? searchInput.value.trim() : '';
    if (!q) {
      searchHint.textContent = selectedNode ? 'Using selected node — or type to filter' : 'Click to browse • type to filter';
      return;
    }
    searchHint.textContent = matches.length === 0 ? 'No matches' :
      matches.length === 1 ? '1 match' : matches.length + ' matches';
  }

  /* Returns array of target node IDs: from search, or selected node. */
  function _getTargetNodes() {
    var q = searchInput ? searchInput.value.trim() : '';
    if (q) {
      var matches = findNodes(q);
      if (matches.length) return matches.map(function(n){return n.id;});
    }
    if (selectedNode) return [selectedNode];
    return [];
  }

  function _getFirstTarget() {
    var t = _getTargetNodes();
    return t.length ? t[0] : null;
  }

  /* ── Main search dropdown (function / script / varflow / include modes) ── */
  function _showSearchDropdown(q) {
    if (currentMode === 'varflow') { _vfSidebarSearch(q); return; }
    if (currentMode === 'module' && window.cgxModuleSearch) { window.cgxModuleSearch(q); return; }
    if (currentMode === 'inc' && window.cgxIncSearch) { window.cgxIncSearch(q); return; }
    var inp = document.getElementById('cg-search');
    var dd  = document.getElementById('cg-search-dropdown');
    if (!inp || !dd) return;
    var lq = (q || '').trim().toLowerCase();
    var candidates = NODE_DATA.filter(function(n){ return n.meta && !n.meta.is_external; });
    var filtered = lq
      ? candidates.filter(function(n){
          return (n.meta.name||'').toLowerCase().indexOf(lq) >= 0 ||
                 (n.meta.qualified_name||'').toLowerCase().indexOf(lq) >= 0;
        })
      : candidates.slice().sort(function(a,b){
          return (a.meta.name||'').localeCompare(b.meta.name||'');
        });
    filtered = filtered.slice(0, 60);
    if (!filtered.length) { dd.style.display='none'; return; }
    /* Position via getBoundingClientRect so it clears the overflow:auto sidebar */
    var rect = inp.getBoundingClientRect();
    dd.style.top   = (rect.bottom + 2) + 'px';
    dd.style.left  = rect.left + 'px';
    dd.style.width = rect.width + 'px';
    function _hi(text, q) {
      if (!q) return esc(text);
      var idx = (text||'').toLowerCase().indexOf(q);
      if (idx < 0) return esc(text);
      return esc(text.slice(0,idx))+'<span class="cg-sd-mark">'+esc(text.slice(idx,idx+q.length))+'</span>'+esc(text.slice(idx+q.length));
    }
    dd.innerHTML = filtered.map(function(n){
      var m = n.meta;
      var fname = (m.file_path||'').replace(/.*[\\/]/g,'');
      var displayName = m.qualified_name || m.name || n.id;
      return '<div class="cg-sd-item" data-nid="'+esc(n.id)+'">'
        +'<span class="cg-sd-name">'+_hi(displayName,lq)+'</span>'
        +'<span class="cg-sd-meta">'+esc(fname)+(m.language?' · '+esc(m.language):'')+'</span>'
        +'</div>';
    }).join('');
    dd.querySelectorAll('.cg-sd-item').forEach(function(item){
      item.addEventListener('mousedown', function(e){
        e.preventDefault();
        var nid = item.dataset.nid;
        var nd = NODE_DATA.find(function(n){return n.id===nid;});
        if (!nd) return;
        var inp2 = document.getElementById('cg-search');
        if (inp2) inp2.value = nd.meta.qualified_name || nd.meta.name || nid;
        dd.style.display = 'none';
        if (currentMode === 'script') {
          _svSelectFn(nid, true);
          _svScrollTo(nid);
        } else if (currentMode === 'module' && window.cgxModuleCenter) {
          window.cgxModuleCenter(nid);
        } else {
          var net = getNet();
          if (net) {
            net.selectNodes([nid]);
            net.fit({ nodes:[nid], animation:{ duration:400 } });
          }
        }
        updateHint(findNodes(inp2 ? inp2.value : ''));
      });
    });
    dd.style.display = 'block';
  }

  if (searchInput) {
    searchInput.addEventListener('focus', function() {
      _showSearchDropdown(searchInput.value);
    });
    searchInput.addEventListener('input', function() {
      _showSearchDropdown(searchInput.value);
      updateHint(findNodes(searchInput.value));
    });
    searchInput.addEventListener('blur', function() {
      setTimeout(function() {
        var dd = document.getElementById('cg-search-dropdown');
        if (dd) dd.style.display = 'none';
      }, 200);
    });
    searchInput.addEventListener('keydown', function(e) {
      if (e.key === 'Escape') {
        var dd = document.getElementById('cg-search-dropdown');
        if (dd) dd.style.display = 'none';
        searchInput.blur();
      }
      if (e.key === 'Enter') {
        var dd2 = document.getElementById('cg-search-dropdown');
        if (dd2) dd2.style.display = 'none';
        var matches = findNodes(searchInput.value);
        updateHint(matches);
        if (!matches.length) return;
        if (currentMode === 'script') {
          _svHighlight(matches.map(function(n){return n.id;}));
          if (matches[0]) _svScrollTo(matches[0].id);
          return;
        }
        if (currentMode === 'module' && window.cgxModuleHighlight) {
          window.cgxModuleHighlight(matches.map(function(n){return n.id;}), true);
          return;
        }
        if (currentMode === 'inc' && window.cgxIncHighlight) {
          window.cgxIncHighlight(matches.map(function(n){return n.id;}));
          return;
        }
        var net = getNet(); if (!net) return;
        net.selectNodes(matches.map(function(n){return n.id;}));
        net.fit({ nodes: [matches[0].id], animation: { duration: 500 } });
      }
    });
  }

  /* ── BFS reachability ─────────────────────────────────────── */
  function _reachable(nodeId, depth, direction) {
    var visited = {};
    var q = [[nodeId, 0]];
    while (q.length) {
      var item = q.shift(), nid = item[0], d = item[1];
      if (visited[nid]) continue;
      visited[nid] = true;
      if (d < depth) {
        if (direction !== 'callees')
          (EDGES_BY_TO[nid] || []).forEach(function(e){
            if (!visited[e.from]) q.push([e.from, d+1]);
          });
        if (direction !== 'callers')
          (EDGES_BY_FROM[nid] || []).forEach(function(e){
            if (!visited[e.to]) q.push([e.to, d+1]);
          });
      }
    }
    return visited;
  }

  function _getDepth() { return parseInt(focusDepth ? focusDepth.value : '3', 10); }
  function _getDir()   { return focusDir ? focusDir.value : 'both'; }

  /* ── Button handlers ──────────────────────────────────────── */
  if (btnHighlight) btnHighlight.addEventListener('click', function() {
    if (currentMode === 'varflow') {
      var vk = _vfGetTargetVar();
      if (!vk) { _flashBtn(btnHighlight, 'No match', 900); return; }
      _vfHighlightBlocks(vk);
      _flashBtn(btnHighlight, _vfCurrentNodes.length + ' blocks', 1200);
      return;
    }
    var targets = _getTargetNodes();
    updateHint(findNodes(searchInput ? searchInput.value : ''));
    if (!targets.length) { _flashBtn(btnHighlight, 'No match', 900); return; }
    if (currentMode === 'script') {
      _svHighlight(targets);
      _flashBtn(btnHighlight, targets.length + ' found', 1200);
      return;
    }
    if (currentMode === 'module' && window.cgxModuleHighlight) {
      window.cgxModuleHighlight(targets, true);
      _flashBtn(btnHighlight, targets.length + ' found', 1200);
      return;
    }
    if (currentMode === 'inc' && window.cgxIncHighlight) {
      window.cgxIncHighlight(targets);
      _flashBtn(btnHighlight, targets.length + ' found', 1200);
      return;
    }
    var net = getNet();
    if (!net) { _flashBtn(btnHighlight, 'Not ready', 900); return; }
    net.selectNodes(targets);
    net.fit({ nodes: [targets[0]], animation: { duration: 500 } });
    _flashBtn(btnHighlight, targets.length + ' found', 1200);
  });

  if (btnCenter) btnCenter.addEventListener('click', function() {
    if (currentMode === 'varflow') {
      var vk = _vfGetTargetVar();
      if (vk && vk !== _vfCurrentVar) _vfSelectVar(vk);
      _vfCenterOnFirst();
      return;
    }
    var target = _getFirstTarget();
    if (!target) { _flashBtn(btnCenter, 'No match', 900); return; }
    if (currentMode === 'script') { _svScrollTo(target); return; }
    if (currentMode === 'module' && window.cgxModuleCenter) { window.cgxModuleCenter(target); return; }
    if (currentMode === 'inc' && window.cgxIncCenter) { window.cgxIncCenter(target); return; }
    var net = getNet();
    if (!net) { _flashBtn(btnCenter, 'Not ready', 900); return; }
    net.selectNodes([target]);
    net.fit({ nodes: [target], animation: { duration: 600 } });
  });

  if (btnIsolate) btnIsolate.addEventListener('click', function() {
    if (currentMode === 'varflow') {
      var vk = _vfGetTargetVar();
      if (!vk) { _flashBtn(btnIsolate, 'No match', 900); return; }
      if (vk !== _vfCurrentVar) _vfSelectVar(vk);
      _vfHighlightBlocks(vk);
      _flashBtn(btnIsolate, _vfCurrentNodes.length + ' blocks', 1400);
      return;
    }
    var target = _getFirstTarget();
    if (!target) { _flashBtn(btnIsolate, 'No match', 900); return; }
    var visited = _reachable(target, _getDepth(), _getDir());
    if (currentMode === 'script') {
      _svIsolate(visited);
      _svSelectedNid = target;
      openDetail(target);
      _flashBtn(btnIsolate, Object.keys(visited).length + ' visible', 1400);
      return;
    }
    if (currentMode === 'module' && window.cgxModuleIsolate) {
      window.cgxModuleIsolate(visited, target);
      _flashBtn(btnIsolate, Object.keys(visited).length + ' visible', 1400);
      return;
    }
    if (currentMode === 'inc' && window.cgxIncIsolate) {
      var incDepth = _getDepth();
      var incDir   = _getDir();
      var incCount = window.cgxIncIsolate(_getFirstTarget(), incDepth, incDir);
      _flashBtn(btnIsolate, incCount + ' visible', 1400);
      return;
    }
    var net = getNet();
    if (!net) { _flashBtn(btnIsolate, 'Not ready', 900); return; }
    _isolateNodes(visited);
    net.selectNodes([target]);
    openDetail(target);
    _flashBtn(btnIsolate, Object.keys(visited).length + ' visible', 1400);
  });

  if (btnExpand) btnExpand.addEventListener('click', function() {
    if (currentMode === 'varflow') {
      var vk = _vfGetTargetVar();
      if (!vk) { _flashBtn(btnExpand, 'No match', 900); return; }
      if (vk !== _vfCurrentVar) {
        _vfSelectVar(vk);
        var inp2=document.getElementById('cg-vf-search-input');
        if (inp2) inp2.value=VAR_FLOW_DATA[vk][0].name;
      }
      _flashBtn(btnExpand, _vfCurrentNodes.length + ' blocks', 1400);
      return;
    }
    var target = _getFirstTarget();
    if (!target) { _flashBtn(btnExpand, 'No match', 900); return; }
    var visited = _reachable(target, _getDepth(), _getDir());
    if (currentMode === 'script') {
      var svEl = document.getElementById('cg-script-view');
      if (svEl) Object.keys(visited).forEach(function(id) {
        var row = svEl.querySelector('.cg-fn-row[data-nid="' + _cssEscape(id) + '"]');
        if (row) { row.classList.remove('sv-dim'); row.classList.add('sv-match'); }
      });
      _flashBtn(btnExpand, Object.keys(visited).length + ' shown', 1400);
      return;
    }
    if (currentMode === 'module' && window.cgxModuleExpand) {
      window.cgxModuleExpand(visited, target);
      _flashBtn(btnExpand, Object.keys(visited).length + ' shown', 1400);
      return;
    }
    if (currentMode === 'inc' && window.cgxIncIsolate) {
      var incCount2 = window.cgxIncIsolate(_getFirstTarget(), _getDepth(), _getDir());
      _flashBtn(btnExpand, incCount2 + ' shown', 1400);
      return;
    }
    var net = getNet();
    if (!net) { _flashBtn(btnExpand, 'Not ready', 900); return; }
    var nodesDs = window.nodes || net.body.data.nodes;
    nodesDs.update(Object.keys(visited).map(function(id){return{id:id,hidden:false};}));
    ALL_NODE_IDS.forEach(function(id) {
      if (VAR_PARENT[id] && visited[VAR_PARENT[id]])
        nodesDs.update([{id:id, hidden:false}]);
    });
    net.redraw();
    net.selectNodes([target]);
    net.fit({ nodes: [target], animation: { duration: 500 } });
    _flashBtn(btnExpand, Object.keys(visited).length + ' shown', 1400);
  });

  if (btnClearFocus) btnClearFocus.addEventListener('click', function() {
    if (currentMode === 'varflow') { _vfClearHighlight(); return; }
    selectedNode = null;
    if (currentMode === 'script') { _svClearHighlight(); updateHint([]); return; }
    if (currentMode === 'module' && window.cgxModuleClearFocus) { window.cgxModuleClearFocus(); updateHint([]); return; }
    if (currentMode === 'inc' && window.cgxIncClearFocus) { window.cgxIncClearFocus(); updateHint([]); return; }
    _showAllNodes();
    updateHint([]);
  });

  if (btnFit) btnFit.addEventListener('click', function() {
    if (currentMode === 'varflow') { _vfFitAll(); return; }
    if (currentMode === 'script') { _svFitView(); return; }
    if (currentMode === 'module' && window.cgxModuleFit) { window.cgxModuleFit(); return; }
    if (currentMode === 'inc' && window.cgxIncFit) { window.cgxIncFit(); return; }
    var net = getNet(); if (!net) return;
    net.fit({ animation: { duration: 500 } });
  });

  if (btnShowAll) btnShowAll.addEventListener('click', function() {
    if (currentMode === 'varflow') { _vfClearHighlight(); return; }
    selectedNode = null;
    if (currentMode === 'script') { _svClearHighlight(); updateHint([]); return; }
    if (currentMode === 'module' && window.cgxModuleClearFocus) { window.cgxModuleClearFocus(); updateHint([]); return; }
    if (currentMode === 'inc' && window.cgxIncClearFocus) { window.cgxIncClearFocus(); updateHint([]); return; }
    _showAllNodes();
    updateHint([]);
  });

  /* Save Layout — mode-aware explicit snapshot to localStorage */
  if (btnSaveLayout) btnSaveLayout.addEventListener('click', function() {
    if (currentMode === 'varflow') {
      try { localStorage.setItem(VF_LAYOUT_PFX + (_vfCurrentVar||''), JSON.stringify(_vfNodeOverrides)); } catch(e) {}
      _flashBtn(btnSaveLayout, 'Saved!', 1200);
      return;
    }
    if (currentMode === 'script') {
      var svLayout = {};
      document.querySelectorAll('#cg-sv-canvas .cg-file-card').forEach(function(c) {
        svLayout[c.dataset.fp] = {x: parseFloat(c.style.left)||0, y: parseFloat(c.style.top)||0};
      });
      try { localStorage.setItem(SV_LAYOUT_KEY, JSON.stringify(svLayout)); } catch(e) {}
      _flashBtn(btnSaveLayout, 'Saved!', 1200);
      return;
    }
    if (currentMode === 'module' && window.cgxModuleSaveLayout) {
      window.cgxModuleSaveLayout();
      _flashBtn(btnSaveLayout, 'Saved!', 1200);
      return;
    }
    /* fn-mode: save sparse diff against INITIAL_POS (saves only moved nodes) */
    var net = getNet(); if (!net) return;
    _fnSaveLayout(net);
    _flashBtn(btnSaveLayout, 'Saved!', 1200);
  });

  if (btnResetLayout) btnResetLayout.addEventListener('click', function() {
    if (currentMode === 'varflow') { _vfResetLayout(); _flashBtn(btnResetLayout, 'Reset!', 1200); return; }
    if (currentMode === 'script') {
      try { localStorage.removeItem(SV_LAYOUT_KEY); } catch(e) {}
      _svBuilt = false; _buildScriptView();
      _flashBtn(btnResetLayout, 'Reset!', 1200); return;
    }
    if (currentMode === 'module' && window.cgxModuleResetLayout) {
      window.cgxModuleResetLayout();
      _flashBtn(btnResetLayout, 'Reset!', 1200); return;
    }
    var net = getNet(); if (!net) return;
    Object.keys(INITIAL_POS).forEach(function(id) {
      try { net.moveNode(id, INITIAL_POS[id].x, INITIAL_POS[id].y); } catch(e) {}
    });
    /* After reset, saved layout = INITIAL_POS, so delta is empty */
    try { localStorage.removeItem(LAYOUT_KEY); } catch(e) {}
    net.fit({ animation: { duration: 600 } });
  });

  if (btnClearSaved) btnClearSaved.addEventListener('click', function() {
    if (currentMode === 'varflow') {
      _vfNodeOverrides = {};
      try { localStorage.removeItem(VF_LAYOUT_PFX + (_vfCurrentVar||'')); } catch(e) {}
      _flashBtn(btnClearSaved, 'Cleared!', 1600);
      return;
    }
    if (currentMode === 'script') {
      try { localStorage.removeItem(SV_LAYOUT_KEY); } catch(e) {}
      _svBuilt = false; _buildScriptView();
      _flashBtn(btnClearSaved, 'Cleared!', 1600);
      return;
    }
    if (currentMode === 'module' && window.cgxModuleClearSaved) {
      window.cgxModuleClearSaved();
      _flashBtn(btnClearSaved, 'Cleared!', 1600);
      return;
    }
    try { localStorage.removeItem(LAYOUT_KEY); } catch(e) {}
    _flashBtn(btnClearSaved, 'Cleared!', 1600);
  });

  /* ── Hover popup ──────────────────────────────────────────── */
  var hoverPopup = document.getElementById('cg-hover-popup');

  function buildHoverHtml(meta, nodeId) {
    if (!meta) return '';
    var h = '';

    h += '<div class="hp-badges">';
    var langCls = 'hp-lang-' + (meta.language || 'ext').toLowerCase().replace(/[^a-z]/g,'');
    if (meta.is_external) langCls = 'hp-lang-ext';
    h += '<span class="hp-badge ' + langCls + '">' + esc(meta.is_external ? 'external' : meta.language) + '</span>';
    if (meta.func_type)
      h += '<span class="hp-badge hp-ftype">' + esc(meta.func_type) + '</span>';
    h += '</div>';

    h += '<div class="hp-name">' + esc(meta.qualified_name || meta.name) + '</div>';

    if (meta.docstring) {
      var lines = String(meta.docstring).split('\\n');
      var firstLine = lines[0].trim().slice(0, 130);
      if (firstLine) h += '<div class="hp-doc">&ldquo;' + esc(firstLine) + (meta.docstring.length > 130 ? '&hellip;' : '') + '&rdquo;</div>';
    }

    if (meta.file_path && meta.file_path !== '<external>') {
      var fname = meta.file_path.replace(/.*[\\\\/]/, '');
      h += _hpRow('Location', esc(fname) + ':' + (meta.line_start || ''));
      if (meta.line_end && meta.line_end > meta.line_start)
        h += _hpRow('Lines', meta.line_start + ' &ndash; ' + meta.line_end);
    } else {
      h += _hpRow('Source', '<em>external / library</em>');
    }

    if (meta.parent && meta.parent !== '<external>') {
      var parentLabel = meta.is_method ? 'Class' : 'Namespace / Module';
      h += _hpRow(parentLabel, esc(meta.parent));
    }

    if (meta.return_type)
      h += _hpRow('Returns', '<span class="hp-val" style="color:#74B3F7">' + esc(meta.return_type) + '</span>');

    if (meta.parameters && meta.parameters.length) {
      h += '<div class="hp-divider"></div>';
      h += '<div class="hp-section-hdr">Parameters (' + meta.parameters.length + ')</div>';
      meta.parameters.forEach(function(p, i) {
        var pstr = esc(p.name);
        if (p.type_hint) pstr += ' <span style="color:#aaa">: ' + esc(p.type_hint) + '</span>';
        h += '<div class="hp-row"><span class="hp-lbl">arg ' + (i+1) + '</span><span class="hp-val">' + pstr + '</span></div>';
      });
    }

    if (meta.tracked_vars && Object.keys(meta.tracked_vars).length) {
      h += '<div class="hp-divider"></div>';
      h += '<div class="hp-section-hdr">Tracked variables</div>';
      Object.keys(meta.tracked_vars).forEach(function(v) {
        var val = String(meta.tracked_vars[v]).slice(0, 60);
        h += '<div class="hp-row"><span class="hp-lbl">local var</span>';
        h += '<span class="hp-val">' + esc(v) + ' = <span style="color:#aaa">' + esc(val) + '</span></span></div>';
      });
    }

    var callers = EDGES_BY_TO[nodeId]   || [];
    var callees = EDGES_BY_FROM[nodeId] || [];
    if (callers.length || callees.length) {
      h += '<div class="hp-divider"></div>';
      if (callers.length) h += _hpRow('Called by', callers.length + ' function(s)');
      if (callees.length) h += _hpRow('Calls',     callees.length + ' function(s)');
    }
    h += '<div style="font-size:9px;color:#4a5a6a;margin-top:6px">Double-click for full details</div>';
    return h;
  }

  function _positionPopup(mx, my) {
    if (!hoverPopup) return;
    var pw = hoverPopup.offsetWidth || 260, ph = hoverPopup.offsetHeight || 160;
    var vw = window.innerWidth, vh = window.innerHeight;
    var x = mx + 18, y = my - 10;
    if (x + pw > vw - 10) x = mx - pw - 12;
    if (y + ph > vh - 10) y = vh - ph - 10;
    if (y < 10) y = 10;
    hoverPopup.style.left = x + 'px';
    hoverPopup.style.top  = y + 'px';
  }

  function showHoverPopup(nodeId) {
    var nd = NODE_DATA.find(function(n){return n.id === nodeId;});
    if (!nd || !nd.meta || !hoverPopup) return;
    hoverPopup.innerHTML = buildHoverHtml(nd.meta, nodeId);
    hoverPopup.style.display = 'block';
    _positionPopup(lastMX, lastMY);
  }
  function hideHoverPopup() { if (hoverPopup) hoverPopup.style.display = 'none'; }

  window.cgCloseDetail = function() {
    var d = document.getElementById('cg-detail');
    if (d) d.classList.remove('open');
  };

  /* ── Click detail side panel ───────────────────────────────── */
  function openDetail(nodeId) {
    var nd = NODE_DATA.find(function(n){return n.id === nodeId;});
    if (!nd || !nd.meta) return;
    var m = nd.meta;
    var h = '<span id="cg-detail-close" onclick="cgCloseDetail()">&#x2715;</span>';
    h += '<div id="cg-detail-title">' + esc(m.qualified_name || m.name) + '</div>';
    if (m.func_type) h += '<div style="font-size:10px;color:#8090a0;margin-bottom:6px">' + esc(m.func_type) + '</div>';
    h += '<div class="cg-ds"><h3>Location</h3>';
    if (m.file_path && m.file_path !== '<external>') {
      h += '<p>' + esc(m.file_path) + ':' + (m.line_start||'');
      if (m.line_end && m.line_end > m.line_start) h += ' &ndash; ' + m.line_end;
      h += '</p>';
    } else h += '<p><em>external / library</em></p>';
    h += '</div>';
    if (m.language) h += '<div class="cg-ds"><h3>Language</h3><p>' + esc(m.language) + '</p></div>';
    if (m.parent)   h += '<div class="cg-ds"><h3>' + (m.is_method?'Class':'Namespace/Module') + '</h3><p>' + esc(m.parent) + '</p></div>';
    if (m.docstring) h += '<div class="cg-ds"><h3>Description</h3><p style="font-style:italic;color:#8da0b0;white-space:pre-wrap">' + esc(m.docstring.slice(0,300)) + '</p></div>';
    if (m.parameters && m.parameters.length) {
      h += '<div class="cg-ds"><h3>Parameters (' + m.parameters.length + ')</h3>';
      m.parameters.forEach(function(p) {
        h += '<p>' + esc((p.type_hint ? p.type_hint + ' ' : '') + p.name) + '</p>';
      });
      h += '</div>';
    }
    if (m.return_type) h += '<div class="cg-ds"><h3>Returns</h3><p>' + esc(m.return_type) + '</p></div>';
    if (m.tracked_vars && Object.keys(m.tracked_vars).length) {
      h += '<div class="cg-ds"><h3>Tracked Variables</h3>';
      Object.keys(m.tracked_vars).forEach(function(v) { h += '<p>' + esc(v + ' = ' + m.tracked_vars[v]) + '</p>'; });
      h += '</div>';
    }
    var callerEdges = EDGES_BY_TO[nodeId]   || [];
    var calleeEdges = EDGES_BY_FROM[nodeId] || [];
    if (callerEdges.length) {
      h += '<div class="cg-ds"><h3>Called by (' + callerEdges.length + ')</h3>';
      callerEdges.slice(0, 20).forEach(function(e) {
        var cn = NODE_DATA.find(function(n){return n.id===e.from;}); var lbl = cn ? (cn.meta&&cn.meta.name)||cn.id : e.from;
        h += '<div class="cg-ci" data-nid="' + esc(e.from) + '" onclick="cgSelectNode(this.dataset.nid)">' + esc(lbl) + '</div>';
      });
      h += '</div>';
    }
    if (calleeEdges.length) {
      h += '<div class="cg-ds"><h3>Calls (' + calleeEdges.length + ')</h3>';
      calleeEdges.slice(0, 20).forEach(function(e) {
        var cn = NODE_DATA.find(function(n){return n.id===e.to;}); var lbl = cn ? (cn.meta&&cn.meta.name)||cn.id : e.to;
        h += '<div class="cg-ci" data-nid="' + esc(e.to) + '" onclick="cgSelectNode(this.dataset.nid)">' + esc(lbl) + '</div>';
      });
      h += '</div>';
    }
    h += '<div style="font-size:10px;color:#5a6a7a;margin-top:12px">Double-click node for full inspection</div>';
    document.getElementById('cg-detail').innerHTML = h;
    document.getElementById('cg-detail').classList.add('open');
  }

  window.cgSelectNode = function(nodeId) {
    _clearEdgeHighlight(false);
    if (currentMode === 'script') { _svSelectFn(nodeId, true); return; }
    var net = getNet(); if (!net) return;
    net.selectNodes([nodeId]);
    net.fit({ nodes: [nodeId], animation: { duration: 500 } });
    openDetail(nodeId);
  };

  /* ── Double-click detail modal ─────────────────────────────── */
  function openModal(nodeId) {
    var nd = NODE_DATA.find(function(n){return n.id === nodeId;});
    if (!nd || !nd.meta) return;
    var m = nd.meta;
    var callerEdges = EDGES_BY_TO[nodeId]   || [];
    var calleeEdges = EDGES_BY_FROM[nodeId] || [];

    var h = '';

    /* Badges */
    h += '<div class="hp-badges" style="margin-bottom:10px">';
    var langCls = 'hp-lang-' + (m.language||'ext').toLowerCase().replace(/[^a-z]/g,'');
    if (m.is_external) langCls = 'hp-lang-ext';
    h += '<span class="hp-badge ' + langCls + '">' + esc(m.is_external?'external':m.language) + '</span>';
    if (m.func_type) h += '<span class="hp-badge hp-ftype">' + esc(m.func_type) + '</span>';
    h += '</div>';

    /* Name */
    h += '<div class="cg-modal-title">' + esc(m.name) + '</div>';
    if (m.qualified_name && m.qualified_name !== m.name)
      h += '<div class="cg-modal-qname">' + esc(m.qualified_name) + '</div>';

    /* ── Location ── */
    h += '<div class="cg-modal-section"><h3>Location</h3>';
    if (m.file_path && m.file_path !== '<external>') {
      h += '<div class="cg-modal-row"><span class="cg-modal-lbl">File</span><span class="cg-modal-val">' + esc(m.file_path) + '</span></div>';
      h += '<div class="cg-modal-row"><span class="cg-modal-lbl">Lines</span><span class="cg-modal-val">' + (m.line_start||'?') + (m.line_end && m.line_end > m.line_start ? ' &ndash; ' + m.line_end : '') + '</span></div>';
    } else {
      h += '<div class="cg-modal-row"><span class="cg-modal-lbl">Source</span><span class="cg-modal-val"><em>external / library</em></span></div>';
    }
    if (m.parent && m.parent !== '<external>') {
      var plabel = m.is_method ? 'Class' : 'Namespace / Module';
      h += '<div class="cg-modal-row"><span class="cg-modal-lbl">' + plabel + '</span><span class="cg-modal-val">' + esc(m.parent) + '</span></div>';
    }
    h += '</div>';

    /* ── Signature ── */
    h += '<div class="cg-modal-section"><h3>Signature</h3>';
    if (m.return_type) {
      h += '<div class="cg-modal-row"><span class="cg-modal-lbl">Return type</span><span class="cg-modal-val" style="color:#74B3F7">' + esc(m.return_type) + '</span></div>';
    } else {
      h += '<div class="cg-modal-row"><span class="cg-modal-lbl">Return type</span><span class="cg-modal-val" style="color:#5a6a7a">not specified</span></div>';
    }
    if (m.parameters && m.parameters.length) {
      h += '<div class="cg-modal-row"><span class="cg-modal-lbl">Parameters</span><span class="cg-modal-val">' + m.parameters.length + ' argument(s)</span></div>';
      m.parameters.forEach(function(p, i) {
        h += '<div class="cg-modal-param">';
        h += '<span class="cg-modal-param-num">' + (i+1) + '.</span>';
        h += '<span class="cg-modal-param-name">' + esc(p.name) + '</span>';
        if (p.type_hint) h += '<span class="cg-modal-param-type">: ' + esc(p.type_hint) + '</span>';
        h += '</div>';
      });
    } else {
      h += '<div class="cg-modal-row"><span class="cg-modal-lbl">Parameters</span><span class="cg-modal-val" style="color:#5a6a7a">none</span></div>';
    }
    h += '</div>';

    /* ── Description ── */
    if (m.docstring) {
      h += '<div class="cg-modal-section"><h3>Description</h3>';
      h += '<div class="cg-modal-doc">' + esc(m.docstring) + '</div>';
      h += '</div>';
    }

    /* ── Variables ── */
    h += '<div class="cg-modal-section"><h3>Variables</h3>';
    var hasTracked = m.tracked_vars && Object.keys(m.tracked_vars).length > 0;
    var vars = Array.isArray(m.variables) ? m.variables : [];
    if (vars.length) {
      h += '<div class="cg-modal-row"><span class="cg-modal-lbl">Detected</span><span class="cg-modal-val">' + vars.length + ' variable(s)</span></div>';
    }
    var kinds = [
      { key: 'static',      label: 'Static' },
      { key: 'global',      label: 'Global' },
      { key: 'local',       label: 'Local' },
      { key: 'dynamic',     label: 'Dynamic / heap allocated' },
      { key: 'field',       label: 'Instance variables / fields' },
      { key: 'environment', label: 'Environment variables' },
      { key: 'constant',    label: 'Constants' }
    ];
    function renderVarRow(v, kindLabel) {
      h += '<div class="cg-modal-param">';
      h += '<span class="cg-modal-var-scope">' + esc(kindLabel) + '</span>';
      h += '<span class="cg-modal-param-name">' + esc(v.name || '?') + '</span>';
      h += '<span class="cg-modal-param-type">type: ' + esc(v.type_hint || 'unknown') + '</span>';
      if (v.source_kind) {
        h += '<span class="cg-modal-var-source">source: ' + esc(v.source_kind);
        if (v.source_detail) h += ' (' + esc(v.source_detail) + ')';
        h += '</span>';
      }
      if (v.value) h += '<span style="color:#aaa">script: ' + esc(String(v.value).slice(0, 140)) + '</span>';
      if (v.line) h += '<span class="cg-modal-var-line">line&nbsp;' + v.line + '</span>';
      h += '</div>';
    }
    kinds.forEach(function(kindInfo) {
      var group = vars.filter(function(v){ return (v.scope || '').toLowerCase() === kindInfo.key; });
      h += '<div class="cg-modal-var-group"><div class="cg-modal-var-head">' + kindInfo.label + ' (' + group.length + ')</div>';
      if (group.length) {
        group.forEach(function(v) { renderVarRow(v, kindInfo.key); });
      } else {
        h += '<div class="cg-modal-var-empty">none detected</div>';
      }
      h += '</div>';
    });
    var knownKinds = {};
    kinds.forEach(function(k){ knownKinds[k.key] = true; });
    var otherVars = vars.filter(function(v) {
      var s = (v.scope || '').toLowerCase();
      return !knownKinds[s];
    });
    if (otherVars.length) {
      h += '<div class="cg-modal-var-group"><div class="cg-modal-var-head">Other (' + otherVars.length + ')</div>';
      otherVars.forEach(function(v) { renderVarRow(v, v.scope || 'var'); });
      h += '</div>';
    }
    if (!vars.length && hasTracked) {
      h += '<div class="cg-modal-row"><span class="cg-modal-lbl">Tracked vars</span><span class="cg-modal-val">' + Object.keys(m.tracked_vars).length + ' variable(s)</span></div>';
      Object.keys(m.tracked_vars).forEach(function(v) {
        var val = String(m.tracked_vars[v]).slice(0, 100);
        h += '<div class="cg-modal-param"><span class="cg-modal-param-name">' + esc(v) + '</span>';
        h += '<span class="cg-modal-param-type"> = ' + esc(val) + '</span></div>';
      });
    }
    if (hasTracked && vars.length) {
      h += '<div class="cg-modal-note">Configured tracked variables are still available as graph annotations when variable tracking is enabled.</div>';
    }
    h += '<div class="cg-modal-note">Variable detection is best-effort static analysis; dynamic fields, macro-generated variables, and runtime-created names may be incomplete.</div>';
    h += '</div>';

    /* ── Called by ── */
    h += '<div class="cg-modal-section"><h3>Called by (' + callerEdges.length + ')</h3>';
    if (callerEdges.length) {
      callerEdges.slice(0, 60).forEach(function(e) {
        var cn = NODE_DATA.find(function(n){return n.id===e.from;});
        var lbl = cn ? (cn.meta&&cn.meta.qualified_name)||cn.id : e.from;
        var sub = cn && cn.meta && cn.meta.file_path && cn.meta.file_path !== '<external>'
                  ? cn.meta.file_path.replace(/.*[\\\\/]/,'') : '';
        h += '<div class="cg-modal-ci" data-nid="' + esc(e.from) + '" onclick="cgSelectNode(this.dataset.nid);cgCloseModal()">';
        h += esc(lbl);
        if (sub) h += '<span style="color:#5a6a7a;font-size:10px"> &mdash; ' + esc(sub) + '</span>';
        h += '</div>';
      });
      if (callerEdges.length > 60) h += '<div class="cg-modal-note">&hellip; and ' + (callerEdges.length-60) + ' more</div>';
    } else {
      h += '<div class="cg-modal-note">Not called by any other function in this project (entry point or unreferenced).</div>';
    }
    h += '</div>';

    /* ── Calls ── */
    h += '<div class="cg-modal-section"><h3>Calls (' + calleeEdges.length + ')</h3>';
    if (calleeEdges.length) {
      calleeEdges.slice(0, 60).forEach(function(e) {
        var cn = NODE_DATA.find(function(n){return n.id===e.to;});
        var lbl = cn ? (cn.meta&&cn.meta.qualified_name)||cn.id : e.to;
        var isExt = cn && cn.meta && cn.meta.is_external;
        var sub = cn && cn.meta && cn.meta.file_path && !isExt
                  ? cn.meta.file_path.replace(/.*[\\\\/]/,'') : '';
        var hasLink = !isExt;
        h += '<div class="cg-modal-ci' + (hasLink ? '' : ' cg-modal-ci-nocursor') + '"' +
             (hasLink ? ' data-nid="' + esc(e.to) + '" onclick="cgSelectNode(this.dataset.nid);cgCloseModal()"' : '') + '>';
        if (isExt) h += '<span style="color:#95A5A6">▨ </span>';
        h += esc(lbl);
        if (sub) h += '<span style="color:#5a6a7a;font-size:10px"> &mdash; ' + esc(sub) + '</span>';
        if (isExt) h += '<span style="color:#5a6a7a;font-size:10px"> &mdash; external</span>';
        if (e.line) h += '<span style="color:#5a6a7a;font-size:10px"> line&nbsp;' + e.line + '</span>';
        h += '</div>';
      });
      if (calleeEdges.length > 60) h += '<div class="cg-modal-note">&hellip; and ' + (calleeEdges.length-60) + ' more</div>';
    } else {
      h += '<div class="cg-modal-note">Does not call any other function in this project (leaf function).</div>';
    }
    h += '</div>';

    var modalBody = document.getElementById('cg-modal-body');
    var modal     = document.getElementById('cg-modal');
    if (!modal || !modalBody) return;
    modalBody.innerHTML = h;
    modal.style.display = 'flex';
  }

  window.cgCloseModal = function() {
    var modal = document.getElementById('cg-modal');
    if (modal) modal.style.display = 'none';
  };
  /* Expose the detail-modal entry point so other views (Module View, etc.)
     can open it for a given function node_id. */
  window.cgOpenModalById = openModal;

  /* Close modal on Escape or backdrop click */
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      cgCloseModal();
      cgCloseEdgePopup();
    }
  });
  (function() {
    var modal = document.getElementById('cg-modal');
    if (modal) modal.addEventListener('click', function(e) {
      if (e.target === modal) cgCloseModal();
    });
  })();

  /* ── Error toggle ─────────────────────────────────────────── */
  var errToggle = document.getElementById('cg-err-toggle');
  if (errToggle) errToggle.addEventListener('click', function() {
    var p = document.getElementById('cg-err-panel');
    p.style.display = p.style.display === 'block' ? 'none' : 'block';
  });

  /* ── fn-mode layout save (sparse: only nodes moved from INITIAL_POS) ── */
  function _fnSaveLayout(net) {
    if (!net) return;
    var allPos = net.getPositions();
    var delta = {};
    var THRESH = 3; /* pixels — ignore sub-pixel jitter */
    Object.keys(allPos).forEach(function(id) {
      var cur = allPos[id], ini = INITIAL_POS[id];
      if (!ini || Math.abs(cur.x - ini.x) > THRESH || Math.abs(cur.y - ini.y) > THRESH) {
        delta[id] = cur;
      }
    });
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(delta)); } catch(e) {}
  }

  /* ── Wire vis.js events (poll until network is ready) ──────── */
  function wire() {
    var net = getNet();
    if (!net) {
      wireAttempts++;
      if (wireAttempts < 60) setTimeout(wire, 200);
      return;
    }

    net.setOptions({
      interaction: { dragNodes:true, dragView:true, zoomView:true, hover:true, tooltipDelay:9999, multiselect:true }
    });

    /* Fn-mode marquee selection (desktop-style drag on background). */
    var fnHost = document.getElementById('mynetwork');
    if (fnHost && !fnHost._fnMarqueeWired) {
      fnHost._fnMarqueeWired = true;
      fnHost.addEventListener('mousedown', function(ev){
        if (ev.button !== 0 && ev.button !== 1) return;
        if (currentMode !== 'fn') return;
        var n = getNet();
        if (!n) return;
        /* Middle mouse: pan vis.js canvas */
        if (ev.button === 1) {
          ev.preventDefault();
          _fnMidPan = { sx: ev.clientX, sy: ev.clientY, vp: n.getViewPosition(), sc: n.getScale() };
          return;
        }
        var rect = fnHost.getBoundingClientRect();
        var p = {x: ev.clientX - rect.left, y: ev.clientY - rect.top};
        var hit = null;
        try { hit = n.getNodeAt(p); } catch(e) {}
        if (hit) return; /* default vis.js node drag/select */
        if (ev.altKey) return; /* keep canvas-pan available with Alt+drag */
        ev.preventDefault();
        ev.stopPropagation();
        var box = document.createElement('div');
        box.className = 'cg-marquee-box';
        fnHost.appendChild(box);
        _fnMarquee = {
          net: n, host: fnHost, box: box,
          start: {x:p.x, y:p.y}, cur: {x:p.x, y:p.y}
        };
        try {
          n.setOptions({ interaction: { dragView:false, dragNodes:true, zoomView:true, hover:true, tooltipDelay:9999, multiselect:true }});
        } catch(e) {}
      });
    }
    if (!document._fnMarqueeDocWired) {
      document._fnMarqueeDocWired = true;
      document.addEventListener('mousemove', function(ev){
        if (_fnMidPan) {
          var n2 = getNet(); if (!n2) return;
          var dx = ev.clientX - _fnMidPan.sx, dy = ev.clientY - _fnMidPan.sy;
          try { n2.moveTo({ position:{ x:_fnMidPan.vp.x - dx/_fnMidPan.sc, y:_fnMidPan.vp.y - dy/_fnMidPan.sc }, animation:false }); } catch(e) {}
          return;
        }
        if (!_fnMarquee) return;
        var rect = _fnMarquee.host.getBoundingClientRect();
        _fnMarquee.cur = {x: ev.clientX - rect.left, y: ev.clientY - rect.top};
        var mr = _mkRect(_fnMarquee.start, _fnMarquee.cur);
        _setBoxRect(_fnMarquee.box, mr);
        var ids = [];
        try {
          var all = _fnMarquee.net.body.data.nodes.getIds();
          var pos = _fnMarquee.net.getPositions(all);
          all.forEach(function(id){
            var dp = _fnMarquee.net.canvasToDOM(pos[id]);
            if (dp.x >= mr.left && dp.x <= mr.right && dp.y >= mr.top && dp.y <= mr.bottom) ids.push(id);
          });
          _fnMarquee.net.selectNodes(ids, false);
        } catch(e) {}
      });
      document.addEventListener('mouseup', function(){
        _fnMidPan = null;
        if (!_fnMarquee) return;
        if (_fnMarquee.box && _fnMarquee.box.parentNode) _fnMarquee.box.parentNode.removeChild(_fnMarquee.box);
        try {
          _fnMarquee.net.setOptions({ interaction: { dragView:true, dragNodes:true, zoomView:true, hover:true, tooltipDelay:9999, multiselect:true }});
        } catch(e) {}
        _fnMarquee = null;
      });
    }

    /* Restore saved positions: sparse delta stored over INITIAL_POS base */
    var savedDelta0 = null;
    try { var raw0 = localStorage.getItem(LAYOUT_KEY); if (raw0) savedDelta0 = JSON.parse(raw0); } catch(e) {}
    if (savedDelta0) {
      Object.keys(savedDelta0).forEach(function(id) {
        try { net.moveNode(id, savedDelta0[id].x, savedDelta0[id].y); } catch(e) {}
      });
    }

    /* Comfortable default across modes: function view starts in layered mode. */
    try {
      if (!HUGE_GRAPH && window._cgApplyFnHierarchicalDefault) window._cgApplyFnHierarchicalDefault(net);
    } catch(e) {}

    /* Fit immediately — drawGraph() ran synchronously before wire(), so afterDrawing
       may never fire for the first draw. Call fit() directly to ensure the viewport
       is centered on the graph on load. */
    try { net.fit({ animation: false }); } catch(e) {}

    /* Also fit after the next draw as a belt-and-suspenders fallback */
    net.on('afterDrawing', function() {
      if (fittedOnce) return;
      fittedOnce = true;
      net.fit({ animation: false });
    });

    /* Auto-save positions on drag (debounced, sparse diff vs INITIAL_POS) */
    var _fnSaveTimer = null;
    net.on('dragStart', function(params) {
      if (!params.nodes || !params.nodes.length) { _fnGroupDrag = null; return; }
      var ids = [];
      try { ids = net.getSelectedNodes() || []; } catch(e) {}
      if (!ids.length) ids = params.nodes.slice();
      var startPos = {};
      try { startPos = net.getPositions(ids); } catch(e) {}
      var rect = null;
      ids.forEach(function(id){
        var p = startPos[id];
        if (!p) return;
        var r = {left:p.x-120, top:p.y-50, right:p.x+120, bottom:p.y+50};
        if (!rect) rect = r;
        else {
          rect.left = Math.min(rect.left, r.left);
          rect.top = Math.min(rect.top, r.top);
          rect.right = Math.max(rect.right, r.right);
          rect.bottom = Math.max(rect.bottom, r.bottom);
        }
      });
      _fnGroupDrag = { ids: ids, startPos: startPos, startRect: rect };
    });
    net.on('dragEnd', function(params) {
      if (!params.nodes || !params.nodes.length) return;
      if (_fnGroupDrag && _fnGroupDrag.ids && _fnGroupDrag.ids.length) {
        var curr = {};
        try { curr = net.getPositions(_fnGroupDrag.ids); } catch(e) {}
        var dx = 0, dy = 0;
        for (var i = 0; i < _fnGroupDrag.ids.length; i++) {
          var id = _fnGroupDrag.ids[i];
          var s = _fnGroupDrag.startPos[id], c = curr[id];
          if (s && c) { dx = c.x - s.x; dy = c.y - s.y; break; }
        }
        if ((dx || dy) && _fnGroupDrag.startRect) _fnMoveAnnotsBy(dx, dy, _fnGroupDrag.startRect);
      }
      _fnGroupDrag = null;
      clearTimeout(_fnSaveTimer);
      _fnSaveTimer = setTimeout(function() { _fnSaveLayout(getNet()); }, 400);
    });

    /* Sync Fn-mode annotation layer transform whenever vis.js redraws */
    net.on('afterDrawing', _fnUpdateLayerTransform);
    net.on('zoom', _fnUpdateLayerTransform);

    /* Page opens in fn mode by default — init annotation/pin layer immediately */
    setTimeout(_fnInitAnnotLayer, 80);

    /* Hover popup */
    net.on('hoverNode', function(p) {
      clearTimeout(hoverTimer);
      hoverTimer = setTimeout(function(){ showHoverPopup(p.node); }, 80);
    });
    net.on('blurNode', function() { clearTimeout(hoverTimer); hideHoverPopup(); });

    /* Single-click: select node, show detail panel */
    net.on('selectNode', function(p) {
      _clearEdgeHighlight(false);
      selectedNode = p.nodes[0];
      updateHint(findNodes(searchInput ? searchInput.value : ''));
      hideHoverPopup();
      openDetail(selectedNode);
    });
    net.on('deselectNode', function() {
      selectedNode = null;
      updateHint(findNodes(searchInput ? searchInput.value : ''));
    });

    /* Double-click: open full detail modal */
    net.on('doubleClick', function(p) {
      if (p.nodes && p.nodes.length > 0) {
        openModal(p.nodes[0]);
      }
    });

    /* Fallback for slot/refactor cases where the vis.js doubleClick event is
       swallowed before it reaches the Network event bus. */
    var netEl = document.getElementById('mynetwork');
    if (netEl && !netEl._cgDblClickFallbackWired) {
      netEl._cgDblClickFallbackWired = true;
      netEl.addEventListener('dblclick', function(ev) {
        var curNet = getNet();
        if (!curNet || currentMode !== 'fn') return;
        try {
          var rect = netEl.getBoundingClientRect();
          var nodeId = curNet.getNodeAt({x: ev.clientX - rect.left, y: ev.clientY - rect.top});
          if (nodeId) openModal(nodeId);
        } catch(e) {}
      });
    }

    /* Click on edge: show call details. Click on background: close detail panel. */
    net.on('click', function(p) {
      if (p.edges && p.edges.length) {
        showEdgeDetails(p.edges[0], p.event && p.event.srcEvent);
        return;
      }
      if ((!p.nodes || !p.nodes.length) && (!p.edges || !p.edges.length)) {
        var d = document.getElementById('cg-detail');
        if (d) d.classList.remove('open');
        _clearEdgeHighlight(false);
      }
    });
  }
  /* ── Large-graph warning + level-of-detail ─────────────────── */
  (function() {
    if (!LARGE_GRAPH) return;
    var warn = document.getElementById('cg-large-graph-warn');
    if (warn) {
      warn.style.display = 'block';
      warn.innerHTML = '<b>&#9888; Large graph (' + NODE_DATA.length + ' nodes)</b><br>'
        + 'Function Nodes mode uses vis.js canvas — smooth above ~2k nodes may be limited. '
        + 'Edges are straight for performance. '
        + 'Labels auto-hide when zoomed out. '
        + 'Script Nodes and Var Flow modes are unaffected.';
    }
    /* Level-of-detail: hide fn-mode labels when scale < LOD threshold */
    var _lodLabelsShown = true;
    var _lodTimer = null;
    var LOD_HIDE = 0.25, LOD_SHOW = 0.30;
    function _checkLod() {
      var net = getNet();
      if (!net || currentMode !== 'fn') return;
      try {
        var sc = net.getScale();
        var want = sc >= LOD_SHOW;
        if (want !== _lodLabelsShown) {
          _lodLabelsShown = want;
          net.setOptions({ nodes: { font: { size: want ? 11 : 0 } } });
        }
      } catch(e) {}
    }
    /* Wire LOD check to zoom events once vis.js is ready */
    var _lodWireTimer = setInterval(function() {
      var net = getNet();
      if (!net) return;
      clearInterval(_lodWireTimer);
      net.on('zoom', function() {
        clearTimeout(_lodTimer);
        _lodTimer = setTimeout(_checkLod, 80);
      });
    }, 300);
  })();

  /* ── CSS escape polyfill ───────────────────────────────────── */
  function _cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/([^\\w\\-])/g, '\\\\$1');
  }

  /* ── Script view mode switching ───────────────────────────── */
  function _updateLayoutBtns(mode) {
    var fnRow = document.getElementById('cg-fn-layout-row');
    var svRow = document.getElementById('cg-sv-layout-row');
    var vfRow = document.getElementById('cg-vf-layout-row');
    if (fnRow) fnRow.style.display = (mode === 'fn')      ? '' : 'none';
    if (svRow) svRow.style.display = (mode === 'script' || mode === 'module')  ? '' : 'none';
    if (vfRow) vfRow.style.display = (mode === 'varflow') ? '' : 'none';
  }

  function setViewMode(mode) {
    currentMode = mode;
    _updateLayoutBtns(mode);
    var netEl = document.getElementById('mynetwork');
    /* Pyvis wraps #mynetwork in a .card div — hide/show that wrapper */
    var netWrapper = (netEl && netEl.parentElement && netEl.parentElement !== document.body)
                     ? netEl.parentElement : netEl;
    var svEl = document.getElementById('cg-script-view');
    var vfEl = document.getElementById('cg-varflow-view');
    if (mode === 'varflow') {
      if (netWrapper) netWrapper.style.display = 'none';
      if (svEl) svEl.style.display = 'none';
      if (vfEl) vfEl.style.display = 'flex';
      if (btnModeFn) btnModeFn.classList.remove('active');
      if (btnModeSv) btnModeSv.classList.remove('active');
      if (btnModeVf) btnModeVf.classList.add('active');
      if (searchHint) searchHint.textContent = 'Type variable name to search';
      if (searchInput) searchInput.placeholder = 'Variable name…';
    } else if (mode === 'script') {
      if (netWrapper) netWrapper.style.display = 'none';
      if (svEl)  svEl.style.display  = 'flex';
      if (vfEl) vfEl.style.display = 'none';
      if (btnModeFn) btnModeFn.classList.remove('active');
      if (btnModeSv) btnModeSv.classList.add('active');
      if (btnModeVf) btnModeVf.classList.remove('active');
      if (searchInput) searchInput.placeholder = 'Function name...';
      if (searchHint) searchHint.textContent = 'Click to browse • type to filter';
      if (!_svBuilt) _buildScriptView();
      if (_edgeHighlightedEdgeId) {
        var edge = _edgeById(_edgeHighlightedEdgeId);
        if (edge) _applyScriptEdgeHighlight(edge);
      }
    } else {
      /* Function Nodes mode (vis.js). Above HUGE_THRESHOLD vis.js becomes unusable —
         show a banner and auto-redirect to Script Nodes mode. */
      if (HUGE_GRAPH) {
        var n = (typeof NODE_DATA !== 'undefined' ? NODE_DATA.length : '?');
        try {
          if (!window._hugeGraphBannerShown) {
            window._hugeGraphBannerShown = true;
            var hint = document.getElementById('cg-search-hint');
            if (hint) {
              hint.innerHTML = '<span style="color:#F7D774">Function Nodes view disabled '
                + '('+n+' nodes &ge; '+HUGE_THRESHOLD+'). Switched to Script Nodes view. '
                + 'Re-run with --summary-by-file or --entry/--depth for a focused graph.</span>';
            }
          }
        } catch(e) {}
        if (!_svBuilt) _buildScriptView();
        if (netWrapper) netWrapper.style.display = 'none';
        if (svEl)  svEl.style.display  = 'flex';
        if (vfEl) vfEl.style.display = 'none';
        if (btnModeFn) btnModeFn.classList.remove('active');
        if (btnModeSv) btnModeSv.classList.add('active');
        if (btnModeVf) btnModeVf.classList.remove('active');
        currentMode = 'script';
        return;
      }
      if (netWrapper) netWrapper.style.display = '';
      if (svEl)  svEl.style.display  = 'none';
      if (vfEl) vfEl.style.display = 'none';
      if (btnModeFn) btnModeFn.classList.add('active');
      if (btnModeSv) btnModeSv.classList.remove('active');
      if (btnModeVf) btnModeVf.classList.remove('active');
      if (searchInput) searchInput.placeholder = 'Function name...';
      if (searchHint) searchHint.textContent = 'Click to browse • type to filter';
      var net = getNet();
      if (net) try { net.fit({ animation: false }); } catch(e) {}
      /* Inject Fn-mode annotation layer on first show */
      setTimeout(_fnInitAnnotLayer, 80);
    }
  }

  if (btnModeFn) btnModeFn.addEventListener('click', function() { setViewMode('fn'); });
  if (btnModeSv) btnModeSv.addEventListener('click', function() { setViewMode('script'); });
  if (btnModeVf) btnModeVf.addEventListener('click', function() { setViewMode('varflow'); });

  /* Expose the original setViewMode so the extras-JS slot dispatcher (which
   * runs in a separate IIFE) can delegate back into the original implementation.
   * Without this `_cgOrigSetViewMode` is undefined → every slot button becomes
   * a no-op and Script / VarFlow / Function modes show a blank background. */
  window.setViewMode = setViewMode;
  window._cgOrigSetViewMode = setViewMode;
  window._cgSetCurrentMode = function(mode) {
    currentMode = mode;
    _updateLayoutBtns(mode);
    if (searchInput) {
      if (mode === 'inc') searchInput.placeholder = 'Header name...';
      else if (mode === 'module') searchInput.placeholder = 'Module name...';
      else if (mode === 'varflow') searchInput.placeholder = 'Variable name...';
      else searchInput.placeholder = 'Function name...';
    }
  };

  /* Auto-redirect huge graphs away from Function Nodes mode on initial load. */
  if (HUGE_GRAPH) {
    var _autoRedirect = function() { try { setViewMode('fn'); } catch(e) {} };
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', _autoRedirect);
    } else {
      setTimeout(_autoRedirect, 50);
    }
  }

  /* ── Script view graph canvas ──────────────────────────────── */
  function _svApplyTransform() {
    var c = document.getElementById('cg-sv-canvas');
    if (c) c.style.transform = 'translate('+_svPanX+'px,'+_svPanY+'px) scale('+_svZoom+')';
  }

  function _svArrowDefs() {
    return '<marker id="sv-arr" markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto">' +
      '<path d="M0,1 L9,5 L0,9 Z" fill="#6c8ebf"/></marker>' +
      '<marker id="sv-arr-active" markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto">' +
      '<path d="M0,1 L9,5 L0,9 Z" fill="#F7D774"/></marker>';
  }

  function _svGetRowAnchor(nid) {
    var canvas = document.getElementById('cg-sv-canvas');
    if (!canvas) return null;
    var row = canvas.querySelector('.cg-fn-row[data-nid="'+_cssEscape(nid)+'"]');
    if (!row) return null;
    var card = row.closest('.cg-file-card');
    if (!card) return null;
    var cx = parseFloat(card.style.left)||0;
    var cy = parseFloat(card.style.top)||0;
    var cw = card.offsetWidth || 290;
    var ry = cy + row.offsetTop + row.offsetHeight/2;
    return { fp:card.dataset.fp, lx:cx, ly:ry, rx:cx+cw, ry:ry };
  }

  function _svDrawEdges() {
    var svgEl = document.getElementById('cg-sv-edges');
    if (!svgEl) return;
    var nfMap = {};
    NODE_DATA.forEach(function(n){ if(n.meta) nfMap[n.id]=n.meta.file_path; });
    var html = '<defs>'+_svArrowDefs()+'</defs>';
    var count = 0;
    EDGE_DATA.forEach(function(e) {
      if (count >= 600) return;
      if (!nfMap[e.from]||!nfMap[e.to]||nfMap[e.from]===nfMap[e.to]) return;
      var sp=_svGetRowAnchor(e.from), dp=_svGetRowAnchor(e.to);
      if (!sp||!dp) return;
      var goRight = sp.lx < dp.lx;
      var sx=goRight?sp.rx:sp.lx, sy=sp.ry, tx=goRight?dp.lx:dp.rx, ty=dp.ry;
      var gap=Math.max(55, Math.abs(tx-sx)*0.42);
      var c1x=goRight?sx+gap:sx-gap, c2x=goRight?tx-gap:tx+gap;
      var color = _edgeBaseColor(e);
      var dash = e.confidence === 'HEURISTIC' ? ' stroke-dasharray="6 4"' : '';
      var active = String(e.id) === String(_edgeHighlightedEdgeId);
      var svd = _svStraightLines
        ? 'M'+sx+','+sy+' L'+tx+','+ty
        : 'M'+sx+','+sy+' C'+c1x+','+sy+' '+c2x+','+ty+' '+tx+','+ty;
      /* data-cat carries the edge category so the shared Edge-Type filter can
         hide/show this path immediately without needing a redraw. */
      var cat = e.category || (e.confidence === 'HEURISTIC' ? 'heuristic' : 'exact');
      html += '<path class="cg-sv-edge' + (active ? ' sv-edge-active' : '') + '" data-eid="' + esc(e.id) +
        '" data-cat="' + esc(cat) + '"' +
        ' d="' + svd + '" stroke="' + color +
        '" stroke-width="1.5" fill="none" opacity="0.55" marker-end="url(' + (active ? '#sv-arr-active' : '#sv-arr') + ')"' + dash + '/>';
      count++;
    });
    svgEl.innerHTML = html;
    /* Re-apply shared Edge Type filter so freshly-drawn paths honour the
       user's current selection (cards-drag, layout updates, etc.). */
    try {
      if (window.cgEdgeFilter) {
        var paths = svgEl.querySelectorAll('.cg-sv-edge[data-eid]');
        for (var pi = 0; pi < paths.length; ++pi) {
          var pel = paths[pi];
          var legend = (typeof cgEdgeCategoryToLegend === 'function')
            ? cgEdgeCategoryToLegend(pel.getAttribute('data-cat') || 'exact')
            : 'confirmed';
          pel.style.display = window.cgEdgeFilter[legend] ? '' : 'none';
        }
      }
    } catch(e) {}
  }

  function _svFitView() {
    var vp=document.getElementById('cg-sv-viewport'), canvas=document.getElementById('cg-sv-canvas');
    if (!vp||!canvas) return;
    var cards=canvas.querySelectorAll('.cg-file-card');
    if (!cards.length) return;
    var mnX=1e9,mnY=1e9,mxX=-1e9,mxY=-1e9;
    cards.forEach(function(c){
      var x=parseFloat(c.style.left)||0,y=parseFloat(c.style.top)||0;
      mnX=Math.min(mnX,x);mnY=Math.min(mnY,y);
      mxX=Math.max(mxX,x+c.offsetWidth);mxY=Math.max(mxY,y+c.offsetHeight);
    });
    var pad=60,cw=mxX-mnX+pad*2,ch=mxY-mnY+pad*2;
    var vw=vp.offsetWidth,vh=vp.offsetHeight;
    var fitZoom = Math.min(vw/cw,vh/ch);
    _svZoom=Math.max(0.24,Math.min(1.25,fitZoom));
    _svPanX=(vw-cw*_svZoom)/2-(mnX-pad)*_svZoom;
    _svPanY=(vh-ch*_svZoom)/2-(mnY-pad)*_svZoom;
    _svApplyTransform();
  }

  /* ── Dependency-aware script view layout ───────────────────── */
  function _svBuildFileLayout(fileMap, cardW) {
    var fileIds = Object.keys(fileMap);
    var langRank = {'Python':0,'C':1,'C++':2,'MATLAB':3};
    var nodeFile = {};
    NODE_DATA.forEach(function(n) {
      if (n.meta && !n.meta.is_external && n.meta.file_path) nodeFile[n.id] = n.meta.file_path;
    });

    fileIds.forEach(function(fp) {
      fileMap[fp].fns.sort(function(a, b) {
        var al = (a.meta && a.meta.line_start) || 0;
        var bl = (b.meta && b.meta.line_start) || 0;
        if (al !== bl) return al - bl;
        return String((a.meta && a.meta.qualified_name) || a.id).localeCompare(String((b.meta && b.meta.qualified_name) || b.id));
      });
    });

    var adj = {}, rev = {}, weights = {};
    fileIds.forEach(function(fp) { adj[fp] = {}; rev[fp] = {}; });
    EDGE_DATA.forEach(function(e) {
      var fromFile = nodeFile[e.from], toFile = nodeFile[e.to];
      if (!fromFile || !toFile || fromFile === toFile || !fileMap[fromFile] || !fileMap[toFile]) return;
      adj[fromFile][toFile] = (adj[fromFile][toFile] || 0) + 1;
      rev[toFile][fromFile] = (rev[toFile][fromFile] || 0) + 1;
      var key = fromFile + '\u001f' + toFile;
      weights[key] = (weights[key] || 0) + 1;
    });

    function fname(fp) { return fp.replace(/.*[\\\\/]/g, ''); }
    function fileSort(a, b) {
      var la = langRank[fileMap[a].lang] == null ? 99 : langRank[fileMap[a].lang];
      var lb = langRank[fileMap[b].lang] == null ? 99 : langRank[fileMap[b].lang];
      if (la !== lb) return la - lb;
      return fname(a).localeCompare(fname(b)) || a.localeCompare(b);
    }
    function estHeight(fp) {
      var h = 52;
      fileMap[fp].fns.forEach(function(n) {
        var outgoing = (EDGES_BY_FROM[n.id] || []).length;
        h += outgoing ? 72 : 48;
      });
      return Math.max(96, h);
    }
    function columnHeight(files) {
      if (!files || !files.length) return 0;
      return files.reduce(function(sum, fp){ return sum + estHeight(fp); }, 0) + Math.max(0, files.length - 1) * CARD_GAP_Y;
    }

    var visited = {}, comps = [];
    fileIds.sort(fileSort).forEach(function(start) {
      if (visited[start]) return;
      var comp = [], q = [start];
      visited[start] = true;
      while (q.length) {
        var fp = q.shift();
        comp.push(fp);
        Object.keys(adj[fp]).concat(Object.keys(rev[fp])).sort(fileSort).forEach(function(nb) {
          if (!visited[nb]) { visited[nb] = true; q.push(nb); }
        });
      }
      comps.push(comp);
    });

    comps.sort(function(a, b) {
      var ae = a.reduce(function(sum, fp){ return sum + Object.keys(adj[fp]).length; }, 0);
      var be = b.reduce(function(sum, fp){ return sum + Object.keys(adj[fp]).length; }, 0);
      if ((be > 0) !== (ae > 0)) return be - ae;
      var af = a.slice().sort(fileSort)[0], bf = b.slice().sort(fileSort)[0];
      var al = langRank[fileMap[af].lang] == null ? 99 : langRank[fileMap[af].lang];
      var bl = langRank[fileMap[bf].lang] == null ? 99 : langRank[fileMap[bf].lang];
      if (al !== bl) return al - bl;
      return b.length - a.length || fileSort(af, bf);
    });

    function layoutComponent(comp) {
      var compSet = {};
      comp.forEach(function(fp){ compSet[fp] = true; });
      var indeg = {}, outdeg = {};
      comp.forEach(function(fp) {
        indeg[fp] = Object.keys(rev[fp]).filter(function(x){ return compSet[x]; }).length;
        outdeg[fp] = Object.keys(adj[fp]).filter(function(x){ return compSet[x]; }).length;
      });

      var remaining = {}, order = [], queue = [];
      comp.forEach(function(fp){ remaining[fp] = true; if (indeg[fp] === 0) queue.push(fp); });
      queue.sort(fileSort);
      while (queue.length) {
        var fp = queue.shift();
        if (!remaining[fp]) continue;
        delete remaining[fp];
        order.push(fp);
        Object.keys(adj[fp]).filter(function(nb){ return compSet[nb]; }).sort(fileSort).forEach(function(nb) {
          indeg[nb]--;
          if (indeg[nb] === 0) queue.push(nb);
        });
        queue.sort(fileSort);
      }
      Object.keys(remaining).sort(function(a, b) {
        return (outdeg[b] - indeg[b]) - (outdeg[a] - indeg[a]) || fileSort(a, b);
      }).forEach(function(fp){ order.push(fp); });

      var layer = {};
      order.forEach(function(fp) {
        var preds = Object.keys(rev[fp]).filter(function(p){ return compSet[p] && layer[p] != null; });
        layer[fp] = preds.length ? Math.max.apply(null, preds.map(function(p){ return layer[p] + 1; })) : 0;
      });
      for (var pass = 0; pass < comp.length; pass++) {
        var changed = false;
        comp.forEach(function(fp) {
          Object.keys(adj[fp]).filter(function(nb){ return compSet[nb]; }).forEach(function(nb) {
            if (layer[nb] <= layer[fp] && !(adj[nb] && adj[nb][fp])) {
              layer[nb] = layer[fp] + 1;
              changed = true;
            }
          });
        });
        if (!changed) break;
      }

      var layers = {};
      comp.forEach(function(fp){ var l = layer[fp] || 0; if (!layers[l]) layers[l] = []; layers[l].push(fp); });
      var layerKeys = Object.keys(layers).map(Number).sort(function(a,b){return a-b;});
      layerKeys.forEach(function(l) { layers[l].sort(fileSort); });

      for (var iter = 0; iter < 4; iter++) {
        layerKeys.forEach(function(l) {
          if (l === layerKeys[0]) return;
          layers[l].sort(function(a, b) {
            function bary(fp) {
              var preds = Object.keys(rev[fp]).filter(function(p){ return compSet[p] && layers[layer[p]]; });
              if (!preds.length) return 0;
              var total = 0, count = 0;
              preds.forEach(function(p) {
                var w = weights[p + '\u001f' + fp] || 1;
                total += layers[layer[p]].indexOf(p) * w; count += w;
              });
              return total / count;
            }
            return bary(a) - bary(b) || fileSort(a, b);
          });
        });
        layerKeys.slice().reverse().forEach(function(l) {
          if (l === layerKeys[layerKeys.length - 1]) return;
          layers[l].sort(function(a, b) {
            function bary(fp) {
              var succ = Object.keys(adj[fp]).filter(function(s){ return compSet[s] && layers[layer[s]]; });
              if (!succ.length) return 0;
              var total = 0, count = 0;
              succ.forEach(function(s) {
                var w = weights[fp + '\u001f' + s] || 1;
                total += layers[layer[s]].indexOf(s) * w; count += w;
              });
              return total / count;
            }
            return bary(a) - bary(b) || fileSort(a, b);
          });
        });
      }
      return { layers: layers, layerKeys: layerKeys };
    }

    var positions = {};
    var CARD_GAP_X = _svCardGapX, CARD_GAP_Y = _svCardGapY, COMP_GAP_X = _svCompGapX, COMP_GAP_Y = _svCompGapY;
    var MAX_CANVAS_W = 9000;
    var cursorX = 80, cursorY = 80, rowH = 0;
    comps.forEach(function(comp) {
      var laid = layoutComponent(comp);
      var compW = laid.layerKeys.length * cardW + Math.max(0, laid.layerKeys.length - 1) * CARD_GAP_X;
      var compH = Math.max.apply(null, laid.layerKeys.map(function(l){ return columnHeight(laid.layers[l]); }).concat([100]));
      if (cursorX > 80 && cursorX + compW > MAX_CANVAS_W) {
        cursorX = 80; cursorY += rowH + COMP_GAP_Y; rowH = 0;
      }
      laid.layerKeys.forEach(function(l, colIdx) {
        var col = laid.layers[l];
        var y = cursorY + Math.max(0, (compH - columnHeight(col)) / 2);
        col.forEach(function(fp) {
          positions[fp] = { x: cursorX + colIdx * (cardW + CARD_GAP_X) + cardW / 2, y: y + 60 };
          y += estHeight(fp) + CARD_GAP_Y;
        });
      });
      cursorX += compW + COMP_GAP_X;
      rowH = Math.max(rowH, compH);
    });

    return {
      positions: positions,
      order: fileIds.slice().sort(function(a, b) {
        var pa = positions[a] || {x:0,y:0}, pb = positions[b] || {x:0,y:0};
        return pa.y - pb.y || pa.x - pb.x || fileSort(a, b);
      })
    };
  }

  /* ── Build script view DOM ─────────────────────────────────── */
  function _buildScriptView() {
    var svEl = document.getElementById('cg-script-view');
    if (!svEl) return;
    _svBuilt = true;
    _svPanX = 0; _svPanY = 0; _svZoom = 1.0;

    var CARD_W = 290;
    var langColors = {'Python':'#4A90D9','C':'#E8832A','C++':'#27AE60','MATLAB':'#8E44AD'};

    var fileMap = {};
    NODE_DATA.forEach(function(n) {
      if (!n.meta || n.meta.is_external) return;
      var fp = n.meta.file_path || '<unknown>';
      if (!fileMap[fp]) fileMap[fp] = { lang: n.meta.language, fns: [] };
      fileMap[fp].fns.push(n);
    });

    var scriptLayout = _svBuildFileLayout(fileMap, CARD_W);
    var filePos = scriptLayout.positions;
    var fileOrder = scriptLayout.order;

    /* Load saved card positions (user may have dragged and saved) */
    var _svSavedLayout = null;
    try { var _svRaw = localStorage.getItem(SV_LAYOUT_KEY); if (_svRaw) _svSavedLayout = JSON.parse(_svRaw); } catch(e) {}

    /* Precompute lookup maps to avoid O(n*m) inner loops */
    var _svNodeById = {};
    NODE_DATA.forEach(function(n) { _svNodeById[n.id] = n; });
    var _svEdgesFrom = {};   /* node_id -> [edge] */
    EDGE_DATA.forEach(function(e) {
      if (!_svEdgesFrom[e.from]) _svEdgesFrom[e.from] = [];
      _svEdgesFrom[e.from].push(e);
    });
    /* For large graphs, collapse cards with many functions to keep DOM size manageable */
    var SV_COLLAPSE_THRESHOLD = LARGE_GRAPH ? 12 : Infinity;

    /* Build absolutely-positioned file cards */
    var langDotColors = {'Python':'#4A90D9','C':'#E8832A','C++':'#27AE60','MATLAB':'#8E44AD'};
    var cardsHtml = '';
    fileOrder.forEach(function(fp) {
      var info = fileMap[fp];
      var fname = fp.replace(/.*[\\\\/]/g, '');
      var dirPart = fp.length > fname.length ? fp.slice(0, fp.length - fname.length - 1) : '';
      var dotColor = langDotColors[info.lang] || '#95A5A6';
      var langLower = (info.lang || '').toLowerCase().replace(/[^a-z]/g, '');
      var pos = filePos[fp];
      var savedCardPos = _svSavedLayout && _svSavedLayout[fp];
      var cx = savedCardPos ? Math.round(savedCardPos.x) : Math.round(pos.x - CARD_W / 2);
      var cy = savedCardPos ? Math.round(savedCardPos.y) : Math.round(pos.y - 60);

      cardsHtml += '<div class="cg-file-card" data-fp="' + esc(fp) +
        '" style="left:' + cx + 'px;top:' + cy + 'px;width:' + CARD_W + 'px">';
      cardsHtml += '<div class="cg-fc-header">';
      cardsHtml += '<button class="cg-fc-collapse-btn" title="Collapse / expand card" onclick="_svToggleCardCollapse(event)">▼</button>';
      cardsHtml += '<div class="cg-dot" style="background:' + dotColor + ';flex-shrink:0;margin-top:0"></div>';
      cardsHtml += '<div class="cg-fc-fname">' + esc(fname) + '</div>';
      if (dirPart) cardsHtml += '<div class="cg-fc-dir" title="' + esc(fp) + '">' + esc(dirPart) + '</div>';
      cardsHtml += '<div class="cg-fc-count">' + info.fns.length + ' fn' + (info.fns.length !== 1 ? 's' : '') + '</div>';
      cardsHtml += '</div><div class="cg-fn-list">';

      var fnsToRender = info.fns;
      var fnsHidden = 0;
      if (info.fns.length > SV_COLLAPSE_THRESHOLD) {
        fnsToRender = info.fns.slice(0, SV_COLLAPSE_THRESHOLD);
        fnsHidden   = info.fns.length - SV_COLLAPSE_THRESHOLD;
      }

      fnsToRender.forEach(function(n) {
        var m = n.meta;
        var sameFile = [], crossFile = [], extCalls = [];
        (_svEdgesFrom[n.id] || []).forEach(function(e) {
          var cn = _svNodeById[e.to];
          if (!cn || !cn.meta) return;
          if (cn.meta.is_external) extCalls.push(cn);
          else if (cn.meta.file_path === fp) sameFile.push(cn);
          else crossFile.push(cn);
        });

        var sig = '(';
        if (m.parameters && m.parameters.length) {
          sig += m.parameters.map(function(p){
            return (p.type_hint ? p.type_hint + ' ' : '') + p.name;
          }).join(', ');
        }
        sig += ')';
        if (m.return_type) sig += ' → ' + m.return_type;

        cardsHtml += '<div class="cg-fn-row" data-nid="' + esc(n.id) + '">';
        cardsHtml += '<div class="cg-fn-top">';
        cardsHtml += '<span class="cg-fn-typebadge hp-lang-' + langLower + '">' + esc(info.lang) + '</span>';
        if (m.parent && m.parent !== '<external>')
          cardsHtml += '<span class="cg-fn-nm">' + esc(m.parent) + '::</span>';
        cardsHtml += '<span class="cg-fn-nm cg-fn-nm-main">' + esc(m.name) + '</span>';
        cardsHtml += '<span class="cg-fn-sig">' + esc(sig) + '</span></div>';

        var maxShow = 6, shown = 0;
        if (sameFile.length || crossFile.length || extCalls.length) {
          cardsHtml += '<div class="cg-fn-callees">';
          sameFile.slice(0, maxShow).forEach(function(cn) {
            cardsHtml += '<span class="cg-cb cg-cb-same" data-nid="' + esc(cn.id) + '" title="same file">' + esc(cn.meta.name) + '</span>';
            shown++;
          });
          crossFile.slice(0, Math.max(0, maxShow - shown)).forEach(function(cn) {
            cardsHtml += '<span class="cg-cb cg-cb-cross" data-nid="' + esc(cn.id) + '" title="' + esc(cn.meta.file_path ? cn.meta.file_path.replace(/.*[\\\\/]/g,'') : '') + '">' + esc(cn.meta.name) + '</span>';
            shown++;
          });
          extCalls.slice(0, Math.max(0, maxShow - shown)).forEach(function(cn) {
            cardsHtml += '<span class="cg-cb cg-cb-ext" title="external">' + esc(cn.meta.name) + '</span>';
            shown++;
          });
          var total = sameFile.length + crossFile.length + extCalls.length;
          if (total > maxShow) cardsHtml += '<span class="cg-cb cg-cb-more">+' + (total - shown) + '</span>';
          cardsHtml += '</div>';
        }
        if (m.line_start) {
          cardsHtml += '<div class="cg-fn-meta">line ' + m.line_start;
          if (m.line_end && m.line_end > m.line_start) cardsHtml += ' – ' + m.line_end;
          cardsHtml += '</div>';
        }
        cardsHtml += '</div>';
      });
      if (fnsHidden > 0) {
        cardsHtml += '<div class="cg-fn-row cg-fn-row-more" style="color:#888;font-size:10px;padding:4px 8px">… and '
          + fnsHidden + ' more functions (large graph mode)</div>';
      }
      cardsHtml += '</div></div>';
    });

    /* Assemble viewport → canvas → (SVG edges + cards) */
    svEl.innerHTML =
      '<button id="cg-sv-annot-btn" title="Draw annotation rectangle (click then drag)" '
      +'onclick="_svToggleAnnotMode()" style="position:absolute;top:10px;right:14px;z-index:20;'
      +'padding:4px 10px;font-size:11px;font-weight:600;border:1px solid #3d4451;'
      +'background:#1a1d23;color:#4ec980;border-radius:4px;cursor:pointer;white-space:nowrap">'
      +'□ Annotate</button>'
      +'<div id="cg-sv-viewport">' +
        '<div id="cg-sv-canvas">' +
          '<svg id="cg-sv-edges" width="1" height="1" ' +
               'style="overflow:visible;position:absolute;top:0;left:0;pointer-events:auto"></svg>' +
          cardsHtml +
        '</div>' +
      '</div>';
    _svRenderAnnots();
    _svRestoreCollapsedState();
    _renderAllPins(document.getElementById('cg-sv-canvas'), 'sv', '');
    window._wireSvAnnotEvents && window._wireSvAnnotEvents();
    /* Wire pin right-click on script viewport */
    (function(){
      var svVp = document.getElementById('cg-sv-viewport');
      if (!svVp || svVp._svPinWired) return;
      svVp._svPinWired = true;
      svVp.addEventListener('contextmenu', function(e){
        if (e.target.closest('.cg-file-card')||e.target.closest('.cg-vf-annot')||e.target.closest('.cg-pin')) return;
        e.preventDefault();
        var c2 = document.getElementById('cg-sv-canvas');
        var r2 = c2 ? c2.getBoundingClientRect() : svVp.getBoundingClientRect();
        _showPinCtxMenu(e.clientX, e.clientY, 'sv', '',
          (e.clientX - r2.left) / _svZoom, (e.clientY - r2.top) / _svZoom);
      });
    })();

    /* Click / dblclick delegation on canvas */
    var canvas = document.getElementById('cg-sv-canvas');
    if (canvas) {
      canvas.addEventListener('click', function(e) {
        var edgePath = e.target.closest && e.target.closest('.cg-sv-edge[data-eid]');
        if (edgePath) { e.stopPropagation(); showEdgeDetails(edgePath.dataset.eid, e); return; }
        var cb = e.target.closest && e.target.closest('.cg-cb[data-nid]');
        if (cb) { e.stopPropagation(); _svSelectFn(cb.dataset.nid, true); return; }
        /* Collapse button is handled by its own onclick — just bail */
        if (e.target.closest && e.target.closest('.cg-fc-collapse-btn')) return;
        var row = e.target.closest && e.target.closest('.cg-fn-row[data-nid]');
        if (row) { _svSelectFn(row.dataset.nid, false); return; }
        var hdr = e.target.closest && e.target.closest('.cg-fc-header');
        if (hdr) { var hCard = hdr.closest('.cg-file-card'); if (hCard) _svSelectCard(hCard.dataset.fp); return; }
        _clearEdgeHighlight(false);
      });
      canvas.addEventListener('dblclick', function(e) {
        var row = e.target.closest && e.target.closest('.cg-fn-row[data-nid]');
        if (row) openModal(row.dataset.nid);
      });
    }

    /* Wheel zoom on viewport */
    var vp = document.getElementById('cg-sv-viewport');
    if (vp) {
      vp.addEventListener('wheel', function(e) {
        e.preventDefault();
        var factor = e.deltaY < 0 ? 1.1 : 0.909;
        var rect = vp.getBoundingClientRect();
        var mx = e.clientX - rect.left, my = e.clientY - rect.top;
        _svZoom = Math.max(0.08, Math.min(3.0, _svZoom * factor));
        /* Zoom around the cursor position */
        _svPanX = mx - (mx - _svPanX) * factor;
        _svPanY = my - (my - _svPanY) * factor;
        _svApplyTransform();
      }, { passive: false });

      /* Mousedown: card drag (header) or viewport pan */
      vp.addEventListener('mousedown', function(e) {
        if (e.button !== 0 && e.button !== 1) return;
        if (e.target.closest && e.target.closest('.cg-sv-edge')) return;
        /* Middle mouse always pans the viewport */
        if (e.button === 1) {
          e.preventDefault();
          vp.classList.add('sv-panning');
          _svViewDrag = { startX: e.clientX, startY: e.clientY, origPanX: _svPanX, origPanY: _svPanY };
          return;
        }
        var hdr = e.target.closest && e.target.closest('.cg-fc-header');
        if (hdr) {
          if (e.target.closest && e.target.closest('.cg-fc-collapse-btn')) return;
          var dCard = hdr.closest('.cg-file-card');
          if (!dCard) return;
          e.preventDefault();
          var fp = dCard.getAttribute('data-fp') || '';
          var useGroup = !!_svMultiSel[fp];
          var cards = [];
          if (useGroup) {
            Object.keys(_svMultiSel).forEach(function(selFp){
              if (!_svMultiSel[selFp]) return;
              var c = svEl.querySelector('.cg-file-card[data-fp="' + _cssEscape(selFp) + '"]');
              if (!c) return;
              cards.push({ card:c, origL:parseFloat(c.style.left)||0, origT:parseFloat(c.style.top)||0 });
            });
          }
          if (!cards.length) {
            cards.push({ card:dCard, origL:parseFloat(dCard.style.left)||0, origT:parseFloat(dCard.style.top)||0 });
            var one = {}; one[fp] = true; _svSetMultiSel(one);
          }
          var selRect = null;
          cards.forEach(function(it){
            var l = it.origL, t = it.origT;
            var r = l + (it.card.offsetWidth || 0);
            var b = t + (it.card.offsetHeight || 0);
            if (!selRect) selRect = {left:l, top:t, right:r, bottom:b};
            else {
              selRect.left = Math.min(selRect.left, l);
              selRect.top = Math.min(selRect.top, t);
              selRect.right = Math.max(selRect.right, r);
              selRect.bottom = Math.max(selRect.bottom, b);
            }
          });
          _svCardDrag = {
            cards: cards,
            startX: e.clientX, startY: e.clientY,
            selRectStart: selRect
          };
        } else if (!_svAnnotMode && !e.target.closest('.cg-fn-row') && !e.target.closest('.cg-cb')) {
          e.preventDefault();
          if (e.altKey) {
            vp.classList.add('sv-panning');
            _svViewDrag = { startX: e.clientX, startY: e.clientY, origPanX: _svPanX, origPanY: _svPanY };
            return;
          }
          var vr = vp.getBoundingClientRect();
          var p0 = {x: e.clientX - vr.left, y: e.clientY - vr.top};
          var box = document.createElement('div');
          box.className = 'cg-marquee-box';
          vp.appendChild(box);
          _svMarquee = {vp:vp, box:box, start:p0, cur:p0};
          _setBoxRect(box, _mkRect(p0, p0));
        }
      });
    }

    /* Global mousemove / mouseup for drag (added once, gated by drag state) */
    document.addEventListener('mousemove', function(e) {
      if (_svCardDrag) {
        var dx = (e.clientX - _svCardDrag.startX) / _svZoom;
        var dy = (e.clientY - _svCardDrag.startY) / _svZoom;
        _svCardDrag.cards.forEach(function(it){
          it.card.style.left = (it.origL + dx) + 'px';
          it.card.style.top  = (it.origT + dy) + 'px';
        });
        _svDrawEdges();
      } else if (_svMarquee) {
        var vr = _svMarquee.vp.getBoundingClientRect();
        _svMarquee.cur = {x: e.clientX - vr.left, y: e.clientY - vr.top};
        var mr = _mkRect(_svMarquee.start, _svMarquee.cur);
        _setBoxRect(_svMarquee.box, mr);
        var map = {};
        var rr = {
          left: (mr.left - _svPanX) / _svZoom,
          top: (mr.top - _svPanY) / _svZoom,
          right: (mr.right - _svPanX) / _svZoom,
          bottom: (mr.bottom - _svPanY) / _svZoom
        };
        var svEl2 = document.getElementById('cg-script-view');
        if (svEl2) {
          svEl2.querySelectorAll('.cg-file-card').forEach(function(card){
            var cr = {
              left: parseFloat(card.style.left) || 0,
              top: parseFloat(card.style.top) || 0,
              right: (parseFloat(card.style.left) || 0) + (card.offsetWidth || 0),
              bottom: (parseFloat(card.style.top) || 0) + (card.offsetHeight || 0)
            };
            if (_rectIntersects(rr, cr)) map[card.getAttribute('data-fp') || ''] = true;
          });
        }
        _svSetMultiSel(map);
      } else if (_svViewDrag) {
        _svPanX = _svViewDrag.origPanX + (e.clientX - _svViewDrag.startX);
        _svPanY = _svViewDrag.origPanY + (e.clientY - _svViewDrag.startY);
        _svApplyTransform();
      }
    });
    document.addEventListener('mouseup', function() {
      if (_svCardDrag) {
        var dx = 0, dy = 0;
        dx = (_svCardDrag.cards.length ? ((parseFloat(_svCardDrag.cards[0].card.style.left)||0) - _svCardDrag.cards[0].origL) : 0);
        dy = (_svCardDrag.cards.length ? ((parseFloat(_svCardDrag.cards[0].card.style.top)||0) - _svCardDrag.cards[0].origT) : 0);
        if (dx || dy) _svMoveAnnotsBy(dx, dy, _svCardDrag.selRectStart || {left:0,top:0,right:0,bottom:0});
      }
      _svCardDrag = null;
      if (_svMarquee) {
        if (_svMarquee.box && _svMarquee.box.parentNode) _svMarquee.box.parentNode.removeChild(_svMarquee.box);
        _svMarquee = null;
      }
      if (_svViewDrag) {
        _svViewDrag = null;
        var vp2 = document.getElementById('cg-sv-viewport');
        if (vp2) vp2.classList.remove('sv-panning');
      }
    });

    /* Draw edges + fit view after the browser has computed card layout */
    requestAnimationFrame(function() {
      _svDrawEdges();
      _svFitView();
    });
  }

  /* ── Script view helpers ───────────────────────────────────── */
  function _svApplyMultiSel() {
    var svEl = document.getElementById('cg-script-view');
    if (!svEl) return;
    svEl.querySelectorAll('.cg-file-card').forEach(function(card){
      var fp = card.getAttribute('data-fp');
      card.classList.toggle('sv-multi-selected', !!_svMultiSel[fp]);
    });
  }
  function _svSetMultiSel(map) {
    _svMultiSel = map || {};
    _svApplyMultiSel();
  }
  function _svMoveAnnotsBy(dx, dy, selectedRect) {
    try {
      var ann = _svAnnotsLoad();
      var changed = false;
      ann.forEach(function(a){
        var ar = {left:a.x, top:a.y, right:a.x+(a.w||0), bottom:a.y+(a.h||0)};
        if (_rectIntersects(ar, selectedRect)) {
          a.x += dx; a.y += dy; changed = true;
        }
      });
      if (changed) { _svAnnotsSave(ann); _svRenderAnnots(); }
    } catch(e) {}
  }

  function _svSelectFn(nid, scrollTo) {
    _clearEdgeHighlight(false);
    _svSelectedNid = nid;
    var svEl = document.getElementById('cg-script-view');
    if (!svEl) return;
    svEl.querySelectorAll('.cg-fn-row.sv-selected').forEach(function(el){ el.classList.remove('sv-selected'); });
    var row = svEl.querySelector('.cg-fn-row[data-nid="' + _cssEscape(nid) + '"]');
    if (row) {
      row.classList.add('sv-selected');
      if (scrollTo) _svScrollTo(nid);
    }
    openDetail(nid);
  }

  function _svSelectCard(fp) {
    _clearEdgeHighlight(false);
    var svEl = document.getElementById('cg-script-view');
    if (!svEl) return;
    svEl.querySelectorAll('.cg-file-card.sv-selected').forEach(function(el){ el.classList.remove('sv-selected'); });
    var card = svEl.querySelector('.cg-file-card[data-fp="' + _cssEscape(fp) + '"]');
    if (card) card.classList.add('sv-selected');
  }

  var _SV_COLLAPSE_KEY = 'cg_sv_collapsed_v1';
  function _svGetCollapsedSet() {
    try { var r = localStorage.getItem(_SV_COLLAPSE_KEY); return r ? JSON.parse(r) : {}; } catch(e) { return {}; }
  }
  function _svSaveCollapsedSet(set) {
    try { localStorage.setItem(_SV_COLLAPSE_KEY, JSON.stringify(set)); } catch(e) {}
  }
  window._svToggleCardCollapse = function(ev) {
    ev.stopPropagation();
    var btn = ev.currentTarget || ev.target;
    var card = btn.closest('.cg-file-card');
    if (!card) return;
    var collapsed = card.classList.toggle('sv-collapsed');
    btn.textContent = collapsed ? '▶' : '▼';
    var fp = card.dataset.fp || '';
    var set = _svGetCollapsedSet();
    if (collapsed) set[fp] = 1; else delete set[fp];
    _svSaveCollapsedSet(set);
  };
  function _svRestoreCollapsedState() {
    var set = _svGetCollapsedSet();
    var svEl = document.getElementById('cg-script-view');
    if (!svEl) return;
    svEl.querySelectorAll('.cg-file-card').forEach(function(card) {
      var fp = card.dataset.fp || '';
      if (set[fp]) {
        card.classList.add('sv-collapsed');
        var btn = card.querySelector('.cg-fc-collapse-btn');
        if (btn) btn.textContent = '▶';
      }
    });
  }

  function _svHighlight(nids) {
    _clearEdgeHighlight(false);
    var svEl = document.getElementById('cg-script-view');
    if (!svEl) return;
    var set = {};
    nids.forEach(function(id){ set[id] = true; });
    svEl.querySelectorAll('.cg-fn-row').forEach(function(row) {
      if (set[row.dataset.nid]) { row.classList.remove('sv-dim'); row.classList.add('sv-match'); }
      else { row.classList.add('sv-dim'); row.classList.remove('sv-match'); }
    });
  }

  function _svClearHighlight() {
    _clearEdgeHighlight(false);
    var svEl = document.getElementById('cg-script-view');
    if (!svEl) return;
    svEl.querySelectorAll('.cg-fn-row').forEach(function(row){
      row.classList.remove('sv-dim', 'sv-match', 'sv-selected');
    });
    svEl.querySelectorAll('.cg-file-card').forEach(function(card){
      card.classList.remove('sv-selected', 'sv-dim');
    });
  }

  function _svIsolate(visited) {
    _clearEdgeHighlight(false);
    var svEl = document.getElementById('cg-script-view');
    if (!svEl) return;
    svEl.querySelectorAll('.cg-fn-row').forEach(function(row) {
      if (visited[row.dataset.nid]) { row.classList.remove('sv-dim'); row.classList.add('sv-match'); }
      else { row.classList.add('sv-dim'); row.classList.remove('sv-match'); }
    });
    svEl.querySelectorAll('.cg-file-card').forEach(function(card) {
      var rows = card.querySelectorAll('.cg-fn-row');
      var allDim = true;
      rows.forEach(function(r){ if (!r.classList.contains('sv-dim')) allDim = false; });
      if (allDim) card.classList.add('sv-dim'); else card.classList.remove('sv-dim');
    });
  }

  function _svScrollTo(nid) {
    /* Pan the canvas to center the target function row in the viewport */
    var vp = document.getElementById('cg-sv-viewport');
    var svEl = document.getElementById('cg-script-view');
    if (!vp || !svEl) return;
    var row = svEl.querySelector('.cg-fn-row[data-nid="' + _cssEscape(nid) + '"]');
    if (!row) return;
    var card = row.closest('.cg-file-card');
    if (!card) return;
    var cardL = parseFloat(card.style.left) || 0;
    var cardT = parseFloat(card.style.top)  || 0;
    var rowCX = cardL + (parseFloat(card.style.width) || 290) / 2;
    var rowCY = cardT + row.offsetTop + row.offsetHeight / 2;
    _svPanX = vp.offsetWidth  / 2 - rowCX * _svZoom;
    _svPanY = vp.offsetHeight / 2 - rowCY * _svZoom;
    _svApplyTransform();
  }

  /* ── Variable Flow Mode ─────────────────────────────────────── */
  var _vfPanX = 0, _vfPanY = 0, _vfZoom = 1.0;
  var _vfViewDrag = null;
  var _vfCurrentVar = null;
  var _vfDeadMode = false;
  var _vfAnnotMode = false;
  var _vfAnnotDrag = null;       /* active annotation drag state */
  var _vfAnnotResize = null;     /* active annotation resize state */
  var _vfAnnotDrawing = null;    /* annotation being drawn */
  var _vfCtxTargetId = null;     /* node id for right-click context menu */
  var _VF_NOTES_KEY  = GRAPH_ID + ':cg_vf_notes_v1';
  var _VF_ANNOTS_KEY = GRAPH_ID + ':cg_vf_annots_v1';

  function _vfApplyTransform() {
    var c = document.getElementById('cg-vf-canvas');
    if (c) c.style.transform = 'translate('+_vfPanX+'px,'+_vfPanY+'px) scale('+_vfZoom+')';
  }
  function _vfApplyMultiSel() {
    var c = document.getElementById('cg-vf-canvas');
    if (!c) return;
    c.querySelectorAll('.cg-vf-node').forEach(function(el){
      el.classList.toggle('vf-multi-selected', !!_vfMultiSel[el.id]);
    });
  }
  function _vfSetMultiSel(map) {
    _vfMultiSel = map || {};
    _vfApplyMultiSel();
  }
  function _vfMoveAnnotsBy(dx, dy, selectedRect) {
    try {
      var ann = _vfAnnotsLoad();
      var changed = false;
      ann.forEach(function(a){
        var ar = {left:a.x, top:a.y, right:a.x+(a.w||0), bottom:a.y+(a.h||0)};
        if (_rectIntersects(ar, selectedRect)) {
          a.x += dx; a.y += dy; changed = true;
        }
      });
      if (changed) { _vfAnnotsSave(ann); _vfRenderAnnots(); }
    } catch(e) {}
  }

  function _vfCategoryLabel(cat) {
    var m = {local:'Local',global:'Global',static:'Static',argument:'Argument',
             return:'Return',member:'Member',const:'Const',env:'Env',heap:'Heap'};
    return m[cat] || cat;
  }

  function _vfActionLabel(action) {
    var m = {declare:'Declared',assign:'Assigned',argument:'Param',field:'Field',
             constant:'Const',global:'Global',static:'Static',env:'Env Read',heap:'Heap Alloc',
             member_access:'Member Access'};
    return m[action] || action;
  }

  function _vfSourceKindExtra(sk) {
    if (sk === 'memory initialization') return 'memset init';
    if (sk === 'memory copy')           return 'memcpy dest';
    if (sk === 'memory copy source')    return 'memcpy src';
    return null;
  }

  function _vfActionDescription(occ) {
    var name = occ.name, fn = occ.function_name;
    var sk = occ.source_kind || '';
    if (sk === 'memory initialization')
      return '"'+name+'" is zero/value-initialized via memset in '+fn+'.';
    if (sk === 'memory copy')
      return '"'+name+'" receives data via memcpy in '+fn+'. Source: '+esc(occ.snippet||'?');
    if (sk === 'memory copy source')
      return '"'+name+'" is read as the source of a memcpy in '+fn+'. Destination: '+esc(occ.snippet||'?');
    if (sk === 'input_file_connect')
      return '"'+name+'" is connected to input path "'+esc(occ.connect_path||occ.snippet||'?')+'" in '+fn+'.';
    var descs = {
      assign:   '"'+name+'" is assigned a value in '+fn+'.',
      declare:  '"'+name+'" is declared (uninitialized) in '+fn+'.',
      argument: '"'+name+'" is received as a function argument by '+fn+'.',
      field:    '"'+name+'" is a class or struct member field used in '+fn+'.',
      constant: '"'+name+'" is used as a constant value in '+fn+'.',
      global:   '"'+name+'" is a global variable referenced inside '+fn+'.',
      static:   '"'+name+'" is a static (persistent) variable in '+fn+'.',
      env:      '"'+name+'" is read from the runtime environment in '+fn+'.',
      heap:     '"'+name+'" is dynamically allocated on the heap in '+fn+'.'
    };
    return descs[occ.action] || '"'+name+'" appears in '+fn+'.';
  }

  function _vfSearch() {
    var inp = document.getElementById('cg-vf-search-input');
    var dd  = document.getElementById('cg-vf-dropdown');
    if (!inp || !dd) return;
    var q = inp.value.trim().toLowerCase();
    var keys = _VF_KEYS;
    var matches;
    if (!q) {
      matches = keys.slice().sort(function(a,b){ return a.localeCompare(b); });
    } else {
      matches = keys.filter(function(k){ return k.indexOf(q) !== -1; });
      matches.sort(function(a,b){
        var ai=a.indexOf(q), bi=b.indexOf(q);
        return ai!==bi ? ai-bi : a.localeCompare(b);
      });
    }
    matches = matches.slice(0, 60);
    if (!matches.length) { dd.style.display='none'; return; }
    dd.innerHTML = matches.map(function(k){
      var occs = VAR_FLOW_DATA[k];
      var displayName = occs[0].name;
      var fileSet = {};
      occs.forEach(function(o){ if(o.file_name) fileSet[o.file_name]=1; });
      var fileList = Object.keys(fileSet).slice(0,3).join(', ');
      var hi;
      if (!q) {
        hi = esc(displayName);
      } else {
        var idx = displayName.toLowerCase().indexOf(q);
        hi = idx >= 0
          ? esc(displayName.slice(0,idx))+'<span class="cg-vf-dd-mark">'+esc(displayName.slice(idx,idx+q.length))+'</span>'+esc(displayName.slice(idx+q.length))
          : esc(displayName);
      }
      var cnt = occs.length;
      return '<div class="cg-vf-dd-item" data-key="'+esc(k)+'">'
           + '<span class="cg-vf-dd-name">'+hi+'</span>'
           + '<span class="cg-vf-dd-count" title="'+esc(fileList)+'">'+cnt+' loc'+(cnt===1?'':'s')+'</span>'
           + (fileList?'<span style="font-size:10px;color:#6a7a8a;display:block;margin-top:1px">'+esc(fileList)+'</span>':'')
           + '</div>';
    }).join('');
    dd.querySelectorAll('.cg-vf-dd-item').forEach(function(item){
      item.addEventListener('mousedown', function(e){
        e.preventDefault();
        var k = item.dataset.key;
        var inp2 = document.getElementById('cg-vf-search-input');
        if (inp2) inp2.value = VAR_FLOW_DATA[k][0].name;
        dd.style.display = 'none';
        _vfSelectVar(k);
      });
    });
    dd.style.display = 'block';
  }

  /* Strip address-of / deref / cast / array index to get the base variable name */
  function _extractBaseVarName(expr) {
    if (!expr) return '';
    var s = expr.trim();
    s = s.replace(/^\\([^)]+\\)\\s*/, '');   /* strip cast */
    s = s.replace(/^[&*]+/, '');             /* strip & * */
    s = s.replace(/\\[.*$/, '');             /* strip [idx] */
    s = s.replace(/^[&*]+/, '');          /* strip again after cast removal */
    s = s.replace(/\\.\\w+$/, '');        /* strip trailing .field access */
    return s.trim();
  }

  /*
   * _vfBuildFlowChain(normKey)
   * BFS from all occurrences of normKey, following argument→parameter mappings
   * through EDGE_DATA call sites to discover aliased parameter names in callees.
   * Returns { entries: [{occ, localName, origName}], flowEdges: [{fromFnId, fromVar, toFnId, toVar, edgeRef}] }
   */
  function _vfBuildFlowChain(normKey) {
    /* Build param-name lookup: nodeId → [param0, param1, ...] */
    var nodeParamNames = {};
    NODE_DATA.forEach(function(nd) {
      if (nd.meta && nd.meta.parameters) {
        nodeParamNames[nd.id] = nd.meta.parameters.map(function(p){ return p.name||''; });
      }
    });

    var entries = [];
    var flowEdges = [];
    var visited = {};          /* key: fnId+'::'+varNameLower */

    function addOccs(fnId, varName, origName) {
      var key = fnId + '::' + varName.toLowerCase();
      if (visited[key]) return;
      visited[key] = true;
      var occs2 = VAR_FLOW_DATA[varName.toLowerCase()] || [];
      occs2.forEach(function(occ) {
        if (occ.function_id === fnId) {
          entries.push({ occ: occ, localName: varName, origName: origName });
        }
      });
    }

    /* Seed from all functions where normKey already appears.
     * No synthetic input_source entries exist any more — `.Connect(...)` produces exactly
     * one block (the receiver), parented by the LUGASI block when present via the intra-fn
     * sort_priority 0 → 1 chain edge in _vfBuildGraph. */
    var rootOccs = VAR_FLOW_DATA[normKey] || [];
    var rootFns = {};
    rootOccs.forEach(function(occ) {
      if (!rootFns[occ.function_id]) {
        rootFns[occ.function_id] = true;
        addOccs(occ.function_id, normKey, normKey);
      }
    });

    /* BFS queue — one entry per function in rootFns */
    var queue = Object.keys(rootFns).map(function(fnId) {
      return { fnId: fnId, varName: normKey, origName: normKey };
    });

    /* Cross-function LUGASI → .Connect link via source-name STRING match.
     * The LUGASI quoted source argument is stored as connect_input_name on
     * custom_input occurrences; the .Connect input name (last `/`-segment of
     * the path) is stored as connect_input_name on input_file_connect
     * occurrences. When both strings match (case-insensitive), draw a chain
     * edge from the LUGASI block to the .Connect receiver block and seed
     * the receiver's variable into the BFS so downstream argument-rename
     * tracking continues from there. Variable names DO NOT need to match
     * (e.g. LUGASI writes `x`, .Connect receiver is `YOYO`); the linking
     * key is purely the source/input string. */
    rootOccs.forEach(function(occ){
      if (occ.source_kind !== 'custom_input') return;
      var key = String(occ.connect_input_name || '').toLowerCase().trim();
      if (!key) return;
      (CONNECT_INDEX[key] || []).forEach(function(co){
        var recvKey = String(co.name || '').toLowerCase();
        var visKey  = co.function_id + '::' + recvKey;
        if (!visited[visKey]) {
          addOccs(co.function_id, co.name, co.name);
          queue.push({ fnId: co.function_id, varName: co.name, origName: co.name });
        }
        var alreadyEdge = flowEdges.some(function(fe){
          return fe.fromFnId === occ.function_id && fe.toFnId === co.function_id
              && (fe.fromVar||'').toLowerCase() === (occ.name||'').toLowerCase()
              && (fe.toVar  ||'').toLowerCase() === recvKey;
        });
        if (!alreadyEdge) {
          flowEdges.push({
            fromFnId: occ.function_id, fromVar: occ.name,
            toFnId:   co.function_id,  toVar:   co.name,
            toSourceKind: 'input_file_connect',
            sortPri:  1,
            edgeRef:  { synthetic: true, link_kind: 'lugasi_to_connect' }
          });
        }
      });
    });
    var qi = 0;
    while (qi < queue.length) {
      var item = queue[qi++];
      var fnId = item.fnId, varName = item.varName, origName = item.origName;
      var vlow = varName.toLowerCase();

      EDGE_DATA.forEach(function(e) {
        if (e.from !== fnId || !e.args || !e.args.length) return;
        for (var j = 0; j < e.args.length; j++) {
          var base = _extractBaseVarName(e.args[j]);
          if (base.toLowerCase() !== vlow) continue;
          /* varName is the j-th argument — find what the callee names it */
          var calleeParms = nodeParamNames[e.to];
          if (calleeParms && j < calleeParms.length && calleeParms[j]) {
            var paramName = calleeParms[j];
            var toKey = e.to + '::' + paramName.toLowerCase();
            var edgeAlreadyExists = flowEdges.some(function(fe) {
              return fe.fromFnId===fnId && fe.toFnId===e.to &&
                     fe.fromVar.toLowerCase()===vlow &&
                     fe.toVar.toLowerCase()===paramName.toLowerCase();
            });
            if (!edgeAlreadyExists) {
              flowEdges.push({ fromFnId: fnId, fromVar: varName,
                               toFnId: e.to,   toVar: paramName, edgeRef: e });
            }
            if (!visited[toKey]) {
              addOccs(e.to, paramName, origName);
              queue.push({ fnId: e.to, varName: paramName, origName: origName });
            }
          } else if (!calleeParms) {
            /* No param info — fall back to same-name tracking */
            var toKey2 = e.to + '::' + vlow;
            if (!visited[toKey2]) {
              addOccs(e.to, varName, origName);
              flowEdges.push({ fromFnId: fnId, fromVar: varName,
                               toFnId: e.to,   toVar: varName, edgeRef: e });
              queue.push({ fnId: e.to, varName: varName, origName: origName });
            }
          }
          break;  /* found our variable at position j; one match per call is enough */
        }
      });
    }

    return { entries: entries, flowEdges: flowEdges };
  }

  function _vfSelectVar(normKey) {
    _vfCurrentVar = normKey;
    /* Restore saved layout for this variable (or start with empty overrides) */
    _vfNodeOverrides = {};
    try { var _vfRaw = localStorage.getItem(VF_LAYOUT_PFX + normKey); if (_vfRaw) _vfNodeOverrides = JSON.parse(_vfRaw); } catch(e) {}
    _vfSelectedEdgeIdx = null;
    var popup = document.getElementById('cg-edge-popup');
    if (popup) popup.style.display = 'none';
    if (!VAR_FLOW_DATA[normKey] || !VAR_FLOW_DATA[normKey].length) return;
    var ph = document.getElementById('cg-vf-placeholder');
    if (ph) ph.style.display = 'none';
    var chain = _vfBuildFlowChain(normKey);
    _vfCurrentChainEdges = chain.flowEdges;
    _vfBuildGraph(chain.entries, chain.flowEdges);
  }

  function _vfBuildGraph(entries, chainEdges) {
    var canvas = document.getElementById('cg-vf-canvas');
    if (!canvas) return;
    chainEdges = chainEdges || [];
    /* Reset branch highlight (VF-2) — node DOM is rebuilt below. */
    _vfBranchActive = false; _vfBranchOriginId = null;
    _vfBranchNodeColor = {}; _vfBranchMerge = {};
    var _lg = document.getElementById('cg-vf-legend');
    if (_lg) _lg.style.display = 'none';

    /* 1. Build function-level DAG from chainEdges; fall back to EDGE_DATA when empty */
    var fnSet = {};
    entries.forEach(function(en) { fnSet[en.occ.function_id] = true; });
    var fnEdgesOut = {}, fnEdgesIn = {};
    Object.keys(fnSet).forEach(function(fn) { fnEdgesOut[fn]=[]; fnEdgesIn[fn]=[]; });

    if (chainEdges.length) {
      chainEdges.forEach(function(ce) {
        if (fnSet[ce.fromFnId] && fnSet[ce.toFnId] && ce.fromFnId !== ce.toFnId) {
          if (fnEdgesOut[ce.fromFnId].indexOf(ce.toFnId) < 0) fnEdgesOut[ce.fromFnId].push(ce.toFnId);
          if (fnEdgesIn[ce.toFnId].indexOf(ce.fromFnId) < 0) fnEdgesIn[ce.toFnId].push(ce.fromFnId);
        }
      });
    } else {
      EDGE_DATA.forEach(function(e) {
        if (fnSet[e.from] && fnSet[e.to] && e.from !== e.to) {
          if (fnEdgesOut[e.from].indexOf(e.to) < 0) fnEdgesOut[e.from].push(e.to);
          if (fnEdgesIn[e.to].indexOf(e.from) < 0)  fnEdgesIn[e.to].push(e.from);
        }
      });
    }

    /* 2. Assign columns via longest-path relaxation */
    var fnCol = {};
    var fnList = Object.keys(fnSet);
    fnList.forEach(function(fn) { fnCol[fn] = 0; });
    for (var iter = 0; iter <= fnList.length; iter++) {
      var changed = false;
      fnList.forEach(function(fn) {
        fnEdgesOut[fn].forEach(function(to) {
          if (fnCol[to] < fnCol[fn] + 1) { fnCol[to] = fnCol[fn] + 1; changed = true; }
        });
      });
      if (!changed) break;
    }

    /* 3. Group functions by column */
    var colFns = {};
    var maxCol = 0;
    fnList.forEach(function(fn) {
      var col = fnCol[fn];
      maxCol = Math.max(maxCol, col);
      if (!colFns[col]) colFns[col] = [];
      colFns[col].push(fn);
    });

    /* 4. Assign rows — order by mean-caller-row to reduce crossings.
     *    For source-column (no incoming edges) ext nodes, sort by sortPri first so
     *    custom_input sources (pri=0) always appear above connect sources (pri=1). */
    var fnSortPri = {};
    chainEdges.forEach(function(ce) {
      if (ce.sortPri !== undefined) fnSortPri[ce.fromFnId] = Math.min(
        fnSortPri[ce.fromFnId] !== undefined ? fnSortPri[ce.fromFnId] : 99, ce.sortPri);
    });
    var fnRow = {};
    for (var col = 0; col <= maxCol; col++) {
      var fns = colFns[col] || [];
      fns.sort(function(a, b) {
        var aC = fnEdgesIn[a], bC = fnEdgesIn[b];
        /* Source nodes (no callers): sort by known priority, then mean-caller-row, then name */
        if (!aC.length && !bC.length) {
          var aPri = (fnSortPri[a] !== undefined ? fnSortPri[a] : 99);
          var bPri = (fnSortPri[b] !== undefined ? fnSortPri[b] : 99);
          if (aPri !== bPri) return aPri - bPri;
        }
        var aR = aC.length ? aC.reduce(function(s,c){ return s+(fnRow[c]||0); },0)/aC.length : 0;
        var bR = bC.length ? bC.reduce(function(s,c){ return s+(fnRow[c]||0); },0)/bC.length : 0;
        return aR - bR || a.localeCompare(b);
      });
      fns.forEach(function(fn, i) { fnRow[fn] = i; });
    }

    /* 5. Group entries per function, sorted by priority then line
     *    Priority: 0=custom_input (highest), 1=connect, 2=normal */
    var fnEntries = {};
    entries.forEach(function(en, idx) {
      var fid = en.occ.function_id;
      if (!fnEntries[fid]) fnEntries[fid] = [];
      fnEntries[fid].push({en: en, idx: idx});
    });
    Object.keys(fnEntries).forEach(function(fid) {
      fnEntries[fid].sort(function(a,b){
        var pa = (a.en.occ.sort_priority !== undefined ? a.en.occ.sort_priority : 2);
        var pb = (b.en.occ.sort_priority !== undefined ? b.en.occ.sort_priority : 2);
        if (pa !== pb) return pa - pb;
        return (a.en.occ.line||0)-(b.en.occ.line||0);
      });
    });

    /* 6. Layout constants */
    var NODE_W=260, NODE_H_EST=156, INTRA_GAP=16, COL_GAP=200, ROW_GAP=96, PAD_X=80, PAD_Y=80;

    var colX = {};
    var cx = PAD_X;
    for (var c = 0; c <= maxCol; c++) { colX[c] = cx; cx += NODE_W + COL_GAP; }

    var rowStackH = {};
    Object.keys(fnEntries).forEach(function(fid) {
      var row = fnRow[fid] || 0;
      var h = fnEntries[fid].length * (NODE_H_EST + INTRA_GAP) - INTRA_GAP;
      rowStackH[row] = Math.max(rowStackH[row] || 0, h);
    });
    var maxRow = 0;
    fnList.forEach(function(fn) { maxRow = Math.max(maxRow, fnRow[fn]||0); });
    var rowY = {};
    var ry = PAD_Y;
    for (var r = 0; r <= maxRow; r++) {
      rowY[r] = ry;
      ry += (rowStackH[r] || NODE_H_EST) + ROW_GAP;
    }

    /* 7. Build final node list */
    var nodes = [];
    Object.keys(fnEntries).forEach(function(fid) {
      var col = fnCol[fid] || 0, row = fnRow[fid] || 0;
      var bx = colX[col], by = rowY[row];
      fnEntries[fid].forEach(function(item, intraIdx) {
        var ov = _vfNodeOverrides['vfn_'+item.idx];
        var nx = ov ? ov.x : bx;
        var ny = ov ? ov.y : (by + intraIdx * (NODE_H_EST + INTRA_GAP));
        nodes.push({id:'vfn_'+item.idx, idx:item.idx, en:item.en, occ:item.en.occ,
                    x:nx, y:ny, w:NODE_W, fnId:fid, col:col, row:row});
      });
    });
    _vfCurrentNodes = nodes;

    /* 8. Render blocks */
    canvas.innerHTML = '<svg id="cg-vf-svg" width="1" height="1" '
      +'style="position:absolute;top:0;left:0;overflow:visible;pointer-events:none"></svg>';

    /* Re-render persistent annotation rects (before nodes so they are below) */
    _vfRenderAnnots();
    _renderAllPins(canvas, 'vf', _vfDeadMode ? '__dead__' : (_vfCurrentVar || '__global__'));

    nodes.forEach(function(nd) {
      var en = nd.en, occ = nd.occ;
      var localName = en.localName;
      var origName  = en.origName;
      var showOrig  = localName.toLowerCase() !== origName.toLowerCase();
      var cat     = occ.category || 'local';
      var catLbl  = _vfCategoryLabel(cat);
      var actLbl  = _vfActionLabel(occ.action);
      var typeTxt = occ.data_type || occ.type_hint || 'unknown';
      var ln = esc(occ.file_name||'') + (occ.line ? ':'+occ.line : '');
      var varDisplay = esc(localName)
        + (showOrig ? '<span class="cg-vf-var-orig">(orig: '+esc(origName)+')</span>' : '');
      var varTitle = showOrig ? localName+' (originally: '+origName+')' : localName;
      var isDead = !!(occ.is_dead);
      var sk = occ.source_kind || '';
      var isConnect = (sk === 'input_file_connect');
      var isCustomInput = (sk === 'custom_input');
      var isMemberAccess = (sk === 'member_access');
      var skExtra = _vfSourceKindExtra(sk);

      /* stamp localName/origName onto occ so modal can access them */
      occ._localName = localName;
      occ._origName  = origName;

      /* Note indicator */
      var noteKey = _vfNoteKey(occ);
      var noteText = _vfGetNote(noteKey);

      var deadBadge = isDead
        ? '<span class="cg-vf-dead-badge" title="'+(occ.dead_reason||'unused')+'">'+
          (occ.action==='argument'?'Unused Param':'Dead Var')+'</span>' : '';
      /* ".connect" / "connect2" badge for connect-family receiver blocks */
      var connectBadge = isConnect
        ? '<span class="cg-vf-connect-badge">'
            + (occ.custom_input_func ? esc(occ.custom_input_func) : '.connect')
            + '</span>'
        : '';
      var memBadge = skExtra
        ? '<span class="cg-vf-memop-badge">'+esc(skExtra)+'</span>' : '';
      /* Classifier badge: WOW_NA / WOW_LI shown on LUGASI/LUGASIAN receiver blocks */
      var classifierBadge = isCustomInput && occ.custom_input_classifier
        ? '<span class="cg-vf-classifier-badge" title="Source classifier: '+esc(occ.custom_input_classifier)+'">'+esc(occ.custom_input_classifier)+'</span>'
        : '';
      var noteDot = noteText
        ? '<div class="cg-vf-note-dot" title="Note: '+esc(noteText)+'" data-notekey="'+esc(noteKey)+'">✎</div>' : '';
      var connectRow = isConnect && occ.connect_path
        ? '<div class="cg-vf-info-row"><span class="cg-vf-info-label">Path</span>'
          +'<span class="cg-vf-info-val" style="font-size:10px;color:#4fc3f7" title="'+esc(occ.connect_path)+'">'+esc(occ.connect_path)+'</span></div>'
        : '';
      var inputNameRow = isConnect && occ.connect_input_name
        ? '<div class="cg-vf-info-row"><span class="cg-vf-info-label">Input</span>'
          +'<span class="cg-vf-info-val cg-vf-var-name">'+esc(occ.connect_input_name)+'</span></div>'
        : (isCustomInput && occ.connect_input_name
          ? '<div class="cg-vf-info-row"><span class="cg-vf-info-label">Source</span>'
            +'<span class="cg-vf-info-val cg-vf-var-name">'+esc(occ.connect_input_name)+'</span></div>'
            +(occ.custom_input_func ? '<div class="cg-vf-info-row"><span class="cg-vf-info-label">Func</span>'
            +'<span class="cg-vf-info-val" style="color:#4ec980">'+esc(occ.custom_input_func)+'</span></div>' : '')
          : '');

      /* member_access: show parent + access expression in the body */
      var memberRows = '';
      if (isMemberAccess) {
        if (occ.parent_name) {
          memberRows += '<div class="cg-vf-info-row"><span class="cg-vf-info-label">Parent</span>'
            + '<span class="cg-vf-info-val cg-vf-var-name" title="'+esc(occ.parent_name)+'">'+esc(occ.parent_name)+'</span></div>';
        }
        if (occ.snippet) {
          memberRows += '<div class="cg-vf-info-row"><span class="cg-vf-info-label">Access</span>'
            + '<span class="cg-vf-info-val" style="font-size:10px;color:#4fc3f7" title="'+esc(occ.snippet)+'">'+esc(occ.snippet)+'</span></div>';
        }
      }

      var el = document.createElement('div');
      el.className = 'cg-vf-node'
        + (isDead ? ' cg-vf-dead' : '')
        + (isConnect ? ' cg-vf-connect-input' : '')
        + (isCustomInput ? ' cg-vf-custom-input' : '')
        + (isMemberAccess ? ' cg-vf-member-access' : '');
      el.id = nd.id;
      el.style.cssText = 'left:'+nd.x+'px;top:'+nd.y+'px;width:'+nd.w+'px;position:absolute';
      el.dataset.idx = nd.idx;
      el.dataset.notekey = noteKey;
      el.innerHTML =
        noteDot
        +'<div class="cg-vf-node-header">'
          +'<span class="cg-vf-cat-badge cg-vfc-'+esc(cat)+'">'+esc(catLbl)+'</span>'
          +'<span class="cg-vf-action-badge cg-vfa-'+esc(occ.action||'assign')+'">'+esc(actLbl)+'</span>'
          +deadBadge+connectBadge+classifierBadge+memBadge
        +'</div>'
        +'<div class="cg-vf-node-body">'
          +'<div class="cg-vf-var-row">'
            +'<span class="cg-vf-var-label">Var</span>'
            +'<span class="cg-vf-var-name" title="'+esc(varTitle)+'">'+varDisplay+'</span>'
          +'</div>'
          +'<div class="cg-vf-info-row">'
            +'<span class="cg-vf-info-label">Type</span>'
            +'<span class="cg-vf-info-val cg-vf-type-val">'+esc(typeTxt)+'</span>'
          +'</div>'
          +'<div class="cg-vf-info-row">'
            +'<span class="cg-vf-info-label">Func</span>'
            +'<span class="cg-vf-info-val cg-vf-fn-val" title="'+esc(occ.function_name)+'">'+esc(occ.function_name)+'</span>'
          +'</div>'
          +'<div class="cg-vf-info-row">'
            +'<span class="cg-vf-info-label">File</span>'
            +'<span class="cg-vf-info-val cg-vf-file-val">'+ln+'</span>'
          +'</div>'
          +connectRow+inputNameRow+memberRows
        +'</div>'
        +(occ.snippet ? '<div class="cg-vf-snippet" title="Double-click for details">'+esc(occ.snippet.trim())+'</div>' : '');

      el.addEventListener('click', function(e){ e.stopPropagation(); _vfNodeClick(nd.id); });
      el.addEventListener('dblclick', function(e){ e.stopPropagation(); _vfOpenModal(nd.occ); });
      el.addEventListener('mousedown', function(e){
        if (e.button !== 0) return;
        e.stopPropagation();
        var entries = [];
        if (_vfMultiSel[nd.id]) {
          _vfCurrentNodes.forEach(function(nx){
            if (_vfMultiSel[nx.id]) entries.push({id:nx.id, startNX:nx.x, startNY:nx.y});
          });
        }
        if (!entries.length) {
          _vfSetMultiSel((function(){ var m={}; m[nd.id]=true; return m; })());
          entries.push({id:nd.id, startNX:nd.x, startNY:nd.y});
        }
        var selRect = null;
        entries.forEach(function(it){
          var l = it.startNX, t = it.startNY, r = l + 300, b = t + 120;
          if (!selRect) selRect = {left:l, top:t, right:r, bottom:b};
          else {
            selRect.left = Math.min(selRect.left, l);
            selRect.top = Math.min(selRect.top, t);
            selRect.right = Math.max(selRect.right, r);
            selRect.bottom = Math.max(selRect.bottom, b);
          }
          var de = document.getElementById(it.id);
          if (de) de.classList.add('vf-dragging');
        });
        _vfNodeDrag = {nodes:entries, startMX:e.clientX, startMY:e.clientY, selRectStart:selRect};
      });
      el.addEventListener('contextmenu', function(e){
        e.preventDefault();
        e.stopPropagation();
        _vfShowCtxMenu(nd.id, occ, e.clientX, e.clientY);
      });
      canvas.appendChild(el);
    });

    /* 9. Build edges: chain (cross-function data flow) + EDGE_DATA fallback + intra-fn sequential */
    var edgeSeen = {};
    var edges = [];
    function pushEdge(from, to, type) {
      var k = from+'→'+to;
      if (edgeSeen[k]) return;
      edgeSeen[k] = true;
      edges.push({from: from, to: to, type: type});
    }

    /* Chain edges: pick the source/target blocks by NAME (ce.fromVar / ce.toVar)
     * first, then fall back to source_kind, then to last/first block in the group.
     *
     * Why this matters: when a single function contains several LUGASI calls
     * whose dest member-expressions share a parent (e.g. `IMU.x_acc`, `IMU.y_acc`,
     * `IMU.z_acc`), each LUGASI emits its own chainEdge with its own fromVar.
     * Without name matching here, every edge collapsed to fg[length-1] (always
     * the same block) and tg[0] (always the same connect), producing crossed
     * arrows where IMU.z_acc appeared to point at IMU.x_acc's .Connect. */
    function _pickByName(group, wantName) {
      if (!wantName) return null;
      var w = String(wantName).toLowerCase();
      for (var i = 0; i < group.length; i++) {
        var item = group[i];
        var n = (item.en && (item.en.localName || (item.en.occ && item.en.occ.name))) || '';
        if (String(n).toLowerCase() === w) return item;
      }
      return null;
    }
    /* Hard rules for block-to-block edges in Variable Flow mode:
     *   - A LUGASI (custom_input) block may emit edges ONLY to a .Connect
     *     (input_file_connect) block. It must never point at a regular
     *     variable/function block — the chain has to go through .Connect.
     *   - A .Connect block must never point at another .Connect block.
     * Returns true when the edge passes both rules. */
    function _vfAllowEdge(fromItem, toItem) {
      if (!fromItem || !toItem) return false;
      var skFrom = (fromItem.en && fromItem.en.occ && fromItem.en.occ.source_kind) || '';
      var skTo   = (toItem.en   && toItem.en.occ   && toItem.en.occ.source_kind)   || '';
      if (skFrom === 'custom_input' && skTo !== 'input_file_connect') return false;
      if (skFrom === 'input_file_connect' && skTo === 'input_file_connect') return false;
      return true;
    }
    chainEdges.forEach(function(ce) {
      var fg = fnEntries[ce.fromFnId], tg = fnEntries[ce.toFnId];
      if (!fg || !tg) return;
      // Source block — prefer name match, fall back to last in source function.
      var sourceItem = _pickByName(fg, ce.fromVar) || fg[fg.length-1];
      // Target block — prefer name match, then source_kind match, then first.
      var targetItem = _pickByName(tg, ce.toVar);
      if (!targetItem && ce.toSourceKind) {
        for (var ti = 0; ti < tg.length; ti++) {
          if (tg[ti].en.occ.source_kind === ce.toSourceKind) { targetItem = tg[ti]; break; }
        }
      }
      if (!targetItem) targetItem = tg[0];
      if (!_vfAllowEdge(sourceItem, targetItem)) return;
      pushEdge('vfn_'+sourceItem.idx, 'vfn_'+targetItem.idx, 'chain');
    });

    /* Fallback: EDGE_DATA between functions both in fnSet (covers same-name paths) */
    var fnToIdxs = {};
    entries.forEach(function(en, idx) {
      if (!fnToIdxs[en.occ.function_id]) fnToIdxs[en.occ.function_id] = [];
      fnToIdxs[en.occ.function_id].push(idx);
    });
    EDGE_DATA.forEach(function(e) {
      var fIdxs = fnToIdxs[e.from], tIdxs = fnToIdxs[e.to];
      if (!fIdxs || !tIdxs) return;
      fIdxs.forEach(function(fi){ tIdxs.forEach(function(ti){
        if (fi === ti) return;
        var fromItem = { en: entries[fi] };
        var toItem   = { en: entries[ti] };
        if (!_vfAllowEdge(fromItem, toItem)) return;
        pushEdge('vfn_'+fi, 'vfn_'+ti, 'call');
      }); });
    });

    /* Intra-function sequential edges: custom_input->input_file_connect gets solid chain arrow */
    Object.keys(fnEntries).forEach(function(fid) {
      var g = fnEntries[fid];
      for (var i = 0; i < g.length-1; i++) {
        if (!_vfAllowEdge(g[i], g[i+1])) continue;
        var skFrom = g[i].en.occ.source_kind || '';
        var skTo   = g[i+1].en.occ.source_kind || '';
        var etype  = (skFrom === 'custom_input' && skTo === 'input_file_connect') ? 'chain' : 'same';
        pushEdge('vfn_'+g[i].idx, 'vfn_'+g[i+1].idx, etype);
      }
    });

    _vfCurrentEdges = edges;

    /* 10. Fit view — leave comfortable margin, cap at 1.0 so blocks aren't too large */
    var maxX = 0, maxY = 0;
    nodes.forEach(function(nd){
      maxX = Math.max(maxX, nd.x+nd.w+PAD_X);
      maxY = Math.max(maxY, nd.y+NODE_H_EST+PAD_Y);
    });
    var area = document.getElementById('cg-vf-graph-area');
    if (area) {
      var aw = area.clientWidth||800, ah = area.clientHeight||600;
      var sc = Math.min((aw-40)/(maxX), (ah-40)/(maxY), 1.0);
      _vfZoom = Math.max(0.1, sc); _vfPanX = PAD_X; _vfPanY = PAD_Y;
      _vfApplyTransform();
    }
    setTimeout(function(){ _vfDrawEdges(edges, nodes); }, 90);
  }

  function _vfEdgeGeometry(e, nodeMap) {
    var fn = nodeMap[e.from], tn = nodeMap[e.to];
    if (!fn || !tn) return null;
    var fEl = document.getElementById(e.from), tEl = document.getElementById(e.to);
    var fh = fEl ? fEl.offsetHeight : 152, th = tEl ? tEl.offsetHeight : 152;
    var fx, fy, tx, ty;
    var sameCol = (fn.col === tn.col);
    if (sameCol) {
      fx = fn.x+fn.w/2; fy = fn.y+fh;
      tx = tn.x+tn.w/2; ty = tn.y;
    } else if (fn.col < tn.col) {
      fx = fn.x+fn.w; fy = fn.y+fh/2;
      tx = tn.x;      ty = tn.y+th/2;
    } else {
      fx = fn.x;      fy = fn.y+fh/2;
      tx = tn.x+tn.w; ty = tn.y+th/2;
    }
    var dx = sameCol ? 0 : Math.abs(tx-fx)*0.4+16;
    var dy = sameCol ? Math.abs(ty-fy)*0.4+16 : 0;
    var cp1x = sameCol ? fx          : fx + (fn.col < tn.col ? dx : -dx);
    var cp1y = sameCol ? fy + dy     : fy;
    var cp2x = sameCol ? tx          : tx - (fn.col < tn.col ? dx : -dx);
    var cp2y = sameCol ? ty - dy     : ty;
    if (_vfStraightLines) return 'M '+fx+' '+fy+' L '+tx+' '+ty;
    return 'M '+fx+' '+fy+' C '+cp1x+' '+cp1y+' '+cp2x+' '+cp2y+' '+tx+' '+ty;
  }

  function _vfDrawEdges(edges, nodes) {
    var svgEl = document.getElementById('cg-vf-svg');
    if (!svgEl) return;
    var nodeMap = {};
    nodes.forEach(function(n){ nodeMap[n.id]=n; });

    _vfEdgeMeta = [];
    var seen = {};
    var visPaths = '';
    var hitPaths = '';
    var eidxCounter = 0;
    /* Branch-highlight (VF-2): per-target-colour arrow markers, built on demand. */
    var branchColorMarkers = {};
    function _vfMarkerForColor(col) {
      if (branchColorMarkers[col]) return branchColorMarkers[col];
      var id = 'vf-arr-b' + Object.keys(branchColorMarkers).length;
      branchColorMarkers[col] = id;
      return id;
    }

    edges.forEach(function(e) {
      var k = e.from+'>'+e.to; if (seen[k]) return; seen[k] = true;
      var d = _vfEdgeGeometry(e, nodeMap);
      if (!d) return;

      var typeClass = e.type==='same' ? 'cg-vf-edge-same' : 'cg-vf-edge-call';
      var css = 'cg-vf-edge ' + typeClass;
      /* Highlight the single selected edge (yellow, matching Function-mode edge-click style) */
      if (_vfSelectedEdgeIdx !== null && eidxCounter === _vfSelectedEdgeIdx) {
        css += ' vf-edge-selected';
      }
      var mid = 'url(#vf-arr-' + (e.type==='same' ? 'same' : 'call') + ')';
      var styleAttr = '';
      /* When a branch highlight is active, colour edges by the branch of the
       * node they flow into; dim edges that are not part of the subgraph. */
      if (_vfBranchActive) {
        var tcol = _vfBranchNodeColor[e.to];
        var inSub = !!tcol && (e.from === _vfBranchOriginId || !!_vfBranchNodeColor[e.from]);
        if (inSub) {
          styleAttr = ' style="stroke:'+tcol+';opacity:1;stroke-width:2.5"';
          mid = 'url(#' + _vfMarkerForColor(tcol) + ')';
        } else {
          styleAttr = ' style="opacity:0.12"';
        }
      }
      var eidx = eidxCounter++;
      _vfEdgeMeta.push({fromId: e.from, toId: e.to, type: e.type});

      visPaths += '<path class="'+css+'" d="'+d+'"'+styleAttr+' marker-end="'+mid+'" pointer-events="none"/>';
      hitPaths += '<path class="cg-vf-hit" data-eidx="'+eidx
               +'" d="'+d+'" stroke="transparent" stroke-width="14"'
               +' fill="none" pointer-events="visibleStroke" style="cursor:pointer"/>';
    });

    /* Per-branch-colour arrow markers (VF-2). */
    var branchMarkerDefs = '';
    Object.keys(branchColorMarkers).forEach(function(col) {
      branchMarkerDefs += '<marker id="'+branchColorMarkers[col]+'" markerWidth="8" markerHeight="8" '
        +'refX="7" refY="4" orient="auto"><path d="M0,1 L7,4 L0,7 Z" fill="'+col+'"/></marker>';
    });

    svgEl.innerHTML =
      '<defs>'
      +'<marker id="vf-arr-call" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">'
      +'<path d="M0,1 L7,4 L0,7 Z" fill="#6c8ebf"/></marker>'
      +'<marker id="vf-arr-same" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">'
      +'<path d="M0,1 L7,4 L0,7 Z" fill="#6c8ebf"/></marker>'
      +branchMarkerDefs
      +'</defs>'
      + visPaths + hitPaths;

    /* Wire hit-area click listeners */
    svgEl.querySelectorAll('.cg-vf-hit').forEach(function(path) {
      var idx = parseInt(path.dataset.eidx, 10);
      path.addEventListener('click', function(evt) {
        evt.stopPropagation();
        _vfEdgeClick(idx, evt);
      });
    });
  }

  function _vfEdgeClick(metaIdx, evt) {
    var meta = _vfEdgeMeta[metaIdx];
    if (!meta) return;

    /* Highlight the selected edge (matches Function-mode yellow edge highlight) */
    _vfSelectedEdgeIdx = metaIdx;
    _vfDrawEdges(_vfCurrentEdges, _vfCurrentNodes);

    /* Show edge popup — same element used by Function mode */
    var popup = document.getElementById('cg-edge-popup');
    if (!popup) return;

    var fromNode = null, toNode = null;
    _vfCurrentNodes.forEach(function(n) {
      if (n.id === meta.fromId) fromNode = n;
      if (n.id === meta.toId)   toNode   = n;
    });

    var fromFn  = fromNode ? fromNode.occ.function_name : '?';
    var toFn    = toNode   ? toNode.occ.function_name   : '?';
    var fromVar = (fromNode && fromNode.en) ? (fromNode.en.localName || fromNode.occ.name) : '?';
    var toVar   = (toNode   && toNode.en)   ? (toNode.en.localName   || toNode.occ.name)   : '?';
    var origName = (fromNode && fromNode.en) ? fromNode.en.origName : fromVar;
    var aliased  = fromVar.toLowerCase() !== toVar.toLowerCase();
    var typeLabel = meta.type === 'same' ? 'intra-function sequence'
                  : meta.type === 'chain' ? 'data-flow (variable renamed)'
                  : 'function call';

    var h = '<button class="cg-edge-close" onclick="cgCloseVfEdgePopup()" title="Close">&#x2715;</button>';
    h += '<div class="cg-edge-title">' + esc(fromFn) + ' → ' + esc(toFn) + '</div>';
    h += '<div class="cg-edge-row"><span class="cg-edge-label">From</span>'
       + '<span class="cg-edge-value">' + esc(fromFn) + '</span></div>';
    h += '<div class="cg-edge-row"><span class="cg-edge-label">To</span>'
       + '<span class="cg-edge-value">' + esc(toFn) + '</span></div>';
    h += '<div class="cg-edge-row"><span class="cg-edge-label">Variable</span>'
       + '<span class="cg-edge-value"><code>' + esc(fromVar) + '</code>';
    if (aliased) {
      h += ' → <code>' + esc(toVar) + '</code>'
         + ' <span style="color:#8899aa;font-size:10px">(tracking: ' + esc(origName) + ')</span>';
    }
    h += '</span></div>';
    h += '<div class="cg-edge-row"><span class="cg-edge-label">Type</span>'
       + '<span class="cg-edge-value">' + esc(typeLabel) + '</span></div>';

    popup.innerHTML = h;
    popup.style.display = 'block';
    _positionEdgePopup(evt);
  }

  window.cgCloseVfEdgePopup = function() {
    var popup = document.getElementById('cg-edge-popup');
    if (popup) popup.style.display = 'none';
    _vfSelectedEdgeIdx = null;
    _vfDrawEdges(_vfCurrentEdges, _vfCurrentNodes);
  };

  function _vfNodeClick(nodeId) {
    var canvas = document.getElementById('cg-vf-canvas');
    if (!canvas) return;
    canvas.querySelectorAll('.cg-vf-node.vf-selected').forEach(function(el){ el.classList.remove('vf-selected'); });
    var el = document.getElementById(nodeId);
    if (el) el.classList.add('vf-selected');
    /* VF-2: clicking the current branch origin again clears the highlight;
     * clicking any other node lights up its downstream flow per-branch. */
    if (_vfBranchActive && _vfBranchOriginId === nodeId) {
      _vfClearBranchHighlight();
    } else {
      _vfApplyBranchHighlight(nodeId);
    }
  }

  /* ── Branch highlight engine (VF-2) ──────────────────────────
   * Colour every downstream path out of `originId`. Each immediate outgoing
   * branch gets a distinct base hue; when a branch splits again the child hues
   * are derived from the parent (rotated per sibling, shaded by depth) so the
   * lineage of a colour is readable. Nodes reachable by more than one branch
   * are flagged as merge points. Anything not downstream is dimmed. */
  function _vfHsl(h, s, l) {
    h = ((Math.round(h) % 360) + 360) % 360;
    return 'hsl(' + h + ',' + Math.round(s) + '%,' + Math.round(l) + '%)';
  }

  function _vfApplyBranchHighlight(originId) {
    if (!_vfCurrentNodes.length) return;

    /* Build downstream adjacency (from -> [to]) over the current edge set. */
    var out = {};
    _vfCurrentEdges.forEach(function(e) {
      if (!out[e.from]) out[e.from] = [];
      if (out[e.from].indexOf(e.to) < 0) out[e.from].push(e.to);
    });

    var children = out[originId] || [];
    if (!children.length) {
      /* Leaf node — nothing downstream to colour. Just select it. */
      _vfClearBranchHighlight();
      var le = document.getElementById(originId);
      if (le) le.classList.add('vf-selected');
      return;
    }

    var colorOf   = {};   /* nodeId -> hsl colour */
    var hueOf      = {};   /* nodeId -> numeric hue (for marker grouping) */
    var branchSet = {};   /* nodeId -> { branchIdx: true } for merge detection */
    var visited   = {};   /* nodeId -> true once coloured */
    var nBranch   = children.length;

    function _recordBranch(id, bIdx) {
      if (!branchSet[id]) branchSet[id] = {};
      branchSet[id][bIdx] = true;
    }
    function _lightness(depth) { return Math.max(40, 64 - depth * 4); }

    /* DFS stack seeded with one entry per immediate branch. */
    var stack = [];
    children.forEach(function(cid, i) {
      stack.push({ id: cid, hue: i * (360 / nBranch), depth: 1, branchIdx: i });
    });

    while (stack.length) {
      var cur = stack.pop();
      _recordBranch(cur.id, cur.branchIdx);
      if (visited[cur.id]) continue;   /* already coloured via another path → merge recorded */
      visited[cur.id] = true;
      hueOf[cur.id]   = cur.hue;
      colorOf[cur.id] = _vfHsl(cur.hue, 68, _lightness(cur.depth));

      var kids = (out[cur.id] || []).filter(function(k){ return k !== originId; });
      var k = kids.length;
      kids.forEach(function(kid, j) {
        /* Derive child hue: when this node splits, fan the children out around
         * the parent hue; the fan shrinks with depth so deeper layers stay near
         * their ancestor's colour. Single-child chains keep the same hue. */
        var childHue = (k > 1)
          ? cur.hue + (j - (k - 1) / 2) * (30 / cur.depth)
          : cur.hue;
        if (!visited[kid]) {
          stack.push({ id: kid, hue: childHue, depth: cur.depth + 1, branchIdx: cur.branchIdx });
        } else {
          _recordBranch(kid, cur.branchIdx);
        }
      });
    }

    /* Merge points: reached from more than one immediate branch. */
    var merge = {};
    Object.keys(branchSet).forEach(function(id) {
      if (Object.keys(branchSet[id]).length > 1) merge[id] = true;
    });

    _vfBranchActive    = true;
    _vfBranchOriginId  = originId;
    _vfBranchNodeColor = colorOf;
    _vfBranchMerge     = merge;

    /* Apply per-node styling. */
    _vfCurrentNodes.forEach(function(nd) {
      var el = document.getElementById(nd.id);
      if (!el) return;
      el.classList.remove('vf-selected', 'vf-dim', 'vf-merge');
      el.style.borderColor = '';
      el.style.boxShadow = '';
      if (nd.id === originId) {
        el.classList.add('vf-selected');
        return;
      }
      var c = colorOf[nd.id];
      if (c) {
        el.style.borderColor = c;
        el.style.boxShadow = '0 0 12px ' + c.replace('hsl', 'hsla').replace(')', ',0.45)');
        if (merge[nd.id]) el.classList.add('vf-merge');
      } else {
        el.classList.add('vf-dim');
      }
    });

    _vfRenderBranchLegend(children, nBranch);
    _vfDrawEdges(_vfCurrentEdges, _vfCurrentNodes);
  }

  function _vfClearBranchHighlight() {
    _vfBranchActive    = false;
    _vfBranchOriginId  = null;
    _vfBranchNodeColor = {};
    _vfBranchMerge     = {};
    var canvas = document.getElementById('cg-vf-canvas');
    if (canvas) {
      canvas.querySelectorAll('.cg-vf-node').forEach(function(el) {
        el.classList.remove('vf-dim', 'vf-merge');
        el.style.borderColor = '';
        el.style.boxShadow = '';
      });
    }
    var lg = document.getElementById('cg-vf-legend');
    if (lg) lg.style.display = 'none';
    _vfDrawEdges(_vfCurrentEdges, _vfCurrentNodes);
  }

  function _vfRenderBranchLegend(children, nBranch) {
    var area = document.getElementById('cg-vf-graph-area');
    if (!area) return;
    var lg = document.getElementById('cg-vf-legend');
    if (!lg) {
      lg = document.createElement('div');
      lg.id = 'cg-vf-legend';
      area.appendChild(lg);
    }
    var rows = '';
    children.forEach(function(cid, i) {
      var col = _vfHsl(i * (360 / nBranch), 68, 60);
      var nd = null;
      _vfCurrentNodes.forEach(function(n){ if (n.id === cid) nd = n; });
      var lbl = nd ? (nd.en.localName || (nd.occ && nd.occ.name) || 'branch') : 'branch';
      var fn = (nd && nd.occ) ? nd.occ.function_name : '';
      rows += '<div class="cg-vf-legend-row">'
            + '<span class="cg-vf-legend-swatch" style="background:' + col + '"></span>'
            + '<span title="' + esc(fn) + '">' + esc(lbl) + '</span></div>';
    });
    lg.innerHTML =
      '<div class="cg-vf-legend-title">Flow branches (' + nBranch + ')</div>'
      + rows
      + '<div class="cg-vf-legend-hint">Each colour is one downstream path. '
      + 'Dashed = merge point (shared by paths). Deeper splits shade the parent colour.</div>'
      + '<button class="cg-vf-legend-clear" type="button">Clear highlight</button>';
    var clr = lg.querySelector('.cg-vf-legend-clear');
    if (clr) clr.addEventListener('click', function(ev){ ev.stopPropagation(); _vfClearBranchHighlight(); });
    lg.style.display = 'block';
  }

  function _vfOpenModal(occ) {
    var modal = document.getElementById('cg-vf-modal');
    if (!modal) return;
    var cat     = occ.category || 'local';
    var catLbl  = _vfCategoryLabel(cat);
    var actLbl  = _vfActionLabel(occ.action);
    var typeTxt = occ.data_type || occ.type_hint || 'unknown';
    var ln      = (occ.file_name||'') + (occ.line ? ':'+occ.line : '');
    var actionDesc = _vfActionDescription(occ);
    var valueRow = occ.value
      ? '<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Source line</span><span class="cg-vf-modal-value mono">'+esc(occ.value)+'</span></div>'
      : '';
    var snippetSec = occ.snippet
      ? '<div class="cg-vf-modal-section"><div class="cg-vf-modal-section-title">Source code</div><div class="cg-vf-modal-code">'+esc(occ.snippet.trim())+'</div></div>'
      : '';
    var localName = occ._localName || occ.name;
    var origName  = occ._origName  || occ.name;
    var showOrig  = localName.toLowerCase() !== origName.toLowerCase();
    var titleEl = document.getElementById('cg-vf-modal-title');
    var bodyEl  = document.getElementById('cg-vf-modal-body');
    if (titleEl) titleEl.textContent = showOrig
      ? localName + ' — originally “' + origName + '”'
      : localName;
    var origRow = showOrig
      ? '<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Tracked as</span><span class="cg-vf-modal-value mono">'+esc(origName)+'</span></div>'
      : '';
    if (bodyEl) bodyEl.innerHTML =
      '<div class="cg-vf-modal-section">'
        +'<div class="cg-vf-modal-section-title">Identity</div>'
        +'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Category</span><span class="cg-vf-modal-value"><span class="cg-vf-cat-badge cg-vfc-'+esc(cat)+'">'+esc(catLbl)+'</span></span></div>'
        +'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Variable</span><span class="cg-vf-modal-value mono">'+esc(localName)+'</span></div>'
        +origRow
        +'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Type</span><span class="cg-vf-modal-value mono">'+esc(typeTxt)+'</span></div>'
      +'</div>'
      +'<div class="cg-vf-modal-section">'
        +'<div class="cg-vf-modal-section-title">Location</div>'
        +'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Function</span><span class="cg-vf-modal-value mono">'+esc(occ.function_name)+'</span></div>'
        +'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">File</span><span class="cg-vf-modal-value mono">'+esc(ln)+'</span></div>'
        +(occ.file_path?'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Full path</span><span class="cg-vf-modal-value" style="font-size:10px;color:#5a6a7a">'+esc(occ.file_path)+'</span></div>':'')
      +'</div>'
      +'<div class="cg-vf-modal-section">'
        +'<div class="cg-vf-modal-section-title">Action</div>'
        +'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Type</span><span class="cg-vf-modal-value"><span class="cg-vf-action-badge cg-vfa-'+esc(occ.action||'assign')+'">'+esc(actLbl)+'</span></span></div>'
        +'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Description</span><span class="cg-vf-modal-action-desc">'+esc(actionDesc)+'</span></div>'
        +valueRow
      +'</div>'
      +(occ.is_dead ? '<div class="cg-vf-modal-section" style="border-left:3px solid #e74c3c">'
        +'<div class="cg-vf-modal-section-title" style="color:#e74c3c">Dead / Unused Variable</div>'
        +'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Status</span><span class="cg-vf-modal-value" style="color:#e74c3c">⛔ '+esc(occ.dead_reason||'unused')+'</span></div>'
        +'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Note</span><span class="cg-vf-modal-action-desc">This variable is declared but never read in the detected code scope. Consider removing it if it is no longer needed.</span></div>'
        +'</div>' : '')
      +(occ.connect_path ? '<div class="cg-vf-modal-section" style="border-left:3px solid #4A90D9">'
        +'<div class="cg-vf-modal-section-title" style="color:#4A90D9">Input File Connection</div>'
        +'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Full path</span><span class="cg-vf-modal-value mono" style="color:#4A90D9">'+esc(occ.connect_path)+'</span></div>'
        +(occ.connect_input_name?'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Input var</span><span class="cg-vf-modal-value mono">'+esc(occ.connect_input_name)+'</span></div>':'')
        +'</div>' : '')
      +snippetSec;
    modal.classList.add('open');
  }

  function _vfCloseModal() {
    var modal = document.getElementById('cg-vf-modal');
    if (modal) modal.classList.remove('open');
  }

  /* ── VarFlow view-control helpers ──────────────────────────── */
  function _vfFitAll() {
    if (!_vfCurrentNodes.length) return;
    var PAD = 50;
    var mnX=1e9,mnY=1e9,mxX=-1e9,mxY=-1e9;
    _vfCurrentNodes.forEach(function(nd){
      var el=document.getElementById(nd.id);
      var h=el?el.offsetHeight:152;
      mnX=Math.min(mnX,nd.x); mnY=Math.min(mnY,nd.y);
      mxX=Math.max(mxX,nd.x+nd.w); mxY=Math.max(mxY,nd.y+h);
    });
    var area=document.getElementById('cg-vf-graph-area');
    if (!area) return;
    var aw=area.clientWidth||800, ah=area.clientHeight||600;
    var cw=mxX-mnX+PAD*2, ch=mxY-mnY+PAD*2;
    var sc=Math.min((aw-20)/cw,(ah-20)/ch,1.5);
    _vfZoom=Math.max(0.08,sc);
    _vfPanX=PAD-mnX*_vfZoom; _vfPanY=PAD-mnY*_vfZoom;
    _vfApplyTransform();
  }

  function _vfCenterOnFirst() {
    var nd = _vfCurrentNodes.length ? _vfCurrentNodes[0] : null;
    if (!nd) return;
    var area=document.getElementById('cg-vf-graph-area');
    if (!area) return;
    var aw=area.clientWidth||800, ah=area.clientHeight||600;
    var el=document.getElementById(nd.id);
    var h=el?el.offsetHeight:152;
    _vfPanX=aw/2-(nd.x+nd.w/2)*_vfZoom;
    _vfPanY=ah/2-(nd.y+h/2)*_vfZoom;
    _vfApplyTransform();
  }

  function _vfHighlightBlocks(normKey) {
    if (normKey && normKey !== _vfCurrentVar) {
      _vfSelectVar(normKey);
      var inp=document.getElementById('cg-vf-search-input');
      if (inp && VAR_FLOW_DATA[normKey]) inp.value=VAR_FLOW_DATA[normKey][0].name;
    }
    _vfClearBranchHighlight();
    var canvas=document.getElementById('cg-vf-canvas');
    if (!canvas) return;
    canvas.querySelectorAll('.cg-vf-node').forEach(function(el){
      el.classList.add('vf-selected'); el.classList.remove('vf-dim');
    });
  }

  function _vfClearHighlight() {
    _vfClearBranchHighlight();
    var canvas=document.getElementById('cg-vf-canvas');
    if (!canvas) return;
    canvas.querySelectorAll('.cg-vf-node').forEach(function(el){
      el.classList.remove('vf-selected','vf-dim');
    });
  }

  function _vfResetLayout() {
    _vfNodeOverrides   = {};
    try { localStorage.removeItem(VF_LAYOUT_PFX + (_vfCurrentVar||'')); } catch(e) {}
    _vfSelectedEdgeIdx = null;
    var popup = document.getElementById('cg-edge-popup');
    if (popup) popup.style.display = 'none';
    if (_vfCurrentVar && VAR_FLOW_DATA[_vfCurrentVar]) {
      var chain = _vfBuildFlowChain(_vfCurrentVar);
      _vfCurrentChainEdges = chain.flowEdges;
      _vfBuildGraph(chain.entries, chain.flowEdges);
    }
  }

  function _vfGetTargetVar() {
    var q=(searchInput?searchInput.value.trim().toLowerCase():'');
    if (q && VAR_FLOW_DATA[q]) return q;
    if (q) {
      var keys=_VF_KEYS;
      for (var i=0;i<keys.length;i++) { if (keys[i].indexOf(q)===0) return keys[i]; }
    }
    return _vfCurrentVar||null;
  }

  function _vfSidebarSearch(q) {
    var inp=document.getElementById('cg-search');
    var dd=document.getElementById('cg-search-dropdown');
    if (!inp||!dd) return;
    var norm=(q||'').toLowerCase().trim();
    var keys=_VF_KEYS;
    var matches;
    if (!norm) {
      matches=keys.slice().sort(function(a,b){return a.localeCompare(b);}).slice(0,40);
    } else {
      matches=keys.filter(function(k){return k.indexOf(norm)!==-1;});
      matches.sort(function(a,b){
        var ai=a.indexOf(norm),bi=b.indexOf(norm);
        return ai!==bi?ai-bi:a.localeCompare(b);
      });
      matches=matches.slice(0,40);
    }
    if (!matches.length){dd.style.display='none';return;}
    var rect=inp.getBoundingClientRect();
    dd.style.cssText='left:'+Math.round(rect.left)+'px;top:'+Math.round(rect.bottom+2)+'px;width:'+Math.round(rect.width)+'px;';
    dd.innerHTML=matches.map(function(k){
      var displayName=VAR_FLOW_DATA[k][0].name;
      var cnt=VAR_FLOW_DATA[k].length;
      var hi;
      if (!norm) {
        hi=esc(displayName);
      } else {
        var idx=displayName.toLowerCase().indexOf(norm);
        hi=idx>=0
          ?esc(displayName.slice(0,idx))+'<span class="cg-sd-mark">'+esc(displayName.slice(idx,idx+norm.length))+'</span>'+esc(displayName.slice(idx+norm.length))
          :esc(displayName);
      }
      return '<div class="cg-sd-item" data-vkey="'+esc(k)+'">'
        +'<div class="cg-sd-name">'+hi+'</div>'
        +'<div class="cg-sd-meta">'+cnt+' occurrence'+(cnt===1?'':'s')+'</div>'
        +'</div>';
    }).join('');
    dd.querySelectorAll('.cg-sd-item').forEach(function(item){
      item.addEventListener('mousedown',function(e){
        e.preventDefault();
        var vkey=item.dataset.vkey;
        dd.style.display='none';
        if (inp) inp.value=VAR_FLOW_DATA[vkey][0].name;
        var vfInp=document.getElementById('cg-vf-search-input');
        if (vfInp) vfInp.value=VAR_FLOW_DATA[vkey][0].name;
        if (searchHint) searchHint.textContent=VAR_FLOW_DATA[vkey].length+' occurrence'+(VAR_FLOW_DATA[vkey].length===1?'':'s');
        _vfSelectVar(vkey);
      });
    });
    dd.style.display='block';
  }

  function _vfRebuildEdges() {
    return _vfCurrentEdges;
  }

  /* Varflow pan/zoom — lazily wired when the view first becomes visible */
  (function(){
    function _wireVfPan(){
      var vp=document.getElementById('cg-vf-viewport');
      if(!vp||vp._vfWired) return;
      vp._vfWired=true;
      vp.addEventListener('mousedown',function(e){
        var isMid = e.button === 1;
        if(!isMid && (e.button!==0||e.target.closest('.cg-vf-node')||_vfAnnotMode)) return;
        e.preventDefault();
        if (e.altKey || isMid) {
          _vfViewDrag={x:e.clientX,y:e.clientY,px:_vfPanX,py:_vfPanY};
          vp.classList.add('vf-panning');
          return;
        }
        var r = vp.getBoundingClientRect();
        var p0 = {x:e.clientX-r.left, y:e.clientY-r.top};
        var box = document.createElement('div');
        box.className = 'cg-marquee-box';
        vp.appendChild(box);
        _vfMarquee = {vp:vp, box:box, start:p0, cur:p0};
        _setBoxRect(box, _mkRect(p0, p0));
      });
      document.addEventListener('mousemove',function(e){
        if (_vfNodeDrag) {
          var dx=(e.clientX-_vfNodeDrag.startMX)/_vfZoom;
          var dy=(e.clientY-_vfNodeDrag.startMY)/_vfZoom;
          _vfNodeDrag.nodes.forEach(function(it){
            var nx=it.startNX+dx, ny=it.startNY+dy;
            var el=document.getElementById(it.id);
            if(el){el.style.left=nx+'px';el.style.top=ny+'px';}
            _vfNodeOverrides[it.id]={x:nx,y:ny};
            _vfCurrentNodes.forEach(function(nd){if(nd.id===it.id){nd.x=nx;nd.y=ny;}});
          });
          _vfDrawEdges(_vfRebuildEdges(),_vfCurrentNodes);
          return;
        }
        if (_vfMarquee) {
          var r2 = _vfMarquee.vp.getBoundingClientRect();
          _vfMarquee.cur = {x:e.clientX-r2.left, y:e.clientY-r2.top};
          var mr = _mkRect(_vfMarquee.start, _vfMarquee.cur);
          _setBoxRect(_vfMarquee.box, mr);
          var rr = {
            left: (mr.left - _vfPanX) / _vfZoom,
            top: (mr.top - _vfPanY) / _vfZoom,
            right: (mr.right - _vfPanX) / _vfZoom,
            bottom: (mr.bottom - _vfPanY) / _vfZoom
          };
          var map = {};
          _vfCurrentNodes.forEach(function(nd){
            var er = document.getElementById(nd.id);
            var w = er ? er.offsetWidth : 300;
            var h = er ? er.offsetHeight : 120;
            var nr = {left:nd.x, top:nd.y, right:nd.x+w, bottom:nd.y+h};
            if (_rectIntersects(rr, nr)) map[nd.id] = true;
          });
          _vfSetMultiSel(map);
          return;
        }
        if(!_vfViewDrag) return;
        _vfPanX=_vfViewDrag.px+(e.clientX-_vfViewDrag.x);
        _vfPanY=_vfViewDrag.py+(e.clientY-_vfViewDrag.y);
        _vfApplyTransform();
      });
      document.addEventListener('mouseup',function(){
        if (_vfNodeDrag) {
          var dx = 0, dy = 0;
          if (_vfNodeDrag.nodes.length) {
            var f = _vfNodeDrag.nodes[0];
            var fel = document.getElementById(f.id);
            dx = (fel ? (parseFloat(fel.style.left)||0) : f.startNX) - f.startNX;
            dy = (fel ? (parseFloat(fel.style.top)||0) : f.startNY) - f.startNY;
          }
          _vfNodeDrag.nodes.forEach(function(it){
            var el=document.getElementById(it.id);
            if(el) el.classList.remove('vf-dragging');
          });
          if (dx || dy) _vfMoveAnnotsBy(dx, dy, _vfNodeDrag.selRectStart || {left:0,top:0,right:0,bottom:0});
          _vfNodeDrag=null;
          /* Redraw edges after block is placed */
          if(_vfCurrentNodes.length) { _vfSelectedEdgeIdx=null; _vfDrawEdges(_vfRebuildEdges(),_vfCurrentNodes); }
          return;
        }
        if (_vfMarquee) {
          if (_vfMarquee.box && _vfMarquee.box.parentNode) _vfMarquee.box.parentNode.removeChild(_vfMarquee.box);
          _vfMarquee = null;
          return;
        }
        if(!_vfViewDrag) return;
        _vfViewDrag=null; vp.classList.remove('vf-panning');
      });
      vp.addEventListener('wheel',function(e){
        e.preventDefault();
        var delta=e.deltaY>0?0.9:1.1;
        var rect=vp.getBoundingClientRect();
        var mx=e.clientX-rect.left,my=e.clientY-rect.top;
        _vfPanX=mx-_vfZoom*delta*((mx-_vfPanX)/_vfZoom);
        _vfPanY=my-_vfZoom*delta*((my-_vfPanY)/_vfZoom);
        _vfZoom=Math.max(0.1,Math.min(4,_vfZoom*delta));
        _vfApplyTransform();
      },{passive:false});
    }
    /* Wire immediately if DOM ready, otherwise on DOMContentLoaded */
    if(document.getElementById('cg-vf-viewport')) _wireVfPan();
    else document.addEventListener('DOMContentLoaded',_wireVfPan);
    /* Also wire when user first clicks Var Flow button */
    if(btnModeVf) btnModeVf.addEventListener('click',function(){ setTimeout(_wireVfPan,50); });
  })();

  /* Varflow search input wiring */
  (function(){
    var inp=document.getElementById('cg-vf-search-input');
    if(!inp) return;
    inp.addEventListener('focus', _vfSearch);
    inp.addEventListener('input', _vfSearch);
    inp.addEventListener('blur', function(){
      setTimeout(function(){
        var dd=document.getElementById('cg-vf-dropdown');
        if(dd) dd.style.display='none';
      }, 200);
    });
    inp.addEventListener('keydown',function(e){
      if(e.key==='Escape'){
        var dd=document.getElementById('cg-vf-dropdown');
        if(dd) dd.style.display='none'; inp.blur();
      }
      if(e.key==='Enter'){
        var k=inp.value.trim().toLowerCase();
        if(VAR_FLOW_DATA[k]){
          var dd2=document.getElementById('cg-vf-dropdown');
          if(dd2) dd2.style.display='none';
          _vfSelectVar(k);
        }
      }
    });
    var clr=document.getElementById('cg-vf-search-clear');
    if(clr) clr.addEventListener('click',function(){
      inp.value='';
      var dd=document.getElementById('cg-vf-dropdown'); if(dd) dd.style.display='none';
      _vfCurrentVar=null; _vfNodeOverrides={};
      var canvas=document.getElementById('cg-vf-canvas');
      if(canvas) canvas.innerHTML='<svg id="cg-vf-svg" width="1" height="1" style="position:absolute;top:0;left:0;overflow:visible;pointer-events:none"></svg>';
      var ph=document.getElementById('cg-vf-placeholder');
      if(ph) ph.style.display='';
    });
  })();

  /* ── Dead variable detection & report ──────────────────────── */

  function _vfDeadVarOccs() {
    var dead = [];
    _VF_KEYS.forEach(function(k) {
      VAR_FLOW_DATA[k].forEach(function(occ) {
        if (occ.is_dead) dead.push(occ);
      });
    });
    return dead;
  }

  window._vfToggleDeadMode = function() {
    _vfDeadMode = !_vfDeadMode;
    var btn = document.getElementById('cg-vf-dead-btn');
    if (btn) btn.classList.toggle('active', _vfDeadMode);
    if (_vfDeadMode) {
      _vfShowDeadVars();
    } else {
      /* Return to last selected variable or clear */
      var canvas = document.getElementById('cg-vf-canvas');
      if (canvas) canvas.innerHTML = '<svg id="cg-vf-svg" width="1" height="1" style="position:absolute;top:0;left:0;overflow:visible;pointer-events:none"></svg>';
      var ph = document.getElementById('cg-vf-placeholder');
      if (ph) ph.style.display = _vfCurrentVar ? 'none' : '';
      if (_vfCurrentVar && VAR_FLOW_DATA[_vfCurrentVar]) {
        var chain = _vfBuildFlowChain(_vfCurrentVar);
        _vfCurrentChainEdges = chain.flowEdges;
        _vfBuildGraph(chain.entries, chain.flowEdges);
      }
    }
  };

  function _vfShowDeadVars() {
    var occs = _vfDeadVarOccs();
    if (!occs.length) {
      var ph = document.getElementById('cg-vf-placeholder');
      if (ph) { ph.textContent = 'No dead variables detected.'; ph.style.display = ''; }
      return;
    }
    var ph = document.getElementById('cg-vf-placeholder');
    if (ph) ph.style.display = 'none';
    /* Build synthetic entries — each dead occ becomes its own block */
    var entries = occs.map(function(occ) {
      return { occ: occ, localName: occ.name, origName: occ.name };
    });
    _vfBuildGraph(entries, []);
  }

  window.cgOpenDeadReport = function() {
    var dead = _vfDeadVarOccs();
    var modal = document.getElementById('cg-dead-modal');
    var body  = document.getElementById('cg-dead-modal-body');
    if (!modal || !body) return;
    var h = '<div class="cg-dead-hdr">'
      +'<span>Variable</span><span>Function</span><span>Line</span>'
      +'<span>Type</span><span>Reason</span></div>';
    if (!dead.length) {
      h += '<div style="padding:18px;color:#5a6a7a;font-size:12px">No dead variables detected.</div>';
    } else {
      dead.forEach(function(occ) {
        h += '<div class="cg-dead-row">'
          +'<span class="cg-dead-name">'+esc(occ.name)+'</span>'
          +'<span class="cg-dead-fn" title="'+esc(occ.function_name)+'">'+esc(occ.function_name)+'</span>'
          +'<span class="cg-dead-line">'+esc((occ.file_name||'')+(occ.line?':'+occ.line:''))+'</span>'
          +'<span class="cg-dead-type">'+esc(occ.data_type||occ.type_hint||'—')+'</span>'
          +'<span class="cg-dead-why">'+esc(occ.dead_reason||'unused')+'</span>'
          +'</div>';
      });
    }
    body.innerHTML = h;
    modal.classList.add('open');
    /* Update sidebar counts while we're at it */
    _vfUpdateDeadCount();
  };

  window.cgCloseDeadReport = function() {
    var m = document.getElementById('cg-dead-modal');
    if (m) m.classList.remove('open');
  };

  function _vfUpdateDeadCount() {
    var dead = _vfDeadVarOccs();
    var params = dead.filter(function(o){ return o.action==='argument' || o.dead_reason==='unused parameter'; });
    var vars   = dead.length - params.length;
    var cv = document.getElementById('cg-dead-count');
    var cp = document.getElementById('cg-dead-param-count');
    if (cv) cv.textContent = vars;
    if (cp) cp.textContent = params.length;
  }

  /* ── localStorage notes ─────────────────────────────────────── */

  function _vfNoteKey(occ) {
    return (occ.function_id || occ.function_name || '') + '::' + (occ.name || '');
  }

  function _vfGetNote(key) {
    try {
      var data = JSON.parse(localStorage.getItem(_VF_NOTES_KEY) || '{}');
      return data[key] || '';
    } catch(e) { return ''; }
  }

  function _vfSetNote(key, text) {
    try {
      var data = JSON.parse(localStorage.getItem(_VF_NOTES_KEY) || '{}');
      if (text) data[key] = text; else delete data[key];
      localStorage.setItem(_VF_NOTES_KEY, JSON.stringify(data));
    } catch(e) {}
  }

  /* ── Right-click context menu ──────────────────────────────── */

  function _vfShowCtxMenu(nodeId, occ, cx, cy) {
    _vfCtxTargetId = nodeId;
    var menu = document.getElementById('cg-vf-ctx-menu');
    if (!menu) return;
    var key = _vfNoteKey(occ);
    var existingNote = _vfGetNote(key);
    var addItem  = document.getElementById('cg-ctx-add-note');
    var editItem = document.getElementById('cg-ctx-edit-note');
    var delItem  = document.getElementById('cg-ctx-del-note');
    if (addItem)  addItem.style.display  = existingNote ? 'none' : '';
    if (editItem) editItem.style.display = existingNote ? '' : 'none';
    if (delItem)  delItem.style.display  = existingNote ? '' : 'none';
    menu.style.cssText = 'display:block;left:'+cx+'px;top:'+cy+'px';
    /* Store occ for handlers */
    menu._occ = occ;
  }

  (function(){
    var menu = document.getElementById('cg-vf-ctx-menu');
    if (!menu) return;

    function _promptNote(existing) {
      var t = prompt(existing ? 'Edit note:' : 'Add note:', existing || '');
      if (t === null) return;   /* cancelled */
      var occ = menu._occ;
      if (!occ) return;
      var key = _vfNoteKey(occ);
      _vfSetNote(key, t.trim());
      /* Refresh current graph to show/hide note dot */
      if (_vfDeadMode) { _vfShowDeadVars(); }
      else if (_vfCurrentVar && VAR_FLOW_DATA[_vfCurrentVar]) {
        var chain = _vfBuildFlowChain(_vfCurrentVar);
        _vfBuildGraph(chain.entries, chain.flowEdges);
      }
    }

    var addItem  = document.getElementById('cg-ctx-add-note');
    var editItem = document.getElementById('cg-ctx-edit-note');
    var delItem  = document.getElementById('cg-ctx-del-note');
    if (addItem)  addItem.addEventListener('click',  function(){ menu.style.display='none'; _promptNote(''); });
    if (editItem) editItem.addEventListener('click', function(){ menu.style.display='none'; _promptNote(_vfGetNote(_vfNoteKey(menu._occ))); });
    if (delItem)  delItem.addEventListener('click',  function(){
      menu.style.display='none';
      if (menu._occ) { _vfSetNote(_vfNoteKey(menu._occ),''); }
      if (_vfDeadMode) { _vfShowDeadVars(); }
      else if (_vfCurrentVar && VAR_FLOW_DATA[_vfCurrentVar]) {
        var chain = _vfBuildFlowChain(_vfCurrentVar);
        _vfBuildGraph(chain.entries, chain.flowEdges);
      }
    });

    /* Hide menu on any click outside */
    document.addEventListener('click', function(e){
      if (!menu.contains(e.target)) menu.style.display='none';
    });
    document.addEventListener('keydown', function(e){
      if (e.key==='Escape') menu.style.display='none';
    });
  })();

  /* ── Annotation rectangles ──────────────────────────────────── */

  function _vfAnnotsKey() {
    if (_vfDeadMode) return _VF_ANNOTS_KEY + '::__dead__';
    return _VF_ANNOTS_KEY + '::' + (_vfCurrentVar || '__global__');
  }
  function _vfAnnotsLoad() {
    try { return JSON.parse(localStorage.getItem(_vfAnnotsKey()) || '[]'); } catch(e) { return []; }
  }
  function _vfAnnotsSave(annots) {
    try { localStorage.setItem(_vfAnnotsKey(), JSON.stringify(annots)); } catch(e) {}
  }

  window._vfToggleAnnotMode = function() {
    _vfAnnotMode = !_vfAnnotMode;
    var btn = document.getElementById('cg-vf-annot-btn');
    if (btn) btn.classList.toggle('active', _vfAnnotMode);
    var vp = document.getElementById('cg-vf-viewport');
    if (vp) vp.style.cursor = _vfAnnotMode ? 'crosshair' : '';
  };

  function _vfRenderAnnots() {
    var canvas = document.getElementById('cg-vf-canvas');
    if (!canvas) return;
    /* Remove existing annot divs */
    canvas.querySelectorAll('.cg-vf-annot').forEach(function(el){ el.remove(); });
    var annots = _vfAnnotsLoad();
    annots.forEach(function(a, i) {
      _vfRenderAnnotEl(canvas, a, i);
    });
  }

  function _vfRenderAnnotEl(canvas, a, idx) {
    var el = document.createElement('div');
    el.className = 'cg-vf-annot';
    el.dataset.annotIdx = idx;
    el.style.cssText = 'left:'+a.x+'px;top:'+a.y+'px;width:'+a.w+'px;height:'+a.h+'px;';
    _annotApplyColor(el, a.color);
    el.innerHTML = '<div class="cg-vf-annot-label" style="font-size:'+(a.fontSize||16)+'px">'+esc(a.label||'')+'</div>'
      +'<button class="cg-vf-annot-del" title="Delete annotation">\xd7</button>'
      +'<div class="cg-vf-annot-resize" title="Resize"></div>';
    el.querySelector('.cg-vf-annot-del').addEventListener('click', function(e){
      e.stopPropagation();
      var annots = _vfAnnotsLoad();
      annots.splice(idx, 1);
      _vfAnnotsSave(annots);
      _vfRenderAnnots();
    });
    /* Double-click to edit label */
    el.addEventListener('dblclick', function(e){
      if (e.target.classList.contains('cg-vf-annot-del') || e.target.classList.contains('cg-vf-annot-resize')) return;
      e.stopPropagation();
      var annots = _vfAnnotsLoad();
      var _cv = annots[idx];
      _showAnnotPicker({label:_cv?_cv.label:'', color:_cv?_cv.color:'#46c864', fontSize:_cv?(_cv.fontSize||24):24}, function(label, color, fontSize) {
        if (label === null) return;
        var ann2 = _vfAnnotsLoad();
        if (ann2[idx]) {
          ann2[idx].label    = label;
          ann2[idx].color    = color    || ann2[idx].color;
          ann2[idx].fontSize = fontSize || ann2[idx].fontSize || 24;
          _vfAnnotsSave(ann2); _vfRenderAnnots();
        }
      });
    });
    /* Drag to move */
    el.addEventListener('mousedown', function(e){
      if (e.target.classList.contains('cg-vf-annot-del') || e.target.classList.contains('cg-vf-annot-resize')) return;
      if (e.button !== 0) return;
      e.stopPropagation();
      _vfAnnotDrag = {idx:idx, startMX:e.clientX, startMY:e.clientY, startX:a.x, startY:a.y};
    });
    /* Resize handle */
    el.querySelector('.cg-vf-annot-resize').addEventListener('mousedown', function(e){
      e.stopPropagation();
      _vfAnnotResize = {idx:idx, startMX:e.clientX, startMY:e.clientY, startW:a.w, startH:a.h};
    });
    canvas.appendChild(el);
  }

  /* Wire annotation drawing on viewport */
  (function(){
    var vp = document.getElementById('cg-vf-viewport');
    if (!vp) { document.addEventListener('DOMContentLoaded', function(){ vp=document.getElementById('cg-vf-viewport'); _wireAnnot(vp); }); return; }
    _wireAnnot(vp);
    function _wireAnnot(vp) {
      if (!vp || vp._annotWired) return;
      vp._annotWired = true;

      vp.addEventListener('mousedown', function(e){
        if (!_vfAnnotMode || e.button !== 0 || e.target.closest('.cg-vf-node') || e.target.closest('.cg-vf-annot')) return;
        e.preventDefault();
        e.stopPropagation();
        var canvas = document.getElementById('cg-vf-canvas');
        var rect = canvas ? canvas.getBoundingClientRect() : vp.getBoundingClientRect();
        var sx = (e.clientX - rect.left) / _vfZoom - _vfPanX/_vfZoom;
        var sy = (e.clientY - rect.top)  / _vfZoom - _vfPanY/_vfZoom;
        _vfAnnotDrawing = {sx:sx, sy:sy, el:null};
      });

      document.addEventListener('mousemove', function(e){
        if (_vfAnnotDrag) {
          var annots = _vfAnnotsLoad();
          if (annots[_vfAnnotDrag.idx]) {
            annots[_vfAnnotDrag.idx].x = _vfAnnotDrag.startX + (e.clientX-_vfAnnotDrag.startMX)/_vfZoom;
            annots[_vfAnnotDrag.idx].y = _vfAnnotDrag.startY + (e.clientY-_vfAnnotDrag.startMY)/_vfZoom;
            _vfAnnotsSave(annots);
            _vfRenderAnnots();
          }
          return;
        }
        if (_vfAnnotResize) {
          var annots2 = _vfAnnotsLoad();
          if (annots2[_vfAnnotResize.idx]) {
            annots2[_vfAnnotResize.idx].w = Math.max(60, _vfAnnotResize.startW + (e.clientX-_vfAnnotResize.startMX)/_vfZoom);
            annots2[_vfAnnotResize.idx].h = Math.max(30, _vfAnnotResize.startH + (e.clientY-_vfAnnotResize.startMY)/_vfZoom);
            _vfAnnotsSave(annots2);
            _vfRenderAnnots();
          }
          return;
        }
        if (!_vfAnnotDrawing) return;
        var canvas = document.getElementById('cg-vf-canvas');
        var rect = canvas ? canvas.getBoundingClientRect() : vp.getBoundingClientRect();
        var cx = (e.clientX - rect.left) / _vfZoom - _vfPanX/_vfZoom;
        var cy = (e.clientY - rect.top)  / _vfZoom - _vfPanY/_vfZoom;
        var w = Math.abs(cx - _vfAnnotDrawing.sx), h = Math.abs(cy - _vfAnnotDrawing.sy);
        var x = Math.min(cx, _vfAnnotDrawing.sx), y = Math.min(cy, _vfAnnotDrawing.sy);
        if (!_vfAnnotDrawing.el) {
          _vfAnnotDrawing.el = document.createElement('div');
          _vfAnnotDrawing.el.className = 'cg-vf-annot';
          _vfAnnotDrawing.el.style.pointerEvents='none';
          if (canvas) canvas.appendChild(_vfAnnotDrawing.el);
        }
        _vfAnnotDrawing.el.style.cssText='left:'+x+'px;top:'+y+'px;width:'+w+'px;height:'+h+'px;pointer-events:none;position:absolute';
      });

      document.addEventListener('mouseup', function(e){
        if (_vfAnnotDrag)   { _vfAnnotDrag   = null; return; }
        if (_vfAnnotResize) { _vfAnnotResize = null; return; }
        if (!_vfAnnotDrawing) return;
        var drawing = _vfAnnotDrawing;
        _vfAnnotDrawing = null;
        if (drawing.el) drawing.el.remove();
        var canvas = document.getElementById('cg-vf-canvas');
        var rect = canvas ? canvas.getBoundingClientRect() : vp.getBoundingClientRect();
        var cx = (e.clientX - rect.left) / _vfZoom - _vfPanX/_vfZoom;
        var cy = (e.clientY - rect.top)  / _vfZoom - _vfPanY/_vfZoom;
        var w = Math.abs(cx - drawing.sx), h = Math.abs(cy - drawing.sy);
        if (w < 20 || h < 15) return;   /* too small — ignore */
        var x = Math.min(cx, drawing.sx), y = Math.min(cy, drawing.sy);
        _showAnnotPicker({label:'', color:'#46c864', fontSize:24}, function(label, color, fontSize) {
          if (label === null) return;
          var annots = _vfAnnotsLoad();
          annots.push({x:x, y:y, w:w, h:h, label:label||'', color:color||'#46c864', fontSize:fontSize||24});
          _vfAnnotsSave(annots);
          _vfRenderAnnots();
        });
      });
    }
  })();

  /* ── Script Mode annotation system ──────────────────────────── */

  var _svAnnotMode = false;
  var _svAnnotDrag = null;
  var _svAnnotResize = null;
  var _svAnnotDrawing = null;
  var _SV_ANNOTS_KEY = GRAPH_ID + ':cg_sv_annots_v1';

  function _svAnnotsLoad() {
    try { return JSON.parse(localStorage.getItem(_SV_ANNOTS_KEY) || '[]'); } catch(e) { return []; }
  }
  function _svAnnotsSave(annots) {
    try { localStorage.setItem(_SV_ANNOTS_KEY, JSON.stringify(annots)); } catch(e) {}
  }
  window._svToggleAnnotMode = function() {
    _svAnnotMode = !_svAnnotMode;
    var btn = document.getElementById('cg-sv-annot-btn');
    if (btn) {
      btn.classList.toggle('active', _svAnnotMode);
      btn.style.background = _svAnnotMode ? '#0e2a18' : '#1a1d23';
      btn.style.borderColor = _svAnnotMode ? '#4ec980' : '#3d4451';
    }
    var vp = document.getElementById('cg-sv-viewport');
    if (vp) vp.style.cursor = _svAnnotMode ? 'crosshair' : '';
  };
  function _svRenderAnnots() {
    var canvas = document.getElementById('cg-sv-canvas');
    if (!canvas) return;
    canvas.querySelectorAll('.cg-vf-annot').forEach(function(el){ el.remove(); });
    _svAnnotsLoad().forEach(function(a, i){ _svRenderAnnotEl(canvas, a, i); });
  }
  function _svRenderAnnotEl(canvas, a, idx) {
    var el = document.createElement('div');
    el.className = 'cg-vf-annot';
    el.style.cssText = 'left:'+a.x+'px;top:'+a.y+'px;width:'+(a.w||120)+'px;height:'+(a.h||60)+'px;';
    _annotApplyColor(el, a.color);
    el.innerHTML =
      '<div class="cg-vf-annot-del" title="Delete">\xd7</div>'
      +(a.label ? '<div class="cg-vf-annot-label" style="font-size:'+(a.fontSize||16)+'px">'+esc(a.label)+'</div>'
                : '<div class="cg-vf-annot-label" style="color:#4a5a6a;font-style:italic;font-size:'+(a.fontSize||16)+'px">dbl-click to label</div>')
      +'<div class="cg-vf-annot-resize" title="Resize"></div>';
    el.querySelector('.cg-vf-annot-del').addEventListener('click', function(e){
      e.stopPropagation();
      var ann = _svAnnotsLoad(); ann.splice(idx,1); _svAnnotsSave(ann); _svRenderAnnots();
    });
    el.addEventListener('dblclick', function(e){
      if (e.target.classList.contains('cg-vf-annot-del')||e.target.classList.contains('cg-vf-annot-resize')) return;
      e.stopPropagation();
      var ann = _svAnnotsLoad();
      var _csv = ann[idx];
      _showAnnotPicker({label:_csv?_csv.label:'', color:_csv?_csv.color:'#46c864', fontSize:_csv?(_csv.fontSize||24):24}, function(lbl, color, fontSize) {
        if (lbl === null) return;
        var ann2 = _svAnnotsLoad();
        if (ann2[idx]) {
          ann2[idx].label=lbl; ann2[idx].color=color||ann2[idx].color;
          ann2[idx].fontSize=fontSize||ann2[idx].fontSize||24;
          _svAnnotsSave(ann2); _svRenderAnnots();
        }
      });
    });
    el.querySelector('.cg-vf-annot-resize').addEventListener('mousedown', function(e){
      e.stopPropagation();
      _svAnnotResize = {idx:idx, startMX:e.clientX, startMY:e.clientY, startW:a.w, startH:a.h};
    });
    el.addEventListener('mousedown', function(e){
      if (e.target.classList.contains('cg-vf-annot-del')||e.target.classList.contains('cg-vf-annot-resize')) return;
      e.stopPropagation();
      _svAnnotDrag = {idx:idx, startMX:e.clientX, startMY:e.clientY, startX:a.x, startY:a.y};
    });
    canvas.appendChild(el);
  }
  /* Wire Script Mode annotation drawing — called after _buildScriptView() injects #cg-sv-viewport */
  window._wireSvAnnotEvents = function() {
    var vp = document.getElementById('cg-sv-viewport');
    if (!vp || vp._svAnnotWired) return;
    vp._svAnnotWired = true;
    vp.addEventListener('mousedown', function(e){
      if (!_svAnnotMode||e.button!==0||e.target.closest('.cg-vf-annot')||e.target.closest('.cg-fn-row')||e.target.closest('.cg-fc-header')) return;
      e.preventDefault(); e.stopPropagation();
      var canvas = document.getElementById('cg-sv-canvas');
      var rect = canvas ? canvas.getBoundingClientRect() : vp.getBoundingClientRect();
      _svAnnotDrawing = {sx:(e.clientX-rect.left)/_svZoom, sy:(e.clientY-rect.top)/_svZoom, el:null};
    });
    document.addEventListener('mousemove', function(e){
      if (_svAnnotDrag) {
        var ann = _svAnnotsLoad();
        if (ann[_svAnnotDrag.idx]) {
          ann[_svAnnotDrag.idx].x = _svAnnotDrag.startX + (e.clientX-_svAnnotDrag.startMX)/_svZoom;
          ann[_svAnnotDrag.idx].y = _svAnnotDrag.startY + (e.clientY-_svAnnotDrag.startMY)/_svZoom;
          _svAnnotsSave(ann); _svRenderAnnots();
        }
        return;
      }
      if (_svAnnotResize) {
        var ann2 = _svAnnotsLoad();
        if (ann2[_svAnnotResize.idx]) {
          ann2[_svAnnotResize.idx].w = Math.max(60, _svAnnotResize.startW+(e.clientX-_svAnnotResize.startMX)/_svZoom);
          ann2[_svAnnotResize.idx].h = Math.max(30, _svAnnotResize.startH+(e.clientY-_svAnnotResize.startMY)/_svZoom);
          _svAnnotsSave(ann2); _svRenderAnnots();
        }
        return;
      }
      if (!_svAnnotDrawing) return;
      var canvas = document.getElementById('cg-sv-canvas');
      var rect = canvas ? canvas.getBoundingClientRect() : vp.getBoundingClientRect();
      var cx=(e.clientX-rect.left)/_svZoom, cy=(e.clientY-rect.top)/_svZoom;
      var w=Math.abs(cx-_svAnnotDrawing.sx), h=Math.abs(cy-_svAnnotDrawing.sy);
      var x=Math.min(cx,_svAnnotDrawing.sx), y=Math.min(cy,_svAnnotDrawing.sy);
      if (!_svAnnotDrawing.el) {
        _svAnnotDrawing.el = document.createElement('div');
        _svAnnotDrawing.el.className = 'cg-vf-annot';
        _svAnnotDrawing.el.style.pointerEvents='none';
        if (canvas) canvas.appendChild(_svAnnotDrawing.el);
      }
      _svAnnotDrawing.el.style.cssText='left:'+x+'px;top:'+y+'px;width:'+w+'px;height:'+h+'px;pointer-events:none;position:absolute';
    });
    document.addEventListener('mouseup', function(e){
      if (_svAnnotDrag)   { _svAnnotDrag=null; return; }
      if (_svAnnotResize) { _svAnnotResize=null; return; }
      if (!_svAnnotDrawing) return;
      var drawing=_svAnnotDrawing; _svAnnotDrawing=null;
      if (drawing.el) drawing.el.remove();
      var canvas = document.getElementById('cg-sv-canvas');
      var rect = canvas ? canvas.getBoundingClientRect() : vp.getBoundingClientRect();
      var cx=(e.clientX-rect.left)/_svZoom, cy=(e.clientY-rect.top)/_svZoom;
      var w=Math.abs(cx-drawing.sx), h=Math.abs(cy-drawing.sy);
      if (w<20||h<15) return;
      var x=Math.min(cx,drawing.sx), y=Math.min(cy,drawing.sy);
      _showAnnotPicker({label:'', color:'#46c864', fontSize:24}, function(lbl, color, fontSize) {
        if (lbl === null) return;
        var ann=_svAnnotsLoad();
        ann.push({x:x,y:y,w:w,h:h,label:lbl||'',color:color||'#46c864',fontSize:fontSize||24});
        _svAnnotsSave(ann); _svRenderAnnots();
      });
    });
  };

  /* ── Function Mode annotation system ────────────────────────── */
  /*
   * Annotations are stored in vis.js graph coordinates.
   * _fnUpdateAnnotPositions() converts them to container-relative screen
   * coordinates on every afterDrawing/zoom event so they track pan+zoom.
   */
  var _fnAnnotMode = false;
  var _fnAnnotDrag = null;
  var _fnAnnotResize = null;
  var _fnAnnotDrawing = null;
  var _FN_ANNOTS_KEY = GRAPH_ID + ':cg_fn_annots_v1';

  function _fnAnnotsLoad() {
    try { return JSON.parse(localStorage.getItem(_FN_ANNOTS_KEY) || '[]'); } catch(e) { return []; }
  }
  function _fnAnnotsSave(annots) {
    try { localStorage.setItem(_FN_ANNOTS_KEY, JSON.stringify(annots)); } catch(e) {}
  }
  function _fnMoveAnnotsBy(dx, dy, selectedRect) {
    try {
      var ann = _fnAnnotsLoad();
      var changed = false;
      ann.forEach(function(a){
        var ar = {left:a.x, top:a.y, right:a.x+(a.w||0), bottom:a.y+(a.h||0)};
        if (_rectIntersects(ar, selectedRect)) {
          a.x += dx; a.y += dy; changed = true;
        }
      });
      if (changed) { _fnAnnotsSave(ann); _fnRenderAnnots(); }
    } catch(e) {}
  }
  window._fnToggleAnnotMode = function() {
    _fnAnnotMode = !_fnAnnotMode;
    var btn = document.getElementById('cg-fn-annot-btn');
    if (btn) {
      btn.classList.toggle('active', _fnAnnotMode);
      btn.style.background = _fnAnnotMode ? '#0e2a18' : '#1a1d23';
      btn.style.borderColor = _fnAnnotMode ? '#4ec980' : '#3d4451';
    }
    var layer = document.getElementById('cg-fn-annot-layer');
    if (layer) layer.style.pointerEvents = _fnAnnotMode ? 'all' : 'none';
  };
  function _fnRenderAnnots() {
    var layer = document.getElementById('cg-fn-annot-layer');
    if (!layer) return;
    layer.querySelectorAll('.cg-vf-annot').forEach(function(el){ el.remove(); });
    _fnAnnotsLoad().forEach(function(a, i){ _fnRenderAnnotEl(layer, a, i); });
    _fnUpdateAnnotPositions();
  }
  function _fnRenderAnnotEl(layer, a, idx) {
    var el = document.createElement('div');
    el.className = 'cg-vf-annot';
    el.dataset.annotIdx = idx;   /* required by _fnUpdateAnnotPositions */
    el.style.cssText = 'left:'+a.x+'px;top:'+a.y+'px;width:'+(a.w||120)+'px;height:'+(a.h||60)+'px;';
    _annotApplyColor(el, a.color);
    el.innerHTML =
      '<div class="cg-vf-annot-del" title="Delete">\xd7</div>'
      +(a.label ? '<div class="cg-vf-annot-label" style="font-size:'+(a.fontSize||16)+'px">'+esc(a.label)+'</div>'
                : '<div class="cg-vf-annot-label" style="color:#4a5a6a;font-style:italic;font-size:'+(a.fontSize||16)+'px">dbl-click to label</div>')
      +'<div class="cg-vf-annot-resize" title="Resize"></div>';
    el.querySelector('.cg-vf-annot-del').addEventListener('click', function(e){
      e.stopPropagation();
      var ann = _fnAnnotsLoad(); ann.splice(idx,1); _fnAnnotsSave(ann); _fnRenderAnnots();
    });
    el.addEventListener('dblclick', function(e){
      if (e.target.classList.contains('cg-vf-annot-del')||e.target.classList.contains('cg-vf-annot-resize')) return;
      e.stopPropagation();
      var ann = _fnAnnotsLoad();
      var _cfn = ann[idx];
      _showAnnotPicker({label:_cfn?_cfn.label:'', color:_cfn?_cfn.color:'#46c864', fontSize:_cfn?(_cfn.fontSize||24):24}, function(lbl, color, fontSize) {
        if (lbl === null) return;
        var ann2 = _fnAnnotsLoad();
        if (ann2[idx]) {
          ann2[idx].label=lbl; ann2[idx].color=color||ann2[idx].color;
          ann2[idx].fontSize=fontSize||ann2[idx].fontSize||24;
          _fnAnnotsSave(ann2); _fnRenderAnnots();
        }
      });
    });
    el.querySelector('.cg-vf-annot-resize').addEventListener('mousedown', function(e){
      e.stopPropagation();
      _fnAnnotResize = {idx:idx, startMX:e.clientX, startMY:e.clientY, startW:a.w, startH:a.h};
    });
    el.addEventListener('mousedown', function(e){
      if (e.target.classList.contains('cg-vf-annot-del')||e.target.classList.contains('cg-vf-annot-resize')) return;
      e.stopPropagation();
      _fnAnnotDrag = {idx:idx, startMX:e.clientX, startMY:e.clientY, startX:a.x, startY:a.y};
    });
    layer.appendChild(el);
  }
  /* Inject the overlay div and button into the vis.js wrapper; wire events once */
  window._fnInitAnnotLayer = function() {
    if (document.getElementById('cg-fn-annot-layer')) {
      _fnRenderAnnots();   /* also calls _fnUpdateAnnotPositions */
      var _pl = document.getElementById('cg-fn-pin-layer') || document.getElementById('cg-fn-annot-layer');
      _renderAllPins(_pl, 'fn', '');
      _fnUpdateLayerTransform();
      return;
    }
    var netEl = document.getElementById('mynetwork');
    var wrapper = (netEl && netEl.parentElement && netEl.parentElement !== document.body)
                  ? netEl.parentElement : netEl;
    if (!wrapper) return;
    wrapper.style.position = 'relative';
    /* Annotation overlay — no CSS transform; annotations positioned in screen coords */
    var layer = document.createElement('div');
    layer.id = 'cg-fn-annot-layer';
    layer.style.cssText = 'position:absolute;top:0;left:0;right:0;bottom:0;pointer-events:none;z-index:10;overflow:visible';
    wrapper.appendChild(layer);
    /* Pin layer — has CSS transform mirroring vis.js viewport; pins positioned in graph coords */
    var pinLayer = document.createElement('div');
    pinLayer.id = 'cg-fn-pin-layer';
    pinLayer.style.cssText = 'position:absolute;top:0;left:0;overflow:visible;pointer-events:none;transform-origin:0 0';
    layer.appendChild(pinLayer);
    /* Annotate button */
    var btn = document.createElement('button');
    btn.id = 'cg-fn-annot-btn';
    btn.textContent = '□ Annotate';
    btn.title = 'Draw annotation rectangle (click then drag)';
    btn.onclick = function(){ window._fnToggleAnnotMode(); };
    btn.style.cssText = 'position:absolute;top:10px;right:14px;z-index:20;padding:4px 10px;font-size:11px;font-weight:600;border:1px solid #3d4451;background:#1a1d23;color:#4ec980;border-radius:4px;cursor:pointer;white-space:nowrap';
    wrapper.appendChild(btn);
    /* Convert screen clientX/Y to vis.js graph coordinates */
    function _fnDomToGraph(clientX, clientY) {
      var net = getNet();
      var ne  = document.getElementById('mynetwork');
      if (!net || !ne) return {x: clientX, y: clientY};
      var r = ne.getBoundingClientRect();
      return net.DOMtoCanvas({x: clientX - r.left, y: clientY - r.top});
    }
    /* Get bounding rect of mynetwork (fallback to zero-origin) */
    function _netRect() {
      var ne = document.getElementById('mynetwork');
      return ne ? ne.getBoundingClientRect() : {left:0, top:0};
    }
    /* Wire draw/drag/resize events */
    layer.addEventListener('mousedown', function(e){
      if (!_fnAnnotMode || e.button !== 0 || e.target.closest('.cg-vf-annot')) return;
      e.preventDefault(); e.stopPropagation();
      var gp = _fnDomToGraph(e.clientX, e.clientY);
      var r  = _netRect();
      _fnAnnotDrawing = {
        sx: gp.x, sy: gp.y,
        screenSX: e.clientX - r.left, screenSY: e.clientY - r.top,
        el: null
      };
    });
    document.addEventListener('mousemove', function(e){
      if (_fnAnnotDrag) {
        var s = (getNet() ? getNet().getScale() : 1);
        var ann = _fnAnnotsLoad();
        if (ann[_fnAnnotDrag.idx]) {
          ann[_fnAnnotDrag.idx].x = _fnAnnotDrag.startX + (e.clientX - _fnAnnotDrag.startMX) / s;
          ann[_fnAnnotDrag.idx].y = _fnAnnotDrag.startY + (e.clientY - _fnAnnotDrag.startMY) / s;
          _fnAnnotsSave(ann); _fnRenderAnnots();
        }
        return;
      }
      if (_fnAnnotResize) {
        var s2  = (getNet() ? getNet().getScale() : 1);
        var ann2 = _fnAnnotsLoad();
        if (ann2[_fnAnnotResize.idx]) {
          ann2[_fnAnnotResize.idx].w = Math.max(40, _fnAnnotResize.startW + (e.clientX - _fnAnnotResize.startMX) / s2);
          ann2[_fnAnnotResize.idx].h = Math.max(20, _fnAnnotResize.startH + (e.clientY - _fnAnnotResize.startMY) / s2);
          _fnAnnotsSave(ann2); _fnRenderAnnots();
        }
        return;
      }
      if (!_fnAnnotDrawing) return;
      /* Drawing preview uses screen coordinates (layer has no transform) */
      var r   = _netRect();
      var scx = e.clientX - r.left, scy = e.clientY - r.top;
      var px  = Math.min(scx, _fnAnnotDrawing.screenSX);
      var py  = Math.min(scy, _fnAnnotDrawing.screenSY);
      var pw  = Math.abs(scx - _fnAnnotDrawing.screenSX);
      var ph  = Math.abs(scy - _fnAnnotDrawing.screenSY);
      if (!_fnAnnotDrawing.el) {
        _fnAnnotDrawing.el = document.createElement('div');
        _fnAnnotDrawing.el.className = 'cg-vf-annot';
        _fnAnnotDrawing.el.style.pointerEvents = 'none';
        layer.appendChild(_fnAnnotDrawing.el);
      }
      _fnAnnotDrawing.el.style.cssText = 'left:'+px+'px;top:'+py+'px;width:'+pw+'px;height:'+ph+'px;pointer-events:none;';
    });
    document.addEventListener('mouseup', function(e){
      if (_fnAnnotDrag)   { _fnAnnotDrag   = null; return; }
      if (_fnAnnotResize) { _fnAnnotResize = null; return; }
      if (!_fnAnnotDrawing) return;
      var drawing = _fnAnnotDrawing; _fnAnnotDrawing = null;
      if (drawing.el) drawing.el.remove();
      /* Reject tiny drags (screen pixels) */
      var r    = _netRect();
      var scxU = e.clientX - r.left, scyU = e.clientY - r.top;
      if (Math.abs(scxU - drawing.screenSX) < 20 || Math.abs(scyU - drawing.screenSY) < 15) return;
      /* Store in graph coordinates */
      var gp = _fnDomToGraph(e.clientX, e.clientY);
      var w  = Math.abs(gp.x - drawing.sx), h = Math.abs(gp.y - drawing.sy);
      var x  = Math.min(gp.x, drawing.sx),  y = Math.min(gp.y, drawing.sy);
      _showAnnotPicker({label:'', color:'#46c864', fontSize:24}, function(lbl, color, fontSize) {
        if (lbl === null) return;
        var ann = _fnAnnotsLoad();
        ann.push({x:x, y:y, w:w, h:h, label:lbl||'', color:color||'#46c864', fontSize:fontSize||24});
        _fnAnnotsSave(ann); _fnRenderAnnots();
      });
    });
    _fnRenderAnnots();   /* positions annotations after initial vis.js layout */
    _renderAllPins(pinLayer, 'fn', '');
    _fnUpdateLayerTransform();   /* apply initial CSS transform to pin layer */
    /*
     * Capture-phase contextmenu on document: vis.js calls stopPropagation() on
     * right-click inside its canvas, so a bubble-phase listener on wrapper would
     * never fire.  A capture listener fires BEFORE any child can stop propagation.
     */
    document.addEventListener('contextmenu', function(e){
      if (currentMode !== 'fn') return;
      var netEl2 = document.getElementById('mynetwork');
      if (!netEl2 || !netEl2.contains(e.target)) return;
      if (e.target.closest('.cg-vf-annot') || e.target.closest('.cg-pin') || e.target.closest('#cg-fn-annot-btn')) return;
      e.preventDefault();
      var gp = _fnDomToGraph(e.clientX, e.clientY);
      _showPinCtxMenu(e.clientX, e.clientY, 'fn', '', gp.x, gp.y);
    }, true /* capture */);
  };

  /* Run dead-var count init after data is available */
  setTimeout(_vfUpdateDeadCount, 100);

  /* Close dead-vars modal on background click or Escape */
  (function(){
    var m = document.getElementById('cg-dead-modal');
    if (m) m.addEventListener('click', function(e){ if(e.target===m) cgCloseDeadReport(); });
  })();

  /* Varflow details modal wiring */
  (function(){
    var closeBtn=document.getElementById('cg-vf-modal-close');
    if(closeBtn) closeBtn.addEventListener('click',_vfCloseModal);
    var modal=document.getElementById('cg-vf-modal');
    if(modal) modal.addEventListener('click',function(e){
      if(e.target===modal) _vfCloseModal();
    });
    document.addEventListener('keydown',function(e){
      if(e.key==='Escape') { _vfCloseModal(); cgCloseDeadReport(); }
    });
  })();

  /* ── Annotation color helper: 50% fill + 85% border from hex color ── */
  function _annotApplyColor(el, color) {
    var c = color || '#46c864';
    if (c.charAt(0) === '#') {
      var hex = c.slice(1);
      if (hex.length === 3) hex = hex[0]+hex[0]+hex[1]+hex[1]+hex[2]+hex[2];
      var r = parseInt(hex.slice(0,2), 16);
      var g = parseInt(hex.slice(2,4), 16);
      var b = parseInt(hex.slice(4,6), 16);
      el.style.background  = 'rgba('+r+','+g+','+b+',0.25)';
      el.style.borderColor = 'rgba('+r+','+g+','+b+',0.85)';
    }
  }

  /* ── Fn-mode annotation layer: apply CSS transform to pin layer, reposition annotations ── */
  function _fnUpdateLayerTransform() {
    var pinLayer = document.getElementById('cg-fn-pin-layer');
    var net = getNet(), cont = document.getElementById('mynetwork');
    if (net && cont && pinLayer) {
      try {
        var scale = net.getScale();
        var vp    = net.getViewPosition();
        var cx    = cont.offsetWidth  / 2;
        var cy    = cont.offsetHeight / 2;
        pinLayer.style.transform = 'translate(' + (cx - vp.x * scale) + 'px,' + (cy - vp.y * scale) + 'px) scale(' + scale + ')';
      } catch(e) {}
    }
    _fnUpdateAnnotPositions();
  }

  function _fnUpdatePinPositions() {
    /* With CSS transform on cg-fn-pin-layer, pins stay in graph-unit coords.
       This function re-syncs DOM positions from saved data (e.g. after a reload). */
    var layer = document.getElementById('cg-fn-pin-layer') || document.getElementById('cg-fn-annot-layer');
    if (!layer) return;
    var pins = _pinsLoad('fn', '');
    layer.querySelectorAll('.cg-pin').forEach(function(el){
      var pid = el.dataset.pinId;
      var p   = pins.find(function(x){ return x.id === pid; });
      if (!p) return;
      el.style.left  = (p.x || 0) + 'px';
      el.style.top   = (p.y || 0) + 'px';
      el.style.width = (p.w || 160) + 'px';
    });
  }

  function _fnUpdateAnnotPositions() {
    var layer = document.getElementById('cg-fn-annot-layer');
    if (!layer) return;
    var net = getNet();
    var cont = document.getElementById('mynetwork');
    if (!net || !cont) return;
    try {
      var scale = net.getScale();
      var vp    = net.getViewPosition();
      var cx    = cont.offsetWidth  / 2;
      var cy    = cont.offsetHeight / 2;
      var annots = _fnAnnotsLoad();
      layer.querySelectorAll('.cg-vf-annot').forEach(function(el){
        var idx = parseInt(el.dataset.annotIdx);
        if (isNaN(idx) || !annots[idx]) return;
        var a = annots[idx];
        el.style.left   = (cx + (a.x - vp.x) * scale) + 'px';
        el.style.top    = (cy + (a.y - vp.y) * scale) + 'px';
        el.style.width  = ((a.w || 120) * scale) + 'px';
        el.style.height = ((a.h ||  60) * scale) + 'px';
      });
    } catch(e) {}
  }

  /* ── Annotation color-picker modal ────────────────────────────── */
  var _AP_COLORS = [
    '#46c864','#4fc3f7','#f7d774','#e74c3c',
    '#c586c0','#f0944a','#4ec9b0','#e8e8e8',
    '#7f8c8d','#ff79c6','#26d0c0','#a8e04a'
  ];
  var _apCallback = null;
  var _apSelectedColor = _AP_COLORS[0];
  var _apSelectedSize  = 24;   /* default label font size (px) */

  (function(){
    var sw = document.getElementById('cg-ap-swatches');
    if (!sw) return;
    _AP_COLORS.forEach(function(c, i){
      var s = document.createElement('div');
      s.className = 'cg-ap-swatch' + (i === 0 ? ' selected' : '');
      s.style.background = c;
      s.dataset.color = c;
      s.addEventListener('click', function(){
        sw.querySelectorAll('.cg-ap-swatch').forEach(function(x){ x.classList.remove('selected'); });
        s.classList.add('selected');
        _apSelectedColor = c;
      });
      sw.appendChild(s);
    });
    /* Wire font-size buttons */
    document.querySelectorAll('.cg-ap-size-btn').forEach(function(btn){
      btn.addEventListener('click', function(){
        document.querySelectorAll('.cg-ap-size-btn').forEach(function(b){ b.classList.remove('selected'); });
        btn.classList.add('selected');
        _apSelectedSize = parseInt(btn.dataset.sz, 10) || 24;
      });
    });
    document.getElementById('cg-ap-ok').addEventListener('click', function(){
      if (!_apCallback) return;
      var lbl = document.getElementById('cg-annot-picker-lbl').value;
      var cb = _apCallback; _apCallback = null;
      document.getElementById('cg-annot-picker').style.display = 'none';
      cb(lbl, _apSelectedColor, _apSelectedSize);
    });
    document.getElementById('cg-ap-cancel').addEventListener('click', function(){
      if (_apCallback) { var cb = _apCallback; _apCallback = null; cb(null, null, null); }
      document.getElementById('cg-annot-picker').style.display = 'none';
    });
    document.addEventListener('keydown', function(e){
      var p = document.getElementById('cg-annot-picker');
      if (!p || p.style.display === 'none') return;
      if (e.key === 'Enter')  { document.getElementById('cg-ap-ok').click(); }
      if (e.key === 'Escape') { document.getElementById('cg-ap-cancel').click(); }
    });
  })();

  function _showAnnotPicker(opts, cb) {
    var p = document.getElementById('cg-annot-picker');
    if (!p) { cb(opts.label || '', opts.color || _AP_COLORS[0], opts.fontSize || 24); return; }
    _apCallback = cb;
    _apSelectedColor = opts.color || _AP_COLORS[0];
    _apSelectedSize  = opts.fontSize || 24;
    p.querySelectorAll('.cg-ap-swatch').forEach(function(s){
      s.classList.toggle('selected', s.dataset.color === _apSelectedColor);
    });
    p.querySelectorAll('.cg-ap-size-btn').forEach(function(b){
      b.classList.toggle('selected', parseInt(b.dataset.sz, 10) === _apSelectedSize);
    });
    var li = document.getElementById('cg-annot-picker-lbl');
    if (li) li.value = opts.label || '';
    p.style.display = 'block';
    setTimeout(function(){ if (li) li.focus(); }, 30);
  }

  /* ── Layout toggles ─────────────────────────────────────────── */
  (function(){
    var btnStraight     = document.getElementById('cg-btn-straight');
    var btnHierarchical = document.getElementById('cg-btn-hierarchical');
    var btnSpread       = document.getElementById('cg-btn-sv-spread');
    var btnVfStraight   = document.getElementById('cg-btn-vf-straight');
    var btnSvStraight   = document.getElementById('cg-btn-sv-straight');

    if (btnStraight) btnStraight.addEventListener('click', function(){
      _fnStraightLines = !_fnStraightLines;
      btnStraight.classList.toggle('active', _fnStraightLines);
      var net = getNet();
      if (net) net.setOptions({edges: {smooth: {
        enabled: !_fnStraightLines, type: 'cubicBezier',
        forceDirection: 'vertical', roundness: 0.4
      }}});
    });

    function _applyFnHierarchical(net) {
      net = net || getNet();
      if (!net) return;
      if (_fnHierarchical) {
        net.setOptions({
          layout: {hierarchical: {enabled:true, direction:'UD',
            levelSeparation:230, nodeSpacing:180, treeSpacing:260}},
          physics: {enabled: true}
        });
        setTimeout(function(){ try { net.setOptions({physics:{enabled:false}}); } catch(e){} }, 1800);
      } else {
        net.setOptions({layout: {hierarchical: {enabled: false}}, physics: {enabled: false}});
        /* Restore: first apply INITIAL_POS, then overlay saved delta (moved nodes) */
        Object.keys(INITIAL_POS).forEach(function(id){
          try { net.moveNode(id, INITIAL_POS[id].x, INITIAL_POS[id].y); } catch(e) {}
        });
        var savedDelta = null;
        try { var raw = localStorage.getItem(LAYOUT_KEY); if (raw) savedDelta = JSON.parse(raw); } catch(e) {}
        if (savedDelta) {
          Object.keys(savedDelta).forEach(function(id){
            try { net.moveNode(id, savedDelta[id].x, savedDelta[id].y); } catch(e) {}
          });
        }
        try { net.fit({animation:false}); } catch(e) {}
      }
    }

    if (btnHierarchical) btnHierarchical.addEventListener('click', function(){
      _fnHierarchical = !_fnHierarchical;
      btnHierarchical.classList.toggle('active', _fnHierarchical);
      _applyFnHierarchical(getNet());
    });

    if (btnSpread) btnSpread.addEventListener('click', function(){
      _svWideSpread = !_svWideSpread;
      btnSpread.classList.toggle('active', _svWideSpread);
      _svCardGapX = _svWideSpread ? 780 : 520;
      _svCardGapY = _svWideSpread ? 360 : 240;
      _svCompGapX = _svWideSpread ? 960 : 680;
      _svCompGapY = _svWideSpread ? 560 : 420;
      /* Rebuild script view with new gaps */
      _svBuilt = false;
      var svEl = document.getElementById('cg-script-view');
      if (svEl) svEl.innerHTML = '';
      _buildScriptView();
    });

    if (btnVfStraight) btnVfStraight.addEventListener('click', function(){
      _vfStraightLines = !_vfStraightLines;
      btnVfStraight.classList.toggle('active', _vfStraightLines);
      if (_vfCurrentEdges && _vfCurrentNodes)
        _vfDrawEdges(_vfCurrentEdges, _vfCurrentNodes);
    });

    if (btnSvStraight) btnSvStraight.addEventListener('click', function(){
      _svStraightLines = !_svStraightLines;
      window.cgStraightEdges = _svStraightLines;
      btnSvStraight.classList.toggle('active', _svStraightLines);
      _svDrawEdges();
      document.dispatchEvent(new CustomEvent('cg:straight-edges:change', {detail:{straight:_svStraightLines}}));
    });

    /* reflect comfort defaults in button UI on load */
    if (btnHierarchical) btnHierarchical.classList.toggle('active', _fnHierarchical);
    if (btnSpread) btnSpread.classList.toggle('active', _svWideSpread);

    /* expose for initial activation after vis network is ready */
    window._cgApplyFnHierarchicalDefault = _applyFnHierarchical;
  })();

  /* ── Pinned notes ──────────────────────────────────────────────── */
  var _PIN_KEY_PREFIX = GRAPH_ID + ':cg_pins_v1';

  function _pinKey(mode, ctx) {
    return _PIN_KEY_PREFIX + '::' + mode + (ctx ? '::' + ctx : '');
  }
  function _pinsLoad(mode, ctx) {
    try { return JSON.parse(localStorage.getItem(_pinKey(mode, ctx)) || '[]'); } catch(e) { return []; }
  }
  function _pinsSave(mode, ctx, pins) {
    try { localStorage.setItem(_pinKey(mode, ctx), JSON.stringify(pins)); } catch(e) {}
  }
  function _pinUid() { return Date.now() + '_' + Math.random().toString(36).slice(2, 7); }

  function _renderPinEl(container, pin, mode, ctx) {
    var el = document.createElement('div');
    el.className = 'cg-pin';
    el.dataset.pinId = pin.id;
    el.style.left  = (pin.x || 0) + 'px';
    el.style.top   = (pin.y || 0) + 'px';
    el.style.width = (pin.w || 160) + 'px';
    el.innerHTML =
      '<div class="cg-pin-header">'
      + '<span style="font-size:11px">&#128204;</span>'
      + '<button class="cg-pin-close" title="Remove">\xd7</button>'
      + '</div>'
      + '<div class="cg-pin-body" contenteditable="true">' + esc(pin.text || '') + '</div>'
      + '<div class="cg-pin-resize"></div>';
    /* Save text on blur */
    el.querySelector('.cg-pin-body').addEventListener('blur', function(){
      var pins = _pinsLoad(mode, ctx);
      var p = pins.find(function(x){ return x.id === pin.id; });
      if (p) { p.text = el.querySelector('.cg-pin-body').textContent; _pinsSave(mode, ctx, pins); }
    });
    /* Close */
    el.querySelector('.cg-pin-close').addEventListener('click', function(e){
      e.stopPropagation();
      var pins = _pinsLoad(mode, ctx);
      _pinsSave(mode, ctx, pins.filter(function(p){ return p.id !== pin.id; }));
      el.remove();
    });
    /* Drag via header */
    el.querySelector('.cg-pin-header').addEventListener('mousedown', function(e){
      if (e.target.classList.contains('cg-pin-close')) return;
      e.stopPropagation(); e.preventDefault();
      var startX = e.clientX, startY = e.clientY, origX = pin.x, origY = pin.y;
      function onMove(ev) {
        var zoom = mode === 'fn' ? (getNet() ? getNet().getScale() : 1)
                 : mode === 'vf' ? _vfZoom : _svZoom;
        pin.x = origX + (ev.clientX - startX) / zoom;
        pin.y = origY + (ev.clientY - startY) / zoom;
        el.style.left = pin.x + 'px'; el.style.top = pin.y + 'px';
      }
      function onUp() {
        var pins = _pinsLoad(mode, ctx);
        var p = pins.find(function(x){ return x.id === pin.id; });
        if (p) { p.x = pin.x; p.y = pin.y; _pinsSave(mode, ctx, pins); }
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
    /* Resize handle */
    el.querySelector('.cg-pin-resize').addEventListener('mousedown', function(e){
      e.stopPropagation(); e.preventDefault();
      var startX = e.clientX, origW = pin.w || 160;
      function onMove(ev) {
        var zoom2 = mode === 'fn' ? (getNet() ? getNet().getScale() : 1)
                  : mode === 'vf' ? _vfZoom : _svZoom;
        pin.w = Math.max(100, origW + (ev.clientX - startX) / zoom2);
        el.style.width = pin.w + 'px';
      }
      function onUp() {
        var pins = _pinsLoad(mode, ctx);
        var p = pins.find(function(x){ return x.id === pin.id; });
        if (p) { p.w = pin.w; _pinsSave(mode, ctx, pins); }
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
    container.appendChild(el);
    if (!pin.text) setTimeout(function(){ el.querySelector('.cg-pin-body').focus(); }, 40);
  }

  function _renderAllPins(container, mode, ctx) {
    if (!container) return;
    container.querySelectorAll('.cg-pin').forEach(function(el){ el.remove(); });
    _pinsLoad(mode, ctx).forEach(function(p){ _renderPinEl(container, p, mode, ctx); });
  }

  /* Pin right-click context menu wiring */
  var _pinCtxState = {mode:'', ctx:'', canvasX:0, canvasY:0};
  function _showPinCtxMenu(clientX, clientY, mode, ctx, canvasX, canvasY) {
    var m = document.getElementById('cg-pin-ctx-menu');
    if (!m) return;
    _pinCtxState = {mode:mode, ctx:ctx, canvasX:canvasX, canvasY:canvasY};
    m.style.cssText = 'display:block;position:fixed;left:' + clientX + 'px;top:' + clientY + 'px;z-index:9000;'
      + 'background:#1a1d23;border:1px solid #2d3139;border-radius:6px;'
      + 'box-shadow:0 6px 20px rgba(0,0,0,0.7);min-width:140px;';
  }
  (function(){
    var addBtn = document.getElementById('cg-pin-ctx-add-btn');
    if (!addBtn) return;
    addBtn.addEventListener('click', function(){
      var m = document.getElementById('cg-pin-ctx-menu');
      if (m) m.style.display = 'none';
      var s = _pinCtxState;
      if (!s.mode) return;
      var pin = {id:_pinUid(), x:s.canvasX, y:s.canvasY, w:160, text:''};
      var pins = _pinsLoad(s.mode, s.ctx);
      pins.push(pin);
      _pinsSave(s.mode, s.ctx, pins);
      var containerId = s.mode === 'vf' ? 'cg-vf-canvas'
                      : s.mode === 'sv' ? 'cg-sv-canvas'
                      : 'cg-fn-pin-layer';
      var cont = document.getElementById(containerId);
      if (!cont && s.mode === 'fn') cont = document.getElementById('cg-fn-annot-layer');
      if (cont) _renderPinEl(cont, pin, s.mode, s.ctx);
    });
    document.addEventListener('click', function(e){
      var m = document.getElementById('cg-pin-ctx-menu');
      if (m && !m.contains(e.target)) m.style.display = 'none';
    });
    document.addEventListener('keydown', function(e){
      if (e.key === 'Escape') {
        var m = document.getElementById('cg-pin-ctx-menu');
        if (m) m.style.display = 'none';
      }
    });
  })();
  /* VF viewport pin right-click */
  (function(){
    var vp = document.getElementById('cg-vf-viewport');
    if (!vp) return;
    vp.addEventListener('contextmenu', function(e){
      if (e.target.closest('.cg-vf-node')||e.target.closest('.cg-vf-annot')||e.target.closest('.cg-pin')) return;
      e.preventDefault();
      var canvas = document.getElementById('cg-vf-canvas');
      var rect = canvas ? canvas.getBoundingClientRect() : vp.getBoundingClientRect();
      var ctx = _vfDeadMode ? '__dead__' : (_vfCurrentVar || '__global__');
      _showPinCtxMenu(e.clientX, e.clientY, 'vf', ctx,
        (e.clientX - rect.left) / _vfZoom - _vfPanX / _vfZoom,
        (e.clientY - rect.top)  / _vfZoom - _vfPanY / _vfZoom);
    });
  })();

  setTimeout(wire, 300);
})();
</script>
"""


# ------------------------------------------------------------------ #
# Renderer class                                                      #
# ------------------------------------------------------------------ #

class HtmlRenderer(BaseRenderer):
    # Per-slot aggregated graphs. The renderer fills the vis.js network from
    # the function-level `graph` always (so the network exists no matter what
    # either slot picks); slot1_graph / slot2_graph are emitted as separate
    # payloads consumed by the JS slot dispatcher.
    secondary_graph: "CallGraph | None" = None    # legacy alias for slot2_graph
    slot1_graph: "CallGraph | None" = None
    slot2_graph: "CallGraph | None" = None

    def render(self, graph: CallGraph, output_path: Path) -> Path:
        if not _PYVIS_AVAILABLE:
            raise ImportError("pyvis is not installed. Run: pip install pyvis")

        out = output_path.with_suffix(".html")
        layout = _compute_layout(graph)
        layout_key = _layout_key(graph)
        net, all_positions = self._build_network(graph, layout)
        raw_html = net.generate_html(notebook=False)
        full_html = self._inject_sidebar(raw_html, graph, all_positions, layout_key)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(full_html, encoding="utf-8")
        return out

    # Node counts above this threshold trigger large-graph rendering optimisations.
    _LARGE_GRAPH_THRESHOLD = 2000
    # Node counts above this threshold disable Function Nodes mode entirely
    # (vis.js becomes unusable). The UI auto-switches to Script Nodes view.
    _HUGE_GRAPH_THRESHOLD = 8000

    def _build_network(
        self, graph: CallGraph, layout: dict[str, tuple[float, float]]
    ) -> tuple["Network", dict[str, dict]]:
        cfg = self.config
        n_nodes = len(graph.functions)
        large_graph = n_nodes >= self._LARGE_GRAPH_THRESHOLD
        huge_graph = n_nodes >= self._HUGE_GRAPH_THRESHOLD

        net = Network(
            height="100%", width="100%",
            directed=True, notebook=False,
            cdn_resources="in_line",
        )

        # Huge graphs: skip populating the Pyvis network entirely. The HUGE_GRAPH
        # JS auto-redirects to Script Nodes view at page load, so the Function
        # canvas is never rendered — feeding it 15K+ nodes just costs MB of
        # JSON and several minutes of `net.generate_html` time for no gain.
        # We still emit *positions* for all nodes (needed by Script Mode +
        # Variable Flow modes) so the rest of the UI keeps working.
        all_positions: dict[str, dict] = {}
        for node_id, _fn in graph.functions.items():
            x, y = layout.get(node_id, (0.0, 0.0))
            all_positions[node_id] = {"x": x, "y": y}

        if huge_graph:
            vis_opts: dict = {
                "physics": {"enabled": False},
                "interaction": {"dragView": False, "zoomView": False, "hover": False},
                "edges": {"smooth": {"enabled": False}},
            }
            net.set_options(json.dumps(vis_opts))
            # Empty network — Script Mode / Variable Flow modes still have full data
            # via the injected JSON. _inject_sidebar handles the rest.
            return net, all_positions

        # Large graphs: straight edges are faster to render (no Bezier math per frame).
        # Also enable hideEdgesOnDrag / hideNodesOnDrag so panning stays responsive.
        edge_smooth = (
            {"enabled": False}
            if large_graph
            else {"enabled": True, "type": "cubicBezier",
                  "forceDirection": "vertical", "roundness": 0.4}
        )
        vis_opts: dict = {
            "physics": {"enabled": False},
            "interaction": {
                "dragNodes": True, "dragView": True, "zoomView": True,
                "hover": (not large_graph),   # hover lookup scans all nodes — skip for huge graphs
                "tooltipDelay": 9999,
                "hideEdgesOnDrag": large_graph,
                "hideEdgesOnZoom": large_graph,
            },
            "edges": {"smooth": edge_smooth},
        }
        net.set_options(json.dumps(vis_opts))

        entry_ids = set(cfg.filter.entry_points)

        for node_id, fn in graph.functions.items():
            color = EXTERNAL_COLOR if fn.is_external else LANG_COLORS.get(fn.language, LANG_COLORS[Language.PYTHON])
            label = _build_node_label(fn, cfg)
            is_entry = fn.name in entry_ids or fn.qualified_name in entry_ids
            border_color = ENTRY_BORDER if is_entry else color["border"]

            # Position already populated by the pre-loop above.
            pos = all_positions[node_id]
            x, y = pos["x"], pos["y"]

            net.add_node(
                node_id, label=label, title="",
                color={**color, "border": border_color},
                borderWidth=(3 if is_entry else 1),
                shape="box",
                font={"size": 11, "face": "monospace", "color": "#ffffff"},
                size=20, x=int(x), y=int(y), physics=False,
            )

        if cfg.variables.track:
            for node_id, fn in graph.functions.items():
                fn_x, fn_y = layout.get(node_id, (0.0, 0.0))
                for i, (var_name, var_value) in enumerate(fn.tracked_vars.items()):
                    ann_id = f"__var__{node_id}__{var_name}"
                    ann_x = fn_x + 160 + i * 70
                    ann_y = fn_y + 100
                    all_positions[ann_id] = {"x": ann_x, "y": ann_y}
                    net.add_node(
                        ann_id, label=f"{var_name}\n= {var_value[:30]}", title="",
                        color=VAR_COLOR, shape="diamond",
                        font={"size": 10, "face": "monospace", "color": "#333"},
                        size=12, x=int(ann_x), y=int(ann_y), physics=False,
                    )
                    net.add_edge(
                        node_id, ann_id, id=f"cg_var_edge_{node_id}_{i}", width=1, dashes=True,
                        title="VAR - tracked variable annotation",
                        arrows={"to": {"enabled": False}},
                        color={"color": "#C8B400", "opacity": 0.6},
                        category="var",
                    )

        for call_idx, call in enumerate(graph.calls):
            if not call.callee_id or call.callee_id not in graph.functions:
                continue
            cat = call.confidence_category or "exact"
            style = _EDGE_STYLE.get(cat, _EDGE_STYLE["exact"])
            tooltip = f"{cat.upper()} — {call.resolution_reason}" if call.resolution_reason else cat.upper()
            net.add_edge(
                call.caller_id, call.callee_id, id=f"cg_edge_{call_idx}",
                title=tooltip,
                dashes=style["dashes"], width=style["width"],
                arrows={"to": {"enabled": True, "scaleFactor": 0.6}},
                color={"color": style["color"], "opacity": style["opacity"]},
                category=cat,
            )

        return net, all_positions

    def _inject_sidebar(
        self,
        raw_html: str,
        graph: CallGraph,
        all_positions: dict[str, dict],
        layout_key: str,
    ) -> str:
        cfg = self.config
        stats = graph.stats()

        node_data = [
            {
                "id": node_id,
                "label": fn.qualified_name,
                "meta": {
                    "name":           fn.name,
                    "qualified_name": fn.qualified_name,
                    "language":       fn.language.display_name(),
                    "file_path":      fn.file_path,
                    "line_start":     fn.line_start,
                    "line_end":       fn.line_end,
                    "parameters":     [{"name": p.name, "type_hint": p.type_hint} for p in fn.parameters],
                    "return_type":    fn.return_type,
                    "parent":         fn.parent,
                    "is_external":    fn.is_external,
                    "is_method":      fn.is_method,
                    "func_type":      fn.func_type,
                    "docstring":      fn.docstring,
                    "tracked_vars":   fn.tracked_vars,
                    "variables":      [
                        {
                            "name": var.name,
                            "scope": var.scope,
                            "type_hint": var.type_hint,
                            "value": var.value,
                            "line": var.line,
                            "file_path": var.file_path,
                            "context": var.context,
                            "source_kind": var.source_kind,
                            "source_detail": var.source_detail,
                        }
                        for var in fn.variables
                    ],
                },
            }
            for node_id, fn in graph.functions.items()
        ]

        edge_data = [
            {
                "id": f"cg_edge_{call_idx}",
                "from": c.caller_id,
                "to": c.callee_id,
                "line": c.call_line,
                "args": c.call_args,
                "call_file": c.call_file,
                "callee_name": c.callee_name,
                "confidence": c.resolution_confidence.value,
                "category": c.confidence_category or "exact",
                "reason": c.resolution_reason or "",
                "underlying": c.underlying_count if c.underlying_count and c.underlying_count > 1 else None,
            }
            for call_idx, c in enumerate(graph.calls)
            if c.callee_id and c.callee_id in graph.functions
        ]

        # Build the complete node ID list (functions + var annotation nodes)
        all_node_ids: list[str] = list(graph.functions.keys())
        var_parent_map: dict[str, str] = {}
        if cfg.variables.track:
            for node_id, fn in graph.functions.items():
                for var_name in fn.tracked_vars:
                    ann_id = f"__var__{node_id}__{var_name}"
                    all_node_ids.append(ann_id)
                    var_parent_map[ann_id] = node_id

        errors_toggle = errors_html = err_class = ""
        if graph.parse_errors:
            errors_toggle = f'<span id="cg-err-toggle">{len(graph.parse_errors)} error(s) — expand</span>'
            errors_html = "".join(
                f'<div class="cg-err-item">{_sanitize(e)}</div>' for e in graph.parse_errors[:50]
            )
            err_class = "cg-stat-err"

        sidebar_html = _SIDEBAR_HTML.format(
            title=_sanitize("Function Call Graph"),
            stat_fn=stats["functions"], stat_calls=stats["calls"],
            stat_files=stats["files_parsed"], stat_errors=stats["parse_errors"],
            err_class=err_class, errors_toggle=errors_toggle, errors_html=errors_html,
            arrow_solid=_ARROW_SOLID, arrow_dashed=_ARROW_DASHED, arrow_var=_ARROW_VAR,
        )

        var_flow_data = _build_var_flow_data(graph)

        # Stable 12-char graph ID: SHA-1 of sorted function IDs.
        # Scopes all localStorage keys so different projects never share annotations.
        _fp = ",".join(sorted(graph.functions.keys()))
        graph_id = hashlib.sha1(_fp.encode()).hexdigest()[:12]

        n_nodes = stats["functions"]
        large_graph_flag = n_nodes >= HtmlRenderer._LARGE_GRAPH_THRESHOLD
        huge_graph_flag  = n_nodes >= HtmlRenderer._HUGE_GRAPH_THRESHOLD

        # Compact JSON: no indentation, no spaces. At 15K nodes this saves megabytes
        # off the embedded payload without changing semantics.
        _compact = lambda obj: json.dumps(obj, separators=(',', ':'))

        sidebar_js = (
            _SIDEBAR_JS
            .replace("CG_NODE_DATA",     _compact(node_data))
            .replace("CG_EDGE_DATA",     _compact(edge_data))
            .replace("CG_INITIAL_POS",   _compact(all_positions))
            .replace("CG_LAYOUT_KEY",    json.dumps(layout_key))
            .replace("CG_ALL_NODE_IDS",  _compact(all_node_ids))
            .replace("CG_VAR_PARENT",    _compact(var_parent_map))
            .replace("CG_VAR_FLOW_DATA", _compact(var_flow_data))
            .replace("CG_GRAPH_ID",      json.dumps(graph_id))
            .replace("CG_LARGE_GRAPH",   json.dumps(large_graph_flag))
            .replace("CG_HUGE_GRAPH",    json.dumps(huge_graph_flag))
            .replace("CG_HUGE_THRESHOLD", json.dumps(HtmlRenderer._HUGE_GRAPH_THRESHOLD))
        )

        detail_div = '<div id="cg-detail"></div>'
        modal_div = (
            '<div id="cg-modal">'
            '<div id="cg-modal-card">'
            '<button id="cg-modal-close" onclick="cgCloseModal()" title="Close (Esc)">&times;</button>'
            '<div id="cg-modal-body"></div>'
            '</div></div>'
        )

        varflow_div = (
            '<div id="cg-varflow-view">'
            '<div id="cg-vf-topbar">'
            '<h2>Variable Flow Mode</h2>'
            '<div id="cg-vf-topbar-row">'
            '<div id="cg-vf-search-wrap">'
            '<input id="cg-vf-search-input" type="text" placeholder="Search variable name…" autocomplete="off"/>'
            '<button id="cg-vf-search-clear" title="Clear">\xd7</button>'
            '<div id="cg-vf-dropdown"></div>'
            '</div>'
            '<button id="cg-vf-dead-btn" title="Show all unused/dead variables" onclick="_vfToggleDeadMode()">'
            '⛔ Dead Vars'
            '</button>'
            '<button id="cg-vf-annot-btn" title="Draw annotation rectangle (click then drag on canvas)" onclick="_vfToggleAnnotMode()">'
            '□ Annotate'
            '</button>'
            '</div>'
            '</div>'
            '<div id="cg-vf-graph-area">'
            '<div id="cg-vf-viewport">'
            '<div id="cg-vf-canvas">'
            '<svg id="cg-vf-svg" width="1" height="1" '
            'style="position:absolute;top:0;left:0;overflow:visible;pointer-events:none"></svg>'
            '</div>'
            '</div>'
            '<div id="cg-vf-placeholder">'
            'Search for a variable name above<br>'
            'to visualize its full lifecycle across the codebase.<br>'
            '<span style="font-size:12px;color:#3a4550">Double-click any block to see code details.</span>'
            '</div>'
            '</div>'
            '</div>'
            '<div id="cg-vf-modal">'
            '<div id="cg-vf-modal-card">'
            '<button id="cg-vf-modal-close" title="Close (Esc)">\xd7</button>'
            '<div id="cg-vf-modal-title"></div>'
            '<div id="cg-vf-modal-body"></div>'
            '</div>'
            '</div>'
            '<div id="cg-dead-modal">'
            '<div id="cg-dead-modal-card">'
            '<div id="cg-dead-modal-header">Dead Variable Report'
            '<button id="cg-dead-modal-close" onclick="cgCloseDeadReport()" title="Close">\xd7</button>'
            '</div>'
            '<div id="cg-dead-modal-body"></div>'
            '</div>'
            '</div>'
            '<div id="cg-vf-ctx-menu">'
            '<div class="cg-vf-ctx-item" id="cg-ctx-add-note">&#9998; Add note</div>'
            '<div class="cg-vf-ctx-item" id="cg-ctx-edit-note" style="display:none">&#9998; Edit note</div>'
            '<div class="cg-vf-ctx-sep"></div>'
            '<div class="cg-vf-ctx-item" id="cg-ctx-del-note" style="display:none">&#128465; Delete note</div>'
            '</div>'
            '<div id="cg-pin-ctx-menu">'
            '<div class="cg-vf-ctx-item" id="cg-pin-ctx-add-btn">&#128204; Add note here</div>'
            '</div>'
            '<div id="cg-annot-picker">'
            '<div id="cg-annot-picker-title">Annotation color</div>'
            '<div class="cg-ap-swatches" id="cg-ap-swatches"></div>'
            '<input type="text" id="cg-annot-picker-lbl" placeholder="Label (optional)" />'
            '<div class="cg-ap-size-row">'
            '<span class="cg-ap-size-lbl">Size:</span>'
            '<span class="cg-ap-size-btn" data-sz="13">S</span>'
            '<span class="cg-ap-size-btn" data-sz="18">M</span>'
            '<span class="cg-ap-size-btn selected" data-sz="24">L</span>'
            '<span class="cg-ap-size-btn" data-sz="32">XL</span>'
            '<span class="cg-ap-size-btn" data-sz="44">XXL</span>'
            '</div>'
            '<div class="cg-ap-btns">'
            '<button class="cg-ap-btn cg-ap-btn-cancel" id="cg-ap-cancel">Cancel</button>'
            '<button class="cg-ap-btn cg-ap-btn-ok" id="cg-ap-ok">OK</button>'
            '</div>'
            '</div>'
        )

        # ── Extras (post-v9.2): render slots, include graph, build info,
        #    architecture, confidence filter. All additive — existing IDs untouched.
        extras_payload = self._build_extras_payload(graph)

        html = raw_html.replace("</head>", _SIDEBAR_CSS + "\n" + _CGX_EXTRAS_CSS + "\n</head>", 1)
        html = html.replace(
            "<body>",
            "<body>\n" + sidebar_html + "\n"
            + '<div id="cg-script-view"></div>' + "\n"
            + varflow_div + "\n"
            + _CGX_EXTRAS_HTML + "\n"
            + detail_div + "\n" + modal_div,
            1,
        )
        extras_js = _CGX_EXTRAS_JS.replace("CGX_EXTRAS_DATA", json.dumps(extras_payload, separators=(",", ":")))
        html = html.replace("</body>", sidebar_js + "\n" + extras_js + "\n</body>", 1)
        return html

    # ------------------------------------------------------------------ #
    # Extras payload (build info, modules, violations, include graph,    #
    # render-slot config, secondary node/edge data for slot 2)            #
    # ------------------------------------------------------------------ #
    def _build_extras_payload(self, graph: CallGraph) -> dict:
        cfg = self.config
        bi = graph.build_info

        build_info_dict = None
        if bi is not None:
            build_info_dict = {
                "source": bi.source,
                "compile_commands_path": bi.compile_commands_path,
                "unit_count": len(bi.units),
                "configuration": bi.configuration,
                "platform": bi.platform,
                "projects": bi.projects,
                "files_not_in_compile_commands": bi.files_not_in_compile_commands[:200],
                "cc_files_not_found": bi.cc_files_not_found[:200],
                "global_define_count": len(bi.global_defines),
                "global_include_count": len(bi.global_includes),
            }

        modules_dict = {
            name: {
                "name": name,
                "inferred_from": m.inferred_from,
                "project": m.project,
                "file_count": len(m.files),
                "files": list(m.files)[:50],
            }
            for name, m in graph.modules.items()
        }

        violations_list = [
            {
                "kind": v.rule_kind,
                "from": v.from_module,
                "to": v.to_module,
                "reason": v.reason,
                "sample_edges": v.sample_edges[:5],
            }
            for v in graph.violations
        ]

        include_dict = None
        if graph.include_graph is not None:
            ig = graph.include_graph
            include_dict = {
                "files": {
                    f: [
                        {
                            "from": e.from_file,
                            "to": e.to_file,
                            "is_system": e.is_system,
                            "resolved": e.resolved,
                            "raw": e.raw_target,
                            "line": e.line,
                        }
                        for e in edges
                    ]
                    for f, edges in ig.files.items()
                },
                "unresolved_count": len(ig.unresolved),
                "cycles": ig.cycles[:25],
                "most_included": ig.most_included,
            }

        # Per-slot payloads. Each slot may be at a different render level. The
        # JS dispatcher reads `slot.level`, decides which view container to
        # show (network / script-cards / module-cards / include), and loads
        # the slot's nodes+edges into that view.
        def _serialize_graph(sg: "CallGraph | None") -> dict | None:
            if sg is None:
                return None
            nodes = [
                {
                    "id": nid,
                    "label": fn.qualified_name,
                    "meta": {
                        "name": fn.name,
                        "qualified_name": fn.qualified_name,
                        "language": fn.language.display_name(),
                        "file_path": fn.file_path,
                        "line_start": fn.line_start,
                        "line_end": fn.line_end,
                        "is_external": fn.is_external,
                        "func_type": fn.func_type,
                        "tracked_vars": fn.tracked_vars,
                    },
                }
                for nid, fn in sg.functions.items()
            ]
            edges = [
                {
                    "id": f"slot_edge_{i}",
                    "from": c.caller_id,
                    "to": c.callee_id,
                    "category": c.confidence_category or "aggregated",
                    "reason": c.resolution_reason or "",
                    "underlying": c.underlying_count if c.underlying_count and c.underlying_count > 1 else None,
                }
                for i, c in enumerate(sg.calls)
                if c.callee_id and c.callee_id in sg.functions
            ]
            return {"nodes": nodes, "edges": edges}

        slot1_data = _serialize_graph(self.slot1_graph) if self.slot1_graph is not None else None
        slot2_data = _serialize_graph(self.slot2_graph) if self.slot2_graph is not None else None
        # Legacy: callers using the older `secondary_graph` kwarg still get the
        # old behaviour (data routed to slot 2). New callers should use the
        # explicit slot1/slot2 kwargs.
        if slot2_data is None and self.secondary_graph is not None:
            slot2_data = _serialize_graph(self.secondary_graph)

        return {
            "slot_1": cfg.render.view_slot_1,
            "slot_2": cfg.render.view_slot_2,
            "slot_labels": {
                "slot_1": _RENDER_LEVEL_LABELS.get(cfg.render.view_slot_1, cfg.render.view_slot_1),
                "slot_2": _RENDER_LEVEL_LABELS.get(cfg.render.view_slot_2, cfg.render.view_slot_2),
            },
            "build_info": build_info_dict,
            "modules": modules_dict,
            "violations": violations_list,
            "include_graph": include_dict,
            # Per-slot payloads: keys are "slot1" / "slot2"; each is {nodes, edges}.
            "slots": {
                "slot1": slot1_data,
                "slot2": slot2_data,
            },
            # Legacy alias — the previous version of the extras JS read this.
            "secondary": slot2_data,
            "include_graph_enabled": cfg.include_graph.enabled and include_dict is not None,
            "include_show_system": bool(cfg.include_graph.follow_system),
        }


# ====================================================================== #
# Extras (post-v9.2) — render slots / include graph / build info /        #
# architecture / confidence filter. Injected into the page alongside the  #
# existing sidebar; existing IDs are never overwritten.                   #
# ====================================================================== #

_CGX_EXTRAS_CSS = r"""
<style id="cgx-extras-css">
.cgx-section { padding: 10px 12px; border-bottom: 1px solid #2d3139; }
.cgx-section label { display: block; color: #6a7a8a; font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
.cgx-row { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #cfd6dd; margin: 3px 0; }
.cgx-pill { display: inline-block; padding: 1px 6px; border-radius: 8px;
  font-size: 10px; font-weight: bold; }
.cgx-pill-exact      { background: #1a3a5a; color: #74b3f7; }
.cgx-pill-heuristic  { background: #4a3010; color: #ffb56a; }
.cgx-pill-unresolved { background: #2a2d33; color: #b0b8c0; }
.cgx-pill-external   { background: #2a2d33; color: #b0b8c0; }
.cgx-pill-aggregated { background: #122a4f; color: #6aa9ff; }
.cgx-pill-violation  { background: #4a1010; color: #ff8585; }
.cgx-violation-line { background: #2a1414; color: #ff8585; padding: 4px 6px; border-radius: 4px;
  margin: 2px 0; font-size: 11px; cursor: pointer; }
.cgx-violation-line:hover { background: #3a1818; }
.cgx-modal {
  display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
  background: rgba(0,0,0,0.6); z-index: 9000; align-items: center; justify-content: center;
}
.cgx-modal.open { display: flex; }
.cgx-modal-card {
  background: #1a1d23; color: #cfd6dd; width: min(720px, 90vw); max-height: 80vh;
  overflow: auto; border: 1px solid #2d3139; border-radius: 6px; padding: 18px;
}
.cgx-modal-card h3 { margin: 0 0 8px 0; }
.cgx-modal-card table { width: 100%; border-collapse: collapse; font-size: 12px; }
.cgx-modal-card th, .cgx-modal-card td { padding: 4px 6px; border-bottom: 1px solid #2d3139; text-align: left; }
.cgx-modal-card th { color: #6a7a8a; font-weight: normal; text-transform: uppercase; font-size: 10px; }
.cgx-cb { margin-right: 4px; }
.cgx-btn-link { background: none; border: none; color: #74b3f7; cursor: pointer; padding: 0; font-size: 12px; }
.cgx-btn-link:hover { text-decoration: underline; }
.cgx-collapsible-body { display: none; }
.cgx-collapsible.open .cgx-collapsible-body { display: block; }
.cgx-collapsible-toggle { cursor: pointer; user-select: none; color: #cfd6dd; }
.cgx-collapsible-toggle::before { content: '▸ '; color: #6a7a8a; }
.cgx-collapsible.open .cgx-collapsible-toggle::before { content: '▾ '; }
/* ── Include Graph view (nodes + edges canvas) ────────────────────────────── */
#cgx-inc-view {
  display: none; flex: 1; height: 100vh; position: relative;
  background: #334155; color: #cfd6dd; overflow: hidden;
  flex-direction: column;
}
#cgx-inc-view.cg-iv-active { display: flex; }

/* top toolbar */
#cg-iv-toolbar {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 7px 12px; border-bottom: 1px solid #2d3a50;
  background: rgba(20,28,40,0.95); flex: 0 0 auto; position: relative; z-index: 4;
}
#cg-iv-toolbar h3 { margin: 0; color: #74b3f7; font-size: 14px; flex: 0 0 auto; }
.cg-iv-stat { font-size: 11px; color: #8fa5c0; background: rgba(30,45,70,0.7);
  border-radius: 10px; padding: 2px 8px; white-space: nowrap; }
.cg-iv-stat b { color: #cfd6dd; }
.cg-iv-filters { display: flex; gap: 8px; margin-left: auto; align-items: center; flex-wrap: wrap; }
.cg-iv-filter-lbl { font-size: 11px; color: #8fa5c0; cursor: pointer;
  display: flex; align-items: center; gap: 4px; user-select: none; }
.cg-iv-filter-lbl input { accent-color: #74b3f7; }

#cg-iv-search {
  padding: 3px 8px; font-size: 12px; background: #1e2229; color: #cfd6dd;
  border: 1px solid #2d3139; border-radius: 4px; outline: none; width: 160px;
}
#cg-iv-search:focus { border-color: #74b3f7; }
#cgx-inc-close {
  background: #1e2229; color: #cfd6dd; border: 1px solid #2d3139;
  padding: 3px 10px; border-radius: 4px; cursor: pointer; font-size: 12px;
}
#cgx-inc-close:hover { background: #2d3a50; color: #fff; }

/* legend bar */
#cg-iv-legend {
  display: flex; align-items: center; gap: 12px; padding: 4px 14px;
  background: rgba(20,28,40,0.7); border-bottom: 1px solid #2d3a50; font-size: 11px;
  color: #8fa5c0; flex: 0 0 auto;
}
.cg-iv-leg { display: flex; align-items: center; gap: 4px; }
.cg-iv-leg-dot { width: 10px; height: 10px; border-radius: 2px; flex: 0 0 auto; }

/* canvas + side panel row */
#cg-iv-body { display: flex; flex: 1 1 0; overflow: hidden; position: relative; }

/* pan/zoom canvas */
#cg-iv-canvas {
  flex: 1 1 0; position: relative; overflow: hidden;
  cursor: grab; user-select: none;
}
#cg-iv-canvas.iv-panning { cursor: grabbing; }
#cg-iv-canvas-inner { position: absolute; top: 0; left: 0; transform-origin: 0 0; }
#cg-iv-arrows { position: absolute; top: 0; left: 0; overflow: visible; pointer-events: none; }

/* header nodes */
.cg-iv-node {
  position: absolute; width: 190px;
  border-radius: 6px; padding: 7px 10px;
  font-size: 11px; cursor: grab;
  border: 1px solid transparent;
  box-shadow: 0 2px 6px rgba(0,0,0,0.35);
  transition: filter 0.12s, border-color 0.12s;
  user-select: none; pointer-events: all;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.cg-iv-node:hover { filter: brightness(1.3); border-color: rgba(255,255,255,0.35) !important; }
.cg-iv-node.iv-selected { border-color: #74b3f7 !important; box-shadow: 0 0 0 2px rgba(116,179,247,0.4); }
.cg-iv-node.iv-multi-selected { border-color: #F7D774 !important; box-shadow: 0 0 0 2px rgba(247,215,116,0.35); }
/* status colours */
.cg-iv-node.st-ok    { background: #1f3028; border-color: #3a7a52; color: #7dda9d; }
.cg-iv-node.st-unres { background: #2e1e0a; border-color: #8a5010; color: #ffb56a;
                        border-style: dashed; opacity: 0.88; }
.cg-iv-node.st-cycle { background: #2a1010; border-color: #8a2020; color: #ff8080; }
.cg-iv-node.st-mixed { background: #252012; border-color: #7a6a10; color: #e8d06e; }
.cg-iv-node.st-sys   { background: #1a1e2a; border-color: #3a4460; color: #7a90b8;
                        border-style: dotted; opacity: 0.80; font-style: italic; }
/* cycle left bar */
.cg-iv-node.st-cycle::before {
  content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
  background: #ff4040; border-radius: 6px 0 0 6px;
}
.cg-iv-node-label { font-weight: 600; font-size: 12px; overflow: hidden; text-overflow: ellipsis; }
.cg-iv-node-sub   { font-size: 9px; color: rgba(255,255,255,0.35); overflow: hidden;
                    text-overflow: ellipsis; margin-top: 1px; }
.cg-iv-node-badge { position: absolute; top: 4px; right: 6px;
                    font-size: 9px; color: rgba(255,255,255,0.4); }

/* side panel */
#cg-iv-panel {
  width: 300px; flex: 0 0 300px; overflow-y: auto;
  border-left: 1px solid #2d3a50; background: #1a2535;
  display: none; flex-direction: column;
}
#cg-iv-panel.open { display: flex; }
#cg-iv-panel-header {
  padding: 8px 12px; border-bottom: 1px solid #2d3a50;
  background: rgba(20,28,40,0.9); position: sticky; top: 0; z-index: 2;
}
#cg-iv-panel-header h4 { margin: 0 0 2px 0; font-size: 12px; color: #74b3f7;
  overflow-wrap: break-word; word-break: break-all; }
#cg-iv-panel-header .cg-iv-path-sub { font-size: 10px; color: #6a7a8a; }
#cg-iv-panel-close {
  float: right; background: none; border: none; color: #6a7a8a;
  cursor: pointer; font-size: 16px; padding: 0 4px; line-height: 1;
}
#cg-iv-panel-close:hover { color: #fff; }
.cg-iv-section { padding: 8px 12px; border-bottom: 1px solid #232f40; }
.cg-iv-section-title {
  font-size: 10px; color: #5a7090; text-transform: uppercase; letter-spacing: .07em;
  margin-bottom: 5px;
}
.cg-iv-edge-item {
  font-size: 11px; padding: 2px 0; display: flex; align-items: baseline; gap: 5px;
  overflow-wrap: break-word; word-break: break-all;
}
.cg-iv-edge-item .cg-iv-line { font-size: 9px; color: #5a7090; flex: 0 0 auto; }
.cg-iv-edge-ok  { color: #7dda9d; cursor: pointer; }
.cg-iv-edge-ok:hover { text-decoration: underline; }
.cg-iv-edge-unr { color: #ffb56a; text-decoration: line-through; cursor: pointer; }
.cg-iv-edge-unr:hover { text-decoration: none; color: #ffd09a; }
.cg-iv-edge-sys { color: #6a7a8a; }
.cg-iv-edge-cyc { color: #ff7070; }
.cg-iv-cycle-path {
  font-size: 11px; color: #ff9090; background: #2a1010;
  border-radius: 4px; padding: 6px 8px; margin-top: 4px;
  word-break: break-all; line-height: 1.6;
}
.cg-iv-cycle-arr { color: #ff5050; }
.cg-iv-backref-item { font-size: 11px; color: #9fc0e0; padding: 2px 0;
  cursor: pointer; overflow-wrap: break-word; word-break: break-all; }
.cg-iv-backref-item:hover { color: #c8e0ff; text-decoration: underline; }
.cg-iv-empty-note { font-size: 11px; color: #5a7090; font-style: italic; }

/* ---------- Module View (shares Script View's visual foundation) ---------- */
#cg-module-view {
  display: none; flex: 1; height: 100vh;
  overflow: hidden; background: #334155;   /* matches #cg-script-view */
  position: relative;
  color: #cfd6dd;
}
#cg-module-view.active { display: block; }
#cgx-mv-header {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 12px; border-bottom: 1px solid #2d3139;
  position: sticky; top: 0; background: rgba(20,28,40,0.95);
  z-index: 5;
}
#cgx-mv-header h3 { margin: 0; color: #e0e0e0; flex: 0 0 auto; font-size: 14px; }
#cgx-mv-empty {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
  color: #6a7a8a; font-size: 14px; text-align: center; max-width: 480px;
}
.cgx-mv-search-wrap { position: relative; flex: 1; min-width: 220px; }
.cgx-mv-search {
  width: 100%; padding: 5px 28px 5px 8px; box-sizing: border-box;
  background: #1e2229; color: #fff; border: 1px solid #2d3139;
  border-radius: 4px; font-size: 12px;
}
.cgx-mv-clear {
  position: absolute; right: 4px; top: 50%; transform: translateY(-50%);
  background: none; border: none; color: #6a7a8a; cursor: pointer;
  font-size: 14px; padding: 0 6px;
}
.cgx-mv-clear:hover { color: #e0e0e0; }
#cgx-mv-dropdown {
  display: none; position: absolute; top: 100%; left: 0; right: 0;
  background: #1a1d23; border: 1px solid #2d3139; border-top: none;
  max-height: 320px; overflow-y: auto; z-index: 100;
}
.cgx-mv-dd-item {
  padding: 4px 10px; cursor: pointer; font-size: 12px; color: #cfd6dd;
  display: flex; gap: 8px; border-bottom: 1px solid #2d3139;
}
.cgx-mv-dd-item:last-child { border-bottom: none; }
.cgx-mv-dd-item:hover { background: #1e3050; }
.cgx-mv-dd-kind { color: #6a7a8a; font-size: 10px; min-width: 60px; text-transform: uppercase; }
.cgx-mv-dd-name { font-family: monospace; flex: 1; }
.cgx-mv-dd-parent { color: #6a7a8a; font-size: 10px; }
#cgx-mv-canvas {
  position: absolute; top: 44px; right: 0; bottom: 0; left: 0;
  overflow: auto;
  cursor: grab;
}
#cgx-mv-canvas.mv-panning { cursor: grabbing; }
#cgx-mv-canvas-inner {
  position: relative;
  min-width: 1600px; min-height: 800px;
}
/* Cards use the same visual style as .cg-file-card (script view) so the
 * mode feels like part of the same family. */
.cgx-mv-card {
  position: absolute;
  background: #1f242c;
  border: 1px solid #3a4250;
  border-radius: 6px;
  width: 290px;
  min-height: 50px;
  font-size: 12px;
  box-shadow: 0 2px 6px rgba(0,0,0,0.35);
  color: #e0e0e0;
}
.cgx-mv-card.matched { border-color: #27AE60; box-shadow: 0 0 0 2px rgba(39, 174, 96, 0.35); }
.cgx-mv-card.dim    { opacity: 0.35; }
.cgx-mv-card.mv-multi-selected { border-color: #F7D774; box-shadow: 0 0 0 2px rgba(247,215,116,0.35); }
.cgx-mv-head {
  display: flex; align-items: center; gap: 6px; padding: 6px 10px;
  background: #2a3140; border-radius: 5px 5px 0 0;
  border-bottom: 1px solid #3a4250;
  cursor: pointer;
  user-select: none;
}
.cgx-mv-head:hover { background: #34405a; }
.cgx-mv-toggle {
  display: inline-block; width: 12px; color: #74b3f7;
  font-size: 11px;
}
.cgx-mv-card.open > .cgx-mv-head > .cgx-mv-toggle::before { content: '▾'; }
.cgx-mv-card           > .cgx-mv-head > .cgx-mv-toggle::before { content: '▸'; }
.cgx-mv-name { font-weight: 700; color: #e0e0e0; flex: 1; font-size: 13px; }
.cgx-mv-badge {
  color: #b0b8c0; font-size: 10px; padding: 1px 5px; border-radius: 3px;
  background: rgba(0,0,0,0.2);
}
.cgx-mv-summary {
  display: flex; flex-wrap: wrap; gap: 10px;
  padding: 5px 10px 7px; color: #b0b8c0; font-size: 11px;
}
.cgx-mv-summary span { white-space: nowrap; }
.cgx-mv-body { display: none; padding: 0 6px 6px; }
.cgx-mv-card.open > .cgx-mv-body { display: block; }
.cgx-mv-file {
  border-top: 1px solid rgba(255,255,255,0.05);
  padding: 2px 0;
}
.cgx-mv-file-head {
  display: flex; gap: 6px; align-items: center;
  padding: 3px 6px; cursor: pointer; border-radius: 3px;
}
.cgx-mv-file-head:hover { background: rgba(116, 179, 247, 0.1); }
.cgx-mv-file.open > .cgx-mv-file-head > .cgx-mv-ftoggle::before { content: '▾'; color:#74b3f7; }
.cgx-mv-file           > .cgx-mv-file-head > .cgx-mv-ftoggle::before { content: '▸'; color:#74b3f7; }
.cgx-mv-fname { font-family: monospace; color: #e0e0e0; flex: 1; font-size: 11px; }
.cgx-mv-fmeta { color: #6a7a8a; font-size: 10px; }
.cgx-mv-fns { display: none; padding: 2px 4px 2px 20px; }
.cgx-mv-file.open > .cgx-mv-fns { display: block; }
.cgx-mv-fn {
  padding: 2px 6px; cursor: pointer; border-radius: 3px;
  color: #cfd6dd; font-family: monospace; font-size: 11px;
}
.cgx-mv-fn:hover { background: #1e3050; color: #fff; }
#cgx-mv-arrows {
  position: absolute; top: 0; left: 0;
  /* JS owns concrete SVG width/height. Keeping CSS width/height at 100%
   * scales the viewBox and makes arrow endpoints drift away from cards. */
  overflow: visible;
  pointer-events: auto;
}
.cgx-mv-fn.matched,
.cgx-mv-file-head.matched { background: #1e3050; color: #fff; box-shadow: inset 0 0 0 1px rgba(39,174,96,0.55); }
.cgx-mv-card.dim { opacity: 0.35; }
.cgx-mv-arrow-label {
  fill: #b0b8c0; font-size: 9px; font-family: monospace;
}

/* Error overlay (graceful fallback when a mode crashes) */
.cgx-error-overlay {
  display: none; position: absolute; top: 0; right: 0; bottom: 0; left: 320px;
  background: rgba(20, 28, 40, 0.92); color: #ffb56a; z-index: 9500;
  align-items: center; justify-content: center; flex-direction: column; gap: 12px;
  padding: 24px; text-align: center;
}
.cgx-error-overlay.open { display: flex; }
.cgx-error-overlay h3 { color: #ffb56a; margin: 0; font-size: 18px; }
.cgx-error-overlay code { color: #cfd6dd; background: rgba(0,0,0,0.35); padding: 2px 6px;
  border-radius: 3px; font-size: 11px; max-width: 720px; word-wrap: break-word; }
.cgx-error-overlay .cgx-error-actions { display: flex; gap: 10px; }

/* ── Light theme: module / include view ── */
body[data-theme="light"] #cg-module-view { background: #dde3ea; color: #1a2535; }
body[data-theme="light"] #cgx-mv-header { background: rgba(240,245,250,0.97); border-bottom-color: #c8d4de; }
body[data-theme="light"] #cgx-mv-header h3 { color: #1a2535; }
body[data-theme="light"] #cgx-mv-empty { color: #7a8898; }
body[data-theme="light"] .cgx-mv-search { background: #fff; border-color: #9eb4c8; color: #1a2535; }
body[data-theme="light"] .cgx-mv-clear { color: #7a8898; }
body[data-theme="light"] .cgx-mv-clear:hover { color: #1a2535; }
body[data-theme="light"] #cgx-mv-dropdown { background: #fff; border-color: #c8d4de; }
body[data-theme="light"] .cgx-mv-dd-item { color: #1a2535; border-bottom-color: #e5edf4; }
body[data-theme="light"] .cgx-mv-dd-item:hover { background: #dbeafe; }
body[data-theme="light"] .cgx-mv-dd-kind,
body[data-theme="light"] .cgx-mv-dd-parent { color: #7a8898; }
body[data-theme="light"] .cgx-mv-card { background: #fff; border-color: #c8d4de; box-shadow: 0 2px 6px rgba(0,0,0,0.08); color: #1a2535; }
body[data-theme="light"] .cgx-mv-head { background: #edf2f7; border-bottom-color: #c8d4de; }
body[data-theme="light"] .cgx-mv-head:hover { background: #e2eaf2; }
body[data-theme="light"] .cgx-mv-name { color: #1a2535; }
body[data-theme="light"] .cgx-mv-summary { color: #4a6070; }
body[data-theme="light"] .cgx-mv-badge { background: rgba(0,0,0,0.06); color: #4a6070; }
body[data-theme="light"] .cgx-mv-fname { color: #1a2535; }
body[data-theme="light"] .cgx-mv-fmeta { color: #7a8898; }
body[data-theme="light"] .cgx-mv-fn { color: #2c4455; }
body[data-theme="light"] .cgx-mv-fn:hover { background: #dbeafe; color: #1d4ed8; }
body[data-theme="light"] .cgx-mv-fn.matched,
body[data-theme="light"] .cgx-mv-file-head.matched { background: #dbeafe; color: #1d4ed8; }
body[data-theme="light"] .cgx-mv-file { border-top-color: rgba(0,0,0,0.07); }
body[data-theme="light"] .cgx-mv-file-head:hover { background: rgba(59,130,246,0.08); }
body[data-theme="light"] .cgx-mv-arrow-label { fill: #4a6070; }
body[data-theme="light"] #cgx-inc-view { background: #dde3ea; color: #1a2535; }
body[data-theme="light"] #cg-iv-toolbar { border-bottom-color: #c8d4de; }
body[data-theme="light"] #cgx-inc-close { background: #edf2f7; border-color: #c8d4de; color: #1a2535; }
body[data-theme="light"] #cgx-inc-close:hover { background: #dde6f0; color: #1a2535; }
body[data-theme="light"] #cg-iv-legend { background: rgba(240,245,250,0.93); border-bottom-color: #c8d4de; color: #4a6070; }
body[data-theme="light"] .cgx-error-overlay { background: rgba(240,245,250,0.96); color: #92400e; }
body[data-theme="light"] .cgx-error-overlay h3 { color: #92400e; }
body[data-theme="light"] #cgx-kbd-hint { background: #f4f7fa; color: #4a6070; border-color: #c8d4de; }
body[data-theme="light"] #cgx-kbd-hint:hover::after { background: #fff; color: #1a2535; border-color: #c8d4de; }
</style>
"""


_CGX_EXTRAS_HTML = r"""
<!-- Shared arrow-marker defs (single source of truth for the 3-type Edge Legend).
     Every card-based view references these via marker-end="url(#cg-arr-*)" so
     all arrows look identical across Function / Script / Module / Folder /
     Library / Namespace modes. -->
<svg id="cg-arrow-defs" style="position:absolute;width:0;height:0;overflow:hidden" aria-hidden="true">
  <defs>
    <marker id="cg-arr-solid"  markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto">
      <path d="M0,1 L9,5 L0,9 Z" fill="#6c8ebf"/>
    </marker>
    <marker id="cg-arr-dashed" markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto">
      <path d="M0,1 L9,5 L0,9 Z" fill="#888"/>
    </marker>
    <marker id="cg-arr-var"    markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto">
      <path d="M0,1 L9,5 L0,9 Z" fill="#C8B400"/>
    </marker>
  </defs>
</svg>
<div id="cgx-inc-view"></div>
<div id="cg-module-view">
  <div id="cgx-mv-header">
    <h3 id="cgx-mv-title">Module View</h3>
    <div class="cgx-mv-search-wrap">
      <input type="text" id="cgx-mv-search" class="cgx-mv-search"
             placeholder="Click to browse · type to filter" autocomplete="off"/>
      <button class="cgx-mv-clear" id="cgx-mv-search-clear" title="Clear">×</button>
      <div id="cgx-mv-dropdown"></div>
    </div>
  </div>
  <div id="cgx-mv-canvas">
    <div id="cgx-mv-canvas-inner">
      <svg id="cgx-mv-arrows" xmlns="http://www.w3.org/2000/svg"></svg>
    </div>
    <div id="cgx-mv-empty" style="display:none">No data for this view.</div>
  </div>
</div>
<div id="cgx-error-overlay" class="cgx-error-overlay">
  <h3 id="cgx-error-title">Mode error</h3>
  <div id="cgx-error-msg" style="max-width:640px"></div>
  <code id="cgx-error-detail"></code>
  <div class="cgx-error-actions">
    <button class="cg-btn" id="cgx-error-back">Back to Slot 1</button>
    <button class="cg-btn" id="cgx-error-close">Dismiss</button>
  </div>
</div>
<div id="cgx-arch-modal" class="cgx-modal">
  <div class="cgx-modal-card">
    <button class="cg-btn" style="float:right" onclick="cgxCloseArchModal()">&times;</button>
    <h3>Architecture Violations</h3>
    <div id="cgx-arch-modal-body"></div>
  </div>
</div>
<div id="cgx-mod-modal" class="cgx-modal">
  <div class="cgx-modal-card">
    <button class="cg-btn" style="float:right" onclick="cgxCloseModModal()">&times;</button>
    <h3 id="cgx-mod-modal-title">Module</h3>
    <div id="cgx-mod-modal-body"></div>
  </div>
</div>
<div id="cgx-kbd-hint" title="Keyboard shortcuts">?</div>
"""


_CGX_EXTRAS_JS = r"""
<script id="cgx-extras-js">
(function(){
  var CGX = CGX_EXTRAS_DATA;
  if (!CGX) return;
  var NODE_DATA = window.CGX_NODE_DATA || [];
  var EDGE_DATA = window.CGX_EDGE_DATA || [];

  /* ---------- rect helpers (defined in _SIDEBAR_JS, exposed globally) ---------- */
  var _mkRect        = window._mkRect        || function(a,b){return{left:Math.min(a.x,b.x),top:Math.min(a.y,b.y),right:Math.max(a.x,b.x),bottom:Math.max(a.y,b.y)};};
  var _rectIntersects = window._rectIntersects || function(r1,r2){return !(r1.right<r2.left||r1.left>r2.right||r1.bottom<r2.top||r1.top>r2.bottom);};
  var _setBoxRect    = window._setBoxRect    || function(box,r){if(!box)return;box.style.left=r.left+'px';box.style.top=r.top+'px';box.style.width=Math.max(0,r.right-r.left)+'px';box.style.height=Math.max(0,r.bottom-r.top)+'px';};


  /* ---------- utilities ---------- */
  function _findSidebar() { return document.getElementById('cg-sidebar'); }
  function _h(tag, attrs, html) {
    var el = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        if (k === 'class') el.className = attrs[k];
        else if (k === 'onclick') el.onclick = attrs[k];
        else el.setAttribute(k, attrs[k]);
      }
    }
    if (html !== undefined) el.innerHTML = html;
    return el;
  }
  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function _cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/([^\w-])/g, '\\$1');
  }
  function _basename(p) { if (!p) return ''; var s = String(p); return s.replace(/^.*[\\\/]/, ''); }
  function _cgxGetNet() {
    try {
      var n = window.network;
      return (n && n.body) ? n : null;
    } catch(e) { return null; }
  }

  /* ---------- slot / mode wiring ---------- */
  var btnFn = document.getElementById('cg-btn-mode-fn');
  var btnSv = document.getElementById('cg-btn-mode-sv');
  var btnVf = document.getElementById('cg-btn-mode-vf');
  if (btnFn && CGX.slot_labels && CGX.slot_labels.slot_1) btnFn.textContent = CGX.slot_labels.slot_1;
  if (btnSv && CGX.slot_labels && CGX.slot_labels.slot_2) btnSv.textContent = CGX.slot_labels.slot_2;
  var btnInc = null;
  if (CGX.include_graph_enabled && btnVf && btnVf.parentElement) {
    btnInc = _h('button', {'class':'cg-btn','id':'cgx-btn-mode-inc'}, 'Include Graph');
    btnVf.parentElement.appendChild(btnInc);
  }

  /* Map render level -> internal mode id used by setViewMode.
   * function       -> 'fn'      (vis.js network)
   * script         -> 'script'  (file cards, original behaviour)
   * folder         -> 'module'  (hierarchy cards, Folder → File → Function)
   * module/lib/ns  -> 'module'  (hierarchy cards, same renderer)
   */
  var LEVEL_TO_MODE = {
    function:  'fn',
    script:    'script',
    folder:    'module',
    module:    'module',
    library:   'module',
    namespace: 'module'
  };

  var SLOTS = {
    slot1: { level: CGX.slot_1 || 'function', payload: CGX.slots && CGX.slots.slot1, btn: btnFn },
    slot2: { level: CGX.slot_2 || 'script',   payload: CGX.slots && CGX.slots.slot2, btn: btnSv }
  };
  var _activeLevel = null;     // last activated render-level
  var _activeSlotId = 'slot1'; // last slot the user activated
  var _cgxCurrentMode = 'fn';  // mode owned by this extras runtime (fn/script/module/varflow/inc)

  /* ---------- SHARED edge-style + edge-filter state ----------
   * The 3-type Edge Legend is the single source of truth for arrow visuals.
   * Internal categories map to exactly 3 visual buckets:
   *   confirmed  = exact | aggregated                  (blue solid)
   *   probable   = heuristic | unresolved | external | violation  (gray dashed)
   *   var        = variable-annotation overlay         (yellow dashed)
   * Renderers call window.cgEdgeStyle(category) to obtain {stroke, dash, marker, legendKey}
   * and read window.cgEdgeFilter[legendKey] to decide visibility. */
  function cgEdgeCategoryToLegend(cat) {
    if (cat === 'exact' || cat === 'aggregated') return 'confirmed';
    if (cat === 'var' || cat === 'variable' || cat === 'var-annotation') return 'var';
    return 'probable';   /* heuristic, unresolved, external, violation, anything else */
  }
  window.cgEdgeCategoryToLegend = cgEdgeCategoryToLegend;

  window.cgEdgeStyle = function(category) {
    var key = cgEdgeCategoryToLegend(category);
    if (key === 'confirmed') return { stroke:'#6c8ebf', dash:'',    width:1.6, marker:'cg-arr-solid',  legendKey:'confirmed' };
    if (key === 'var')       return { stroke:'#C8B400', dash:'3,3', width:1.2, marker:'cg-arr-var',    legendKey:'var'       };
    return                          { stroke:'#888',    dash:'5,3', width:1.5, marker:'cg-arr-dashed', legendKey:'probable' };
  };

  var EDGE_FILTER_KEY = 'cg_edge_filter_' + (typeof GRAPH_ID === 'string' ? GRAPH_ID : 'default');
  var _LEGEND_KEYS = ['confirmed','probable','var'];

  /* Shared filter state on window so every renderer can read it directly. */
  window.cgEdgeFilter = (function(){
    var out = {};
    try {
      var stored = localStorage.getItem(EDGE_FILTER_KEY);
      if (stored) out = JSON.parse(stored) || {};
    } catch(e) {}
    _LEGEND_KEYS.forEach(function(k){ if (typeof out[k] !== 'boolean') out[k] = true; });
    return out;
  })();

  /* Back-compat shim for any caller that still reads the old per-category map. */
  var _edgeFilterState = (function(){
    var s = {};
    function fill(){
      s.exact      = !!window.cgEdgeFilter.confirmed;
      s.aggregated = !!window.cgEdgeFilter.confirmed;
      s.heuristic  = !!window.cgEdgeFilter.probable;
      s.unresolved = !!window.cgEdgeFilter.probable;
      s.external   = !!window.cgEdgeFilter.probable;
      s.violation  = !!window.cgEdgeFilter.probable;
      s.var        = !!window.cgEdgeFilter.var;
    }
    fill();
    document.addEventListener('cg:edge-filter:change', fill);
    return s;
  })();

  function cgSetEdgeFilter(legendKey, on) {
    if (_LEGEND_KEYS.indexOf(legendKey) < 0) return;
    window.cgEdgeFilter[legendKey] = !!on;
    try { localStorage.setItem(EDGE_FILTER_KEY, JSON.stringify(window.cgEdgeFilter)); } catch(e){}
    document.dispatchEvent(new CustomEvent('cg:edge-filter:change'));
  }
  window.cgSetEdgeFilter = cgSetEdgeFilter;

  function _saveFilter() { /* retained for callers; cgSetEdgeFilter does the actual save */ }

  /* ---------- Sidebar sections ---------- */
  var sb = _findSidebar();

  /* Edge filter is driven by the existing "Edge types" legend (checkboxes
   * already injected into the legend rows by _SIDEBAR_HTML). Three checkboxes
   * map to category sets:
   *   Confirmed   = exact + aggregated
   *   Probable    = heuristic + unresolved + external
   *   Variable    = variable-annotation overlay
   * A 4th "Violation" row is revealed when violations exist. */
  function applyEdgeFilter() {
    var mode = _cgxCurrentMode || 'fn';
    var entry = RENDER_MODES[mode];
    if (entry && entry.edgeFilter) entry.edgeFilter(_edgeFilterState);
  }

  function _syncEtCheckboxesFromState() {
    function set(id, val) { var el = document.getElementById(id); if (el) el.checked = !!val; }
    set('cg-et-cb-exact',    window.cgEdgeFilter.confirmed);
    set('cg-et-cb-probable', window.cgEdgeFilter.probable);
    set('cg-et-cb-var',      window.cgEdgeFilter.var);
  }
  _syncEtCheckboxesFromState();

  /* Three legend checkboxes — each toggles ONE legend key.
   * Violations fold into 'probable' (strict 3-type legend). */
  var cbExact = document.getElementById('cg-et-cb-exact');
  if (cbExact) cbExact.addEventListener('change', function(){
    cgSetEdgeFilter('confirmed', cbExact.checked);
  });
  var cbProb = document.getElementById('cg-et-cb-probable');
  if (cbProb) cbProb.addEventListener('change', function(){
    cgSetEdgeFilter('probable', cbProb.checked);
  });
  var cbVar = document.getElementById('cg-et-cb-var');
  if (cbVar) cbVar.addEventListener('change', function(){
    cgSetEdgeFilter('var', cbVar.checked);
  });

  /* Optional 4th 'Violation' row is no longer used as its own visual category —
   * keep it hidden permanently. Violations still surface in the Architecture
   * sidebar modal + edge tooltip. */
  var vrow = document.getElementById('cg-et-violation-row');
  if (vrow) vrow.style.display = 'none';

  /* Central listener: every renderer subscribes and re-applies its own filter.
   * This is what makes the checkbox response immediate across all modes. */
  document.addEventListener('cg:edge-filter:change', function(){
    _syncEtCheckboxesFromState();
    applyEdgeFilter();
    /* Var-annotation overlay (vis.js gold-dashed edges + any DOM .cg-vf-anno-arrow). */
    var showVar = window.cgEdgeFilter.var;
    try {
      Array.prototype.forEach.call(document.querySelectorAll('.cg-vf-anno-arrow, .cgx-var-arrow'), function(el){
        el.style.display = showVar ? '' : 'none';
      });
    } catch(e) {}
    var net = _cgxGetNet();
    if (net) {
      if (net && net.body && net.body.data && net.body.data.edges) {
        try {
          var ids = net.body.data.edges.getIds();
          var updates = [];
          ids.forEach(function(id){
            var e = net.body.data.edges.get(id);
            if (!e || !e.color) return;
            var col = (typeof e.color === 'string') ? e.color : (e.color.color || '');
            if (String(col).toLowerCase().indexOf('c8b400') >= 0) updates.push({id:id, hidden:!showVar});
          });
          if (updates.length) net.body.data.edges.update(updates);
        } catch(e2) {}
      }
    }
  });

  /* Build Info section */
  if (CGX.build_info) {
    var bi = CGX.build_info;
    var biHtml = '<div class="cgx-collapsible">' +
      '<div class="cgx-collapsible-toggle">Build Info (' + esc(bi.source) + ')</div>' +
      '<div class="cgx-collapsible-body">' +
      '<div class="cgx-row">compile_commands.json: ' + (bi.compile_commands_path ? esc(bi.compile_commands_path) : '<i>not found</i>') + '</div>' +
      '<div class="cgx-row">Units: ' + bi.unit_count + '</div>' +
      (bi.configuration ? '<div class="cgx-row">Configuration: ' + esc(bi.configuration) + '</div>' : '') +
      (bi.platform ? '<div class="cgx-row">Platform: ' + esc(bi.platform) + '</div>' : '') +
      (bi.projects && bi.projects.length ? '<div class="cgx-row">Projects: ' + esc(bi.projects.join(', ')) + '</div>' : '') +
      (bi.files_not_in_compile_commands && bi.files_not_in_compile_commands.length ?
        '<div class="cgx-row" style="color:#ffb56a">Files missing from CC: ' + bi.files_not_in_compile_commands.length + '</div>' : '') +
      (bi.cc_files_not_found && bi.cc_files_not_found.length ?
        '<div class="cgx-row" style="color:#ffb56a">CC entries not on disk: ' + bi.cc_files_not_found.length + '</div>' : '') +
      '</div></div>';
    var biSec = _h('div', {'class':'cgx-section'}, '<label>Build</label>' + biHtml);
    if (sb) sb.appendChild(biSec);
    var t = biSec.querySelector('.cgx-collapsible-toggle');
    if (t) t.addEventListener('click', function(){ t.parentElement.classList.toggle('open'); });
  }

  /* Architecture section + Violations modal */
  var moduleCount = Object.keys(CGX.modules || {}).length;
  var violationCount = (CGX.violations || []).length;
  if (moduleCount || violationCount) {
    var archHtml =
      '<div class="cgx-row">Modules: <b>' + moduleCount + '</b></div>' +
      '<div class="cgx-row">Violations: <b style="color:' + (violationCount ? '#ff8585' : '#cfd6dd') + '">' + violationCount + '</b></div>' +
      (violationCount ? '<div class="cgx-row"><button class="cgx-btn-link" id="cgx-arch-open">Show violations…</button></div>' : '');
    var archSec = _h('div', {'class':'cgx-section'}, '<label>Architecture</label>' + archHtml);
    if (sb) sb.appendChild(archSec);
    var openBtn = document.getElementById('cgx-arch-open');
    if (openBtn) openBtn.addEventListener('click', cgxOpenArchModal);
  }
  function cgxOpenArchModal() {
    var body = document.getElementById('cgx-arch-modal-body');
    if (!body) return;
    if (!CGX.violations || !CGX.violations.length) {
      body.innerHTML = '<p>No violations.</p>';
    } else {
      var rows = CGX.violations.map(function(v){
        return '<tr>' +
          '<td><span class="cgx-pill cgx-pill-violation">' + esc(v.kind) + '</span></td>' +
          '<td>' + esc(v.from) + '</td>' +
          '<td>&rarr; ' + esc(v.to) + '</td>' +
          '<td style="color:#cfd6dd">' + esc(v.reason || '') + '</td>' +
          '<td style="color:#6a7a8a">' + (v.sample_edges || []).length + ' sample edge(s)</td>' +
        '</tr>';
      }).join('');
      body.innerHTML = '<table><thead><tr><th>Kind</th><th>From</th><th>To</th><th>Reason</th><th>Samples</th></tr></thead><tbody>' + rows + '</tbody></table>';
    }
    document.getElementById('cgx-arch-modal').classList.add('open');
  }
  window.cgxOpenArchModal = cgxOpenArchModal;
  window.cgxCloseArchModal = function(){ document.getElementById('cgx-arch-modal').classList.remove('open'); };
  window.cgxCloseModModal = function(){ document.getElementById('cgx-mod-modal').classList.remove('open'); };

  /* ---------- Module-View controls (sidebar) ---------- */
  var mvSec = _h('div', {'class':'cgx-section','id':'cgx-mv-controls','style':'display:none'},
    '<label>Module View</label>' +
    '<div class="cgx-row">'+
      '<button class="cgx-btn-link" id="cgx-mv-expand-all">Expand all</button>'+
      '<span style="color:#6a7a8a"> - </span>'+
      '<button class="cgx-btn-link" id="cgx-mv-collapse-all">Collapse all</button>'+
    '</div>'+
    '<div class="cgx-row">'+
      '<span style="color:#6a7a8a">·</span>'+
      '<button class="cgx-btn-link" id="cgx-mv-expand-one">Expand one level</button>'+
      '<span style="color:#6a7a8a"> - </span>'+
      '<button class="cgx-btn-link" id="cgx-mv-collapse-one">Collapse one level</button>'+
    '</div>'+
    '<div class="cgx-row">'+
      '<input type="checkbox" class="cgx-cb" id="cgx-mv-hide-intra">'+
      '<span>Hide intra-module edges</span>'+
    '</div>'+
    '<div class="cgx-row">'+
      '<span>Top-N per card:</span>'+
      '<input type="number" id="cgx-mv-topn" value="25" min="5" max="500" style="width:60px;background:#1e2229;color:#fff;border:1px solid #2d3139;border-radius:3px;padding:2px 4px">'+
    '</div>'
  );
  if (sb) sb.appendChild(mvSec);

  /* ---------- Include Graph view (nodes + edges canvas) ---------- */
  if (CGX.include_graph_enabled && CGX.include_graph) {
    var incView = document.getElementById('cgx-inc-view');
    var ig = CGX.include_graph;

    /* ── helpers ──────────────────────────────────────────────────── */
    var _HDR_EXTS = /\.(h|hpp|hxx|hh|inl|tpp)$/i;
    function _isHeader(p) { return _HDR_EXTS.test((p||'').replace(/\\/g,'/').replace(/\?.*$/,'')); }
    function _base(p) { return (p||'').replace(/\\/g,'/').split('/').pop(); }
    function _dir(p)  { var s=(p||'').replace(/\\/g,'/'); var i=s.lastIndexOf('/'); return i>0?s.slice(0,i):'(root)'; }

    /* cycle set */
    var _cycleFiles = {};
    (ig.cycles||[]).forEach(function(cyc){ cyc.forEach(function(f){ _cycleFiles[f]=true; }); });
    function _cyclesFor(f){ return (ig.cycles||[]).filter(function(c){ return c.indexOf(f)>=0; }); }

    /* in-degree (times included) */
    var _inDeg = {};
    Object.keys(ig.files).forEach(function(src){
      ig.files[src].forEach(function(e){ if(e.resolved) _inDeg[e.to]=(_inDeg[e.to]||0)+1; });
    });
    (ig.most_included||[]).forEach(function(mi){ _inDeg[mi[0]]=mi[1]; });

    /* reverse index: who includes F */
    var _revIdx = {};
    Object.keys(ig.files).forEach(function(src){
      ig.files[src].forEach(function(e){
        if(e.resolved){ if(!_revIdx[e.to]) _revIdx[e.to]=[]; _revIdx[e.to].push(src); }
      });
    });

    /* node status */
    function _nodeStatus(fp, edges) {
      if(_cycleFiles[fp]) return 'cycle';
      if(edges && edges.some(function(e){ return !e.is_system&&!e.resolved; })) return 'mixed';
      return 'ok';
    }

    /* ── state (declared before _ivBuildGraph so it can read _ivShowSys) ─ */
    var _ivShowSys = !!CGX.include_show_system;
    var _ivSearch  = '';
    var _ivSelected = null;
    var _ivFocusIds = null; /* null = show all; array = show only these node IDs */
    var _ivZoom = 1.0, _ivPanX = 0, _ivPanY = 0, _ivDrag = null;
    var _ivNodeDrag = null; /* for dragging individual nodes */
    var _ivMarquee = null, _ivMultiSel = {};
    var NODE_W = 190, NODE_H = 54;
    var _ivVisNodes = [], _ivVisEdges = [];

    /* ── build node/edge sets (rebuilt when system-includes toggle changes) ── */
    var _projHdrs = {}; // path -> edges[] (always populated, all project headers)
    Object.keys(ig.files).forEach(function(fp){ if(_isHeader(fp)) _projHdrs[fp]=ig.files[fp]; });

    /* Mutable arrays rebuilt by _ivBuildGraph() */
    var _ivNodes   = [];
    var _ivNodeIds = {};
    var _ivEdges   = [];
    var _ivMissingCount = 0; /* track for stat badge updates */

    function _ivBuildGraph() {
      _ivNodes   = [];
      _ivNodeIds = {};
      _ivEdges   = [];

      /* Project header nodes */
      Object.keys(_projHdrs).forEach(function(fp){
        _ivNodes.push({id:fp, label:_base(fp), path:fp, isMissing:false,
                       status:_nodeStatus(fp,_projHdrs[fp]), inDeg:_inDeg[fp]||0, isSys:false});
      });

      /* Missing (unresolved user) headers */
      var _missingHdrs = {};
      Object.keys(_projHdrs).forEach(function(src){
        _projHdrs[src].forEach(function(e){
          if (!e.resolved && !e.is_system && _isHeader(e.raw)){
            if (!_missingHdrs[e.raw]) _missingHdrs[e.raw] = [];
            _missingHdrs[e.raw].push({from:src, line:e.line});
          }
        });
      });
      Object.keys(_missingHdrs).forEach(function(raw){
        _ivNodes.push({id:'miss:'+raw, label:_base(raw), path:raw, isMissing:true,
                       status:'unres', inDeg:0, isSys:false});
      });
      _ivMissingCount = Object.keys(_missingHdrs).length;

      /* System include nodes are created per include-edge (per caller header),
       * so each <...> node stays as a leaf directly under the header that includes it. */
      function _ivSysNodeId(src, e, ix){
        var ln = (e && e.line != null) ? String(e.line) : String(ix || 0);
        return 'sys:' + src + '::' + e.raw + '::' + ln;
      }
      if (_ivShowSys) {
        var _sysNodeSeen = {};
        Object.keys(_projHdrs).forEach(function(src){
          _projHdrs[src].forEach(function(e, ix){
            if (!e.is_system) return;
            var sid = _ivSysNodeId(src, e, ix);
            if (_sysNodeSeen[sid]) return;
            _sysNodeSeen[sid] = true;
            _ivNodes.push({id:sid, label:'<'+e.raw+'>', path:e.raw, isMissing:false,
                           status:'sys', inDeg:0, isSys:true});
          });
        });
      }

      /* node id lookup */
      _ivNodes.forEach(function(n){ _ivNodeIds[n.id] = true; });

      /* edges */
      Object.keys(_projHdrs).forEach(function(src){
        _projHdrs[src].forEach(function(e, ix){
          if (e.is_system) {
            if (_ivShowSys) {
              var sid = _ivSysNodeId(src, e, ix);
              if (_ivNodeIds[sid]) _ivEdges.push({from:src, to:sid, isMissing:false, isSys:true});
            }
          } else if (e.resolved && _ivNodeIds[e.to]) {
            _ivEdges.push({from:src, to:e.to, isMissing:false, isSys:false});
          } else if (!e.resolved && _isHeader(e.raw)) {
            _ivEdges.push({from:src, to:'miss:'+e.raw, isMissing:true, isSys:false});
          }
        });
      });
    }

    /* initial build */
    _ivBuildGraph();

    /* ── skeleton HTML ────────────────────────────────────────────── */
    incView.innerHTML =
      '<div id="cg-iv-toolbar">' +
        '<h3>Include Graph</h3>' +
        '<span class="cg-iv-stat" id="cg-iv-s-files"><b>'+Object.keys(_projHdrs).length+'</b> headers</span>' +
        '<span class="cg-iv-stat" id="cg-iv-s-miss"><b>'+_ivMissingCount+'</b> missing</span>' +
        '<span class="cg-iv-stat" id="cg-iv-s-cyc"><b>'+(ig.cycles||[]).length+'</b> cycles</span>' +
        '<div class="cg-iv-filters">' +
          '<input id="cg-iv-search" type="text" placeholder="search headers…" spellcheck="false">' +
        '</div>' +
        '<button id="cgx-inc-close" title="Close">✕ Close</button>' +
      '</div>' +
      '<div id="cg-iv-legend">' +
        '<span class="cg-iv-leg"><span class="cg-iv-leg-dot" style="background:#1f3028;border:1px solid #27ae60"></span>resolved</span>' +
        '<span class="cg-iv-leg"><span class="cg-iv-leg-dot" style="background:#2e1e0a;border:1px dashed #e67e22"></span>missing</span>' +
        '<span class="cg-iv-leg"><span class="cg-iv-leg-dot" style="background:#2a1010;border:1px solid #e74c3c"></span>cycle</span>' +
        '<span class="cg-iv-leg"><span class="cg-iv-leg-dot" style="background:#252012;border:1px solid #7a6a10"></span>mixed</span>' +
        '<span class="cg-iv-leg"><span class="cg-iv-leg-dot" style="background:#1a1e2a;border:1px dotted #3a4460"></span>system</span>' +
        '<span style="margin-left:auto;font-size:10px;color:#5a7090">scroll to zoom · middle-drag to pan · drag background to multi-select · drag nodes to move</span>' +
      '</div>' +
      '<div id="cg-iv-body">' +
        '<div id="cg-iv-canvas"><div id="cg-iv-canvas-inner">' +
          '<svg id="cg-iv-arrows" xmlns="http://www.w3.org/2000/svg" style="overflow:visible">' +
            '<defs>' +
              '<marker id="iv-arr-ok"  markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto" markerUnits="strokeWidth"><path d="M0,1 L9,5 L0,9 Z" fill="#27ae60"/></marker>' +
              '<marker id="iv-arr-mis" markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto" markerUnits="strokeWidth"><path d="M0,1 L9,5 L0,9 Z" fill="#e67e22"/></marker>' +
              '<marker id="iv-arr-cyc" markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto" markerUnits="strokeWidth"><path d="M0,1 L9,5 L0,9 Z" fill="#e74c3c"/></marker>' +
              '<marker id="iv-arr-sys" markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto" markerUnits="strokeWidth"><path d="M0,1 L9,5 L0,9 Z" fill="#3a4460"/></marker>' +
            '</defs>' +
          '</svg>' +
        '</div></div>' +
        '<div id="cg-iv-panel"><div id="cg-iv-panel-header">' +
          '<button id="cg-iv-panel-close">×</button>' +
          '<h4 id="cg-iv-ph-name"></h4>' +
          '<div class="cg-iv-path-sub" id="cg-iv-ph-path"></div>' +
        '</div><div id="cg-iv-panel-body"></div></div>' +
      '</div>';

    /* ── layered top-down layout (comfortable default) ───────────── */
    /* Goals:
     *  - Stable, readable levels from roots (top) to leaves (bottom)
     *  - Fewer crossings via parent-barycenter ordering
     *  - System/missing headers stay leaf-like under their callers */
    function _ivLayout(nodes, edges) {
      if(!nodes.length) return [];
      var idToIdx = {}; nodes.forEach(function(n,i){ idToIdx[n.id]=i; });
      var N = nodes.length;

      /* Forward adjacency: parent→children (A includes B => A is parent of B) */
      var fwdAdj = nodes.map(function(){ return []; });
      var revAdj = nodes.map(function(){ return []; });
      var outDeg = new Array(N).fill(0);
      var inDeg  = new Array(N).fill(0);
      var seenEdge = {};
      edges.forEach(function(e){
        var si=idToIdx[e.from], di=idToIdx[e.to];
        if(si==null||di==null||si===di) return;
        var k = si + '>' + di;
        if (seenEdge[k]) return;
        seenEdge[k] = true;
        fwdAdj[si].push(di);
        revAdj[di].push(si);
        outDeg[si]++;
        inDeg[di]++;
      });

      function _isTerminal(i){
        return !!(nodes[i].isSys || nodes[i].isMissing);
      }
      function _label(i){
        return String((nodes[i] && nodes[i].label) || '');
      }

      /* Root candidates: non-terminal headers with no incoming edges. */
      var roots = [];
      for(var i=0;i<N;i++){
        if(!_isTerminal(i) && inDeg[i]===0) roots.push(i);
      }
      if(!roots.length){
        /* fallback root = highest out-degree non-terminal */
        var best = 0;
        for(var bi=0;bi<N;bi++){
          if(_isTerminal(bi)) continue;
          if(_isTerminal(best) || outDeg[bi] > outDeg[best]) best = bi;
        }
        roots = [best];
      }
      roots.sort(function(a,b){
        if(outDeg[b] !== outDeg[a]) return outDeg[b]-outDeg[a];
        return _label(a).localeCompare(_label(b));
      });

      /* BFS depth from roots (downstream). */
      var depth = new Array(N).fill(-1);
      var bfsQ = [];
      roots.forEach(function(r){ if(depth[r]===-1){ depth[r]=0; bfsQ.push(r); } });
      var bfsH = 0;
      while(bfsH<bfsQ.length){
        var cur = bfsQ[bfsH++];
        fwdAdj[cur].forEach(function(ch){
          if(depth[ch]===-1){
            depth[ch] = depth[cur]+1;
            bfsQ.push(ch);
          }
        });
      }

      /* Disconnected non-terminal components become additional roots. */
      for(var ui=0;ui<N;ui++){
        if(depth[ui]!==-1 || _isTerminal(ui)) continue;
        depth[ui] = 0;
        var q2=[ui], q2h=0;
        while(q2h<q2.length){
          var cc=q2[q2h++];
          fwdAdj[cc].forEach(function(ch){
            if(depth[ch]===-1){
              depth[ch]=depth[cc]+1;
              q2.push(ch);
            }
          });
        }
      }

      /* Force terminal nodes to be one layer below their deepest parent. */
      for(var ti=0;ti<N;ti++){
        if(!_isTerminal(ti)) continue;
        var pMax = -1;
        for(var pi=0;pi<revAdj[ti].length;pi++){
          var pd = depth[revAdj[ti][pi]];
          if(pd>pMax) pMax=pd;
        }
        if(pMax>=0) depth[ti]=pMax+1;
      }

      /* Any still-unplaced nodes go to the bottom-ish layer. */
      var maxDepth = 0;
      for(var md=0;md<N;md++) if(depth[md]>maxDepth) maxDepth=depth[md];
      for(var u2=0;u2<N;u2++){
        if(depth[u2]===-1) depth[u2]=maxDepth+1;
      }

      /* Build layers. */
      maxDepth = 0;
      var layers = {};
      for(var li=0;li<N;li++){
        var d = depth[li];
        if(!layers[d]) layers[d]=[];
        layers[d].push(li);
        if(d>maxDepth) maxDepth=d;
      }

      /* Initial order in each layer: non-terminal first, then by out-degree/name. */
      Object.keys(layers).forEach(function(k){
        layers[k].sort(function(a,b){
          var ta=_isTerminal(a)?1:0, tb=_isTerminal(b)?1:0;
          if(ta!==tb) return ta-tb;
          if(outDeg[b]!==outDeg[a]) return outDeg[b]-outDeg[a];
          return _label(a).localeCompare(_label(b));
        });
      });

      /* Barycenter sweeps to reduce crossings and keep children under parents. */
      for(var pass=0; pass<4; pass++){
        for(var d1=1; d1<=maxDepth; d1++){
          var prevLayer = layers[d1-1] || [];
          var curLayer  = layers[d1] || [];
          var prevPos = {};
          for(var pp=0;pp<prevLayer.length;pp++) prevPos[prevLayer[pp]] = pp;
          curLayer.sort(function(a,b){
            function bary(idx){
              var ps = revAdj[idx].filter(function(p){ return depth[p]===d1-1; });
              if(!ps.length) return 1e9;
              var sum = 0;
              ps.forEach(function(p){ sum += (prevPos[p] != null ? prevPos[p] : 0); });
              return sum / ps.length;
            }
            var ba = bary(a), bb = bary(b);
            if(ba !== bb) return ba - bb;
            return _label(a).localeCompare(_label(b));
          });
          layers[d1] = curLayer;
        }
      }

      /* Position layers centered on widest layer. */
      var H_GAP = 46;
      var V_GAP = 92;
      var BASE_X = 90;
      var BASE_Y = 70;
      var layerWidth = {};
      var maxWidth = 0;
      for(var d2=0; d2<=maxDepth; d2++){
        var cnt = (layers[d2]||[]).length;
        var w = cnt ? (cnt*NODE_W + (cnt-1)*H_GAP) : NODE_W;
        layerWidth[d2] = w;
        if(w>maxWidth) maxWidth = w;
      }

      var pos = new Array(N);
      for(var d3=0; d3<=maxDepth; d3++){
        var row = layers[d3] || [];
        var rowW = layerWidth[d3];
        var startX = BASE_X + Math.max(0, (maxWidth-rowW)/2);
        var y = BASE_Y + d3*(NODE_H+V_GAP);
        for(var rx=0; rx<row.length; rx++){
          var idx = row[rx];
          pos[idx] = { x: startX + rx*(NODE_W+H_GAP), y: y };
        }
      }

      /* Gentle nudge: single-parent terminal nodes align beneath their parent. */
      for(var d4=1; d4<=maxDepth; d4++){
        var row2 = layers[d4] || [];
        row2.forEach(function(idx){
          if(!_isTerminal(idx)) return;
          var ps = revAdj[idx].filter(function(p){ return depth[p]===d4-1; });
          if(ps.length!==1) return;
          var pIdx = ps[0];
          if(pos[pIdx] && pos[idx]) pos[idx].x = pos[pIdx].x;
        });
        /* prevent overlap after nudge */
        row2.sort(function(a,b){ return pos[a].x - pos[b].x; });
        for(var s=1; s<row2.length; s++){
          var prev = row2[s-1], cur2 = row2[s];
          var minX = pos[prev].x + NODE_W + 14;
          if(pos[cur2].x < minX) pos[cur2].x = minX;
        }
      }

      return pos;
    }

    /* ── draw arrows ──────────────────────────────────────────────── */
    function _ivDrawArrows() {
      var svg = document.getElementById('cg-iv-arrows');
      if(!svg) return;
      Array.prototype.forEach.call(svg.querySelectorAll('path.iv-arrow'), function(p){ p.parentNode.removeChild(p); });
      _ivVisEdges.forEach(function(e){
        var fromEl = _ivNodeEls[e.from], toEl = _ivNodeEls[e.to];
        if(!fromEl||!toEl) return;
        /* from bottom-center of parent to top-center of child */
        var fx = parseInt(fromEl.style.left)+NODE_W/2;
        var fy = parseInt(fromEl.style.top)+NODE_H;
        var tx = parseInt(toEl.style.left)+NODE_W/2;
        var ty = parseInt(toEl.style.top);
        /* vertical bezier curve */
        var midY = (fy+ty)/2;
        var d='M'+fx+','+fy+' C'+fx+','+midY+' '+tx+','+midY+' '+tx+','+ty;
        var isCyc = _cycleFiles[e.from]&&_cycleFiles[e.to];
        var marker = isCyc?'iv-arr-cyc':(e.isMissing?'iv-arr-mis':'iv-arr-ok');
        var stroke = isCyc?'#e74c3c':(e.isMissing?'#e67e22':'#27ae60');
        var path = document.createElementNS('http://www.w3.org/2000/svg','path');
        path.setAttribute('class','iv-arrow');
        path.setAttribute('d',d);
        path.setAttribute('fill','none');
        path.setAttribute('stroke',stroke);
        path.setAttribute('stroke-width','2');
        path.setAttribute('stroke-dasharray', e.isMissing?'5,3':'');
        path.setAttribute('marker-end','url(#'+marker+')');
        path.setAttribute('opacity', e.isSys?'0.9':'0.8');
        svg.appendChild(path);
      });
      var inner = document.getElementById('cg-iv-canvas-inner');
      if(inner){ svg.style.width=inner.style.width; svg.style.height=inner.style.height; }
    }

    /* ── render graph ─────────────────────────────────────────────── */
    var _ivNodeEls = {};
    function _ivRender() {
      var q = _ivSearch.toLowerCase();
      var baseNodes = (_ivFocusIds !== null)
        ? _ivNodes.filter(function(n){ return _ivFocusIds.indexOf(n.id) >= 0; })
        : _ivNodes;
      _ivVisNodes = baseNodes.filter(function(n){
        return !q || n.label.toLowerCase().indexOf(q)>=0 || n.path.toLowerCase().indexOf(q)>=0;
      });
      var visIds = {}; _ivVisNodes.forEach(function(n){ visIds[n.id]=true; });
      _ivVisEdges = _ivEdges.filter(function(e){ return visIds[e.from]&&visIds[e.to]; });

      var pos = _ivLayout(_ivVisNodes, _ivVisEdges);

      /* size inner canvas */
      var maxX=0, maxY=0;
      pos.forEach(function(p){ if(!p) return; if(p.x+NODE_W>maxX) maxX=p.x+NODE_W; if(p.y+NODE_H>maxY) maxY=p.y+NODE_H; });
      var inner = document.getElementById('cg-iv-canvas-inner');
      if(!inner) return;
      var w = Math.max(maxX+200, 800);
      var h = Math.max(maxY+200, 600);
      inner.style.width=w+'px'; inner.style.height=h+'px';

      /* remove old nodes */
      Array.prototype.forEach.call(inner.querySelectorAll('.cg-iv-node'), function(el){ el.parentNode.removeChild(el); });
      _ivNodeEls = {};

      /* place nodes */
      _ivVisNodes.forEach(function(n, i){
        var p = pos[i];
        if(!p) return;
        var el = document.createElement('div');
        el.className = 'cg-iv-node st-'+n.status
          + (_ivSelected===n.id?' iv-selected':'')
          + (_ivMultiSel[n.id] ? ' iv-multi-selected' : '');
        el.style.left = p.x+'px'; el.style.top = p.y+'px';
        el.setAttribute('data-nid', n.id);
        el.title = n.path;
        var badge = n.inDeg ? '<span class="cg-iv-node-badge">×'+n.inDeg+'</span>' : '';
        el.innerHTML = '<div class="cg-iv-node-label">'+esc(n.label)+'</div>' +
                       '<div class="cg-iv-node-sub">'+esc(_dir(n.path))+'</div>' + badge;
        inner.appendChild(el);
        _ivNodeEls[n.id] = el;
      });

      /* draw arrows */
      setTimeout(_ivDrawArrows, 0);
    }
    function _ivApplyMultiSel() {
      Array.prototype.forEach.call(document.querySelectorAll('.cg-iv-node'), function(el){
        var nid = el.getAttribute('data-nid');
        el.classList.toggle('iv-multi-selected', !!_ivMultiSel[nid]);
      });
    }
    function _ivSetMultiSel(map) {
      _ivMultiSel = map || {};
      _ivApplyMultiSel();
    }

    /* ── apply pan/zoom transform ─────────────────────────────────── */
    function _ivApplyTransform(){
      var inner=document.getElementById('cg-iv-canvas-inner');
      if(!inner) return;
      inner.style.transformOrigin='0 0';
      inner.style.transform='translate('+_ivPanX+'px,'+_ivPanY+'px) scale('+_ivZoom+')';
    }

    /* ── side panel ───────────────────────────────────────────────── */
    function _ivOpenPanel(nid) {
      _ivSelected = nid;
      /* update selected node highlight */
      Array.prototype.forEach.call(document.querySelectorAll('.cg-iv-node'), function(el){
        el.classList.toggle('iv-selected', el.getAttribute('data-nid')===nid);
      });
      var node = null;
      _ivNodes.forEach(function(n){ if(n.id===nid) node=n; });
      if(!node) return;
      var panel = document.getElementById('cg-iv-panel');
      var pbody = document.getElementById('cg-iv-panel-body');
      document.getElementById('cg-iv-ph-name').textContent = node.label;
      document.getElementById('cg-iv-ph-path').textContent = node.path;
      panel.classList.add('open');

      var edges = node.isMissing ? [] : (_projHdrs[nid] || []);
      var incoming = _revIdx[nid] || [];
      var cycles = _cyclesFor(nid);
      var html = '';

      /* outgoing includes */
      var visEdges = edges.filter(function(e){ return _ivShowSys||!e.is_system; });
      html += '<div class="cg-iv-section"><div class="cg-iv-section-title">Includes ('+visEdges.length+')</div>';
      if(node.isMissing){
        var refs = _missingHdrs[nid.replace(/^miss:/,'')] || [];
        html += '<div class="cg-iv-empty-note">Missing header &mdash; not found in project</div>';
        if(refs.length){
          html += '<div class="cg-iv-section-title" style="margin-top:6px">Referenced by ('+refs.length+')</div>';
          refs.forEach(function(r){ html += '<div class="cg-iv-backref-item" data-goto="'+esc(r.from)+'">'+esc(_base(r.from))+(r.line?' <span style="color:#5a7090">L'+r.line+'</span>':'')+'</div>'; });
        }
      } else if(!visEdges.length){
        html += '<div class="cg-iv-empty-note">No local includes</div>';
      } else {
        visEdges.forEach(function(e){
          var cls = e.is_system?'cg-iv-edge-sys':(e.resolved?'cg-iv-edge-ok':'cg-iv-edge-unr');
          var nav = (!e.is_system&&e.resolved) ? ' data-goto="'+esc(e.to)+'"' : '';
          var navMiss = (!e.resolved&&!e.is_system) ? ' data-unr="'+esc(e.raw)+'"' : '';
          html += '<div class="cg-iv-edge-item"><span class="'+cls+'"'+nav+navMiss+'>'+(e.is_system?'&lt;':'&quot;')+esc(e.raw)+(e.is_system?'&gt;':'&quot;')+
            (!e.resolved&&!e.is_system?' <i style="font-size:9px">(not found)</i>':'')+
            '</span>'+(e.line?'<span class="cg-iv-line">L'+e.line+'</span>':'')+'</div>';
        });
      }
      html += '</div>';

      /* incoming */
      html += '<div class="cg-iv-section"><div class="cg-iv-section-title">Included by ('+incoming.length+')</div>';
      if(!incoming.length) html += '<div class="cg-iv-empty-note">Not included by any scanned header</div>';
      else incoming.forEach(function(src){ html += '<div class="cg-iv-backref-item" data-goto="'+esc(src)+'">'+esc(_base(src))+'</div>'; });
      html += '</div>';

      /* cycles */
      if(cycles.length){
        html += '<div class="cg-iv-section"><div class="cg-iv-section-title">Cycles ('+cycles.length+')</div>';
        cycles.forEach(function(cyc){
          var disp = cyc.map(function(s){ return _base(s); }); disp.push(disp[0]);
          html += '<div class="cg-iv-cycle-path">'+disp.map(function(s,i){ return i===0?'<b>'+esc(s)+'</b>':esc(s); }).join('<span class="cg-iv-cycle-arr"> → </span>')+'</div>';
        });
        html += '</div>';
      }

      pbody.innerHTML = html;
      /* wire panel clicks */
      Array.prototype.forEach.call(pbody.querySelectorAll('[data-goto]'), function(el){
        el.addEventListener('click', function(){ _ivOpenPanel(el.getAttribute('data-goto')); _ivScrollTo(el.getAttribute('data-goto')); });
      });
      Array.prototype.forEach.call(pbody.querySelectorAll('[data-unr]'), function(el){
        el.addEventListener('click', function(){
          var raw = el.getAttribute('data-unr');
          _ivOpenPanel('miss:'+raw);
          _ivScrollTo('miss:'+raw);
        });
      });
    }

    function _ivScrollTo(nid){
      var el = _ivNodeEls[nid];
      if(!el) return;
      var canvas = document.getElementById('cg-iv-canvas');
      if(!canvas) return;
      var cr = canvas.getBoundingClientRect();
      var x = parseInt(el.style.left)*_ivZoom + _ivPanX;
      var y = parseInt(el.style.top)*_ivZoom + _ivPanY;
      _ivPanX += cr.width/2 - x - NODE_W*_ivZoom/2;
      _ivPanY += cr.height/2 - y - NODE_H*_ivZoom/2;
      _ivApplyTransform();
    }

    /* ── wire controls ────────────────────────────────────────────── */
    var srch = document.getElementById('cg-iv-search');
    if(srch) srch.addEventListener('input', function(){ _ivSearch=srch.value; _ivRender(); });


    var cBtn = document.getElementById('cgx-inc-close');
    if(cBtn) cBtn.addEventListener('click', function(){ activateSlot(_activeSlotId==='slot2'?'slot2':'slot1'); });

    var pClose = document.getElementById('cg-iv-panel-close');
    if(pClose) pClose.addEventListener('click', function(){
      document.getElementById('cg-iv-panel').classList.remove('open');
      _ivSelected=null;
      Array.prototype.forEach.call(document.querySelectorAll('.cg-iv-node'), function(el){ el.classList.remove('iv-selected'); });
    });

    /* ── pan/zoom + node drag wiring ─────────────────────────────── */
    (function(){
      var canvas = document.getElementById('cg-iv-canvas');
      if(!canvas||canvas._ivWired) return;
      canvas._ivWired = true;

      /* wheel zoom */
      canvas.addEventListener('wheel', function(ev){
        if(!document.getElementById('cgx-inc-view').classList.contains('cg-iv-active')) return;
        ev.preventDefault();
        var factor = ev.deltaY<0?1.1:0.909;
        var rect = canvas.getBoundingClientRect();
        var mx=ev.clientX-rect.left, my=ev.clientY-rect.top;
        var old=_ivZoom;
        _ivZoom = Math.max(0.08, Math.min(3.0, _ivZoom*factor));
        var act=_ivZoom/old;
        _ivPanX = mx-(mx-_ivPanX)*act;
        _ivPanY = my-(my-_ivPanY)*act;
        _ivApplyTransform();
      },{passive:false});

      /* mousedown: node drag or canvas pan */
      canvas.addEventListener('mousedown', function(ev){
        if(ev.button!==0 && ev.button!==1) return;
        var nodeEl = ev.target.closest ? ev.target.closest('.cg-iv-node') : null;
        /* Middle mouse always pans */
        if (ev.button === 1) {
          ev.preventDefault();
          _ivDrag={sx:ev.clientX,sy:ev.clientY,ox:_ivPanX,oy:_ivPanY};
          canvas.classList.add('iv-panning');
          return;
        }
        if(nodeEl){
          /* start node drag */
          ev.preventDefault(); ev.stopPropagation();
          var nid = nodeEl.getAttribute('data-nid') || '';
          var dragNodes = [];
          if (_ivMultiSel[nid]) {
            Array.prototype.forEach.call(document.querySelectorAll('.cg-iv-node.iv-multi-selected'), function(el){
              dragNodes.push({
                el: el,
                id: el.getAttribute('data-nid'),
                ox: parseInt(el.style.left)||0,
                oy: parseInt(el.style.top)||0
              });
            });
          }
          if (!dragNodes.length) {
            var one={}; if (nid) one[nid]=true; _ivSetMultiSel(one);
            dragNodes.push({el:nodeEl, id:nid, ox:parseInt(nodeEl.style.left)||0, oy:parseInt(nodeEl.style.top)||0});
          }
          dragNodes.forEach(function(it){ it.el.style.zIndex='10'; it.el.style.cursor='grabbing'; });
          _ivNodeDrag = {nodes:dragNodes, sx:ev.clientX, sy:ev.clientY};
        } else {
          ev.preventDefault();
          if (ev.altKey) {
            _ivDrag={sx:ev.clientX,sy:ev.clientY,ox:_ivPanX,oy:_ivPanY};
            canvas.classList.add('iv-panning');
            return;
          }
          var rc = canvas.getBoundingClientRect();
          var p0 = {x:ev.clientX-rc.left, y:ev.clientY-rc.top};
          var box = document.createElement('div');
          box.className = 'cg-marquee-box';
          canvas.appendChild(box);
          _ivMarquee = {canvas:canvas, box:box, start:p0, cur:p0};
          _setBoxRect(box, _mkRect(p0, p0));
        }
      });

      document.addEventListener('mousemove', function(ev){
        if(_ivNodeDrag){
          var dx = (ev.clientX-_ivNodeDrag.sx)/_ivZoom;
          var dy = (ev.clientY-_ivNodeDrag.sy)/_ivZoom;
          _ivNodeDrag.nodes.forEach(function(it){
            it.el.style.left = (it.ox+dx)+'px';
            it.el.style.top  = (it.oy+dy)+'px';
          });
          _ivDrawArrows();
        } else if(_ivMarquee){
          var rc2 = _ivMarquee.canvas.getBoundingClientRect();
          _ivMarquee.cur = {x:ev.clientX-rc2.left, y:ev.clientY-rc2.top};
          var mr = _mkRect(_ivMarquee.start, _ivMarquee.cur);
          _setBoxRect(_ivMarquee.box, mr);
          var rr = {
            left: (mr.left - _ivPanX) / _ivZoom,
            top: (mr.top - _ivPanY) / _ivZoom,
            right: (mr.right - _ivPanX) / _ivZoom,
            bottom: (mr.bottom - _ivPanY) / _ivZoom
          };
          var map = {};
          Array.prototype.forEach.call(document.querySelectorAll('.cg-iv-node'), function(el){
            var nr = {
              left: parseInt(el.style.left)||0,
              top: parseInt(el.style.top)||0,
              right: (parseInt(el.style.left)||0) + (el.offsetWidth||190),
              bottom: (parseInt(el.style.top)||0) + (el.offsetHeight||60)
            };
            if (_rectIntersects(rr, nr)) {
              var id = el.getAttribute('data-nid') || '';
              if (id) map[id] = true;
            }
          });
          _ivSetMultiSel(map);
        } else if(_ivDrag){
          _ivPanX=_ivDrag.ox+(ev.clientX-_ivDrag.sx);
          _ivPanY=_ivDrag.oy+(ev.clientY-_ivDrag.sy);
          _ivApplyTransform();
        }
      });

      document.addEventListener('mouseup', function(ev){
        if(_ivNodeDrag){
          _ivNodeDrag.nodes.forEach(function(it){
            it.el.style.zIndex = '';
            it.el.style.cursor = '';
          });
          /* if barely moved, treat as click → open panel */
          var movedDist = Math.abs(ev.clientX-_ivNodeDrag.sx)+Math.abs(ev.clientY-_ivNodeDrag.sy);
          if(movedDist<4 && _ivNodeDrag.nodes.length===1){
            var nid = _ivNodeDrag.nodes[0].id;
            if(nid) _ivOpenPanel(nid);
          }
          _ivNodeDrag=null;
        }
        if(_ivMarquee){
          if (_ivMarquee.box && _ivMarquee.box.parentNode) _ivMarquee.box.parentNode.removeChild(_ivMarquee.box);
          _ivMarquee = null;
        }
        if(_ivDrag){
          _ivDrag=null;
          canvas.classList.remove('iv-panning');
        }
      });
    })();

    _ivRender();

    /* ── Expose API for sidebar integration ───────────────────── */
    /* ── reachability helper (used by isolate) ─────────────────────── */
    function _ivReachable(seedIds, dir, maxDepth) {
      /* dir: 'down'=forward edges only, 'up'=reverse only, 'both'=both directions
       * maxDepth: max hops (undefined/null = unlimited) */
      var visited = {};    /* id -> depth reached */
      var queue = [];
      seedIds.forEach(function(id){ visited[id] = 0; queue.push(id); });
      var i = 0;
      while (i < queue.length) {
        var cur = queue[i++];
        var curDepth = visited[cur];
        if (maxDepth !== undefined && maxDepth !== null && curDepth >= maxDepth) continue;
        _ivEdges.forEach(function(e){
          var next = null;
          if (dir !== 'up'   && e.from === cur && !(e.to   in visited)) next = e.to;
          if (dir !== 'down' && e.to   === cur && !(e.from in visited)) next = e.from;
          if (next !== null) { visited[next] = curDepth + 1; queue.push(next); }
        });
      }
      return Object.keys(visited);
    }

    /* ── escape helper for sidebar dropdown HTML ──────────────────── */
    function _ivEsc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

    window.cgxIncSearch = function(q) {
      /* Populate the sidebar cg-search-dropdown with matching headers */
      var inp = document.getElementById('cg-search');
      var dd  = document.getElementById('cg-search-dropdown');
      if (!inp || !dd) return;
      var lq = (q || '').trim().toLowerCase();
      var candidates = lq
        ? _ivNodes.filter(function(n){
            return n.label.toLowerCase().indexOf(lq) >= 0 ||
                   n.id.toLowerCase().indexOf(lq) >= 0;
          })
        : _ivNodes.slice(0, 50);
      if (!candidates.length) { dd.style.display = 'none'; return; }
      var rect = inp.getBoundingClientRect();
      dd.style.top   = (rect.bottom + 2) + 'px';
      dd.style.left  = rect.left + 'px';
      dd.style.width = rect.width + 'px';
      function _hi(text, q2) {
        if (!q2) return _ivEsc(text);
        var idx = (text||'').toLowerCase().indexOf(q2);
        if (idx < 0) return _ivEsc(text);
        return _ivEsc(text.slice(0,idx))
          +'<span class="cg-sd-mark">'+_ivEsc(text.slice(idx,idx+q2.length))+'</span>'
          +_ivEsc(text.slice(idx+q2.length));
      }
      var statusColor = { ok:'#7dda9d', unres:'#ffb56a', cycle:'#ff7070', mixed:'#c8b400', unresolved:'#ffb56a' };
      dd.innerHTML = candidates.map(function(n){
        var col = statusColor[n.status] || '#aaaaaa';
        var dir = n.path.replace(/[\\/][^\\/]*$/, '') || '';
        dir = dir.replace(/.*[\\/]/g, '');
        return '<div class="cg-sd-item" data-nid="'+_ivEsc(n.id)+'">'
          +'<span class="cg-sd-name" style="color:'+col+'">'+_hi(n.label, lq)+'</span>'
          +'<span class="cg-sd-meta">'+_ivEsc(dir)+(n.isMissing?' · missing':'')+'</span>'
          +'</div>';
      }).join('');
      dd.querySelectorAll('.cg-sd-item').forEach(function(item){
        item.addEventListener('mousedown', function(e){
          e.preventDefault();
          var nid = item.dataset.nid;
          if (inp) inp.value = nid;
          dd.style.display = 'none';
          /* Just pan to the node — do NOT filter; user must click Isolate to filter */
          window.cgxIncCenter(nid);
        });
      });
      dd.style.display = 'block';
    };

    window.cgxIncHighlight = function(targets) {
      /* Visually highlight matched nodes (bright ring, dim others) without filtering */
      if (!targets || !targets.length) return;
      var matched = {};
      _ivNodes.forEach(function(n){
        if (targets.some(function(t){
          return n.id === t || n.label.toLowerCase() === (t||'').toLowerCase() ||
                 n.id.toLowerCase().indexOf((t||'').toLowerCase()) >= 0;
        })) matched[n.id] = true;
      });
      if (!Object.keys(matched).length) return;
      Array.prototype.forEach.call(document.querySelectorAll('.cg-iv-node'), function(el){
        var nid = el.getAttribute('data-nid') || '';
        el.classList.toggle('iv-selected', !!matched[nid]);
        el.style.opacity = matched[nid] ? '1' : '0.25';
      });
      /* pan to first match */
      var first = Object.keys(matched)[0];
      if (first) _ivScrollTo(first);
    };

    window.cgxIncIsolate = function(target, depth, dir) {
      /* Show only the target node + reachable headers according to depth/dir.
       * dir: 'down'/'callees' = headers it includes; 'up'/'callers' = headers that include it; 'both' = both */
      if (!target) return 0;
      var seed = null;
      _ivNodes.forEach(function(n){
        if (n.id === target || n.label.toLowerCase() === (target||'').toLowerCase()) seed = n.id;
      });
      if (!seed) return 0;
      /* Normalize direction: sidebar uses 'both'/'callees'/'callers'
       * callees = downstream (A includes B → B is callee of A) → dir='down'
       * callers = upstream (who includes me?)                  → dir='up'  */
      var normDir = (dir === 'callers') ? 'up' :
                    (dir === 'callees') ? 'down' : 'both';
      var maxDepth = (depth === undefined || depth === null || depth <= 0) ? undefined : depth;
      _ivFocusIds = _ivReachable([seed], normDir, maxDepth);
      _ivRender();
      setTimeout(function(){ _ivScrollTo(seed); }, 80);
      return _ivFocusIds.length;
    };

    window.cgxIncCenter = function(target) {
      /* Pan to a node without filtering */
      var nid = null;
      _ivNodes.forEach(function(n){
        if (n.id===target || n.label.toLowerCase()===(target||'').toLowerCase() ||
           n.id.toLowerCase().indexOf((target||'').toLowerCase())>=0) nid=n.id;
      });
      if (nid) { _ivOpenPanel(nid); _ivScrollTo(nid); }
    };

    window.cgxIncFit = function() {
      /* Reset zoom/pan to fit all nodes */
      var canvas = document.getElementById('cg-iv-canvas');
      var inner = document.getElementById('cg-iv-canvas-inner');
      if (!canvas||!inner) return;
      var cw = canvas.clientWidth, ch = canvas.clientHeight;
      var iw = parseInt(inner.style.width)||800, ih = parseInt(inner.style.height)||600;
      var scale = Math.min(cw/iw, ch/ih, 1.0)*0.9;
      _ivZoom = scale;
      _ivPanX = (cw - iw*scale)/2;
      _ivPanY = (ch - ih*scale)/2;
      _ivApplyTransform();
    };

    window.cgxIncClearFocus = function() {
      /* Remove all focus filtering and restore the full graph */
      _ivFocusIds = null;
      _ivSelected = null;
      _ivRender();
      /* Also clear any visual highlight (opacity/ring) left by Highlight button */
      setTimeout(function(){
        Array.prototype.forEach.call(document.querySelectorAll('.cg-iv-node'), function(el){
          el.classList.remove('iv-selected');
          el.style.opacity = '';
        });
        var panel = document.getElementById('cg-iv-panel');
        if (panel) panel.classList.remove('open');
      }, 50);
    };

    window.cgxIncGetNodes = function() {
      /* Return node list for sidebar search dropdown */
      return _ivNodes.map(function(n){ return {id:n.id, label:n.label, path:n.path, status:n.status}; });
    };

  } /* end if (ig && incView) */

  /* ====================================================================
   * Reuses Script View's visual foundation (same bg, same card style).
   * Consumes a slot payload:
   *   { nodes: [{id, label, meta:{file_path, tracked_vars:{__hierarchy__: jsonStr}}}, ...],
   *     edges: [{from, to, category, reason}, ...] }
   * Renders a topologically-laid-out grid of expandable cards with orthogonal SVG arrows.
   * ==================================================================== */
  var _mvBuiltFor = null;       // [slotId, level] tuple key for cache invalidation
  var _mvSearch = '';
  var _mvHideIntra = false;
  var _mvTopN = 25;
  var _mvCardById = {};       // module key -> card DOM
  var _mvCardByNodeId = {};   // graph node_id -> card DOM
  var _mvAggEdges = [];       // module-level edges (between cards)
  var _mvFnIndex = {};        // function node_id -> {nodeId, file}
  var _mvZoom = 1.0;
  var _mvPanX = 0;
  var _mvPanY = 0;
  var _mvViewDrag = null;
  var _mvMarquee = null, _mvMultiSel = {};

  function _mvApplyTransform() {
    var inner = document.getElementById('cgx-mv-canvas-inner');
    if (!inner) return;
    inner.style.transformOrigin = '0 0';
    inner.style.transform = 'translate(' + _mvPanX + 'px,' + _mvPanY + 'px) scale(' + _mvZoom + ')';
  }
  function _mvApplyMultiSel() {
    Array.prototype.forEach.call(document.querySelectorAll('.cgx-mv-card'), function(c){
      c.classList.toggle('mv-multi-selected', !!_mvMultiSel[c.getAttribute('data-id') || '']);
    });
  }
  function _mvSetMultiSel(map) {
    _mvMultiSel = map || {};
    _mvApplyMultiSel();
  }

  function _mvParseHierarchy(node) {
    var hraw = node.meta && node.meta.tracked_vars && node.meta.tracked_vars.__hierarchy__;
    if (!hraw) return [];
    try { return JSON.parse(hraw); } catch(e) { return []; }
  }

  function _mvCardKey(node) {
    /* qualified_name is "<level>::Key"; display the trailing Key. */
    var q = (node.label || (node.meta && node.meta.qualified_name) || '').split('::');
    return q.length >= 2 ? q[q.length - 1] : ((node.meta && node.meta.name) || node.label || '');
  }

  /* Comfortable default layered layout for aggregated modes
   * (module / folder / namespace / library). */
  function _mvComputeLayout(nodes, edges) {
    var N = nodes.length;
    if (!N) return {};
    var idToIdx = {};
    nodes.forEach(function(n, i){ idToIdx[n.id] = i; });

    var inEdges  = nodes.map(function(){ return []; });
    var outEdges = nodes.map(function(){ return []; });
    var inDeg = new Array(N).fill(0);
    var outDeg = new Array(N).fill(0);
    var w = {}; /* fromIdx|toIdx -> weight */
    var seen = {};
    edges.forEach(function(e){
      var si = idToIdx[e.from], di = idToIdx[e.to];
      if (si == null || di == null || si === di) return;
      var k = si + '|' + di;
      w[k] = (w[k] || 0) + 1;
      if (seen[k]) return;
      seen[k] = true;
      outEdges[si].push(di);
      inEdges[di].push(si);
      outDeg[si]++; inDeg[di]++;
    });

    function _nodeName(i){
      var n = nodes[i] || {};
      var lbl = n.label || (n.meta && (n.meta.qualified_name || n.meta.name)) || n.id || '';
      return String(lbl);
    }

    /* roots: no incoming; fallback to highest (out-in) */
    var roots = [];
    for (var i = 0; i < N; i++) if (inDeg[i] === 0) roots.push(i);
    if (!roots.length) {
      var best = 0;
      for (var bi = 1; bi < N; bi++) {
        var sb = outDeg[bi] - inDeg[bi];
        var sa = outDeg[best] - inDeg[best];
        if (sb > sa) best = bi;
      }
      roots = [best];
    }
    roots.sort(function(a,b){
      if (outDeg[b] !== outDeg[a]) return outDeg[b] - outDeg[a];
      return _nodeName(a).localeCompare(_nodeName(b));
    });

    /* BFS depth from roots */
    var depth = new Array(N).fill(-1);
    var q = [];
    roots.forEach(function(r){ if (depth[r] === -1) { depth[r] = 0; q.push(r); } });
    var h = 0;
    while (h < q.length) {
      var cur = q[h++];
      outEdges[cur].forEach(function(nx){
        if (depth[nx] === -1) {
          depth[nx] = depth[cur] + 1;
          q.push(nx);
        }
      });
    }
    /* disconnected nodes */
    for (var u = 0; u < N; u++) {
      if (depth[u] !== -1) continue;
      depth[u] = 0;
      var q2 = [u], h2 = 0;
      while (h2 < q2.length) {
        var c = q2[h2++];
        outEdges[c].forEach(function(nx){
          if (depth[nx] === -1) { depth[nx] = depth[c] + 1; q2.push(nx); }
        });
      }
    }

    var layers = {};
    var maxDepth = 0;
    for (var li = 0; li < N; li++) {
      var d = depth[li] || 0;
      if (!layers[d]) layers[d] = [];
      layers[d].push(li);
      if (d > maxDepth) maxDepth = d;
    }

    /* initial order */
    Object.keys(layers).forEach(function(k){
      layers[k].sort(function(a,b){
        if (outDeg[b] !== outDeg[a]) return outDeg[b] - outDeg[a];
        return _nodeName(a).localeCompare(_nodeName(b));
      });
    });

    /* barycenter sweeps to reduce crossings */
    for (var pass = 0; pass < 4; pass++) {
      /* top-down */
      for (var d1 = 1; d1 <= maxDepth; d1++) {
        var prev = layers[d1 - 1] || [];
        var curL = layers[d1] || [];
        var prevPos = {};
        for (var p = 0; p < prev.length; p++) prevPos[prev[p]] = p;
        curL.sort(function(a,b){
          function bary(idx){
            var preds = inEdges[idx].filter(function(x){ return depth[x] === d1 - 1; });
            if (!preds.length) return 1e9;
            var sum = 0, cnt = 0;
            preds.forEach(function(pr){
              var ww = w[pr + '|' + idx] || 1;
              sum += (prevPos[pr] != null ? prevPos[pr] : 0) * ww;
              cnt += ww;
            });
            return cnt ? (sum / cnt) : 1e9;
          }
          var ba = bary(a), bb = bary(b);
          if (ba !== bb) return ba - bb;
          return _nodeName(a).localeCompare(_nodeName(b));
        });
        layers[d1] = curL;
      }
      /* bottom-up */
      for (var d2 = maxDepth - 1; d2 >= 0; d2--) {
        var next = layers[d2 + 1] || [];
        var curB = layers[d2] || [];
        var nextPos = {};
        for (var np = 0; np < next.length; np++) nextPos[next[np]] = np;
        curB.sort(function(a,b){
          function bary(idx){
            var succ = outEdges[idx].filter(function(x){ return depth[x] === d2 + 1; });
            if (!succ.length) return 1e9;
            var sum = 0, cnt = 0;
            succ.forEach(function(sc){
              var ww = w[idx + '|' + sc] || 1;
              sum += (nextPos[sc] != null ? nextPos[sc] : 0) * ww;
              cnt += ww;
            });
            return cnt ? (sum / cnt) : 1e9;
          }
          var ba = bary(a), bb = bary(b);
          if (ba !== bb) return ba - bb;
          return _nodeName(a).localeCompare(_nodeName(b));
        });
        layers[d2] = curB;
      }
    }

    /* centered layer positions */
    var H_SEP = 340, V_SEP = 190, PAD_X = 40, PAD_Y = 40;
    var layerW = {};
    var maxW = 0;
    for (var d3 = 0; d3 <= maxDepth; d3++) {
      var cnt = (layers[d3] || []).length;
      var wrow = cnt ? ((cnt - 1) * V_SEP) : 0;
      layerW[d3] = wrow;
      if (wrow > maxW) maxW = wrow;
    }

    var positions = {};
    for (var d4 = 0; d4 <= maxDepth; d4++) {
      var row = layers[d4] || [];
      var startY = PAD_Y + Math.max(0, (maxW - layerW[d4]) / 2);
      var x = PAD_X + d4 * H_SEP;
      for (var r = 0; r < row.length; r++) {
        positions[nodes[row[r]].id] = { x: x, y: startY + r * V_SEP };
      }
    }
    return positions;
  }

  function buildModuleView(payload, levelLabel) {
    var inner = document.getElementById('cgx-mv-canvas-inner');
    var emptyEl = document.getElementById('cgx-mv-empty');
    if (!inner) return;
    /* Strip everything inside except the SVG arrows layer (which we'll keep & reuse). */
    Array.prototype.forEach.call(inner.querySelectorAll('.cgx-mv-card'), function(c){ c.remove(); });
    var svg = document.getElementById('cgx-mv-arrows');
    if (svg) svg.innerHTML = '';
    _mvCardById = {};
    _mvCardByNodeId = {};
    _mvFnIndex = {};
    _mvAggEdges = (payload && payload.edges) ? payload.edges.slice() : [];
    window.CG_EDGE_NODE_NAMES = {};
    _mvSetMultiSel({});
    _mvZoom = 1.0;
    _mvPanX = 0;
    _mvPanY = 0;
    _mvApplyTransform();

    var nodes = (payload && payload.nodes) ? payload.nodes : [];
    if (emptyEl) {
      emptyEl.style.display = nodes.length ? 'none' : 'block';
      if (!nodes.length) {
        emptyEl.textContent = 'No data for ' + (levelLabel || 'this') +
          ' view yet — try a different render level or supply architecture.modules in the config.';
        return;
      }
    }

    var positions = _mvComputeLayout(nodes, _mvAggEdges);
    var maxX = 0, maxY = 0;

    nodes.forEach(function(node){
      var key = _mvCardKey(node);
      window.CG_EDGE_NODE_NAMES[node.id] = key || node.id;
      var hierarchy = _mvParseHierarchy(node);
      var fnCount = 0;
      hierarchy.forEach(function(f){ fnCount += (f.fns||[]).length; });
      var inE = 0, outE = 0;
      _mvAggEdges.forEach(function(e){
        if (e.from === node.id) outE++;
        if (e.to   === node.id) inE++;
      });

      var card = _h('div', {'class':'cgx-mv-card', 'data-key':key, 'data-id':node.id});
      var pos = positions[node.id] || { x: 40, y: 40 };
      card.style.left = pos.x + 'px';
      card.style.top  = pos.y + 'px';
      maxX = Math.max(maxX, pos.x + 320);
      maxY = Math.max(maxY, pos.y + 240);

      var headInner = '<span class="cgx-mv-toggle"></span>' +
        '<span class="cgx-mv-name">' + esc(key) + '</span>' +
        '<span class="cgx-mv-badge">' + esc(levelLabel || (node.meta && node.meta.func_type) || '') + '</span>';
      var head = _h('div', {'class':'cgx-mv-head'}, headInner);
      var summary = _h('div', {'class':'cgx-mv-summary'},
        '<span>' + hierarchy.length + ' file(s)</span>' +
        '<span>' + fnCount + ' fn(s)</span>' +
        '<span style="color:#74b3f7">in: ' + inE + '</span>' +
        '<span style="color:#e08a00">out: ' + outE + '</span>');
      var body = _h('div', {'class':'cgx-mv-body'}, '');

      hierarchy.forEach(function(filerec){
        var fp = filerec.file;
        var fns = filerec.fns || [];
        var fileEl = _h('div', {'class':'cgx-mv-file','data-file':fp},
          '<div class="cgx-mv-file-head">'+
            '<span class="cgx-mv-ftoggle"></span>'+
            '<span class="cgx-mv-fname">' + esc(_basename(fp)) + '</span>'+
            '<span class="cgx-mv-fmeta">'+ fns.length +' fn(s)</span>'+
          '</div>'+
          '<div class="cgx-mv-fns"></div>'
        );
        var fnList = fns.slice(0, _mvTopN);
        var hidden = fns.length - fnList.length;
        var fnsBox = fileEl.querySelector('.cgx-mv-fns');
        function _addFnRow(pair) {
          var fid = pair[0], flabel = pair[1];
          _mvFnIndex[fid] = {nodeId: node.id, file: fp};
          if (!window.CG_EDGE_NODE_NAMES[fid]) window.CG_EDGE_NODE_NAMES[fid] = flabel || fid;
          var row = _h('div', {'class':'cgx-mv-fn','data-fid':fid}, esc(flabel));
          row.addEventListener('click', function(ev){
            ev.stopPropagation();
            try {
              if (typeof cgOpenModalById === 'function') cgOpenModalById(fid);
            } catch(err) { console.warn('module-view: cannot open detail modal', err); }
          });
          fnsBox.appendChild(row);
        }
        fnList.forEach(_addFnRow);
        fns.slice(fnList.length).forEach(function(pair){
          if (pair && pair[0]) _mvFnIndex[pair[0]] = {nodeId: node.id, file: fp};
        });
        if (hidden > 0) {
          var more = _h('div', {'class':'cgx-mv-fn','style':'color:#74b3f7'},
            '… show '+hidden+' more');
          more.addEventListener('click', function(ev){
            ev.stopPropagation();
            fns.slice(_mvTopN).forEach(_addFnRow);
            more.remove();
            _mvRedrawArrows();
          });
          fnsBox.appendChild(more);
        }
        fileEl.querySelector('.cgx-mv-file-head').addEventListener('click', function(ev){
          ev.stopPropagation();
          fileEl.classList.toggle('open');
          _mvRedrawArrows();
        });
        body.appendChild(fileEl);
      });

      card.appendChild(head);
      card.appendChild(summary);
      card.appendChild(body);
      _mvWireCard(card, head);

      inner.appendChild(card);
      _mvCardById[key] = card;
      _mvCardByNodeId[node.id] = card;
    });

    /* Grow canvas to fit. */
    inner.style.minWidth  = (maxX + 80) + 'px';
    inner.style.minHeight = (maxY + 80) + 'px';

    _mvLoadPositions();
    _mvRedrawArrows();

    /* Initial fit uses the same pan/zoom state as later interactions. */
    _mvFitToView();
  }

  /* Fit without a second transform path; drag and arrows stay in canvas coords. */
  function _mvFitToView() {
    var canvas = document.getElementById('cgx-mv-canvas');
    if (!canvas) return;
    var maxR = 0, maxB = 0;
    Object.keys(_mvCardById).forEach(function(k){
      var c = _mvCardById[k];
      var r = (c.offsetLeft || 0) + (c.offsetWidth  || 0);
      var b = (c.offsetTop  || 0) + (c.offsetHeight || 0);
      if (r > maxR) maxR = r;
      if (b > maxB) maxB = b;
    });
    var vw = (canvas.clientWidth  || 800) - 24;
    var vh = (canvas.clientHeight || 600) - 24;
    var contentW = maxR + 80;
    var contentH = maxB + 80;
    if (contentW <= 0 || contentH <= 0) return;
    var s = Math.min(vw / contentW, vh / contentH, 1.0);
    if (s < 0.4) s = 0.4;  /* floor — don't shrink past usability */
    _mvZoom = Math.max(0.35, Math.min(1.0, s));
    _mvPanX = 12;
    _mvPanY = 12;
    _mvApplyTransform();
    _mvRedrawArrows();
  }

  /* Drag-vs-click detection: a click that didn't move the card collapses/expands;
     a drag (>4px movement) repositions and suppresses the toggle. */
  function _mvWireCard(card, head) {
    var dragging = false;
    var moved = false;
    var startX, startY;
    var dragCards = [];
    head.addEventListener('mousedown', function(ev){
      if (ev.button !== 0) return;
      dragging = true;
      moved = false;
      dragCards = [];
      var id = card.getAttribute('data-id') || '';
      if (_mvMultiSel[id]) {
        Array.prototype.forEach.call(document.querySelectorAll('.cgx-mv-card.mv-multi-selected'), function(c){
          dragCards.push({card:c, origLeft:parseFloat(c.style.left)||0, origTop:parseFloat(c.style.top)||0});
        });
      }
      if (!dragCards.length) {
        var one = {}; if (id) one[id] = true; _mvSetMultiSel(one);
        dragCards.push({card:card, origLeft:parseFloat(card.style.left)||0, origTop:parseFloat(card.style.top)||0});
      }
      dragCards.forEach(function(it){ it.card.style.zIndex = 20; });
      startX = ev.clientX; startY = ev.clientY;
      ev.preventDefault();
    });
    document.addEventListener('mousemove', function(ev){
      if (!dragging) return;
      var dx = ev.clientX - startX, dy = ev.clientY - startY;
      if (!moved && (Math.abs(dx) > 4 || Math.abs(dy) > 4)) {
        moved = true;
      }
      if (moved) {
        dragCards.forEach(function(it){
          it.card.style.left = (it.origLeft + dx / _mvZoom) + 'px';
          it.card.style.top  = (it.origTop  + dy / _mvZoom) + 'px';
        });
        _mvGrowCanvas();
        _mvRedrawArrows();
      }
    });
    document.addEventListener('mouseup', function(ev){
      if (!dragging) return;
      dragging = false;
      dragCards.forEach(function(it){ it.card.style.zIndex = ''; });
      if (moved) {
        _mvSavePositions();
      } else if (dragCards.length === 1) {
        /* True click — toggle expand/collapse. */
        card.classList.toggle('open');
        _mvRedrawArrows();
      }
      moved = false;
      dragCards = [];
    });
  }

  var MV_POS_KEY = 'cgx_mv_pos_' + (typeof GRAPH_ID === 'string' ? GRAPH_ID : 'default');
  function _mvSavePositions() {
    try {
      var out = {};
      Object.keys(_mvCardById).forEach(function(k){
        var c = _mvCardById[k];
        if (c.style.left && c.style.top) out[k] = { left: c.style.left, top: c.style.top };
      });
      localStorage.setItem(MV_POS_KEY + '_' + (_mvBuiltFor || ''), JSON.stringify(out));
    } catch(e){}
  }
  function _mvLoadPositions() {
    try {
      var raw = localStorage.getItem(MV_POS_KEY + '_' + (_mvBuiltFor || ''));
      if (!raw) return;
      var pos = JSON.parse(raw);
      Object.keys(pos).forEach(function(k){
        var c = _mvCardById[k];
        if (c && pos[k] && pos[k].left && pos[k].top) {
          c.style.left = pos[k].left;
          c.style.top  = pos[k].top;
        }
      });
    } catch(e){}
  }

  function _mvRectInInner(el) {
    var inner = document.getElementById('cgx-mv-canvas-inner');
    if (!inner || !el) return null;
    var left = 0, top = 0, cur = el;
    while (cur && cur !== inner) {
      left += cur.offsetLeft || 0;
      top  += cur.offsetTop  || 0;
      cur = cur.offsetParent;
    }
    return { left:left, top:top, right:left + el.offsetWidth, bottom:top + el.offsetHeight,
             width:el.offsetWidth, height:el.offsetHeight };
  }

  function _mvEdgeRectInInner(el) {
    var r = _mvRectInInner(el);
    if (!r) return null;
    var card = el.closest && el.closest('.cgx-mv-card');
    if (!card || card === el) return r;
    var cr = _mvRectInInner(card);
    if (!cr) return r;
    return {
      left: cr.left, right: cr.right, width: cr.width,
      top: r.top, bottom: r.bottom, height: r.height
    };
  }

  /* Script-style edge path between two arbitrary elements (cards OR function rows),
   * in untransformed canvas-inner coordinates.
   *
   * Bleed slightly into the card border so SVG anti-aliasing doesn't leave a
   * visible gap between the arrow and the destination card in card-based modes.
   */
  var MV_EDGE_TOUCH_BLEED = 2.0;
  function _mvSmoothPathRects(sEl, dEl, lane) {
    var sR = _mvEdgeRectInInner(sEl);
    var dR = _mvEdgeRectInInner(dEl);
    if (!sR || !dR) return '';
    var sCenterX = sR.left + sR.width / 2;
    var dCenterX = dR.left + dR.width / 2;
    var goRight = sCenterX < dCenterX;
    var sx = goRight ? sR.right - MV_EDGE_TOUCH_BLEED : sR.left + MV_EDGE_TOUCH_BLEED;
    var dx = goRight ? dR.left + MV_EDGE_TOUCH_BLEED : dR.right - MV_EDGE_TOUCH_BLEED;
    var sy = sR.top + sR.height / 2;
    var dy = dR.top + dR.height / 2;
    if (window.cgStraightEdges) {
      return 'M' + sx + ',' + sy + ' L' + dx + ',' + dy;
    }
    var gap = Math.max(55, Math.abs(dx - sx) * 0.42);
    var c1x = goRight ? sx + gap : sx - gap;
    var c2x = goRight ? dx - gap : dx + gap;
    return 'M' + sx + ',' + sy + ' C' + c1x + ',' + sy + ' ' + c2x + ',' + dy + ' ' + dx + ',' + dy;
  }
  function _mvOrthoPathRects(sEl, dEl, lane) { return _mvSmoothPathRects(sEl, dEl, lane); }
  function _mvOrthoPath(srcCard, dstCard, lane) { return _mvSmoothPathRects(srcCard, dstCard, lane); }

  function _mvCanvasBounds() {
    var minL = 0, minT = 0, maxR = 1200, maxB = 600;
    Object.keys(_mvCardById).forEach(function(k){
      var c = _mvCardById[k];
      var l = c.offsetLeft || 0;
      var t = c.offsetTop || 0;
      var r = l + (c.offsetWidth || 0);
      var b = t + (c.offsetHeight || 0);
      if (l < minL) minL = l;
      if (t < minT) minT = t;
      if (r > maxR) maxR = r;
      if (b > maxB) maxB = b;
    });
    return {minL:minL, minT:minT, maxR:maxR, maxB:maxB};
  }

  /* Expand canvas-inner to cover every card's current footprint plus padding.
   * Prevents arrow clipping when cards are dragged outside the initial bounds. */
  function _mvGrowCanvas() {
    var inner = document.getElementById('cgx-mv-canvas-inner');
    if (!inner) return;
    var b = _mvCanvasBounds();
    inner.style.minWidth  = (Math.max(1200, b.maxR - b.minL) + 240) + 'px';
    inner.style.minHeight = (Math.max(600,  b.maxB - b.minT) + 240) + 'px';
  }

  /* Visible function nodes inside an expanded card. Returns set of fn ids
   * where the file row is also open. */
  function _mvVisibleFunctionIds(card) {
    var out = {};
    if (!card || !card.classList.contains('open')) return out;
    Array.prototype.forEach.call(card.querySelectorAll('.cgx-mv-file.open .cgx-mv-fn'), function(row){
      var id = row.getAttribute('data-fid');
      if (id) out[id] = row;
    });
    return out;
  }

  function _mvAnchorForFn(fid) {
    var info = _mvFnIndex[fid];
    if (!info) return null;
    var card = _mvCardByNodeId[info.nodeId];
    if (!card) return null;
    if (card.classList.contains('open')) {
      var fileEl = card.querySelector('.cgx-mv-file[data-file="' + _cssEscape(info.file) + '"]');
      if (fileEl) {
        if (fileEl.classList.contains('open')) {
          var row = fileEl.querySelector('.cgx-mv-fn[data-fid="' + _cssEscape(fid) + '"]');
          if (row) return {el:row, key:'fn:' + fid, cardId:info.nodeId};
        }
        var fileHead = fileEl.querySelector('.cgx-mv-file-head');
        if (fileHead) return {el:fileHead, key:'file:' + info.nodeId + ':' + info.file, cardId:info.nodeId};
      }
    }
    return {el:card, key:'card:' + info.nodeId, cardId:info.nodeId};
  }

  /* Cap function-level arrows per card pair so big modules don't explode. */
  var MV_FN_PAIR_CAP = 500;

  function _mvRedrawArrows() {
    var svg = document.getElementById('cgx-mv-arrows');
    var inner = document.getElementById('cgx-mv-canvas-inner');
    if (!svg || !inner) return;
    _mvGrowCanvas();
    var bounds = _mvCanvasBounds();
    var pad = 180;
    var minX = bounds.minL - pad, minY = bounds.minT - pad;
    var width = Math.max(inner.scrollWidth, bounds.maxR - minX + pad);
    var height = Math.max(inner.scrollHeight, bounds.maxB - minY + pad, 400);
    svg.style.left = minX + 'px';
    svg.style.top = minY + 'px';
    svg.setAttribute('width', width);
    svg.setAttribute('height', height);
    /* Keep CSS box size in sync with SVG user units (avoid endpoint drift). */
    svg.style.width = width + 'px';
    svg.style.height = height + 'px';
    svg.setAttribute('viewBox', minX + ' ' + minY + ' ' + width + ' ' + height);
    /* No local <defs> — use the shared #cg-arrow-defs markers. */
    svg.innerHTML = '';

    function _mvEdgeStyle(edge, cat) {
      if (cat === 'violation') return {stroke:'#e23b3b', dash:'', width:1.5, marker:'cg-arr-solid', legendKey:cgEdgeCategoryToLegend(cat)};
      if ((edge && edge.confidence === 'HEURISTIC') || cat === 'heuristic' || cat === 'unresolved' || cat === 'external')
        return {stroke:'#888888', dash:'6 4', width:1.5, marker:'cg-arr-dashed', legendKey:cgEdgeCategoryToLegend(cat)};
      return {stroke:'#6c8ebf', dash:'', width:1.5, marker:'cg-arr-solid', legendKey:cgEdgeCategoryToLegend(cat)};
    }
    function _addArrow(pathD, cat, reason, edge, widthOverride) {
      if (!pathD) return;
      var st = _mvEdgeStyle(edge, cat);
      if (!window.cgEdgeFilter[st.legendKey]) return;
      var pathEl = document.createElementNS('http://www.w3.org/2000/svg','path');
      pathEl.setAttribute('class', 'cg-sv-edge cgx-mv-edge');
      pathEl.setAttribute('d', pathD);
      pathEl.setAttribute('fill','none');
      pathEl.setAttribute('stroke', st.stroke);
      pathEl.setAttribute('stroke-width', (widthOverride || st.width));
      if (st.dash) pathEl.setAttribute('stroke-dasharray', st.dash);
      pathEl.setAttribute('marker-end', 'url(#' + st.marker + ')');
      pathEl.setAttribute('opacity', '0.55');
      pathEl.setAttribute('data-cat', cat);
      if (edge && edge.id != null) pathEl.setAttribute('data-eid', String(edge.id));
      pathEl.style.pointerEvents = 'stroke';
      pathEl.addEventListener('click', function(ev){
        ev.stopPropagation();
        if (typeof window.cgShowEdgeDetails === 'function') window.cgShowEdgeDetails(edge, ev);
      });
      var title = document.createElementNS('http://www.w3.org/2000/svg','title');
      title.textContent = cat.toUpperCase() + (reason ? ' — ' + reason : '');
      pathEl.appendChild(title);
      svg.appendChild(pathEl);
    }

    /* Pre-compute the visible function rows for each open card. */
    var visibleFnsByCard = {};
    Object.keys(_mvCardByNodeId).forEach(function(nid){
      visibleFnsByCard[nid] = _mvVisibleFunctionIds(_mvCardByNodeId[nid]);
    });
    /* Reverse index: function-id -> {card, row, cardId}. */
    var fnIndex = {};
    Object.keys(visibleFnsByCard).forEach(function(cardId){
      var map = visibleFnsByCard[cardId];
      Object.keys(map).forEach(function(fid){
        fnIndex[fid] = { cardId: cardId, row: map[fid] };
      });
    });

    /* PASS 1 — function-to-function arrows when BOTH sides are expanded
     * to function rows. Uses primary EDGE_DATA (function-level edges). */
    var pairCounter = {};
    var drawnFnPairs = {};
    var drawnFunctionEdges = 0;
    var visualEdgeCount = 0;
    if (typeof EDGE_DATA !== 'undefined' && EDGE_DATA.length) {
      for (var i = 0; i < EDGE_DATA.length; ++i) {
        var e = EDGE_DATA[i];
        var sIdx = _mvAnchorForFn(e.from), dIdx = _mvAnchorForFn(e.to);
        if (!sIdx || !dIdx) continue;
        if (_mvHideIntra && sIdx.cardId === dIdx.cardId) continue;
        var cat = e.category || 'exact';
        /* _addArrow itself short-circuits on filter; we still count pairs first
         * so the cap is consistent regardless of visibility. */
        var pairKey = sIdx.key + '||' + dIdx.key + '||' + cat;
        pairCounter[pairKey] = (pairCounter[pairKey] || 0) + 1;
        if (pairCounter[pairKey] > 1 || pairCounter[pairKey] > MV_FN_PAIR_CAP) continue;
        _addArrow(_mvOrthoPathRects(sIdx.el, dIdx.el, visualEdgeCount++), cat, e.reason || '', e);
        drawnFnPairs[pairKey] = true;
        drawnFunctionEdges++;
      }
    }

    /* PASS 2 — aggregated card-to-card arrows. Skip pairs that already have
     * function-level arrows so we don't draw two parallel lines per pair. */
    _mvAggEdges.forEach(function(e){
      if (drawnFunctionEdges) return;
      var cat = e.category || 'aggregated';
      if (_mvHideIntra && e.from === e.to) return;
      var sCard = _mvCardByNodeId[e.from], dCard = _mvCardByNodeId[e.to];
      if (!sCard || !dCard) return;
      var pairKey = e.from + '||' + e.to;
      if (drawnFnPairs[pairKey]) return;
        _addArrow(_mvOrthoPath(sCard, dCard, visualEdgeCount++), cat, e.reason || '', e);
    });
  }

  function _mvRebuildActive(skipSave) {
    var slotId = _activeSlotId || 'slot1';
    var slot = SLOTS[slotId];
    if (!slot) return;
    var openCards = {};
    var openFiles = {};
    Array.prototype.forEach.call(document.querySelectorAll('.cgx-mv-card.open'), function(c){
      openCards[c.getAttribute('data-id')] = true;
    });
    Array.prototype.forEach.call(document.querySelectorAll('.cgx-mv-file.open'), function(f){
      var c = f.closest('.cgx-mv-card');
      if (c) openFiles[c.getAttribute('data-id') + '|' + f.getAttribute('data-file')] = true;
    });
    if (!skipSave) _mvSavePositions();
    var level = slot.level || 'module';
    _mvBuiltFor = slotId + ':' + level;
    buildModuleView(slot.payload, level.charAt(0).toUpperCase() + level.slice(1));
    Object.keys(openCards).forEach(function(id){
      var c = _mvCardByNodeId[id];
      if (c) c.classList.add('open');
    });
    Object.keys(openFiles).forEach(function(key){
      var parts = key.split('|');
      var c = _mvCardByNodeId[parts[0]];
      if (c) {
        var f = c.querySelector('.cgx-mv-file[data-file="' + _cssEscape(parts.slice(1).join('|')) + '"]');
        if (f) f.classList.add('open');
      }
    });
    _mvRedrawArrows();
  }

  /* Module-view sidebar controls */
  var mvExpandAllBtn = document.getElementById('cgx-mv-expand-all');
  if (mvExpandAllBtn) mvExpandAllBtn.addEventListener('click', function(){
    Array.prototype.forEach.call(document.querySelectorAll('.cgx-mv-card'), function(c){ c.classList.add('open'); });
    Array.prototype.forEach.call(document.querySelectorAll('.cgx-mv-file'), function(f){ f.classList.add('open'); });
    _mvRedrawArrows();
  });
  var mvCollapseBtn = document.getElementById('cgx-mv-collapse-all');
  if (mvCollapseBtn) mvCollapseBtn.addEventListener('click', function(){
    Array.prototype.forEach.call(document.querySelectorAll('.cgx-mv-card.open'), function(c){ c.classList.remove('open'); });
    Array.prototype.forEach.call(document.querySelectorAll('.cgx-mv-file.open'), function(f){ f.classList.remove('open'); });
    _mvRedrawArrows();
  });
  var mvExpandBtn = document.getElementById('cgx-mv-expand-one');
  if (mvExpandBtn) mvExpandBtn.addEventListener('click', function(){
    var cards = Array.prototype.slice.call(document.querySelectorAll('.cgx-mv-card'));
    var closedCard = cards.some(function(c){ return !c.classList.contains('open'); });
    if (closedCard) {
      cards.forEach(function(c){ c.classList.add('open'); });
    } else {
      Array.prototype.forEach.call(document.querySelectorAll('.cgx-mv-card.open .cgx-mv-file'), function(f){ f.classList.add('open'); });
    }
    _mvRedrawArrows();
  });
  var mvCollapseOneBtn = document.getElementById('cgx-mv-collapse-one');
  if (mvCollapseOneBtn) mvCollapseOneBtn.addEventListener('click', function(){
    var openFiles = Array.prototype.slice.call(document.querySelectorAll('.cgx-mv-file.open'));
    if (openFiles.length) {
      openFiles.forEach(function(f){ f.classList.remove('open'); });
    } else {
      Array.prototype.forEach.call(document.querySelectorAll('.cgx-mv-card.open'), function(c){ c.classList.remove('open'); });
    }
    _mvRedrawArrows();
  });
  var mvHideIntraCb = document.getElementById('cgx-mv-hide-intra');
  if (mvHideIntraCb) mvHideIntraCb.addEventListener('change', function(ev){
    _mvHideIntra = ev.target.checked;
    _mvRedrawArrows();
  });
  var mvTopNInput = document.getElementById('cgx-mv-topn');
  function _mvApplyTopNInput(ev) {
    var n = parseInt(ev.target.value, 10);
    if (!isNaN(n) && n > 0 && n !== _mvTopN) { _mvTopN = n; _mvRebuildActive(); }
  }
  if (mvTopNInput) {
    mvTopNInput.addEventListener('change', _mvApplyTopNInput);
    mvTopNInput.addEventListener('input', _mvApplyTopNInput);
  }

  (function(){
    var canvas = document.getElementById('cgx-mv-canvas');
    if (!canvas || canvas._mvViewportWired) return;
    canvas._mvViewportWired = true;
    canvas.addEventListener('wheel', function(ev){
      if (!document.getElementById('cg-module-view').classList.contains('active')) return;
      ev.preventDefault();
      var factor = ev.deltaY < 0 ? 1.1 : 0.909;
      var rect = canvas.getBoundingClientRect();
      var mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
      var oldZoom = _mvZoom;
      _mvZoom = Math.max(0.08, Math.min(3.0, _mvZoom * factor));
      var actual = _mvZoom / oldZoom;
      _mvPanX = mx - (mx - _mvPanX) * actual;
      _mvPanY = my - (my - _mvPanY) * actual;
      _mvApplyTransform();
    }, { passive:false });
    canvas.addEventListener('mousedown', function(ev){
      if (ev.button !== 0 && ev.button !== 1) return;
      if (ev.target.closest && ev.target.closest('.cgx-mv-edge')) return;
      if (ev.target.closest && ev.target.closest('.cgx-mv-card')) return;
      if (window.cgCloseEdgePopup) window.cgCloseEdgePopup();
      ev.preventDefault();
      /* Middle mouse always pans */
      if (ev.button === 1) {
        _mvViewDrag = { startX:ev.clientX, startY:ev.clientY, origPanX:_mvPanX, origPanY:_mvPanY };
        canvas.classList.add('mv-panning');
        return;
      }
      if (ev.altKey) {
        _mvViewDrag = { startX:ev.clientX, startY:ev.clientY, origPanX:_mvPanX, origPanY:_mvPanY };
        canvas.classList.add('mv-panning');
        return;
      }
      var r = canvas.getBoundingClientRect();
      var p0 = {x:ev.clientX-r.left, y:ev.clientY-r.top};
      var box = document.createElement('div');
      box.className = 'cg-marquee-box';
      canvas.appendChild(box);
      _mvMarquee = {canvas:canvas, box:box, start:p0, cur:p0};
      _setBoxRect(box, _mkRect(p0, p0));
    });
    document.addEventListener('mousemove', function(ev){
      if (_mvMarquee) {
        var r2 = _mvMarquee.canvas.getBoundingClientRect();
        _mvMarquee.cur = {x:ev.clientX-r2.left, y:ev.clientY-r2.top};
        var mr = _mkRect(_mvMarquee.start, _mvMarquee.cur);
        _setBoxRect(_mvMarquee.box, mr);
        var rr = {
          left: (mr.left - _mvPanX) / _mvZoom,
          top: (mr.top - _mvPanY) / _mvZoom,
          right: (mr.right - _mvPanX) / _mvZoom,
          bottom: (mr.bottom - _mvPanY) / _mvZoom
        };
        var map = {};
        Array.prototype.forEach.call(document.querySelectorAll('.cgx-mv-card'), function(card){
          var cr = {
            left: parseFloat(card.style.left)||0,
            top: parseFloat(card.style.top)||0,
            right: (parseFloat(card.style.left)||0) + (card.offsetWidth||0),
            bottom: (parseFloat(card.style.top)||0) + (card.offsetHeight||0)
          };
          if (_rectIntersects(rr, cr)) map[card.getAttribute('data-id') || ''] = true;
        });
        _mvSetMultiSel(map);
        return;
      }
      if (!_mvViewDrag) return;
      _mvPanX = _mvViewDrag.origPanX + (ev.clientX - _mvViewDrag.startX);
      _mvPanY = _mvViewDrag.origPanY + (ev.clientY - _mvViewDrag.startY);
      _mvApplyTransform();
    });
    document.addEventListener('mouseup', function(){
      if (_mvMarquee) {
        if (_mvMarquee.box && _mvMarquee.box.parentNode) _mvMarquee.box.parentNode.removeChild(_mvMarquee.box);
        _mvMarquee = null;
      }
      if (!_mvViewDrag) return;
      _mvViewDrag = null;
      canvas.classList.remove('mv-panning');
    });
  })();

  /* ---------- Module View search dropdown (Var-Flow-style) ---------- */
  var mvSearchInput = document.getElementById('cgx-mv-search');
  var mvSearchDD = document.getElementById('cgx-mv-dropdown');
  var mvSearchClear = document.getElementById('cgx-mv-search-clear');

  function _mvBuildSearchIndex() {
    var entries = [];
    var slotId = _activeSlotId || 'slot1';
    var payload = (SLOTS[slotId] && SLOTS[slotId].payload) || {};
    var levelLabel = (SLOTS[slotId] && SLOTS[slotId].level) || 'item';
    (payload.nodes || []).forEach(function(node){
      var key = _mvCardKey(node);
      entries.push({ kind: levelLabel, name: key, id: node.id, parent: '' });
      var h = _mvParseHierarchy(node);
      h.forEach(function(rec){
        entries.push({ kind: 'file', name: _basename(rec.file), id: node.id + '#' + rec.file, parent: key, file: rec.file, parentId: node.id });
        (rec.fns || []).forEach(function(p){
          entries.push({ kind: 'fn', name: p[1], id: p[0], parent: key + ' / ' + _basename(rec.file), parentId: node.id, file: rec.file });
        });
      });
    });
    return entries;
  }

  function _mvRenderDropdown(query) {
    if (!mvSearchDD) return;
    var entries = _mvBuildSearchIndex();
    var q = (query || '').toLowerCase().trim();
    var matches = entries.filter(function(e){
      if (!q) return true;
      return e.name.toLowerCase().indexOf(q) >= 0 ||
             (e.parent && e.parent.toLowerCase().indexOf(q) >= 0);
    }).slice(0, 100);
    if (!matches.length) {
      mvSearchDD.innerHTML = '<div class="cgx-mv-dd-item"><span class="cgx-mv-dd-kind">—</span><span class="cgx-mv-dd-name">No matches</span></div>';
    } else {
      mvSearchDD.innerHTML = matches.map(function(m){
        return '<div class="cgx-mv-dd-item" data-kind="'+esc(m.kind)+'" data-id="'+esc(m.id)+'"'+
          (m.parentId ? ' data-parent-id="'+esc(m.parentId)+'"' : '') +
          (m.file ? ' data-file="'+esc(m.file)+'"' : '') + '>'+
          '<span class="cgx-mv-dd-kind">'+esc(m.kind)+'</span>'+
          '<span class="cgx-mv-dd-name">'+esc(m.name)+'</span>'+
          (m.parent ? '<span class="cgx-mv-dd-parent">in '+esc(m.parent)+'</span>' : '')+
          '</div>';
      }).join('');
    }
    mvSearchDD.style.display = 'block';
  }

  function _mvFocusSearchResult(item) {
    var card = null;
    if (item.dataset.kind === 'fn') {
      var pid = item.dataset.parentId;
      var fid = item.dataset.id;
      card = _mvCardByNodeId[pid];
      if (card) {
        card.classList.add('open');
        if (item.dataset.file) {
          var file = card.querySelector('.cgx-mv-file[data-file="'+_cssEscape(item.dataset.file)+'"]');
          if (file) file.classList.add('open');
        }
        var row = card.querySelector('.cgx-mv-fn[data-fid="'+_cssEscape(fid)+'"]');
        if (row) row.scrollIntoView({block:'center', behavior:'smooth'});
      }
    } else if (item.dataset.kind === 'file') {
      card = _mvCardByNodeId[item.dataset.parentId];
      if (card) {
        card.classList.add('open');
        var fl = card.querySelector('.cgx-mv-file[data-file="'+_cssEscape(item.dataset.file)+'"]');
        if (fl) { fl.classList.add('open'); fl.scrollIntoView({block:'center', behavior:'smooth'}); }
      }
    } else {
      card = _mvCardByNodeId[item.dataset.id];
      if (card) {
        card.classList.add('open');
        card.scrollIntoView({block:'center', inline:'center', behavior:'smooth'});
      }
    }
    /* Brief highlight */
    if (card) {
      card.classList.add('matched');
      setTimeout(function(){ card.classList.remove('matched'); }, 1400);
    }
    _mvRedrawArrows();
    if (mvSearchDD) mvSearchDD.style.display = 'none';
  }

  function _mvCenterElement(el) {
    var canvas = document.getElementById('cgx-mv-canvas');
    var r = _mvRectInInner(el);
    if (!canvas || !r) return;
    var cx = r.left + r.width / 2;
    var cy = r.top + r.height / 2;
    _mvPanX = (canvas.clientWidth || 800) / 2 - cx * _mvZoom;
    _mvPanY = (canvas.clientHeight || 600) / 2 - cy * _mvZoom;
    _mvApplyTransform();
  }

  function _mvEnsureVisibleFn(fid) {
    var info = _mvFnIndex[fid];
    if (!info) return null;
    var card = _mvCardByNodeId[info.nodeId];
    if (!card) return null;
    card.classList.add('open');
    var fileEl = card.querySelector('.cgx-mv-file[data-file="' + _cssEscape(info.file) + '"]');
    if (fileEl) fileEl.classList.add('open');
    var row = fileEl ? fileEl.querySelector('.cgx-mv-fn[data-fid="' + _cssEscape(fid) + '"]') : null;
    _mvRedrawArrows();
    return row || (fileEl && fileEl.querySelector('.cgx-mv-file-head')) || card;
  }

  function _mvClearHighlight() {
    Array.prototype.forEach.call(document.querySelectorAll('.cgx-mv-card.dim, .cgx-mv-card.matched, .cgx-mv-fn.matched, .cgx-mv-file-head.matched'), function(el){
      el.classList.remove('dim', 'matched');
    });
  }

  window.cgxModuleHighlight = function(ids, centerFirst) {
    _mvClearHighlight();
    var seenCards = {};
    (ids || []).forEach(function(fid){
      var info = _mvFnIndex[fid];
      if (!info) return;
      var card = _mvCardByNodeId[info.nodeId];
      if (!card) return;
      seenCards[info.nodeId] = true;
      card.classList.add('matched');
      var anchor = _mvEnsureVisibleFn(fid);
      if (anchor) anchor.classList.add('matched');
    });
    Array.prototype.forEach.call(document.querySelectorAll('.cgx-mv-card'), function(card){
      if (!seenCards[card.getAttribute('data-id')]) card.classList.add('dim');
    });
    if (centerFirst && ids && ids.length) window.cgxModuleCenter(ids[0]);
    _mvRedrawArrows();
  };

  window.cgxModuleCenter = function(fid) {
    var el = _mvEnsureVisibleFn(fid);
    if (el) _mvCenterElement(el);
  };

  window.cgxModuleExpand = function(visited, target) {
    var ids = Object.keys(visited || {});
    ids.forEach(_mvEnsureVisibleFn);
    if (target) window.cgxModuleCenter(target);
    _mvRedrawArrows();
  };

  window.cgxModuleIsolate = function(visited, target) {
    window.cgxModuleHighlight(Object.keys(visited || {}), false);
    if (target) window.cgxModuleCenter(target);
  };

  window.cgxModuleClearFocus = function() {
    _mvClearHighlight();
    _mvRedrawArrows();
  };
  window.cgxModuleFit = _mvFitToView;
  window.cgxModuleSaveLayout = _mvSavePositions;
  window.cgxModuleResetLayout = function() {
    try { localStorage.removeItem(MV_POS_KEY + '_' + (_mvBuiltFor || '')); } catch(e) {}
    _mvRebuildActive(true);
  };
  window.cgxModuleClearSaved = window.cgxModuleResetLayout;
  window.cgxModuleSearch = function(q) {
    if (mvSearchInput) mvSearchInput.value = q || '';
    _mvRenderDropdown(q || '');
  };
  document.addEventListener('cg:straight-edges:change', function(){ _mvRedrawArrows(); });

  if (mvSearchInput) {
    mvSearchInput.addEventListener('focus', function(){
      _mvRenderDropdown(mvSearchInput.value);
    });
    mvSearchInput.addEventListener('input', function(ev){
      _mvSearch = (ev.target.value || '').toLowerCase().trim();
      _mvRenderDropdown(_mvSearch);
    });
    mvSearchInput.addEventListener('blur', function(){
      setTimeout(function(){ if (mvSearchDD) mvSearchDD.style.display = 'none'; }, 150);
    });
  }
  if (mvSearchDD) {
    mvSearchDD.addEventListener('mousedown', function(ev){
      var item = ev.target.closest('.cgx-mv-dd-item');
      if (item && item.dataset.id) {
        ev.preventDefault();
        _mvFocusSearchResult(item);
      }
    });
  }
  if (mvSearchClear) {
    mvSearchClear.addEventListener('click', function(){
      if (mvSearchInput) { mvSearchInput.value = ''; mvSearchInput.focus(); }
      if (mvSearchDD) mvSearchDD.style.display = 'none';
    });
  }

  /* ---------- Mode registry ---------- */
  /* The main IIFE exposes setViewMode on window so we can call it from here. */
  var _origSetViewMode = window._cgOrigSetViewMode || window.setViewMode;

  function _hideAllViews() {
    var netEl = document.getElementById('mynetwork');
    var netWrap = (netEl && netEl.parentElement && netEl.parentElement !== document.body) ? netEl.parentElement : netEl;
    if (netWrap) netWrap.style.display = 'none';
    var svEl = document.getElementById('cg-script-view');
    if (svEl) svEl.style.display = 'none';
    var vfEl = document.getElementById('cg-varflow-view');
    if (vfEl) vfEl.style.display = 'none';
    var incEl = document.getElementById('cgx-inc-view');
    if (incEl) { incEl.classList.remove('cg-iv-active'); }
    var mvEl = document.getElementById('cg-module-view');
    if (mvEl) mvEl.classList.remove('active');
    [btnFn, btnSv, btnVf, btnInc].forEach(function(b){ if (b) b.classList.remove('active'); });
    var mvCtl = document.getElementById('cgx-mv-controls');
    if (mvCtl) mvCtl.style.display = 'none';
  }

  /* All activators are wrapped in _safeActivate so a crash in one mode
     does not corrupt global state. NODE_DATA / EDGE_DATA are NEVER mutated —
     the function-level graph stays primary throughout. Script view groups
     by file_path naturally; module/folder/library/namespace views read their
     own slot payload (`SLOTS[slotId].payload`) directly. */

  function _showErrorOverlay(title, err) {
    var ov = document.getElementById('cgx-error-overlay');
    var t  = document.getElementById('cgx-error-title');
    var m  = document.getElementById('cgx-error-msg');
    var d  = document.getElementById('cgx-error-detail');
    if (!ov) { console.error(title, err); return; }
    if (t) t.textContent = title;
    if (m) m.textContent = 'This mode failed to render. Other modes are still available — click "Back to Slot 1" or any sidebar button to switch.';
    if (d) d.textContent = (err && (err.stack || err.message)) ? (err.stack || err.message) : String(err);
    ov.classList.add('open');
  }
  function _hideErrorOverlay() {
    var ov = document.getElementById('cgx-error-overlay');
    if (ov) ov.classList.remove('open');
  }
  var errBack = document.getElementById('cgx-error-back');
  if (errBack) errBack.addEventListener('click', function(){ _hideErrorOverlay(); activateSlot('slot1'); });
  var errClose = document.getElementById('cgx-error-close');
  if (errClose) errClose.addEventListener('click', _hideErrorOverlay);

  function _safeActivate(label, fn) {
    try { _hideErrorOverlay(); fn(); }
    catch(err) { console.error('[' + label + '] activation failed:', err); _showErrorOverlay(label + ' — error', err); }
  }

  function _activateFn() {
    _hideAllViews();
    var slot = SLOTS[_activeSlotId] || SLOTS.slot1;
    var btn = slot && slot.btn ? slot.btn : btnFn;
    if (btn) btn.classList.add('active');
    if (typeof _origSetViewMode === 'function') _origSetViewMode('fn');
    setTimeout(applyEdgeFilter, 60);
  }

  function _activateScriptForSlot(slotId) {
    /* The script view groups the primary function-level NODE_DATA by file_path.
       For slot.level = 'script' or 'function' (default), no data manipulation is
       needed — script view reads NODE_DATA directly. We do NOT swap NODE_DATA
       because EDGES_BY_FROM / EDGES_BY_TO are cached at page load and a swap
       would silently corrupt every later mode. */
    _hideAllViews();
    var btn = SLOTS[slotId].btn;
    if (btn) btn.classList.add('active');
    if (typeof _origSetViewMode === 'function') _origSetViewMode('script');
    setTimeout(applyEdgeFilter, 80);
  }

  function _activateModuleForSlot(slotId) {
    _hideAllViews();
    var btn = SLOTS[slotId].btn;
    if (btn) btn.classList.add('active');
    var mvEl = document.getElementById('cg-module-view');
    if (mvEl) mvEl.classList.add('active');
    var mvCtl = document.getElementById('cgx-mv-controls');
    if (mvCtl) mvCtl.style.display = '';
    var slot = SLOTS[slotId];
    var level = slot && slot.level ? slot.level : 'module';
    var levelLabel = level.charAt(0).toUpperCase() + level.slice(1);
    var title = document.getElementById('cgx-mv-title');
    if (title) title.textContent = levelLabel + ' View';
    var builtKey = slotId + ':' + level;
    if (_mvBuiltFor !== builtKey) {
      _mvBuiltFor = builtKey;
      buildModuleView(slot && slot.payload, levelLabel);
    }
    setTimeout(_mvRedrawArrows, 30);
    setTimeout(applyEdgeFilter, 80);
  }

  function _activateVarFlow() {
    _hideAllViews();
    if (btnVf) btnVf.classList.add('active');
    if (typeof _origSetViewMode === 'function') _origSetViewMode('varflow');
  }

  function _activateInclude() {
    _hideAllViews();
    if (btnInc) btnInc.classList.add('active');
    var incEl = document.getElementById('cgx-inc-view');
    if (incEl) incEl.classList.add('cg-iv-active');
    setTimeout(function(){ if (window.cgxIncFit) window.cgxIncFit(); }, 50);
  }

  /* edge-filter implementations per mode — all read window.cgEdgeFilter */
  function _filterNetEdges() {
    var net = _cgxGetNet();
    if (!net) return;
    try {
      var edges = net.body.data.edges;
      if (!edges) return;
      var ids = edges.getIds();
      var updates = [];
      ids.forEach(function(id) {
        var ed = EDGE_DATA.find(function(d){ return String(d.id) === String(id); });
        var netEdge = edges.get(id) || {};
        var cat = (ed && ed.category) || netEdge.category || (String(id).indexOf('cg_var_edge_') === 0 ? 'var' : null);
        if (!cat) {
          var col = (typeof netEdge.color === 'string') ? netEdge.color : ((netEdge.color && netEdge.color.color) || '');
          var colLc = String(col).toLowerCase();
          if (colLc.indexOf('c8b400') >= 0) cat = 'var';
          else if (netEdge.dashes || colLc.indexOf('e23b3b') >= 0 || colLc.indexOf('888') >= 0) cat = 'heuristic';
          else cat = 'exact';
        }
        var legend = cgEdgeCategoryToLegend(cat);
        var hide = !window.cgEdgeFilter[legend];
        updates.push({id: id, hidden: hide});
      });
      edges.update(updates);
    } catch(e) { console.warn('edge filter (net) failed', e); }
  }
  function _filterScriptEdges(state) {
    /* Script view arrows are <path class="cg-sv-edge" data-eid=... data-cat=.../>.
     * We read each path's data-cat, map it through the shared legend bucket,
     * and toggle display based on window.cgEdgeFilter directly. This runs
     * synchronously so the user never needs to drag to refresh. */
    try {
      var paths = document.querySelectorAll('#cg-script-view .cg-sv-edge[data-eid]');
      for (var i = 0; i < paths.length; ++i) {
        var el = paths[i];
        var cat = el.getAttribute('data-cat') || 'exact';
        var legend = cgEdgeCategoryToLegend(cat);
        el.style.display = window.cgEdgeFilter[legend] ? '' : 'none';
      }
    } catch(e) {}
  }
  function _filterModuleEdges(state) { _mvRedrawArrows(); }
  function _filterIncludeEdges(state) { /* include view: own toggles */ }

  var RENDER_MODES = {
    fn:      { view: 'mynetwork',       enter: function(){ _safeActivate('Function View',   _activateFn); },                                  edgeFilter: _filterNetEdges },
    script:  { view: 'cg-script-view',  enter: function(){ _safeActivate('Script View',     function(){ _activateScriptForSlot(_activeSlotId); }); }, edgeFilter: _filterScriptEdges },
    module:  { view: 'cg-module-view',  enter: function(){ _safeActivate('Module View',     function(){ _activateModuleForSlot(_activeSlotId); }); }, edgeFilter: _filterModuleEdges },
    varflow: { view: 'cg-varflow-view', enter: function(){ _safeActivate('Variable Flow',   _activateVarFlow); },                              edgeFilter: null },
    inc:     { view: 'cgx-inc-view',    enter: function(){ _safeActivate('Include Graph',   _activateInclude); },                              edgeFilter: _filterIncludeEdges }
  };
  window.RENDER_MODES = RENDER_MODES;

  window.setViewMode = function(mode) {
    _cgxCurrentMode = mode || 'fn';
    if (typeof window._cgSetCurrentMode === 'function') window._cgSetCurrentMode(mode);
    var entry = RENDER_MODES[mode];
    if (entry && entry.enter) {
      entry.enter();
      return;
    }
    // Unknown mode -> fall back to original
    if (typeof _origSetViewMode === 'function') return _origSetViewMode(mode);
  };

  function activateSlot(slotId) {
    _activeSlotId = slotId;
    var slot = SLOTS[slotId];
    if (!slot) return;
    _activeLevel = slot.level;
    var mode = LEVEL_TO_MODE[slot.level] || 'fn';
    window.setViewMode(mode);
  }
  window.activateSlot = activateSlot;

  /* Wire slot buttons */
  if (btnFn) {
    btnFn.replaceWith(btnFn.cloneNode(true));   // strip any pre-existing listeners
    btnFn = document.getElementById('cg-btn-mode-fn');
    btnFn.addEventListener('click', function(){ activateSlot('slot1'); });
  }
  if (btnSv) {
    btnSv.replaceWith(btnSv.cloneNode(true));
    btnSv = document.getElementById('cg-btn-mode-sv');
    btnSv.addEventListener('click', function(){ activateSlot('slot2'); });
  }
  if (btnVf) {
    btnVf.replaceWith(btnVf.cloneNode(true));
    btnVf = document.getElementById('cg-btn-mode-vf');
    btnVf.addEventListener('click', function(){ window.setViewMode('varflow'); });
  }
  if (btnInc) btnInc.addEventListener('click', function(){ window.setViewMode('inc'); });

  // Re-resolve SLOT button references and re-attach (some browsers cache).
  SLOTS.slot1.btn = btnFn;
  SLOTS.slot2.btn = btnSv;

  /* ---------- Keyboard shortcuts ---------- */
  document.addEventListener('keydown', function(ev){
    var target = ev.target;
    var tag = (target && target.tagName) ? target.tagName.toLowerCase() : '';
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || (target && target.isContentEditable)) {
      if (ev.key === 'Escape' && tag === 'input') { target.blur(); }
      return;
    }
    if (ev.key === '1')        { ev.preventDefault(); activateSlot('slot1'); }
    else if (ev.key === '2')   { ev.preventDefault(); activateSlot('slot2'); }
    else if (ev.key === 'v' || ev.key === 'V') { ev.preventDefault(); window.setViewMode('varflow'); }
    else if ((ev.key === 'i' || ev.key === 'I') && btnInc) { ev.preventDefault(); window.setViewMode('inc'); }
    else if (ev.key === '/') {
      var s = document.getElementById('cg-search') || document.getElementById('cgx-mv-search');
      if (s) { ev.preventDefault(); s.focus(); s.select(); }
    }
    else if (ev.key === 'Escape') {
      ['cgx-arch-modal','cgx-mod-modal'].forEach(function(id){
        var m = document.getElementById(id);
        if (m && m.classList.contains('open')) m.classList.remove('open');
      });
    }
  });

  /* ---------- Apply persisted filter on first load ---------- */
  setTimeout(function(){ applyEdgeFilter(); }, 300);

  /* ---------- Activate the slot that matches the existing default ---------- */
  // The renderer's initial state is the original 'fn' view (vis.js network).
  // We don't change that — just make sure SLOTS.slot1's level is consistent.
  _activeSlotId = 'slot1';
  _activeLevel = SLOTS.slot1.level;
  // If slot 1's default level isn't function, switch immediately on load so the
  // user sees the correct visual for their selected level.
  if (SLOTS.slot1.level !== 'function') {
    setTimeout(function(){ activateSlot('slot1'); }, 50);
  }
})();
</script>
"""
