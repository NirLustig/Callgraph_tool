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

import base64
import hashlib
import json
import re
import zlib
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
# PERF-5 — embedded payload compression                              #
# ------------------------------------------------------------------ #
# Large graphs embed multi-MB JSON literals into the self-contained HTML.
# We zlib-deflate the JSON (raw DEFLATE, no header) and base64-encode it,
# then decode + inflate synchronously in the browser via the embedded
# `__cgJ` helper below. This keeps the output 100% self-contained
# (no CDN, Rule 8) while cutting file size ~5-10x on large projects.

# Only serialized payloads at least this many bytes are compressed under the
# "auto" policy. Below this the ~6 KB inflate helper isn't worth embedding.
_COMPRESS_THRESHOLD = 32 * 1024


def _deflate_b64(s: str) -> str:
    """Raw-DEFLATE + base64 a UTF-8 string for synchronous browser inflation."""
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    raw = co.compress(s.encode("utf-8")) + co.flush()
    return base64.b64encode(raw).decode("ascii")


# Synchronous raw-DEFLATE (RFC 1951) inflate + base64->UTF-8->JSON helper.
# Standard tinf-style algorithm; no third-party code. Exposes window.__cgJ
# so embedded payloads become `var X = __cgJ("<base64>");`.
_DECOMP_JS = r"""
<script id="cg-decomp-js">
(function (root) {
  "use strict";
  function Tree(){this.table=new Uint16Array(16);this.trans=new Uint16Array(288);}
  function Data(s){this.s=s;this.i=0;this.tag=0;this.bitcount=0;this.dest=[];this.ltree=new Tree();this.dtree=new Tree();}
  var LENGTH_BITS=new Uint8Array(30),LENGTH_BASE=new Uint16Array(30),DIST_BITS=new Uint8Array(30),DIST_BASE=new Uint16Array(30);
  var CLCIDX=new Uint8Array([16,17,18,0,8,7,9,6,10,5,11,4,12,3,13,2,14,1,15]);
  var sltree=new Tree(),sdtree=new Tree(),offs=new Uint16Array(16);
  function buildBitsBase(bits,base,delta,first){var i,sum;for(i=0;i<delta;++i)bits[i]=0;for(i=0;i<30-delta;++i)bits[i+delta]=(i/delta)|0;for(sum=first,i=0;i<30;++i){base[i]=sum;sum+=1<<bits[i];}}
  function buildFixedTrees(lt,dt){var i;for(i=0;i<7;++i)lt.table[i]=0;lt.table[7]=24;lt.table[8]=152;lt.table[9]=112;for(i=0;i<24;++i)lt.trans[i]=256+i;for(i=0;i<144;++i)lt.trans[24+i]=i;for(i=0;i<8;++i)lt.trans[24+144+i]=280+i;for(i=0;i<112;++i)lt.trans[24+144+8+i]=144+i;for(i=0;i<5;++i)dt.table[i]=0;dt.table[5]=32;for(i=0;i<32;++i)dt.trans[i]=i;}
  function buildTree(t,lengths,off,num){var i,sum;for(i=0;i<16;++i)t.table[i]=0;for(i=0;i<num;++i)t.table[lengths[off+i]]++;t.table[0]=0;for(sum=0,i=0;i<16;++i){offs[i]=sum;sum+=t.table[i];}for(i=0;i<num;++i){if(lengths[off+i])t.trans[offs[lengths[off+i]]++]=i;}}
  function getBit(d){if(d.bitcount--===0){d.tag=d.s[d.i++];d.bitcount=7;}var bit=d.tag&1;d.tag>>>=1;return bit;}
  function readBits(d,num,base){if(!num)return base;while(d.bitcount<24){d.tag|=d.s[d.i++]<<d.bitcount;d.bitcount+=8;}var val=d.tag&(0xffff>>>(16-num));d.tag>>>=num;d.bitcount-=num;return val+base;}
  function decodeSymbol(d,t){while(d.bitcount<24){d.tag|=d.s[d.i++]<<d.bitcount;d.bitcount+=8;}var sum=0,cur=0,len=0,tag=d.tag;do{cur=2*cur+(tag&1);tag>>>=1;++len;sum+=t.table[len];cur-=t.table[len];}while(cur>=0);d.tag=tag;d.bitcount-=len;return t.trans[sum+cur];}
  function decodeTrees(d,lt,dt){var lengths=new Uint8Array(320),hlit,hdist,hclen,i,num,length;hlit=readBits(d,5,257);hdist=readBits(d,5,1);hclen=readBits(d,4,4);for(i=0;i<19;++i)lengths[i]=0;for(i=0;i<hclen;++i){lengths[CLCIDX[i]]=readBits(d,3,0);}buildTree(d.ltree,lengths,0,19);for(num=0;num<hlit+hdist;){var sym=decodeSymbol(d,d.ltree);switch(sym){case 16:var prev=lengths[num-1];for(length=readBits(d,2,3);length;--length)lengths[num++]=prev;break;case 17:for(length=readBits(d,3,3);length;--length)lengths[num++]=0;break;case 18:for(length=readBits(d,7,11);length;--length)lengths[num++]=0;break;default:lengths[num++]=sym;break;}}buildTree(lt,lengths,0,hlit);buildTree(dt,lengths,hlit,hdist);}
  function inflateBlockData(d,lt,dt){while(true){var sym=decodeSymbol(d,lt);if(sym===256)return;if(sym<256){d.dest.push(sym);}else{var length,dist,o2,i;sym-=257;length=readBits(d,LENGTH_BITS[sym],LENGTH_BASE[sym]);dist=decodeSymbol(d,dt);o2=d.dest.length-readBits(d,DIST_BITS[dist],DIST_BASE[dist]);for(i=o2;i<o2+length;++i){d.dest.push(d.dest[i]);}}}}
  function inflateUncompressedBlock(d){var length,invlength;while(d.bitcount>8){d.i--;d.bitcount-=8;}length=d.s[d.i+1];length=256*length+d.s[d.i];invlength=d.s[d.i+3];invlength=256*invlength+d.s[d.i+2];if(length!==(~invlength&0x0000ffff))throw new Error("inflate: bad length");d.i+=4;for(var i=length;i;--i)d.dest.push(d.s[d.i++]);d.bitcount=0;}
  function inflate(source){var d=new Data(source),bfinal,btype;do{bfinal=getBit(d);btype=readBits(d,2,0);if(btype===0){inflateUncompressedBlock(d);}else if(btype===1){inflateBlockData(d,sltree,sdtree);}else if(btype===2){decodeTrees(d,d.ltree,d.dtree);inflateBlockData(d,d.ltree,d.dtree);}else{throw new Error("inflate: bad block type");}}while(!bfinal);return Uint8Array.from(d.dest);}
  buildBitsBase(LENGTH_BITS,LENGTH_BASE,4,3);buildBitsBase(DIST_BITS,DIST_BASE,2,1);LENGTH_BITS[28]=0;LENGTH_BASE[28]=258;buildFixedTrees(sltree,sdtree);
  function b64ToBytes(b64){var bin=root.atob(b64),len=bin.length,bytes=new Uint8Array(len);for(var i=0;i<len;++i)bytes[i]=bin.charCodeAt(i);return bytes;}
  function cgJ(b64){var bytes=inflate(b64ToBytes(b64));var text=new root.TextDecoder("utf-8").decode(bytes);return JSON.parse(text);}
  root.__cgInflate=inflate;root.__cgJ=cgJ;
})(window);
</script>
"""



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

# Root-rank border colors (gold / silver / bronze)
ROOT_BORDERS = {1: "#FFD700", 2: "#C0C0C0", 3: "#CD7F32"}
ROOT_LABELS  = {1: "👑 Root #1", 2: "★ Root #2", 3: "★ Root #3"}
# Crown / star label suffix added to vis.js node labels for root nodes
ROOT_SUFFIX  = {1: "\n👑 Root #1", 2: "\n★ Root #2", 3: "\n★ Root #3"}

# Name-bonus table for root scoring
_ROOT_NAME_BONUSES = {
    "main":     10_000,
    "Main":      9_000,
    "__main__":  9_500,
    "run":       2_000,
    "start":     2_000,
    "entry":     2_000,
    "launch":    2_000,
    "execute":   2_000,
    "init":      1_500,
    "app":       1_000,
}
_ROOT_FILE_PREFIX_BONUS = 5_000   # file basename starts with main/app/run/index
_ROOT_MATLAB_SCRIPT_BONUS = 8_000  # MATLAB func_type == 'script'


def _compute_root_ranks(graph: "CallGraph") -> dict[str, int]:
    """Return {node_id: rank} for the top-3 root candidates (rank 1 = best).

    Algorithm:
    1. Build in_degree from internal (non-external) edges.
    2. Zero-in-degree nodes are candidates.  Fallback: all non-external nodes
       if the graph is fully cyclic (every node has callers).
    3. BFS downstream from each candidate → subtree_size (unique reachable nodes).
    4. score = name_bonus + subtree_size.  Top 3 unique node_ids → ranks 1/2/3.
    """
    import os
    from collections import deque

    node_ids = set(graph.functions.keys())
    if not node_ids:
        return {}

    # Build adjacency and in-degree only among internal non-external nodes.
    adj: dict[str, list[str]] = {n: [] for n in node_ids}
    in_deg: dict[str, int]    = {n: 0  for n in node_ids}
    for c in graph.calls:
        if c.caller_id in node_ids and c.callee_id in node_ids and c.caller_id != c.callee_id:
            if not graph.functions[c.callee_id].is_external:
                adj[c.caller_id].append(c.callee_id)
                in_deg[c.callee_id] += 1

    # Candidate pool: zero-in-degree AND not external.
    candidates = [n for n in node_ids
                  if in_deg[n] == 0 and not graph.functions[n].is_external]
    if not candidates:
        # Fully cyclic: use lowest-in-degree non-external nodes as fallback.
        min_deg = min((in_deg[n] for n in node_ids
                       if not graph.functions[n].is_external), default=0)
        candidates = [n for n in node_ids
                      if in_deg[n] == min_deg and not graph.functions[n].is_external]

    # BFS subtree size for each candidate.
    def bfs_size(start: str) -> int:
        visited: set[str] = {start}
        q = deque([start])
        while q:
            u = q.popleft()
            for v in adj.get(u, []):
                if v not in visited:
                    visited.add(v)
                    q.append(v)
        return len(visited) - 1   # exclude origin itself

    # Score each candidate.
    scores: list[tuple[float, str]] = []
    for nid in candidates:
        fn = graph.functions[nid]
        bonus = 0
        # Name bonus
        bonus += _ROOT_NAME_BONUSES.get(fn.name, 0)
        # MATLAB script
        if fn.func_type == "script":
            bonus += _ROOT_MATLAB_SCRIPT_BONUS
        # File basename prefix bonus
        bname = os.path.basename(fn.file_path).lower()
        if any(bname.startswith(p) for p in ("main", "app", "run", "index")):
            bonus += _ROOT_FILE_PREFIX_BONUS
        score = bonus + bfs_size(nid)
        scores.append((score, nid))

    scores.sort(key=lambda t: -t[0])

    result: dict[str, int] = {}
    for rank, (_, nid) in enumerate(scores[:3], start=1):
        result[nid] = rank
    return result


def _compute_include_root_ranks(ig: "IncludeGraph") -> dict[str, int]:
    """Return {file_path: rank} top-3 root candidates for include graph.

    Root = project header that nothing else includes (zero in-degree among
    project files) AND has the largest transitive include subtree.
    """
    import os
    from collections import deque

    all_files = set(ig.files.keys())
    if not all_files:
        return {}

    # Build adjacency and in-degree (project files only, skip system includes)
    adj: dict[str, list[str]] = {f: [] for f in all_files}
    in_deg: dict[str, int] = {f: 0 for f in all_files}
    for src, edges in ig.files.items():
        for e in edges:
            if e.is_system:
                continue
            dst = e.to_file if e.resolved else None
            if dst and dst in all_files and dst != src:
                adj[src].append(dst)
                in_deg[dst] += 1

    candidates = [f for f in all_files if in_deg[f] == 0]
    if not candidates:
        min_d = min(in_deg.values(), default=0)
        candidates = [f for f in all_files if in_deg[f] == min_d]

    def bfs_size(start: str) -> int:
        visited: set[str] = {start}
        q = deque([start])
        while q:
            u = q.popleft()
            for v in adj.get(u, []):
                if v not in visited:
                    visited.add(v)
                    q.append(v)
        return len(visited) - 1

    _ROOT_INC_NAME_BONUS = {
        "main": 8_000,
        "stdafx": 6_000,
        "pch": 5_000,
        "config": 3_000,
        "precomp": 4_000,
    }
    scores: list[tuple[float, str]] = []
    for fp in candidates:
        bname = os.path.splitext(os.path.basename(fp).lower())[0]
        bonus = _ROOT_INC_NAME_BONUS.get(bname, 0)
        score = bonus + bfs_size(fp)
        scores.append((score, fp))

    scores.sort(key=lambda t: -t[0])
    result: dict[str, int] = {}
    for rank, (_, fp) in enumerate(scores[:3], start=1):
        result[fp] = rank
    return result




# ================================================================== #
# Smart Top-Down Callgraph Layout (Sugiyama-style)                    #
# ------------------------------------------------------------------ #
# Replaces the old rigid "file-lane" layout. Goals: keep callers above #
# callees (top-down), minimise edge crossings (barycenter sweeps),     #
# keep connected nodes close, treat same-file proximity as a SOFT      #
# preference, avoid a huge horizontal span and a fake-root top layer,  #
# pack disconnected components in 2D, and stay fully deterministic.    #
# Pure Python, runs at generation time. Not a force-directed layout.   #
# ================================================================== #

_SL_BARYCENTER_SWEEPS = 16
_SL_MAX_ROOTS_PER_COMPONENT = 30
_SL_COMPONENT_MARGIN_X = 800.0
_SL_COMPONENT_MARGIN_Y = 600.0
_SL_MAX_PACKING_ROW_WIDTH = 10000.0
_SL_X_COMPACT_PASSES = 4

# --- LAY-4: two-level clustered (file centroids + micro members) ------- #
_SL_L2_MIN_NODES = 220
_SL_L2_MIN_FILES = 8
_SL_L2_CLUSTER_PAD_X = 260.0
_SL_L2_CLUSTER_PAD_Y = 180.0
_SL_L2_LAYER_GAP_SCALE = 1.30
_SL_L2_NODE_GAP_SCALE = 1.15

# --- Size-aware spacing (prevents node overlap) ----------------------- #
# vis.js renders nodes as monospace 11px "box" shapes; box width follows the
# widest label line. These estimate the real footprint so layout reserves
# enough room and nothing is placed on top of another node.
_SL_CHAR_W = 7.2          # px per monospace char at the rendered font size
_SL_LABEL_PAD_X = 40.0    # box horizontal padding (both sides, incl. premium margin)
_SL_LINE_H = 16.0         # px per label line
_SL_LABEL_PAD_Y = 30.0    # box vertical padding (incl. premium margin)
_SL_MIN_NODE_W = 90.0
_SL_MIN_NODE_H = 44.0
_SL_PAD_X = 80.0          # min clear gap between two node boxes in a row
_SL_ROW_GAP_Y = 80.0      # gap between wrapped sub-rows inside one layer
_SL_SCC_PAD = 44.0        # padding between members inside an expanded SCC block
# Rectangle shaping: wide layers wrap into stacked sub-rows so a large graph
# reads as a rectangle (not a downward triangle) and the horizontal span shrinks.
_SL_TARGET_ASPECT = 1.7   # desired width : height ratio for a component
_SL_KEEP_BEST_MAX_NODES = 1500  # cap for crossing-count keep-best (perf bound)

# Architectural-root name bonuses (real entry points score high).
_SL_ROOT_NAME_BONUS = {
    "main": 1000.0, "entry": 600.0, "start": 500.0, "run": 500.0,
    "init": 400.0, "setup": 400.0, "loop": 350.0, "step": 350.0,
    "process": 350.0, "execute": 350.0, "update": 300.0, "handle": 300.0,
    "callback": 300.0,
}
# Substrings that mark a node as a low-priority root candidate.
_SL_DEPRIORITIZE = ("test", "mock", "stub", "fake", "dummy", "helper",
                    "util", "getter", "setter", "scratch", "generated")


def _sl_sweeps_for(n_total: int) -> int:
    """Auto-scale barycenter sweeps down for very large graphs (deterministic)."""
    if n_total > 10000:
        return 4
    if n_total > 5000:
        return 6
    return _SL_BARYCENTER_SWEEPS


def _sl_median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    m = len(s)
    return s[m // 2] if m % 2 else (s[m // 2 - 1] + s[m // 2]) / 2.0


def _sl_estimate_size(label: str) -> tuple[float, float]:
    """Estimate a node's rendered box (w, h) in px from its label text.

    Mirrors the vis.js monospace "box" sizing: width follows the widest line,
    height follows the line count. Used so layout reserves the real footprint
    and never stacks two boxes on the same spot."""
    lines = label.split("\n") if label else [""]
    longest = max((len(ln) for ln in lines), default=0)
    w = longest * _SL_CHAR_W + _SL_LABEL_PAD_X
    h = len(lines) * _SL_LINE_H + _SL_LABEL_PAD_Y
    return (max(w, _SL_MIN_NODE_W), max(h, _SL_MIN_NODE_H))


def _sl_supernode_geometry(
    sccs: list[list[str]],
    node_sizes: dict[str, tuple[float, float]],
    name_of: dict[str, str],
    line_of: dict[str, int],
) -> tuple[list[float], list[float], list[dict[str, tuple[float, float]]]]:
    """Per super-node footprint (width, height) and member offsets.

    Single-node SCCs use the node box size. Multi-node SCCs are laid out as a
    compact square-ish grid; the block's full width/height is reserved so its
    members never spill over neighbouring nodes or into adjacent layers."""
    import math
    n = len(sccs)
    sw = [0.0] * n
    sh = [0.0] * n
    offsets: list[dict[str, tuple[float, float]]] = [dict() for _ in range(n)]
    for i, scc in enumerate(sccs):
        members = sorted(scc, key=lambda m: (line_of.get(m, 0), name_of.get(m, ""), m))
        if len(members) == 1:
            w, h = node_sizes.get(members[0], (_SL_MIN_NODE_W, _SL_MIN_NODE_H))
            sw[i], sh[i] = w, h
            offsets[i] = {members[0]: (0.0, 0.0)}
            continue
        k = len(members)
        cols = max(1, int(math.ceil(math.sqrt(k))))
        rows = int(math.ceil(k / cols))
        cellw = max(node_sizes.get(m, (_SL_MIN_NODE_W, _SL_MIN_NODE_H))[0]
                    for m in members) + _SL_SCC_PAD
        cellh = max(node_sizes.get(m, (_SL_MIN_NODE_W, _SL_MIN_NODE_H))[1]
                    for m in members) + _SL_SCC_PAD
        sw[i] = cols * cellw
        sh[i] = rows * cellh
        off: dict[str, tuple[float, float]] = {}
        for idx, m in enumerate(members):
            r, c = divmod(idx, cols)
            off[m] = ((c - (cols - 1) / 2.0) * cellw,
                      (r - (rows - 1) / 2.0) * cellh)
        offsets[i] = off
    return sw, sh, offsets


def _sl_weak_components(
    nodes: set[str],
    successors: dict[str, set[str]],
    predecessors: dict[str, set[str]],
) -> list[list[str]]:
    """Weakly-connected components, sorted by (-size, min node id)."""
    seen: set[str] = set()
    comps: list[list[str]] = []
    for s in sorted(nodes):
        if s in seen:
            continue
        comp: list[str] = []
        dq = deque([s])
        seen.add(s)
        while dq:
            u = dq.popleft()
            comp.append(u)
            for v in successors.get(u, ()):  # noqa: SIM118
                if v in nodes and v not in seen:
                    seen.add(v)
                    dq.append(v)
            for v in predecessors.get(u, ()):  # noqa: SIM118
                if v in nodes and v not in seen:
                    seen.add(v)
                    dq.append(v)
        comps.append(comp)
    comps.sort(key=lambda c: (-len(c), min(c)))
    return comps


def _sl_tarjan_scc(
    comp_nodes: list[str], successors: dict[str, set[str]]
) -> tuple[list[list[str]], dict[str, int]]:
    """Iterative Tarjan SCC. Returns (sccs, node->scc_index).
    SCCs come out in reverse-topological order (sinks first)."""
    comp_set = set(comp_nodes)
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[list[str]] = []
    node_to_scc: dict[str, int] = {}
    counter = 0
    for root in sorted(comp_nodes):
        if root in index_of:
            continue
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work: list[tuple[str, object]] = [
            (root, iter(sorted(successors.get(root, ()))))
        ]
        while work:
            node, it = work[-1]
            advanced = False
            for w in it:  # type: ignore[assignment]
                if w not in comp_set:
                    continue
                if w not in index_of:
                    index_of[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(successors.get(w, ())))))
                    advanced = True
                    break
                if w in on_stack and index_of[w] < low[node]:
                    low[node] = index_of[w]
            if advanced:
                continue
            if low[node] == index_of[node]:
                scc: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    node_to_scc[w] = len(sccs)
                    if w == node:
                        break
                sccs.append(scc)
            work.pop()
            if work:
                parent = work[-1][0]
                if low[node] < low[parent]:
                    low[parent] = low[node]
    return sccs, node_to_scc


def _sl_condense(
    sccs: list[list[str]],
    node_to_scc: dict[str, int],
    comp_nodes: list[str],
    successors: dict[str, set[str]],
) -> tuple[list[set[int]], list[set[int]]]:
    """Build the SCC-condensed DAG (super-node adjacency)."""
    n = len(sccs)
    dsucc: list[set[int]] = [set() for _ in range(n)]
    dpred: list[set[int]] = [set() for _ in range(n)]
    for u in comp_nodes:
        su = node_to_scc[u]
        for v in successors.get(u, ()):
            sv = node_to_scc.get(v)
            if sv is not None and sv != su:
                dsucc[su].add(sv)
                dpred[sv].add(su)
    return dsucc, dpred


def _sl_scc_file(sccs: list[list[str]], file_of: dict[str, str]) -> list[str]:
    """Dominant file per SCC (most common member file; min on tie)."""
    out: list[str] = []
    for scc in sccs:
        counts: dict[str, int] = defaultdict(int)
        for m in scc:
            counts[file_of.get(m, "")] += 1
        best = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        out.append(best[0][0] if best else "")
    return out


def _sl_select_roots(
    sccs: list[list[str]],
    dsucc: list[set[int]],
    dpred: list[set[int]],
    scc_file: list[str],
    name_of: dict[str, str],
    file_of: dict[str, str],
) -> set[int]:
    """Pick real architectural roots among zero-in-degree SCCs (cap MAX_ROOTS)."""
    cand = [i for i in range(len(sccs)) if not dpred[i]]
    if not cand:
        return set()

    def score(i: int) -> float:
        members = sccs[i]
        name_bonus = 0.0
        penalty = 0.0
        for m in members:
            nm = name_of.get(m, "").lower()
            fp = file_of.get(m, "").lower()
            if nm in _SL_ROOT_NAME_BONUS:
                name_bonus = max(name_bonus, _SL_ROOT_NAME_BONUS[nm])
            else:
                for key, val in _SL_ROOT_NAME_BONUS.items():
                    if nm.startswith(key):
                        name_bonus = max(name_bonus, val * 0.6)
            if any(k in nm or k in fp for k in _SL_DEPRIORITIZE):
                penalty += 50.0
        fanout = len(dsucc[i])
        cross_file = sum(1 for sv in dsucc[i] if scc_file[sv] != scc_file[i])
        return name_bonus + fanout * 5.0 + cross_file * 3.0 - penalty

    def name_bonus_of(i: int) -> float:
        b = 0.0
        for m in sccs[i]:
            nm = name_of.get(m, "").lower()
            if nm in _SL_ROOT_NAME_BONUS:
                b = max(b, _SL_ROOT_NAME_BONUS[nm])
            else:
                for key, val in _SL_ROOT_NAME_BONUS.items():
                    if nm.startswith(key):
                        b = max(b, val * 0.6)
        return b

    # A source is a REAL architectural root only with a positive signal:
    # an entry-point name, high fan-out, or cross-file reach. Trivial no-signal
    # sources are left for the pull-down so they don't clutter the top layer.
    def qualifies(i: int) -> bool:
        return (name_bonus_of(i) > 0.0
                or len(dsucc[i]) >= 3
                or sum(1 for sv in dsucc[i] if scc_file[sv] != scc_file[i]) >= 2)

    qualified = [i for i in cand if qualifies(i)]
    if not qualified:
        # Anchor: keep the single best-scoring source so the component has a top.
        best = min(cand, key=lambda i: (-score(i), min(sccs[i])))
        return {best}
    ranked = sorted(qualified, key=lambda i: (-score(i), min(sccs[i])))
    return set(ranked[:_SL_MAX_ROOTS_PER_COMPONENT])


def _sl_assign_layers(
    n: int, dsucc: list[set[int]], dpred: list[set[int]], roots: set[int]
) -> list[int]:
    """Layer assignment ranked FROM selected roots, sinking non-root sources.

    1. Selected roots sit at layer 0; longest-path forward ranks everything
       reachable from a root.
    2. Nodes NOT reachable from any root (the would-be "fake roots" and their
       exclusive subtrees) are pushed DOWN (ALAP) to just above their earliest
       ranked successor — so they plug into the tree at the right depth instead
       of piling in layer 0.
    3. A final downward-correction guarantees every caller sits above its callee.
    """
    indeg = [len(dpred[i]) for i in range(n)]
    dq = deque(sorted(i for i in range(n) if indeg[i] == 0))
    topo: list[int] = []
    rem = indeg[:]
    while dq:
        u = dq.popleft()
        topo.append(u)
        for v in sorted(dsucc[u]):
            rem[v] -= 1
            if rem[v] == 0:
                dq.append(v)

    layer: list[int | None] = [None] * n
    for r in roots:
        layer[r] = 0

    # (1) Longest path forward from roots over the topological order.
    for u in topo:
        if layer[u] is None:
            continue
        for v in dsucc[u]:
            nl = layer[u] + 1
            if layer[v] is None or nl > layer[v]:
                layer[v] = nl

    # (2) ALAP sink for unreachable nodes: reverse topo so successors are known.
    for u in reversed(topo):
        if layer[u] is not None:
            continue
        succ_layers = [layer[v] for v in dsucc[u] if layer[v] is not None]
        if succ_layers:
            layer[u] = max(0, min(succ_layers) - 1)

    # Any still-unranked nodes (isolated subgraphs with no ranked successor):
    # rank by longest path from their own sources.
    for u in topo:
        if layer[u] is not None:
            continue
        pred_layers = [layer[p] for p in dpred[u] if layer[p] is not None]
        layer[u] = (max(pred_layers) + 1) if pred_layers else 0

    lay = [int(x) for x in layer]  # type: ignore[arg-type]

    # (3) Downward-correction: enforce caller strictly above callee on every edge.
    for u in topo:
        for v in dsucc[u]:
            if lay[v] <= lay[u]:
                lay[v] = lay[u] + 1
    return lay


def _sl_count_crossings(
    order: dict[int, list[int]],
    layer_ids: list[int],
    layer: list[int],
    dsucc: list[set[int]],
) -> int:
    """Count crossings on consecutive-layer edges for the given ordering."""
    pos: dict[int, int] = {}
    for L in layer_ids:
        for idx, i in enumerate(order[L]):
            pos[i] = idx
    total = 0
    for li in range(len(layer_ids) - 1):
        upper = layer_ids[li]
        lower_layer = layer_ids[li + 1]
        pairs: list[tuple[int, int]] = []
        for u in order[upper]:
            for v in dsucc[u]:
                if layer[v] == lower_layer and v in pos:
                    pairs.append((pos[u], pos[v]))
        pairs.sort()
        # count inversions in the second coordinate
        seconds = [p[1] for p in pairs]
        for a in range(len(seconds)):
            for b in range(a + 1, len(seconds)):
                if seconds[b] < seconds[a]:
                    total += 1
    return total


def _sl_order_layers(
    layer: list[int],
    n: int,
    dsucc: list[set[int]],
    dpred: list[set[int]],
    tie_key,
    sweeps: int,
) -> tuple[dict[int, list[int]], list[int]]:
    """Barycenter/median sweeps to minimise crossings. Returns (order, layer_ids).

    Tracks the lowest-crossing ordering seen across all sweeps and returns that
    (keep-best), so extra sweeps can only help — never regress."""
    layers: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        layers[layer[i]].append(i)
    layer_ids = sorted(layers)
    order = {L: sorted(layers[L], key=tie_key) for L in layer_ids}
    pos: dict[int, int] = {}
    for L in layer_ids:
        for idx, i in enumerate(order[L]):
            pos[i] = idx

    keep_best = n <= _SL_KEEP_BEST_MAX_NODES and len(layer_ids) > 1
    best_order = {L: list(order[L]) for L in layer_ids} if keep_best else order
    best_cross = (_sl_count_crossings(order, layer_ids, layer, dsucc)
                  if keep_best else 0)

    for s in range(sweeps):
        down = (s % 2 == 0)
        seq = layer_ids[1:] if down else layer_ids[-2::-1]
        adj = dpred if down else dsucc
        for L in seq:
            def bary(i: int) -> float:
                nb = adj[i]
                ps = [pos[x] for x in nb if x in pos]
                return _sl_median(ps) if ps else float(pos[i])
            order[L] = sorted(order[L], key=lambda i: (bary(i), tie_key(i)))
            for idx, i in enumerate(order[L]):
                pos[i] = idx
        if keep_best:
            c = _sl_count_crossings(order, layer_ids, layer, dsucc)
            if c < best_cross:
                best_cross = c
                best_order = {L: list(order[L]) for L in layer_ids}
    return best_order, layer_ids


def _sl_assign_x(
    order: dict[int, list[int]],
    layer_ids: list[int],
    dsucc: list[set[int]],
    dpred: list[set[int]],
    scc_file: list[str],
    sw: list[float],
    node_gap: float,
) -> dict[int, float]:
    """X with soft file-affinity. Order is FIXED (preserves crossing reduction);
    only x positions move, with a per-pair width-aware min-gap so boxes never
    overlap (gap = half left width + half right width + clear padding)."""
    def min_gap(a: int, b: int) -> float:
        return sw[a] / 2.0 + sw[b] / 2.0 + _SL_PAD_X

    x: dict[int, float] = {}
    for L in layer_ids:
        row = order[L]
        cx = 0.0
        for idx, i in enumerate(row):
            if idx > 0:
                cx += min_gap(row[idx - 1], i)
            x[i] = cx
        if row:
            mid = (x[row[0]] + x[row[-1]]) / 2.0
            for i in row:
                x[i] -= mid

    for _ in range(_SL_X_COMPACT_PASSES):
        for L in layer_ids:
            row = order[L]
            k = len(row)
            if not k:
                continue
            same_by_file: dict[str, list[float]] = defaultdict(list)
            for i in row:
                same_by_file[scc_file[i]].append(x[i])
            targets: list[float] = []
            for idx, i in enumerate(row):
                nb = list(dpred[i]) + list(dsucc[i])
                nb_x = [x[v] for v in nb if v in x]
                nmed = _sl_median(nb_x) if nb_x else x[i]
                smed = _sl_median(same_by_file[scc_file[i]])
                src_bias = (idx - (k - 1) / 2.0) * node_gap
                targets.append(0.70 * nmed + 0.25 * smed + 0.05 * src_bias)
            # Left-to-right width-aware min-gap enforcement in the fixed order.
            for j in range(1, k):
                floor = targets[j - 1] + min_gap(row[j - 1], row[j])
                if targets[j] < floor:
                    targets[j] = floor
            for j, i in enumerate(row):
                x[i] = targets[j]
            mid = (x[row[0]] + x[row[-1]]) / 2.0
            for i in row:
                x[i] -= mid
    return x


def _sl_layout_component(
    comp_nodes: list[str],
    successors: dict[str, set[str]],
    name_of: dict[str, str],
    file_of: dict[str, str],
    line_of: dict[str, int],
    node_sizes: dict[str, tuple[float, float]],
    layer_gap: float,
    node_gap: float,
    sweeps: int,
) -> tuple[dict[str, tuple[float, float]], bool, int]:
    """Lay out one weak component. Returns (local_positions, has_root, scc_count).

    Size-aware: every node reserves its true box footprint so nothing overlaps.
    Wide layers are wrapped into stacked sub-rows so large graphs read as a
    rectangle (not a downward triangle) with a much smaller horizontal span."""
    sccs, node_to_scc = _sl_tarjan_scc(comp_nodes, successors)
    dsucc, dpred = _sl_condense(sccs, node_to_scc, comp_nodes, successors)
    scc_file = _sl_scc_file(sccs, file_of)
    roots = _sl_select_roots(sccs, dsucc, dpred, scc_file, name_of, file_of)
    layer = _sl_assign_layers(len(sccs), dsucc, dpred, roots)

    def tie_key(i: int) -> tuple:
        members = sccs[i]
        m0 = min(members)
        return (scc_file[i], min(line_of.get(m, 0) for m in members),
                min(name_of.get(m, "") for m in members), m0)

    order, layer_ids = _sl_order_layers(
        layer, len(sccs), dsucc, dpred, tie_key, sweeps
    )
    sw, sh, offsets = _sl_supernode_geometry(sccs, node_sizes, name_of, line_of)
    xs = _sl_assign_x(order, layer_ids, dsucc, dpred, scc_file, sw, node_gap)

    # --- Rectangle shaping: wrap wide layers into stacked sub-rows ------- #
    widest = (max(sw) if sw else 0.0) + _SL_PAD_X

    def split_rows(row: list[int], target_w: float) -> list[list[int]]:
        subrows: list[list[int]] = []
        cur: list[int] = []
        cur_w = 0.0
        for i in row:
            add = sw[i] + (_SL_PAD_X if cur else 0.0)
            if cur and cur_w + add > target_w:
                subrows.append(cur)
                cur, cur_w, add = [], 0.0, sw[i]
            cur.append(i)
            cur_w += add
        if cur:
            subrows.append(cur)
        return subrows

    def total_height(target_w: float) -> float:
        h = 0.0
        for li, L in enumerate(layer_ids):
            rws = split_rows(order[L], target_w)
            for si, sr in enumerate(rws):
                h += max(sh[i] for i in sr)
                if si < len(rws) - 1:
                    h += _SL_ROW_GAP_Y
            if li < len(layer_ids) - 1:
                h += layer_gap
        return h

    # Two fixed-point iterations toward the target aspect ratio (deterministic).
    natural_w = 0.0
    for L in layer_ids:
        roww = sum(sw[i] for i in order[L]) + _SL_PAD_X * max(0, len(order[L]) - 1)
        natural_w = max(natural_w, roww)
    target_w = natural_w
    for _ in range(2):
        h = total_height(target_w)
        target_w = max(widest, _SL_TARGET_ASPECT * h)

    # --- Place nodes: width-aware x per sub-row, cumulative height-aware y - #
    local: dict[str, tuple[float, float]] = {}
    y_cursor = 0.0
    for li, L in enumerate(layer_ids):
        rws = split_rows(order[L], target_w)
        single = len(rws) == 1
        for si, sr in enumerate(rws):
            row_h = max(sh[i] for i in sr)
            cy = y_cursor + row_h / 2.0
            if single:
                # Keep the crossing-aligned x positions from _sl_assign_x.
                row_x = {i: xs[i] for i in sr}
            else:
                # Re-pack this sub-row left-to-right, width-aware, centred.
                row_x = {}
                cx = 0.0
                prev = None
                for i in sr:
                    if prev is not None:
                        cx += sw[prev] / 2.0 + sw[i] / 2.0 + _SL_PAD_X
                    row_x[i] = cx
                    prev = i
                mid = (row_x[sr[0]] + row_x[sr[-1]]) / 2.0
                for i in sr:
                    row_x[i] -= mid
            for i in sr:
                bx, by = row_x[i], cy
                for m, (ox, oy) in offsets[i].items():
                    local[m] = (bx + ox, by + oy)
            y_cursor += row_h
            if si < len(rws) - 1:
                y_cursor += _SL_ROW_GAP_Y
        if li < len(layer_ids) - 1:
            y_cursor += layer_gap

    return local, bool(roots), len(sccs)


def _sl_pack_components(
    comp_results: list[tuple[dict[str, tuple[float, float]], bool, list[str]]],
    successors: dict[str, set[str]],
    file_of: dict[str, str],
) -> dict[str, tuple[float, float]]:
    """Importance-sort components and shelf-pack them in 2D (no single long row)."""
    items = []
    for local, has_root, comp_nodes in comp_results:
        comp_set = set(comp_nodes)
        edge_count = 0
        cross_file = 0
        for u in comp_nodes:
            for v in successors.get(u, ()):
                if v in comp_set:
                    edge_count += 1
                    if file_of.get(u) != file_of.get(v):
                        cross_file += 1
        xs = [p[0] for p in local.values()]
        ys = [p[1] for p in local.values()]
        min_x, max_x = (min(xs), max(xs)) if xs else (0.0, 0.0)
        min_y, max_y = (min(ys), max(ys)) if ys else (0.0, 0.0)
        width = max_x - min_x
        height = max_y - min_y
        importance = (1 if has_root else 0, edge_count, len(comp_nodes), cross_file)
        items.append({
            "local": local, "min_x": min_x, "min_y": min_y,
            "width": width, "height": height, "importance": importance,
            "key": min(comp_nodes) if comp_nodes else "",
        })

    items.sort(key=lambda it: (tuple(-v for v in it["importance"]), it["key"]))

    final: dict[str, tuple[float, float]] = {}
    cursor_x = 0.0
    row_y = 0.0
    row_height = 0.0
    for it in items:
        if cursor_x > 0.0 and cursor_x + it["width"] > _SL_MAX_PACKING_ROW_WIDTH:
            row_y += row_height + _SL_COMPONENT_MARGIN_Y
            cursor_x = 0.0
            row_height = 0.0
        off_x = cursor_x - it["min_x"]
        off_y = row_y - it["min_y"]
        for nid, (px, py) in it["local"].items():
            final[nid] = (px + off_x, py + off_y)
        cursor_x += it["width"] + _SL_COMPONENT_MARGIN_X
        row_height = max(row_height, it["height"])
    return final


def _sl_diagnostics(stats: dict) -> None:
    """Optional layout diagnostics (off by default; CG_LAYOUT_DEBUG=1 to enable)."""
    import os
    import sys
    if not os.environ.get("CG_LAYOUT_DEBUG"):
        return
    sys.stderr.write(
        "[cg-layout] " + "  ".join(f"{k}={v}" for k, v in stats.items()) + "\n"
    )


def _sl_layout_l2_clustered(
    all_nodes: set[str],
    successors: dict[str, set[str]],
    predecessors: dict[str, set[str]],
    name_of: dict[str, str],
    file_of: dict[str, str],
    line_of: dict[str, int],
    node_sizes: dict[str, tuple[float, float]],
    layer_gap: float,
    node_gap: float,
    sweeps: int,
) -> dict[str, tuple[float, float]] | None:
    """LAY-4: two-level clustered layout.

    Macro level: one centroid per source file (cluster graph with aggregated
    inter-file call edges).
    Micro level: functions inside each file cluster keep a local top-down layout.
    Final coords = macro centroid + micro local coords.

    Returns None when the graph is too small to benefit; caller falls back to
    the standard smart top-down layout."""
    # Gate to avoid unnecessary work on small graphs.
    non_empty_files = {file_of.get(n, "") for n in all_nodes if file_of.get(n, "")}
    if len(all_nodes) < _SL_L2_MIN_NODES or len(non_empty_files) < _SL_L2_MIN_FILES:
        return None

    # Cluster assignment: file path; no-file nodes become singleton pseudo-clusters
    # so unrelated externals don't collapse into one giant cluster.
    cluster_of: dict[str, str] = {}
    clusters: dict[str, list[str]] = defaultdict(list)
    for n in sorted(all_nodes):
        fp = file_of.get(n, "")
        cid = fp if fp else f"__nofile__::{n}"
        cluster_of[n] = cid
        clusters[cid].append(n)

    # Micro layout per cluster.
    micro_pos: dict[str, dict[str, tuple[float, float]]] = {}
    cluster_size: dict[str, tuple[float, float]] = {}
    for cid, members in clusters.items():
        local, _has_root, _scc = _sl_layout_component(
            members,
            successors,
            name_of,
            file_of,
            line_of,
            node_sizes,
            max(180.0, layer_gap * 0.62),
            max(120.0, node_gap * 0.55),
            max(3, min(10, sweeps)),
        )
        if not local:
            local = {members[0]: (0.0, 0.0)}

        xs = [p[0] for p in local.values()]
        ys = [p[1] for p in local.values()]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        cx = (min_x + max_x) / 2.0
        cy = (min_y + max_y) / 2.0
        norm_local: dict[str, tuple[float, float]] = {}
        for nid, (x, y) in local.items():
            norm_local[nid] = (x - cx, y - cy)
        micro_pos[cid] = norm_local

        width = max_x - min_x
        height = max_y - min_y
        cluster_size[cid] = (
            max(220.0, width + _SL_L2_CLUSTER_PAD_X),
            max(160.0, height + _SL_L2_CLUSTER_PAD_Y),
        )

    # Macro cluster graph (aggregated inter-cluster edges).
    cluster_nodes: set[str] = set(clusters.keys())
    csucc: dict[str, set[str]] = defaultdict(set)
    cpred: dict[str, set[str]] = defaultdict(set)
    for u in all_nodes:
        cu = cluster_of[u]
        for v in successors.get(u, ()):  # noqa: SIM118
            if v not in all_nodes:
                continue
            cv = cluster_of[v]
            if cu == cv:
                continue
            csucc[cu].add(cv)
            cpred[cv].add(cu)

    cname_of = {cid: (cid.replace('\\', '/').split('/')[-1] if not cid.startswith('__nofile__::') else name_of.get(clusters[cid][0], cid))
                for cid in cluster_nodes}
    cfile_of = {cid: cid for cid in cluster_nodes}
    cline_of = {cid: 0 for cid in cluster_nodes}

    c_components = _sl_weak_components(cluster_nodes, csucc, cpred)
    c_results: list[tuple[dict[str, tuple[float, float]], bool, list[str]]] = []
    for comp_nodes in c_components:
        local_c, has_root_c, _ = _sl_layout_component(
            comp_nodes,
            csucc,
            cname_of,
            cfile_of,
            cline_of,
            cluster_size,
            layer_gap * _SL_L2_LAYER_GAP_SCALE,
            node_gap * _SL_L2_NODE_GAP_SCALE,
            max(3, min(12, sweeps)),
        )
        c_results.append((local_c, has_root_c, comp_nodes))

    macro_pos = _sl_pack_components(c_results, csucc, cfile_of)
    if not macro_pos:
        return None

    final: dict[str, tuple[float, float]] = {}
    for cid, members in clusters.items():
        mcx, mcy = macro_pos.get(cid, (0.0, 0.0))
        local = micro_pos.get(cid, {})
        for n in members:
            lx, ly = local.get(n, (0.0, 0.0))
            final[n] = (mcx + lx, mcy + ly)
    return final


def _compute_layout(
    graph: CallGraph,
    h_sep: int = 420,
    v_sep: int = 340,
    node_sizes: dict[str, tuple[float, float]] | None = None,
) -> dict[str, tuple[float, float]]:
    """
    Smart top-down call-graph layout.

    A deterministic Sugiyama-style layered layout for the Function-mode initial
    placement. The graph is split into weakly-connected components laid out
    independently; within each component cycles are collapsed via SCC
    condensation, real architectural roots are selected (not every no-caller
    node), vertical layers are assigned by longest path (callers above callees),
    edge crossings are reduced with barycenter sweeps, and x positions blend
    connected-neighbour position (0.70), same-file affinity (0.25) and source
    order (0.05) — so same-file functions stay *usually* near without forming
    rigid lanes. Components are importance-ranked and shelf-packed in 2D to keep
    the canvas compact. Returns {node_id: (x, y)}.
    """
    all_nodes = set(graph.functions.keys())
    if not all_nodes:
        return {}

    successors: dict[str, set[str]] = defaultdict(set)
    predecessors: dict[str, set[str]] = defaultdict(set)
    edge_total = 0
    for call in graph.calls:
        c, e = call.caller_id, call.callee_id
        if e and e in all_nodes and c in all_nodes and c != e:
            if e not in successors[c]:
                edge_total += 1
            successors[c].add(e)
            predecessors[e].add(c)

    name_of: dict[str, str] = {}
    file_of: dict[str, str] = {}
    line_of: dict[str, int] = {}
    for n in all_nodes:
        fn = graph.functions.get(n)
        name_of[n] = fn.name if fn is not None else n
        file_of[n] = (fn.file_path if (fn is not None and fn.file_path) else "")
        line_of[n] = (fn.line_start if (fn is not None and fn.line_start) else 0)

    # Real box footprints drive spacing so nothing overlaps. Callers may pass
    # the actual rendered-label sizes; otherwise estimate from the node name.
    if node_sizes is None:
        node_sizes = {n: _sl_estimate_size(name_of[n]) for n in all_nodes}
    else:
        node_sizes = {n: node_sizes.get(n) or _sl_estimate_size(name_of[n])
                      for n in all_nodes}

    layer_gap = float(v_sep)
    node_gap = float(h_sep) * 0.6
    sweeps = _sl_sweeps_for(len(all_nodes))

    final = _sl_layout_l2_clustered(
        all_nodes, successors, predecessors,
        name_of, file_of, line_of, node_sizes,
        layer_gap, node_gap, sweeps,
    )

    if final is None:
        components = _sl_weak_components(all_nodes, successors, predecessors)

        comp_results: list[tuple[dict[str, tuple[float, float]], bool, list[str]]] = []
        scc_total = 0
        for comp_nodes in components:
            local, has_root, scc_count = _sl_layout_component(
                comp_nodes, successors, name_of, file_of, line_of, node_sizes,
                layer_gap, node_gap, sweeps,
            )
            scc_total += scc_count
            comp_results.append((local, has_root, comp_nodes))

        final = _sl_pack_components(comp_results, successors, file_of)
        diag_mode = 'smart-topdown'
        diag_weak = len(components)
        diag_largest = (max(len(c) for c in components) if components else 0)
        diag_scc = scc_total
    else:
        diag_mode = 'l2-clustered'
        components = _sl_weak_components(all_nodes, successors, predecessors)
        diag_weak = len(components)
        diag_largest = (max(len(c) for c in components) if components else 0)
        diag_scc = -1

    # Center the whole canvas horizontally (matches prior behaviour).
    if final:
        xs = [p[0] for p in final.values()]
        ys = [p[1] for p in final.values()]
        mid = (min(xs) + max(xs)) / 2.0
        for n in list(final.keys()):
            px, py = final[n]
            final[n] = (px - mid, py)
        _sl_diagnostics({
            "mode": diag_mode,
            "nodes": len(all_nodes), "edges": edge_total,
            "weak_components": diag_weak,
            "largest_component": diag_largest,
            "sccs": diag_scc, "sweeps": sweeps,
            "canvas_w": round(max(xs) - min(xs), 1),
            "canvas_h": round(max(ys) - min(ys), 1),
        })

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

            # VFI-9: member-level variable identity. A plain member read
            # (`cfg.speed`) is recorded by the parser with name=leaf (`speed`)
            # and parent_name=`cfg`. Bucketing it under the bare leaf merges
            # unrelated members (`cfg.speed` + `engine.speed`) into one phantom
            # flow AND severs it from the full-path-keyed custom-input/connect
            # destinations (`lugasi(&cfg.speed, …)` is recorded under `cfg.speed`).
            # Re-key member reads by their full `parent.member` path so each
            # member is a distinct end-to-end identity that lines up with the
            # interprocedural arg/full-name matching (VFI-2) and with the
            # custom-input destinations carrying the same path.
            _mem_parent = getattr(var, "parent_name", "") or ""
            _is_member_read = (sk == "member_access" and bool(_mem_parent))
            if _is_member_read:
                name = _mem_parent + "." + name
                norm = name.lower()

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
            _cat = _category(sc, kind)
            # Build the record dict, dropping empty/default fields to shrink JSON payload
            # (significant on large .sln projects where this can be 150K+ occurrences).
            _rec = {
                "name": name,
                "category": _cat,
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
            # Full source statement line for the SOURCE row + modal (e.g. "x = fn(a);")
            _fsrc = (getattr(var, "full_source", "") or "")[:200]
            if _fsrc:                           _rec["full_source"] = _fsrc
            if getattr(var, "is_dead", False): _rec["is_dead"] = True
            _dr = getattr(var, "dead_reason", "") or ""
            if _dr:                             _rec["dead_reason"] = _dr
            _dcat = getattr(var, "dead_category", "") or ""
            if _dcat:                           _rec["dead_category"] = _dcat
            _dconf = getattr(var, "dead_confidence", "") or ""
            if _dconf:                          _rec["dead_confidence"] = _dconf
            _drl = getattr(var, "read_lines", None) or []
            _dwl = getattr(var, "write_lines", None) or []
            if _drl:                            _rec["read_lines"] = list(_drl)[:20]
            if _dwl:                            _rec["write_lines"] = list(_dwl)[:20]
            if getattr(var, "is_suppressed", False):
                _rec["is_suppressed"] = True
                _sr = getattr(var, "suppress_reason", "") or ""
                if _sr:                         _rec["suppress_reason"] = _sr
            _cp = getattr(var, "connect_path", "") or ""
            if _cp:                             _rec["connect_path"] = _cp
            _cin = getattr(var, "connect_input_name", "") or ""
            if _cin:                            _rec["connect_input_name"] = _cin
            if _custom_func:                    _rec["custom_input_func"] = _custom_func
            if _custom_classifier:              _rec["custom_input_classifier"] = _custom_classifier
            _parent = getattr(var, "parent_name", "") or ""
            if _parent:                         _rec["parent_name"] = _parent
            # VFI-3: cross-variable assignment source
            _asrc = getattr(var, "assign_src", "") or ""
            if _asrc:                           _rec["assign_src"] = _asrc.lower()
            # VF-10: adjacent intent comment (above or inline right-side)
            _dc = getattr(var, "doc_comment", "") or ""
            if _dc:                             _rec["doc_comment"] = _dc[:200]
            # VFI-1: scope identity for "split by scope" grouping.Function-scoped
            # vars (local / heap) and parameters default to function_id on the
            # consumer side, so only emit scope_id for genuinely broader scopes:
            # globals share program scope, statics/consts share file scope, struct
            # members share their type scope.
            # VFI-9: member identities (reads and full-path custom-input/connect
            # destinations) carry an "m:<full.path>" scope id so "split by scope"
            # keeps `cfg.speed` and `engine.speed` apart yet merges every read of
            # the same member across the functions that touch it.
            if _is_member_read:
                _rec["scope_id"] = "m:" + norm
            elif _mem_parent and "." in name:
                _rec["scope_id"] = "m:" + name.lower()
            elif _cat in ("global", "env"):
                _rec["scope_id"] = "global"
            elif _cat in ("static", "const"):
                _rec["scope_id"] = "f:" + fp
            elif _cat == "member":
                _rec["scope_id"] = "t:" + (var.type_hint or "")
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
.hp-virtual      { background:#3a2e1a; color:#f0c674; }
.hp-name  { font-size: 13px; font-weight: 700; color: #DCDCAA; margin-bottom: 6px; word-break: break-all; }
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
.cg-modal-title { font-size: 16px; font-weight: 700; color: #DCDCAA; margin-bottom: 3px; margin-right: 40px; word-break: break-all; }
.cg-modal-qname { font-size: 11px; color: #8090a0; margin-bottom: 12px; font-family: monospace; word-break: break-all; }
.cg-modal-section { margin-top: 14px; padding-top: 14px; border-top: 1px solid #2d3139; }
.cg-modal-section h3 { font-size: 10px; text-transform: uppercase; color: #6a7a8a; letter-spacing: 0.8px; margin-bottom: 8px; }
.cg-modal-row { display: flex; gap: 8px; margin-bottom: 5px; font-size: 12px; align-items: flex-start; }
.cg-modal-lbl { color: #6a7a8a; min-width: 130px; flex-shrink: 0; font-size: 10px; text-transform: uppercase; letter-spacing: 0.4px; padding-top: 2px; }
.cg-modal-val { color: #ddd; font-family: monospace; word-break: break-all; line-height: 1.5; }
.cg-modal-param { background: #1a1d23; border-radius: 4px; padding: 5px 10px; margin-bottom: 4px; font-family: monospace; font-size: 11px; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.cg-modal-param-num { color: #6a7a8a; min-width: 20px; }
.cg-modal-param-name { color: #4EC9B0; }
.cg-modal-param-type { color: #74B3F7; }
.cg-hl-fn { color: #DCDCAA; }
.cg-hl-var { color: #4EC9B0; }
.cg-hl-type { color: #7FB069; }
.cg-hl-kw { color: #4A90D9; font-weight: 600; }
.cg-hl-kw-union { color: #C77DD9; font-weight: 600; }
.cg-hl-kw-enum { color: #E0A458; font-weight: 600; }
.cg-hl-kw-qual { color: #B896DC; }
.cg-hl-builtin { color: #74B3F7; }
.cg-hl-punct { color: #8090a0; }
/* ── Function-mode signature cards (scale-with-zoom overlay) ── */
.cg-fnsig { position:absolute; transform:translate(-50%,-50%); pointer-events:none;
  font-family:'JetBrains Mono','Cascadia Code','SF Mono',Consolas,'Segoe UI Mono',monospace;
  background:#1b1f27; border:2px solid #4A90D9; border-left-width:5px; border-radius:7px;
  box-shadow:0 4px 14px rgba(0,0,0,0.38); padding:5px 10px; white-space:nowrap; color:#c8d0da; line-height:1.4;
  content-visibility:auto; contain-intrinsic-size:150px 42px; }
.cg-fnsig-detail { font-size:12px; }
.cg-fnsig-medium { font-size:12px; padding:4px 9px; }
.cg-fnsig-sel { box-shadow:0 0 0 3px rgba(116,179,247,0.45),0 6px 18px rgba(0,0,0,0.45); }
/* Language contour thickens as the graph zooms out so nodes never blend into
   the background (the layer scales, so a larger base width keeps it visible). */
#cg-fn-sig-layer[data-zo="1"] .cg-fnsig { border-width:3px; border-left-width:6px; }
#cg-fn-sig-layer[data-zo="2"] .cg-fnsig { border-width:5px; border-left-width:8px; }
#cg-fn-sig-layer[data-zo="3"] .cg-fnsig { border-width:9px; border-left-width:12px; }
.cg-fnsig-parent { color:#8090a0; font-size:10px; }
.cg-fnsig-foot { color:#6a7a8a; font-size:10px; margin-top:2px; }
.cg-fnsig-badge { display:inline-block; font-size:9px; font-weight:700; padding:0 5px; border-radius:8px; margin-left:7px; vertical-align:middle; }
.cg-fnsig-badge-root { background:#33280a; color:#ffd76a; }
.cg-fnsig-badge-ext { background:#242a30; color:#9aa7b3; }
.cg-fnsig-pill { position:absolute; transform:translate(-50%,-50%); pointer-events:none;
  display:flex; align-items:center; gap:6px; background:#1b1f27; border:1px solid #2d3139;
  border-radius:11px; padding:2px 9px 2px 7px; font-size:11px; color:#d2dae4;
  box-shadow:0 2px 9px rgba(0,0,0,0.32); white-space:nowrap;
  font-family:'JetBrains Mono','Cascadia Code','SF Mono',Consolas,'Segoe UI Mono',monospace; }
.cg-fnsig-dot { width:8px; height:8px; border-radius:50%; flex:0 0 auto; }
#cg-fnmap { box-shadow:0 4px 16px rgba(0,0,0,0.45); border:1px solid #2d3139; }
body[data-theme="light"] .cg-fnsig, body[data-theme="light"] .cg-fnsig-pill {
  background:#ffffff; border-color:#c8d4de; color:#33414f; box-shadow:0 3px 12px rgba(0,0,0,0.14); }
body[data-theme="light"] .cg-fnsig-sel { border-color:#1d4ed8; box-shadow:0 0 0 3px rgba(29,78,216,0.28),0 6px 18px rgba(0,0,0,0.18); }
body[data-theme="light"] .cg-fnsig-foot, body[data-theme="light"] .cg-fnsig-parent { color:#7a8898; }
body[data-theme="light"] #cg-fnmap { border-color:#c8d4de; }
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
.cg-fc-fname { font-size: 13px; font-weight: 700; color: #9CDCFE; white-space: nowrap; }
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
.cg-fn-nm-main { font-weight: 700; color: #DCDCAA; }
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

/* ── Root / Entry-Point badges ── */
.cg-root-badge {
  display: inline-flex; align-items: center; gap: 3px;
  font-size: 10px; font-weight: 700; border-radius: 4px; padding: 1px 6px;
  white-space: nowrap; flex-shrink: 0; letter-spacing: 0.2px; cursor: default;
  pointer-events: none;
}
.cg-root-badge.rank-1 { background: rgba(255,215,0,0.18); color: #FFD700; border: 1px solid rgba(255,215,0,0.45); }
.cg-root-badge.rank-2 { background: rgba(192,192,192,0.18); color: #C0C0C0; border: 1px solid rgba(192,192,192,0.45); }
.cg-root-badge.rank-3 { background: rgba(205,127,50,0.18); color: #CD7F32; border: 1px solid rgba(205,127,50,0.45); }


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
/* VF-8: hotspot fire badge in dropdown */
.cg-vf-hot-badge {
  font-size: 12px; margin-right: 5px; flex-shrink: 0;
  filter: drop-shadow(0 0 3px rgba(255,140,0,0.7));
}
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
.cg-vf-var-name { font-size: 13px; font-weight: 700; color: #4EC9B0; font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cg-vf-info-row { display: flex; align-items: baseline; gap: 5px; margin-top: 3px; }
.cg-vf-info-label { font-size: 9px; color: #4a5a6a; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; flex-shrink: 0; min-width: 30px; }
.cg-vf-info-val { font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cg-vf-type-val  { color: #74B3F7; font-family: monospace; }
.cg-vf-fn-val    { color: #DCDCAA; }
.cg-vf-file-val  { color: #5a6a7a; font-size: 10px; }
.cg-vf-snippet {
  font-size: 10px; color: #9cdcfe; background: #141720; padding: 5px 9px;
  border-top: 1px solid #2a2f38; font-family: Consolas, monospace;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
/* VF-10: inline "why" doc-comment chip */
.cg-vf-doc-chip {
  font-size: 10px; color: #b4c9a0; background: #121a0f; padding: 4px 9px 5px;
  border-top: 1px solid #1e2d1a; font-style: italic;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
/* SOURCE row — monospace code line, wraps so full line is visible */
.cg-vf-source-row { align-items: flex-start; }
.cg-vf-source-val {
  font-family: Consolas, monospace; font-size: 10px; color: #9cdcfe;
  white-space: pre-wrap; word-break: break-all; line-height: 1.4;
  border-left: 2px solid #3a4a6a; padding-left: 5px; margin-top: 1px;
}
.cg-vf-type-chip {
  font-size: 10px; line-height: 1.2; border-radius: 10px; padding: 2px 7px;
  border: 1px solid #4A90D9; background: #142233; color: #9CDCFE;
  cursor: pointer; font-family: Consolas, monospace;
}
.cg-vf-type-chip:hover { background: #1c3048; border-color: #74B3F7; }
/* edges — blue palette matching Function/Script mode */
.cg-vf-edge       { stroke: #6c8ebf; stroke-width: 2;   fill: none; opacity: 0.85; }
.cg-vf-edge-call  { /* inherits */ }
.cg-vf-edge-chain { /* inherits */ }
.cg-vf-edge-same  { stroke-dasharray: 5,4; stroke-width: 1.5; opacity: 0.65; }
/* VFI-3: cross-variable assignment edge — dashed orange */
.cg-vf-edge-assign { stroke: #e67e22; stroke-dasharray: 6,4; stroke-width: 1.8; opacity: 0.85; }
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
/* category-specific dead badges (Variable Flow) */
.cg-vf-dead-badge.dv-unused      { background:#4a1010; color:#e74c3c; border-color:#7a1a1a; }
.cg-vf-dead-badge.dv-dead_store  { background:#4a3410; color:#e6a23c; border-color:#7a5a1a; }
.cg-vf-dead-badge.dv-unused_param{ background:#3a1040; color:#c678dd; border-color:#5a1a6a; }
.cg-vf-dead-badge.dv-dead_alloc  { background:#102a3a; color:#5fb0d8; border-color:#1a4a5a; }
.cg-vf-dead-badge.dv-unused_value{ background:#13332a; color:#4ec99a; border-color:#1a5a44; }
.cg-vf-dead-badge.dv-suppressed  { background:#222831; color:#8a97a8; border-color:#39414d; }
.cg-vf-dead-badge .dv-conf {
  font-size: 8px; opacity: 0.8; margin-left: 3px; font-weight: 600;
}
.cg-vf-dead-badge.dv-lowconf { opacity: 0.92; }
.cg-vf-dead-badge.dv-lowconf::after {
  content: "≈"; margin-left: 3px; font-weight: 700;
}
.cg-vf-node.cg-vf-suppressed { border: 1px dashed #5a6675 !important; opacity: 0.85; }
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
  pointer-events: none; cursor: default; z-index: 1;
  min-width: 80px; min-height: 40px;
}
/* grip/del/resize always interactive even though body is click-through */
.annot-mode-active .cg-vf-annot { pointer-events: all; cursor: move; }
.cg-vf-annot-grip {
  position: absolute; top: 0; left: 0; width: 18px; height: 18px;
  cursor: move; pointer-events: all; z-index: 3;
  background: rgba(70,200,100,0.55); border-radius: 3px 0 4px 0;
}
.cg-vf-annot-grip::before {
  content: ''; position: absolute; top: 4px; left: 4px; width: 8px; height: 8px;
  background:
    linear-gradient(rgba(255,255,255,0.85) 0 0) 0 0/8px 1.5px no-repeat,
    linear-gradient(rgba(255,255,255,0.85) 0 0) 0 3px/8px 1.5px no-repeat,
    linear-gradient(rgba(255,255,255,0.85) 0 0) 0 6px/8px 1.5px no-repeat;
}
.cg-vf-annot-label {
  position: absolute; top: 6px; left: 22px; right: 28px;
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
  pointer-events: all;
}
.cg-vf-annot-del:hover { color: #e74c3c; }
.cg-vf-annot-resize {
  position: absolute; bottom: 0; right: 0; width: 14px; height: 14px;
  cursor: se-resize; background: rgba(70,200,100,0.25); border-radius: 0 0 3px 0;
  pointer-events: all;
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
/* Split-by-scope toggle button (VFI-1) */
#cg-vf-scope-btn {
  padding: 4px 10px; font-size: 11px; font-weight: 600;
  border: 1px solid #3d4451; background: #1a1d23; color: #6cb6ff;
  border-radius: 4px; cursor: pointer; white-space: nowrap; flex-shrink: 0;
}
#cg-vf-scope-btn.active { background: #102132; border-color: #4A90D9; }
#cg-vf-scope-btn:hover { background: #14202e; }
/* Flow-direction toggle button (VFI-7) */
#cg-vf-backward-btn {
  padding: 4px 10px; font-size: 11px; font-weight: 600;
  border: 1px solid #3d4451; background: #1a1d23; color: #d8a657;
  border-radius: 4px; cursor: pointer; white-space: nowrap; flex-shrink: 0;
}
#cg-vf-backward-btn.active { background: #2a1f0e; border-color: #e0a458; }
#cg-vf-backward-btn:hover { background: #221a10; }
/* VF-6: Highlight-direction toggle button */
#cg-vf-hldir-btn {
  padding: 4px 10px; font-size: 11px; font-weight: 600;
  border: 1px solid #3d4451; background: #1a1d23; color: #a78bfa;
  border-radius: 4px; cursor: pointer; white-space: nowrap; flex-shrink: 0;
}
#cg-vf-hldir-btn.active { background: #1e1631; border-color: #7c3aed; }
#cg-vf-hldir-btn:hover { background: #18122a; }
/* Cross-mode flow-trace control: [☑ trace] [⇟ Downstream] */
.cg-trace-ctl {
  display: inline-flex; align-items: center; gap: 6px;
  vertical-align: middle; white-space: nowrap;
}
.cg-trace-ctl .cg-trace-cb-lbl {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 11px; font-weight: 600; color: #a78bfa; cursor: pointer;
  user-select: none;
}
.cg-trace-ctl .cg-trace-cb-lbl input { cursor: pointer; margin: 0; }
.cg-trace-ctl .cg-trace-dir-btn {
  padding: 4px 10px; font-size: 11px; font-weight: 600;
  border: 1px solid #3d4451; background: #1a1d23; color: #a78bfa;
  border-radius: 4px; cursor: pointer; white-space: nowrap; flex-shrink: 0;
}
.cg-trace-ctl .cg-trace-dir-btn.active { background: #1e1631; border-color: #7c3aed; }
.cg-trace-ctl .cg-trace-dir-btn:hover { background: #18122a; }
/* Function-mode floating overlay control (no per-mode toolbar exists). */
#cg-fn-trace-ctl {
  position: absolute; top: 10px; left: 10px; z-index: 60;
  background: rgba(20,23,30,0.92); border: 1px solid #3d4451;
  border-radius: 6px; padding: 6px 9px; backdrop-filter: blur(3px);
  box-shadow: 0 2px 8px rgba(0,0,0,0.4);
}
/* Shared trace styling for custom-DOM modes (script/module/include tiles). */
.cg-trace-dim { opacity: 0.25 !important; transition: opacity .15s; }
.cg-trace-lit { transition: border-color .15s, box-shadow .15s; }
.cg-trace-merge { border-style: dashed !important; }
/* VF mode: enable checkbox sits to the left of the direction button. */
#cg-vf-trace-cb-lbl {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 11px; font-weight: 600; color: #a78bfa; cursor: pointer;
  user-select: none; white-space: nowrap; flex-shrink: 0;
}
#cg-vf-trace-cb-lbl input { cursor: pointer; margin: 0; }
#cg-vf-family-row {
  display: flex; align-items: center; gap: 6px; padding: 4px 0 2px 0;
  flex-wrap: wrap;
}
#cg-vf-family-row .cg-vf-fam-label {
  font-size: 10px; color: #5a6a7a; text-transform: uppercase; letter-spacing: 0.6px; margin-right: 2px;
}
.cg-vf-fam-btn {
  padding: 2px 9px; font-size: 11px; border-radius: 12px; cursor: pointer;
  border: 1px solid #3d4451; background: #23272f; color: #8899aa;
  white-space: nowrap; flex-shrink: 0; transition: opacity 0.15s;
}
.cg-vf-fam-btn.active { color: #e2e8f0; border-color: #6e8fa8; background: #1a2535; }
.cg-vf-fam-btn[data-fam="lugasi"].active { border-color: #e0a458; color: #e0a458; background: #1f1808; }
.cg-vf-fam-btn[data-fam="connect"].active { border-color: #4fc3f7; color: #4fc3f7; background: #08171f; }
.cg-vf-fam-btn[data-fam="member"].active { border-color: #a78bfa; color: #a78bfa; background: #170f2a; }
.cg-vf-fam-btn[data-fam="variable"].active { border-color: #4ec980; color: #4ec980; background: #0a1f14; }
/* VF-4: separator + "show all family flow" action buttons */
.cg-vf-fam-sep { width: 1px; height: 16px; background: #3d4451; margin: 0 4px; flex-shrink: 0; }
.cg-vf-fam-show-btn {
  padding: 2px 10px; font-size: 11px; font-weight: 600; border-radius: 12px; cursor: pointer;
  border: 1px solid #3d4451; background: #23272f; color: #c8d4e0;
  white-space: nowrap; flex-shrink: 0;
}
.cg-vf-fam-show-btn:hover { background: #2a3340; border-color: #6e8fa8; }
.cg-vf-fam-show-btn.active { border-color: #4A90D9; background: #102132; color: #e2e8f0; }
.cg-vf-fam-show-btn[data-fam="lugasi"]:hover { border-color: #e0a458; }
.cg-vf-fam-show-btn[data-fam="lugasi"].active { border-color: #e0a458; color: #e0a458; background: #1f1808; }
.cg-vf-fam-show-btn[data-fam="connect"]:hover { border-color: #4fc3f7; }
.cg-vf-fam-show-btn[data-fam="connect"].active { border-color: #4fc3f7; color: #4fc3f7; background: #08171f; }
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
.cg-dead-why  { color: #8a97a8; font-size: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cg-dead-cat-hdr {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 14px 2px; margin-top: 4px;
}
.cg-dead-cat-n {
  font-size: 10px; color: #6a7a8a; font-weight: 700;
  background: #1e2530; border-radius: 8px; padding: 0 7px;
}
.cg-dead-conf { font-size: 10px; font-weight: 700; text-transform: uppercase; }
.cg-dead-conf-high   { color: #e06c5a; }
.cg-dead-conf-medium { color: #e6a23c; }
.cg-dead-conf-low    { color: #8a97a8; }

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
body[data-theme="light"] .hp-name { color: #a16207; }
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
body[data-theme="light"] .hp-virtual { background:#fef3c7; color:#b45309; }
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
body[data-theme="light"] .cg-modal-title { color: #a16207; }
body[data-theme="light"] .cg-modal-qname { color: #7a8898; }
body[data-theme="light"] .cg-modal-section { border-top-color: #c8d4de; }
body[data-theme="light"] .cg-modal-section h3 { color: #7a8898; }
body[data-theme="light"] .cg-modal-lbl { color: #7a8898; }
body[data-theme="light"] .cg-modal-val { color: #1a2535; }
body[data-theme="light"] .cg-modal-param { background: #edf2f7; color: #1a2535; }
body[data-theme="light"] .cg-modal-param-num { color: #7a8898; }
body[data-theme="light"] .cg-modal-param-name { color: #0f766e; }
body[data-theme="light"] .cg-modal-param-type { color: #1d4ed8; }
body[data-theme="light"] .cg-hl-fn { color: #a16207; }
body[data-theme="light"] .cg-hl-var { color: #0f766e; }
body[data-theme="light"] .cg-hl-type { color: #15803d; }
body[data-theme="light"] .cg-hl-kw { color: #1d4ed8; font-weight: 600; }
body[data-theme="light"] .cg-hl-kw-union { color: #9333ea; font-weight: 600; }
body[data-theme="light"] .cg-hl-kw-enum { color: #b45309; font-weight: 600; }
body[data-theme="light"] .cg-hl-kw-qual { color: #7c3aed; }
body[data-theme="light"] .cg-hl-builtin { color: #2563eb; }
body[data-theme="light"] .cg-hl-punct { color: #64748b; }
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
body[data-theme="light"] .cg-fc-fname { color: #0369a1; }
body[data-theme="light"] .cg-fc-dir { color: #7a8898; }
body[data-theme="light"] .cg-fc-count { background: #dde6f0; color: #4a6070; }
body[data-theme="light"] .cg-fn-row { border-bottom-color: #e5edf4; }
body[data-theme="light"] .cg-fn-row:hover { background: #f0f5fb; }
body[data-theme="light"] .cg-fn-row.sv-selected { background: #dbeafe !important; border-left-color: #3b82f6; }
body[data-theme="light"] .cg-fn-row.sv-match { background: #dcfce7 !important; border-left-color: #22c55e; }
body[data-theme="light"] .cg-fn-row.sv-edge-endpoint { background: #fef9c3 !important; border-left-color: #eab308; }
body[data-theme="light"] .cg-fn-nm { color: #1a2535; }
body[data-theme="light"] .cg-fn-nm-main { color: #a16207; }
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
body[data-theme="light"] .cg-vf-var-name { color: #0f766e; }
body[data-theme="light"] .cg-vf-fn-val { color: #a16207; }
body[data-theme="light"] .cg-vf-file-val { color: #9eb4c8; }
body[data-theme="light"] .cg-vf-type-val { color: #1d4ed8; }
body[data-theme="light"] .cg-vf-snippet { background: #edf2f7; border-top-color: #e5edf4; color: #1e6da0; }
body[data-theme="light"] .cg-vf-doc-chip { background: #f1f8eb; border-top-color: #d4e8c2; color: #4a7a30; }
body[data-theme="light"] .cg-vf-source-val { color: #1e6da0; border-left-color: #9ab0d0; }
body[data-theme="light"] .cg-vf-type-chip { background: #eaf2fb; color: #1d4ed8; border-color: #9ab0d0; }
body[data-theme="light"] .cg-vf-type-chip:hover { background: #dbeafe; border-color: #3b82f6; }
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
body[data-theme="light"] #cg-vf-scope-btn { background: #f0f6ff; color: #1d4ed8; border-color: #93c5fd; }
body[data-theme="light"] #cg-vf-scope-btn:hover { background: #dbeafe; }
body[data-theme="light"] #cg-vf-scope-btn.active { background: #bfdbfe; border-color: #2563eb; }
body[data-theme="light"] #cg-vf-backward-btn { background: #fffaf0; color: #b45309; border-color: #fcd34d; }
body[data-theme="light"] #cg-vf-backward-btn:hover { background: #fef3c7; }
body[data-theme="light"] #cg-vf-backward-btn.active { background: #fde68a; border-color: #d97706; }
body[data-theme="light"] #cg-vf-hldir-btn { background: #f5f3ff; color: #7c3aed; border-color: #c4b5fd; }
body[data-theme="light"] #cg-vf-hldir-btn:hover { background: #ede9fe; }
body[data-theme="light"] #cg-vf-hldir-btn.active { background: #ddd6fe; border-color: #7c3aed; }
body[data-theme="light"] .cg-trace-ctl .cg-trace-cb-lbl,
body[data-theme="light"] #cg-vf-trace-cb-lbl { color: #7c3aed; }
body[data-theme="light"] .cg-trace-ctl .cg-trace-dir-btn { background: #f5f3ff; color: #7c3aed; border-color: #c4b5fd; }
body[data-theme="light"] .cg-trace-ctl .cg-trace-dir-btn:hover { background: #ede9fe; }
body[data-theme="light"] .cg-trace-ctl .cg-trace-dir-btn.active { background: #ddd6fe; border-color: #7c3aed; }
body[data-theme="light"] #cg-fn-trace-ctl { background: rgba(255,255,255,0.92); border-color: #c8d4de; }
body[data-theme="light"] .cg-vf-fam-btn { background: #f4f7fa; color: #7a8898; border-color: #c8d4de; }
body[data-theme="light"] .cg-vf-fam-btn.active { background: #e8f0fa; color: #1a2535; border-color: #6e8fa8; }
body[data-theme="light"] .cg-vf-fam-btn[data-fam="lugasi"].active { border-color: #d97706; color: #b45309; background: #fef3c7; }
body[data-theme="light"] .cg-vf-fam-btn[data-fam="connect"].active { border-color: #0284c7; color: #0369a1; background: #e0f2fe; }
body[data-theme="light"] .cg-vf-fam-btn[data-fam="member"].active { border-color: #7c3aed; color: #6d28d9; background: #ede9fe; }
body[data-theme="light"] .cg-vf-fam-btn[data-fam="variable"].active { border-color: #15803d; color: #166534; background: #dcfce7; }
body[data-theme="light"] .cg-vf-fam-sep { background: #c8d4de; }
body[data-theme="light"] .cg-vf-fam-show-btn { background: #f4f7fa; color: #1a2535; border-color: #c8d4de; }
body[data-theme="light"] .cg-vf-fam-show-btn:hover { background: #e8f0fa; border-color: #6e8fa8; }
body[data-theme="light"] .cg-vf-fam-show-btn.active { background: #e0f2fe; border-color: #0284c7; color: #0369a1; }
body[data-theme="light"] .cg-vf-fam-show-btn[data-fam="lugasi"].active { border-color: #d97706; color: #b45309; background: #fef3c7; }
body[data-theme="light"] .cg-vf-fam-show-btn[data-fam="connect"].active { border-color: #0284c7; color: #0369a1; background: #e0f2fe; }
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
      <button class="cg-btn" id="cg-btn-fn-cluster" title="Cluster function nodes by source file (vis.js prototype)">&#128451; File clusters</button>
      <button class="cg-btn active" id="cg-btn-minimap" title="Toggle overview minimap (semantic-zoom navigation)">&#128506; Minimap</button>
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
  window.cgGraphId = GRAPH_ID;          /* exposed so the Nodebook engine can scope its storage */
  var LARGE_GRAPH   = CG_LARGE_GRAPH;  /* true when node count >= LARGE threshold (2000) */
  var HUGE_GRAPH    = CG_HUGE_GRAPH;   /* true when node count >= HUGE threshold (default 8000) */
  var HUGE_THRESHOLD = CG_HUGE_THRESHOLD;
  var VIRT_DOM      = CG_VIRT_DOM;     /* PERF-8: viewport virtualisation for DOM modes */
  window.CG_VIRT_DOM = VIRT_DOM;       /* exposed so the extras IIFE (module/include) can read it */
  var ROOT_RANKS    = CG_ROOT_RANKS;   /* {node_id: rank} for top-3 root candidates */
  window.CGX_ROOT_RANKS = ROOT_RANKS;  /* exposed for module/include extras */
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

  /* VF-8: hotspot scoring — rank variables by total occurrences + fan-out.
   * Computed once; used to sort the empty-query dropdown and badge top items.
   * Score = occ_count + fn_fanout * 3  (fan-out weighted 3× to surface
   * "god variables" that touch many functions over just deeply-occurring ones). */
  var _VF_HOTSPOT = {};
  var _VF_HOTSPOT_MAX = 0;
  _VF_KEYS.forEach(function(k) {
    var occs = VAR_FLOW_DATA[k] || [];
    var fnSet = {};
    occs.forEach(function(o) { fnSet[o.function_id || o.function_name || '?'] = true; });
    var fanout = Object.keys(fnSet).length;
    var score = occs.length + fanout * 3;
    _VF_HOTSPOT[k] = score;
    if (score > _VF_HOTSPOT_MAX) _VF_HOTSPOT_MAX = score;
  });
  /* Threshold: show 🔥 badge when score is above 90th-percentile of all variables. */
  var _VF_HOTSPOT_SCORES = _VF_KEYS.map(function(k){ return _VF_HOTSPOT[k]; }).sort(function(a,b){ return b-a; });
  var _VF_HOTSPOT_P90 = _VF_HOTSPOT_SCORES[Math.floor(_VF_HOTSPOT_SCORES.length * 0.1)] || 8;

  /* ── PERF-8: viewport virtualisation helper ──────────────────────────
     Applies CSS `content-visibility:auto` to every node element in a custom-DOM
     mode so the browser natively skips layout+paint of off-screen cards while
     panning huge solution graphs. `contain-intrinsic-size` is pinned to each
     element's REAL measured size so card.offsetWidth/Height (used by edge,
     marquee, fit and drag logic) stay correct even while the card is skipped.
     A two-pass read-then-write avoids interleaved layout thrash. Inert (no-op)
     unless VIRT_DOM is on, so default small-graph output is unchanged. */
  function _cgVirtualize(rootEl, selector) {
    if (!window.CG_VIRT_DOM || !rootEl) return;
    var els = rootEl.querySelectorAll(selector);
    if (!els.length) return;
    var dims = new Array(els.length);
    for (var i = 0; i < els.length; i++) {           /* pass 1: read all sizes */
      dims[i] = [els[i].offsetWidth, els[i].offsetHeight];
    }
    for (var j = 0; j < els.length; j++) {           /* pass 2: write virt props */
      var w = dims[j][0], h = dims[j][1];
      if (w > 0 && h > 0) {
        els[j].style.containIntrinsicSize = w + 'px ' + h + 'px';
        els[j].style.contentVisibility = 'auto';
      }
    }
  }
  window._cgVirtualize = _cgVirtualize;
  /* Refresh one element's pinned intrinsic-size after its content height changes
     (e.g. a Script-View card collapse/expand). Safe to call while on-screen. */
  function _cgVirtRefresh(el) {
    if (!window.CG_VIRT_DOM || !el) return el;
    el.style.contentVisibility = '';                 /* force full render to measure */
    var w = el.offsetWidth, h = el.offsetHeight;
    return { el: el, w: w, h: h,
      apply: function() {
        if (w > 0 && h > 0) {
          el.style.containIntrinsicSize = w + 'px ' + h + 'px';
          el.style.contentVisibility = 'auto';
        }
      } };
  }
  window._cgVirtRefresh = _cgVirtRefresh;

  /* ── Shared flow-trace colour engine (cross-mode) ─────────────────────
     Extracted from Variable Flow's VF-2/VF-6 branch highlight so every mode
     (Function / Script / Module / Include / Var Flow) can "click a node →
     colour each downstream/upstream flow path" identically.

     Pure function: given the clicked origin, a flat edge list and a direction,
     it returns per-node branch colours + merge flags. Each mode then paints the
     result onto its own DOM/canvas. No DOM access here, so it is safe to call
     from either embedded script (it lives on `window`).

       origin    : node id that was clicked
       edges     : [{from, to}, ...] in the current mode
       direction : 'downstream' (forward edges) | 'upstream' (reverse edges)
     returns {
       color     : { nodeId: 'hsl(...)' }   per-branch colour for reached nodes
       hue       : { nodeId: <deg> }         numeric hue (marker grouping)
       merge     : { nodeId: true }          reached from >1 immediate branch
       neighbors : [immediate branch node ids]
       nBranch   : neighbors.length
       branchColor(i) -> legend swatch colour for branch i
     } */
  function cgFlowTraceColors(origin, edges, direction) {
    var isUpstream = (direction === 'upstream');
    var adj = {};
    (edges || []).forEach(function(e) {
      if (!e) return;
      var src = isUpstream ? e.to : e.from;
      var dst = isUpstream ? e.from : e.to;
      if (src == null || dst == null) return;
      if (!adj[src]) adj[src] = [];
      if (adj[src].indexOf(dst) < 0) adj[src].push(dst);
    });
    function _hsl(h, s, l) {
      h = ((Math.round(h) % 360) + 360) % 360;
      return 'hsl(' + h + ',' + Math.round(s) + '%,' + Math.round(l) + '%)';
    }
    function _lightness(depth) { return Math.max(40, 64 - depth * 4); }
    var neighbors = adj[origin] || [];
    var nBranch = neighbors.length;
    var colorOf = {}, hueOf = {}, branchSet = {}, visited = {};
    function _rec(id, b) { if (!branchSet[id]) branchSet[id] = {}; branchSet[id][b] = true; }
    var stack = [];
    neighbors.forEach(function(cid, i) {
      stack.push({ id: cid, hue: i * (360 / nBranch), depth: 1, branchIdx: i });
    });
    while (stack.length) {
      var cur = stack.pop();
      _rec(cur.id, cur.branchIdx);
      if (visited[cur.id]) continue;
      visited[cur.id] = true;
      hueOf[cur.id] = cur.hue;
      colorOf[cur.id] = _hsl(cur.hue, 68, _lightness(cur.depth));
      var kids = (adj[cur.id] || []).filter(function(k) { return k !== origin; });
      var k = kids.length;
      kids.forEach(function(kid, j) {
        var childHue = (k > 1)
          ? cur.hue + (j - (k - 1) / 2) * (30 / cur.depth)
          : cur.hue;
        if (!visited[kid]) {
          stack.push({ id: kid, hue: childHue, depth: cur.depth + 1, branchIdx: cur.branchIdx });
        } else {
          _rec(kid, cur.branchIdx);
        }
      });
    }
    var merge = {};
    Object.keys(branchSet).forEach(function(id) {
      if (Object.keys(branchSet[id]).length > 1) merge[id] = true;
    });
    return {
      color: colorOf, hue: hueOf, merge: merge,
      neighbors: neighbors, nBranch: nBranch,
      branchColor: function(i) { return _hsl(i * (360 / nBranch), 68, 60); }
    };
  }
  window.cgFlowTraceColors = cgFlowTraceColors;

  /* Per-mode flow-trace preference helpers (enable flag + direction), persisted
     independently per mode in localStorage. Default: enabled + downstream. */
  function cgTraceEnabled(mode) {
    try {
      var v = localStorage.getItem('cg-trace-enabled-' + mode);
      return v === null ? true : (v === '1');
    } catch (e) { return true; }
  }
  function cgTraceSetEnabled(mode, on) {
    try { localStorage.setItem('cg-trace-enabled-' + mode, on ? '1' : '0'); } catch (e) {}
  }
  function cgTraceDir(mode) {
    try { return localStorage.getItem('cg-trace-dir-' + mode) || 'downstream'; }
    catch (e) { return 'downstream'; }
  }
  function cgTraceSetDir(mode, dir) {
    try { localStorage.setItem('cg-trace-dir-' + mode, dir); } catch (e) {}
  }
  window.cgTraceEnabled = cgTraceEnabled;
  window.cgTraceSetEnabled = cgTraceSetEnabled;
  window.cgTraceDir = cgTraceDir;
  window.cgTraceSetDir = cgTraceSetDir;

  /* ── Reusable toolbar control: [☑ Trace] [⇟ Downstream] ───────────────
     Builds a compact control group for a mode's toolbar. `onToggle(enabled)`
     and `onDir(dir)` fire when the user flips the checkbox / direction button.
     Returns the container element (caller appends it wherever it likes). */
  function cgBuildTraceControl(mode, onToggle, onDir) {
    var wrap = document.createElement('span');
    wrap.className = 'cg-trace-ctl';
    wrap.setAttribute('data-mode', mode);
    var enabled = cgTraceEnabled(mode);
    var dir = cgTraceDir(mode);
    var cbId = 'cg-trace-cb-' + mode;
    wrap.innerHTML =
      '<label class="cg-trace-cb-lbl" title="Click a node to colour its flow paths. ' +
        'Uncheck to drag nodes without re-tracing.">' +
        '<input type="checkbox" id="' + cbId + '"' + (enabled ? ' checked' : '') + '> trace' +
      '</label>' +
      '<button type="button" class="cg-trace-dir-btn' + (dir === 'upstream' ? ' active' : '') + '" ' +
        'title="Flow direction: Downstream = nodes this flows into; Upstream = nodes that feed into this">' +
        (dir === 'upstream' ? '\u2b9d Upstream' : '\u2b9f Downstream') +
      '</button>';
    var cb = wrap.querySelector('input');
    var db = wrap.querySelector('.cg-trace-dir-btn');
    if (cb) cb.addEventListener('change', function() {
      cgTraceSetEnabled(mode, cb.checked);
      if (onToggle) onToggle(cb.checked);
    });
    if (db) db.addEventListener('click', function() {
      var nd = (cgTraceDir(mode) === 'upstream') ? 'downstream' : 'upstream';
      cgTraceSetDir(mode, nd);
      db.classList.toggle('active', nd === 'upstream');
      db.textContent = (nd === 'upstream') ? '\u2b9d Upstream' : '\u2b9f Downstream';
      if (onDir) onDir(nd);
    });
    return wrap;
  }
  window.cgBuildTraceControl = cgBuildTraceControl;

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

  /* ASSIGN_DST_INDEX["fnId::srcVarLower"] = [{dstKey, occ}, ...]
     Built from VAR_FLOW_DATA occurrences that have assign_src set.
     Used by _vfBuildFlowChain (VFI-3) to find variables assigned FROM a tracked var. */
  var ASSIGN_DST_INDEX = {};
  _VF_KEYS.forEach(function(dstKey) {
    VAR_FLOW_DATA[dstKey].forEach(function(o) {
      if (o.assign_src) {
        var k2 = o.function_id + '::' + String(o.assign_src).toLowerCase();
        (ASSIGN_DST_INDEX[k2] = ASSIGN_DST_INDEX[k2] || []).push({dstKey: dstKey, occ: o});
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
  var _fnHierarchical  = false;  /* default: precomputed file-clustered layout
                                    (callers above callees + same-file grouping).
                                    "Layered" button opts into vis.js hierarchical. */
  var _fnClusterByFile = false;  /* LAY-3: vis.js native per-file clusters */
  var _fnClusterIds = {};        /* cluster node id -> 1 */
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
  /* VF-6: click-highlight direction: 'downstream' (default/VF-2) or 'upstream' */
  var _vfHighlightDirection = (function(){
    try { return localStorage.getItem('cg-vf-highlight-dir') || 'downstream'; } catch(e) { return 'downstream'; }
  })();
  /* VF-4: family visibility filter — toggle each family on/off */
  var _vfFamilyFilter = (function(){
    var def = { lugasi: true, connect: true, member: true, variable: true };
    try {
      var s = localStorage.getItem('cg-vf-family-filter');
      return s ? Object.assign(def, JSON.parse(s)) : def;
    } catch(e) { return def; }
  })();

  /* ── Network accessor ─────────────────────────────────────── */
  function getNet() {
    try {
      var n = window.network;
      return (n && n.body) ? n : null;
    } catch(e) { return null; }
  }

  var _fnNodeFileById = {};
  NODE_DATA.forEach(function(n){ _fnNodeFileById[n.id] = (n.meta && n.meta.file_path) ? String(n.meta.file_path) : ''; });

  function _fnFileBase(fp){ return String(fp || '').replace(/.*[\\/]/g, ''); }

  function _fnOpenAllFileClusters(net){
    net = net || getNet();
    if (!net) return;
    Object.keys(_fnClusterIds).forEach(function(cid){
      try {
        if (net.isCluster && net.isCluster(cid)) {
          net.openCluster(cid, { releaseFunction: function(clusterPos, childPos){ return childPos; } });
        }
      } catch(e) {}
      delete _fnClusterIds[cid];
    });
  }

  function _fnApplyFileClusters(net){
    net = net || getNet();
    if (!net) return;
    _fnOpenAllFileClusters(net);
    if (!_fnClusterByFile) return;

    var ids = [];
    try { ids = net.body.data.nodes.getIds(); } catch(e) { return; }
    var fileCounts = {};
    ids.forEach(function(id){
      id = String(id || '');
      if (!id || id.indexOf('__var__') === 0 || id.indexOf('cg_file_cluster::') === 0) return;
      var fp = _fnNodeFileById[id] || '';
      if (!fp) return;
      fileCounts[fp] = (fileCounts[fp] || 0) + 1;
    });

    var files = Object.keys(fileCounts).filter(function(fp){ return fileCounts[fp] >= 2; }).sort();
    if (!files.length) return;

    var isLight = (document.body && document.body.getAttribute('data-theme') === 'light');
    var bg = isLight ? '#e8eff8' : '#1d2734';
    var border = isLight ? '#7fa1c2' : '#4a6d92';
    var font = isLight ? '#1f3b57' : '#c7d9ee';

    files.forEach(function(fp){
      var cid = 'cg_file_cluster::' + fp;
      try {
        net.cluster({
          joinCondition: function(nodeOptions){
            var id = String((nodeOptions && nodeOptions.id) || '');
            if (!id || id.indexOf('__var__') === 0 || id.indexOf('cg_file_cluster::') === 0) return false;
            return (_fnNodeFileById[id] || '') === fp;
          },
          clusterNodeProperties: {
            id: cid,
            shape: 'box',
            borderWidth: 2,
            label: _fnFileBase(fp) + ' (' + fileCounts[fp] + ')',
            title: fp + ' - ' + fileCounts[fp] + ' functions',
            color: {
              background: bg,
              border: border,
              highlight: { background: bg, border: border },
              hover: { background: bg, border: border }
            },
            font: { size: 12, face: 'monospace', color: font }
          }
        });
        if (net.isCluster && net.isCluster(cid)) _fnClusterIds[cid] = 1;
      } catch(e) {}
    });
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
  function _cgHiType(typeStr){
    var s = String(typeStr == null ? '' : typeStr);
    if (!s) return '';
    var KW = {'struct':'cg-hl-kw','class':'cg-hl-kw','union':'cg-hl-kw-union','enum':'cg-hl-kw-enum','typedef':'cg-hl-kw'};
    var QUAL = {'const':1,'volatile':1,'unsigned':1,'signed':1,'static':1,'extern':1,'register':1,'restrict':1,'inline':1,'mutable':1};
    var BUILTIN = {'int':1,'char':1,'float':1,'double':1,'short':1,'long':1,'void':1,'bool':1,'wchar_t':1,'size_t':1,'ssize_t':1,'int8_t':1,'int16_t':1,'int32_t':1,'int64_t':1,'uint8_t':1,'uint16_t':1,'uint32_t':1,'uint64_t':1,'uintptr_t':1,'intptr_t':1,'ptrdiff_t':1};
    var toks = s.match(/[A-Za-z_][A-Za-z0-9_]*|\\s+|[^A-Za-z0-9_\\s]+/g) || [];
    var out = '';
    toks.forEach(function(t){
      if (/^\\s+$/.test(t)) { out += t; return; }
      if (/^[A-Za-z_]/.test(t)) {
        var lc = t.toLowerCase();
        if (KW[lc]) out += '<span class="' + KW[lc] + '">' + esc(t) + '</span>';
        else if (QUAL[lc]) out += '<span class="cg-hl-kw-qual">' + esc(t) + '</span>';
        else if (BUILTIN[lc]) out += '<span class="cg-hl-builtin">' + esc(t) + '</span>';
        else out += '<span class="cg-hl-type">' + esc(t) + '</span>';
      } else {
        out += '<span class="cg-hl-punct">' + esc(t) + '</span>';
      }
    });
    return out;
  }
  function _vfHiSourceText(text, occ) {
    var s = String(text == null ? '' : text);
    if (!s) return '';
    var fnName = occ && occ.function_name ? String(occ.function_name) : '';
    var nameMap = {};
    function addName(v) {
      if (!v) return;
      var k = String(v);
      if (k) nameMap[k] = true;
    }
    addName(occ && occ.name);
    addName(occ && occ._localName);
    addName(occ && occ._origName);
    addName(occ && occ.connect_input_name);
    var typeWords = {};
    var typeText = occ && (occ.data_type || occ.type_hint || '');
    if (typeText) {
      var typeToks = String(typeText).match(/[A-Za-z_][A-Za-z0-9_]*/g) || [];
      typeToks.forEach(function(t){
        var lc = t.toLowerCase();
        if (lc === 'struct' || lc === 'class' || lc === 'typedef') typeWords[t] = 'cg-hl-kw';
        else if (lc === 'union') typeWords[t] = 'cg-hl-kw-union';
        else if (lc === 'enum') typeWords[t] = 'cg-hl-kw-enum';
        else if (lc === 'const' || lc === 'volatile' || lc === 'unsigned' || lc === 'signed' || lc === 'static' || lc === 'extern' || lc === 'register' || lc === 'restrict' || lc === 'inline' || lc === 'mutable') typeWords[t] = 'cg-hl-kw-qual';
        else if (lc === 'int' || lc === 'char' || lc === 'float' || lc === 'double' || lc === 'short' || lc === 'long' || lc === 'void' || lc === 'bool' || lc === 'wchar_t' || lc === 'size_t' || lc === 'ssize_t' || lc === 'int8_t' || lc === 'int16_t' || lc === 'int32_t' || lc === 'int64_t' || lc === 'uint8_t' || lc === 'uint16_t' || lc === 'uint32_t' || lc === 'uint64_t' || lc === 'uintptr_t' || lc === 'intptr_t' || lc === 'ptrdiff_t') typeWords[t] = 'cg-hl-builtin';
        else typeWords[t] = 'cg-hl-type';
      });
    }
    var toks = s.match(/[A-Za-z_][A-Za-z0-9_]*|\\s+|[^A-Za-z0-9_\\s]+/g) || [];
    var out = '';
    toks.forEach(function(t){
      if (/^\\s+$/.test(t)) { out += t; return; }
      if (/^[A-Za-z_]/.test(t)) {
        if (fnName && t === fnName) out += '<span class="cg-hl-fn">' + esc(t) + '</span>';
        else if (nameMap[t]) out += '<span class="cg-hl-var">' + esc(t) + '</span>';
        else if (typeWords[t]) out += '<span class="' + typeWords[t] + '">' + esc(t) + '</span>';
        else out += esc(t);
      } else {
        out += '<span class="cg-hl-punct">' + esc(t) + '</span>';
      }
    });
    return out;
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

  /* ── Function-mode flow-trace (vis.js) ───────────────────────────────
     Click a node → colour its downstream/upstream flow paths using the shared
     cross-mode engine. This vis.js build does NOT support a per-node `opacity`
     option (confirmed via diagnostics), so dimming is done with supported
     channels only: muted `color`/`font` for non-traced nodes, branch-hued
     `color.border` + `shadow` for traced nodes, and — most importantly — the
     vis EDGES along each path are recoloured per branch (with the rest dimmed
     via the supported edge `color.opacity`) so the downstream/upstream flow is
     clearly visible. Original node AND edge styling is snapshotted so clearing
     restores the exact prior look, including any active edge-category colours
     (Rule 9). The click→summary (selectNode→openDetail) keeps working — the
     trace layers on top of it. */
  var _fnTraceOrigin = null;
  var _fnTraceSnap = {};
  var _fnTraceEdgeSnap = {};
  function _fnTraceSnapshot(ds, id) {
    if (_fnTraceSnap[id]) return;
    var it = ds.get(id) || {};
    _fnTraceSnap[id] = { color: it.color, borderWidth: it.borderWidth,
                         shadow: it.shadow, font: it.font };
  }
  function _fnTraceSnapEdge(ds, id) {
    if (_fnTraceEdgeSnap[id]) return;
    var it = ds.get(id) || {};
    _fnTraceEdgeSnap[id] = { color: it.color, width: it.width };
  }
  function _fnDimEdgeColor(snapColor) {
    var base = '#5a6472';
    if (typeof snapColor === 'string') base = snapColor;
    else if (snapColor && snapColor.color) base = snapColor.color;
    return { color: base, opacity: 0.07 };
  }
  function _fnClearTrace() {
    var net = getNet();
    if (!net) { _fnTraceSnap = {}; _fnTraceEdgeSnap = {}; _fnTraceOrigin = null; return; }
    var ds = window.nodes || net.body.data.nodes;
    var eds = window.edges || net.body.data.edges;
    var updates = [];
    Object.keys(_fnTraceSnap).forEach(function(id) {
      var s = _fnTraceSnap[id];
      updates.push({ id: id,
        color: (s.color === undefined ? null : s.color),
        borderWidth: (s.borderWidth === undefined ? 1 : s.borderWidth),
        shadow: (s.shadow === undefined ? false : s.shadow),
        font: (s.font === undefined ? null : s.font) });
    });
    if (updates.length && ds) { try { ds.update(updates); } catch(e) {} }
    var eupd = [];
    Object.keys(_fnTraceEdgeSnap).forEach(function(id) {
      var s = _fnTraceEdgeSnap[id];
      eupd.push({ id: id,
        color: (s.color === undefined ? null : s.color),
        width: (s.width === undefined ? null : s.width) });
    });
    if (eupd.length && eds) { try { eds.update(eupd); } catch(e) {} }
    _fnTraceSnap = {};
    _fnTraceEdgeSnap = {};
    _fnTraceOrigin = null;
    try { net.redraw(); } catch(e) {}
  }
  function _fnApplyTrace(originId) {
    var net = getNet();
    if (!net) return;
    var ds = window.nodes || net.body.data.nodes;
    if (!ds) return;
    _fnClearTrace();
    var trace = window.cgFlowTraceColors(originId, EDGE_DATA, window.cgTraceDir('fn'));
    if (!trace.neighbors.length) return;   /* leaf/root in this direction */
    _fnTraceOrigin = originId;
    var updates = [];
    ds.getIds().forEach(function(id) {
      _fnTraceSnapshot(ds, id);
      if (id === originId) {
        updates.push({ id: id, borderWidth: 3,
          shadow: { enabled: true, color: 'rgba(247,215,116,0.95)', size: 20, x: 0, y: 0 } });
        return;
      }
      var c = trace.color[id];
      if (c) {
        var oc = _fnTraceSnap[id].color;
        var bg = (oc && typeof oc === 'object' && oc.background) ? oc.background
               : (typeof oc === 'string' ? oc : '#1f2630');
        var sz = trace.merge[id] ? 20 : 14;
        updates.push({ id: id, borderWidth: 3,
          color: { background: bg, border: c, highlight: { background: bg, border: c } },
          shadow: { enabled: true, color: c, size: sz, x: 0, y: 0 } });
      } else {
        updates.push({ id: id, borderWidth: 1, shadow: false,
          color: { background: '#222831', border: '#333a44',
                   highlight: { background: '#222831', border: '#333a44' } },
          font: { color: '#55606e' } });
      }
    });
    try { ds.update(updates); } catch(e) {}

    /* Colour the vis edges along the traced paths; dim the rest. */
    var eds = window.edges || net.body.data.edges;
    if (eds) {
      var isUp = (window.cgTraceDir('fn') === 'upstream');
      var origStr = String(originId);
      var eupd = [];
      eds.getIds().forEach(function(eid) {
        _fnTraceSnapEdge(eds, eid);
        var ed = null;
        for (var k = 0; k < EDGE_DATA.length; k++) {
          if (String(EDGE_DATA[k].id) === String(eid)) { ed = EDGE_DATA[k]; break; }
        }
        var ecol = null;
        if (ed) {
          var src = isUp ? ed.to : ed.from;
          var dst = isUp ? ed.from : ed.to;
          var srcOk = (String(src) === origStr) || trace.color[src];
          var dc = trace.color[dst];
          if (srcOk && dc) ecol = dc;
        }
        if (ecol) {
          eupd.push({ id: eid, width: 3,
            color: { color: ecol, highlight: ecol, opacity: 1, inherit: false } });
        } else {
          var dc2 = _fnDimEdgeColor(_fnTraceEdgeSnap[eid].color);
          eupd.push({ id: eid,
            color: { color: dc2.color, highlight: dc2.color, opacity: dc2.opacity, inherit: false } });
        }
      });
      if (eupd.length) { try { eds.update(eupd); } catch(e) {} }
    }
    try { net.redraw(); } catch(e) {}
  }
  window._fnClearTrace = _fnClearTrace;

  /* Floating control for Function mode (it has no per-mode toolbar). */
  function _fnEnsureTraceControl() {
    if (document.getElementById('cg-fn-trace-ctl')) return;
    var netEl = document.getElementById('mynetwork');
    var host = (netEl && netEl.parentElement) ? netEl.parentElement : netEl;
    if (!host) return;
    if (getComputedStyle(host).position === 'static') host.style.position = 'relative';
    var ctl = window.cgBuildTraceControl('fn',
      function(enabled) { if (!enabled) _fnClearTrace(); },
      function(dir) { if (_fnTraceOrigin) _fnApplyTrace(_fnTraceOrigin); });
    ctl.id = 'cg-fn-trace-ctl';
    host.appendChild(ctl);
    _fnSyncTraceControlVis();
  }
  function _fnSyncTraceControlVis() {
    var ctl = document.getElementById('cg-fn-trace-ctl');
    if (ctl) ctl.style.display = (currentMode === 'fn') ? '' : 'none';
  }
  window._fnSyncTraceControlVis = _fnSyncTraceControlVis;

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
        h += '<span class="cg-edge-arg-type">type: ' + _cgHiType(argType) + '</span></div>';
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
    if (currentMode === 'types' && window.cgxTypeSearch) { window.cgxTypeSearch(q); return; }
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
      if (currentMode === 'types') return;
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
        if (currentMode === 'types' && window.cgxTypeSearchEnter) { window.cgxTypeSearchEnter(searchInput.value); return; }
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
    if (currentMode === 'types' && window.cgxTypeHighlight) {
      var thq = searchInput ? searchInput.value.trim() : '';
      var thCount = window.cgxTypeHighlight(thq ? [thq] : []);
      _flashBtn(btnHighlight, thCount ? thCount + ' found' : 'No match', 1200);
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
    if (currentMode === 'types' && window.cgxTypeCenter) {
      var tcq = searchInput ? searchInput.value.trim() : '';
      if (!tcq || !window.cgxTypeCenter(tcq)) _flashBtn(btnCenter, 'No match', 900);
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
    if (currentMode === 'types' && window.cgxTypeIsolate) {
      var tq = searchInput ? searchInput.value.trim() : '';
      if (!tq) { _flashBtn(btnIsolate, 'No match', 900); return; }
      var tCount = window.cgxTypeIsolate(tq, _getDepth(), _getDir());
      _flashBtn(btnIsolate, tCount ? tCount + ' visible' : 'No match', 1400);
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
    if (currentMode === 'types' && window.cgxTypeExpand) {
      var teq = searchInput ? searchInput.value.trim() : '';
      if (!teq) { _flashBtn(btnExpand, 'No match', 900); return; }
      var teCount = window.cgxTypeExpand(teq, _getDepth(), _getDir());
      _flashBtn(btnExpand, teCount ? teCount + ' shown' : 'No match', 1400);
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

  /* Clear any active flow-trace highlight, in whichever mode is active. */
  function _cgClearAllTraces() {
    try { _fnClearTrace(); } catch(e) {}
    try { _svClearTrace(); } catch(e) {}
    try { if (_vfBranchActive) _vfClearBranchHighlight(); } catch(e) {}
    try { if (window._mvClearTrace) window._mvClearTrace(); } catch(e) {}
    try { if (window._ivClearTrace) window._ivClearTrace(); } catch(e) {}
  }
  window._cgClearAllTraces = _cgClearAllTraces;
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') _cgClearAllTraces();
  });

  if (btnClearFocus) btnClearFocus.addEventListener('click', function() {
    _cgClearAllTraces();
    if (currentMode === 'varflow') { _vfClearHighlight(); return; }
    selectedNode = null;
    if (currentMode === 'script') { _svClearHighlight(); updateHint([]); return; }
    if (currentMode === 'module' && window.cgxModuleClearFocus) { window.cgxModuleClearFocus(); updateHint([]); return; }
    if (currentMode === 'inc' && window.cgxIncClearFocus) { window.cgxIncClearFocus(); updateHint([]); return; }
    if (currentMode === 'types' && window.cgxTypeClearFocus) { window.cgxTypeClearFocus(); updateHint([]); return; }
    _showAllNodes();
    updateHint([]);
  });

  if (btnFit) btnFit.addEventListener('click', function() {
    if (currentMode === 'varflow') { _vfFitAll(); return; }
    if (currentMode === 'script') { _svFitView(); return; }
    if (currentMode === 'module' && window.cgxModuleFit) { window.cgxModuleFit(); return; }
    if (currentMode === 'inc' && window.cgxIncFit) { window.cgxIncFit(); return; }
    if (currentMode === 'types' && window.cgxTypeFit) { window.cgxTypeFit(); return; }
    var net = getNet(); if (!net) return;
    net.fit({ animation: { duration: 500 } });
  });

  if (btnShowAll) btnShowAll.addEventListener('click', function() {
    _cgClearAllTraces();
    if (currentMode === 'varflow') { _vfClearHighlight(); return; }
    selectedNode = null;
    if (currentMode === 'script') { _svClearHighlight(); updateHint([]); return; }
    if (currentMode === 'module' && window.cgxModuleClearFocus) { window.cgxModuleClearFocus(); updateHint([]); return; }
    if (currentMode === 'inc' && window.cgxIncClearFocus) { window.cgxIncClearFocus(); updateHint([]); return; }
    if (currentMode === 'types' && window.cgxTypeShowAll) { window.cgxTypeShowAll(); updateHint([]); return; }
    _showAllNodes();
    updateHint([]);
  });

  /* Save Layout — mode-aware explicit snapshot to localStorage */
  if (btnSaveLayout) btnSaveLayout.addEventListener('click', function() {
    if (currentMode === 'varflow') {
      try { localStorage.setItem(VF_LAYOUT_PFX + _vfLayoutKey(), JSON.stringify(_vfNodeOverrides)); } catch(e) {}
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
    if (currentMode === 'types' && window.cgxTypeSaveLayout) {
      window.cgxTypeSaveLayout();
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
    if (currentMode === 'types' && window.cgxTypeResetLayout) {
      window.cgxTypeResetLayout();
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
      try { localStorage.removeItem(VF_LAYOUT_PFX + _vfLayoutKey()); } catch(e) {}
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
    if (currentMode === 'types' && window.cgxTypeClearSaved) {
      window.cgxTypeClearSaved();
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
    if (meta.is_virtual)
      h += '<span class="hp-badge hp-virtual">virtual</span>';
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
      h += _hpRow('Returns', '<span class="hp-val">' + _cgHiType(meta.return_type) + '</span>');

    if (meta.parameters && meta.parameters.length) {
      h += '<div class="hp-divider"></div>';
      h += '<div class="hp-section-hdr">Parameters (' + meta.parameters.length + ')</div>';
      meta.parameters.forEach(function(p, i) {
        var pstr = '<span class="cg-hl-var">' + esc(p.name) + '</span>';
        if (p.type_hint) pstr += ' : ' + _cgHiType(p.type_hint);
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
    if (m.is_virtual) h += '<span class="hp-badge hp-virtual">virtual</span>';
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
      h += '<div class="cg-modal-row"><span class="cg-modal-lbl">Return type</span><span class="cg-modal-val">' + _cgHiType(m.return_type) + '</span></div>';
    } else {
      h += '<div class="cg-modal-row"><span class="cg-modal-lbl">Return type</span><span class="cg-modal-val" style="color:#5a6a7a">not specified</span></div>';
    }
    if (m.parameters && m.parameters.length) {
      h += '<div class="cg-modal-row"><span class="cg-modal-lbl">Parameters</span><span class="cg-modal-val">' + m.parameters.length + ' argument(s)</span></div>';
      m.parameters.forEach(function(p, i) {
        h += '<div class="cg-modal-param">';
        h += '<span class="cg-modal-param-num">' + (i+1) + '.</span>';
        h += '<span class="cg-modal-param-name">' + esc(p.name) + '</span>';
        if (p.type_hint) h += '<span class="cg-modal-param-type">: ' + _cgHiType(p.type_hint) + '</span>';
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
      h += '<span class="cg-modal-param-type">type: ' + _cgHiType(v.type_hint || 'unknown') + '</span>';
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

    var _fnFontColor = (document.body && document.body.getAttribute('data-theme') === 'light') ? '#a16207' : '#DCDCAA';
    net.setOptions({
      interaction: { dragNodes:true, dragView:true, zoomView:true, hover:true, tooltipDelay:9999, multiselect:true },
      nodes: { font: { color: _fnFontColor } }
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

    if (_fnClusterByFile) _fnApplyFileClusters(net);

    /* Default function view = precomputed file-clustered layout (INITIAL_POS +
       saved delta already applied above). The "Layered" button opts into vis.js
       hierarchical on demand; we no longer auto-apply it on load. */

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
        var nid = p.nodes[0];
        if (_fnClusterByFile && net.isCluster && net.isCluster(nid)) {
          try { net.openCluster(nid); delete _fnClusterIds[nid]; } catch(e) {}
          return;
        }
        openModal(nid);
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
          if (nodeId) {
            if (_fnClusterByFile && curNet.isCluster && curNet.isCluster(nodeId)) {
              try { curNet.openCluster(nodeId); delete _fnClusterIds[nodeId]; } catch(e) {}
              return;
            }
            openModal(nodeId);
          }
        } catch(e) {}
      });
    }

    /* Click on node: flow-trace only (no edge popup). Click on a bare edge:
       show call details. Click on background: close detail panel + clear. */
    net.on('click', function(p) {
      if (p.nodes && p.nodes.length) {
        /* A node was clicked → the ONLY action is the flow-trace highlight
           (gated by the per-mode checkbox). No "X calls Y" edge popup, even
           though vis includes the node's connected edges in p.edges.
           The short summary on the right is driven separately by selectNode.
           Clicking the same origin again clears the trace (keeps the summary). */
        if (window.cgTraceEnabled('fn')) {
          if (_fnTraceOrigin === p.nodes[0]) _fnClearTrace();
          else _fnApplyTrace(p.nodes[0]);
        }
        return;
      }
      if (p.edges && p.edges.length) {
        showEdgeDetails(p.edges[0], p.event && p.event.srcEvent);
        return;
      }
      if ((!p.nodes || !p.nodes.length) && (!p.edges || !p.edges.length)) {
        var d = document.getElementById('cg-detail');
        if (d) d.classList.remove('open');
        _clearEdgeHighlight(false);
        _fnClearTrace();
      }
    });
    _fnEnsureTraceControl();
  }
  /* ── Large-graph warning ───────────────────────────────────── */
  (function() {
    if (!LARGE_GRAPH) return;
    var warn = document.getElementById('cg-large-graph-warn');
    if (warn) {
      warn.style.display = 'block';
      warn.innerHTML = '<b>&#9888; Large graph (' + NODE_DATA.length + ' nodes)</b><br>'
        + 'Function Nodes mode uses vis.js canvas — smooth above ~2k nodes may be limited. '
        + 'Edges are straight for performance. '
        + 'Node labels stay readable at every zoom level (semantic zoom). '
        + 'Script Nodes and Var Flow modes are unaffected.';
    }
  })();

  /* ── Function-mode signature cards + minimap ──
     Each function node is drawn ONCE as a syntax-highlighted source-signature
     card (reusing the .cg-hl-* palette from Script/VarFlow/Types modes). The
     cards live in a single transformed layer that mirrors the vis.js viewport,
     so they scale and pan with zoom exactly like the original nodes — zoom-in
     enlarges, zoom-out shrinks. The underlying vis.js box is made invisible so
     there is only ONE node on screen. A minimap gives spatial context. The
     layer is pointer-events:none, so vis.js keeps hover / click / drag /
     selection. */
  (function() {
    var FN_CARD_MAX = 4000;   /* above this, keep plain vis.js boxes (perf) */
    var LANGCOL = {'python':'#4A90D9','c':'#E8832A','c++':'#27AE60','cpp':'#27AE60','matlab':'#8E44AD'};
    var _layer=null, _cards={}, _pos=null, _posT=0, _hid=false, _wired=false, _built=false;
    var _disabled=(NODE_DATA.length>FN_CARD_MAX);
    var _sel={};
    var _map=null, _mapCtx=null, _mapVisible=(NODE_DATA.length>150), _mapT=0, _bbox=null;
    var _byId={}; NODE_DATA.forEach(function(n){ _byId[n.id]=n.meta; });

    function _isFn(id){ return String(id).indexOf('__var__')!==0; }
    function _meta(id){ return _byId[id] || null; }
    function _langColor(meta){
      if (!meta || meta.is_external) return '#95A5A6';
      return LANGCOL[String(meta.language||'').toLowerCase()] || '#95A5A6';
    }

    function _refreshPos(){
      var net=getNet(); if(!net) return;
      try { _pos=net.getPositions(); } catch(e) { _pos=null; }
      _posT=Date.now();
      _computeBBox();
    }
    function _computeBBox(){
      if(!_pos){ _bbox=null; return; }
      var minx=1e9,miny=1e9,maxx=-1e9,maxy=-1e9,any=false;
      for(var id in _pos){ if(!_isFn(id)) continue; var p=_pos[id];
        if(p.x<minx)minx=p.x; if(p.y<miny)miny=p.y; if(p.x>maxx)maxx=p.x; if(p.y>maxy)maxy=p.y; any=true; }
      _bbox = any ? {minx:minx,miny:miny,maxx:maxx,maxy:maxy} : null;
    }
    /* Make the vis.js function boxes invisible so only the card shows. */
    function _hideBoxes(){
      if(_hid) return;
      var net=getNet(); if(!net||!net.body) return;
      try {
        var TR='rgba(0,0,0,0)';
        var ids=net.body.data.nodes.getIds(), upd=[];
        ids.forEach(function(id){
          if(!_isFn(id)) return;
          upd.push({id:id, borderWidth:0, shadow:{enabled:false}, font:{color:TR},
            color:{background:TR, border:TR,
                   highlight:{background:TR, border:TR}, hover:{background:TR, border:TR}}});
        });
        if(upd.length) net.body.data.nodes.update(upd);
        _hid=true;
      } catch(e){}
    }
    function _badge(meta){
      if(meta && meta.root_rank) return '<span class="cg-fnsig-badge cg-fnsig-badge-root">Root #'+meta.root_rank+'</span>';
      if(meta && meta.is_external) return '<span class="cg-fnsig-badge cg-fnsig-badge-ext">external</span>';
      return '';
    }
    function _buildCard(el, meta){
      el.className='cg-fnsig cg-fnsig-detail';
      el.style.borderColor=_langColor(meta);
      var h='';
      if(meta.parent && meta.parent!=='<external>')
        h += '<span class="cg-fnsig-parent">'+esc(meta.parent)+'::</span>';
      var head='';
      if(meta.return_type) head += _cgHiType(meta.return_type)+' ';
      head += '<span class="cg-hl-fn" style="font-weight:700">'+esc(meta.name||'')+'</span>';
      head += '<span class="cg-hl-punct">(</span>';
      if(meta.parameters && meta.parameters.length){
        head += meta.parameters.map(function(p){
          var s=''; if(p.type_hint) s += _cgHiType(p.type_hint)+' ';
          s += '<span class="cg-hl-var">'+esc(p.name||'')+'</span>'; return s;
        }).join('<span class="cg-hl-punct">, </span>');
      }
      head += '<span class="cg-hl-punct">)</span>';
      h += '<div>'+head+_badge(meta)+'</div>';
      if(meta.file_path && meta.file_path!=='<external>'){
        var fn2=String(meta.file_path).replace(/.*[\\\\/]/,'');
        h += '<div class="cg-fnsig-foot">'+esc(fn2)+(meta.line_start?(':'+meta.line_start):'')+'</div>';
      }
      el.innerHTML=h;
    }
    function _ensureLayer(){
      if(_layer) return _layer;
      var netEl=document.getElementById('mynetwork');
      var wr=(netEl&&netEl.parentElement&&netEl.parentElement!==document.body)?netEl.parentElement:netEl;
      if(!wr) return null;
      wr.style.position='relative';
      _layer=document.createElement('div');
      _layer.id='cg-fn-sig-layer';
      _layer.style.cssText='position:absolute;top:0;left:0;pointer-events:none;z-index:6;transform-origin:0 0';
      wr.appendChild(_layer);
      _ensureMap(wr);
      return _layer;
    }
    /* Create every card once, positioned in graph coords (static). */
    function _buildAll(){
      if(_built || _disabled || !_layer || !_pos) return;
      var frag=document.createDocumentFragment();
      for(var i=0;i<NODE_DATA.length;i++){
        var id=NODE_DATA[i].id, meta=NODE_DATA[i].meta;
        var p=_pos[id]; if(!meta || !p) continue;
        var el=document.createElement('div');
        _buildCard(el, meta);
        el.style.left=p.x+'px'; el.style.top=p.y+'px';
        _cards[id]=el; frag.appendChild(el);
      }
      _layer.appendChild(frag);
      _built=true;
    }
    /* Re-sync card graph positions (after drag / layout change). */
    function _repositionAll(){
      if(!_pos) return;
      for(var id in _cards){ var p=_pos[id]; if(p){ _cards[id].style.left=p.x+'px'; _cards[id].style.top=p.y+'px'; } }
    }
    function _applySel(nodes){
      var next={};
      (nodes||[]).forEach(function(id){ next[id]=1; });
      for(var id in _sel){ if(!next[id] && _cards[id]) _cards[id].classList.remove('cg-fnsig-sel'); }
      for(var id2 in next){ if(_cards[id2]) _cards[id2].classList.add('cg-fnsig-sel'); }
      _sel=next;
    }
    function _update(){
      var net=getNet(); if(!net) return;
      if(currentMode!=='fn'){ if(_layer)_layer.style.display='none'; if(_map)_map.style.display='none'; return; }
      if(!_ensureLayer()) return;
      _layer.style.display='';
      if(_map) _map.style.display=_mapVisible?'':'none';
      if(!_disabled){
        _hideBoxes();
        if(!_pos) _refreshPos();
        _buildAll();
      }
      var cont=document.getElementById('mynetwork'); if(!cont) return;
      var scale, vp;
      try { scale=net.getScale(); vp=net.getViewPosition(); } catch(e){ return; }
      var W=cont.offsetWidth, H=cont.offsetHeight, cx=W/2, cy=H/2;
      /* Mirror the vis.js viewport transform so cards scale + pan with zoom. */
      _layer.style.transform='translate('+(cx-vp.x*scale)+'px,'+(cy-vp.y*scale)+'px) scale('+scale+')';
      /* Thicken the language contour as we zoom out so nodes stay visible. */
      var zo = scale>=0.55 ? '0' : (scale>=0.38 ? '1' : (scale>=0.24 ? '2' : '3'));
      if(_layer.dataset.zo!==zo) _layer.dataset.zo=zo;
      var now=Date.now();
      if(!_disabled && (now-_posT)>600){ _refreshPos(); _repositionAll(); }
      _drawMap(scale, vp, W, H);
    }

    /* ── Minimap ── */
    function _ensureMap(wr){
      if(_map) return;
      _map=document.createElement('canvas');
      _map.id='cg-fnmap'; _map.width=180; _map.height=130;
      _map.style.cssText='position:absolute;right:12px;bottom:12px;z-index:15;border-radius:6px;cursor:pointer';
      if(!_mapVisible) _map.style.display='none';
      wr.appendChild(_map);
      try { _mapCtx=_map.getContext('2d'); } catch(e) { _mapCtx=null; }
      _map.addEventListener('mousedown', _mapNav);
      _map.addEventListener('mousemove', function(e){ if(e.buttons&1) _mapNav(e); });
    }
    function _mapNav(e){
      if(!_bbox) return; var net=getNet(); if(!net) return;
      var r=_map.getBoundingClientRect();
      var gw=(_bbox.maxx-_bbox.minx)||1, gh=(_bbox.maxy-_bbox.miny)||1;
      var pad=6, s=Math.min((_map.width-2*pad)/gw,(_map.height-2*pad)/gh);
      var ox=(_map.width-gw*s)/2, oy=(_map.height-gh*s)/2;
      var gx=_bbox.minx+((e.clientX-r.left)-ox)/s;
      var gy=_bbox.miny+((e.clientY-r.top)-oy)/s;
      try { net.moveTo({position:{x:gx,y:gy}, animation:false}); } catch(e2){}
    }
    function _drawMap(scale, vp, W, H){
      if(!_map || !_mapVisible || !_mapCtx || !_pos){ if(!_bbox && !_disabled) { if(!_pos)_refreshPos(); } return; }
      if(!_bbox) return;
      var now=Date.now(); if(now-_mapT<60) return; _mapT=now;
      var mw=_map.width, mh=_map.height, pad=6;
      var gw=(_bbox.maxx-_bbox.minx)||1, gh=(_bbox.maxy-_bbox.miny)||1;
      var s=Math.min((mw-2*pad)/gw,(mh-2*pad)/gh);
      var ox=(mw-gw*s)/2, oy=(mh-gh*s)/2;
      var light=document.body.getAttribute('data-theme')==='light';
      _mapCtx.clearRect(0,0,mw,mh);
      _mapCtx.fillStyle=light?'rgba(255,255,255,0.92)':'rgba(20,24,31,0.90)';
      _mapCtx.fillRect(0,0,mw,mh);
      _mapCtx.strokeStyle=light?'#c8d4de':'#2d3139'; _mapCtx.strokeRect(0.5,0.5,mw-1,mh-1);
      for(var id in _pos){ if(!_isFn(id)) continue; var p=_pos[id];
        _mapCtx.fillStyle=_langColor(_meta(id));
        _mapCtx.fillRect(ox+(p.x-_bbox.minx)*s-1, oy+(p.y-_bbox.miny)*s-1, 2, 2);
      }
      var hx=(W/2)/scale, hy=(H/2)/scale;
      _mapCtx.strokeStyle=light?'#1d4ed8':'#74B3F7'; _mapCtx.lineWidth=1;
      _mapCtx.strokeRect(ox+((vp.x-hx)-_bbox.minx)*s, oy+((vp.y-hy)-_bbox.miny)*s, (2*hx)*s, (2*hy)*s);
    }
    function _toggleMap(){
      _mapVisible=!_mapVisible;
      if(_map) _map.style.display=_mapVisible?'':'none';
      var mb=document.getElementById('cg-btn-minimap');
      if(mb) mb.classList.toggle('active', _mapVisible);
      if(_mapVisible) _update();
    }

    function _wire(){
      if(_wired) return; var net=getNet(); if(!net) return; _wired=true;
      _refreshPos();
      net.on('afterDrawing', _update);
      net.on('zoom', _update);
      net.on('dragEnd', function(){ _refreshPos(); _repositionAll(); _update(); });
      net.on('selectNode', function(p){ _applySel(p.nodes); });
      net.on('deselectNode', function(){ _applySel([]); });
      net.on('dragStart', function(){ try { _applySel(net.getSelectedNodes()||[]); } catch(e){} });
      var mb=document.getElementById('cg-btn-minimap');
      if(mb){ mb.classList.toggle('active', _mapVisible); mb.addEventListener('click', _toggleMap); }
      var fb=document.getElementById('cg-btn-mode-fn');
      if(fb) fb.addEventListener('click', function(){ setTimeout(_update, 90); });
      _update();
      setTimeout(_update, 40);
    }
    var _t=setInterval(function(){ if(getNet()){ clearInterval(_t); _wire(); } }, 60);
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
      if (net) {
        try { if (_fnClusterByFile) _fnApplyFileClusters(net); } catch(e) {}
        try { net.fit({ animation: false }); } catch(e) {}
      }
      /* Inject Fn-mode annotation layer on first show */
      setTimeout(_fnInitAnnotLayer, 80);
    }
    if (window._fnSyncTraceControlVis) window._fnSyncTraceControlVis();
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
  /* Cross-mode jump target for Type Nodes Mode (TYP-3): isolate a set of
   * function node_ids in the Function view. Caller switches to 'fn' first. */
  window.cgIsolateFunctionSet = function(ids) {
    var present = {}, visited = {};
    ALL_NODE_IDS.forEach(function(id) { present[id] = 1; });
    (ids || []).forEach(function(id) { if (present[id]) visited[id] = 1; });
    var n = Object.keys(visited).length;
    if (!n) return 0;
    _isolateNodes(visited);
    var net = getNet();
    if (net) { try { net.selectNodes(Object.keys(visited).slice(0, 1)); } catch (e) {} }
    return n;
  };
  window._cgSetCurrentMode = function(mode) {
    currentMode = mode;
    _updateLayoutBtns(mode);
    if (window._fnSyncTraceControlVis) window._fnSyncTraceControlVis();
    if (searchInput) {
      if (mode === 'inc') searchInput.placeholder = 'Header name...';
      else if (mode === 'module') searchInput.placeholder = 'Module name...';
      else if (mode === 'varflow') searchInput.placeholder = 'Variable name...';
      else if (mode === 'types') searchInput.placeholder = 'Type / struct / field\u2026';
      else searchInput.placeholder = 'Function name...';
    }
    if (searchHint && mode === 'types') searchHint.textContent = 'Search types \u2022 click a result to focus';
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
    var ot = row.offsetTop, oh = row.offsetHeight;
    /* PERF-8: when content-visibility skips this off-screen card the inner row
       offsets read 0 — fall back to the build-time cache. Collapsed cards keep
       their legacy header-anchored behaviour (oh stays 0). */
    if (!oh && !card.classList.contains('sv-collapsed')) {
      var _g = (window._svRowGeom||{})[nid];
      if (_g) { ot = _g.oy; oh = _g.oh; }
    }
    var ry = cy + ot + oh/2;
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
    function folderOf(fp) {
      var m = String(fp).replace(/[\\\\/]+$/, '').match(/^(.*)[\\\\/][^\\\\/]+$/);
      return m ? m[1] : '';
    }
    function compFolder(comp) {
      /* dominant immediate-parent folder among a component's files */
      var counts = {}, best = '', bestN = -1;
      comp.forEach(function(fp){ var f = folderOf(fp); counts[f] = (counts[f]||0)+1; });
      Object.keys(counts).forEach(function(f){
        if (counts[f] > bestN || (counts[f] === bestN && f < best)) { bestN = counts[f]; best = f; }
      });
      return best;
    }
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

    /* Folder super-clusters: keep the ordering heuristic above as the folder
       order (first-seen), but make all components of the same immediate-parent
       folder contiguous so same-folder file-cards sit together. Spacing only —
       a FOLDER_GAP is inserted between folders during packing (no labels). */
    (function() {
      var seen = {}, buckets = [], byFolder = {};
      comps.forEach(function(comp) {
        var f = compFolder(comp);
        if (!(f in byFolder)) { byFolder[f] = []; buckets.push(f); seen[f] = true; }
        byFolder[f].push(comp);
      });
      var regrouped = [];
      buckets.forEach(function(f) {
        byFolder[f].forEach(function(comp) { comp._folder = f; regrouped.push(comp); });
      });
      comps = regrouped;
    })();

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
    /* Each distinct immediate-parent folder starts on its own row band, with a
       clear vertical gap, so folders read as separate horizontal clusters even
       when components wrap. Spacing only — no labels or bands. */
    var FOLDER_GAP_Y = Math.round(COMP_GAP_Y * 1.8);
    var prevFolder = null;
    comps.forEach(function(comp) {
      var laid = layoutComponent(comp);
      var compW = laid.layerKeys.length * cardW + Math.max(0, laid.layerKeys.length - 1) * CARD_GAP_X;
      var compH = Math.max.apply(null, laid.layerKeys.map(function(l){ return columnHeight(laid.layers[l]); }).concat([100]));
      /* new folder → break to a fresh row band with extra vertical separation */
      if (prevFolder !== null && comp._folder !== prevFolder && cursorX > 80) {
        cursorX = 80; cursorY += rowH + FOLDER_GAP_Y; rowH = 0;
      }
      prevFolder = comp._folder;
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
      /* Root badge: show highest rank among functions in this file */
      var fileRootRank = null;
      info.fns.forEach(function(n) { var r = ROOT_RANKS[n.id]; if (r && (fileRootRank === null || r < fileRootRank)) fileRootRank = r; });
      if (fileRootRank === 1) cardsHtml += '<span class="cg-root-badge rank-1">&#x1F451; Root</span>';
      else if (fileRootRank === 2) cardsHtml += '<span class="cg-root-badge rank-2">&#x2605; Root #2</span>';
      else if (fileRootRank === 3) cardsHtml += '<span class="cg-root-badge rank-3">&#x2605; Root #3</span>';
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
    _svEnsureTraceControl();
    _svRestoreCollapsedState();
    /* PERF-8: cache each row's geometry (before virtualising) so off-screen edge
       anchoring stays correct, then virtualise the file cards. Only active when
       VIRT_DOM is on; otherwise this is a cheap no-op and output is unchanged. */
    if (window.CG_VIRT_DOM) {
      var _svCanvasEl = document.getElementById('cg-sv-canvas');
      window._svRowGeom = {};
      if (_svCanvasEl) {
        _svCanvasEl.querySelectorAll('.cg-fn-row[data-nid]').forEach(function(r){
          window._svRowGeom[r.dataset.nid] = { oy: r.offsetTop, oh: r.offsetHeight };
        });
        _cgVirtualize(_svCanvasEl, '.cg-file-card');
      }
    }
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
        if (row) {
          _svSelectFn(row.dataset.nid, false);
          if (window.cgTraceEnabled('script')) _svApplyTrace(row.dataset.nid);
          return;
        }
        var hdr = e.target.closest && e.target.closest('.cg-fc-header');
        if (hdr) { var hCard = hdr.closest('.cg-file-card'); if (hCard) _svSelectCard(hCard.dataset.fp); return; }
        _clearEdgeHighlight(false);
        _svClearTrace();
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
  /* File cards whose center lies inside a graph-space rect (grip-drag bundling) */
  function _svCardsInRect(rx, ry, rw, rh) {
    var out = [];
    var svEl = document.getElementById('cg-script-view');
    if (!svEl) return out;
    svEl.querySelectorAll('.cg-file-card').forEach(function(card){
      var l = parseFloat(card.style.left)||0, t = parseFloat(card.style.top)||0;
      var cx = l + (card.offsetWidth||0)/2, cy = t + (card.offsetHeight||0)/2;
      if (cx >= rx && cx <= rx+rw && cy >= ry && cy <= ry+rh) out.push({card:card, origL:l, origT:t});
    });
    return out;
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

  /* ── Script-mode flow-trace ──────────────────────────────────────────
     Same call graph as Function mode (EDGE_DATA), painted onto the file-card
     function rows + the SVG edges. */
  var _svTraceOrigin = null;
  function _svClearTrace() {
    var svEl = document.getElementById('cg-script-view');
    _svTraceOrigin = null;
    if (!svEl) return;
    svEl.querySelectorAll('.cg-fn-row.cg-trace-lit, .cg-fn-row.cg-trace-dim, .cg-fn-row.cg-trace-merge')
      .forEach(function(el) {
        el.classList.remove('cg-trace-lit', 'cg-trace-dim', 'cg-trace-merge');
        el.style.borderLeft = '';
        el.style.boxShadow = '';
      });
    try { _svDrawEdges(); } catch(e) {}   /* restore edge colours */
  }
  window._svClearTrace = _svClearTrace;
  function _svApplyTrace(originId) {
    var svEl = document.getElementById('cg-script-view');
    if (!svEl) return;
    _svClearTrace();
    var trace = window.cgFlowTraceColors(originId, EDGE_DATA, window.cgTraceDir('script'));
    if (!trace.neighbors.length) return;
    _svTraceOrigin = originId;
    svEl.querySelectorAll('.cg-fn-row[data-nid]').forEach(function(row) {
      var id = row.dataset.nid;
      if (id === originId) return;
      var c = trace.color[id];
      if (c) {
        row.classList.add('cg-trace-lit');
        row.style.borderLeft = '3px solid ' + c;
        row.style.boxShadow = 'inset 0 0 0 1px ' + c;
        if (trace.merge[id]) row.classList.add('cg-trace-merge');
      } else {
        row.classList.add('cg-trace-dim');
      }
    });
    var svg = document.getElementById('cg-sv-edges');
    if (svg) {
      svg.querySelectorAll('.cg-sv-edge[data-eid]').forEach(function(p) {
        var e = _edgeById(p.dataset.eid);
        if (!e) return;
        var frIn = (e.from === originId) || trace.color[e.from];
        var toIn = (e.to === originId) || trace.color[e.to];
        if (frIn && toIn) {
          var col = trace.color[e.to] || trace.color[e.from];
          if (col) { p.style.stroke = col; p.style.opacity = '0.95'; p.style.strokeWidth = '2.5'; }
        } else {
          p.style.opacity = '0.12';
        }
      });
    }
  }
  function _svEnsureTraceControl() {
    var svEl = document.getElementById('cg-script-view');
    if (!svEl || document.getElementById('cg-sv-trace-ctl')) return;
    var ctl = window.cgBuildTraceControl('script',
      function(enabled) { if (!enabled) _svClearTrace(); },
      function(dir) { if (_svTraceOrigin) _svApplyTrace(_svTraceOrigin); });
    ctl.id = 'cg-sv-trace-ctl';
    ctl.style.cssText = 'position:absolute;top:10px;left:10px;z-index:20;'
      + 'background:rgba(20,23,30,0.92);border:1px solid #3d4451;border-radius:6px;'
      + 'padding:6px 9px;box-shadow:0 2px 8px rgba(0,0,0,0.4)';
    svEl.appendChild(ctl);
  }
  window._svEnsureTraceControl = _svEnsureTraceControl;

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
    /* PERF-8: the card height just changed — re-pin its intrinsic size and refresh
       the cached row geometry so off-screen anchoring/fit stay correct. */
    if (window.CG_VIRT_DOM) {
      var _r = _cgVirtRefresh(card);
      if (window._svRowGeom) {
        card.querySelectorAll('.cg-fn-row[data-nid]').forEach(function(rr){
          window._svRowGeom[rr.dataset.nid] = { oy: rr.offsetTop, oh: rr.offsetHeight };
        });
      }
      if (_r && _r.apply) _r.apply();
    }
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
    var ot = row.offsetTop, oh = row.offsetHeight;
    if (!oh && !card.classList.contains('sv-collapsed')) {     /* PERF-8 off-screen fallback */
      var _g = (window._svRowGeom||{})[nid];
      if (_g) { ot = _g.oy; oh = _g.oh; }
    }
    var rowCY = cardT + ot + oh / 2;
    _svPanX = vp.offsetWidth  / 2 - rowCX * _svZoom;
    _svPanY = vp.offsetHeight / 2 - rowCY * _svZoom;
    _svApplyTransform();
  }

  /* ── Variable Flow Mode ─────────────────────────────────────── */
  var _vfPanX = 0, _vfPanY = 0, _vfZoom = 1.0;
  var _vfViewDrag = null;
  var _vfCurrentVar = null;
  var _vfDeadMode = false;
  /* VFI-1: "split by scope" vs "merge by name". Default OFF = exact legacy
   * behaviour (one identity per name). Persisted per browser. */
  var _vfScopeSplit = (function(){ try { return localStorage.getItem('cg-vf-scope-split')==='1'; } catch(e){ return false; } })();
  /* VFI-7: forward (downstream def-use) vs backward (upstream "where from?") flow
   * direction. Default OFF = forward = exact legacy behaviour. Persisted per browser. */
  var _vfBackwardFlow = (function(){ try { return localStorage.getItem('cg-vf-backward-flow')==='1'; } catch(e){ return false; } })();
  var _vfCurrentScope = null;               /* selected scope id in split mode */
  var _VF_SCOPE_SEP = '\u001f';             /* unit separator in composite dropdown keys */
  function _vfScopeId(occ){ return (occ && (occ.scope_id || occ.function_id)) || ''; }
  /* localStorage layout key — composite in split mode so per-scope layouts persist. */
  function _vfLayoutKey(){ return _vfCurrentScope ? (_vfCurrentVar + _VF_SCOPE_SEP + _vfCurrentScope) : (_vfCurrentVar || ''); }
  /* Distinct scope groups for a name key: [{scopeId, label, count, occ}] */
  function _vfScopeGroups(nameKey){
    var occs = VAR_FLOW_DATA[nameKey] || [];
    var order = [], by = {};
    occs.forEach(function(o){
      var sid = _vfScopeId(o);
      if (!by[sid]) { by[sid] = { scopeId: sid, count: 0, occ: o, label: '' }; order.push(sid); }
      by[sid].count++;
    });
    return order.map(function(sid){
      var g = by[sid];
      if (sid === 'global') g.label = 'global';
      else if (sid.indexOf('m:') === 0) g.label = 'member ' + sid.slice(2);
      else if (sid.indexOf('f:') === 0) g.label = 'file ' + sid.slice(2).split('/').pop();
      else if (sid.indexOf('t:') === 0) g.label = 'type ' + sid.slice(2);
      else { var occ = g.occ; g.label = occ.function_name || sid; }
      return g;
    });
  }
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
  /* Nodes whose center lies inside a graph-space rect (for grip-drag bundling) */
  function _vfNodesInRect(rx, ry, rw, rh) {
    var out = [];
    _vfCurrentNodes.forEach(function(nd){
      var el = document.getElementById(nd.id);
      var w = el ? el.offsetWidth : 300, h = el ? el.offsetHeight : 120;
      var cx = nd.x + w/2, cy = nd.y + h/2;
      if (cx >= rx && cx <= rx+rw && cy >= ry && cy <= ry+rh) out.push({id:nd.id, startNX:nd.x, startNY:nd.y});
    });
    return out;
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

  /* Dead-variable category → short badge label + tooltip. */
  function _vfDeadCatLabel(cat) {
    var m = {unused:'Unused', dead_store:'Dead Store', unused_param:'Unused Param',
             dead_alloc:'Dead Alloc', unused_value:'Unused Value'};
    return m[cat] || 'Dead Var';
  }
  /* Build the per-block dead/suppressed badge HTML from an occurrence record. */
  function _vfDeadBadge(occ) {
    if (occ.is_suppressed) {
      var sr = occ.suppress_reason || 'intentionally unused';
      return '<span class="cg-vf-dead-badge dv-suppressed" title="Suppressed: '+esc(sr)+'">Suppressed</span>';
    }
    if (!occ.is_dead) return '';
    var cat  = occ.dead_category || (occ.action==='argument' ? 'unused_param' : 'unused');
    var conf = occ.dead_confidence || 'high';
    var lbl  = _vfDeadCatLabel(cat);
    var rl = (occ.read_lines||[]).length, wl = (occ.write_lines||[]).length;
    var why = 'read '+rl+'\u00d7'
            + (occ.write_lines && occ.write_lines.length ? ' \u00b7 written '+wl+'\u00d7 @ L'+occ.write_lines.join(',L') : ' \u00b7 written '+wl+'\u00d7');
    var tip = lbl + ' (' + conf + ' confidence) \u2014 ' + why
            + (conf==='low' ? '  [best-effort]' : '');
    var lowCls = (conf==='low') ? ' dv-lowconf' : '';
    return '<span class="cg-vf-dead-badge dv-'+esc(cat)+lowCls+'" title="'+esc(tip)+'">'
         + esc(lbl) + '<span class="dv-conf">'+esc(conf.charAt(0).toUpperCase())+'</span></span>';
  }

  function _vfSourceKindExtra(sk) {
    if (sk === 'memory initialization') return 'memset init';
    if (sk === 'memory copy')           return 'memcpy dest';
    if (sk === 'memory copy source')    return 'memcpy src';
    return null;
  }

  /* VF-4: classify each occurrence into a display family for the filter panel. */
  function _vfNodeFamily(occ) {
    var sk = (occ && occ.source_kind) || '';
    if (sk === 'custom_input') return 'lugasi';
    if (sk === 'input_file_connect') return 'connect';
    if (sk === 'member_access') return 'member';
    return 'variable';
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
      /* VF-8: empty query → sort hottest first */
      matches = keys.slice().sort(function(a,b){ return (_VF_HOTSPOT[b]||0) - (_VF_HOTSPOT[a]||0) || a.localeCompare(b); });
    } else {
      matches = keys.filter(function(k){ return k.indexOf(q) !== -1; });
      matches.sort(function(a,b){
        var ai=a.indexOf(q), bi=b.indexOf(q);
        return ai!==bi ? ai-bi : a.localeCompare(b);
      });
    }
    matches = matches.slice(0, 60);
    if (!matches.length) { dd.style.display='none'; return; }
    /* In split-by-scope mode, expand each name into its distinct scope groups. */
    var items = [];
    matches.forEach(function(k){
      var occs = VAR_FLOW_DATA[k];
      if (_vfScopeSplit) {
        var groups = _vfScopeGroups(k);
        if (groups.length > 1) {
          groups.forEach(function(g){
            items.push({ key: k + _VF_SCOPE_SEP + g.scopeId, name: g.occ.name,
                         meta: g.label, count: g.count });
          });
          return;
        }
      }
      var fileSet = {};
      occs.forEach(function(o){ if(o.file_name) fileSet[o.file_name]=1; });
      items.push({ key: k, name: occs[0].name,
                   meta: Object.keys(fileSet).slice(0,3).join(', '), count: occs.length });
    });
    items = items.slice(0, 80);
    if (!items.length) { dd.style.display='none'; return; }
    dd.innerHTML = items.map(function(it){
      var displayName = it.name;
      var hi;
      if (!q) {
        hi = esc(displayName);
      } else {
        var idx = displayName.toLowerCase().indexOf(q);
        hi = idx >= 0
          ? esc(displayName.slice(0,idx))+'<span class="cg-vf-dd-mark">'+esc(displayName.slice(idx,idx+q.length))+'</span>'+esc(displayName.slice(idx+q.length))
          : esc(displayName);
      }
      var cnt = it.count;
      var score = _VF_HOTSPOT[it.key.split(_VF_SCOPE_SEP)[0]] || 0;
      var hotBadge = (score >= _VF_HOTSPOT_P90 && _VF_HOTSPOT_MAX > 4)
        ? '<span class="cg-vf-hot-badge" title="Hotspot: '+score+' score ('+cnt+' refs, '+Math.round(score/4)+' functions)">🔥</span>'
        : '';
      return '<div class="cg-vf-dd-item" data-key="'+esc(it.key)+'">'
           + hotBadge
           + '<span class="cg-vf-dd-name">'+hi+'</span>'
           + '<span class="cg-vf-dd-count" title="'+esc(it.meta)+'">'+cnt+' loc'+(cnt===1?'':'s')+'</span>'
           + (it.meta?'<span style="font-size:10px;color:#6a7a8a;display:block;margin-top:1px">'+esc(it.meta)+'</span>':'')
           + '</div>';
    }).join('');
    dd.querySelectorAll('.cg-vf-dd-item').forEach(function(item){
      item.addEventListener('mousedown', function(e){
        e.preventDefault();
        var k = item.dataset.key;
        var namePart = k.split(_VF_SCOPE_SEP)[0];
        var inp2 = document.getElementById('cg-vf-search-input');
        if (inp2) inp2.value = (VAR_FLOW_DATA[namePart] ? VAR_FLOW_DATA[namePart][0].name : namePart);
        dd.style.display = 'none';
        _vfSelectVar(k);
      });
    });
    dd.style.display = 'block';
  }

  /* Strip cast / address-of / deref / array index but KEEP the .field member
   * path. `&cfg.speed` -> `cfg.speed`. Used so that a custom-input destination
   * written to a struct member (e.g. LUGASI(&cfg.speed, ...)) keeps flowing
   * when `cfg.speed` is later passed by value to a renamed parameter. */
  function _extractFullVarName(expr) {
    if (!expr) return '';
    var s = expr.trim();
    s = s.replace(/^\\([^)]+\\)\\s*/, '');   /* strip cast */
    s = s.replace(/^[&*]+/, '');             /* strip & * */
    s = s.replace(/\\[.*$/, '');             /* strip [idx] */
    s = s.replace(/^[&*]+/, '');          /* strip again after cast removal */
    return s.trim();
  }

  /* Strip address-of / deref / cast / array index to get the base variable name */
  function _extractBaseVarName(expr) {
    var s = _extractFullVarName(expr);
    s = s.replace(/\\.\\w+$/, '');        /* strip trailing .field access */
    return s.trim();
  }

  /*
   * _vfBuildFlowChain(normKey, seedScopeId, direction)
   * BFS from all occurrences of normKey.
   *   direction = 'forward' (default, VFI-2): follow argument→parameter mappings
   *     through EDGE_DATA call sites to discover aliased parameter names in callees
   *     (downstream def-use).
   *   direction = 'backward' (VFI-7): when the tracked variable is a callee
   *     parameter, walk each caller's positional argument expression back to the
   *     originating variable (upstream "where did this come from?"). Edge
   *     orientation stays source→sink; only the traversal direction differs.
   * Returns { entries: [{occ, localName, origName}], flowEdges: [{fromFnId, fromVar, toFnId, toVar, edgeRef}] }
   */
  function _vfBuildFlowChain(normKey, seedScopeId, direction) {
    direction = direction || (_vfBackwardFlow ? 'backward' : 'forward');
    var _vfBackward = (direction === 'backward');
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
    if (seedScopeId != null) {
      rootOccs = rootOccs.filter(function(o){ return _vfScopeId(o) === seedScopeId; });
    }
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

      if (_vfBackward) {
        /* VFI-7 backward: if varName is a parameter of fnId, walk each caller's
         * positional argument expression back to its originating variable. */
        var myParms = nodeParamNames[fnId];
        if (!myParms) continue;
        var pk = -1;
        for (var pi = 0; pi < myParms.length; pi++) {
          if (myParms[pi] && myParms[pi].toLowerCase() === vlow) { pk = pi; break; }
        }
        if (pk < 0) continue;   /* not a parameter — origin reached on this path */
        EDGE_DATA.forEach(function(e) {
          if (e.to !== fnId || !e.args || pk >= e.args.length) return;
          var argExpr = e.args[pk];
          var baseB = _extractBaseVarName(argExpr);
          var fullB = _extractFullVarName(argExpr);
          /* prefer the full member path if it is a tracked variable, else base */
          var srcName = (fullB && VAR_FLOW_DATA[fullB.toLowerCase()]) ? fullB : baseB;
          if (!srcName) return;
          var callerFn = e.from;
          var slow = srcName.toLowerCase();
          var edgeExistsB = flowEdges.some(function(fe) {
            return fe.fromFnId===callerFn && fe.toFnId===fnId &&
                   fe.fromVar.toLowerCase()===slow &&
                   fe.toVar.toLowerCase()===vlow;
          });
          if (!edgeExistsB) {
            flowEdges.push({ fromFnId: callerFn, fromVar: srcName,
                             toFnId: fnId,       toVar: varName, edgeRef: e });
          }
          var toKeyB = callerFn + '::' + slow;
          if (!visited[toKeyB]) {
            addOccs(callerFn, srcName, origName);
            queue.push({ fnId: callerFn, varName: srcName, origName: origName });
          }
        });
        continue;
      }

      EDGE_DATA.forEach(function(e) {
        if (e.from !== fnId || !e.args || !e.args.length) return;
        for (var j = 0; j < e.args.length; j++) {
          var base = _extractBaseVarName(e.args[j]);
          var full = _extractFullVarName(e.args[j]);
          if (base.toLowerCase() !== vlow && full.toLowerCase() !== vlow) continue;
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

    /* VFI-3: Cross-variable assignment edges.
     * After the main BFS, scan every reached entry for same-function assignment links:
     * - upstream: occ.assign_src names another tracked var → add it and emit srcVar→thisVar.
     * - downstream: ASSIGN_DST_INDEX[fnId::thisVar] lists vars assigned FROM thisVar → add
     *   them and emit thisVar→dstVar.
     * One-hop only (we add to entries but do NOT re-queue, so chains don't grow unboundedly). */
    var _assignEdgeAdded = {};
    function _emitAssignEdge(fromFnId, fromVar, toFnId, toVar) {
      var k = fromFnId + '::' + fromVar.toLowerCase() + '→' + toFnId + '::' + toVar.toLowerCase();
      if (_assignEdgeAdded[k]) return;
      _assignEdgeAdded[k] = true;
      flowEdges.push({ fromFnId: fromFnId, fromVar: fromVar,
                       toFnId: toFnId,     toVar: toVar,
                       link_kind: 'cross_var_assign' });
    }
    var _entriesSnap = entries.slice();  /* snapshot before addOccs may grow entries */
    _entriesSnap.forEach(function(en) {
      var fnId2  = en.occ.function_id;
      var varKey2 = String(en.localName || en.occ.name || '').toLowerCase();
      /* upstream: this occurrence was assigned FROM another tracked variable */
      if (en.occ.assign_src) {
        var srcKey = String(en.occ.assign_src).toLowerCase();
        if (VAR_FLOW_DATA[srcKey]) {
          if (!visited[fnId2 + '::' + srcKey]) addOccs(fnId2, srcKey, srcKey);
          _emitAssignEdge(fnId2, srcKey, fnId2, varKey2);
        }
      }
      /* downstream: other tracked vars that were assigned FROM this variable */
      var idxKey2 = fnId2 + '::' + varKey2;
      (ASSIGN_DST_INDEX[idxKey2] || []).forEach(function(item) {
        var dstKey = item.dstKey;
        if (!visited[fnId2 + '::' + dstKey]) addOccs(fnId2, dstKey, dstKey);
        _emitAssignEdge(fnId2, varKey2, fnId2, dstKey);
      });
    });

    return { entries: entries, flowEdges: flowEdges };
  }

  function _vfSelectVar(rawKey) {
    /* rawKey may be a plain name key, or a composite "name<sep>scopeId"
     * produced by the split-by-scope dropdown. */
    var normKey = rawKey, scopeId = null;
    var sep = rawKey.indexOf(_VF_SCOPE_SEP);
    if (sep !== -1) { normKey = rawKey.slice(0, sep); scopeId = rawKey.slice(sep + 1); }
    _vfCurrentVar = normKey;
    _vfCurrentScope = scopeId;
    /* Restore saved layout for this variable (or start with empty overrides) */
    _vfNodeOverrides = {};
    var _layoutKey = _vfLayoutKey();
    try { var _vfRaw = localStorage.getItem(VF_LAYOUT_PFX + _layoutKey); if (_vfRaw) _vfNodeOverrides = JSON.parse(_vfRaw); } catch(e) {}
    _vfSelectedEdgeIdx = null;
    var popup = document.getElementById('cg-edge-popup');
    if (popup) popup.style.display = 'none';
    if (!VAR_FLOW_DATA[normKey] || !VAR_FLOW_DATA[normKey].length) return;
    /* VF-4: leaving aggregate family view — clear the action-button highlight. */
    document.querySelectorAll('.cg-vf-fam-show-btn.active').forEach(function(b){ b.classList.remove('active'); });
    var ph = document.getElementById('cg-vf-placeholder');
    if (ph) ph.style.display = 'none';
    var chain = _vfBuildFlowChain(normKey, scopeId);
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
      var isSuppressed = !!(occ.is_suppressed);
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

      var deadBadge = _vfDeadBadge(occ);
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
          ? '<div class="cg-vf-info-row"><span class="cg-vf-info-label">Input</span>'
            +'<span class="cg-vf-info-val cg-vf-var-name">'+esc(occ.connect_input_name)+'</span></div>'
            +(occ.custom_input_func ? '<div class="cg-vf-info-row"><span class="cg-vf-info-label">Func</span>'
            +'<span class="cg-vf-info-val"><span class="cg-hl-fn">'+esc(occ.custom_input_func)+'</span></span></div>' : '')
          : '');

      /* member_access: show parent object (the access expression itself is
         already shown in full by the unified SOURCE row below). */
      var memberRows = '';
      if (isMemberAccess) {
        if (occ.parent_name) {
          memberRows += '<div class="cg-vf-info-row"><span class="cg-vf-info-label">Parent</span>'
            + '<span class="cg-vf-info-val cg-vf-var-name" title="'+esc(occ.parent_name)+'">'+esc(occ.parent_name)+'</span></div>';
        }
      }

      var el = document.createElement('div');
      el.className = 'cg-vf-node'
        + (isDead ? ' cg-vf-dead' : '')
        + (isSuppressed ? ' cg-vf-suppressed' : '')
        + (isConnect ? ' cg-vf-connect-input' : '')
        + (isCustomInput ? ' cg-vf-custom-input' : '')
        + (isMemberAccess ? ' cg-vf-member-access' : '');
      el.id = nd.id;
      el.style.cssText = 'left:'+nd.x+'px;top:'+nd.y+'px;width:'+nd.w+'px;position:absolute';
      el.dataset.idx = nd.idx;
      el.dataset.notekey = noteKey;
      el.dataset.family = _vfNodeFamily(occ);
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
            +'<span class="cg-vf-info-val cg-vf-type-val">'+_cgHiType(typeTxt)+'</span>'
          +'</div>'
          +(function(){
            if (!window.cgTypeFindByName || !window.cgTypeFindByName(typeTxt)) return '';
            return '<div class="cg-vf-info-row"><span class="cg-vf-info-label">Type node</span><button class="cg-vf-type-chip" data-tt="'+esc(typeTxt)+'" title="Open this type in Type Nodes Mode">open</button></div>';
          })()
          +'<div class="cg-vf-info-row">'
            +'<span class="cg-vf-info-label">Func</span>'
            +'<span class="cg-vf-info-val cg-vf-fn-val" title="'+esc(occ.function_name)+'"><span class="cg-hl-fn">'+esc(occ.function_name)+'</span></span>'
          +'</div>'
          +'<div class="cg-vf-info-row">'
            +'<span class="cg-vf-info-label">File</span>'
            +'<span class="cg-vf-info-val cg-vf-file-val">'+ln+'</span>'
          +'</div>'
          +(function(){ var _src = occ.full_source || occ.value; return _src
            ? '<div class="cg-vf-info-row cg-vf-source-row">'
                +'<span class="cg-vf-info-label">Source</span>'
                +'<span class="cg-vf-info-val cg-vf-source-val" title="'+esc(_src)+'">'+_vfHiSourceText(_src, occ)+'</span>'
              +'</div>'
            : ''; })()
          +connectRow+inputNameRow+memberRows
        +'</div>'
        +(occ.doc_comment ? '<div class="cg-vf-doc-chip" title="' + esc(occ.doc_comment) + '">💬 ' + esc(occ.doc_comment) + '</div>' : '');

      Array.prototype.forEach.call(el.querySelectorAll('.cg-vf-type-chip'), function(chip){
        chip.addEventListener('click', function(e){
          e.preventDefault();
          e.stopPropagation();
          var tt = chip.getAttribute('data-tt') || '';
          if (window.cgTypeJumpFromName) window.cgTypeJumpFromName(tt);
        });
      });

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

    /* PERF-8: virtualise VF node cards (edges below read each node's own
       offsetHeight, which stays correct via the pinned intrinsic-size). */
    _cgVirtualize(canvas, '.cg-vf-node');

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
      var isAssign = (ce.link_kind === 'cross_var_assign');
      var edgeType = isAssign ? 'assign' : 'chain';
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
      /* Cross-var assign edges bypass the custom_input→connect constraint since
       * they represent variable-to-variable data flow, not call-chain flow. */
      if (!isAssign && !_vfAllowEdge(sourceItem, targetItem)) return;
      if (!sourceItem || !targetItem) return;
      pushEdge('vfn_'+sourceItem.idx, 'vfn_'+targetItem.idx, edgeType);
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

    /* VF-4: apply family filter visibility after building nodes */
    _vfApplyFamilyFilter();

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

    /* VF-4: collect hidden nodes so we can skip their edges. */
    var _hiddenNodes = {};
    nodes.forEach(function(nd) {
      var el2 = document.getElementById(nd.id);
      if (el2 && el2.style.display === 'none') _hiddenNodes[nd.id] = true;
    });

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

    var _isUpstreamHL = (_vfHighlightDirection === 'upstream');

    edges.forEach(function(e) {
      var k = e.from+'>'+e.to; if (seen[k]) return; seen[k] = true;
      /* VF-4: skip edges involving hidden-family nodes */
      if (_hiddenNodes[e.from] || _hiddenNodes[e.to]) return;
      var d = _vfEdgeGeometry(e, nodeMap);
      if (!d) return;

      var typeClass = e.type==='same'   ? 'cg-vf-edge-same'
                    : e.type==='assign' ? 'cg-vf-edge-assign'
                    :                     'cg-vf-edge-call';
      var css = 'cg-vf-edge ' + typeClass;
      /* Highlight the single selected edge (yellow, matching Function-mode edge-click style) */
      if (_vfSelectedEdgeIdx !== null && eidxCounter === _vfSelectedEdgeIdx) {
        css += ' vf-edge-selected';
      }
      var mid = 'url(#vf-arr-' + (e.type==='same' ? 'same' : e.type==='assign' ? 'assign' : 'call') + ')';
      var styleAttr = '';
      /* When a branch highlight is active, colour edges by the branch of the
       * node they flow into (downstream) or flow from (upstream). */
      if (_vfBranchActive) {
        if (_isUpstreamHL) {
          /* VF-6 upstream: edge colour follows the source (from) node's colour.
           * Edge is in-subgraph when its source is coloured AND its target is
           * either the origin or also a coloured ancestor. */
          var fcol = _vfBranchNodeColor[e.from];
          var inSub = !!fcol && (e.to === _vfBranchOriginId || !!_vfBranchNodeColor[e.to]);
          if (inSub) {
            styleAttr = ' style="stroke:'+fcol+';opacity:1;stroke-width:2.5"';
            mid = 'url(#' + _vfMarkerForColor(fcol) + ')';
          } else {
            styleAttr = ' style="opacity:0.12"';
          }
        } else {
          /* VF-2 downstream: original logic */
          var tcol = _vfBranchNodeColor[e.to];
          var inSub = !!tcol && (e.from === _vfBranchOriginId || !!_vfBranchNodeColor[e.from]);
          if (inSub) {
            styleAttr = ' style="stroke:'+tcol+';opacity:1;stroke-width:2.5"';
            mid = 'url(#' + _vfMarkerForColor(tcol) + ')';
          } else {
            styleAttr = ' style="opacity:0.12"';
          }
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
      +'<marker id="vf-arr-assign" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">'
      +'<path d="M0,1 L7,4 L0,7 Z" fill="#e67e22"/></marker>'
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
    var typeLabel = meta.type === 'same'   ? 'intra-function sequence'
                  : meta.type === 'chain'  ? 'data-flow (variable renamed)'
                  : meta.type === 'assign' ? 'cross-variable assignment'
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
    /* Flow-trace disabled → just select the block (lets the user drag freely). */
    if (!window.cgTraceEnabled('varflow')) {
      if (_vfBranchActive) _vfClearBranchHighlight();
      return;
    }
    /* VF-2: clicking the current branch origin again clears the highlight;
     * clicking any other node lights up its downstream flow per-branch. */
    if (_vfBranchActive && _vfBranchOriginId === nodeId) {
      _vfClearBranchHighlight();
    } else {
      _vfApplyBranchHighlight(nodeId);
    }
  }

  /* VF enable toggle (mirrors the cross-mode trace checkbox). */
  function _vfToggleTraceEnabled(on) {
    window.cgTraceSetEnabled('varflow', on);
    if (!on && _vfBranchActive) _vfClearBranchHighlight();
  }
  window._vfToggleTraceEnabled = _vfToggleTraceEnabled;

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

    var isUpstream = (_vfHighlightDirection === 'upstream');

    /* Shared cross-mode engine computes per-branch colours + merge flags.
     * (VF-2 downstream / VF-6 upstream — behaviour-identical to the previous
     * inline implementation, now reused by every other mode too.) */
    var trace = window.cgFlowTraceColors(originId, _vfCurrentEdges, _vfHighlightDirection);
    var neighbors = trace.neighbors;
    if (!neighbors.length) {
      /* Leaf/root node — nothing in the selected direction. Just select it. */
      _vfClearBranchHighlight();
      var le = document.getElementById(originId);
      if (le) le.classList.add('vf-selected');
      return;
    }

    var colorOf = trace.color;
    var merge   = trace.merge;
    var nBranch = trace.nBranch;

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

    _vfRenderBranchLegend(neighbors, nBranch, isUpstream);
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

  function _vfRenderBranchLegend(children, nBranch, isUpstream) {
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
    var title = isUpstream
      ? 'Upstream sources (' + nBranch + ')'
      : 'Flow branches (' + nBranch + ')';
    var hint = isUpstream
      ? 'Each colour is one upstream source path. Dashed = confluence point (merges from multiple sources). Deeper chains shade the ancestor colour.'
      : 'Each colour is one downstream path. Dashed = merge point (shared by paths). Deeper splits shade the parent colour.';
    lg.innerHTML =
      '<div class="cg-vf-legend-title">' + title + '</div>'
      + rows
      + '<div class="cg-vf-legend-hint">' + hint + '</div>'
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
    /* Unified source line: prefer the full statement, fall back to the RHS
       value, then the short context snippet. Shown once as a "Source" section. */
    var srcLine = occ.full_source || occ.value || (occ.snippet ? occ.snippet.trim() : '');
    var valueRow = '';
    var snippetSec = srcLine
      ? '<div class="cg-vf-modal-section"><div class="cg-vf-modal-section-title">Source</div><div class="cg-vf-modal-code">'+_vfHiSourceText(srcLine, occ)+'</div></div>'
      : '';
    var localName = occ._localName || occ.name;
    var origName  = occ._origName  || occ.name;
    var showOrig  = localName.toLowerCase() !== origName.toLowerCase();
    var titleEl = document.getElementById('cg-vf-modal-title');
    var bodyEl  = document.getElementById('cg-vf-modal-body');
    if (titleEl) titleEl.innerHTML = showOrig
      ? '<span class="cg-hl-var">' + esc(localName) + '</span> — originally “<span class="cg-hl-var">' + esc(origName) + '</span>”'
      : '<span class="cg-hl-var">' + esc(localName) + '</span>';
    var origRow = showOrig
      ? '<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Tracked as</span><span class="cg-vf-modal-value mono"><span class="cg-hl-var">'+esc(origName)+'</span></span></div>'
      : '';
    if (bodyEl) bodyEl.innerHTML =
      '<div class="cg-vf-modal-section">'
        +'<div class="cg-vf-modal-section-title">Identity</div>'
        +'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Category</span><span class="cg-vf-modal-value"><span class="cg-vf-cat-badge cg-vfc-'+esc(cat)+'">'+esc(catLbl)+'</span></span></div>'
        +'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Variable</span><span class="cg-vf-modal-value mono"><span class="cg-hl-var">'+esc(localName)+'</span></span></div>'
        +origRow
        +'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Type</span><span class="cg-vf-modal-value mono">'+_cgHiType(typeTxt)+'</span></div>'
      +'</div>'
      +'<div class="cg-vf-modal-section">'
        +'<div class="cg-vf-modal-section-title">Location</div>'
        +'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Function</span><span class="cg-vf-modal-value mono"><span class="cg-hl-fn">'+esc(occ.function_name)+'</span></span></div>'
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
        +(occ.connect_input_name?'<div class="cg-vf-modal-row"><span class="cg-vf-modal-label">Input var</span><span class="cg-vf-modal-value mono"><span class="cg-hl-var">'+esc(occ.connect_input_name)+'</span></span></div>':'')
        +'</div>' : '')
      +snippetSec
      +(occ.doc_comment ? '<div class="cg-vf-modal-section" style="border-left:3px solid #5a8a4a">'
        +'<div class="cg-vf-modal-section-title" style="color:#7abc5a">💬 Source Comment</div>'
        +'<div class="cg-vf-modal-action-desc" style="white-space:pre-wrap;font-style:italic">'+esc(occ.doc_comment)+'</div>'
        +'</div>' : '');
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
    try { localStorage.removeItem(VF_LAYOUT_PFX + _vfLayoutKey()); } catch(e) {}
    _vfSelectedEdgeIdx = null;
    var popup = document.getElementById('cg-edge-popup');
    if (popup) popup.style.display = 'none';
    if (_vfCurrentVar && VAR_FLOW_DATA[_vfCurrentVar]) {
      var chain = _vfBuildFlowChain(_vfCurrentVar, _vfCurrentScope);
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
    /* Split-by-scope expansion (VFI-1) */
    var items=[];
    matches.forEach(function(k){
      if (_vfScopeSplit) {
        var groups=_vfScopeGroups(k);
        if (groups.length>1) {
          groups.forEach(function(g){
            items.push({ vkey:k+_VF_SCOPE_SEP+g.scopeId, name:g.occ.name, meta:g.label, count:g.count });
          });
          return;
        }
      }
      items.push({ vkey:k, name:VAR_FLOW_DATA[k][0].name, meta:'', count:VAR_FLOW_DATA[k].length });
    });
    items=items.slice(0,60);
    dd.innerHTML=items.map(function(it){
      var displayName=it.name;
      var hi;
      if (!norm) {
        hi=esc(displayName);
      } else {
        var idx=displayName.toLowerCase().indexOf(norm);
        hi=idx>=0
          ?esc(displayName.slice(0,idx))+'<span class="cg-sd-mark">'+esc(displayName.slice(idx,idx+norm.length))+'</span>'+esc(displayName.slice(idx+norm.length))
          :esc(displayName);
      }
      var metaTxt=(it.meta?esc(it.meta)+' · ':'')+it.count+' occurrence'+(it.count===1?'':'s');
      return '<div class="cg-sd-item" data-vkey="'+esc(it.vkey)+'">'
        +'<div class="cg-sd-name">'+hi+'</div>'
        +'<div class="cg-sd-meta">'+metaTxt+'</div>'
        +'</div>';
    }).join('');
    dd.querySelectorAll('.cg-sd-item').forEach(function(item){
      item.addEventListener('mousedown',function(e){
        e.preventDefault();
        var vkey=item.dataset.vkey;
        var namePart=vkey.split(_VF_SCOPE_SEP)[0];
        var occs=VAR_FLOW_DATA[namePart];
        dd.style.display='none';
        if (inp&&occs) inp.value=occs[0].name;
        var vfInp=document.getElementById('cg-vf-search-input');
        if (vfInp&&occs) vfInp.value=occs[0].name;
        if (searchHint&&occs) searchHint.textContent=occs.length+' occurrence'+(occs.length===1?'':'s');
        _vfSelectVar(vkey);
      });
    });
    dd.style.display='block';
  }

  /* VF-4: show/hide nodes by family; refresh edges to exclude hidden endpoints. */
  function _vfApplyFamilyFilter() {
    var hiddenIds = {};
    _vfCurrentNodes.forEach(function(nd) {
      var el = document.getElementById(nd.id);
      if (!el) return;
      var fam = el.dataset.family || 'variable';
      var visible = !!_vfFamilyFilter[fam];
      el.style.display = visible ? '' : 'none';
      if (!visible) hiddenIds[nd.id] = true;
    });
    /* Refresh edges so hidden-endpoint edges disappear */
    _vfDrawEdges(_vfCurrentEdges, _vfCurrentNodes);
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
    /* Reflect persisted split-by-scope state on the toolbar button (VFI-1). */
    var scopeBtn=document.getElementById('cg-vf-scope-btn');
    if(scopeBtn) scopeBtn.classList.toggle('active', _vfScopeSplit);
    /* Reflect persisted flow-direction state on the toolbar button (VFI-7). */
    var backwardBtn=document.getElementById('cg-vf-backward-btn');
    if(backwardBtn){
      backwardBtn.classList.toggle('active', _vfBackwardFlow);
      backwardBtn.textContent = _vfBackwardFlow ? '\u2b9c Backward' : '\u2b9e Forward';
    }
    /* VF-6: reflect persisted highlight-direction state. */
    var hldirBtn=document.getElementById('cg-vf-hldir-btn');
    if(hldirBtn){
      var _isUpHL = (_vfHighlightDirection === 'upstream');
      hldirBtn.classList.toggle('active', _isUpHL);
      hldirBtn.textContent = _isUpHL ? '\u2b9d Upstream' : '\u2b9f Downstream';
    }
    /* Reflect persisted flow-trace enable flag on the VF checkbox. */
    var _vfTraceCb = document.getElementById('cg-vf-trace-cb');
    if (_vfTraceCb) _vfTraceCb.checked = window.cgTraceEnabled('varflow');
    /* VF-4: reflect persisted family filter state on the pill buttons. */
    document.querySelectorAll('.cg-vf-fam-btn').forEach(function(btn) {
      var fam = btn.dataset.fam;
      if (fam) btn.classList.toggle('active', !!_vfFamilyFilter[fam]);
    });
    /* VF-4: derive the custom-input ("lugasi") show-button label from the
     * function names the C parser actually emitted, so renaming those custom
     * functions in the parser updates the UI automatically — the renamed name
     * is never hard-coded in the renderer. The button is hidden when the
     * project has no custom-input blocks at all. */
    var _ciNames = _vfFamilyFuncNames('custom_input');
    var _ciBtn = document.querySelector('.cg-vf-fam-show-btn[data-fam="lugasi"]');
    if (_ciBtn) {
      var _ciLbl = _ciNames.length ? _ciNames.join(' / ') : 'Custom input';
      var _ciSpan = _ciBtn.querySelector('.cg-vf-fam-show-lbl');
      if (_ciSpan) _ciSpan.textContent = _ciLbl;
      _ciBtn.title = 'Show every ' + _ciLbl + ' source block and its downstream flow';
      _ciBtn.style.display = _ciNames.length ? '' : 'none';
    }
    /* Connect button: prefer the connect2-style free-function names when present,
     * otherwise the generic ".connect" method label. Hidden when no connect blocks. */
    var _coNames = _vfFamilyFuncNames('input_file_connect');
    var _coHasAny = (function(){
      for (var i=0;i<_VF_KEYS.length;i++){
        var occs=VAR_FLOW_DATA[_VF_KEYS[i]]||[];
        for (var j=0;j<occs.length;j++){ if((occs[j].source_kind||'')==='input_file_connect') return true; }
      }
      return false;
    })();
    var _coBtn = document.querySelector('.cg-vf-fam-show-btn[data-fam="connect"]');
    if (_coBtn) {
      var _coSpan = _coBtn.querySelector('.cg-vf-fam-show-lbl');
      var _coLbl = _coNames.length ? _coNames.join(' / ') : '.connect';
      if (_coSpan) _coSpan.textContent = _coLbl;
      _coBtn.title = 'Show every ' + _coLbl + ' input block and its downstream flow';
      _coBtn.style.display = _coHasAny ? '' : 'none';
    }
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

  window._vfToggleScopeSplit = function() {
    _vfScopeSplit = !_vfScopeSplit;
    try { localStorage.setItem('cg-vf-scope-split', _vfScopeSplit ? '1' : '0'); } catch(e) {}
    var btn = document.getElementById('cg-vf-scope-btn');
    if (btn) btn.classList.toggle('active', _vfScopeSplit);
    /* Re-render the current selection under the new grouping for instant feedback. */
    if (_vfCurrentVar && VAR_FLOW_DATA[_vfCurrentVar]) {
      if (_vfScopeSplit) {
        var groups = _vfScopeGroups(_vfCurrentVar);
        var sid = null;
        if (_vfCurrentScope && groups.some(function(g){ return g.scopeId === _vfCurrentScope; })) sid = _vfCurrentScope;
        else if (groups.length) sid = groups[0].scopeId;
        _vfSelectVar(sid != null ? _vfCurrentVar + _VF_SCOPE_SEP + sid : _vfCurrentVar);
      } else {
        _vfCurrentScope = null;
        _vfSelectVar(_vfCurrentVar);
      }
    }
  };

  window._vfToggleBackwardFlow = function() {
    _vfBackwardFlow = !_vfBackwardFlow;
    try { localStorage.setItem('cg-vf-backward-flow', _vfBackwardFlow ? '1' : '0'); } catch(e) {}
    var btn = document.getElementById('cg-vf-backward-btn');
    if (btn) {
      btn.classList.toggle('active', _vfBackwardFlow);
      btn.textContent = _vfBackwardFlow ? '\u2b9c Backward' : '\u2b9e Forward';
    }
    /* Re-render the current selection under the new direction for instant feedback. */
    if (_vfCurrentVar && VAR_FLOW_DATA[_vfCurrentVar]) {
      _vfSelectVar(_vfCurrentScope != null ? _vfCurrentVar + _VF_SCOPE_SEP + _vfCurrentScope : _vfCurrentVar);
    }
  };


  /* VF-6: toggle click-highlight direction (downstream ↔ upstream). */
  window._vfToggleHighlightDirection = function() {
    _vfHighlightDirection = (_vfHighlightDirection === 'upstream') ? 'downstream' : 'upstream';
    try { localStorage.setItem('cg-vf-highlight-dir', _vfHighlightDirection); } catch(e) {}
    var btn = document.getElementById('cg-vf-hldir-btn');
    if (btn) {
      var isUp = (_vfHighlightDirection === 'upstream');
      btn.classList.toggle('active', isUp);
      btn.textContent = isUp ? '\u2b9d Upstream' : '\u2b9f Downstream';
    }
    /* Re-apply highlight in new direction if one is active */
    if (_vfBranchActive && _vfBranchOriginId) {
      _vfApplyBranchHighlight(_vfBranchOriginId);
    }
  };

  /* VF-4: toggle visibility of a node family (Variable / Member only — the
   * LUGASI/.connect families are driven by the "All flows" action buttons,
   * see _vfShowFamily). */
  window._vfToggleFamilyFilter = function(fam) {
    _vfFamilyFilter[fam] = !_vfFamilyFilter[fam];
    try { localStorage.setItem('cg-vf-family-filter', JSON.stringify(_vfFamilyFilter)); } catch(e) {}
    var btn = document.querySelector('.cg-vf-fam-btn[data-fam="' + fam + '"]');
    if (btn) btn.classList.toggle('active', _vfFamilyFilter[fam]);
    _vfApplyFamilyFilter();
  };

  /* VF-4: distinct parser-emitted function names for a source family. Used to
   * label the "All flows" buttons without baking the custom function name into
   * the renderer (the names come straight from the analysed code). */
  function _vfFamilyFuncNames(targetKind) {
    var seen = {}, order = [];
    _VF_KEYS.forEach(function(k){
      (VAR_FLOW_DATA[k] || []).forEach(function(occ){
        if ((occ.source_kind || '') === targetKind && occ.custom_input_func && !seen[occ.custom_input_func]) {
          seen[occ.custom_input_func] = true; order.push(occ.custom_input_func);
        }
      });
    });
    return order;
  }

  /* VF-4: show EVERY block of a source family together with its downstream flow,
   * aggregated into one graph. fam: 'lugasi' (custom_input) | 'connect' (input_file_connect). */
  window._vfShowFamily = function(fam) {
    var targetKind = (fam === 'connect') ? 'input_file_connect' : 'custom_input';
    var seedKeys = {};
    _VF_KEYS.forEach(function(k){
      (VAR_FLOW_DATA[k] || []).forEach(function(occ){
        if ((occ.source_kind || '') === targetKind) seedKeys[k] = true;
      });
    });
    var keys = Object.keys(seedKeys);
    var ph = document.getElementById('cg-vf-placeholder');
    var canvas = document.getElementById('cg-vf-canvas');
    if (!keys.length) {
      var none = (fam === 'connect')
        ? (_vfFamilyFuncNames('input_file_connect').join(' / ') || '.connect')
        : (_vfFamilyFuncNames('custom_input').join(' / ') || 'custom input');
      if (canvas) canvas.innerHTML = '<svg id="cg-vf-svg" width="1" height="1" style="position:absolute;top:0;left:0;overflow:visible;pointer-events:none"></svg>';
      if (ph) { ph.textContent = 'No ' + none + ' blocks found in this project.'; ph.style.display = ''; }
      return;
    }
    if (ph) ph.style.display = 'none';
    /* Leave dead mode if it was on. */
    if (_vfDeadMode) {
      _vfDeadMode = false;
      var dbtn = document.getElementById('cg-vf-dead-btn');
      if (dbtn) dbtn.classList.remove('active');
    }
    /* Aggregate mode — no single current variable. */
    _vfCurrentVar = null; _vfCurrentScope = null; _vfNodeOverrides = {};
    _vfSelectedEdgeIdx = null;
    var allEntries = [], allEdges = [], seenEntry = {};
    keys.forEach(function(k){
      var chain = _vfBuildFlowChain(k, null);   /* respects the Forward/Backward toggle */
      chain.entries.forEach(function(en){
        var o = en.occ;
        var ek = (o.function_id||'') + '|' + (o.line||'') + '|' + (o.name||'') + '|' + (o.source_kind||'');
        if (seenEntry[ek]) return;
        seenEntry[ek] = true;
        allEntries.push(en);
      });
      chain.flowEdges.forEach(function(e){ allEdges.push(e); });
    });
    _vfCurrentChainEdges = allEdges;
    _vfBuildGraph(allEntries, allEdges);
    /* Mark which family action is active. */
    document.querySelectorAll('.cg-vf-fam-show-btn').forEach(function(b){
      b.classList.toggle('active', b.dataset.fam === fam);
    });
  };

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
        var chain = _vfBuildFlowChain(_vfCurrentVar, _vfCurrentScope);
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
    /* VF-4: leaving aggregate family view — clear the action-button highlight. */
    document.querySelectorAll('.cg-vf-fam-show-btn.active').forEach(function(b){ b.classList.remove('active'); });
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

    /* group by dead_category */
    var ORDER = ['unused','dead_store','unused_param','dead_alloc','unused_value'];
    var groups = {};
    dead.forEach(function(occ){
      var c = occ.dead_category || (occ.action==='argument' ? 'unused_param' : 'unused');
      (groups[c] = groups[c] || []).push(occ);
    });
    var h = '';
    if (!dead.length) {
      h += '<div style="padding:18px;color:#5a6a7a;font-size:12px">No dead variables detected.</div>';
    } else {
      ORDER.forEach(function(cat){
        var rows = groups[cat];
        if (!rows || !rows.length) return;
        h += '<div class="cg-dead-cat-hdr"><span class="cg-vf-dead-badge dv-'+esc(cat)
           + '">'+esc(_vfDeadCatLabel(cat))+'</span><span class="cg-dead-cat-n">'+rows.length+'</span></div>';
        h += '<div class="cg-dead-hdr">'
          +'<span>Variable</span><span>Function</span><span>Line</span>'
          +'<span>Conf</span><span>Why</span></div>';
        rows.forEach(function(occ){
          var conf = occ.dead_confidence || 'high';
          var rl = (occ.read_lines||[]).length, wl = (occ.write_lines||[]).length;
          var why = 'read '+rl+'\u00d7 \u00b7 written '+wl+'\u00d7'
                  + (occ.write_lines && occ.write_lines.length ? ' @ L'+occ.write_lines.join(',L') : '');
          h += '<div class="cg-dead-row">'
            +'<span class="cg-dead-name">'+esc(occ.name)+'</span>'
            +'<span class="cg-dead-fn" title="'+esc(occ.function_name)+'">'+esc(occ.function_name)+'</span>'
            +'<span class="cg-dead-line">'+esc((occ.file_name||'')+(occ.line?':'+occ.line:''))+'</span>'
            +'<span class="cg-dead-conf cg-dead-conf-'+esc(conf)+'">'+esc(conf)
              +(conf==='low'?' \u2248':'')+'</span>'
            +'<span class="cg-dead-why" title="'+esc(why)+'">'+esc(why)+'</span>'
            +'</div>';
        });
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
    var params = dead.filter(function(o){ return o.dead_category==='unused_param' || o.action==='argument' || o.dead_reason==='unused parameter'; });
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
        var chain = _vfBuildFlowChain(_vfCurrentVar, _vfCurrentScope);
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
        var chain = _vfBuildFlowChain(_vfCurrentVar, _vfCurrentScope);
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
    var canvas = document.getElementById('cg-vf-canvas');
    if (canvas) canvas.classList.toggle('annot-mode-active', _vfAnnotMode);
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
      +'<div class="cg-vf-annot-grip" title="Drag annotation (+ nodes inside)"></div>'
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
    /* Drag to move — fires from grip (outside annot-mode) or body (in annot-mode);
       body is click-through to nodes underneath, grip stays grabbable */
    el.addEventListener('mousedown', function(e){
      if (e.target.classList.contains('cg-vf-annot-del') || e.target.classList.contains('cg-vf-annot-resize')) return;
      if (e.button !== 0) return;
      e.stopPropagation();
      _vfAnnotDrag = {idx:idx, el:el, startMX:e.clientX, startMY:e.clientY, startX:a.x, startY:a.y,
                      moved:false, nodes:_vfNodesInRect(a.x, a.y, a.w, a.h)};
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
          var dx=e.clientX-_vfAnnotDrag.startMX, dy=e.clientY-_vfAnnotDrag.startMY;
          if (!_vfAnnotDrag.moved && (Math.abs(dx)>4||Math.abs(dy)>4)) _vfAnnotDrag.moved=true;
          if (_vfAnnotDrag.moved) {
          var gdx=(e.clientX-_vfAnnotDrag.startMX)/_vfZoom, gdy=(e.clientY-_vfAnnotDrag.startMY)/_vfZoom;
          var annots = _vfAnnotsLoad();
          if (annots[_vfAnnotDrag.idx]) {
            annots[_vfAnnotDrag.idx].x = _vfAnnotDrag.startX + gdx;
            annots[_vfAnnotDrag.idx].y = _vfAnnotDrag.startY + gdy;
            _vfAnnotsSave(annots);
            _vfRenderAnnots();
          }
          var nlist = _vfAnnotDrag.nodes || [];
          if (nlist.length) {
            nlist.forEach(function(it){
              var nx=it.startNX+gdx, ny=it.startNY+gdy;
              var ne=document.getElementById(it.id);
              if(ne){ne.style.left=nx+'px';ne.style.top=ny+'px';}
              _vfNodeOverrides[it.id]={x:nx,y:ny};
              _vfCurrentNodes.forEach(function(nd){if(nd.id===it.id){nd.x=nx;nd.y=ny;}});
            });
            _vfDrawEdges(_vfRebuildEdges(),_vfCurrentNodes);
          }
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
        if (_vfAnnotDrag) {
          if (_vfAnnotDrag.moved && _vfAnnotDrag.nodes && _vfAnnotDrag.nodes.length) {
            _vfSelectedEdgeIdx=null; _vfDrawEdges(_vfRebuildEdges(),_vfCurrentNodes);
          }
          _vfAnnotDrag=null;
          return;
        }
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
    var canvas = document.getElementById('cg-sv-canvas');
    if (canvas) canvas.classList.toggle('annot-mode-active', _svAnnotMode);
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
      +'<div class="cg-vf-annot-grip" title="Drag annotation (+ cards inside)"></div>'
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
      if (e.button !== 0) return;
      e.stopPropagation();
      _svAnnotDrag = {idx:idx, el:el, startMX:e.clientX, startMY:e.clientY, startX:a.x, startY:a.y,
                      moved:false, cards:_svCardsInRect(a.x, a.y, a.w||120, a.h||60)};
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
        var dx=e.clientX-_svAnnotDrag.startMX, dy=e.clientY-_svAnnotDrag.startMY;
        if (!_svAnnotDrag.moved && (Math.abs(dx)>4||Math.abs(dy)>4)) _svAnnotDrag.moved=true;
        if (_svAnnotDrag.moved) {
        var gdx=(e.clientX-_svAnnotDrag.startMX)/_svZoom, gdy=(e.clientY-_svAnnotDrag.startMY)/_svZoom;
        var ann = _svAnnotsLoad();
        if (ann[_svAnnotDrag.idx]) {
          ann[_svAnnotDrag.idx].x = _svAnnotDrag.startX + gdx;
          ann[_svAnnotDrag.idx].y = _svAnnotDrag.startY + gdy;
          _svAnnotsSave(ann); _svRenderAnnots();
        }
        var clist = _svAnnotDrag.cards || [];
        if (clist.length) {
          clist.forEach(function(it){
            it.card.style.left = (it.origL+gdx)+'px';
            it.card.style.top  = (it.origT+gdy)+'px';
          });
          _svDrawEdges();
        }
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
      if (_svAnnotDrag) {
        if (_svAnnotDrag.moved && _svAnnotDrag.cards && _svAnnotDrag.cards.length) _svDrawEdges();
        _svAnnotDrag=null;
        return;
      }
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
    if (layer) {
      layer.style.pointerEvents = _fnAnnotMode ? 'all' : 'none';
      layer.classList.toggle('annot-mode-active', _fnAnnotMode);
    }
  };
  /* vis.js nodes whose center lies inside a graph-space rect (grip-drag bundling) */
  function _fnNodesInRect(rx, ry, rw, rh) {
    var out = [];
    var net = getNet(); if (!net) return out;
    try {
      var pos = net.getPositions();
      Object.keys(pos).forEach(function(id){
        var p = pos[id];
        if (p.x >= rx && p.x <= rx+rw && p.y >= ry && p.y <= ry+rh) out.push({id:id, startNX:p.x, startNY:p.y});
      });
    } catch(e) {}
    return out;
  }
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
      +'<div class="cg-vf-annot-grip" title="Drag annotation (+ nodes inside)"></div>'
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
      if (e.button !== 0) return;
      e.stopPropagation();
      _fnAnnotDrag = {idx:idx, el:el, startMX:e.clientX, startMY:e.clientY, startX:a.x, startY:a.y,
                      moved:false, nodes:_fnNodesInRect(a.x, a.y, a.w||120, a.h||60)};
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
        var dx=e.clientX-_fnAnnotDrag.startMX, dy=e.clientY-_fnAnnotDrag.startMY;
        if (!_fnAnnotDrag.moved && (Math.abs(dx)>4||Math.abs(dy)>4)) _fnAnnotDrag.moved=true;
        if (_fnAnnotDrag.moved) {
        var s = (getNet() ? getNet().getScale() : 1);
        var gdx=(e.clientX-_fnAnnotDrag.startMX)/s, gdy=(e.clientY-_fnAnnotDrag.startMY)/s;
        var ann = _fnAnnotsLoad();
        if (ann[_fnAnnotDrag.idx]) {
          ann[_fnAnnotDrag.idx].x = _fnAnnotDrag.startX + gdx;
          ann[_fnAnnotDrag.idx].y = _fnAnnotDrag.startY + gdy;
          _fnAnnotsSave(ann); _fnRenderAnnots();
        }
        var nlist = _fnAnnotDrag.nodes || [];
        if (nlist.length) {
          var net0 = getNet();
          if (net0) nlist.forEach(function(it){
            try { net0.moveNode(it.id, it.startNX+gdx, it.startNY+gdy); } catch(e){}
          });
        }
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
      if (_fnAnnotDrag) { _fnAnnotDrag = null; return; }
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
    var btnFnCluster    = document.getElementById('cg-btn-fn-cluster');
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

    if (btnFnCluster) btnFnCluster.addEventListener('click', function(){
      _fnClusterByFile = !_fnClusterByFile;
      btnFnCluster.classList.toggle('active', _fnClusterByFile);
      _fnApplyFileClusters(getNet());
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
    if (btnFnCluster) btnFnCluster.classList.toggle('active', _fnClusterByFile);
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

  /* ── Nodebook: capture/restore adapters for SIDEBAR-owned modes ──
     Each adapter serialises the live state of one mode and restores it.
     Registered on window.CG_NB_ADAPTERS; the Nodebook engine (separate block)
     switches to the mode first, then calls restore(state). Rule 12: every
     viewable mode is capturable. All guarded so capture never throws into the
     user's normal interaction. */
  window.CG_NB_ADAPTERS = window.CG_NB_ADAPTERS || {};

  window.CG_NB_ADAPTERS['fn'] = {
    label: 'Function Nodes',
    title: function(st){ var s = st && st.search; return s ? ('Function Nodes \u00b7 ' + s) : 'Function Nodes'; },
    capture: function(){
      var st = { search: (searchInput ? searchInput.value : '') };
      try {
        var net = getNet();
        if (net) { st.positions = net.getPositions(); st.scale = net.getScale(); st.center = net.getViewPosition(); }
      } catch(e){}
      return st;
    },
    restore: function(st){
      if (!st) return;
      try {
        var net = getNet();
        if (net && st.positions) {
          Object.keys(st.positions).forEach(function(id){
            try { net.moveNode(id, st.positions[id].x, st.positions[id].y); } catch(e){}
          });
        }
        if (net && st.center && st.scale) net.moveTo({ position: st.center, scale: st.scale, animation:false });
      } catch(e){}
      if (searchInput && typeof st.search === 'string') searchInput.value = st.search;
    }
  };

  window.CG_NB_ADAPTERS['script'] = {
    label: 'Script Nodes',
    title: function(st){ var s = st && st.search; return s ? ('Script \u00b7 ' + s) : 'Script Nodes'; },
    capture: function(){
      var st = { search:(searchInput?searchInput.value:''),
                 pan:{x:_svPanX, y:_svPanY, z:_svZoom}, cards:{}, collapsed:[] };
      document.querySelectorAll('#cg-sv-canvas .cg-file-card').forEach(function(c){
        st.cards[c.dataset.fp] = {x:parseFloat(c.style.left)||0, y:parseFloat(c.style.top)||0};
        if (c.classList.contains('sv-collapsed')) st.collapsed.push(c.dataset.fp);
      });
      return st;
    },
    restore: function(st){
      if (!st) return;
      try { if (st.cards) localStorage.setItem(SV_LAYOUT_KEY, JSON.stringify(st.cards)); } catch(e){}
      try {
        if (st.collapsed) {
          var set = {}; st.collapsed.forEach(function(fp){ set[fp]=1; });
          localStorage.setItem(_SV_COLLAPSE_KEY, JSON.stringify(set));
        }
      } catch(e){}
      _svBuilt = false; _buildScriptView();
      if (st.pan){ _svPanX=st.pan.x; _svPanY=st.pan.y; _svZoom=st.pan.z; _svApplyTransform(); }
      if (searchInput && typeof st.search==='string') searchInput.value = st.search;
    }
  };

  window.CG_NB_ADAPTERS['varflow'] = {
    label: 'Variable Flow',
    title: function(st){ var v = st && st.varName; return v ? ('VarFlow \u00b7 ' + v) : 'Variable Flow'; },
    capture: function(){
      return {
        varName: _vfCurrentVar || '',
        scope: _vfCurrentScope || null,
        overrides: JSON.parse(JSON.stringify(_vfNodeOverrides || {})),
        pan: {x:_vfPanX, y:_vfPanY, z:_vfZoom}
      };
    },
    restore: function(st){
      if (!st || !st.varName) return;
      var lk = st.scope ? (st.varName + _VF_SCOPE_SEP + st.scope) : st.varName;
      try { if (st.overrides) localStorage.setItem(VF_LAYOUT_PFX + lk, JSON.stringify(st.overrides)); } catch(e){}
      _vfSelectVar(lk);
      if (st.pan){ _vfPanX=st.pan.x; _vfPanY=st.pan.y; _vfZoom=st.pan.z; _vfApplyTransform(); }
    }
  };

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
        root_ranks = _compute_root_ranks(graph)
        node_sizes = {}
        for nid, fn in graph.functions.items():
            lbl = _build_node_label(fn, self.config)
            rk = root_ranks.get(nid)
            if rk:
                lbl = lbl + ROOT_SUFFIX[rk]
            node_sizes[nid] = _sl_estimate_size(lbl)
        layout = _compute_layout(graph, node_sizes=node_sizes)
        layout_key = _layout_key(graph)
        net, all_positions = self._build_network(graph, layout, root_ranks)
        raw_html = net.generate_html(notebook=False)
        full_html = self._inject_sidebar(raw_html, graph, all_positions, layout_key, root_ranks)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(full_html, encoding="utf-8")
        return out

    # Node counts above this threshold trigger large-graph rendering optimisations.
    _LARGE_GRAPH_THRESHOLD = 2000
    # Node counts above this threshold disable Function Nodes mode entirely
    # (vis.js becomes unusable). The UI auto-switches to Script Nodes view.
    _HUGE_GRAPH_THRESHOLD = 8000
    # PERF-8: node counts at/above this threshold enable viewport virtualisation
    # (CSS content-visibility) for the custom-DOM modes when virtualize_dom="auto".
    _VIRT_DOM_THRESHOLD = 400
    # At/below this node count the Function view uses the syntax-highlighted
    # signature-card overlay (must match FN_CARD_MAX in the sidebar JS). The
    # underlying vis.js boxes are rendered fully transparent so the old plain
    # boxes never flash before the cards take over.
    _FN_CARD_MAX = 4000

    def _build_network(
        self, graph: CallGraph, layout: dict[str, tuple[float, float]],
        root_ranks: dict[str, int],
    ) -> tuple["Network", dict[str, dict]]:
        cfg = self.config
        n_nodes = len(graph.functions)
        large_graph = n_nodes >= self._LARGE_GRAPH_THRESHOLD
        huge_graph = n_nodes >= self._HUGE_GRAPH_THRESHOLD
        # When the signature-card overlay is active, the vis.js boxes are made
        # invisible up-front so the old plain-box render never flashes on load.
        fn_cards_active = (not huge_graph) and (n_nodes <= self._FN_CARD_MAX)

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
        # Executive-clean premium node styling (applied uniformly so both
        # light and dark themes share the same refined geometry): softly
        # rounded corners, generous label margin, a subtle depth shadow and a
        # stronger selection accent. Shadow is perf-gated off for large graphs.
        premium_shadow = (
            {"enabled": True, "color": "rgba(0,0,0,0.22)", "size": 10, "x": 0, "y": 3}
            if (not large_graph and not fn_cards_active) else {"enabled": False}
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
            "nodes": {
                "shape": "box",
                "shapeProperties": {"borderRadius": 6},
                "margin": 10,
                "borderWidthSelected": 3,
                "font": {
                    "size": 12,
                    "face": "'JetBrains Mono','Cascadia Code','SF Mono',Consolas,'Segoe UI Mono',monospace",
                    "color": "#ffffff",
                    "strokeWidth": 0,
                },
                "shadow": premium_shadow,
            },
            "edges": {"smooth": edge_smooth},
        }
        net.set_options(json.dumps(vis_opts))

        entry_ids = set(cfg.filter.entry_points)

        for node_id, fn in graph.functions.items():
            color = EXTERNAL_COLOR if fn.is_external else LANG_COLORS.get(fn.language, LANG_COLORS[Language.PYTHON])
            label = _build_node_label(fn, cfg)
            is_entry = fn.name in entry_ids or fn.qualified_name in entry_ids
            rank = root_ranks.get(node_id)
            if rank:
                border_color = ROOT_BORDERS[rank]
                label = label + ROOT_SUFFIX[rank]
            elif is_entry:
                border_color = ENTRY_BORDER
            else:
                border_color = color["border"]

            # Position already populated by the pre-loop above.
            pos = all_positions[node_id]
            x, y = pos["x"], pos["y"]

            if fn_cards_active:
                # Invisible vis.js box: the signature card is the visible node.
                # Kept as a sized, hit-testable node (label drives box size) so
                # hover / click / drag / edge anchoring still work.
                _TR = "rgba(0,0,0,0)"
                net.add_node(
                    node_id, label=label, title="",
                    color={"background": _TR, "border": _TR,
                           "highlight": {"background": _TR, "border": _TR},
                           "hover": {"background": _TR, "border": _TR}},
                    borderWidth=0, shape="box", shadow=False,
                    font={
                        "size": 12,
                        "face": "'JetBrains Mono','Cascadia Code','SF Mono',Consolas,'Segoe UI Mono',monospace",
                        "color": _TR, "strokeWidth": 0,
                    },
                    size=20, x=int(x), y=int(y), physics=False,
                )
            else:
                net.add_node(
                    node_id, label=label, title="",
                    color={**color, "border": border_color},
                    borderWidth=(3 if (is_entry or rank) else 1),
                    shape="box",
                    font={
                        "size": 12,
                        "face": "'JetBrains Mono','Cascadia Code','SF Mono',Consolas,'Segoe UI Mono',monospace",
                        "color": "#ffffff", "strokeWidth": 0,
                    },
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
        root_ranks: dict[str, int],
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
                    "is_virtual":     fn.is_virtual,
                    "func_type":      fn.func_type,
                    "docstring":      fn.docstring,
                    "tracked_vars":   fn.tracked_vars,
                    "root_rank":      root_ranks.get(node_id),
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
        type_data = _build_type_data(graph)

        # Stable 12-char graph ID: SHA-1 of sorted function IDs.
        # Scopes all localStorage keys so different projects never share annotations.
        _fp = ",".join(sorted(graph.functions.keys()))
        graph_id = hashlib.sha1(_fp.encode()).hexdigest()[:12]

        n_nodes = stats["functions"]
        large_graph_flag = n_nodes >= HtmlRenderer._LARGE_GRAPH_THRESHOLD
        huge_graph_flag  = n_nodes >= HtmlRenderer._HUGE_GRAPH_THRESHOLD

        # PERF-8: viewport virtualisation policy for the custom-DOM modes.
        # output.virtualize_dom: True=always, False=never, "auto"=by node count.
        _vd = getattr(self.config.output, "virtualize_dom", "auto")
        if _vd is True:
            virt_dom_flag = True
        elif _vd is False:
            virt_dom_flag = False
        else:
            virt_dom_flag = n_nodes >= HtmlRenderer._VIRT_DOM_THRESHOLD

        # Compact JSON: no indentation, no spaces. At 15K nodes this saves megabytes
        # off the embedded payload without changing semantics.
        _compact = lambda obj: json.dumps(obj, separators=(',', ':'))

        # PERF-5: optionally compress each big payload to `__cgJ("<base64>")`.
        # Policy from output.compress_payload: True=always, False=never, "auto"=by size.
        _cp = getattr(self.config.output, "compress_payload", "auto")
        _used_compression = [False]

        def _emit(obj):
            s = _compact(obj)
            if _cp is False:
                return s
            if _cp is True or (_cp != False and len(s) >= _COMPRESS_THRESHOLD):
                _used_compression[0] = True
                return "__cgJ(" + json.dumps(_deflate_b64(s)) + ")"
            return s

        sidebar_js = (
            _SIDEBAR_JS
            .replace("CG_NODE_DATA",     _emit(node_data))
            .replace("CG_EDGE_DATA",     _emit(edge_data))
            .replace("CG_INITIAL_POS",   _emit(all_positions))
            .replace("CG_LAYOUT_KEY",    json.dumps(layout_key))
            .replace("CG_ALL_NODE_IDS",  _emit(all_node_ids))
            .replace("CG_VAR_PARENT",    _emit(var_parent_map))
            .replace("CG_VAR_FLOW_DATA", _emit(var_flow_data))
            .replace("CG_GRAPH_ID",      json.dumps(graph_id))
            .replace("CG_LARGE_GRAPH",   json.dumps(large_graph_flag))
            .replace("CG_HUGE_GRAPH",    json.dumps(huge_graph_flag))
            .replace("CG_HUGE_THRESHOLD", json.dumps(HtmlRenderer._HUGE_GRAPH_THRESHOLD))
            .replace("CG_VIRT_DOM",      json.dumps(virt_dom_flag))
            .replace("CG_ROOT_RANKS",    _emit(root_ranks))
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
            '<button id="cg-vf-scope-btn" title="Split variables by scope (don\'t merge unrelated same-named locals)" onclick="_vfToggleScopeSplit()">'
            '⚎ Split scope'
            '</button>'
            '<button id="cg-vf-backward-btn" title="Flow direction: Forward = downstream def-use (where does this value go?); Backward = upstream trace (where did this value come from?)" onclick="_vfToggleBackwardFlow()">'
            '\u2b9e Forward'
            '</button>'
            '<button id="cg-vf-hldir-btn" title="Click-highlight direction: Downstream = colour nodes this block flows into (VF-2); Upstream = colour nodes that feed into this block (VF-6)" onclick="_vfToggleHighlightDirection()">'
            '\u2b9f Downstream'
            '</button>'
            '<label id="cg-vf-trace-cb-lbl" title="Click a block to colour its flow paths. Uncheck to drag blocks without re-tracing.">'
            '<input type="checkbox" id="cg-vf-trace-cb" onchange="_vfToggleTraceEnabled(this.checked)"> trace'
            '</label>'
            '<button id="cg-vf-dead-btn" title="Show all unused/dead variables" onclick="_vfToggleDeadMode()">'
            '⛔ Dead Vars'
            '</button>'
            '<button id="cg-vf-annot-btn" title="Draw annotation rectangle (click then drag on canvas)" onclick="_vfToggleAnnotMode()">'
            '□ Annotate'
            '</button>'
            '</div>'
            '<div id="cg-vf-family-row">'
            '<span class="cg-vf-fam-label">Show:</span>'
            '<button class="cg-vf-fam-btn" data-fam="variable" onclick="_vfToggleFamilyFilter(\'variable\')" title="Toggle visibility of normal assign/read variable blocks">🔵 Variable</button>'
            '<button class="cg-vf-fam-btn" data-fam="member" onclick="_vfToggleFamilyFilter(\'member\')" title="Toggle visibility of member-access (struct/class field) blocks">🟣 Member</button>'
            '<span class="cg-vf-fam-sep"></span>'
            '<span class="cg-vf-fam-label">All flows:</span>'
            '<button class="cg-vf-fam-show-btn" data-fam="lugasi" onclick="_vfShowFamily(\'lugasi\')" title="Show every custom-input source block and its downstream flow">🟠 <span class="cg-vf-fam-show-lbl">…</span></button>'
            '<button class="cg-vf-fam-show-btn" data-fam="connect" onclick="_vfShowFamily(\'connect\')" title="Show every .connect input block and its downstream flow">🔗 <span class="cg-vf-fam-show-lbl">.connect</span></button>'
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

        html = raw_html.replace("</head>", _SIDEBAR_CSS + "\n" + _CGX_EXTRAS_CSS + "\n" + _NODEBOOK_CSS + "\n" + _TYPE_VIEW_CSS + "\n</head>", 1)
        html = html.replace(
            "<body>",
            "<body>\n" + sidebar_html + "\n"
            + '<div id="cg-script-view"></div>' + "\n"
            + varflow_div + "\n"
            + '<div id="cg-type-view"></div>' + "\n"
            + _CGX_EXTRAS_HTML + "\n"
            + _NODEBOOK_HTML + "\n"
            + detail_div + "\n" + modal_div,
            1,
        )
        extras_js = _CGX_EXTRAS_JS.replace("CGX_EXTRAS_DATA", _emit(extras_payload))
        type_view_js = _TYPE_VIEW_JS.replace("CG_TYPE_DATA", _emit(type_data))
        # PERF-5: only embed the ~6 KB inflate bootstrap when something was compressed.
        decomp_js = _DECOMP_JS if _used_compression[0] else ""
        html = html.replace("</body>", decomp_js + sidebar_js + "\n" + extras_js + "\n" + _NODEBOOK_JS + "\n" + type_view_js + "\n</body>", 1)
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
            # Compute root ranks for include graph (fewest includers + largest subtree)
            inc_root_ranks = _compute_include_root_ranks(ig)
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
                            "guard": e.guard,
                        }
                        for e in edges
                    ]
                    for f, edges in ig.files.items()
                },
                "unresolved_count": len(ig.unresolved),
                "cycles": ig.cycles[:25],
                "most_included": ig.most_included,
                "root_ranks": inc_root_ranks,
                # INC-1: includes dropped by a definitely-false preprocessor guard.
                "excluded": [
                    {
                        "from": e.from_file,
                        "to": e.to_file,
                        "raw": e.raw_target,
                        "line": e.line,
                        "guard": e.guard,
                        "is_system": e.is_system,
                    }
                    for e in ig.excluded[:200]
                ],
                "excluded_count": len(ig.excluded),
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
                        "is_virtual": fn.is_virtual,
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
            "type_mode_enabled": bool(getattr(graph, "type_graph", None) and graph.type_graph.types),
        }


def _build_type_data(graph: "CallGraph") -> dict:
    """Serialize ``graph.type_graph`` into the Type Nodes Mode payload.

    Shape: ``{types:{id:{...}}, edges:[...], roots:[...], stats:{...}}``.
    Returns an empty skeleton when no type graph is present so the view JS
    always has a well-formed object to read (graceful degradation, Rule).
    """
    tg = getattr(graph, "type_graph", None)
    if tg is None or not getattr(tg, "types", None):
        return {"types": {}, "edges": [], "roots": [], "stats": {}}

    types_out: dict = {}
    for tid, td in tg.types.items():
        members_out = [
            {
                "name":           m.name,
                "type_text":      m.type_text,
                "is_pointer":     m.is_pointer,
                "array_dims":     list(m.array_dims or []),
                "bitfield_width": m.bitfield_width,
                "is_func_ptr":    m.is_func_ptr,
                "func_ptr_sig":   m.func_ptr_signature,
                "canonical_type": m.canonical_type,
                "anon_child_id":  m.anon_child_id,
                "line":           m.line,
            }
            for m in td.members
        ]
        types_out[tid] = {
            "id":          tid,
            "name":        td.display_name,
            "tag":         td.tag_name,
            "kind":        td.kind,
            "aliases":     list(td.aliases or []),
            "file":        td.file,
            "line":        td.line_start,
            "members":     members_out,
            "enum_values": [list(ev) for ev in (td.enum_values or [])],
            "is_anon":     td.is_anonymous,
            "parent":      td.parent_type_id,
            "doc":         td.doc_comment,
            "used_by":     list(td.used_by_functions or []),
        }

    edges_out = [
        {
            "src":     e.src_type_id,
            "dst":     e.dst_type_id,
            "kind":    e.kind,
            "members": list(e.member_names or []),
            "count":   e.count,
        }
        for e in tg.edges
    ]

    stats = tg.stats or tg.stats_dict()
    return {
        "types": types_out,
        "edges": edges_out,
        "roots": list(tg.roots or []),
        "stats": stats,
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
#cg-iv-showall {
  background: #2b2140; color: #d9c7ff; border: 1px solid #5a4790;
  padding: 3px 10px; border-radius: 4px; cursor: pointer; font-size: 12px;
}
#cg-iv-showall:hover { background: #3a2d57; color: #fff; }
.cg-iv-guard {
  display: inline-block; font-size: 9px; color: #c89bff;
  background: rgba(120,80,200,0.16); border: 1px solid rgba(120,80,200,0.4);
  border-radius: 3px; padding: 0 4px; margin-left: 5px; vertical-align: middle;
}
.cg-iv-panel-btn {
  display: block; width: 100%; box-sizing: border-box; margin: 4px 0 0;
  background: #243a52; color: #cfe2f7; border: 1px solid #35597e;
  padding: 5px 8px; border-radius: 4px; cursor: pointer; font-size: 11px; text-align: left;
}
.cg-iv-panel-btn:hover { background: #2f4d6c; color: #fff; }
.cg-iv-excl-item { font-size: 11px; padding: 2px 0; color: #c8b8e0; }

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
.cg-iv-node-label { font-weight: 600; font-size: 12px; color: #9CDCFE; overflow: hidden; text-overflow: ellipsis; }
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
.cgx-mv-name { font-weight: 700; color: #C586C0; flex: 1; font-size: 13px; }
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
.cgx-mv-fname { font-family: monospace; color: #9CDCFE; flex: 1; font-size: 11px; }
.cgx-mv-fmeta { color: #6a7a8a; font-size: 10px; }
.cgx-mv-fns { display: none; padding: 2px 4px 2px 20px; }
.cgx-mv-file.open > .cgx-mv-fns { display: block; }
.cgx-mv-fn {
  padding: 2px 6px; cursor: pointer; border-radius: 3px;
  color: #DCDCAA; font-family: monospace; font-size: 11px;
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
body[data-theme="light"] .cgx-mv-name { color: #7c3aed; }
body[data-theme="light"] .cgx-mv-summary { color: #4a6070; }
body[data-theme="light"] .cgx-mv-badge { background: rgba(0,0,0,0.06); color: #4a6070; }
body[data-theme="light"] .cgx-mv-fname { color: #0369a1; }
body[data-theme="light"] .cgx-mv-fmeta { color: #7a8898; }
body[data-theme="light"] .cgx-mv-fn { color: #a16207; }
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
body[data-theme="light"] .cg-iv-node-label { color: #0369a1; }
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
  try { window.CGX = CGX; } catch(e) {}  /* expose for Investigator / debugging */
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

  /* Nodebook controls stay inside the View Mode section but on their own second
     row (the mode-button row was too tight). Available in every mode (Rule 12):
     a ➕ Capture button snapshots the current view and a 📓 Nodebook tab opens
     the saved-pages gallery. */
  var _modeRow = (btnVf && btnVf.parentElement) || (btnFn && btnFn.parentElement);
  var btnCapture = null, btnNodebook = null, btnType = null;
  if (_modeRow) {
    var _nbRow = _h('div', {'class':'cg-nb-btn-row'});
    btnCapture  = _h('button', {'class':'cg-btn','id':'cgx-btn-nb-capture','title':'Capture current view to Nodebook (N)'}, '➕ Capture');
    btnNodebook = _h('button', {'class':'cg-btn','id':'cgx-btn-mode-nodebook','title':'Open Nodebook (B)'}, '📓 Nodebook');
    _nbRow.appendChild(btnCapture);
    _nbRow.appendChild(btnNodebook);
    if (_modeRow.parentElement) _modeRow.parentElement.insertBefore(_nbRow, _modeRow.nextSibling);
    else _modeRow.appendChild(_nbRow);

    /* Type Nodes Mode gets its own row directly under the Capture/Nodebook row,
       shown only when the backend detected type definitions (CGX.type_mode_enabled). */
    if (CGX.type_mode_enabled) {
      var _typeRow = _h('div', {'class':'cg-nb-btn-row'});
      btnType = _h('button', {'class':'cg-btn','id':'cgx-btn-mode-types','title':'Type Nodes Mode — struct / union / enum / typedef / class graph (T)'}, '🧬 Type Mode');
      _typeRow.appendChild(btnType);
      if (_nbRow.parentElement) _nbRow.parentElement.insertBefore(_typeRow, _nbRow.nextSibling);
      else _modeRow.appendChild(_typeRow);
    }
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
    '</div>'+
    '<div class="cgx-row" id="cgx-mv-trace-row" style="margin-top:4px"></div>'
  );
  if (sb) sb.appendChild(mvSec);

  /* ---------- Include Graph view (nodes + edges canvas) ---------- */
  if (CGX.include_graph_enabled && CGX.include_graph) {
    var incView = document.getElementById('cgx-inc-view');
    var ig = CGX.include_graph;

    /* ── helpers ──────────────────────────────────────────────────── */
    var _HDR_EXTS = /\.(h|hpp|hxx|hh|inl|tpp)$/i;
    var _M_EXT = /\.m$/i;
    function _isHeader(p) { return _HDR_EXTS.test((p||'').replace(/\\/g,'/').replace(/\?.*$/,'')); }
    /* A graph node is any C/C++ header OR a MATLAB .m module file (gap G5). */
    function _isGraphNode(p) {
      var s = (p||'').replace(/\\/g,'/').replace(/\?.*$/,'');
      return _HDR_EXTS.test(s) || _M_EXT.test(s);
    }
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
    var _ivHeat = false;          /* INC-2: colour nodes by include depth */
    var _ivDepth = {}, _ivMaxDepth = 0; /* INC-2: per-node max include depth */
    var _ivExcluded = ig.excluded || [];      /* INC-1: guarded-out includes */
    var _ivExclCount = ig.excluded_count || 0;

    /* ── build node/edge sets (rebuilt when system-includes toggle changes) ── */
    var _projHdrs = {}; // path -> edges[] (project headers AND .m module files, gap G5)
    Object.keys(ig.files).forEach(function(fp){ if(_isGraphNode(fp)) _projHdrs[fp]=ig.files[fp]; });

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

    /* ── INC-2: longest include-depth per node ─────────────────────────
     * depth(node) = length of the longest chain of #includes leading INTO the
     * node (0 = a root that nothing includes). Deeply-nested headers — the ones
     * that inflate compile times — get the highest values. Cycle-safe. */
    function _ivComputeDepth() {
      _ivDepth = {}; _ivMaxDepth = 0;
      var radj = {}; /* node -> parents (headers that include it) */
      _ivNodes.forEach(function(n){ radj[n.id] = []; });
      _ivEdges.forEach(function(e){ if (radj[e.to]) radj[e.to].push(e.from); });
      var state = {}; /* 0=unvisited 1=on-stack 2=done */
      function dfs(id){
        if (state[id] === 2) return _ivDepth[id];
        if (state[id] === 1) return 0; /* cycle guard */
        state[id] = 1;
        var best = 0;
        (radj[id] || []).forEach(function(p){
          var d = dfs(p) + 1;
          if (d > best) best = d;
        });
        _ivDepth[id] = best; state[id] = 2;
        if (best > _ivMaxDepth) _ivMaxDepth = best;
        return best;
      }
      _ivNodes.forEach(function(n){ dfs(n.id); });
    }
    _ivComputeDepth();

    function _ivHeatColor(d) {
      var t = _ivMaxDepth > 0 ? Math.max(0, Math.min(1, d / _ivMaxDepth)) : 0;
      /* hue 200 (blue, shallow) -> 0 (red, deep) */
      var hue = Math.round(200 * (1 - t));
      var light = 22 + Math.round(t * 8);
      return { bg: 'hsl('+hue+',55%,'+light+'%)',
               border: 'hsl('+hue+',60%,'+(light+22)+'%)',
               text: 'hsl('+hue+',45%,88%)' };
    }

    /* ── skeleton HTML ────────────────────────────────────────────── */
    incView.innerHTML =
      '<div id="cg-iv-toolbar">' +
        '<h3>Include Graph</h3>' +
        '<span class="cg-iv-stat" id="cg-iv-s-files"><b>'+Object.keys(_projHdrs).length+'</b> headers</span>' +
        '<span class="cg-iv-stat" id="cg-iv-s-miss"><b>'+_ivMissingCount+'</b> missing</span>' +
        '<span class="cg-iv-stat" id="cg-iv-s-cyc"><b>'+(ig.cycles||[]).length+'</b> cycles</span>' +
        (_ivExclCount ? '<span class="cg-iv-stat" id="cg-iv-s-excl" title="Includes dropped by a preprocessor guard (INC-1)" style="cursor:pointer;color:#c89bff"><b>'+_ivExclCount+'</b> guarded-out</span>' : '') +
        '<div class="cg-iv-filters">' +
          '<label class="cg-iv-filter-lbl" title="INC-2: colour headers by their maximum include depth"><input type="checkbox" id="cg-iv-heat"> 🌡 depth heat-map</label>' +
          '<button id="cg-iv-showall" title="INC-3: clear focus and show the whole graph" style="display:none">↺ Show all</button>' +
          '<input id="cg-iv-search" type="text" placeholder="search headers…" spellcheck="false">' +
          '<span id="cg-iv-trace-mount" style="display:inline-flex;align-items:center"></span>' +
        '</div>' +
        '<button id="cgx-inc-close" title="Close">✕ Close</button>' +
      '</div>' +
      '<div id="cg-iv-legend">' +
        '<span id="cg-iv-legend-status">' +
        '<span class="cg-iv-leg"><span class="cg-iv-leg-dot" style="background:#1f3028;border:1px solid #27ae60"></span>resolved</span>' +
        '<span class="cg-iv-leg"><span class="cg-iv-leg-dot" style="background:#2e1e0a;border:1px dashed #e67e22"></span>missing</span>' +
        '<span class="cg-iv-leg"><span class="cg-iv-leg-dot" style="background:#2a1010;border:1px solid #e74c3c"></span>cycle</span>' +
        '<span class="cg-iv-leg"><span class="cg-iv-leg-dot" style="background:#252012;border:1px solid #7a6a10"></span>mixed</span>' +
        '<span class="cg-iv-leg"><span class="cg-iv-leg-dot" style="background:#1a1e2a;border:1px dotted #3a4460"></span>system</span>' +
        '</span>' +
        '<span id="cg-iv-legend-heat" style="display:none;align-items:center;gap:6px">' +
          'include depth&nbsp;<span style="font-size:10px">shallow</span>' +
          '<span style="display:inline-block;width:120px;height:10px;border-radius:3px;background:linear-gradient(90deg,hsl(200,60%,24%),hsl(120,60%,26%),hsl(50,65%,30%),hsl(0,65%,30%))"></span>' +
          '<span style="font-size:10px">deep</span>' +
        '</span>' +
        '<span style="margin-left:auto;font-size:10px;color:#5a7090">scroll to zoom · middle-drag to pan · drag background to multi-select · double-click a header to focus its include closure</span>' +
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
        path.setAttribute('data-from', e.from);
        path.setAttribute('data-to', e.to);
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
        var _d = _ivDepth[n.id] || 0;
        el.title = n.path + '  ·  include depth ' + _d;
        var badge = n.inDeg ? '<span class="cg-iv-node-badge">×'+n.inDeg+'</span>' : '';
        if (_ivHeat) {
          var hc = _ivHeatColor(_d);
          el.style.background = hc.bg;
          el.style.borderColor = hc.border;
          el.style.color = hc.text;
          badge = '<span class="cg-iv-node-badge">d'+_d+(n.inDeg?' ×'+n.inDeg:'')+'</span>';
        }
        el.innerHTML = '<div class="cg-iv-node-label">'+esc(n.label)+'</div>' +
                       '<div class="cg-iv-node-sub">'+esc(_dir(n.path))+'</div>' + badge;
        /* Root badge for include graph */
        var ivRootRank = ig.root_ranks && ig.root_ranks[n.id];
        if (ivRootRank === 1) el.innerHTML += '<span class="cg-root-badge rank-1" style="position:absolute;bottom:4px;left:4px;font-size:9px">&#x1F451; Root</span>';
        else if (ivRootRank === 2) el.innerHTML += '<span class="cg-root-badge rank-2" style="position:absolute;bottom:4px;left:4px;font-size:9px">&#x2605; Root #2</span>';
        else if (ivRootRank === 3) el.innerHTML += '<span class="cg-root-badge rank-3" style="position:absolute;bottom:4px;left:4px;font-size:9px">&#x2605; Root #3</span>';
        inner.appendChild(el);
        _ivNodeEls[n.id] = el;
      });

      /* PERF-8: virtualise include-graph node tiles (arrows read each tile's own
         offsetWidth/Height, preserved by the pinned intrinsic-size). */
      if (window._cgVirtualize) window._cgVirtualize(inner, '.cg-iv-node');

      /* INC-3: reflect focus state on the "Show all" button */
      var _sa = document.getElementById('cg-iv-showall');
      if (_sa) _sa.style.display = (_ivFocusIds !== null) ? '' : 'none';

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

      /* INC-3: prune-to-closure actions (skip for missing headers) */
      if (!node.isMissing) {
        html += '<div class="cg-iv-section">' +
          '<button class="cg-iv-panel-btn" data-prune="down">⌖ Show include closure (everything this pulls in)</button>' +
          '<button class="cg-iv-panel-btn" data-prune="up">⤢ Show who includes this (closure)</button>' +
          (_ivFocusIds !== null ? '<button class="cg-iv-panel-btn" data-prune="clear">↺ Show whole graph</button>' : '') +
        '</div>';
      }

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
          var guard = e.guard ? ' <span class="cg-iv-guard" title="Conditional include — only active under this preprocessor guard">'+esc(e.guard)+'</span>' : '';
          html += '<div class="cg-iv-edge-item"><span class="'+cls+'"'+nav+navMiss+'>'+(e.is_system?'&lt;':'&quot;')+esc(e.raw)+(e.is_system?'&gt;':'&quot;')+
            (!e.resolved&&!e.is_system?' <i style="font-size:9px">(not found)</i>':'')+
            '</span>'+guard+(e.line?'<span class="cg-iv-line">L'+e.line+'</span>':'')+'</div>';
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
      /* INC-3: wire prune-to-closure buttons */
      Array.prototype.forEach.call(pbody.querySelectorAll('[data-prune]'), function(el){
        el.addEventListener('click', function(){
          var mode = el.getAttribute('data-prune');
          if (mode === 'clear') { if (window.cgxIncClearFocus) window.cgxIncClearFocus(); return; }
          if (window.cgxIncIsolate) window.cgxIncIsolate(nid, 0, mode === 'up' ? 'callers' : 'callees');
        });
      });
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

    /* INC-1: list the includes that were dropped by a definitely-false guard. */
    function _ivShowExcluded() {
      var panel = document.getElementById('cg-iv-panel');
      var pbody = document.getElementById('cg-iv-panel-body');
      if (!panel || !pbody) return;
      document.getElementById('cg-iv-ph-name').textContent = 'Guarded-out includes';
      document.getElementById('cg-iv-ph-path').textContent =
        _ivExclCount + ' include(s) dropped by preprocessor guards (INC-1)';
      panel.classList.add('open');
      var html = '<div class="cg-iv-section"><div class="cg-iv-empty-note">' +
        'These #include lines sit inside a branch the build defines prove is not ' +
        'taken (e.g. an #if 0 block, or the inactive side of an #ifdef whose macro ' +
        'is known-defined), so they are excluded from the graph.</div>';
      (_ivExcluded || []).forEach(function(e){
        var src = (e.from || '').replace(/\\/g,'/').split('/').pop();
        html += '<div class="cg-iv-excl-item">' +
          '<b>'+esc(e.is_system?'<'+e.raw+'>':'"'+e.raw+'"')+'</b>' +
          ' <span class="cg-iv-guard">'+esc(e.guard||'')+'</span>' +
          '<div style="color:#5a7090;font-size:10px">in '+esc(src)+(e.line?' · L'+e.line:'')+'</div>' +
        '</div>';
      });
      html += '</div>';
      pbody.innerHTML = html;
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

    /* INC-2: depth heat-map toggle */
    var heatCb = document.getElementById('cg-iv-heat');
    if (heatCb) {
      try { if (localStorage.getItem('cgxIncHeat') === '1') { heatCb.checked = true; _ivHeat = true; } } catch(e){}
      var _statusLeg = document.getElementById('cg-iv-legend-status');
      var _heatLeg = document.getElementById('cg-iv-legend-heat');
      function _syncHeatLegend(){
        if (_statusLeg) _statusLeg.style.display = _ivHeat ? 'none' : '';
        if (_heatLeg) _heatLeg.style.display = _ivHeat ? 'inline-flex' : 'none';
      }
      _syncHeatLegend();
      heatCb.addEventListener('change', function(){
        _ivHeat = heatCb.checked;
        try { localStorage.setItem('cgxIncHeat', _ivHeat ? '1' : '0'); } catch(e){}
        _syncHeatLegend();
        _ivRender();
      });
    }

    /* INC-3: "Show all" clears focus filtering */
    var showAllBtn = document.getElementById('cg-iv-showall');
    if (showAllBtn) showAllBtn.addEventListener('click', function(){
      if (window.cgxIncClearFocus) window.cgxIncClearFocus();
    });

    /* INC-1: clicking the "guarded-out" stat lists the dropped includes */
    var exclStat = document.getElementById('cg-iv-s-excl');
    if (exclStat) exclStat.addEventListener('click', function(){ _ivShowExcluded(); });


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

      /* INC-3: double-click a header to focus its transitive include closure */
      canvas.addEventListener('dblclick', function(ev){
        var nodeEl = ev.target.closest ? ev.target.closest('.cg-iv-node') : null;
        if (!nodeEl) return;
        ev.preventDefault();
        var nid = nodeEl.getAttribute('data-nid') || '';
        if (nid && window.cgxIncIsolate) window.cgxIncIsolate(nid, 0, 'callees');
      });

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
          if (window._ivClearTrace) _ivClearTrace();
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
            if(nid) {
              _ivOpenPanel(nid);
              if (window.cgTraceEnabled('inc') && typeof _ivApplyTrace === 'function') _ivApplyTrace(nid);
            }
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

    /* ── Include-mode flow-trace ──────────────────────────────────────
       Click a header → colour its downstream/upstream include paths using the
       shared engine, dimming unreached tiles AND recolouring the SVG arrows
       along each branch (with the rest dimmed). */
    var _ivTraceOrigin = null;
    function _ivEnsureArrowMarker(col) {
      var svg = document.getElementById('cg-iv-arrows');
      if (!svg) return '';
      var defs = svg.querySelector('defs');
      if (!defs) return '';
      var id = 'iv-arr-tr-' + col.replace(/[^a-zA-Z0-9]/g, '');
      if (!document.getElementById(id)) {
        var m = document.createElementNS('http://www.w3.org/2000/svg','marker');
        m.setAttribute('id', id);
        m.setAttribute('markerWidth','10'); m.setAttribute('markerHeight','10');
        m.setAttribute('refX','9'); m.setAttribute('refY','5');
        m.setAttribute('orient','auto'); m.setAttribute('markerUnits','strokeWidth');
        var mp = document.createElementNS('http://www.w3.org/2000/svg','path');
        mp.setAttribute('d','M0,1 L9,5 L0,9 Z'); mp.setAttribute('fill', col);
        m.appendChild(mp); defs.appendChild(m);
      }
      return id;
    }
    function _ivClearTrace() {
      _ivTraceOrigin = null;
      Array.prototype.forEach.call(document.querySelectorAll('.cg-iv-node'), function(el) {
        el.classList.remove('cg-trace-lit', 'cg-trace-merge');
        el.style.borderColor = '';
        el.style.boxShadow = '';
        el.style.opacity = '';
      });
      /* Rebuild arrows from scratch → restores default stroke/marker/opacity. */
      try { _ivDrawArrows(); } catch(e) {}
    }
    window._ivClearTrace = _ivClearTrace;
    function _ivApplyTrace(originId) {
      _ivClearTrace();
      var trace = window.cgFlowTraceColors(originId, _ivEdges, window.cgTraceDir('inc'));
      if (!trace.neighbors.length) return;
      _ivTraceOrigin = originId;
      Array.prototype.forEach.call(document.querySelectorAll('.cg-iv-node'), function(el) {
        var nid = el.getAttribute('data-nid') || '';
        if (nid === originId) return;
        var c = trace.color[nid];
        if (c) {
          el.classList.add('cg-trace-lit');
          el.style.borderColor = c;
          el.style.boxShadow = '0 0 10px ' + c;
          el.style.opacity = '1';
          if (trace.merge[nid]) el.classList.add('cg-trace-merge');
        } else {
          el.style.opacity = '0.18';
        }
      });
      /* Recolour the SVG arrows along the traced paths; dim the rest. */
      var svg = document.getElementById('cg-iv-arrows');
      if (svg) {
        var isUp = (window.cgTraceDir('inc') === 'upstream');
        var origStr = String(originId);
        Array.prototype.forEach.call(svg.querySelectorAll('path.iv-arrow'), function(path) {
          var from = path.getAttribute('data-from');
          var to   = path.getAttribute('data-to');
          var src = isUp ? to : from;
          var dst = isUp ? from : to;
          var srcOk = (String(src) === origStr) || trace.color[src];
          var dc = trace.color[dst];
          if (srcOk && dc) {
            path.setAttribute('stroke', dc);
            path.setAttribute('stroke-width', '3');
            path.setAttribute('opacity', '1');
            var mid = _ivEnsureArrowMarker(dc);
            if (mid) path.setAttribute('marker-end', 'url(#' + mid + ')');
          } else {
            path.setAttribute('opacity', '0.08');
          }
        });
      }
    }
    function _ivMountTraceControl() {
      var mount = document.getElementById('cg-iv-trace-mount');
      if (!mount || mount._wired) return;
      mount._wired = true;
      var ctl = window.cgBuildTraceControl('inc',
        function(enabled) { if (!enabled) _ivClearTrace(); },
        function(dir) { if (_ivTraceOrigin) _ivApplyTrace(_ivTraceOrigin); });
      mount.appendChild(ctl);
    }
    _ivMountTraceControl();

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

    /* Nodebook state capture/restore for the Include graph. Captures pan/zoom,
       search text and every node tile's position so a saved page reopens exactly
       as the user arranged it. */
    window.cgxIncGetState = function() {
      var pos = {};
      Array.prototype.forEach.call(document.querySelectorAll('.cg-iv-node'), function(el){
        var nid = el.getAttribute('data-nid');
        if (nid) pos[nid] = { left: parseFloat(el.style.left)||0, top: parseFloat(el.style.top)||0 };
      });
      var inp = document.getElementById('cg-search');
      return { pan:{x:_ivPanX, y:_ivPanY, z:_ivZoom}, search: inp ? inp.value : '', positions: pos };
    };
    window.cgxIncSetState = function(st) {
      if (!st) return;
      try {
        if (st.positions) {
          Object.keys(st.positions).forEach(function(nid){
            var el = _ivNodeEls[nid];
            if (el) { el.style.left = st.positions[nid].left + 'px'; el.style.top = st.positions[nid].top + 'px'; }
          });
          setTimeout(_ivDrawArrows, 20);
        }
      } catch(e){}
      if (st.pan) { _ivPanX = st.pan.x; _ivPanY = st.pan.y; _ivZoom = st.pan.z; _ivApplyTransform(); }
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
      /* Root badge: find best root_rank among functions in this module's hierarchy */
      var mvRootRank = null;
      var rootRanksMap = window.CGX_ROOT_RANKS || {};
      hierarchy.forEach(function(filerec){
        (filerec.fns || []).forEach(function(pair){
          var r = rootRanksMap[pair[0]];
          if (r && (mvRootRank === null || r < mvRootRank)) mvRootRank = r;
        });
      });
      if (mvRootRank === 1) headInner += '<span class="cg-root-badge rank-1">&#x1F451; Root</span>';
      else if (mvRootRank === 2) headInner += '<span class="cg-root-badge rank-2">&#x2605; Root #2</span>';
      else if (mvRootRank === 3) headInner += '<span class="cg-root-badge rank-3">&#x2605; Root #3</span>';
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
            if (window.CG_VIRT_DOM && window._cgVirtRefresh) {
              var _rc = window._cgVirtRefresh(card); if (_rc && _rc.apply) _rc.apply();
            }
            _mvRedrawArrows();
          });
          fnsBox.appendChild(more);
        }
        fileEl.querySelector('.cgx-mv-file-head').addEventListener('click', function(ev){
          ev.stopPropagation();
          fileEl.classList.toggle('open');
          if (window.CG_VIRT_DOM && window._cgVirtRefresh) {
            var _rc = window._cgVirtRefresh(card); if (_rc && _rc.apply) _rc.apply();
          }
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

    /* PERF-8: virtualise module cards (fit + arrow geometry read each card's own
       offsetWidth/Height, preserved by the pinned intrinsic-size). */
    if (window._cgVirtualize) window._cgVirtualize(inner, '.cgx-mv-card');

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
        /* True click — toggle expand/collapse, then flow-trace (if enabled). */
        card.classList.toggle('open');
        if (window.CG_VIRT_DOM && window._cgVirtRefresh) {
          var _rc = window._cgVirtRefresh(card); if (_rc && _rc.apply) _rc.apply();
        }
        _mvRedrawArrows();
        if (window.cgTraceEnabled('module') && typeof _mvApplyTrace === 'function') {
          var _mid = card.getAttribute('data-id') || '';
          if (_mid) _mvApplyTrace(_mid);
        }
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

  /* ── Module-mode flow-trace (Folder / Library / Namespace) ───────────
     Operates on the aggregated card-to-card edges; colours module cards. */
  var _mvTraceOrigin = null;
  function _mvClearTrace() {
    _mvTraceOrigin = null;
    Array.prototype.forEach.call(document.querySelectorAll('.cgx-mv-card'), function(el) {
      el.classList.remove('cg-trace-lit', 'cg-trace-merge');
      el.style.borderColor = '';
      el.style.boxShadow = '';
      el.style.opacity = '';
    });
  }
  window._mvClearTrace = _mvClearTrace;
  function _mvApplyTrace(originId) {
    _mvClearTrace();
    var trace = window.cgFlowTraceColors(originId, _mvAggEdges, window.cgTraceDir('module'));
    if (!trace.neighbors.length) return;
    _mvTraceOrigin = originId;
    Object.keys(_mvCardByNodeId).forEach(function(id) {
      var el = _mvCardByNodeId[id];
      if (!el) return;
      if (id === originId) return;
      var c = trace.color[id];
      if (c) {
        el.classList.add('cg-trace-lit');
        el.style.borderColor = c;
        el.style.boxShadow = '0 0 10px ' + c;
        el.style.opacity = '1';
        if (trace.merge[id]) el.classList.add('cg-trace-merge');
      } else {
        el.style.opacity = '0.2';
      }
    });
  }
  function _mvMountTraceControl() {
    var mount = document.getElementById('cgx-mv-trace-row');
    if (!mount || mount._wired) return;
    mount._wired = true;
    var ctl = window.cgBuildTraceControl('module',
      function(enabled) { if (!enabled) _mvClearTrace(); },
      function(dir) { if (_mvTraceOrigin) _mvApplyTrace(_mvTraceOrigin); });
    mount.appendChild(ctl);
  }
  _mvMountTraceControl();

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

  /* Nodebook state capture/restore for the Module view. Captures pan/zoom, the
     per-card position + expanded state, top-N and search so a saved page reopens
     with the user's exact card arrangement. Restores onto the active build. */
  window.cgxModuleGetState = function() {
    var cards = {};
    Array.prototype.forEach.call(document.querySelectorAll('.cgx-mv-card'), function(c){
      var id = c.getAttribute('data-id');
      if (id) cards[id] = { left:parseFloat(c.style.left)||0, top:parseFloat(c.style.top)||0,
                            open:c.classList.contains('open') };
    });
    var lvl = (_mvBuiltFor && _mvBuiltFor.indexOf(':') >= 0) ? _mvBuiltFor.split(':')[1] : 'module';
    return { builtFor:_mvBuiltFor, level:lvl, pan:{x:_mvPanX, y:_mvPanY, z:_mvZoom}, topN:_mvTopN,
             search:(mvSearchInput?mvSearchInput.value:''), cards:cards };
  };
  window.cgxModuleSetState = function(st) {
    if (!st) return;
    try {
      if (st.cards) {
        Object.keys(st.cards).forEach(function(id){
          var c = _mvCardByNodeId[id];
          if (!c) return;
          c.style.left = st.cards[id].left + 'px';
          c.style.top  = st.cards[id].top + 'px';
          if (st.cards[id].open) c.classList.add('open'); else c.classList.remove('open');
        });
      }
    } catch(e){}
    if (st.pan) { _mvPanX = st.pan.x; _mvPanY = st.pan.y; _mvZoom = st.pan.z; _mvApplyTransform(); }
    setTimeout(_mvRedrawArrows, 30);
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
    var nbEl = document.getElementById('cg-nodebook');
    if (nbEl) nbEl.style.display = 'none';
    var tyEl = document.getElementById('cg-type-view');
    if (tyEl) tyEl.style.display = 'none';
    [btnFn, btnSv, btnVf, btnInc, btnNodebook, btnType].forEach(function(b){ if (b) b.classList.remove('active'); });
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

  function _activateNodebook() {
    _hideAllViews();
    if (btnNodebook) btnNodebook.classList.add('active');
    var nbEl = document.getElementById('cg-nodebook');
    if (nbEl) nbEl.style.display = 'flex';
    if (typeof window.cgNodebookOpen === 'function') window.cgNodebookOpen();
  }

  function _activateType() {
    _hideAllViews();
    if (btnType) btnType.classList.add('active');
    if (typeof window.cgTypeActivate === 'function') window.cgTypeActivate();
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
    inc:     { view: 'cgx-inc-view',    enter: function(){ _safeActivate('Include Graph',   _activateInclude); },                              edgeFilter: _filterIncludeEdges },
    nodebook:{ view: 'cg-nodebook',     enter: function(){ _safeActivate('Nodebook',        _activateNodebook); },                             edgeFilter: null },
    types:   { view: 'cg-type-view',    enter: function(){ _safeActivate('Type Nodes Mode', _activateType); },                                 edgeFilter: null }
  };
  window.RENDER_MODES = RENDER_MODES;

  /* The Nodebook needs to know which real view was active so "Capture" snapshots
     the right mode. We remember the last non-nodebook mode here. */
  window.CG_NB_LAST_MODE = window.CG_NB_LAST_MODE || 'fn';
  window.setViewMode = function(mode) {
    _cgxCurrentMode = mode || 'fn';
    if (mode && mode !== 'nodebook') window.CG_NB_LAST_MODE = mode;
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

  /* Nodebook: restore a captured module/folder page onto its *original* slot.
     Folder and Module are both internal mode 'module' but live in different
     slots (e.g. slot1=folder, slot2=module). The captured state carries
     `builtFor` = '<slotId>:<level>', so we re-activate that exact slot before
     the adapter replays card positions — this is what keeps a folder capture
     and a module capture independent instead of overriding each other. */
  window.cgxModuleActivateBuilt = function(builtFor){
    if (!builtFor || builtFor.indexOf(':') < 0){ window.setViewMode('module'); return; }
    var slotId = builtFor.split(':')[0];
    if (SLOTS[slotId]){ activateSlot(slotId); }
    else { window.setViewMode('module'); }
  };

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
  if (btnNodebook) btnNodebook.addEventListener('click', function(){ window.setViewMode('nodebook'); });
  if (btnType) btnType.addEventListener('click', function(){ window.setViewMode('types'); });
  if (btnCapture) btnCapture.addEventListener('click', function(){ if (window.cgNodebookCaptureCurrent) window.cgNodebookCaptureCurrent(); });

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
    else if (ev.key === 'n' || ev.key === 'N') { ev.preventDefault(); if (window.cgNodebookCaptureCurrent) window.cgNodebookCaptureCurrent(); }
    else if (ev.key === 'b' || ev.key === 'B') { ev.preventDefault(); window.setViewMode('nodebook'); }
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


# Nodebook constants appended below

# ------------------------------------------------------------------ #
# Nodebook — saveable, restorable "favorites" of live views.          #
# Additive only (Rule 9): empty/inert by default, existing IDs and    #
# modes untouched. Self-contained (Rule 8): thumbnails are rasterised  #
# in-browser via an SVG <foreignObject> snapshot of the live DOM (no   #
# external library/CDN). All five modes capture + restore (Rule 12).   #
# ------------------------------------------------------------------ #

_NODEBOOK_CSS = r"""
<style id="cg-nodebook-css">
.cg-nb-btn-row { display:flex; gap:6px; margin-top:6px; }
.cg-nb-btn-row .cg-btn { flex:1; }
#cg-nodebook { display:none; flex:1; height:100vh; overflow:auto; box-sizing:border-box;
  background:#1a1d24; color:#e6e6e6; padding:18px 22px; flex-direction:column; }
#cg-nb-header { display:flex; align-items:center; gap:12px; margin-bottom:14px; flex-wrap:wrap; }
#cg-nb-header h2 { margin:0; font-size:18px; font-weight:600; }
#cg-nb-count { opacity:.65; font-size:13px; }
#cg-nb-header .cg-nb-spacer { flex:1; }
#cg-nb-header button { background:#2a2f3a; color:#e6e6e6; border:1px solid #3a4151;
  border-radius:6px; padding:6px 12px; cursor:pointer; font-size:13px; }
#cg-nb-header button:hover { background:#343b48; }
#cg-nb-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(240px, 1fr));
  gap:16px; align-content:start; }
.cg-nb-card { background:#222732; border:1px solid #333b49; border-radius:10px;
  overflow:hidden; display:flex; flex-direction:column; transition:border-color .15s; }
.cg-nb-card.cg-nb-drag-over { border-color:#5b8cff; }
.cg-nb-card[draggable="true"] .cg-nb-thumb,
.cg-nb-card[draggable="true"] .cg-nb-thumb-ph { cursor:grab; }
.cg-nb-thumb { width:100%; height:140px; object-fit:cover; background:#11141a;
  display:block; border-bottom:1px solid #333b49; }
.cg-nb-thumb-ph { width:100%; height:140px; display:flex; align-items:center; justify-content:center;
  background:#11141a; color:#5b6472; font-size:34px; border-bottom:1px solid #333b49; }
.cg-nb-body { padding:10px 12px; display:flex; flex-direction:column; gap:6px; }
.cg-nb-title { font-size:14px; font-weight:600; color:#fff; background:transparent; border:none;
  border-bottom:1px solid transparent; padding:2px 0; width:100%; box-sizing:border-box; }
.cg-nb-title:focus { outline:none; border-bottom-color:#5b8cff; }
.cg-nb-meta { font-size:11px; opacity:.6; display:flex; gap:8px; align-items:center; }
.cg-nb-badge { background:#33405e; color:#bcd0ff; border-radius:4px; padding:1px 6px; font-size:10px; }
.cg-nb-actions { display:flex; gap:6px; margin-top:4px; }
.cg-nb-actions button { flex:1; background:#2a2f3a; color:#dfe6f2; border:1px solid #3a4151;
  border-radius:6px; padding:5px 0; cursor:pointer; font-size:12px; }
.cg-nb-actions button:hover { background:#343b48; }
.cg-nb-actions button.cg-nb-del:hover { background:#5a2730; border-color:#7a3540; }
#cg-nb-empty { opacity:.6; font-size:14px; padding:40px 0; text-align:center; grid-column:1/-1; }
#cg-nb-toast { position:fixed; bottom:24px; left:50%; transform:translateX(-50%);
  background:#2a2f3a; color:#fff; border:1px solid #3a4151; border-radius:8px;
  padding:10px 16px; font-size:13px; z-index:99999; opacity:0; pointer-events:none;
  transition:opacity .2s; }
#cg-nb-toast.show { opacity:1; }
body[data-theme="light"] #cg-nodebook { background:#f5f6f8; color:#222; }
body[data-theme="light"] #cg-nb-header button { background:#fff; color:#222; border-color:#cdd3dd; }
body[data-theme="light"] .cg-nb-card { background:#fff; border-color:#dde1e8; }
body[data-theme="light"] .cg-nb-thumb, body[data-theme="light"] .cg-nb-thumb-ph { background:#eef0f3; }
body[data-theme="light"] .cg-nb-title { color:#111; }
body[data-theme="light"] .cg-nb-actions button { background:#f0f2f5; color:#222; border-color:#cdd3dd; }
</style>
"""


_NODEBOOK_HTML = r"""
<div id="cg-nodebook">
  <div id="cg-nb-header">
    <h2>&#128218; Nodebook</h2>
    <span id="cg-nb-count">0 pages</span>
    <span class="cg-nb-spacer"></span>
    <button id="cg-nb-export" title="Export this Nodebook to a .nodebook.json file">&#11015; Export</button>
    <button id="cg-nb-import" title="Import pages from a .nodebook.json file">&#11014; Import</button>
    <button id="cg-nb-clear" title="Remove all pages">&#128465; Clear all</button>
    <input id="cg-nb-import-file" type="file" accept=".json,application/json" style="display:none">
  </div>
  <div id="cg-nb-grid"></div>
</div>
<div id="cg-nb-toast"></div>
"""


_NODEBOOK_JS = r"""
<script>
/* Nodebook engine. Self-contained, additive, runs after the sidebar + extras
   IIFEs so every per-mode adapter (window.CG_NB_ADAPTERS) and the cgx state
   helpers already exist. */
(function(){
  "use strict";

  window.CG_NB_ADAPTERS = window.CG_NB_ADAPTERS || {};

  /* Include + Module adapters: thin wrappers over the GetState/SetState helpers
     defined in the extras IIFE. Registered here so all five modes are present. */
  if (!window.CG_NB_ADAPTERS['inc']) window.CG_NB_ADAPTERS['inc'] = {
    label:'Include Graph',
    title:function(st){ var s = st && st.search; return s ? ('Include \u00b7 ' + s) : 'Include Graph'; },
    capture:function(){ try { return window.cgxIncGetState ? window.cgxIncGetState() : {}; } catch(e){ return {}; } },
    restore:function(st){ try { if (window.cgxIncSetState) window.cgxIncSetState(st); } catch(e){} }
  };
  if (!window.CG_NB_ADAPTERS['module']) window.CG_NB_ADAPTERS['module'] = {
    label:'Module View',
    title:function(st){
      var lvl = (st && st.level) ? (st.level.charAt(0).toUpperCase() + st.level.slice(1)) : 'Module';
      var s = st && st.search;
      return s ? (lvl + ' \u00b7 ' + s) : (lvl + ' View');
    },
    capture:function(){ try { return window.cgxModuleGetState ? window.cgxModuleGetState() : {}; } catch(e){ return {}; } },
    restore:function(st){ try { if (window.cgxModuleSetState) window.cgxModuleSetState(st); } catch(e){} }
  };

  function _nbKey(){ return (window.cgGraphId || 'default') + ':cg_nodebook_v1'; }
  function _nbAdapter(mode){ return window.CG_NB_ADAPTERS[mode] || null; }

  function _nbLoad(){
    try {
      var raw = localStorage.getItem(_nbKey());
      if (raw){ var b = JSON.parse(raw); if (b && b.pages) return b; }
    } catch(e){}
    return { version:1, graphId:(window.cgGraphId || 'default'), pages:[] };
  }
  function _nbSave(book){
    try { localStorage.setItem(_nbKey(), JSON.stringify(book)); return true; }
    catch(e){
      try {
        book.pages.forEach(function(p){ p.thumb = null; });
        localStorage.setItem(_nbKey(), JSON.stringify(book));
        _nbToast('Storage full \u2014 pages saved without thumbnails');
        return true;
      } catch(e2){ _nbToast('Could not save Nodebook (storage full)'); return false; }
    }
  }

  var _toastTimer = null;
  function _nbToast(msg){
    var t = document.getElementById('cg-nb-toast');
    if (!t) return;
    t.textContent = msg; t.classList.add('show');
    if (_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function(){ t.classList.remove('show'); }, 2400);
  }

  /* Rasterise the current live view to a JPEG dataURL. For the vis.js function
     view we use the native canvas; for DOM modes we serialise the node into an
     SVG <foreignObject> with all page styles inlined, then draw it to a canvas.
     Fully offline. On any failure we return null and the card shows a glyph. */
  var _NB_SEL = { fn:'#mynetwork', script:'#cg-script-view', varflow:'#cg-varflow-view',
                  inc:'#cgx-inc-view', module:'#cg-module-view' };
  function _nbRasterize(mode, cb){
    try {
      if (mode === 'fn'){
        var cv = document.querySelector('#mynetwork canvas');
        if (cv){ try { return cb(cv.toDataURL('image/jpeg', 0.6)); } catch(e){} }
      }
      var node = document.querySelector(_NB_SEL[mode] || '');
      if (!node){ return cb(null); }
      var rect = node.getBoundingClientRect();
      var w = Math.max(1, Math.round(rect.width)), h = Math.max(1, Math.round(rect.height));
      if (w < 4 || h < 4){ return cb(null); }
      var scale = Math.min(1, 480 / w);
      var styleText = '';
      Array.prototype.forEach.call(document.querySelectorAll('style'), function(s){ styleText += s.textContent + '\n'; });
      var clone = node.cloneNode(true);
      clone.style.display = '';
      var xml = new XMLSerializer().serializeToString(clone);
      var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + w + '" height="' + h + '">'
              + '<defs><style><![CDATA[' + styleText + ']]></style></defs>'
              + '<foreignObject width="100%" height="100%">'
              + '<div xmlns="http://www.w3.org/1999/xhtml" style="width:' + w + 'px;height:' + h + 'px;">'
              + xml + '</div></foreignObject></svg>';
      var img = new Image();
      img.onload = function(){
        try {
          var canvas = document.createElement('canvas');
          canvas.width = Math.max(1, Math.round(w * scale));
          canvas.height = Math.max(1, Math.round(h * scale));
          var ctx = canvas.getContext('2d');
          ctx.fillStyle = '#11141a'; ctx.fillRect(0, 0, canvas.width, canvas.height);
          ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
          cb(canvas.toDataURL('image/jpeg', 0.6));
        } catch(e){ cb(null); }
      };
      img.onerror = function(){ cb(null); };
      img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
    } catch(e){ cb(null); }
  }

  function _nbCapture(targetId){
    var mode = window.CG_NB_LAST_MODE || 'fn';
    var ad = _nbAdapter(mode);
    if (!ad){ _nbToast('Nothing to capture in this mode'); return; }
    var state = {};
    try { state = ad.capture() || {}; } catch(e){ state = {}; }
    var title;
    try { title = ad.title ? ad.title(state) : (ad.label || mode); } catch(e){ title = ad.label || mode; }
    _nbRasterize(mode, function(thumb){
      var book = _nbLoad();
      if (targetId){
        var pg = null;
        for (var i = 0; i < book.pages.length; i++){ if (book.pages[i].id === targetId){ pg = book.pages[i]; break; } }
        if (pg){
          pg.state = state; pg.thumb = thumb || pg.thumb; pg.updatedAt = Date.now();
          _nbSave(book); _nbToast('Updated "' + pg.title + '"');
          _nbRenderGalleryIfOpen();
        }
        return;
      }
      var page = { id:'nb_' + Date.now() + '_' + Math.random().toString(36).slice(2,7),
                   title:title, mode:mode, thumb:(thumb || null),
                   createdAt:Date.now(), updatedAt:Date.now(), state:state };
      book.pages.push(page);
      _nbSave(book);
      _nbToast('Captured "' + title + '" \u2192 Nodebook');
      _nbRenderGalleryIfOpen();
    });
  }
  window.cgNodebookCaptureCurrent = function(){ _nbCapture(null); };

  function _nbOpenPage(page){
    if (!page) return;
    var st = page.state || {};
    /* Module/folder pages must restore onto their captured slot (not whatever
       slot happens to be active) so separate captures stay independent. */
    if (page.mode === 'module' && st.builtFor && window.cgxModuleActivateBuilt){
      window.cgxModuleActivateBuilt(st.builtFor);
    } else {
      try { window.setViewMode(page.mode); } catch(e){}
    }
    var ad = _nbAdapter(page.mode);
    if (ad && ad.restore){
      setTimeout(function(){ try { ad.restore(page.state); } catch(e){} }, 300);
    }
  }

  function _nbDelete(id){
    var book = _nbLoad();
    book.pages = book.pages.filter(function(p){ return p.id !== id; });
    _nbSave(book); _nbRenderGallery();
  }
  function _nbRename(id, title){
    var book = _nbLoad();
    book.pages.forEach(function(p){ if (p.id === id){ p.title = title; p.updatedAt = Date.now(); } });
    _nbSave(book);
  }

  var _dragId = null;
  function _nbReorder(srcId, beforeId){
    if (srcId === beforeId) return;
    var book = _nbLoad();
    var idx = -1, i;
    for (i = 0; i < book.pages.length; i++){ if (book.pages[i].id === srcId){ idx = i; break; } }
    if (idx < 0) return;
    var moved = book.pages.splice(idx, 1)[0];
    if (beforeId == null){ book.pages.push(moved); }
    else {
      var bi = -1;
      for (i = 0; i < book.pages.length; i++){ if (book.pages[i].id === beforeId){ bi = i; break; } }
      if (bi < 0) book.pages.push(moved); else book.pages.splice(bi, 0, moved);
    }
    _nbSave(book); _nbRenderGallery();
  }

  function _fmtDate(ts){
    try { var d = new Date(ts); return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}); }
    catch(e){ return ''; }
  }

  function _nbCard(page){
    var card = document.createElement('div');
    card.className = 'cg-nb-card';
    card.setAttribute('draggable', 'true');
    card.dataset.id = page.id;

    if (page.thumb){
      var img = document.createElement('img');
      img.className = 'cg-nb-thumb'; img.src = page.thumb; img.alt = page.title;
      card.appendChild(img);
    } else {
      var ph = document.createElement('div');
      ph.className = 'cg-nb-thumb-ph'; ph.textContent = '\uD83D\uDCC4';
      card.appendChild(ph);
    }

    var body = document.createElement('div'); body.className = 'cg-nb-body';
    var titleInp = document.createElement('input');
    titleInp.className = 'cg-nb-title'; titleInp.value = page.title || '(untitled)';
    titleInp.title = 'Click to rename';
    titleInp.addEventListener('change', function(){ _nbRename(page.id, titleInp.value); });
    titleInp.addEventListener('keydown', function(ev){ if (ev.key === 'Enter'){ titleInp.blur(); } });
    titleInp.addEventListener('mousedown', function(ev){ ev.stopPropagation(); });
    body.appendChild(titleInp);

    var meta = document.createElement('div'); meta.className = 'cg-nb-meta';
    var badge = document.createElement('span'); badge.className = 'cg-nb-badge';
    var ad = _nbAdapter(page.mode);
    var badgeText = (ad && ad.label) ? ad.label : page.mode;
    if (page.mode === 'module' && page.state && page.state.level){
      badgeText = page.state.level.charAt(0).toUpperCase() + page.state.level.slice(1) + ' View';
    }
    badge.textContent = badgeText;
    meta.appendChild(badge);
    var dt = document.createElement('span'); dt.textContent = _fmtDate(page.updatedAt || page.createdAt);
    meta.appendChild(dt);
    body.appendChild(meta);

    var actions = document.createElement('div'); actions.className = 'cg-nb-actions';
    var openB = document.createElement('button'); openB.textContent = 'Open';
    openB.addEventListener('click', function(){ _nbOpenPage(page); });
    var updB = document.createElement('button'); updB.textContent = 'Update';
    updB.title = 'Re-capture the current live view into this page (switch to its mode first)';
    updB.addEventListener('click', function(){
      if ((window.CG_NB_LAST_MODE || 'fn') !== page.mode){
        _nbToast('Open this page first, arrange it, then Update');
        return;
      }
      /* Folder vs Module share mode 'module' — guard on the captured level too so
         Update never clobbers a folder page with module state (or vice versa). */
      if (page.mode === 'module' && page.state && page.state.builtFor &&
          window.cgxModuleGetState){
        var cur = null;
        try { cur = window.cgxModuleGetState(); } catch(e){}
        if (cur && cur.builtFor && cur.builtFor !== page.state.builtFor){
          _nbToast('Open this page first, arrange it, then Update');
          return;
        }
      }
      _nbCapture(page.id);
    });
    var delB = document.createElement('button'); delB.className = 'cg-nb-del'; delB.textContent = 'Delete';
    delB.addEventListener('click', function(){ _nbDelete(page.id); });
    actions.appendChild(openB); actions.appendChild(updB); actions.appendChild(delB);
    body.appendChild(actions);
    card.appendChild(body);

    card.addEventListener('dragstart', function(ev){ _dragId = page.id; try { ev.dataTransfer.effectAllowed = 'move'; } catch(e){} });
    card.addEventListener('dragover', function(ev){ ev.preventDefault(); card.classList.add('cg-nb-drag-over'); });
    card.addEventListener('dragleave', function(){ card.classList.remove('cg-nb-drag-over'); });
    card.addEventListener('drop', function(ev){ ev.preventDefault(); card.classList.remove('cg-nb-drag-over');
      if (_dragId && _dragId !== page.id){ _nbReorder(_dragId, page.id); } _dragId = null; });
    return card;
  }

  function _nbRenderGallery(){
    var grid = document.getElementById('cg-nb-grid');
    var count = document.getElementById('cg-nb-count');
    if (!grid) return;
    var book = _nbLoad();
    grid.innerHTML = '';
    if (count) count.textContent = book.pages.length + (book.pages.length === 1 ? ' page' : ' pages');
    if (!book.pages.length){
      var empty = document.createElement('div'); empty.id = 'cg-nb-empty';
      empty.textContent = 'No pages yet. Open any view, arrange it, then press \u201c\u2795 Capture\u201d (or N) to add it here.';
      grid.appendChild(empty);
      return;
    }
    book.pages.forEach(function(p){ grid.appendChild(_nbCard(p)); });
  }
  window.cgNodebookRenderGallery = _nbRenderGallery;
  function _nbRenderGalleryIfOpen(){
    var nb = document.getElementById('cg-nodebook');
    if (nb && nb.style.display !== 'none') _nbRenderGallery();
  }
  window.cgNodebookOpen = function(){ _nbRenderGallery(); };

  function _nbExport(){
    var book = _nbLoad();
    var blob = new Blob([JSON.stringify(book, null, 2)], {type:'application/json'});
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = (window.cgGraphId || 'graph') + '.nodebook.json';
    document.body.appendChild(a); a.click();
    setTimeout(function(){ document.body.removeChild(a); URL.revokeObjectURL(url); }, 100);
  }
  function _nbImport(file){
    var reader = new FileReader();
    reader.onload = function(){
      try {
        var imported = JSON.parse(reader.result);
        if (!imported || !imported.pages){ _nbToast('Not a valid .nodebook.json file'); return; }
        var book = _nbLoad();
        var existing = {}; book.pages.forEach(function(p){ existing[p.id] = 1; });
        var added = 0;
        imported.pages.forEach(function(p){ if (p && p.id && !existing[p.id]){ book.pages.push(p); added++; } });
        _nbSave(book); _nbRenderGallery();
        _nbToast('Imported ' + added + ' page' + (added === 1 ? '' : 's'));
      } catch(e){ _nbToast('Could not read file'); }
    };
    reader.readAsText(file);
  }

  function _wireNodebook(){
    var exp = document.getElementById('cg-nb-export');
    if (exp) exp.addEventListener('click', _nbExport);
    var imp = document.getElementById('cg-nb-import');
    var impFile = document.getElementById('cg-nb-import-file');
    if (imp && impFile){
      imp.addEventListener('click', function(){ impFile.click(); });
      impFile.addEventListener('change', function(){ if (impFile.files && impFile.files[0]){ _nbImport(impFile.files[0]); impFile.value = ''; } });
    }
    var clr = document.getElementById('cg-nb-clear');
    if (clr) clr.addEventListener('click', function(){
      var book = _nbLoad();
      if (!book.pages.length){ return; }
      if (window.confirm('Remove all ' + book.pages.length + ' Nodebook pages? This cannot be undone.')){
        book.pages = []; _nbSave(book); _nbRenderGallery();
      }
    });
  }

  if (document.readyState === 'loading'){ document.addEventListener('DOMContentLoaded', _wireNodebook); }
  else { _wireNodebook(); }
})();
</script>
"""


# ====================================================================== #
# Type Nodes Mode (M2) — self-contained card view for the type graph.     #
# Renders struct/union/enum/typedef/class cards with member rows, edges,  #
# drag / pan / zoom, search over type + member names, kind filters, and a #
# Nodebook capture/restore adapter. Reads the CG_TYPE_DATA payload.        #
# ====================================================================== #
_TYPE_VIEW_CSS = r"""
<style id="cg-type-view-css">
#cg-type-view {
  display: none; flex: 1; height: 100vh; flex-direction: column;
  overflow: hidden; background: #334155; position: relative;
}
#cg-tv-topbar {
  padding: 12px 16px 11px; background: #23272e;
  border-bottom: 1px solid #2d3139; flex-shrink: 0;
}
#cg-tv-topbar h2 {
  font-size: 10px; font-weight: 600; color: #8090a0;
  text-transform: uppercase; letter-spacing: 0.7px; margin: 0 0 9px;
}
#cg-tv-topbar-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
#cg-tv-search-wrap { position: relative; display: flex; gap: 6px; flex: 1; min-width: 200px; }
#cg-tv-search-input {
  flex: 1; padding: 8px 12px; border: 1px solid #3d4451; border-radius: 5px;
  background: #1a1d23; color: #e0e0e0; font-size: 13px; outline: none;
}
#cg-tv-search-input:focus { border-color: #4A90D9; }
#cg-tv-search-clear {
  background: #2d3139; color: #8090a0; border: none; border-radius: 5px;
  padding: 0 10px; cursor: pointer; font-size: 14px;
}
#cg-tv-dropdown {
  display: none; position: absolute; top: 40px; left: 0; right: 0; z-index: 200;
  background: #23272e; border: 1px solid #3d4451; border-radius: 6px;
  max-height: 320px; overflow-y: auto; box-shadow: 0 8px 24px rgba(0,0,0,0.6);
}
#cg-tv-dropdown.open { display: block; }
.cg-tv-dd-item { padding: 7px 12px; cursor: pointer; border-bottom: 1px solid #2d3139; }
.cg-tv-dd-item:last-child { border-bottom: none; }
.cg-tv-dd-item:hover, .cg-tv-dd-item.active { background: #1e2530; }
.cg-tv-dd-nm { font-family: monospace; font-size: 12px; color: #74B3F7; font-weight: 700; }
.cg-tv-dd-sub { font-size: 10px; color: #6a7a8a; }
.cg-tv-btn {
  background: #2d3139; color: #c0c8d4; border: 1px solid #3d4451; border-radius: 5px;
  padding: 7px 11px; cursor: pointer; font-size: 12px; white-space: nowrap;
}
.cg-tv-btn:hover { background: #353b47; }
.cg-tv-btn.active { background: #1a3a5a; border-color: #4A90D9; color: #74B3F7; }
#cg-tv-kindrow { display: flex; gap: 6px; align-items: center; margin-top: 8px; flex-wrap: wrap; }
.cg-tv-kind-lbl { font-size: 10px; color: #8090a0; text-transform: uppercase; letter-spacing: 0.5px; }
.cg-tv-kind-btn {
  font-size: 11px; padding: 3px 9px; border-radius: 12px; cursor: pointer;
  border: 1px solid #3d4451; background: #1a1d23; color: #c0c8d4;
}
.cg-tv-kind-btn.off { opacity: 0.4; }
#cg-tv-stats { margin-left: auto; font-size: 10px; color: #5a6a7a; }
#cg-tv-graph { flex: 1; position: relative; overflow: hidden; }
#cg-tv-viewport {
  width: 100%; height: 100%; position: absolute; inset: 0; overflow: hidden;
  cursor: grab; -webkit-user-select: none; user-select: none;
}
#cg-tv-viewport.tv-panning { cursor: grabbing; }
#cg-tv-canvas { position: absolute; top: 0; left: 0; transform-origin: 0 0; }
#cg-tv-svg { position: absolute; top: 0; left: 0; overflow: visible; pointer-events: none; }
.cg-tv-edge { pointer-events: stroke; cursor: pointer; transition: stroke-width .12s, opacity .12s, stroke .12s; }
.cg-tv-edge:hover, .cg-tv-edge.tv-edge-active { stroke: #F7D774 !important; opacity: 1 !important; stroke-width: 3 !important; }
.cg-tv-elabel { font-family: monospace; font-size: 10px; fill: #8090a0; pointer-events: none; }
#cg-tv-placeholder {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%);
  text-align: center; color: #5a6a7a; font-size: 15px; line-height: 1.7;
}
.cg-tv-card {
  position: absolute; z-index: 10; width: 240px;
  background: #23272e; border: 1px solid #2d3139; border-radius: 8px;
  overflow: hidden; box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  transition: box-shadow .15s, opacity .15s;
}
.cg-tv-card:hover { box-shadow: 0 6px 22px rgba(0,0,0,0.7); }
.cg-tv-card.tv-dim { opacity: 0.16; }
.cg-tv-card.tv-root { border-color: #FFD700; box-shadow: 0 0 0 2px rgba(255,215,0,0.3); }
.cg-tv-card.tv-match { border-color: #27AE60; box-shadow: 0 0 0 2px rgba(39,174,96,0.4); }
.cg-tv-card.tv-flash { border-color: #F7D774; box-shadow: 0 0 0 3px rgba(247,215,116,0.6); }
.cg-tv-hd {
  display: flex; align-items: center; gap: 7px; padding: 8px 10px;
  background: #1e2530; border-bottom: 1px solid #2d3139; cursor: move;
}
.cg-tv-hd:hover { background: #252c3a; }
.cg-tv-collapse {
  flex-shrink: 0; width: 15px; height: 15px; border: none; background: none;
  color: #6a7a8a; font-size: 10px; cursor: pointer; padding: 0; line-height: 1;
}
.cg-tv-collapse:hover { color: #c0c8d4; }
.cg-tv-card.tv-collapsed .cg-tv-body { display: none; }
.cg-tv-name { font-family: monospace; font-size: 13px; font-weight: 700; color: #e0e0e0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }
.cg-tv-name.k-struct  { color: #4A90D9; }
.cg-tv-name.k-union   { color: #C77DD9; }
.cg-tv-name.k-enum    { color: #E0A458; }
.cg-tv-name.k-typedef { color: #58C9B9; }
.cg-tv-name.k-class   { color: #7FB069; }
.cg-tv-badge { font-size: 9px; font-weight: 700; padding: 1px 6px; border-radius: 3px; flex-shrink: 0; letter-spacing: .3px; color: #10141a; }
.cg-tv-badge.k-struct  { background: #4A90D9; }
.cg-tv-badge.k-union   { background: #C77DD9; }
.cg-tv-badge.k-enum    { background: #E0A458; }
.cg-tv-badge.k-typedef { background: #58C9B9; }
.cg-tv-badge.k-class   { background: #7FB069; }
.cg-tv-sub { padding: 2px 10px 0; font-family: monospace; font-size: 10px; color: #6a7a8a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cg-tv-file { padding: 1px 10px 5px; font-size: 9px; color: #5a6a7a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cg-tv-file.cg-tv-file-link { cursor: pointer; border-bottom: 1px dotted #3a4a60; }
.cg-tv-file.cg-tv-file-link:hover { color: #9eb4c8; border-bottom-color: #74B3F7; }
.cg-tv-body { padding: 3px 0; max-height: 340px; overflow-y: auto; }
.cg-tv-row {
  display: flex; align-items: baseline; gap: 6px; padding: 3px 10px;
  font-family: monospace; font-size: 11px; border-bottom: 1px solid #23272e;
}
.cg-tv-row:last-child { border-bottom: none; }
.cg-tv-row.tv-row-hi { background: #342f18; }
.cg-tv-mname { color: #e0e0e0; }
.cg-tv-mname.cg-tv-enum-const { color: #D4C977; font-weight: 600; }
.cg-tv-mtype { color: #8090a0; margin-left: auto; text-align: right; }
.cg-tv-chip { color: #74B3F7; cursor: pointer; border-bottom: 1px dotted #4A90D9; }
.cg-tv-chip:hover { color: #a9d0fb; }
.cg-tv-eval { color: #E0A458; margin-left: auto; }
.cg-tv-more { padding: 4px 10px; font-size: 10px; color: #5a6a7a; cursor: pointer; }
.cg-tv-more:hover { color: #8090a0; }

/* syntax highlighting for type text in member rows */
.cg-tv-type-tok { font-family: monospace; }
.cg-tv-type-tok.k-keyword-struct { color: #4A90D9; font-weight: 600; }
.cg-tv-type-tok.k-keyword-union { color: #C77DD9; font-weight: 600; }
.cg-tv-type-tok.k-keyword-enum { color: #E0A458; font-weight: 600; }
.cg-tv-type-tok.k-keyword-qual { color: #B896DC; }
.cg-tv-type-tok.k-builtin { color: #74B3F7; }
.cg-tv-type-tok.k-type-struct { color: #7FB069; }
.cg-tv-type-tok.k-type-union { color: #9CDCFE; }
.cg-tv-type-tok.k-type-enum { color: #D4C977; }
.cg-tv-type-tok.k-type-typedef { color: #58C9B9; }
.cg-tv-type-tok.k-type-class { color: #7FB069; }
.cg-tv-type-tok.k-punct { color: #8090a0; }
.cg-tv-eval { color: #E0A458; }
.cg-tv-eval.k-enum-const { color: #D4C977; font-weight: 600; }


/* marquee box (multi-select) */
.cg-tv-marquee-box {
  position: absolute; z-index: 9999; pointer-events: none;
  border: 1px solid #74b3f7; background: rgba(116,179,247,0.18);
  box-shadow: inset 0 0 0 1px rgba(116,179,247,0.25);
}

.cg-tv-card.tv-multi-selected {
  border-color: #f59e0b; box-shadow: 0 0 0 2px rgba(245,158,11,0.3);
}

/* anonymous inline group + extract button (TYP-4) */
.cg-tv-anongroup { margin: 2px 6px 4px 16px; border-left: 2px solid #4A90D9;
  background: rgba(74,144,217,0.06); border-radius: 0 4px 4px 0; }
.cg-tv-anonhd { display: flex; align-items: center; gap: 6px; padding: 3px 8px;
  font-family: monospace; font-size: 10px; color: #74B3F7; }
.cg-tv-anonlbl { flex: 1; }
.cg-tv-extract { font-size: 9px; padding: 1px 6px; border-radius: 8px; cursor: pointer;
  border: 1px solid #4A90D9; background: #1a3a5a; color: #74B3F7; }
.cg-tv-extract:hover { background: #235079; }

/* isolate breadcrumb (TYP-2) */
#cg-tv-breadcrumb { display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  margin-top: 8px; font-size: 11px; color: #8090a0; }
.cg-tv-crumb { cursor: pointer; color: #74B3F7; border-bottom: 1px dotted #4A90D9; }
.cg-tv-crumb:hover { color: #a9d0fb; }
.cg-tv-crumb-cur { color: #F7D774; border-bottom-color: #F7D774; font-weight: 700; }
.cg-tv-crumb-all { color: #c0c8d4; border-bottom: none; font-weight: 700; }
.cg-tv-crumb-sep { color: #5a6a7a; }
.cg-tv-crumb-cnt { margin-left: auto; font-size: 10px; color: #5a6a7a; }

/* right-click context menu (TYP-3) */
.cg-tv-ctx { position: fixed; z-index: 9999; min-width: 200px; background: #23272e;
  border: 1px solid #3d4451; border-radius: 6px; box-shadow: 0 10px 30px rgba(0,0,0,0.6);
  padding: 4px 0; font-size: 12px; }
.cg-tv-ctx-item { display: flex; align-items: baseline; gap: 8px; padding: 7px 12px; cursor: pointer; }
.cg-tv-ctx-item:hover { background: #1e2530; }
.cg-tv-ctx-dis { opacity: 0.4; cursor: default; }
.cg-tv-ctx-dis:hover { background: none; }
.cg-tv-ctx-lbl { color: #e0e0e0; flex: 1; }
.cg-tv-ctx-sub { color: #6a7a8a; font-size: 10px; }

/* ---------- light theme (cards follow dark/light toggle) ---------- */
body[data-theme="light"] #cg-type-view { background: #dde3ea; }
body[data-theme="light"] #cg-tv-topbar { background: #eef2f7; border-bottom-color: #c8d4de; }
body[data-theme="light"] #cg-tv-topbar h2 { color: #5a6a7a; }
body[data-theme="light"] #cg-tv-search-input { background: #fff; color: #1a2535; border-color: #c8d4de; }
body[data-theme="light"] #cg-tv-search-clear { background: #dde6f0; color: #5a6a7a; }
body[data-theme="light"] #cg-tv-dropdown { background: #fff; border-color: #c8d4de; box-shadow: 0 8px 24px rgba(0,0,0,0.18); }
body[data-theme="light"] .cg-tv-dd-item { border-bottom-color: #e5edf4; }
body[data-theme="light"] .cg-tv-dd-item:hover, body[data-theme="light"] .cg-tv-dd-item.active { background: #eef4fb; }
body[data-theme="light"] .cg-tv-dd-nm { color: #1d4ed8; }
body[data-theme="light"] .cg-tv-btn { background: #e6edf5; color: #2d3a4a; border-color: #c8d4de; }
body[data-theme="light"] .cg-tv-btn:hover { background: #d8e3f0; }
body[data-theme="light"] .cg-tv-btn.active { background: #dbeafe; border-color: #3b82f6; color: #1d4ed8; }
body[data-theme="light"] .cg-tv-kind-btn { background: #fff; color: #2d3a4a; border-color: #c8d4de; }
body[data-theme="light"] .cg-tv-card { background: #fff; border-color: #c8d4de; box-shadow: 0 4px 14px rgba(0,0,0,0.10); }
body[data-theme="light"] .cg-tv-card:hover { box-shadow: 0 6px 20px rgba(0,0,0,0.16); }
body[data-theme="light"] .cg-tv-card.tv-root { border-color: #d4a017; box-shadow: 0 0 0 2px rgba(212,160,23,0.3); }
body[data-theme="light"] .cg-tv-hd { background: #edf2f7; border-bottom-color: #c8d4de; }
body[data-theme="light"] .cg-tv-hd:hover { background: #e2eaf2; }
body[data-theme="light"] .cg-tv-collapse { color: #8090a0; }
body[data-theme="light"] .cg-tv-collapse:hover { color: #2d3a4a; }
body[data-theme="light"] .cg-tv-name { color: #1a2535; }
body[data-theme="light"] .cg-tv-name.k-struct  { color: #1d4ed8; }
body[data-theme="light"] .cg-tv-name.k-union   { color: #7c3aed; }
body[data-theme="light"] .cg-tv-name.k-enum    { color: #b45309; }
body[data-theme="light"] .cg-tv-name.k-typedef { color: #0d9488; }
body[data-theme="light"] .cg-tv-name.k-class   { color: #059669; }
body[data-theme="light"] .cg-tv-sub { color: #7a8898; }
body[data-theme="light"] .cg-tv-file { color: #94a3b0; }
body[data-theme="light"] .cg-tv-file.cg-tv-file-link { border-bottom-color: #b8c5d4; }
body[data-theme="light"] .cg-tv-file.cg-tv-file-link:hover { color: #64748b; border-bottom-color: #3b82f6; }
body[data-theme="light"] .cg-tv-row { border-bottom-color: #eef2f6; }
body[data-theme="light"] .cg-tv-row.tv-row-hi { background: #fef3c7; }
body[data-theme="light"] .cg-tv-mname { color: #1a2535; }
body[data-theme="light"] .cg-tv-mname.cg-tv-enum-const { color: #ca8a04; }
body[data-theme="light"] .cg-tv-mtype { color: #4a6070; }
body[data-theme="light"] .cg-tv-chip { color: #1d4ed8; border-bottom-color: #3b82f6; }
body[data-theme="light"] .cg-tv-chip:hover { color: #2563eb; }
body[data-theme="light"] .cg-tv-eval { color: #b45309; }
body[data-theme="light"] .cg-tv-more { color: #94a3b0; }
body[data-theme="light"] .cg-tv-type-tok.k-keyword-struct { color: #1d4ed8; }
body[data-theme="light"] .cg-tv-type-tok.k-keyword-union { color: #7c3aed; }
body[data-theme="light"] .cg-tv-type-tok.k-keyword-enum { color: #b45309; }
body[data-theme="light"] .cg-tv-type-tok.k-keyword-qual { color: #6d28d9; }
body[data-theme="light"] .cg-tv-type-tok.k-builtin { color: #2563eb; }
body[data-theme="light"] .cg-tv-type-tok.k-type-struct { color: #059669; }
body[data-theme="light"] .cg-tv-type-tok.k-type-union { color: #0891b2; }
body[data-theme="light"] .cg-tv-type-tok.k-type-enum { color: #ca8a04; }
body[data-theme="light"] .cg-tv-type-tok.k-type-typedef { color: #0d9488; }
body[data-theme="light"] .cg-tv-type-tok.k-type-class { color: #059669; }
body[data-theme="light"] .cg-tv-type-tok.k-punct { color: #6b7280; }
body[data-theme="light"] .cg-tv-marquee-box { border-color: #3b82f6; background: rgba(59,130,246,0.1); }
body[data-theme="light"] .cg-tv-card.tv-multi-selected { border-color: #f59e0b; box-shadow: 0 0 0 2px rgba(245,158,11,0.3); }
body[data-theme="light"] .cg-tv-anongroup { background: rgba(59,130,246,0.06); border-left-color: #3b82f6; }
body[data-theme="light"] .cg-tv-anonhd { color: #1d4ed8; }
body[data-theme="light"] .cg-tv-extract { background: #dbeafe; color: #1d4ed8; border-color: #3b82f6; }
body[data-theme="light"] .cg-tv-ctx { background: #fff; border-color: #c8d4de; box-shadow: 0 10px 30px rgba(0,0,0,0.18); }
body[data-theme="light"] .cg-tv-ctx-item:hover { background: #eef4fb; }
body[data-theme="light"] .cg-tv-ctx-lbl { color: #1a2535; }
body[data-theme="light"] .cg-tv-elabel { fill: #64748b; }
</style>
"""

_TYPE_VIEW_JS = r"""
<script id="cg-type-view-js">
(function(){
  var TD = CG_TYPE_DATA;
  if (!TD || typeof TD !== 'object') TD = {};
  if (!TD.types) TD.types = {};
  if (!TD.edges) TD.edges = [];
  if (!TD.roots) TD.roots = [];

  var GID   = (window.cgGraphId || 'g');
  var POSKEY = 'cgTypePos:' + GID;
  var COLKEY = 'cgTypeCollapse:' + GID;
  var LAYER_EDGE = { contains_value:1, array_of:1, alias_of:1, inherits:1 };
  var KIND_BADGE = { struct:'struct', union:'union', enum:'enum', typedef:'typedef', class:'class' };
  var EDGE_STYLE = {
    contains_value:   { color:'#74B3F7', dash:'',        head:'tv-arrow' },
    contains_pointer: { color:'#F9B06E', dash:'6,4',     head:'tv-arrow-p' },
    array_of:         { color:'#58D68D', dash:'',        head:'tv-arrow' },
    alias_of:         { color:'#C0C8D4', dash:'2,4',     head:'tv-arrow' },
    inherits:         { color:'#E0A458', dash:'7,3,2,3', head:'tv-arrow' }
  };
  var NODE_GAP = 56, LAYER_VGAP = 80, MARGIN = 60;

  var view, vp, canvas, svg, dd, searchInput, statsEl, placeholder, bc, ctxMenu;
  var built = false;
  var cards = {};                 // type_id -> {el, x, y, w, h}
  var kindOn = { struct:true, union:true, enum:true, typedef:true, class:true };
  var pan = { x: 0, y: 0, s: 1 };
  var extracted = {};             // anon type_id -> 1 once popped out as its own card (TYP-4)
  var isoActive = false, isoSet = {}, isoStack = [];   // isolate + breadcrumb (TYP-2)
  var _tvTypeNameToTid = null;    // lower-case name/alias/tag -> tid (TYP-9)
  var _tvMultiSel = {};           // type_id -> 1 for multi-selected cards
  var _tvMarquee = null;          // marquee box state during drag
  var _panning = false, _panStart = null;  // pan state

  function _h(tag, attrs, txt){
    var e = document.createElement(tag);
    if (attrs) for (var k in attrs){ if (k === 'class') e.className = attrs[k]; else e.setAttribute(k, attrs[k]); }
    if (txt != null) e.textContent = txt;
    return e;
  }
  function _loadJSON(key){ try { return JSON.parse(localStorage.getItem(key) || '{}'); } catch(e){ return {}; } }
  function _saveJSON(key, obj){ try { localStorage.setItem(key, JSON.stringify(obj)); } catch(e){} }

  function _tvEnsureTypeIndex(){
    if (_tvTypeNameToTid) return;
    _tvTypeNameToTid = {};
    Object.keys(TD.types).forEach(function(tid){
      var td = TD.types[tid] || {};
      [td.name, td.tag].forEach(function(n){
        var k = String(n || '').trim().toLowerCase();
        if (k && _tvTypeNameToTid[k] == null) _tvTypeNameToTid[k] = tid;
      });
      (td.aliases || []).forEach(function(a){
        var k = String(a || '').trim().toLowerCase();
        if (k && _tvTypeNameToTid[k] == null) _tvTypeNameToTid[k] = tid;
      });
    });
  }

  function _tvFindTypeByName(typeText){
    _tvEnsureTypeIndex();
    var raw = String(typeText || '').trim();
    if (!raw) return null;
    var exact = _tvTypeNameToTid[raw.toLowerCase()];
    if (exact) return exact;
    var kw = {
      struct:1, class:1, union:1, enum:1, typedef:1, const:1, volatile:1,
      unsigned:1, signed:1, static:1, extern:1, register:1, restrict:1,
      inline:1, mutable:1, int:1, char:1, float:1, double:1, short:1,
      long:1, void:1, bool:1, wchar_t:1, size_t:1, ssize_t:1, int8_t:1,
      int16_t:1, int32_t:1, int64_t:1, uint8_t:1, uint16_t:1, uint32_t:1,
      uint64_t:1, uintptr_t:1, intptr_t:1, ptrdiff_t:1
    };
    var toks = raw.match(/[A-Za-z_][A-Za-z0-9_]*/g) || [];
    for (var i = toks.length - 1; i >= 0; --i) {
      var t = toks[i].toLowerCase();
      if (kw[t]) continue;
      if (_tvTypeNameToTid[t]) return _tvTypeNameToTid[t];
    }
    return null;
  }

  function _jumpTypeToInclude(filePath){
    var fp = String(filePath || '').trim();
    if (!fp || typeof window.setViewMode !== 'function') return;
    window.setViewMode('inc');
    setTimeout(function(){
      try { if (window.cgxIncCenter) window.cgxIncCenter(fp); } catch(e) {}
      try { if (window.cgxIncIsolate) window.cgxIncIsolate(fp, 9999, 'down'); } catch(e) {}
    }, 120);
  }

  function _memberTypeText(m){
    var t = m.type_text || '';
    if (m.is_func_ptr && m.func_ptr_sig) return m.func_ptr_sig;
    for (var i = 0; i < (m.is_pointer || 0); ++i) t += ' *';
    (m.array_dims || []).forEach(function(d){ t += '[' + d + ']'; });
    if (m.bitfield_width != null) t += ' : ' + m.bitfield_width;
    return t.trim();
  }

  function _isInlineAnon(tid){
    var td = TD.types[tid];
    return !!(td && td.is_anon && td.parent && TD.types[td.parent] && !extracted[tid]);
  }

  function _buildCards(){
    Object.keys(TD.types).forEach(function(tid){
      if (_isInlineAnon(tid)) return;   // rendered inline inside its parent (TYP-4)
      _buildCardFor(tid);
    });
  }

  function _buildCardFor(tid){
      var col = _loadJSON(COLKEY);
      var td = TD.types[tid];
      var card = _h('div', { 'class':'cg-tv-card', 'data-tid': tid });
      if (TD.roots.indexOf(tid) >= 0) card.classList.add('tv-root');
      if (col[tid]) card.classList.add('tv-collapsed');

      var hd = _h('div', { 'class':'cg-tv-hd' });
      var cb = _h('button', { 'class':'cg-tv-collapse', title:'Collapse' }, col[tid] ? '\u25B8' : '\u25BE');
      cb.addEventListener('mousedown', function(ev){ ev.stopPropagation(); });
      cb.addEventListener('click', function(ev){
        ev.stopPropagation();
        card.classList.toggle('tv-collapsed');
        var open = !card.classList.contains('tv-collapsed');
        cb.textContent = open ? '\u25BE' : '\u25B8';
        var c = _loadJSON(COLKEY); if (open) delete c[tid]; else c[tid] = 1; _saveJSON(COLKEY, c);
        _measureOne(tid); _drawEdges();
      });
      hd.appendChild(cb);
      var nameSpan = _h('span', { 'class':'cg-tv-name k-' + (td.kind || 'struct'), title: td.name }, td.name);
      hd.appendChild(nameSpan);
      hd.appendChild(_h('span', { 'class':'cg-tv-badge k-' + (td.kind || 'struct') }, KIND_BADGE[td.kind] || td.kind || ''));
      card.appendChild(hd);

      if (td.tag && td.aliases && td.aliases.length && td.tag !== td.name)
        card.appendChild(_h('div', { 'class':'cg-tv-sub' }, td.kind + ' ' + td.tag));
      if (td.file) {
        var fchip = _h('div', { 'class':'cg-tv-file cg-tv-file-link', title: td.file + (td.line ? ':' + td.line : '') + ' - Open in Include Graph' },
                       td.file.split('/').pop() + (td.line ? ':' + td.line : ''));
        fchip.addEventListener('mousedown', function(ev){ ev.stopPropagation(); });
        fchip.addEventListener('click', function(ev){ ev.stopPropagation(); _jumpTypeToInclude(td.file); });
        card.appendChild(fchip);
      }

      var body = _h('div', { 'class':'cg-tv-body' });
      if (td.kind === 'enum') {
        var evs = td.enum_values || [];
        var shownE = Math.min(evs.length, 40);
        for (var e = 0; e < shownE; ++e) {
          var row = _h('div', { 'class':'cg-tv-row' });
          var nameSpan = _h('span', { 'class':'cg-tv-mname cg-tv-enum-const' }, evs[e][0]);
          row.appendChild(nameSpan);
          if (evs[e][1] != null && evs[e][1] !== '')
            row.appendChild(_h('span', { 'class':'cg-tv-eval' }, '= ' + evs[e][1]));
          body.appendChild(row);
        }
        if (evs.length > shownE) body.appendChild(_h('div', { 'class':'cg-tv-more' }, '\u2026 ' + (evs.length - shownE) + ' more'));
      } else {
        var mem = td.members || [];
        var shownN = 0, LIMIT = 14;
        var addMore = function(){
          var more = _h('div', { 'class':'cg-tv-more' }, '\u2026 ' + (mem.length - LIMIT) + ' more');
          more.addEventListener('mousedown', function(ev){ ev.stopPropagation(); });
          more.addEventListener('click', function(ev){
            ev.stopPropagation();
            for (var j = LIMIT; j < mem.length; ++j) _appendMember(body, mem[j], tid, more);
            more.remove(); _measureOne(tid); _drawEdges();
          });
          body.appendChild(more);
        };
        for (var mi = 0; mi < mem.length; ++mi) {
          if (mi >= LIMIT) { addMore(); break; }
          _appendMember(body, mem[mi], tid); shownN++;
        }
        if (!mem.length) body.appendChild(_h('div', { 'class':'cg-tv-more' }, '(no members)'));
      }
      card.appendChild(body);

      hd.addEventListener('mousedown', function(ev){
        if (ev.button === 0 && (ev.shiftKey || ev.ctrlKey)) {
          ev.preventDefault();
          ev.stopPropagation();
          // Shift: add to selection
          if (ev.shiftKey) {
            var newSel = JSON.parse(JSON.stringify(_tvMultiSel));
            newSel[tid] = 1;
            _tvSetMultiSel(newSel);
          }
          // Ctrl: toggle
          else if (ev.ctrlKey) {
            var newSel2 = JSON.parse(JSON.stringify(_tvMultiSel));
            newSel2[tid] = newSel2[tid] ? 0 : 1;
            if (!newSel2[tid]) delete newSel2[tid];
            _tvSetMultiSel(newSel2);
          }
        } else {
          _startDrag(ev, tid);
        }
      });
      hd.addEventListener('dblclick', function(ev){ ev.preventDefault(); _isolate(tid, true); });
      card.addEventListener('contextmenu', function(ev){ ev.preventDefault(); _openCtxMenu(ev, tid); });
      canvas.appendChild(card);
      cards[tid] = { el: card, x: 0, y: 0, w: 0, h: 0 };
  }

  /* Append a member row; if it references an inline anonymous type, also append
     that type's fields as an indented group with an "extract" button (TYP-4). */
  function _appendMember(body, m, ownerTid, before){
    var row = _memberRow(m, ownerTid);
    if (before) body.insertBefore(row, before); else body.appendChild(row);
    var aid = m.anon_child_id;
    if (aid && _isInlineAnon(aid)) {
      var grp = _inlineAnonGroup(aid, ownerTid);
      if (before) body.insertBefore(grp, before); else body.appendChild(grp);
    }
  }

  function _inlineAnonGroup(anonId, ownerTid){
    var atd = TD.types[anonId];
    var grp = _h('div', { 'class':'cg-tv-anongroup', 'data-anon': anonId });
    var ghd = _h('div', { 'class':'cg-tv-anonhd' });
    ghd.appendChild(_h('span', { 'class':'cg-tv-anonlbl' }, (atd.kind || 'anon') + ' { \u2026 }'));
    var ext = _h('button', { 'class':'cg-tv-extract', title:'Extract as its own node' }, 'extract \u2b21');
    ext.addEventListener('mousedown', function(ev){ ev.stopPropagation(); });
    ext.addEventListener('click', function(ev){ ev.stopPropagation(); _extractAnon(anonId, ownerTid); });
    ghd.appendChild(ext);
    grp.appendChild(ghd);
    if (atd.kind === 'enum') {
      (atd.enum_values || []).slice(0, 20).forEach(function(ev2){
        var r = _h('div', { 'class':'cg-tv-row' });
        r.appendChild(_h('span', { 'class':'cg-tv-mname' }, ev2[0]));
        if (ev2[1] != null && ev2[1] !== '') r.appendChild(_h('span', { 'class':'cg-tv-eval' }, '= ' + ev2[1]));
        grp.appendChild(r);
      });
    } else {
      (atd.members || []).slice(0, 20).forEach(function(mm){ _appendMember(grp, mm, anonId); });
    }
    return grp;
  }

  function _extractAnon(anonId, ownerTid){
    extracted[anonId] = 1;
    if (!cards[anonId]) _buildCardFor(anonId);
    var oc = cards[ownerTid], nc = cards[anonId];
    if (oc && nc) { nc.x = oc.x + (oc.w || 240) + 60; nc.y = oc.y + 40; }
    _measureOne(anonId); _applyPos(anonId);
    var q = window.CSS && CSS.escape ? CSS.escape(anonId) : anonId;
    var groups = canvas.querySelectorAll('.cg-tv-anongroup[data-anon="' + q + '"]');
    Array.prototype.forEach.call(groups, function(g){ g.remove(); });
    _measureOne(ownerTid);
    _applyVisibility();
    _drawEdges();
    focusType(anonId);
  }

  function _memberRow(m, ownerTid){
    var row = _h('div', { 'class':'cg-tv-row', 'data-mname': m.name });
    row.appendChild(_h('span', { 'class':'cg-tv-mname' }, m.name));
    var tt = _memberTypeText(m);
    var canon = m.canonical_type || m.anon_child_id;
    
    // Build syntax-highlighted type text
    var typeSpan = _h('span', {});
    if (canon && TD.types[canon]) {
      typeSpan.className = 'cg-tv-mtype cg-tv-chip';
      typeSpan.title = 'Go to ' + tt;
      typeSpan.addEventListener('mousedown', function(ev){ ev.stopPropagation(); });
      typeSpan.addEventListener('click', function(ev){ ev.stopPropagation(); focusType(canon); });
    } else {
      typeSpan.className = 'cg-tv-mtype';
    }
    
    // Syntax highlight: color keywords (struct, enum, union, etc) and built-in types
    _highlightTypeText(typeSpan, tt, m);
    row.appendChild(typeSpan);
    return row;
  }

  function _highlightTypeText(container, typeText, memberObj){
    var KEYWORDS = {
      'struct': 'k-keyword-struct',
      'union': 'k-keyword-union',
      'enum': 'k-keyword-enum',
      'unsigned': 'k-keyword-qual',
      'signed': 'k-keyword-qual',
      'const': 'k-keyword-qual',
      'volatile': 'k-keyword-qual'
    };
    var BUILTIN = {
      'int': 'k-builtin', 'char': 'k-builtin', 'float': 'k-builtin', 
      'double': 'k-builtin', 'short': 'k-builtin', 'long': 'k-builtin',
      'void': 'k-builtin', 'bool': 'k-builtin'
    };
    
    // Split by whitespace but keep track of positions
    var tokens = typeText.match(/\S+/g) || [];
    tokens.forEach(function(tok, idx){
      if (idx > 0) container.appendChild(document.createTextNode(' '));
      
      var span = document.createElement('span');
      var cls = 'cg-tv-type-tok';
      
      // Check if it's a keyword
      if (KEYWORDS[tok]) {
        span.className = cls + ' ' + KEYWORDS[tok];
      }
      // Check if it's a builtin type
      else if (BUILTIN[tok]) {
        span.className = cls + ' ' + BUILTIN[tok];
      }
      // Check if it's a pointer or array marker
      else if (tok === '*' || tok === '&' || tok === '[' || tok === ']') {
        span.className = cls + ' k-punct';
      }
      // Check if it ends with [ or starts with ]
      else if (tok.match(/^\[.*\]$/)) {
        span.className = cls + ' k-punct';
      }
      // Otherwise it's a type name - color by canonical type
      else {
        var canon = memberObj.canonical_type || memberObj.anon_child_id;
        if (canon && TD.types[canon]) {
          var kind = TD.types[canon].kind || 'struct';
          span.className = cls + ' k-type-' + kind;
        } else {
          span.className = cls;
        }
      }
      
      span.textContent = tok;
      container.appendChild(span);
    });
  }

  function _measureOne(tid){
    var c = cards[tid]; if (!c) return;
    c.w = c.el.offsetWidth; c.h = c.el.offsetHeight;
  }
  function _measureAll(){ Object.keys(cards).forEach(_measureOne); }

  function _layout(){
    var ids = Object.keys(TD.types);
    var indeg = {}, adj = {};
    ids.forEach(function(id){ indeg[id] = 0; adj[id] = []; });
    TD.edges.forEach(function(e){
      if (!LAYER_EDGE[e.kind]) return;
      if (e.src === e.dst) return;
      if (!TD.types[e.src] || !TD.types[e.dst]) return;
      adj[e.src].push(e.dst); indeg[e.dst]++;
    });
    var layer = {}, roots = [];
    ids.forEach(function(id){ if (indeg[id] === 0) roots.push(id); });
    if (!roots.length) roots = ids.slice();
    roots.forEach(function(id){ layer[id] = 0; });
    // Longest-path relaxation (deterministic, cycle-safe via visit cap).
    var changed = true, guard = 0;
    while (changed && guard++ < ids.length + 4) {
      changed = false;
      ids.forEach(function(id){
        var lv = layer[id] == null ? 0 : layer[id];
        adj[id].forEach(function(d){
          if (layer[d] == null || layer[d] < lv + 1) { layer[d] = lv + 1; changed = true; }
        });
      });
    }
    ids.forEach(function(id){ if (layer[id] == null) layer[id] = 0; });

    var byLayer = {};
    ids.forEach(function(id){ (byLayer[layer[id]] = byLayer[layer[id]] || []).push(id); });
    var layers = Object.keys(byLayer).map(Number).sort(function(a,b){ return a - b; });

    var saved = _loadJSON(POSKEY);
    var yCursor = MARGIN;
    layers.forEach(function(L){
      var group = byLayer[L].sort();
      var maxH = 0, x = MARGIN;
      group.forEach(function(id){
        var c = cards[id]; if (!c) return;
        c.x = x; c.y = yCursor;
        x += (c.w || 240) + NODE_GAP;
        if ((c.h || 0) > maxH) maxH = c.h;
      });
      yCursor += maxH + LAYER_VGAP;
    });
    // Saved positions override the computed grid.
    Object.keys(saved).forEach(function(id){
      if (cards[id]) { cards[id].x = saved[id].x; cards[id].y = saved[id].y; }
    });
    _applyAllPos();
  }

  function _applyPos(tid){
    var c = cards[tid]; if (!c) return;
    c.el.style.left = c.x + 'px'; c.el.style.top = c.y + 'px';
  }
  function _applyAllPos(){ Object.keys(cards).forEach(_applyPos); }

  function _rectEdge(cx, cy, w, h, tx, ty){
    var dx = tx - cx, dy = ty - cy;
    if (dx === 0 && dy === 0) return { x: cx, y: cy };
    var hw = w / 2, hh = h / 2;
    var sx = dx === 0 ? Infinity : hw / Math.abs(dx);
    var sy = dy === 0 ? Infinity : hh / Math.abs(dy);
    var s = Math.min(sx, sy);
    return { x: cx + dx * s, y: cy + dy * s };
  }

  function _drawEdges(){
    if (!svg) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var NS = 'http://www.w3.org/2000/svg';
    var defs = document.createElementNS(NS, 'defs');
    Object.keys(EDGE_STYLE).forEach(function(k){
      var st = EDGE_STYLE[k];
      var mk = document.createElementNS(NS, 'marker');
      mk.setAttribute('id', 'tvm-' + k); mk.setAttribute('markerWidth','8'); mk.setAttribute('markerHeight','8');
      mk.setAttribute('refX','7'); mk.setAttribute('refY','3'); mk.setAttribute('orient','auto');
      var p = document.createElementNS(NS, 'path');
      p.setAttribute('d','M0,0 L7,3 L0,6 Z'); p.setAttribute('fill', st.color);
      mk.appendChild(p); defs.appendChild(mk);
    });
    svg.appendChild(defs);

    // Build a map of edges for bidirectional detection
    var edgeMap = {};
    TD.edges.forEach(function(e, idx){
      var key = e.src + '→' + e.dst;
      edgeMap[key] = idx;
    });

    TD.edges.forEach(function(e, idx){
      var a = cards[e.src], b = cards[e.dst];
      if (!a || !b) return;
      if (a.el.style.display === 'none' || b.el.style.display === 'none') return;
      var st = EDGE_STYLE[e.kind] || EDGE_STYLE.contains_value;
      var ax = a.x + a.w / 2, ay = a.y + a.h / 2;
      var bx = b.x + b.w / 2, by = b.y + b.h / 2;
      var p1, p2;
      if (e.src === e.dst) {
        // self-loop (linked list / tree pointer)
        var lx = a.x + a.w, ly = a.y + 14;
        var loop = document.createElementNS(NS, 'path');
        loop.setAttribute('d', 'M' + lx + ',' + ly + ' q 34,-4 34,18 q 0,22 -34,10');
        loop.setAttribute('fill','none'); loop.setAttribute('stroke', st.color);
        loop.setAttribute('stroke-width','1.6'); if (st.dash) loop.setAttribute('stroke-dasharray', st.dash);
        loop.setAttribute('marker-end','url(#tvm-' + e.kind + ')');
        loop.setAttribute('class','cg-tv-edge'); svg.appendChild(loop);
        return;
      }
      p1 = _rectEdge(ax, ay, a.w, a.h, bx, by);
      p2 = _rectEdge(bx, by, b.w, b.h, ax, ay);
      var path = document.createElementNS(NS, 'path');
      var mx = (p1.x + p2.x) / 2, my = (p1.y + p2.y) / 2;
      path.setAttribute('d', 'M' + p1.x + ',' + p1.y + ' Q ' + mx + ',' + (my - 18) + ' ' + p2.x + ',' + p2.y);
      path.setAttribute('fill','none'); path.setAttribute('stroke', st.color);
      path.setAttribute('stroke-width','1.6'); path.setAttribute('opacity','0.75');
      if (st.dash) path.setAttribute('stroke-dasharray', st.dash);
      path.setAttribute('marker-end','url(#tvm-' + e.kind + ')');
      path.setAttribute('class','cg-tv-edge'); path.setAttribute('data-eid', idx);
      var lbl = (e.members || []).slice(0, 3).join(', ');
      if (e.count > 3) lbl += ' (' + e.count + ')';
      path.addEventListener('mouseenter', function(){ _hiMembers(e, true); });
      path.addEventListener('mouseleave', function(){ _hiMembers(e, false); });
      svg.appendChild(path);
      if (lbl) {
        // Check for bidirectional edges and offset labels to avoid overlap
        var reverseKey = e.dst + '→' + e.src;
        var hasBidirectional = edgeMap[reverseKey] !== undefined;
        var labelOffset = 0;
        if (hasBidirectional) {
          // Offset labels vertically: first edge offset down, reverse offset up
          labelOffset = idx < edgeMap[reverseKey] ? 14 : -14;
        }
        var tx = document.createElementNS(NS, 'text');
        tx.setAttribute('x', mx); tx.setAttribute('y', my - 20 + labelOffset);
        tx.setAttribute('text-anchor','middle'); tx.setAttribute('class','cg-tv-elabel');
        tx.textContent = lbl; svg.appendChild(tx);
      }
    });
    _sizeSvg();
  }

  function _hiMembers(e, on){
    var a = cards[e.src]; if (!a) return;
    (e.members || []).forEach(function(nm){
      var row = a.el.querySelector('.cg-tv-row[data-mname="' + (window.CSS && CSS.escape ? CSS.escape(nm) : nm) + '"]');
      if (row) row.classList.toggle('tv-row-hi', on);
    });
  }

  /* ---------- visibility (kind filter + isolate) ---------- */
  function _applyVisibility(){
    Object.keys(cards).forEach(function(id){
      var td = TD.types[id]; if (!td) return;
      var vis = (!isoActive || isoSet[id]) && kindOn[td.kind];
      cards[id].el.style.display = vis ? '' : 'none';
    });
  }

  /* ---------- multi-select + marquee ---------- */
  function _tvApplyMultiSel(){
    Object.keys(cards).forEach(function(tid){
      cards[tid].el.classList.toggle('tv-multi-selected', !!_tvMultiSel[tid]);
    });
  }
  function _tvSetMultiSel(map){
    _tvMultiSel = map || {};
    _tvApplyMultiSel();
  }
  function _mkRect(p1, p2){
    var x1 = Math.min(p1.x, p2.x), x2 = Math.max(p1.x, p2.x);
    var y1 = Math.min(p1.y, p2.y), y2 = Math.max(p1.y, p2.y);
    return { left: x1, top: y1, right: x2, bottom: y2 };
  }
  function _setBoxRect(box, rect){
    box.style.left = rect.left + 'px';
    box.style.top = rect.top + 'px';
    box.style.width = (rect.right - rect.left) + 'px';
    box.style.height = (rect.bottom - rect.top) + 'px';
  }
  function _cardsInRect(rx, ry, rw, rh){
    var out = [];
    Object.keys(cards).forEach(function(tid){
      var c = cards[tid];
      var cx = c.x + c.w / 2, cy = c.y + c.h / 2;
      if (cx >= rx && cx <= rx + rw && cy >= ry && cy <= ry + rh) out.push(tid);
    });
    return out;
  }

  /* ---------- isolate + breadcrumb (TYP-2) ---------- */
  function _isoClosure(tid){
    var fwd = {}, bwd = {};
    TD.edges.forEach(function(e){
      (fwd[e.src] = fwd[e.src] || []).push(e.dst);
      (bwd[e.dst] = bwd[e.dst] || []).push(e.src);
    });
    var set = {}; set[tid] = 1;
    var st = [tid];
    while (st.length){ var n = st.pop(); (fwd[n] || []).forEach(function(d){ if (!set[d]){ set[d] = 1; st.push(d); } }); }
    st = [tid];
    while (st.length){ var n2 = st.pop(); (bwd[n2] || []).forEach(function(s){ if (!set[s]){ set[s] = 1; st.push(s); } }); }
    return set;
  }

  /* Depth/Direction-aware reachability (parity with cgxIncIsolate).
   * dir: 'callees'/'down' = types this one contains/points to (forward);
   *      'callers'/'up'   = types that contain/reference this one (backward);
   *      'both'           = union of both.
   * depth: 0 = target only; N = N hops; <=0/undefined/9999 = unlimited. */
  function _isoReachable(tid, depth, dir){
    var fwd = {}, bwd = {};
    TD.edges.forEach(function(e){
      (fwd[e.src] = fwd[e.src] || []).push(e.dst);
      (bwd[e.dst] = bwd[e.dst] || []).push(e.src);
    });
    var normDir = (dir === 'callers' || dir === 'up')   ? 'up'   :
                  (dir === 'callees' || dir === 'down') ? 'down' : 'both';
    var maxDepth = (depth === undefined || depth === null || depth >= 9999) ? Infinity : depth;
    var set = {}; set[tid] = 0;
    function bfs(adj){
      var q = [tid];
      while (q.length){
        var n = q.shift(); var d = set[n];
        if (d >= maxDepth) continue;
        (adj[n] || []).forEach(function(m){
          if (set[m] === undefined || set[m] > d + 1){ set[m] = d + 1; q.push(m); }
        });
      }
    }
    if (normDir === 'down' || normDir === 'both') bfs(fwd);
    if (normDir === 'up'   || normDir === 'both') bfs(bwd);
    var out = {}; Object.keys(set).forEach(function(k){ out[k] = 1; });
    return out;
  }

  function _isolate(tid, pushCrumb, depth, dir){
    if (!cards[tid]) return;
    isoActive = true;
    isoSet = (depth === undefined && dir === undefined)
             ? _isoClosure(tid)
             : _isoReachable(tid, depth, dir);
    if (pushCrumb){
      // if re-isolating a type already in the trail, truncate to it
      var at = isoStack.indexOf(tid);
      if (at >= 0) isoStack = isoStack.slice(0, at + 1);
      else isoStack.push(tid);
    }
    _applyVisibility(); _drawEdges(); _renderBreadcrumb();
    focusType(tid);
  }

  function _showAllIso(){
    isoActive = false; isoSet = {}; isoStack = [];
    _applyVisibility(); _drawEdges(); _renderBreadcrumb();
  }

  function _renderBreadcrumb(){
    if (!bc) return;
    bc.innerHTML = '';
    if (!isoActive || !isoStack.length){ bc.style.display = 'none'; return; }
    bc.style.display = 'flex';
    var all = _h('span', { 'class':'cg-tv-crumb cg-tv-crumb-all', title:'Show all types' }, '\u25a0 all');
    all.addEventListener('click', _showAllIso);
    bc.appendChild(all);
    isoStack.forEach(function(tid, i){
      bc.appendChild(_h('span', { 'class':'cg-tv-crumb-sep' }, '\u203a'));
      var td = TD.types[tid];
      var crumb = _h('span', { 'class':'cg-tv-crumb' + (i === isoStack.length - 1 ? ' cg-tv-crumb-cur' : '') },
                     td ? td.name : tid);
      crumb.addEventListener('click', function(){ _isolate(tid, true); });
      bc.appendChild(crumb);
    });
    var cnt = Object.keys(isoSet).length;
    bc.appendChild(_h('span', { 'class':'cg-tv-crumb-cnt' }, cnt + ' visible'));
  }

  /* ---------- context menu (TYP-3) ---------- */
  function _closeCtxMenu(){ if (ctxMenu){ ctxMenu.remove(); ctxMenu = null; } }

  function _openCtxMenu(ev, tid){
    _closeCtxMenu();
    var td = TD.types[tid]; if (!td) return;
    ctxMenu = _h('div', { 'class':'cg-tv-ctx' });
    var addItem = function(label, sub, fn, disabled){
      var it = _h('div', { 'class':'cg-tv-ctx-item' + (disabled ? ' cg-tv-ctx-dis' : '') });
      it.appendChild(_h('span', { 'class':'cg-tv-ctx-lbl' }, label));
      if (sub) it.appendChild(_h('span', { 'class':'cg-tv-ctx-sub' }, sub));
      if (!disabled) it.addEventListener('click', function(e2){ e2.stopPropagation(); _closeCtxMenu(); fn(); });
      ctxMenu.appendChild(it);
    };
    addItem('Isolate containment', 'up + down', function(){ _isolate(tid, true); });
    var nfn = (td.used_by || []).length;
    addItem('Show functions using this type', nfn ? nfn + ' fn' : 'none',
            function(){ _showFunctionsUsing(tid); }, !nfn);
    document.body.appendChild(ctxMenu);
    var mw = ctxMenu.offsetWidth || 220, mh = ctxMenu.offsetHeight || 80;
    var x = Math.min(ev.clientX, window.innerWidth - mw - 8);
    var y = Math.min(ev.clientY, window.innerHeight - mh - 8);
    ctxMenu.style.left = x + 'px'; ctxMenu.style.top = y + 'px';
  }

  function _showFunctionsUsing(tid){
    var td = TD.types[tid]; if (!td) return;
    var ids = td.used_by || [];
    if (!ids.length) return;
    try { window.setViewMode('fn'); } catch(e){}
    setTimeout(function(){
      if (window.cgIsolateFunctionSet) window.cgIsolateFunctionSet(ids);
    }, 140);
  }

  function _sizeSvg(){
    var maxX = 1000, maxY = 800;
    Object.keys(cards).forEach(function(id){
      var c = cards[id]; maxX = Math.max(maxX, c.x + c.w + 200); maxY = Math.max(maxY, c.y + c.h + 200);
    });
    svg.setAttribute('width', maxX); svg.setAttribute('height', maxY);
    canvas.style.width = maxX + 'px'; canvas.style.height = maxY + 'px';
  }

  /* ---------- pan / zoom / drag ---------- */
  function _applyTransform(){
    canvas.style.transform = 'translate(' + pan.x + 'px,' + pan.y + 'px) scale(' + pan.s + ')';
  }
  function _initViewport(){
    vp.addEventListener('mousedown', function(ev){
      if (ev.button !== 0 && ev.button !== 1) return;
      if (ev.button === 1) {
        ev.preventDefault();
        _panning = true;
        _panStart = { x: ev.clientX - pan.x, y: ev.clientY - pan.y };
        vp.classList.add('tv-panning');
        return;
      }
      var hdr = ev.target.closest && ev.target.closest('.cg-tv-hd');
      if (hdr) {
        // card header: will be handled by drag logic
        return;
      }
      if (ev.target.closest && ev.target.closest('.cg-tv-card')) return;
      
      // Marquee on background (not on card)
      var r = vp.getBoundingClientRect();
      var p0 = { x: ev.clientX - r.left, y: ev.clientY - r.top };
      
      ev.preventDefault();
      ev.stopPropagation();
      
      var box = document.createElement('div');
      box.className = 'cg-tv-marquee-box';
      vp.appendChild(box);
      
      _tvMarquee = { box: box, start: p0, cur: p0 };
    });
    
    window.addEventListener('mousemove', function(ev){
      if (_tvMarquee) {
        var r = vp.getBoundingClientRect();
        _tvMarquee.cur = { x: ev.clientX - r.left, y: ev.clientY - r.top };
        var mr = _mkRect(_tvMarquee.start, _tvMarquee.cur);
        _setBoxRect(_tvMarquee.box, mr);
        return;
      }
      if (!_panning) return;
      pan.x = ev.clientX - _panStart.x; pan.y = ev.clientY - _panStart.y; _applyTransform();
    });
    
    window.addEventListener('mouseup', function(){
      if (_tvMarquee) {
        if (_tvMarquee.box && _tvMarquee.box.parentNode) _tvMarquee.box.parentNode.removeChild(_tvMarquee.box);
        var mr = _mkRect(_tvMarquee.start, _tvMarquee.cur);
        var ids = _cardsInRect(mr.left / pan.s - pan.x / pan.s, mr.top / pan.s - pan.y / pan.s,
                               (mr.right - mr.left) / pan.s, (mr.bottom - mr.top) / pan.s);
        _tvMarquee = null;
        
        // Apply multi-select based on modifiers
        var newSel = {};
        if (!ev.ctrlKey && !ev.shiftKey) {
          // Plain click: replace
          ids.forEach(function(id){ newSel[id] = 1; });
        } else if (ev.shiftKey) {
          // Shift: add to current
          Object.keys(_tvMultiSel).forEach(function(id){ newSel[id] = 1; });
          ids.forEach(function(id){ newSel[id] = 1; });
        } else if (ev.ctrlKey) {
          // Ctrl: toggle
          newSel = JSON.parse(JSON.stringify(_tvMultiSel));
          ids.forEach(function(id){
            if (newSel[id]) delete newSel[id];
            else newSel[id] = 1;
          });
        }
        _tvSetMultiSel(newSel);
        return;
      }
      _panning = false; vp.classList.remove('tv-panning');
    });
    
    vp.addEventListener('wheel', function(ev){
      ev.preventDefault();
      var r = vp.getBoundingClientRect();
      var mx = ev.clientX - r.left, my = ev.clientY - r.top;
      var old = pan.s;
      var ns = old * (ev.deltaY < 0 ? 1.1 : 0.9);
      ns = Math.max(0.15, Math.min(2.5, ns));
      pan.x = mx - (mx - pan.x) * (ns / old);
      pan.y = my - (my - pan.y) * (ns / old);
      pan.s = ns; _applyTransform();
    }, { passive: false });
  }

  var _drag = null;
  function _startDrag(ev, tid){
    if (ev.button !== 0) return;
    ev.preventDefault();
    ev.stopPropagation();
    
    var c = cards[tid]; if (!c) return;
    
    // Collect all cards to drag (this card + multi-selected if this is part of selection)
    var toDrag = [];
    var isMulti = !!_tvMultiSel[tid];
    if (isMulti) {
      // Dragging a multi-selected card: move all selected cards
      Object.keys(_tvMultiSel).forEach(function(id){
        if (_tvMultiSel[id] && cards[id]) {
          toDrag.push({ tid: id, card: cards[id], ox: cards[id].x, oy: cards[id].y });
        }
      });
    } else {
      // Single card drag, but also clear multi-select
      toDrag.push({ tid: tid, card: c, ox: c.x, oy: c.y });
      _tvSetMultiSel({});
    }
    
    _drag = { toDrag: toDrag, sx: ev.clientX, sy: ev.clientY };
    
    var mv = function(e2){
      if (!_drag) return;
      var dx = (e2.clientX - _drag.sx) / pan.s;
      var dy = (e2.clientY - _drag.sy) / pan.s;
      _drag.toDrag.forEach(function(it){
        it.card.x = it.ox + dx;
        it.card.y = it.oy + dy;
        _applyPos(it.tid);
      });
      _drawEdges();
    };
    
    var up = function(){
      window.removeEventListener('mousemove', mv); window.removeEventListener('mouseup', up);
      var sv = _loadJSON(POSKEY);
      _drag.toDrag.forEach(function(it){
        sv[it.tid] = { x: it.card.x, y: it.card.y };
      });
      _saveJSON(POSKEY, sv);
      _drag = null;
    };
    
    window.addEventListener('mousemove', mv); window.addEventListener('mouseup', up);
  }

  /* ---------- search ---------- */
  function _matches(q){
    q = q.toLowerCase(); var out = [];
    Object.keys(TD.types).forEach(function(tid){
      var td = TD.types[tid]; var hit = null;
      if ((td.name || '').toLowerCase().indexOf(q) >= 0) hit = 'name';
      else if ((td.tag || '').toLowerCase().indexOf(q) >= 0) hit = 'tag';
      else if ((td.aliases || []).some(function(a){ return a.toLowerCase().indexOf(q) >= 0; })) hit = 'alias';
      else if (td.kind === 'enum') {
        var ec = (td.enum_values || []).filter(function(ev){ return (ev[0] || '').toLowerCase().indexOf(q) >= 0; });
        if (ec.length) hit = 'const:' + ec[0][0];
      }
      if (!hit) {
        var mm = (td.members || []).filter(function(m){ return (m.name || '').toLowerCase().indexOf(q) >= 0; });
        if (mm.length) hit = 'member:' + mm[0].name;
      }
      if (hit) out.push({ tid: tid, hit: hit });
    });
    return out.slice(0, 40);
  }
  function _renderDropdown(list){
    dd.innerHTML = '';
    if (!list.length) { dd.classList.remove('open'); return; }
    list.forEach(function(it){
      var td = TD.types[it.tid];
      var row = _h('div', { 'class':'cg-tv-dd-item' });
      row.appendChild(_h('span', { 'class':'cg-tv-dd-nm' }, td.name));
      var sub = '  ' + td.kind;
      if (it.hit.indexOf('member:') === 0) sub += '  \u2022 field ' + it.hit.slice(7);
      else if (it.hit.indexOf('const:') === 0) sub += '  \u2022 value ' + it.hit.slice(6);
      row.appendChild(_h('span', { 'class':'cg-tv-dd-sub' }, sub));
      row.addEventListener('click', function(){ focusType(it.tid); dd.classList.remove('open'); });
      dd.appendChild(row);
    });
    dd.classList.add('open');
  }
  function _applyFilter(q){
    if (!q) { Object.keys(cards).forEach(function(id){ cards[id].el.classList.remove('tv-dim','tv-match'); }); return; }
    var set = {}; _matches(q).forEach(function(it){ set[it.tid] = 1; });
    Object.keys(cards).forEach(function(id){
      cards[id].el.classList.toggle('tv-match', !!set[id]);
      cards[id].el.classList.toggle('tv-dim', !set[id]);
    });
  }

  function focusType(tid){
    var c = cards[tid]; if (!c) return;
    if (c.el.classList.contains('tv-collapsed')) { /* keep as is */ }
    var r = vp.getBoundingClientRect();
    pan.s = Math.max(0.6, Math.min(pan.s, 1.2));
    pan.x = r.width / 2 - (c.x + c.w / 2) * pan.s;
    pan.y = r.height / 2 - (c.y + c.h / 2) * pan.s;
    _applyTransform();
    c.el.classList.add('tv-flash');
    setTimeout(function(){ c.el.classList.remove('tv-flash'); }, 1400);
  }
  window.cgTypeFocus = focusType;
  window.cgTypeFindByName = _tvFindTypeByName;
  window.cgTypeJumpFromName = function(typeText){
    var tid = _tvFindTypeByName(typeText);
    if (!tid) return false;
    if (typeof window.setViewMode === 'function') window.setViewMode('types');
    setTimeout(function(){ if (window.cgTypeFocus) window.cgTypeFocus(tid); }, 90);
    return true;
  };

  /* ---- Shared-sidebar parity APIs (mirror cgxInc*): isolate-as-filter,
   *      expand, center, highlight, show-all, fit. Reused by the left toolbar
   *      via the currentMode==='types' branches. -------------------------- */
  function _tvResolveTarget(target){
    if (target == null) return null;
    if (cards[target]) return target;              // already a type_id
    return _tvFindTypeByName(String(target));      // name / alias / tag / verbatim
  }
  window.cgxTypeIsolate = function(target, depth, dir){
    var tid = _tvResolveTarget(target);
    if (!tid) return 0;
    _isolate(tid, true, depth, dir);
    return Object.keys(isoSet).length;
  };
  window.cgxTypeExpand = function(target, depth, dir){
    var tid = _tvResolveTarget(target);
    if (!tid) return 0;
    var reach = _isoReachable(tid, depth, dir);
    if (isoActive){ Object.keys(reach).forEach(function(k){ isoSet[k] = 1; }); }
    else { isoActive = true; isoSet = reach; }
    _applyVisibility(); _drawEdges(); _renderBreadcrumb();
    focusType(tid);
    return Object.keys(reach).length;
  };
  window.cgxTypeCenter = function(target){
    var tid = _tvResolveTarget(target);
    if (tid) focusType(tid);
    return !!tid;
  };
  window.cgxTypeHighlight = function(targets){
    if (!targets) return 0;
    if (!Array.isArray(targets)) targets = [targets];
    var sel = {}, first = null;
    targets.forEach(function(t){
      var tid = _tvResolveTarget(t);
      if (tid){ sel[tid] = 1; if (!first) first = tid; }
    });
    _tvSetMultiSel(sel);
    if (first) focusType(first);
    return Object.keys(sel).length;
  };
  /* Shared-sidebar type search: routes #cg-search when in Type Nodes mode
     (mirrors cgxModuleSearch / cgxIncSearch). Dims non-matches, fills the
     sidebar dropdown with matched types, and focuses on click / Enter. */
  function _tvEscHtml(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
  window.cgxTypeSearch = function(q){
    var lq = (q||'').trim();
    _applyFilter(lq);
    var dd2 = document.getElementById('cg-search-dropdown');
    var inp = document.getElementById('cg-search');
    var hintEl = document.getElementById('cg-search-hint');
    if (!dd2 || !inp) return;
    if (!lq){ dd2.style.display='none'; dd2.innerHTML=''; if(hintEl) hintEl.textContent='Search types \u2022 click a result to focus'; return; }
    var list = _matches(lq);
    if (hintEl) hintEl.textContent = list.length===0 ? 'No matches' : (list.length===1 ? '1 match' : list.length+' matches');
    if (!list.length){ dd2.style.display='none'; dd2.innerHTML=''; return; }
    var rect = inp.getBoundingClientRect();
    dd2.style.top=(rect.bottom+2)+'px'; dd2.style.left=rect.left+'px'; dd2.style.width=rect.width+'px';
    var llq = lq.toLowerCase();
    function _hi(text){ var t=String(text||''); var i=t.toLowerCase().indexOf(llq); if(i<0) return _tvEscHtml(t);
      return _tvEscHtml(t.slice(0,i))+'<span class="cg-sd-mark">'+_tvEscHtml(t.slice(i,i+lq.length))+'</span>'+_tvEscHtml(t.slice(i+lq.length)); }
    dd2.innerHTML = list.map(function(it){
      var td = TD.types[it.tid]||{}; var sub = td.kind||'type';
      if (it.hit.indexOf('member:')===0) sub += ' \u00b7 field '+it.hit.slice(7);
      else if (it.hit.indexOf('const:')===0) sub += ' \u00b7 value '+it.hit.slice(6);
      var fn = (td.file||'').replace(/.*[\\\\/]/g,'');
      return '<div class="cg-sd-item" data-tid="'+_tvEscHtml(it.tid)+'">'
        +'<span class="cg-sd-name">'+_hi(td.name||it.tid)+'</span>'
        +'<span class="cg-sd-meta">'+_tvEscHtml(sub)+(fn?(' \u00b7 '+_tvEscHtml(fn)):'')+'</span></div>';
    }).join('');
    Array.prototype.forEach.call(dd2.querySelectorAll('.cg-sd-item'), function(item){
      item.addEventListener('mousedown', function(e){
        e.preventDefault();
        var tid=item.dataset.tid;
        if(inp && TD.types[tid]) inp.value = TD.types[tid].name||'';
        dd2.style.display='none';
        var sel={}; sel[tid]=1; try{ _tvSetMultiSel(sel); }catch(_e){}
        focusType(tid);
      });
    });
    dd2.style.display='block';
  };
  window.cgxTypeSearchEnter = function(q){
    var list = _matches((q||'').trim());
    if (list.length){ var sel={}; sel[list[0].tid]=1; try{ _tvSetMultiSel(sel); }catch(_e){} focusType(list[0].tid); }
    return list.length;
  };
  window.cgxTypeShowAll = function(){ _showAllIso(); _tvSetMultiSel({}); };
  window.cgxTypeClearFocus = window.cgxTypeShowAll;
  window.cgxTypeFit = function(){ _fit(); };
  window.cgxTypeSaveLayout = function(){
    var sv = {}; Object.keys(cards).forEach(function(id){ sv[id] = { x: cards[id].x, y: cards[id].y }; });
    _saveJSON(POSKEY, sv);
  };
  window.cgxTypeResetLayout = function(){
    try { localStorage.removeItem(POSKEY); } catch(e){}
    _measureAll(); _layout(); _drawEdges(); _fit();
  };
  window.cgxTypeClearSaved = function(){ try { localStorage.removeItem(POSKEY); } catch(e){} };

  function _fit(){
    var ids = Object.keys(cards); if (!ids.length) return;
    var minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9;
    ids.forEach(function(id){ var c = cards[id];
      minX = Math.min(minX, c.x); minY = Math.min(minY, c.y);
      maxX = Math.max(maxX, c.x + c.w); maxY = Math.max(maxY, c.y + c.h); });
    var r = vp.getBoundingClientRect();
    var gw = maxX - minX + 120, gh = maxY - minY + 120;
    pan.s = Math.max(0.15, Math.min(1.4, Math.min(r.width / gw, r.height / gh)));
    pan.x = (r.width - gw * pan.s) / 2 - minX * pan.s + 60 * pan.s;
    pan.y = (r.height - gh * pan.s) / 2 - minY * pan.s + 60 * pan.s;
    _applyTransform();
  }

  function _buildTopbar(){
    var bar = _h('div', { id:'cg-tv-topbar' });
    bar.appendChild(_h('h2', null, 'Type Nodes Mode'));
    var row = _h('div', { id:'cg-tv-topbar-row' });
    var sw = _h('div', { id:'cg-tv-search-wrap' });
    searchInput = _h('input', { id:'cg-tv-search-input', type:'text', placeholder:'Search type / alias / field name\u2026', autocomplete:'off' });
    var clr = _h('button', { id:'cg-tv-search-clear', title:'Clear' }, '\u00d7');
    dd = _h('div', { id:'cg-tv-dropdown' });
    sw.appendChild(searchInput); sw.appendChild(clr); sw.appendChild(dd);
    row.appendChild(sw);
    var fit = _h('button', { 'class':'cg-tv-btn', title:'Fit all cards' }, 'Fit');
    fit.addEventListener('click', _fit);
    var reset = _h('button', { 'class':'cg-tv-btn', title:'Reset saved layout' }, 'Reset layout');
    reset.addEventListener('click', function(){
      try { localStorage.removeItem(POSKEY); } catch(e){}
      _measureAll(); _layout(); _drawEdges(); _fit();
    });
    row.appendChild(fit); row.appendChild(reset);
    statsEl = _h('span', { id:'cg-tv-stats' });
    row.appendChild(statsEl);
    bar.appendChild(row);

    var krow = _h('div', { id:'cg-tv-kindrow' });
    krow.appendChild(_h('span', { 'class':'cg-tv-kind-lbl' }, 'Kinds:'));
    ['struct','union','enum','typedef','class'].forEach(function(k){
      var b = _h('button', { 'class':'cg-tv-kind-btn', 'data-k':k }, k);
      b.addEventListener('click', function(){
        kindOn[k] = !kindOn[k]; b.classList.toggle('off', !kindOn[k]);
        _applyVisibility();
        _drawEdges();
      });
      krow.appendChild(b);
    });
    bar.appendChild(krow);

    bc = _h('div', { id:'cg-tv-breadcrumb' });
    bc.style.display = 'none';
    bar.appendChild(bc);

    searchInput.addEventListener('input', function(){
      var q = searchInput.value.trim();
      _applyFilter(q);
      if (q) _renderDropdown(_matches(q)); else dd.classList.remove('open');
    });
    searchInput.addEventListener('keydown', function(ev){
      if (ev.key === 'Enter') { var m = _matches(searchInput.value.trim()); if (m.length) { focusType(m[0].tid); dd.classList.remove('open'); } }
      else if (ev.key === 'Escape') { dd.classList.remove('open'); }
    });
    clr.addEventListener('click', function(){ searchInput.value = ''; _applyFilter(''); dd.classList.remove('open'); searchInput.focus(); });
    document.addEventListener('click', function(ev){ if (!sw.contains(ev.target)) dd.classList.remove('open'); _closeCtxMenu(); });
    document.addEventListener('keydown', function(ev){
      if (ev.key === 'Escape'){ _closeCtxMenu(); if (isoActive) _showAllIso(); }
    });
    return bar;
  }

  function ensureBuilt(){
    if (built) return;
    view = document.getElementById('cg-type-view');
    if (!view) return;
    built = true;
    view.appendChild(_buildTopbar());
    var graph = _h('div', { id:'cg-tv-graph' });
    vp = _h('div', { id:'cg-tv-viewport' });
    canvas = _h('div', { id:'cg-tv-canvas' });
    svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('id', 'cg-tv-svg');
    canvas.appendChild(svg);
    vp.appendChild(canvas); graph.appendChild(vp); view.appendChild(graph);

    var n = Object.keys(TD.types).length;
    if (!n) {
      placeholder = _h('div', { id:'cg-tv-placeholder' });
      placeholder.innerHTML = 'No type definitions detected in this project.<br>' +
        '<span style="font-size:12px">Type Nodes Mode visualises struct / union / enum / typedef / class relationships (C/C++).</span>';
      graph.appendChild(placeholder);
      if (statsEl) statsEl.textContent = '0 types';
      return;
    }
    _buildCards();
    _initViewport();
    // Measure + layout must run while the view is visible (offsetWidth != 0).
    requestAnimationFrame(function(){
      _measureAll(); _layout(); _drawEdges(); _fit();
      if (statsEl) {
        var s = TD.stats || {};
        statsEl.textContent = (s.types || n) + ' types \u00b7 ' + (TD.edges.length) + ' edges \u00b7 ' + (TD.roots.length) + ' roots';
      }
    });
  }

  /* ---------- public activator + Nodebook adapter ---------- */
  window.cgTypeActivate = function(){
    var v = document.getElementById('cg-type-view');
    if (v) v.style.display = 'flex';
    ensureBuilt();
    // Re-layout if this is the first real paint (cards were 0-width before show).
    if (built && Object.keys(cards).length) {
      var anyUnmeasured = Object.keys(cards).some(function(id){ return !cards[id].w; });
      if (anyUnmeasured) requestAnimationFrame(function(){ _measureAll(); _layout(); _drawEdges(); _fit(); });
    }
  };

  window.CG_NB_ADAPTERS = window.CG_NB_ADAPTERS || {};
  window.CG_NB_ADAPTERS['types'] = {
    capture: function(){
      var pos = {}; Object.keys(cards).forEach(function(id){ pos[id] = { x: cards[id].x, y: cards[id].y }; });
      return { pan: { x: pan.x, y: pan.y, s: pan.s }, pos: pos, search: searchInput ? searchInput.value : '' };
    },
    restore: function(state){
      window.cgTypeActivate();
      if (!state) return;
      var apply = function(){
        if (state.pos) Object.keys(state.pos).forEach(function(id){ if (cards[id]) { cards[id].x = state.pos[id].x; cards[id].y = state.pos[id].y; } });
        _applyAllPos(); _drawEdges();
        if (state.pan) { pan.x = state.pan.x; pan.y = state.pan.y; pan.s = state.pan.s; _applyTransform(); }
        if (state.search && searchInput) { searchInput.value = state.search; _applyFilter(state.search); }
      };
      requestAnimationFrame(apply);
    }
  };
})();
</script>
"""
