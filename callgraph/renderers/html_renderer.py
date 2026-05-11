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

        level: dict[str, int] = {}
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
            if t and t != "unknown":
                type_votes[t] = type_votes.get(t, 0) + 1

        if not type_votes:
            continue

        best_type = max(type_votes, key=lambda t: type_votes[t])

        # Pass 3: propagate best known type to remaining unknowns
        for occ in occs:
            if not occ["data_type"] or occ["data_type"] == "unknown":
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
            })

        for var in fn.variables:
            name = var.name or ""
            if not name or name in ("self", "cls"):
                continue
            norm = name.lower()
            sk = (var.source_kind or "").lower()
            sc = (var.scope or "").lower()
            kind = sk if sk in _SCOPE_KINDS else sc

            if kind == "constant":
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
            result[norm].append({
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
                "snippet": (var.source_detail or var.context or var.value or "")[:200],
                "type_hint": var.type_hint or "",
                "source_kind": sk,
                "value": (var.value or "")[:120],
            })

    for key in result:
        result[key].sort(key=lambda x: (x["file_path"], x["line"]))
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
  background: #1a1d23 !important; color: #e0e0e0;
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
  background: #1a1d23 !important;
  border: none !important; position: relative !important;
  flex: 1 !important; padding: 0 !important;
}

