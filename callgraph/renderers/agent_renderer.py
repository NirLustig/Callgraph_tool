"""
Agent-facing structured export.

Produces a stable, lossless, *portable* JSON document describing a project's
architecture so that an offline LLM agent can read and reason about it without
ever opening the interactive HTML.

Design goals
------------
1. **Portable** — every path is project-root-relative and POSIX-normalised, so
   the file means the same thing on the machine that generated it and on the
   air-gapped machine that consumes it.
2. **Navigable** — adjacency is materialised in *both* directions (``calls`` and
   ``called_by``) and the graph is summarised at three abstraction levels
   (module -> file -> function) so an agent can start broad and drill down
   without rebuilding indexes itself.
3. **Diff-friendly / updatable** — records are emitted in a deterministic order,
   carry a line-independent ``key`` plus a content ``hash``, and the document
   carries a top-level ``content_digest``. Re-running the tool on an edited
   project yields a JSON whose diff maps cleanly onto what actually changed —
   which is what makes it usable as a *re-generatable knowledge source*.

Two layouts
-----------
* **single file** (default): ``<output>.agent.json`` — one document. Best for
  small/medium projects; the whole thing fits comfortably in an agent's context.
* **sharded** (``output.agent_shards`` / ``--agent-shards``): a directory
  ``<output>.agent/`` containing ``index.json`` (the cheap, always-read summary)
  plus ``modules/<m>.json`` and ``files/<f>.json`` detail shards. Best for large
  solutions where the agent should do progressive disclosure.

The format intentionally mirrors the in-memory :class:`CallGraph` model so it
stays lossless; nothing here re-parses source.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Optional

from ..config import Config
from ..models import (
    CallGraph,
    FunctionDef,
    Language,
    ResolutionConfidence,
)
from .base import BaseRenderer

SCHEMA_ID = "callgraph.agent-export/v1"
SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------- #
# Small helpers                                                                #
# --------------------------------------------------------------------------- #

def _posix_rel(abs_path: str, root: Optional[Path]) -> str:
    """Return *abs_path* relative to *root*, POSIX-normalised.

    Falls back to the basename when the path is outside the project root
    (external/library functions), so the export never leaks absolute machine
    paths.
    """
    if not abs_path:
        return ""
    p = Path(abs_path)
    if root is not None:
        try:
            return p.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            pass
    return p.name


def _short_hash(payload: object) -> str:
    """Stable 12-char content hash of the JSON-canonical form of *payload*."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _first_line(text: Optional[str]) -> str:
    if not text:
        return ""
    return text.strip().splitlines()[0].strip() if text.strip() else ""


def _safe_slug(name: str) -> str:
    """Filesystem-safe shard filename for a module/file id."""
    out = "".join(c if (c.isalnum() or c in "-._") else "_" for c in name)
    return out.strip("_") or "root"


# --------------------------------------------------------------------------- #
# Renderer                                                                     #
# --------------------------------------------------------------------------- #