/* ── Script view graph canvas ── */
#cg-script-view {
  display: none; flex: 1; height: 100vh;
  overflow: hidden; background: #1a1d23; position: relative;
}
#cg-sv-viewport {
  width: 100%; height: 100%; position: relative; overflow: hidden;
  cursor: grab; -webkit-user-select: none; user-select: none;
}
#cg-sv-viewport.sv-panning { cursor: grabbing; }
#cg-sv-canvas { position: absolute; top: 0; left: 0; transform-origin: 0 0; }
#cg-sv-edges  { position: absolute; top: 0; left: 0; overflow: visible; pointer-events: auto; }
.cg-sv-edge {
  pointer-events: stroke; cursor: pointer;
  transition: stroke-width 0.12s, opacity 0.12s, stroke 0.12s;
}
.cg-sv-edge:hover { stroke: #F7D774; opacity: 0.95; stroke-width: 3; }
.cg-sv-edge.sv-edge-active { stroke: #F7D774 !important; opacity: 1 !important; stroke-width: 4 !important; }
.cg-file-card {
  position: absolute;
  background: #23272e; border: 1px solid #2d3139; border-radius: 8px;
  overflow: hidden; box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  transition: box-shadow 0.15s, opacity 0.15s;
}
.cg-file-card:hover { box-shadow: 0 6px 22px rgba(0,0,0,0.7); }
.cg-file-card.sv-dim { opacity: 0.18; }
.cg-file-card.sv-selected { border-color: #4A90D9; }
.cg-fc-header {
  display: flex; align-items: center; gap: 8px; padding: 8px 12px;
  background: #1e2530; border-bottom: 1px solid #2d3139; cursor: move;
}
.cg-fc-header:hover { background: #252c3a; }
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
  background: #1a1d23; position: relative;
}
#cg-vf-topbar {
  padding: 12px 16px 11px; background: #23272e;
  border-bottom: 1px solid #2d3139; flex-shrink: 0;
}
#cg-vf-topbar h2 {
  font-size: 10px; font-weight: 600; color: #8090a0;
  text-transform: uppercase; letter-spacing: 0.7px; margin: 0 0 9px;
}
#cg-vf-search-wrap { position: relative; display: flex; gap: 6px; }
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
  -webkit-user-select: none; user-select: none;
}
.cg-vf-node:hover { border-color: #4A90D9; box-shadow: 0 0 12px rgba(74,144,217,0.28); }
.cg-vf-node.vf-selected { border-color: #F7D774; box-shadow: 0 0 14px rgba(247,215,116,0.36); }
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
.cg-vfc-heap     { background: #1a0a1a; color: #d7ba7d; border: 1px solid #3a1a2a; }
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
.cg-vfa-heap     { background: #1a0a1a; color: #d7ba7d; border: 1px solid #3a1a2a; }
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
</style>
"""

_SIDEBAR_HTML = """\
<div id="cg-sidebar">
  <div id="cg-header">
    <h1>CallGraph Analyzer</h1>
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
      <button class="cg-btn" id="cg-btn-resetlayout" title="Restore original computed layout">Reset layout</button>
      <button class="cg-btn" id="cg-btn-clearsaved"  title="Remove saved positions from browser storage">Clear saved</button>
    </div>
    <div class="cg-hint" style="margin-top:4px">Double-click any node for full details</div>
  </div>

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

  <!-- Legend: edges -->
  <div class="cg-section">
    <label>Edge types</label>
    <div class="cg-legend-item">
      {arrow_solid}
      <span><b>Confirmed call</b>&nbsp;&mdash; function found exactly in this project</span>
    </div>
    <div class="cg-legend-item">
      {arrow_dashed}
      <span><b>Probable call</b>&nbsp;&mdash; name matched, exact location uncertain</span>
    </div>
    <div class="cg-legend-item">
      {arrow_var}
      <span><b>Variable annotation</b>&nbsp;&mdash; tracked variable value</span>
    </div>
    <div class="cg-legend-note">Drag any node to reposition. Positions auto-saved.</div>
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
  var _edgeHighlightedEdgeId = null;
  var _edgeHighlightedNodes = [];
  var _vfNodeDrag = null;
  var _vfNodeOverrides = {};
  var _vfCurrentNodes = [];
  var _vfCurrentEdges = [];
  var _vfCurrentChainEdges = [];
  var _vfSelectedEdgeIdx = null;  /* index into _vfEdgeMeta — which edge is highlighted */
  var _vfEdgeMeta = [];           /* [{fromId, toId, type}] — indexed by hit-path data-eidx */

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
  var btnResetLayout = document.getElementById('cg-btn-resetlayout');
  var btnClearSaved  = document.getElementById('cg-btn-clearsaved');
  var btnModeFn      = document.getElementById('cg-btn-mode-fn');
  var btnModeSv      = document.getElementById('cg-btn-mode-sv');
  var btnModeVf      = document.getElementById('cg-btn-mode-vf');
  var edgePopup      = document.getElementById('cg-edge-popup');

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

  function _nodeMeta(nid) {
    var nd = NODE_DATA.find(function(n){ return n.id === nid; });
    return nd && nd.meta ? nd.meta : null;
  }

  function _nodeDisplayName(nid) {
    var m = _nodeMeta(nid);
    return m ? (m.qualified_name || m.name || nid) : nid;
  }

  function _nodeShortName(nid) {
    var m = _nodeMeta(nid);
    return m ? (m.name || m.qualified_name || nid) : nid;
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
    }

    var h = '<button class="cg-edge-close" onclick="cgCloseEdgePopup()" title="Close">&#x2715;</button>';
    h += '<div class="cg-edge-title">' + esc(_nodeShortName(edge.from)) + ' calls ' + esc(_nodeShortName(edge.to)) + '</div>';
    h += '<div class="cg-edge-row"><span class="cg-edge-label">Caller</span><span class="cg-edge-value">' + esc(_nodeDisplayName(edge.from)) + '</span></div>';
    h += '<div class="cg-edge-row"><span class="cg-edge-label">Callee</span><span class="cg-edge-value">' + esc(_nodeDisplayName(edge.to)) + '</span></div>';
    h += '<div class="cg-edge-row"><span class="cg-edge-label">Line</span><span class="cg-edge-value">' + esc(lineText) + '</span></div>';
    h += '<div class="cg-edge-row"><span class="cg-edge-label">Call</span><span class="cg-edge-value">' + esc(_edgeCallText(edge)) + '</span></div>';
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

  /* ── Main search dropdown (function / script / varflow modes) ── */
  function _showSearchDropdown(q) {
    if (currentMode === 'varflow') { _vfSidebarSearch(q); return; }
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
          EDGE_DATA.filter(function(e){return e.to===nid;}).forEach(function(e){
            if (!visited[e.from]) q.push([e.from, d+1]);
          });
        if (direction !== 'callers')
          EDGE_DATA.filter(function(e){return e.from===nid;}).forEach(function(e){
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
    _showAllNodes();
    updateHint([]);
  });

  if (btnFit) btnFit.addEventListener('click', function() {
    if (currentMode === 'varflow') { _vfFitAll(); return; }
    if (currentMode === 'script') { _svFitView(); return; }
    var net = getNet(); if (!net) return;
    net.fit({ animation: { duration: 500 } });
  });

  if (btnShowAll) btnShowAll.addEventListener('click', function() {
    if (currentMode === 'varflow') { _vfClearHighlight(); return; }
    selectedNode = null;
    if (currentMode === 'script') { _svClearHighlight(); updateHint([]); return; }
    _showAllNodes();
    updateHint([]);
  });

  if (btnResetLayout) btnResetLayout.addEventListener('click', function() {
    if (currentMode === 'varflow') { _vfResetLayout(); _flashBtn(btnResetLayout, 'Reset!', 1200); return; }
    if (currentMode === 'script') { _flashBtn(btnResetLayout, 'N/A here', 1600); return; }
    var net = getNet(); if (!net) return;
    Object.keys(INITIAL_POS).forEach(function(id) {
      try { net.moveNode(id, INITIAL_POS[id].x, INITIAL_POS[id].y); } catch(e) {}
    });
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(net.getPositions())); } catch(e) {}
    net.fit({ animation: { duration: 600 } });
  });

  if (btnClearSaved) btnClearSaved.addEventListener('click', function() {
    if (currentMode === 'varflow') {
      _vfNodeOverrides = {};
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

    var callers = EDGE_DATA.filter(function(e){return e.to   === nodeId;});
    var callees = EDGE_DATA.filter(function(e){return e.from === nodeId;});
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
    var callerEdges = EDGE_DATA.filter(function(e){return e.to   === nodeId;});
    var calleeEdges = EDGE_DATA.filter(function(e){return e.from === nodeId;});
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
    var callerEdges = EDGE_DATA.filter(function(e){return e.to   === nodeId;});
    var calleeEdges = EDGE_DATA.filter(function(e){return e.from === nodeId;});

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

  /* ── Wire vis.js events (poll until network is ready) ──────── */
  function wire() {
    var net = getNet();
    if (!net) {
      wireAttempts++;
      if (wireAttempts < 60) setTimeout(wire, 200);
      return;
    }

    net.setOptions({
      interaction: { dragNodes:true, dragView:true, zoomView:true, hover:true, tooltipDelay:9999 }
    });

    /* Restore saved positions from localStorage */
    var savedPos = null;
    try { var raw = localStorage.getItem(LAYOUT_KEY); if (raw) savedPos = JSON.parse(raw); } catch(e) {}
    if (savedPos) {
      Object.keys(savedPos).forEach(function(id) {
        try { net.moveNode(id, savedPos[id].x, savedPos[id].y); } catch(e) {}
      });
    }

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

    /* Auto-save positions on drag */
    net.on('dragEnd', function(params) {
      if (params.nodes && params.nodes.length > 0) {
        try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(net.getPositions())); } catch(e) {}
      }
    });

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
  /* ── CSS escape polyfill ───────────────────────────────────── */
  function _cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/([^\\w\\-])/g, '\\\\$1');
  }

  /* ── Script view mode switching ───────────────────────────── */
  function setViewMode(mode) {
    currentMode = mode;
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
    }
  }

  if (btnModeFn) btnModeFn.addEventListener('click', function() { setViewMode('fn'); });
  if (btnModeSv) btnModeSv.addEventListener('click', function() { setViewMode('script'); });
  if (btnModeVf) btnModeVf.addEventListener('click', function() { setViewMode('varflow'); });

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
      html += '<path class="cg-sv-edge' + (active ? ' sv-edge-active' : '') + '" data-eid="' + esc(e.id) +
        '" d="M'+sx+','+sy+' C'+c1x+','+sy+' '+c2x+','+ty+' '+tx+','+ty+'" stroke="' + color +
        '" stroke-width="1.5" fill="none" opacity="0.55" marker-end="url(' + (active ? '#sv-arr-active' : '#sv-arr') + ')"' + dash + '/>';
      count++;
    });
    svgEl.innerHTML = html;
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
        var outgoing = EDGE_DATA.filter(function(e){ return e.from === n.id; }).length;
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
    var CARD_GAP_X = 520, CARD_GAP_Y = 240, COMP_GAP_X = 680, COMP_GAP_Y = 420;
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
      var cx = Math.round(pos.x - CARD_W / 2);
      var cy = Math.round(pos.y - 60);

      cardsHtml += '<div class="cg-file-card" data-fp="' + esc(fp) +
        '" style="left:' + cx + 'px;top:' + cy + 'px;width:' + CARD_W + 'px">';
      cardsHtml += '<div class="cg-fc-header">';
      cardsHtml += '<div class="cg-dot" style="background:' + dotColor + ';flex-shrink:0;margin-top:0"></div>';
      cardsHtml += '<div class="cg-fc-fname">' + esc(fname) + '</div>';
      if (dirPart) cardsHtml += '<div class="cg-fc-dir" title="' + esc(fp) + '">' + esc(dirPart) + '</div>';
      cardsHtml += '<div class="cg-fc-count">' + info.fns.length + ' fn' + (info.fns.length !== 1 ? 's' : '') + '</div>';
      cardsHtml += '</div><div class="cg-fn-list">';

      info.fns.forEach(function(n) {
        var m = n.meta;
        var sameFile = [], crossFile = [], extCalls = [];
        EDGE_DATA.filter(function(e){ return e.from === n.id; }).forEach(function(e) {
          var cn = NODE_DATA.find(function(x){ return x.id === e.to; });
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
      cardsHtml += '</div></div>';
    });

    /* Assemble viewport → canvas → (SVG edges + cards) */
    svEl.innerHTML =
      '<div id="cg-sv-viewport">' +
        '<div id="cg-sv-canvas">' +
          '<svg id="cg-sv-edges" width="1" height="1" ' +
               'style="overflow:visible;position:absolute;top:0;left:0;pointer-events:auto"></svg>' +
          cardsHtml +
        '</div>' +
      '</div>';

    /* Click / dblclick delegation on canvas */
    var canvas = document.getElementById('cg-sv-canvas');
    if (canvas) {
      canvas.addEventListener('click', function(e) {
        var edgePath = e.target.closest && e.target.closest('.cg-sv-edge[data-eid]');
        if (edgePath) { e.stopPropagation(); showEdgeDetails(edgePath.dataset.eid, e); return; }
        var cb = e.target.closest && e.target.closest('.cg-cb[data-nid]');
        if (cb) { e.stopPropagation(); _svSelectFn(cb.dataset.nid, true); return; }
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
        if (e.button !== 0) return;
        if (e.target.closest && e.target.closest('.cg-sv-edge')) return;
        var hdr = e.target.closest && e.target.closest('.cg-fc-header');
        if (hdr) {
          var dCard = hdr.closest('.cg-file-card');
          if (!dCard) return;
          e.preventDefault();
          _svCardDrag = {
            card: dCard,
            startX: e.clientX, startY: e.clientY,
            origL: parseFloat(dCard.style.left) || 0,
            origT: parseFloat(dCard.style.top)  || 0
          };
        } else if (!e.target.closest('.cg-fn-row') && !e.target.closest('.cg-cb')) {
          e.preventDefault();
          vp.classList.add('sv-panning');
          _svViewDrag = { startX: e.clientX, startY: e.clientY, origPanX: _svPanX, origPanY: _svPanY };
        }
      });
    }

    /* Global mousemove / mouseup for drag (added once, gated by drag state) */
    document.addEventListener('mousemove', function(e) {
      if (_svCardDrag) {
        var dx = (e.clientX - _svCardDrag.startX) / _svZoom;
        var dy = (e.clientY - _svCardDrag.startY) / _svZoom;
        _svCardDrag.card.style.left = (_svCardDrag.origL + dx) + 'px';
        _svCardDrag.card.style.top  = (_svCardDrag.origT + dy) + 'px';
        _svDrawEdges();
      } else if (_svViewDrag) {
        _svPanX = _svViewDrag.origPanX + (e.clientX - _svViewDrag.startX);
        _svPanY = _svViewDrag.origPanY + (e.clientY - _svViewDrag.startY);
        _svApplyTransform();
      }
    });
    document.addEventListener('mouseup', function() {
      _svCardDrag = null;
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

  function _vfApplyTransform() {
    var c = document.getElementById('cg-vf-canvas');
    if (c) c.style.transform = 'translate('+_vfPanX+'px,'+_vfPanY+'px) scale('+_vfZoom+')';
  }

  function _vfCategoryLabel(cat) {
    var m = {local:'Local',global:'Global',static:'Static',argument:'Argument',
             return:'Return',member:'Member',const:'Const',env:'Env',heap:'Heap'};
    return m[cat] || cat;
  }

  function _vfActionLabel(action) {
    var m = {declare:'Declared',assign:'Assigned',argument:'Param',field:'Field',
             constant:'Const',global:'Global',static:'Static',env:'Env Read',heap:'Heap Alloc'};
    return m[action] || action;
  }

  function _vfActionDescription(occ) {
    var name = occ.name, fn = occ.function_name;
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
    var keys = Object.keys(VAR_FLOW_DATA);
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

    /* Seed from all functions where normKey already appears */
    var rootOccs = VAR_FLOW_DATA[normKey] || [];
    var rootFns = {};
    rootOccs.forEach(function(occ) {
      if (!rootFns[occ.function_id]) {
        rootFns[occ.function_id] = true;
        addOccs(occ.function_id, normKey, normKey);
      }
    });

    /* BFS queue */
    var queue = Object.keys(rootFns).map(function(fnId) {
      return { fnId: fnId, varName: normKey, origName: normKey };
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
    _vfNodeOverrides = {};
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

    /* 4. Assign rows — order by mean-caller-row to reduce crossings */
    var fnRow = {};
    for (var col = 0; col <= maxCol; col++) {
      var fns = colFns[col] || [];
      fns.sort(function(a, b) {
        var aC = fnEdgesIn[a], bC = fnEdgesIn[b];
        var aR = aC.length ? aC.reduce(function(s,c){ return s+(fnRow[c]||0); },0)/aC.length : 0;
        var bR = bC.length ? bC.reduce(function(s,c){ return s+(fnRow[c]||0); },0)/bC.length : 0;
        return aR - bR || a.localeCompare(b);
      });
      fns.forEach(function(fn, i) { fnRow[fn] = i; });
    }

    /* 5. Group entries per function, sorted by line */
    var fnEntries = {};
    entries.forEach(function(en, idx) {
      var fid = en.occ.function_id;
      if (!fnEntries[fid]) fnEntries[fid] = [];
      fnEntries[fid].push({en: en, idx: idx});
    });
    Object.keys(fnEntries).forEach(function(fid) {
      fnEntries[fid].sort(function(a,b){ return (a.en.occ.line||0)-(b.en.occ.line||0); });
    });

    /* 6. Layout constants */
    var NODE_W=260, NODE_H_EST=156, INTRA_GAP=16, COL_GAP=160, ROW_GAP=70, PAD_X=80, PAD_Y=80;

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

      /* stamp localName/origName onto occ so modal can access them */
      occ._localName = localName;
      occ._origName  = origName;

      var el = document.createElement('div');
      el.className = 'cg-vf-node';
      el.id = nd.id;
      el.style.cssText = 'left:'+nd.x+'px;top:'+nd.y+'px;width:'+nd.w+'px';
      el.dataset.idx = nd.idx;
      el.innerHTML =
        '<div class="cg-vf-node-header">'
          +'<span class="cg-vf-cat-badge cg-vfc-'+esc(cat)+'">'+esc(catLbl)+'</span>'
          +'<span class="cg-vf-action-badge cg-vfa-'+esc(occ.action||'assign')+'">'+esc(actLbl)+'</span>'
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
        +'</div>'
        +(occ.snippet ? '<div class="cg-vf-snippet" title="Double-click for details">'+esc(occ.snippet.trim())+'</div>' : '');

      el.addEventListener('click', function(e){ e.stopPropagation(); _vfNodeClick(nd.id); });
      el.addEventListener('dblclick', function(e){ e.stopPropagation(); _vfOpenModal(nd.occ); });
      el.addEventListener('mousedown', function(e){
        if (e.button !== 0) return;
        e.stopPropagation();
        _vfNodeDrag = {id:nd.id, startMX:e.clientX, startMY:e.clientY, startNX:nd.x, startNY:nd.y};
        el.classList.add('vf-dragging');
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

    /* Chain edges: last block in fromFn → first block in toFn */
    chainEdges.forEach(function(ce) {
      var fg = fnEntries[ce.fromFnId], tg = fnEntries[ce.toFnId];
      if (!fg || !tg) return;
      pushEdge('vfn_'+fg[fg.length-1].idx, 'vfn_'+tg[0].idx, 'chain');
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
        if (fi !== ti) pushEdge('vfn_'+fi, 'vfn_'+ti, 'call');
      }); });
    });

    /* Intra-function sequential edges */
    Object.keys(fnEntries).forEach(function(fid) {
      var g = fnEntries[fid];
      for (var i = 0; i < g.length-1; i++)
        pushEdge('vfn_'+g[i].idx, 'vfn_'+g[i+1].idx, 'same');
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
      var eidx = eidxCounter++;
      _vfEdgeMeta.push({fromId: e.from, toId: e.to, type: e.type});

      visPaths += '<path class="'+css+'" d="'+d+'" marker-end="'+mid+'" pointer-events="none"/>';
      hitPaths += '<path class="cg-vf-hit" data-eidx="'+eidx
               +'" d="'+d+'" stroke="transparent" stroke-width="14"'
               +' fill="none" pointer-events="visibleStroke" style="cursor:pointer"/>';
    });

    svgEl.innerHTML =
      '<defs>'
      +'<marker id="vf-arr-call" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">'
      +'<path d="M0,1 L7,4 L0,7 Z" fill="#6c8ebf"/></marker>'
      +'<marker id="vf-arr-same" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">'
      +'<path d="M0,1 L7,4 L0,7 Z" fill="#6c8ebf"/></marker>'
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
    /* Do NOT redraw edges — node clicks should not change edge appearance */
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
    var canvas=document.getElementById('cg-vf-canvas');
    if (!canvas) return;
    canvas.querySelectorAll('.cg-vf-node').forEach(function(el){
      el.classList.add('vf-selected'); el.classList.remove('vf-dim');
    });
  }

  function _vfClearHighlight() {
    var canvas=document.getElementById('cg-vf-canvas');
    if (!canvas) return;
    canvas.querySelectorAll('.cg-vf-node').forEach(function(el){
      el.classList.remove('vf-selected','vf-dim');
    });
  }

  function _vfResetLayout() {
    _vfNodeOverrides   = {};
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
      var keys=Object.keys(VAR_FLOW_DATA);
      for (var i=0;i<keys.length;i++) { if (keys[i].indexOf(q)===0) return keys[i]; }
    }
    return _vfCurrentVar||null;
  }

  function _vfSidebarSearch(q) {
    var inp=document.getElementById('cg-search');
    var dd=document.getElementById('cg-search-dropdown');
    if (!inp||!dd) return;
    var norm=(q||'').toLowerCase().trim();
    var keys=Object.keys(VAR_FLOW_DATA);
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
        if(e.button!==0||e.target.closest('.cg-vf-node')) return;
        _vfViewDrag={x:e.clientX,y:e.clientY,px:_vfPanX,py:_vfPanY};
        vp.classList.add('vf-panning'); e.preventDefault();
      });
      document.addEventListener('mousemove',function(e){
        if (_vfNodeDrag) {
          var dx=(e.clientX-_vfNodeDrag.startMX)/_vfZoom;
          var dy=(e.clientY-_vfNodeDrag.startMY)/_vfZoom;
          var nx=_vfNodeDrag.startNX+dx, ny=_vfNodeDrag.startNY+dy;
          var el=document.getElementById(_vfNodeDrag.id);
          if(el){el.style.left=nx+'px';el.style.top=ny+'px';}
          _vfNodeOverrides[_vfNodeDrag.id]={x:nx,y:ny};
          _vfCurrentNodes.forEach(function(nd){if(nd.id===_vfNodeDrag.id){nd.x=nx;nd.y=ny;}});
          return;
        }
        if(!_vfViewDrag) return;
        _vfPanX=_vfViewDrag.px+(e.clientX-_vfViewDrag.x);
        _vfPanY=_vfViewDrag.py+(e.clientY-_vfViewDrag.y);
        _vfApplyTransform();
      });
      document.addEventListener('mouseup',function(){
        if (_vfNodeDrag) {
          var el=document.getElementById(_vfNodeDrag.id);
          if(el) el.classList.remove('vf-dragging');
          _vfNodeDrag=null;
          /* Redraw edges after block is placed */
          if(_vfCurrentNodes.length) { _vfSelectedEdgeIdx=null; _vfDrawEdges(_vfRebuildEdges(),_vfCurrentNodes); }
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

  /* Varflow details modal wiring */
  (function(){
    var closeBtn=document.getElementById('cg-vf-modal-close');
    if(closeBtn) closeBtn.addEventListener('click',_vfCloseModal);
    var modal=document.getElementById('cg-vf-modal');
    if(modal) modal.addEventListener('click',function(e){
      if(e.target===modal) _vfCloseModal();
    });
    document.addEventListener('keydown',function(e){
      if(e.key==='Escape') _vfCloseModal();
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

    def _build_network(
        self, graph: CallGraph, layout: dict[str, tuple[float, float]]
    ) -> tuple["Network", dict[str, dict]]:
        cfg = self.config
        net = Network(
            height="100%", width="100%",
            directed=True, notebook=False,
            cdn_resources="in_line",
        )

        net.set_options(json.dumps({
            "physics": {"enabled": False},
            "interaction": {
                "dragNodes": True, "dragView": True, "zoomView": True,
                "hover": True, "tooltipDelay": 9999,
            },
            "edges": {
                "smooth": {"enabled": True, "type": "cubicBezier",
                           "forceDirection": "vertical", "roundness": 0.4},
            },
        }))

        entry_ids = set(cfg.filter.entry_points)
        all_positions: dict[str, dict] = {}

        for node_id, fn in graph.functions.items():
            color = EXTERNAL_COLOR if fn.is_external else LANG_COLORS.get(fn.language, LANG_COLORS[Language.PYTHON])
            label = _build_node_label(fn, cfg)
            is_entry = fn.name in entry_ids or fn.qualified_name in entry_ids
            border_color = ENTRY_BORDER if is_entry else color["border"]

            x, y = layout.get(node_id, (0.0, 0.0))
            all_positions[node_id] = {"x": x, "y": y}

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
                        node_id, ann_id, width=1, dashes=True,
                        arrows={"to": {"enabled": False}},
                        color={"color": "#C8B400", "opacity": 0.6},
                    )

        for call_idx, call in enumerate(graph.calls):
            if not call.callee_id or call.callee_id not in graph.functions:
                continue
            is_heuristic = call.resolution_confidence == ResolutionConfidence.HEURISTIC
            net.add_edge(
                call.caller_id, call.callee_id, id=f"cg_edge_{call_idx}", title="",
                dashes=is_heuristic, width=1.5,
                arrows={"to": {"enabled": True, "scaleFactor": 0.6}},
                color={"color": "#888888" if is_heuristic else "#6c8ebf", "opacity": 0.85},
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

        sidebar_js = (
            _SIDEBAR_JS
            .replace("CG_NODE_DATA",     json.dumps(node_data))
            .replace("CG_EDGE_DATA",     json.dumps(edge_data))
            .replace("CG_INITIAL_POS",   json.dumps(all_positions))
            .replace("CG_LAYOUT_KEY",    json.dumps(layout_key))
            .replace("CG_ALL_NODE_IDS",  json.dumps(all_node_ids))
            .replace("CG_VAR_PARENT",    json.dumps(var_parent_map))
            .replace("CG_VAR_FLOW_DATA", json.dumps(var_flow_data))
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
            '<div id="cg-vf-search-wrap">'
            '<input id="cg-vf-search-input" type="text" placeholder="Search variable name…" autocomplete="off"/>'
            '<button id="cg-vf-search-clear" title="Clear">\xd7</button>'
            '<div id="cg-vf-dropdown"></div>'
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
        )

        html = raw_html.replace("</head>", _SIDEBAR_CSS + "\n</head>", 1)
        html = html.replace(
            "<body>",
            "<body>\n" + sidebar_html + "\n"
            + '<div id="cg-script-view"></div>' + "\n"
            + varflow_div + "\n"
            + detail_div + "\n" + modal_div,
            1,
        )
        html = html.replace("</body>", sidebar_js + "\n</body>", 1)
        return html