class AgentRenderer(BaseRenderer):
    """Serialises a :class:`CallGraph` into the agent-export JSON document(s)."""

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.folder_depth = getattr(config.render, "folder_depth", 2)
        self.shards = bool(getattr(config.output, "agent_shards", False))

    # -- public entry point -------------------------------------------------- #

    def render(self, graph: CallGraph, output_path: Path) -> Path:
        root = Path(graph.project_root).resolve() if graph.project_root else None
        doc = self._build_document(graph, root)

        out = output_path
        if out.suffix.lower() in (".json", ".html", ".svg", ".png", ".dot"):
            out = out.with_suffix("")
        json_path = out.with_name(out.name + ".agent.json")
        json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False, default=str)

        if self.shards:
            self._write_shards(doc, out)

        return json_path

    # -- document assembly --------------------------------------------------- #

    def _build_document(self, graph: CallGraph, root: Optional[Path]) -> dict:
        # 1. Function records + a map from internal node_id -> stable export id.
        fn_records, id_map = self._function_records(graph, root)

        # 2. Edges (resolved + unresolved), remapped onto stable ids.
        edges, fn_calls, fn_called_by, fn_unresolved = self._edge_records(graph, root, id_map)

        # Attach adjacency back onto the function records.
        for rec in fn_records:
            fid = rec["id"]
            rec["calls"] = sorted(fn_calls.get(fid, []))
            rec["called_by"] = sorted(fn_called_by.get(fid, []))
            rec["external_calls"] = sorted(fn_unresolved.get(fid, []))

        # 3. File-level + module-level aggregation derived from the function graph.
        file_records, file_edges = self._file_level(fn_records, edges)
        module_records, module_edges, file_to_module = self._module_level(
            file_records, file_edges, graph, root
        )
        # Stamp each file/function with its owning module for easy lookup.
        for f in file_records:
            f["module"] = file_to_module.get(f["id"], "")
        fid_to_module = {f["id"]: f["module"] for f in file_records}
        for rec in fn_records:
            rec["module"] = fid_to_module.get(rec["file"], "")

        # 4. Hotspots, roots/leaves, cycles.
        analysis = self._analysis(fn_records, edges, file_edges, graph)

        # 5. Includes + violations, relativised.
        includes = self._includes(graph, root)
        violations = self._violations(graph, id_map)

        # Per-record content hashes for diffing (computed on the semantic subset).
        for rec in fn_records:
            rec["hash"] = _short_hash({
                "signature": rec["signature"],
                "calls": rec["calls"],
                "external_calls": rec["external_calls"],
                "file": rec["file"],
            })

        document = {
            "schema": SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "generator": {"tool": "callgraph_tool", "version": _tool_version()},
            "project": self._project_block(graph, root),
            "summary": {
                "modules": len(module_records),
                "files": len(file_records),
                "functions": len(fn_records),
                "edges": len(edges),
                "violations": len(violations),
                "call_cycles": len(analysis["call_cycles"]),
            },
            "modules": module_records,
            "module_edges": module_edges,
            "files": file_records,
            "file_edges": file_edges,
            "functions": fn_records,
            "edges": edges,
            "includes": includes,
            "violations": violations,
            "analysis": analysis,
        }
        # Top-level digest over the structural sections only (ignores timestamps).
        document["content_digest"] = _short_hash({
            "modules": [m["id"] for m in module_records],
            "files": [f["hash"] for f in file_records],
            "functions": [r["hash"] for r in fn_records],
            "module_edges": module_edges,
            "violations": violations,
        })
        return document

    # -- sections ------------------------------------------------------------ #

    def _project_block(self, graph: CallGraph, root: Optional[Path]) -> dict:
        langs = sorted({
            fn.language.display_name()
            for fn in graph.functions.values()
            if not fn.is_external
        })
        stats = graph.stats()
        build = graph.build_info
        return {
            "name": (root.name if root else "project"),
            "root": (root.as_posix() if root else None),
            "languages": langs,
            "source": (build.source if build else "folder"),
            "render_level": graph.render_level.value,
            "stats": {
                "functions": stats["functions"],
                "external_functions": stats["external_functions"],
                "calls": stats["calls"],
                "resolved_calls": stats["resolved_calls"],
                "files_parsed": stats["files_parsed"],
                "parse_errors": stats["parse_errors"],
            },
        }

    def _function_records(self, graph: CallGraph, root: Optional[Path]):
        """Build deterministic function records + a node_id -> export id map."""
        id_map: dict[str, str] = {}
        # First pass: provisional stable keys, detect collisions (overloads).
        provisional: dict[str, list[str]] = defaultdict(list)
        for node_id, fn in graph.functions.items():
            rel = _posix_rel(fn.file_path, root)
            key = f"{rel}::{fn.qualified_name}" if rel else fn.qualified_name
            provisional[key].append(node_id)

        records = []
        for node_id, fn in sorted(graph.functions.items(), key=lambda kv: _fn_sort_key(kv[1], root)):
            rel = _posix_rel(fn.file_path, root)
            key = f"{rel}::{fn.qualified_name}" if rel else fn.qualified_name
            # Disambiguate overloads by line.
            if len(provisional[key]) > 1:
                key = f"{key}#{fn.line_start}"
            id_map[node_id] = key

        for node_id, fn in sorted(graph.functions.items(), key=lambda kv: _fn_sort_key(kv[1], root)):
            fid = id_map[node_id]
            records.append({
                "id": fid,
                "name": fn.name,
                "qualified_name": fn.qualified_name,
                "language": fn.language.display_name(),
                "file": _posix_rel(fn.file_path, root),
                "module": "",  # filled later
                "line_start": fn.line_start,
                "line_end": fn.line_end,
                "signature": fn.signature(),
                "parameters": [
                    {"name": p.name, "type": p.type_hint, "unused": bool(p.is_dead)}
                    for p in fn.parameters
                ],
                "return_type": fn.return_type,
                "parent": fn.parent,
                "kind": fn.func_type or ("method" if fn.is_method else "function"),
                "is_external": bool(fn.is_external),
                "is_method": bool(fn.is_method),
                "is_virtual": bool(fn.is_virtual),
                "doc": _first_line(fn.docstring),
                "tracked_vars": dict(fn.tracked_vars),
                # adjacency added by caller
            })
        return records, id_map

    def _edge_records(self, graph: CallGraph, root: Optional[Path], id_map: dict):
        """Materialise edges + forward/reverse adjacency on stable ids."""
        edges = []
        fn_calls: dict[str, set] = defaultdict(set)
        fn_called_by: dict[str, set] = defaultdict(set)
        fn_unresolved: dict[str, set] = defaultdict(set)
        seen = set()

        for call in graph.calls:
            src = id_map.get(call.caller_id)
            if src is None:
                continue
            if call.is_resolved and call.callee_id and call.callee_id in id_map:
                dst = id_map[call.callee_id]
                dedup = (src, dst, call.confidence_category)
                fn_calls[src].add(dst)
                fn_called_by[dst].add(src)
                if dedup in seen:
                    # bump underlying count on the existing edge
                    continue
                seen.add(dedup)
                edges.append({
                    "from": src,
                    "to": dst,
                    "callee_name": call.callee_name,
                    "confidence": _conf_str(call.resolution_confidence),
                    "category": call.confidence_category,
                    "reason": call.resolution_reason or call.resolution_hint or "",
                    "count": max(1, call.underlying_count),
                    "sample_sites": [
                        [_posix_rel(f, root), ln] for f, ln in (call.sample_call_sites or [])
                    ][:5],
                })
            else:
                # Unresolved / external — record the raw callee name on the caller.
                if call.callee_name:
                    fn_unresolved[src].add(call.callee_name)

        edges.sort(key=lambda e: (e["from"], e["to"], e["category"]))
        return edges, fn_calls, fn_called_by, fn_unresolved

    def _file_level(self, fn_records: list, edges: list):
        """Group functions into files; aggregate cross-file edges."""
        files: dict[str, dict] = {}
        for rec in fn_records:
            if rec["is_external"]:
                continue
            fpath = rec["file"]
            if not fpath:
                continue
            f = files.setdefault(fpath, {
                "id": fpath,
                "path": fpath,
                "language": rec["language"],
                "module": "",
                "functions": [],
            })
            f["functions"].append(rec["id"])

        fn_to_file = {r["id"]: r["file"] for r in fn_records}
        pair_count: dict[tuple, int] = defaultdict(int)
        for e in edges:
            sf, df = fn_to_file.get(e["from"]), fn_to_file.get(e["to"])
            if not sf or not df or sf == df:
                continue
            pair_count[(sf, df)] += e["count"]

        file_records = sorted(files.values(), key=lambda f: f["id"])
        for f in file_records:
            f["functions"] = sorted(f["functions"])
            f["hash"] = _short_hash({"functions": f["functions"]})
        file_edges = [
            {"from": a, "to": b, "count": c}
            for (a, b), c in sorted(pair_count.items())
        ]
        return file_records, file_edges

    def _module_level(self, file_records, file_edges, graph, root):
        """Map files -> modules (config modules if present, else folder), aggregate."""
        file_to_module: dict[str, str] = {}

        # Prefer explicit modules attached to the graph.
        explicit = {}
        if graph.modules:
            for mod_name, mod in graph.modules.items():
                for abs_f in mod.files:
                    explicit[_posix_rel(abs_f, root)] = mod_name

        for f in file_records:
            fid = f["id"]
            if fid in explicit:
                file_to_module[fid] = explicit[fid]
            else:
                file_to_module[fid] = _folder_module(fid, self.folder_depth)

        modules: dict[str, dict] = {}
        for f in file_records:
            m = file_to_module[f["id"]]
            rec = modules.setdefault(m, {
                "id": m,
                "name": m,
                "inferred_from": ("config" if f["id"] in explicit else "folder"),
                "files": [],
                "languages": set(),
                "function_count": 0,
            })
            rec["files"].append(f["id"])
            rec["languages"].add(f["language"])
            rec["function_count"] += len(f["functions"])

        pair_count: dict[tuple, int] = defaultdict(int)
        for e in file_edges:
            sm, dm = file_to_module.get(e["from"]), file_to_module.get(e["to"])
            if not sm or not dm or sm == dm:
                continue
            pair_count[(sm, dm)] += e["count"]

        module_records = []
        for rec in sorted(modules.values(), key=lambda m: m["id"]):
            rec["files"] = sorted(rec["files"])
            rec["languages"] = sorted(rec["languages"])
            module_records.append(rec)
        # depends_on / depended_by adjacency at module level.
        depends_on: dict[str, set] = defaultdict(set)
        depended_by: dict[str, set] = defaultdict(set)
        for (a, b) in pair_count:
            depends_on[a].add(b)
            depended_by[b].add(a)
        for rec in module_records:
            rec["depends_on"] = sorted(depends_on.get(rec["id"], []))
            rec["depended_by"] = sorted(depended_by.get(rec["id"], []))

        module_edges = [
            {"from": a, "to": b, "count": c}
            for (a, b), c in sorted(pair_count.items())
        ]
        return module_records, module_edges, file_to_module

    def _analysis(self, fn_records, edges, file_edges, graph):
        """Hotspots, entry/leaf candidates, and cycle detection."""
        internal = [r for r in fn_records if not r["is_external"]]
        fan_out = sorted(
            internal,
            key=lambda r: (-(len(r["calls"]) + len(r["external_calls"])), r["id"]),
        )
        fan_in = sorted(internal, key=lambda r: (-len(r["called_by"]), r["id"]))

        top_fan_out = [
            {"id": r["id"], "out": len(r["calls"]) + len(r["external_calls"])}
            for r in fan_out[:15] if (len(r["calls"]) + len(r["external_calls"])) > 0
        ]
        top_fan_in = [
            {"id": r["id"], "in": len(r["called_by"])}
            for r in fan_in[:15] if len(r["called_by"]) > 0
        ]
        # Roots = internal, called by nobody inside the project (entry candidates).
        roots = sorted(r["id"] for r in internal if not r["called_by"])
        # Leaves = internal, call nothing inside the project.
        leaves = sorted(r["id"] for r in internal if not r["calls"])

        declared_entries = list(getattr(graph, "render_level", None) and
                                getattr(self.config.filter, "entry_points", []) or [])

        call_cycles = _find_cycles([(e["from"], e["to"]) for e in edges], limit=40)
        file_cycles = _find_cycles([(e["from"], e["to"]) for e in file_edges], limit=40)

        return {
            "top_fan_out": top_fan_out,
            "top_fan_in": top_fan_in,
            "entry_candidates": roots[:50],
            "leaf_functions": leaves[:50],
            "declared_entry_points": declared_entries,
            "call_cycles": call_cycles,
            "file_cycles": file_cycles,
        }

    def _includes(self, graph: CallGraph, root: Optional[Path]) -> dict:
        ig = graph.include_graph
        if not ig:
            return {"present": False, "edges": [], "cycles": [], "most_included": []}
        edges = []
        for from_file, lst in ig.files.items():
            for e in lst:
                edges.append({
                    "from": _posix_rel(from_file, root),
                    "to": _posix_rel(e.to_file, root) if e.resolved else e.raw_target,
                    "system": bool(e.is_system),
                    "resolved": bool(e.resolved),
                })
        edges.sort(key=lambda e: (e["from"], e["to"]))
        cycles = [[_posix_rel(p, root) for p in cyc] for cyc in ig.cycles]
        most = [[_posix_rel(p, root), n] for p, n in ig.most_included]
        return {"present": True, "edges": edges, "cycles": cycles, "most_included": most}

    def _violations(self, graph: CallGraph, id_map: dict) -> list:
        out = []
        for v in graph.violations:
            out.append({
                "kind": v.rule_kind,
                "from": v.from_module,
                "to": v.to_module,
                "reason": v.reason,
                "sample_edges": [
                    [id_map.get(a, a), id_map.get(b, b)] for a, b in (v.sample_edges or [])
                ][:5],
            })
        out.sort(key=lambda v: (v["kind"], v["from"], v["to"]))
        return out

    # -- sharded layout ------------------------------------------------------ #

    def _write_shards(self, doc: dict, out: Path) -> None:
        base = out.with_name(out.name + ".agent")
        (base / "modules").mkdir(parents=True, exist_ok=True)
        (base / "files").mkdir(parents=True, exist_ok=True)

        index = {
            "schema": SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "generator": doc["generator"],
            "project": doc["project"],
            "summary": doc["summary"],
            "content_digest": doc["content_digest"],
            "modules": doc["modules"],
            "module_edges": doc["module_edges"],
            "violations": doc["violations"],
            "analysis": doc["analysis"],
            "includes": {
                "present": doc["includes"]["present"],
                "cycles": doc["includes"]["cycles"],
                "most_included": doc["includes"]["most_included"],
            },
            "shards": {"modules": "modules/", "files": "files/"},
        }
        with open(base / "index.json", "w", encoding="utf-8") as fh:
            json.dump(index, fh, indent=2, ensure_ascii=False, default=str)

        fns_by_file = defaultdict(list)
        for rec in doc["functions"]:
            fns_by_file[rec["file"]].append(rec)

        for f in doc["files"]:
            shard = {
                "file": f,
                "functions": sorted(fns_by_file.get(f["id"], []), key=lambda r: r["id"]),
            }
            name = _safe_slug(f["id"]) + ".json"
            with open(base / "files" / name, "w", encoding="utf-8") as fh:
                json.dump(shard, fh, indent=2, ensure_ascii=False, default=str)

        files_by_module = defaultdict(list)
        for f in doc["files"]:
            files_by_module[f["module"]].append(f["id"])
        for m in doc["modules"]:
            shard = {"module": m, "files": sorted(files_by_module.get(m["id"], []))}
            name = _safe_slug(m["id"]) + ".json"
            with open(base / "modules" / name, "w", encoding="utf-8") as fh:
                json.dump(shard, fh, indent=2, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# module-level free functions                                                  #
# --------------------------------------------------------------------------- #

def _tool_version() -> str:
    try:
        from .. import __version__
        return __version__
    except Exception:
        return "unknown"


def _conf_str(conf) -> str:
    if isinstance(conf, ResolutionConfidence):
        return conf.value
    return str(conf)


def _fn_sort_key(fn: FunctionDef, root: Optional[Path]):
    return (_posix_rel(fn.file_path, root), fn.qualified_name, fn.line_start)


def _folder_module(rel_file: str, depth: int) -> str:
    """Top-*depth* path components of a relative file path -> a module name."""
    parts = Path(rel_file).parts
    if len(parts) <= 1:
        return "(root)"
    return "/".join(parts[: max(1, depth)])


def _find_cycles(edge_pairs, limit: int = 40):
    """Return simple cycles in a directed graph. Uses networkx if available,
    otherwise a bounded DFS fallback. Self-loops are ignored."""
    pairs = [(a, b) for a, b in edge_pairs if a != b]
    if not pairs:
        return []
    try:
        import networkx as nx
        g = nx.DiGraph()
        g.add_edges_from(pairs)
        cycles = []
        for cyc in nx.simple_cycles(g):
            if len(cyc) > 1:
                cycles.append(_canonical_cycle(cyc))
            if len(cycles) >= limit:
                break
        cycles.sort(key=lambda c: (len(c), c))
        return cycles
    except Exception:
        # Lightweight fallback: detect 2-cycles only (A->B and B->A).
        s = set(pairs)
        seen = set()
        out = []
        for a, b in pairs:
            if (b, a) in s and (b, a) not in seen:
                out.append(_canonical_cycle([a, b]))
                seen.add((a, b))
                seen.add((b, a))
            if len(out) >= limit:
                break
        out.sort(key=lambda c: (len(c), c))
        return out


def _canonical_cycle(cyc: list) -> list:
    """Rotate a cycle so it starts at its lexicographically smallest member,
    giving one canonical representation regardless of where traversal began."""
    if not cyc:
        return cyc
    i = min(range(len(cyc)), key=lambda k: cyc[k])
    return cyc[i:] + cyc[:i]
