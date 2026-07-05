"""
Agent Knowledge Pack — multi-file export (``callgraph-agent-knowledge-pack/v1``).

The output of this renderer is a *directory* (``<output>.knowledge/``) containing the
fixed set of files below, built to the project's agent-knowledge-pack specification so an
offline LLM agent can read the whole architecture and convert it to Markdown:

    manifest.json
    architecture.graph.json
    functions.jsonl
    calls.jsonl
    files.jsonl
    variables.jsonl
    modules.json
    architecture.json
    include_graph.json
    build_context.json
    matlab_project.json
    entry_points.json
    parse_quality.json
    indexes.json
    obsidian_agent_instructions.md

Notes
-----
* ``function_id`` follows the declared strategy ``path_rel::qualified_name::line_start``.
* All natural-language ``agent_*`` guidance fields are populated only with facts that can
  be derived deterministically from the static graph — never invented.
* Output is deterministic for a fixed input (records are emitted in sorted order), so the
  pack can be regenerated and diffed as an updatable knowledge source. Use ``--parallel 1``
  for byte-stable output.
"""
from __future__ import annotations

import hashlib
import json
import platform as _platform
import re
import sys
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import Config
from ..models import CallGraph, FunctionDef, ResolutionConfidence
from .base import BaseRenderer

SCHEMA_VERSION = "callgraph-agent-knowledge-pack/v1"
PACKAGE_KIND = "agent_knowledge_pack"
_TOOL_NAME = "Callgraph_tool"


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #

def _tool_version() -> str:
    try:
        from .. import __version__
        return __version__
    except Exception:
        return "unknown"


def _posix_rel(abs_path: str, root: Optional[Path]) -> str:
    if not abs_path:
        return ""
    p = Path(abs_path)
    if root is not None:
        try:
            return p.resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            pass
    return p.name


def _display(abs_path: str, root: Optional[Path]) -> str:
    rel = _posix_rel(abs_path, root)
    return rel or Path(abs_path).name


def _short_hash(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _first_doc(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = text.strip()
    return t.splitlines()[0].strip() if t else None


_TOK = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _terms(identifier: str) -> list:
    """Deterministic identifier tokenisation (camelCase + snake_case) → hint terms."""
    if not identifier:
        return []
    parts = []
    for chunk in identifier.replace("::", "_").replace(".", "_").split("_"):
        parts += _TOK.findall(chunk)
    seen, out = set(), []
    for p in parts:
        lp = p.lower()
        if len(lp) >= 3 and lp not in seen:
            seen.add(lp)
            out.append(lp)
    return out


def _safe_ext(path_rel: str) -> str:
    suf = Path(path_rel).suffix.lower()
    return suf or "unknown"


def _safe_slug(name: str) -> str:
    out = "".join(c if (c.isalnum() or c in "-._") else "_" for c in name)
    return out.strip("_") or "root"


def _config_section(obj) -> dict:
    try:
        if is_dataclass(obj):
            return json.loads(json.dumps(asdict(obj), default=str))
        return json.loads(json.dumps(obj, default=str))
    except Exception:
        return {}


def _conf(conf) -> str:
    if isinstance(conf, ResolutionConfidence):
        return conf.value
    return str(conf)


def _fn_facts(fn, file_rel, n_resolved, n_other, fan_in) -> list:
    facts = [f"Defined in {file_rel} (lines {fn.line_start}-{fn.line_end})."]
    facts.append(f"Calls {n_resolved} project function(s); {n_other} external/unresolved.")
    facts.append(f"Called by {fan_in} project function(s).")
    if fn.is_virtual:
        facts.append("Declared virtual (may be overridden / dynamically dispatched).")
    if fn.is_external:
        facts.append("External symbol — defined outside the analysed sources.")
    return facts


def _call_rules(call, src, dst, resolved, is_ext) -> dict:
    name = call.callee_name
    if resolved:
        if call.confidence_category == "heuristic":
            return {"safe_to_state": [f"{src} likely calls {name} (heuristic name match)."],
                    "not_safe_to_state": ["Do not state this call as certain; it is a name-based guess."]}
        return {"safe_to_state": [f"{src} calls {name} (exact resolution)."], "not_safe_to_state": []}
    if is_ext:
        return {"safe_to_state": [f"{src} calls external/library symbol '{name}'."],
                "not_safe_to_state": ["Do not assume the implementation is in the analysed sources."]}
    return {"safe_to_state": [f"{src} references '{name}', which the tool could not resolve."],
            "not_safe_to_state": ["Do not treat unresolved as absent; it may be dynamic, macro, or external."]}


# --------------------------------------------------------------------------- #
# renderer                                                                     #
# --------------------------------------------------------------------------- #

class AgentPackRenderer(BaseRenderer):
    """Serialise a :class:`CallGraph` into the multi-file agent knowledge pack."""

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.folder_depth = getattr(config.render, "folder_depth", 1)
        self.root: Optional[Path] = None

    # -- entry point --------------------------------------------------------- #

    def render(self, graph: CallGraph, output_path: Path) -> Path:
        self.root = Path(graph.project_root).resolve() if graph.project_root else None

        out = output_path
        if out.suffix.lower() in (".json", ".html", ".svg", ".png", ".dot"):
            out = out.with_suffix("")
        pack = out.with_name(out.name + ".knowledge")
        pack.mkdir(parents=True, exist_ok=True)

        m = self._model(graph)
        integrity: dict[str, str] = {}

        def dump_json(name, obj, ihash_key=None):
            blob = json.dumps(obj, indent=2, ensure_ascii=False, default=str).encode("utf-8")
            (pack / name).write_bytes(blob)
            if ihash_key:
                integrity[ihash_key] = _sha256_bytes(blob)

        def dump_jsonl(name, rows, ihash_key=None):
            lines = [json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) for r in rows]
            blob = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
            (pack / name).write_bytes(blob)
            if ihash_key:
                integrity[ihash_key] = _sha256_bytes(blob)

        dump_json("architecture.graph.json", m["graph"], "graph_hash")
        dump_jsonl("functions.jsonl", m["functions_jsonl"], "functions_hash")
        dump_jsonl("calls.jsonl", m["calls_jsonl"], "calls_hash")
        dump_jsonl("files.jsonl", m["files_jsonl"], "files_hash")
        dump_jsonl("variables.jsonl", m["variables_jsonl"], "variables_hash")
        dump_json("modules.json", m["modules"])
        dump_json("architecture.json", m["architecture"])
        dump_json("include_graph.json", m["include_graph"])
        dump_json("build_context.json", m["build_context"])
        dump_json("matlab_project.json", m["matlab_project"])
        dump_json("entry_points.json", m["entry_points"])
        dump_json("parse_quality.json", m["parse_quality"])
        dump_json("indexes.json", m["indexes"])

        (pack / "obsidian_agent_instructions.md").write_text(_OBSIDIAN_INSTRUCTIONS, encoding="utf-8")

        integrity["config_hash"] = _sha256_bytes(
            json.dumps(m["config_effective"], sort_keys=True, default=str).encode("utf-8")
        )

        dump_json("manifest.json", self._manifest(graph, m, integrity))
        return pack / "manifest.json"

    # -- model computation --------------------------------------------------- #

    def _model(self, graph: CallGraph) -> dict:
        root = self.root

        id_map = {nid: self._fid(fn) for nid, fn in graph.functions.items()}

        fn_to_file_rel = {id_map[nid]: _posix_rel(fn.file_path, root) for nid, fn in graph.functions.items()}
        file_to_module = self._file_module_map(graph, fn_to_file_rel)
        fn_to_module = {fid: file_to_module.get(fr) for fid, fr in fn_to_file_rel.items()}
        module_id_of = lambda name: ("module:" + name) if name else None

        call_rows, by_caller, by_callee = self._calls(graph, id_map, fn_to_file_rel, file_to_module)
        viol_fn_ids = self._violation_fn_ids(graph, id_map)
        fn_records = self._functions(graph, id_map, fn_to_file_rel, fn_to_module,
                                     module_id_of, by_caller, by_callee, viol_fn_ids)
        file_records, _file_edges = self._files(graph, id_map, fn_to_file_rel, file_to_module,
                                                module_id_of, by_caller, by_callee, call_rows)
        modules_doc = self._modules(graph, fn_records, file_records, file_to_module, fn_to_module,
                                    module_id_of, call_rows)
        architecture_doc = self._architecture(graph, id_map, fn_to_file_rel, call_rows)
        include_doc = self._include_graph(graph)
        build_doc = self._build_context(graph)
        matlab_doc = self._matlab(graph)
        entry_doc = self._entry_points(fn_records, call_rows)
        pq_doc = self._parse_quality(graph, call_rows, fn_records)
        idx_doc = self._indexes(fn_records, file_records, modules_doc, call_rows, graph, id_map)
        variables_jsonl = self._variables(graph, id_map, fn_to_file_rel)

        snapshot_id = "snap-" + _short_hash({
            "f": [r["function_id"] + ":" + r["signature"] for r in fn_records],
            "c": [r["call_id"] + ":" + (r["callee"]["function_id"] or r["callee"]["raw_name"] or "") for r in call_rows],
            "v": len(graph.violations),
        })

        self._stamp(fn_records, snapshot_id, "function")
        self._stamp(call_rows, snapshot_id, "call")
        self._stamp(file_records, snapshot_id, "file")
        self._stamp(variables_jsonl, snapshot_id, "variable")

        config_effective = {
            "filter": _config_section(self.config.filter),
            "variables": _config_section(self.config.variables),
            "output": _config_section(self.config.output),
            "build": _config_section(self.config.build),
            "selection": _config_section(self.config.selection),
            "render": _config_section(self.config.render),
            "include_graph": _config_section(self.config.include_graph),
            "architecture": _config_section(self.config.architecture),
            "parser": _config_section(getattr(self.config, "parser", {})),
        }

        graph_doc = self._canonical_graph(graph, snapshot_id, fn_records, call_rows)

        return {
            "snapshot_id": snapshot_id,
            "config_effective": config_effective,
            "graph": graph_doc,
            "functions_jsonl": fn_records,
            "calls_jsonl": call_rows,
            "files_jsonl": file_records,
            "variables_jsonl": variables_jsonl,
            "modules": self._with_meta(modules_doc, snapshot_id, "modules"),
            "architecture": self._with_meta(architecture_doc, snapshot_id, "architecture"),
            "include_graph": self._with_meta(include_doc, snapshot_id, "include_graph"),
            "build_context": self._with_meta(build_doc, snapshot_id, "build_context"),
            "matlab_project": self._with_meta(matlab_doc, snapshot_id, "matlab_project"),
            "entry_points": self._with_meta(entry_doc, snapshot_id, "entry_points"),
            "parse_quality": self._with_meta(pq_doc, snapshot_id, "parse_quality"),
            "indexes": self._with_meta(idx_doc, snapshot_id, "indexes"),
        }

    # -- builders ------------------------------------------------------------ #

    def _fid(self, fn: FunctionDef) -> str:
        rel = _posix_rel(fn.file_path, self.root) or Path(fn.file_path).name
        return f"{rel}::{fn.qualified_name}::{fn.line_start}"

    def _file_block(self, abs_path: str) -> dict:
        return {"path_abs": abs_path or None, "path_rel": _posix_rel(abs_path, self.root),
                "path_display": _display(abs_path, self.root)}

    def _file_module_map(self, graph, fn_to_file_rel) -> dict:
        explicit = {}
        if graph.modules:
            for name, mod in graph.modules.items():
                for abs_f in mod.files:
                    explicit[_posix_rel(abs_f, self.root)] = name
        out = {}
        for fr in set(fn_to_file_rel.values()):
            if fr in explicit:
                out[fr] = explicit[fr]
            else:
                parts = Path(fr).parts
                out[fr] = "(root)" if len(parts) <= 1 else "/".join(parts[: max(1, self.folder_depth)])
        return out

    def _calls(self, graph, id_map, fn_to_file_rel, file_to_module):
        items = [c for c in graph.calls if id_map.get(c.caller_id) is not None]
        items.sort(key=lambda c: (id_map.get(c.caller_id, ""), _posix_rel(c.call_file, self.root),
                                  c.call_line, c.callee_name or ""))
        viol_pairs = set()
        for v in graph.violations:
            for a, b in (v.sample_edges or []):
                viol_pairs.add((id_map.get(a), id_map.get(b)))

        rows, by_caller, by_callee = [], defaultdict(list), defaultdict(list)
        for i, call in enumerate(items, 1):
            cid = f"call:{i:07d}"
            src = id_map[call.caller_id]
            dst = id_map.get(call.callee_id) if call.callee_id else None
            resolved = bool(call.is_resolved and dst)
            sf = fn_to_file_rel.get(src, "")
            df = fn_to_file_rel.get(dst, "") if dst else None
            sm = file_to_module.get(sf)
            dm = file_to_module.get(df) if df else None
            same_file = bool(df) and sf == df
            same_mod = bool(dm) and sm == dm
            is_ext = (call.confidence_category == "external")
            row = {
                "call_id": cid,
                "caller": {"function_id": src,
                           "name": _name_of(src), "qualified_name": _qual_of(src),
                           "file_rel": sf, "module": sm},
                "callee": {"function_id": dst, "raw_name": call.callee_name,
                           "resolved_name": _name_of(dst) if dst else None,
                           "qualified_name": _qual_of(dst) if dst else None,
                           "file_rel": df, "module": dm,
                           "is_project_function": bool(dst), "is_external": is_ext},
                "source_location": {"call_file_abs": call.call_file or None,
                                    "call_file_rel": _posix_rel(call.call_file, self.root),
                                    "call_line": call.call_line},
                "call_args": list(call.call_args or []),
                "resolution": {"is_resolved": resolved,
                               "resolution_confidence": _conf(call.resolution_confidence),
                               "confidence_category": call.confidence_category,
                               "resolution_reason": call.resolution_reason or "",
                               "resolution_hint": call.resolution_hint or ""},
                "aggregation": {"underlying_count": max(1, call.underlying_count),
                                "sample_call_sites": [{"file": _posix_rel(f, self.root), "line": ln}
                                                      for f, ln in (call.sample_call_sites or [])][:5]},
                "relationship_classification": {
                    "same_function": src == dst, "same_file": same_file,
                    "cross_file": bool(df) and not same_file, "same_module": same_mod,
                    "cross_module": bool(dm) and not same_mod,
                    "external_or_unresolved": not resolved,
                    "architecture_violation": (src, dst) in viol_pairs},
                "agent_interpretation_rules": _call_rules(call, src, dst, resolved, is_ext),
            }
            rows.append(row)
            by_caller[src].append(row)
            if dst:
                by_callee[dst].append(row)
        return rows, by_caller, by_callee

    def _functions(self, graph, id_map, fn_to_file_rel, fn_to_module, module_id_of,
                   by_caller, by_callee, viol_fn_ids):
        records = []
        for nid, fn in sorted(graph.functions.items(),
                              key=lambda kv: (_posix_rel(kv[1].file_path, self.root),
                                              kv[1].qualified_name, kv[1].line_start)):
            fid = id_map[nid]
            outgoing = by_caller.get(fid, [])
            incoming = by_callee.get(fid, [])
            fr = fn_to_file_rel.get(fid, "")
            mod = fn_to_module.get(fid)
            resolved = [c for c in outgoing if c["resolution"]["is_resolved"]]
            heuristic = [c for c in resolved if c["resolution"]["confidence_category"] == "heuristic"]
            external = [c for c in outgoing if c["callee"]["is_external"]]
            unresolved = [c for c in outgoing if not c["resolution"]["is_resolved"] and not c["callee"]["is_external"]]
            cross_file = [c for c in resolved if c["relationship_classification"]["cross_file"]]
            cross_mod = [c for c in resolved if c["relationship_classification"]["cross_module"]]
            records.append({
                "function_id": fid, "stable_id_strategy": "path::qualified_name::line_start",
                "name": fn.name, "qualified_name": fn.qualified_name, "display_name": fn.qualified_name,
                "signature": fn.signature(),
                "language": {"enum": fn.language.name, "display": fn.language.display_name()},
                "file": self._file_block(fn.file_path),
                "location": {"line_start": fn.line_start, "line_end": fn.line_end,
                             "line_span": max(0, (fn.line_end or 0) - (fn.line_start or 0))},
                "kind": {"func_type": fn.func_type or ("method" if fn.is_method else "function"),
                         "is_external": bool(fn.is_external), "is_method": bool(fn.is_method),
                         "is_virtual": bool(fn.is_virtual), "parent": fn.parent},
                "parameters": [self._param(p) for p in fn.parameters],
                "return_type": fn.return_type, "docstring": fn.docstring,
                "tracked_vars": dict(fn.tracked_vars),
                "variables": [self._var_inline(v) for v in fn.variables],
                "calls_out": {"count": len(outgoing), "resolved": len(resolved),
                              "heuristic": len(heuristic), "unresolved": len(unresolved),
                              "external": len(external), "call_ids": [c["call_id"] for c in outgoing]},
                "calls_in": {"count": len(incoming), "call_ids": [c["call_id"] for c in incoming]},
                "callers": [{"function_id": c["caller"]["function_id"],
                             "qualified_name": c["caller"]["qualified_name"],
                             "file_rel": c["caller"]["file_rel"], "module": c["caller"]["module"],
                             "call_id": c["call_id"]} for c in incoming],
                "callees": [{"function_id": c["callee"]["function_id"], "callee_name": c["callee"]["raw_name"],
                             "qualified_name": c["callee"]["qualified_name"], "file_rel": c["callee"]["file_rel"],
                             "module": c["callee"]["module"], "call_id": c["call_id"],
                             "resolution_confidence": c["resolution"]["resolution_confidence"],
                             "confidence_category": c["resolution"]["confidence_category"]} for c in outgoing],
                "module": {"name": mod, "module_id": module_id_of(mod),
                           "inferred_from": self._inferred_from(graph, fr)},
                "architecture": {"participates_in_violation": fid in viol_fn_ids,
                                 "violation_ids": sorted(viol_fn_ids.get(fid, []))},
                "graph_metrics": {"fan_in": len(incoming), "fan_out": len(outgoing),
                                  "resolved_fan_out": len(resolved), "heuristic_fan_out": len(heuristic),
                                  "unresolved_fan_out": len(unresolved), "external_fan_out": len(external),
                                  "cross_file_fan_out": len(cross_file), "cross_module_fan_out": len(cross_mod)},
                "agent_evidence": {
                    "docstring": _first_doc(fn.docstring),
                    "variable_source_lines": sorted({v.line for v in fn.variables if v.source_detail}),
                    "important_terms": _terms(fn.qualified_name),
                    "safe_summary_facts": _fn_facts(fn, fr, len(resolved), len(external) + len(unresolved), len(incoming)),
                    "limitations": ["Static analysis only; dynamic dispatch and runtime callbacks may be missing."]},
            })
        return records

    def _param(self, p) -> dict:
        return {"name": p.name, "type_hint": p.type_hint, "display": str(p),
                "is_dead": bool(p.is_dead), "dead_category": p.dead_category,
                "dead_confidence": p.dead_confidence, "read_lines": list(p.read_lines or []),
                "write_lines": list(p.write_lines or []), "is_suppressed": bool(p.is_suppressed),
                "suppress_reason": p.suppress_reason}

    def _var_inline(self, v) -> dict:
        return {"name": v.name, "scope": v.scope, "type_hint": v.type_hint, "value": v.value,
                "line": v.line, "source_kind": v.source_kind, "source_detail": v.source_detail,
                "is_dead": bool(v.is_dead), "dead_category": v.dead_category, "dead_confidence": v.dead_confidence}

    def _variables(self, graph, id_map, fn_to_file_rel):
        rows = []
        for nid, fn in sorted(graph.functions.items(),
                              key=lambda kv: (_posix_rel(kv[1].file_path, self.root),
                                              kv[1].qualified_name, kv[1].line_start)):
            fid = id_map[nid]
            fr = fn_to_file_rel.get(fid, "")
            for v in fn.variables:
                rows.append({
                    "variable_id": f"{fid}::var::{v.name}::{v.line}", "name": v.name, "kind": "variable",
                    "scope": v.scope, "type_hint": v.type_hint, "value": v.value,
                    "function": {"function_id": fid, "qualified_name": fn.qualified_name, "file_rel": fr,
                                 "line_start": fn.line_start, "line_end": fn.line_end},
                    "file": {"path_abs": v.file_path or fn.file_path or None,
                             "path_rel": _posix_rel(v.file_path or fn.file_path, self.root)},
                    "location": {"line": v.line, "read_lines": list(v.read_lines or []),
                                 "write_lines": list(v.write_lines or [])},
                    "source": {"context": v.context, "source_kind": v.source_kind, "source_detail": v.source_detail,
                               "full_source": v.full_source, "doc_comment": v.doc_comment},
                    "flow_metadata": {"tracked_value_repr": fn.tracked_vars.get(v.name),
                                      "connect_path": v.connect_path, "connect_input_name": v.connect_input_name,
                                      "custom_input_func": v.custom_input_func,
                                      "custom_input_classifier": v.custom_input_classifier,
                                      "parent_name": v.parent_name, "assign_src": v.assign_src},
                    "dead_code": {"is_dead": bool(v.is_dead), "dead_reason": v.dead_reason,
                                  "dead_category": v.dead_category, "dead_confidence": v.dead_confidence,
                                  "is_suppressed": bool(v.is_suppressed), "suppress_reason": v.suppress_reason},
                    "agent_evidence": {
                        "safe_facts": [f"Variable '{v.name}' in {fn.qualified_name} at {fr}:{v.line}."],
                        "possible_meaning_terms": _terms(v.name),
                        "do_not_claim_global_data_flow_unless_call_arg_flow_exists": True},
                })
        rows.sort(key=lambda r: r["variable_id"])
        return rows

    def _files(self, graph, id_map, fn_to_file_rel, file_to_module, module_id_of,
               by_caller, by_callee, call_rows):
        fn_by_file = defaultdict(list)
        for nid, fn in graph.functions.items():
            if fn.is_external:
                continue
            fn_by_file[_posix_rel(fn.file_path, self.root)].append((id_map[nid], fn))

        out_pair = defaultdict(int)
        for c in call_rows:
            if not c["callee"]["function_id"]:
                continue
            sf, df = c["caller"]["file_rel"], c["callee"]["file_rel"]
            if sf and df and sf != df:
                out_pair[(sf, df)] += 1
        out_by, in_by = defaultdict(list), defaultdict(list)
        for (a, b), n in out_pair.items():
            out_by[a].append({"file_id": "file:" + b, "path_rel": b, "calls_count": n})
            in_by[b].append({"file_id": "file:" + a, "path_rel": a, "calls_count": n})

        incl_map, included_by_map, cycle_files = self._include_maps(graph)

        records = []
        for fr in sorted(fn_by_file):
            defs = sorted(fn_by_file[fr], key=lambda t: t[0])
            abs_path = next((fn.file_path for _, fn in defs), fr)
            langs = {fn.language.display_name() for _, fn in defs}
            lang = next(iter(langs)) if len(langs) == 1 else "mixed"
            mod = file_to_module.get(fr)
            ext_callees, unres_callees = set(), set()
            for fid, _fn in defs:
                for c in by_caller.get(fid, []):
                    if c["callee"]["is_external"]:
                        ext_callees.add(c["callee"]["raw_name"])
                    elif not c["resolution"]["is_resolved"]:
                        unres_callees.add(c["callee"]["raw_name"])
            records.append({
                "file_id": "file:" + fr,
                "path": {**self._file_block(abs_path), "extension": _safe_ext(fr)},
                "language": lang,
                "module": {"name": mod, "module_id": module_id_of(mod),
                           "inferred_from": self._inferred_from(graph, fr)},
                "build_membership": self._build_membership(graph, abs_path),
                "functions_defined": [{"function_id": fid, "qualified_name": fn.qualified_name,
                                       "line_start": fn.line_start, "line_end": fn.line_end,
                                       "is_external": bool(fn.is_external), "is_method": bool(fn.is_method),
                                       "is_virtual": bool(fn.is_virtual)} for fid, fn in defs],
                "file_call_graph": {
                    "outgoing_calls_count": sum(e["calls_count"] for e in out_by.get(fr, [])),
                    "incoming_calls_count": sum(e["calls_count"] for e in in_by.get(fr, [])),
                    "outgoing_files": sorted(out_by.get(fr, []), key=lambda e: e["path_rel"]),
                    "incoming_files": sorted(in_by.get(fr, []), key=lambda e: e["path_rel"]),
                    "external_callees": sorted(ext_callees), "unresolved_callees": sorted(unres_callees)},
                "include_graph": {"includes": incl_map.get(fr, []), "included_by": included_by_map.get(fr, []),
                                  "unresolved_includes": [e for e in incl_map.get(fr, []) if not e["resolved"]],
                                  "excluded_includes": [], "participates_in_include_cycle": fr in cycle_files,
                                  "include_cycle_ids": []},
                "architecture": {"outgoing_modules": [], "incoming_modules": [], "violations": []},
                "quality": {"parse_errors": [], "warnings": []},
                "agent_notes": {"safe_summary_facts": [
                    f"Defines {len(defs)} function(s).",
                    f"Calls into {len(out_by.get(fr, []))} other file(s); called by {len(in_by.get(fr, []))}."],
                    "limitations": ["File-level edges are aggregated from resolved calls only."]},
            })
        file_edges = [{"from": a, "to": b, "count": n} for (a, b), n in sorted(out_pair.items())]
        return records, file_edges

    def _modules(self, graph, fn_records, file_records, file_to_module, fn_to_module, module_id_of, call_rows):
        mod_files = defaultdict(list)
        for f in file_records:
            mod_files[f["module"]["name"]].append(f)
        fn_by_module = defaultdict(list)
        for r in fn_records:
            if not r["kind"]["is_external"]:
                fn_by_module[r["module"]["name"]].append(r)

        edge_count, edge_breakdown, edge_samples, internal_calls = (
            defaultdict(int), defaultdict(lambda: defaultdict(int)), defaultdict(list), defaultdict(list))
        callee_index = defaultdict(list)
        for c in call_rows:
            if c["callee"]["function_id"]:
                callee_index[c["callee"]["function_id"]].append(c)
            sm, dm = c["caller"]["module"], c["callee"]["module"]
            if sm and dm and sm != dm:
                edge_count[(sm, dm)] += 1
                edge_breakdown[(sm, dm)][c["resolution"]["confidence_category"]] += 1
                if len(edge_samples[(sm, dm)]) < 5:
                    edge_samples[(sm, dm)].append(c["call_id"])
            elif sm and dm and sm == dm:
                internal_calls[sm].append(c["call_id"])

        viol_pairs = {(v.from_module, v.to_module) for v in graph.violations}
        incoming, outgoing = defaultdict(list), defaultdict(list)
        for (a, b), n in edge_count.items():
            outgoing[a].append({"module": b, "calls_count": n, "sample_call_ids": edge_samples[(a, b)]})
            incoming[b].append({"module": a, "calls_count": n, "sample_call_ids": edge_samples[(a, b)]})

        modules = []
        for name in sorted(mod_files):
            fns = fn_by_module.get(name, [])
            public, internal = [], []
            for r in fns:
                callers_outside = any(c["caller"]["module"] != name for c in callee_index.get(r["function_id"], []))
                if callers_outside:
                    public.append({"function_id": r["function_id"], "qualified_name": r["qualified_name"],
                                   "reason": "called_from_another_module"})
                elif r["calls_in"]["count"] == 0:
                    public.append({"function_id": r["function_id"], "qualified_name": r["qualified_name"],
                                   "reason": "no_project_callers"})
                else:
                    internal.append({"function_id": r["function_id"], "qualified_name": r["qualified_name"],
                                     "reason": "only_called_inside_same_module"})
            modules.append({
                "module_id": module_id_of(name), "name": name,
                "inferred_from": self._module_inferred_from(graph, name), "project": None,
                "files": [{"file_id": f["file_id"], "path_rel": f["path"]["path_rel"]}
                          for f in sorted(mod_files[name], key=lambda x: x["path"]["path_rel"])],
                "functions": [{"function_id": r["function_id"], "qualified_name": r["qualified_name"],
                               "file_rel": r["file"]["path_rel"]} for r in sorted(fns, key=lambda x: x["function_id"])],
                "module_metrics": {
                    "files_count": len(mod_files[name]), "functions_count": len(fns),
                    "incoming_calls_count": sum(e["calls_count"] for e in incoming.get(name, [])),
                    "outgoing_calls_count": sum(e["calls_count"] for e in outgoing.get(name, [])),
                    "internal_calls_count": len(internal_calls.get(name, [])),
                    "external_or_unresolved_calls_count": sum(r["calls_out"]["external"] + r["calls_out"]["unresolved"] for r in fns),
                    "incoming_modules_count": len(incoming.get(name, [])),
                    "outgoing_modules_count": len(outgoing.get(name, [])),
                    "architecture_violations_count": sum(1 for v in graph.violations if v.from_module == name)},
                "dependencies": {"incoming_modules": sorted(incoming.get(name, []), key=lambda e: e["module"]),
                                 "outgoing_modules": sorted(outgoing.get(name, []), key=lambda e: e["module"]),
                                 "internal_call_ids": internal_calls.get(name, []),
                                 "cross_module_call_ids": [s for e in outgoing.get(name, []) for s in e["sample_call_ids"]]},
                "public_api_candidates": sorted(public, key=lambda x: x["function_id"])[:100],
                "internal_function_candidates": sorted(internal, key=lambda x: x["function_id"])[:100],
                "architecture": {"violations": [v.reason for v in graph.violations if v.from_module == name],
                                 "rules_touching_this_module": []},
                "agent_summary_hints": {"safe_facts": [f"Module '{name}' groups {len(mod_files[name])} file(s) and {len(fns)} function(s)."],
                                        "inference_allowed_if_marked": True},
            })

        module_edges = []
        for (a, b), n in sorted(edge_count.items()):
            bd = edge_breakdown[(a, b)]
            module_edges.append({"edge_id": f"medge:{_safe_slug(a)}->{_safe_slug(b)}",
                                 "from_module": a, "to_module": b, "calls_count": n,
                                 "confidence_breakdown": {k: bd.get(k, 0) for k in ("exact", "heuristic", "unresolved", "external", "violation")},
                                 "sample_call_ids": edge_samples[(a, b)], "is_violation": (a, b) in viol_pairs})
        return {"modules": modules, "module_edges": module_edges}

    def _architecture(self, graph, id_map, fn_to_file_rel, call_rows):
        rules = []
        for i, raw in enumerate(getattr(self.config.architecture, "rules", []) or []):
            d = raw if isinstance(raw, dict) else _config_section(raw)
            rules.append({"rule_id": f"rule:{i:03d}", "kind": d.get("kind"),
                          "from_module": d.get("from_module") or d.get("from"),
                          "to_module": d.get("to_module") or d.get("to"),
                          "allowed_targets": d.get("allowed_targets", []), "layers": d.get("layers", []),
                          "reason": d.get("reason", ""), "source": "config.architecture.rules"})
        call_by_pair = defaultdict(list)
        for c in call_rows:
            call_by_pair[(c["caller"]["function_id"], c["callee"]["function_id"])].append(c)
        violations, by_kind, by_pair = [], defaultdict(int), defaultdict(int)
        for i, v in enumerate(graph.violations):
            samples = []
            for a, b in (v.sample_edges or [])[:5]:
                fa, fb = id_map.get(a, a), id_map.get(b, b)
                cm = call_by_pair.get((fa, fb), [])
                samples.append({"caller_id": fa, "callee_id": fb,
                                "call_id": cm[0]["call_id"] if cm else None,
                                "caller_name": _qual_of(fa), "callee_name": _qual_of(fb),
                                "caller_file_rel": fn_to_file_rel.get(fa), "callee_file_rel": fn_to_file_rel.get(fb)})
            violations.append({"violation_id": f"viol:{i:03d}", "rule_kind": v.rule_kind,
                               "from_module": v.from_module, "to_module": v.to_module, "reason": v.reason,
                               "sample_edges": samples,
                               "severity": {"level": "warning", "reason": "Reported by the architecture rule engine."},
                               "agent_explanation": {"safe_statement": "This is a reported architecture violation generated by the rule engine.",
                                                     "do_not_reinterpret": True}})
            by_kind[v.rule_kind] += 1
            by_pair[f"{v.from_module}->{v.to_module}"] += 1
        return {"rules": rules, "violations": violations,
                "summary": {"rules_count": len(rules), "violations_count": len(violations),
                            "violations_by_kind": dict(by_kind), "violations_by_module_pair": dict(by_pair)}}

    def _include_maps(self, graph):
        incl_map, included_by_map, cycle_files = defaultdict(list), defaultdict(list), set()
        ig = graph.include_graph
        if ig:
            for from_file, lst in ig.files.items():
                ff = _posix_rel(from_file, self.root)
                for e in lst:
                    tf = _posix_rel(e.to_file, self.root) if e.resolved and e.to_file else None
                    incl_map[ff].append({"to_file": tf, "raw_target": e.raw_target, "is_system": bool(e.is_system),
                                         "resolved": bool(e.resolved), "line": e.line, "guard": e.guard})
                    if tf:
                        included_by_map[tf].append({"from_file": ff, "line": e.line, "raw_target": e.raw_target})
            for cyc in ig.cycles:
                for p in cyc:
                    cycle_files.add(_posix_rel(p, self.root))
        return incl_map, included_by_map, cycle_files

    def _include_graph(self, graph):
        ig = graph.include_graph
        enabled = bool(getattr(self.config.include_graph, "enabled", False))
        follow = bool(getattr(self.config.include_graph, "follow_system", False))
        if not ig:
            return {"enabled": enabled, "follow_system_headers": follow, "files": [], "edges": [],
                    "unresolved": [], "excluded": [], "cycles": [], "most_included": [],
                    "summary": {"files_count": 0, "edges_count": 0, "unresolved_count": 0,
                                "excluded_count": 0, "cycles_count": 0, "system_edges_count": 0},
                    "agent_usage": _INCLUDE_USAGE}
        files, edges, eid = [], [], 0
        for from_file, lst in sorted(ig.files.items()):
            ff = _posix_rel(from_file, self.root)
            includes = []
            for e in lst:
                eid += 1
                rec = {"include_edge_id": f"inc:{eid:06d}", "from_file": ff, "to_file": e.to_file,
                       "to_file_rel": _posix_rel(e.to_file, self.root) if e.to_file else None,
                       "raw_target": e.raw_target, "is_system": bool(e.is_system),
                       "resolved": bool(e.resolved), "line": e.line, "guard": e.guard}
                includes.append(rec)
                edges.append(rec)
            files.append({"file_id": "file:" + ff, "path_abs": from_file, "path_rel": ff,
                          "includes": includes, "included_by": []})
        unresolved = [e for e in edges if not e["resolved"]]
        system_edges = [e for e in edges if e["is_system"]]
        cycles = [{"cycle_id": f"icyc:{i:03d}", "files": [{"path_abs": p, "path_rel": _posix_rel(p, self.root)} for p in cyc],
                   "length": len(cyc)} for i, cyc in enumerate(ig.cycles)]
        most = [{"file": p, "file_rel": _posix_rel(p, self.root), "included_count": n} for p, n in ig.most_included]
        return {"enabled": enabled, "follow_system_headers": follow, "files": files, "edges": edges,
                "unresolved": unresolved, "excluded": [], "cycles": cycles, "most_included": most,
                "summary": {"files_count": len(files), "edges_count": len(edges), "unresolved_count": len(unresolved),
                            "excluded_count": 0, "cycles_count": len(cycles), "system_edges_count": len(system_edges)},
                "agent_usage": _INCLUDE_USAGE}

    def _build_context(self, graph):
        bi = graph.build_info
        if not bi:
            return {"build_info_available": False, "source": "folder", "compile_commands_path": None,
                    "configuration": None, "platform": None, "projects": [], "compile_units": [],
                    "global_defines": {}, "global_includes": [], "files_not_in_compile_commands": [],
                    "cc_files_not_found": [], "warnings": [], "agent_interpretation": _BUILD_INTERP}
        units = []
        for src, u in sorted((bi.units or {}).items()):
            units.append({"source_file": u.source_file, "source_file_rel": _posix_rel(u.source_file, self.root),
                          "directory": u.directory, "command": u.command, "arguments": list(u.arguments or []),
                          "includes": list(u.includes or []), "defines": dict(u.defines or {}),
                          "extra_flags": list(u.extra_flags or [])})
        return {"build_info_available": True, "source": bi.source, "compile_commands_path": bi.compile_commands_path,
                "configuration": bi.configuration, "platform": bi.platform, "projects": list(bi.projects or []),
                "compile_units": units, "global_defines": dict(bi.global_defines or {}),
                "global_includes": list(bi.global_includes or []),
                "files_not_in_compile_commands": [{"path_abs": p, "path_rel": _posix_rel(p, self.root)}
                                                  for p in (bi.files_not_in_compile_commands or [])],
                "cc_files_not_found": list(bi.cc_files_not_found or []), "warnings": list(bi.warnings or []),
                "agent_interpretation": _BUILD_INTERP}

    def _matlab(self, graph):
        mp = graph.matlab_project
        if not mp:
            return {"available": False, "source": None, "project_name": None, "project_files": [],
                    "packages": [], "class_folders": [], "addpath_dirs": [], "warnings": [],
                    "agent_interpretation": _MATLAB_INTERP}
        return {"available": True, "source": mp.source, "project_name": mp.project_name,
                "project_files": list(mp.project_files or []),
                "packages": [{"name": p, "path": None} for p in (mp.packages or [])],
                "class_folders": [{"class_name": c, "path": None} for c in (mp.class_folders or [])],
                "addpath_dirs": list(mp.addpath_dirs or []), "warnings": list(mp.warnings or []),
                "agent_interpretation": _MATLAB_INTERP}

    def _entry_points(self, fn_records, call_rows):
        configured = []
        for name in (getattr(self.config.filter, "entry_points", []) or []):
            matched = [r["function_id"] for r in fn_records if r["name"] == name or r["qualified_name"] == name]
            configured.append({"name": name, "matched_function_ids": matched,
                               "source": "config.filter.entry_points | CLI --entry",
                               "max_depth": getattr(self.config.filter, "max_depth", 0) or 0})
        std_names = {"main", "winmain", "wmain", "dllmain", "setup", "loop", "_start", "start", "run", "init"}
        roots, std = [], []
        for r in fn_records:
            if r["kind"]["is_external"]:
                continue
            if r["calls_in"]["count"] == 0:
                roots.append({"function_id": r["function_id"], "qualified_name": r["qualified_name"],
                              "file_rel": r["file"]["path_rel"], "module": r["module"]["name"],
                              "reason": "no project callers", "confidence": "heuristic"})
            if r["name"].lower() in std_names:
                std.append({"function_id": r["function_id"], "qualified_name": r["qualified_name"],
                            "reason": "standard entry-point name", "confidence": "heuristic"})
        flow = [{"root_function_id": r["function_id"], "qualified_name": r["qualified_name"],
                 "downstream_function_count": 0, "downstream_module_count": 0, "unresolved_edges_count": 0,
                 "architecture_violations_count": 0,
                 "recommended_md_page": "Flows/" + _safe_slug(r["function_id"]) + ".md"}
                for r in sorted(roots, key=lambda x: x["function_id"])[:30]]
        return {"configured": configured, "graph_roots": sorted(roots, key=lambda x: x["function_id"])[:200],
                "standard_entry_candidates": sorted(std, key=lambda x: x["function_id"]),
                "flow_candidates": flow, "agent_usage": _ENTRY_USAGE}

    def _parse_quality(self, graph, call_rows, fn_records):
        resolved = [c for c in call_rows if c["resolution"]["is_resolved"]]
        heuristic = [c for c in resolved if c["resolution"]["confidence_category"] == "heuristic"]
        unresolved = [c for c in call_rows if not c["resolution"]["is_resolved"]]
        externals = [r for r in fn_records if r["kind"]["is_external"]]
        return {
            "summary": {"files_parsed": graph.total_files_parsed, "parse_errors_count": len(graph.parse_errors),
                        "functions_count": len([r for r in fn_records if not r["kind"]["is_external"]]),
                        "calls_count": len(call_rows), "resolved_calls": len(resolved),
                        "heuristic_calls": len(heuristic), "unresolved_calls": len(unresolved),
                        "external_functions": len(externals), "architecture_violations": len(graph.violations),
                        "include_unresolved": len(graph.include_graph.unresolved) if graph.include_graph else 0,
                        "include_cycles": len(graph.include_graph.cycles) if graph.include_graph else 0},
            "parse_errors": [{"error_id": f"perr:{i:03d}", "message": str(e), "file_rel": None, "language": None}
                             for i, e in enumerate(graph.parse_errors[:200])],
            "unresolved_calls_sample": [{"call_id": c["call_id"], "caller_id": c["caller"]["function_id"],
                                         "callee_name": c["callee"]["raw_name"], "file_rel": c["source_location"]["call_file_rel"],
                                         "line": c["source_location"]["call_line"],
                                         "resolution_reason": c["resolution"]["resolution_reason"],
                                         "resolution_hint": c["resolution"]["resolution_hint"]} for c in unresolved[:50]],
            "heuristic_calls_sample": [{"call_id": c["call_id"], "caller_id": c["caller"]["function_id"],
                                        "callee_id": c["callee"]["function_id"], "callee_name": c["callee"]["raw_name"],
                                        "file_rel": c["source_location"]["call_file_rel"], "line": c["source_location"]["call_line"],
                                        "resolution_reason": c["resolution"]["resolution_reason"]} for c in heuristic[:50]],
            "external_functions_sample": [{"function_id": r["function_id"], "name": r["name"]} for r in externals[:50]],
            "build_warnings": list(graph.build_info.warnings) if graph.build_info else [], "include_warnings": [],
            "known_limitations": _KNOWN_LIMITATIONS,
            "agent_rules": {"must_disclose_uncertainty": True, "must_not_hide_parse_errors": True,
                            "must_not_treat_unresolved_as_absent": True}}

    def _indexes(self, fn_records, file_records, modules_doc, call_rows, graph, id_map):
        by_id, by_name, by_qual = {}, defaultdict(list), defaultdict(list)
        for r in fn_records:
            by_id[r["function_id"]] = {"qualified_name": r["qualified_name"], "file_rel": r["file"]["path_rel"],
                                       "module": r["module"]["name"], "line_start": r["location"]["line_start"],
                                       "line_end": r["location"]["line_end"]}
            by_name[r["name"]].append({"function_id": r["function_id"], "qualified_name": r["qualified_name"],
                                       "file_rel": r["file"]["path_rel"], "module": r["module"]["name"]})
            by_qual[r["qualified_name"]].append(r["function_id"])
        by_file = {f["path"]["path_rel"]: {"file_id": f["file_id"], "language": f["language"],
                   "module": f["module"]["name"], "function_ids": [d["function_id"] for d in f["functions_defined"]]}
                   for f in file_records}
        by_module = {m["name"]: {"module_id": m["module_id"], "file_ids": [x["file_id"] for x in m["files"]],
                     "function_ids": [x["function_id"] for x in m["functions"]],
                     "incoming_modules": [e["module"] for e in m["dependencies"]["incoming_modules"]],
                     "outgoing_modules": [e["module"] for e in m["dependencies"]["outgoing_modules"]]}
                     for m in modules_doc["modules"]}
        by_ext, by_unres, by_var = defaultdict(list), defaultdict(list), defaultdict(list)
        for c in call_rows:
            entry = {"call_id": c["call_id"], "caller_id": c["caller"]["function_id"],
                     "file_rel": c["source_location"]["call_file_rel"], "line": c["source_location"]["call_line"]}
            if c["callee"]["is_external"]:
                by_ext[c["callee"]["raw_name"]].append(entry)
            elif not c["resolution"]["is_resolved"]:
                by_unres[c["callee"]["raw_name"]].append(entry)
        for nid, fn in graph.functions.items():
            for v in fn.variables:
                by_var[v.name].append({"variable_id": f"{id_map[nid]}::var::{v.name}::{v.line}",
                                       "function_id": id_map[nid], "file_rel": _posix_rel(fn.file_path, self.root),
                                       "scope": v.scope})
        by_viol = {f"viol:{i:03d}": {"from_module": v.from_module, "to_module": v.to_module, "sample_call_ids": []}
                   for i, v in enumerate(graph.violations)}
        internal = [r for r in fn_records if not r["kind"]["is_external"]]
        hi_in = sorted(internal, key=lambda r: (-r["graph_metrics"]["fan_in"], r["function_id"]))[:15]
        hi_out = sorted(internal, key=lambda r: (-r["graph_metrics"]["fan_out"], r["function_id"]))[:15]
        most_incl = ([{"file_rel": _posix_rel(p, self.root), "included_count": n}
                      for p, n in graph.include_graph.most_included] if graph.include_graph else [])
        mods_out = sorted(modules_doc["modules"], key=lambda m: -m["module_metrics"]["outgoing_modules_count"])[:15]
        return {"by_function_id": by_id, "by_function_name": {k: v for k, v in sorted(by_name.items())},
                "by_qualified_name": {k: sorted(v) for k, v in sorted(by_qual.items())},
                "by_file": by_file, "by_module": by_module,
                "by_external_callee": {k: v for k, v in sorted(by_ext.items())},
                "by_unresolved_callee": {k: v for k, v in sorted(by_unres.items())},
                "by_variable_name": {k: v for k, v in sorted(by_var.items())},
                "by_architecture_violation": by_viol,
                "hotspots": {"high_fan_in_functions": [{"function_id": r["function_id"], "qualified_name": r["qualified_name"],
                                                        "fan_in": r["graph_metrics"]["fan_in"]} for r in hi_in if r["graph_metrics"]["fan_in"]],
                             "high_fan_out_functions": [{"function_id": r["function_id"], "qualified_name": r["qualified_name"],
                                                         "fan_out": r["graph_metrics"]["fan_out"]} for r in hi_out if r["graph_metrics"]["fan_out"]],
                             "most_included_files": most_incl,
                             "modules_with_most_outgoing_dependencies": [{"module": m["name"], "outgoing_modules_count": m["module_metrics"]["outgoing_modules_count"]}
                                                                         for m in mods_out if m["module_metrics"]["outgoing_modules_count"]]}}

    def _canonical_graph(self, graph, snapshot_id, fn_records, call_rows):
        stats = graph.stats()
        functions = [{
            "function_id": r["function_id"], "name": r["name"], "qualified_name": r["qualified_name"],
            "signature": r["signature"], "language": r["language"]["display"], "file": r["file"],
            "location": r["location"], "kind": r["kind"], "parameters": r["parameters"],
            "return_type": r["return_type"], "docstring": r["docstring"], "tracked_vars": r["tracked_vars"],
            "variables_count": len(r["variables"]), "graph_metrics": r["graph_metrics"],
            "relationships": {"callers": [c["function_id"] for c in r["callers"]],
                              "callees": [c["function_id"] for c in r["callees"] if c["function_id"]],
                              "unresolved_callees": [c["callee_name"] for c in r["callees"]
                                                     if not c["function_id"] and c["confidence_category"] != "external"],
                              "external_callees": [c["callee_name"] for c in r["callees"]
                                                   if not c["function_id"] and c["confidence_category"] == "external"]},
            "module": r["module"], "architecture": r["architecture"]} for r in fn_records]
        calls = [{
            "call_id": c["call_id"], "caller_id": c["caller"]["function_id"], "callee_id": c["callee"]["function_id"],
            "callee_name": c["callee"]["raw_name"],
            "call_file": {"path_abs": c["source_location"]["call_file_abs"], "path_rel": c["source_location"]["call_file_rel"],
                          "path_display": c["source_location"]["call_file_rel"]},
            "call_line": c["source_location"]["call_line"], "call_args": c["call_args"],
            "is_resolved": c["resolution"]["is_resolved"], "resolution_confidence": c["resolution"]["resolution_confidence"],
            "confidence_category": c["resolution"]["confidence_category"], "resolution_reason": c["resolution"]["resolution_reason"],
            "resolution_hint": c["resolution"]["resolution_hint"], "underlying_count": c["aggregation"]["underlying_count"],
            "sample_call_sites": c["aggregation"]["sample_call_sites"],
            "module_edge": {"from_module": c["caller"]["module"], "to_module": c["callee"]["module"],
                            "is_cross_module": c["relationship_classification"]["cross_module"]},
            "file_edge": {"from_file": c["caller"]["file_rel"], "to_file": c["callee"]["file_rel"],
                          "is_cross_file": c["relationship_classification"]["cross_file"]}} for c in call_rows]
        return {"schema_version": SCHEMA_VERSION, "snapshot_id": snapshot_id, "entity_type": "canonical_graph",
                "project": self._project_block(graph),
                "stats": {"files_parsed": stats["files_parsed"], "functions": stats["functions"], "calls": stats["calls"],
                          "resolved_calls": stats["resolved_calls"],
                          "heuristic_calls": sum(1 for c in calls if c["confidence_category"] == "heuristic"),
                          "unresolved_calls": sum(1 for c in calls if not c["is_resolved"]),
                          "external_functions": stats["external_functions"], "parse_errors": stats["parse_errors"],
                          "modules": len(graph.modules) if graph.modules else 0,
                          "architecture_violations": len(graph.violations)},
                "functions": functions, "calls": calls,
                "files": [], "modules": {}, "architecture": {}, "include_graph": {},
                "build_context": {}, "matlab_project": {}, "parse_quality": {}}

    # -- manifest + small utils --------------------------------------------- #

    def _manifest(self, graph, m, integrity):
        stats = graph.stats()
        n_funcs = len([r for r in m["functions_jsonl"] if not r["kind"]["is_external"]])
        calls = m["calls_jsonl"]
        return {
            "schema_version": SCHEMA_VERSION, "package_kind": PACKAGE_KIND, "snapshot_id": m["snapshot_id"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generator": {"tool": _TOOL_NAME, "version": _tool_version(), "command": " ".join(sys.argv),
                          "python_version": _platform.python_version(), "platform": _platform.platform()},
            "project": {**self._project_block(graph), "source_selection": _config_section(self.config.selection)},
            "config_effective": m["config_effective"],
            "stats": {"files_parsed": stats["files_parsed"], "functions": n_funcs, "calls": len(calls),
                      "resolved_calls": sum(1 for c in calls if c["resolution"]["is_resolved"]),
                      "heuristic_calls": sum(1 for c in calls if c["resolution"]["confidence_category"] == "heuristic"),
                      "unresolved_calls": sum(1 for c in calls if not c["resolution"]["is_resolved"]),
                      "external_functions": stats["external_functions"], "parse_errors": stats["parse_errors"],
                      "modules": len(m["modules"]["modules"]), "architecture_rules": len(m["architecture"]["rules"]),
                      "architecture_violations": len(m["architecture"]["violations"]),
                      "include_files": len(m["include_graph"]["files"]), "include_edges": len(m["include_graph"]["edges"]),
                      "include_cycles": len(m["include_graph"]["cycles"]), "variables": len(m["variables_jsonl"])},
            "files": {"canonical_graph": "architecture.graph.json", "functions": "functions.jsonl",
                      "calls": "calls.jsonl", "files_index": "files.jsonl", "variables": "variables.jsonl",
                      "modules": "modules.json", "architecture": "architecture.json", "include_graph": "include_graph.json",
                      "build_context": "build_context.json", "matlab_project": "matlab_project.json",
                      "entry_points": "entry_points.json", "parse_quality": "parse_quality.json",
                      "indexes": "indexes.json", "obsidian_instructions": "obsidian_agent_instructions.md"},
            "integrity": {"graph_hash": integrity.get("graph_hash"), "functions_hash": integrity.get("functions_hash"),
                          "calls_hash": integrity.get("calls_hash"), "files_hash": integrity.get("files_hash"),
                          "variables_hash": integrity.get("variables_hash"), "config_hash": integrity.get("config_hash")},
            "determinism_note": "Records are emitted in sorted order; generate with --parallel 1 for byte-stable output."}

    def _project_block(self, graph):
        root = self.root
        bi = graph.build_info
        input_kind = "folder"
        if bi and bi.source:
            input_kind = {"compile_commands": "compile_commands", "sln": "sln"}.get(bi.source, bi.source)
        return {"name": (root.name if root else "project"), "root_abs": (str(root) if root else None),
                "input_path": (str(root) if root else None), "input_kind": input_kind,
                "languages_detected": sorted({fn.language.display_name() for fn in graph.functions.values() if not fn.is_external})}

    def _inferred_from(self, graph, file_rel):
        if graph.modules:
            for mod in graph.modules.values():
                for abs_f in mod.files:
                    if _posix_rel(abs_f, self.root) == file_rel:
                        return mod.inferred_from or "config"
        return "folder"

    def _module_inferred_from(self, graph, name):
        if graph.modules and name in graph.modules:
            return graph.modules[name].inferred_from or "config"
        return "folder"

    def _build_membership(self, graph, abs_path):
        bi = graph.build_info
        unit = bi.units.get(abs_path) if (bi and bi.units) else None
        return {"source": (bi.source if bi else "folder"), "projects": list(bi.projects) if bi else [],
                "in_compile_commands": unit is not None, "compile_unit_available": unit is not None,
                "defines_count": len(unit.defines) if unit else 0, "includes_count": len(unit.includes) if unit else 0,
                "extra_flags_count": len(unit.extra_flags) if unit else 0}

    def _violation_fn_ids(self, graph, id_map) -> dict:
        out = defaultdict(list)
        for i, v in enumerate(graph.violations):
            vid = f"viol:{i:03d}"
            for a, b in (v.sample_edges or []):
                for nid in (a, b):
                    fid = id_map.get(nid)
                    if fid:
                        out[fid].append(vid)
        return out

    def _stamp(self, rows, snapshot_id, entity_type):
        for r in rows:
            r["schema_version"] = SCHEMA_VERSION
            r["snapshot_id"] = snapshot_id
            r.setdefault("entity_type", entity_type)

    def _with_meta(self, doc, snapshot_id, entity_type):
        return {"schema_version": SCHEMA_VERSION, "snapshot_id": snapshot_id, "entity_type": entity_type, **doc}


# --------------------------------------------------------------------------- #
# tiny id helpers                                                              #
# --------------------------------------------------------------------------- #

def _name_of(fid: Optional[str]) -> Optional[str]:
    if not fid or "::" not in fid:
        return fid
    qual = fid.split("::")[1]
    return qual.split(".")[-1]


def _qual_of(fid: Optional[str]) -> Optional[str]:
    if not fid or "::" not in fid:
        return fid
    return fid.split("::")[1]


# --------------------------------------------------------------------------- #
# static text blocks                                                           #
# --------------------------------------------------------------------------- #

_KNOWN_LIMITATIONS = [
    "Static call graph cannot fully resolve all dynamic dispatch or runtime callbacks.",
    "Unresolved does not always mean error; it may mean external library call, macro limitation, "
    "dynamic dispatch, missing source, or parser limitation.",
    "Heuristic resolution must be treated as probable, not certain.",
    "HTML rendering options may be lossy; the knowledge pack should use canonical graph data.",
]
_INCLUDE_USAGE = {"safe_to_use_for": ["header dependency analysis", "cycle detection", "finding highly included headers"],
                  "limitations": ["include graph is not equivalent to runtime call graph",
                                  "system headers may be hidden from render view but still recorded"]}
_BUILD_INTERP = {"safe_facts": ["Files listed in compile_units have build metadata",
                                "Files outside compile_commands may still be parsed but lack full compiler context"],
                 "limitations": ["Missing compile flags may affect macro expansion and include resolution"]}
_MATLAB_INTERP = {"safe_facts": ["Packages and @class folders are MATLAB namespace/class structure hints"],
                  "limitations": ["MATLAB dynamic dispatch may remain unresolved"]}
_ENTRY_USAGE = {"safe_to_use_for": ["creating flow pages", "prioritizing architecture overview", "impact analysis"],
                "limitations": ["graph roots are not always true runtime entry points",
                                "callbacks may appear as roots even when called by framework/runtime"]}

_OBSIDIAN_INSTRUCTIONS = """# Obsidian agent instructions — convert this pack to a Markdown vault

You are an offline agent. This directory is a faithful, deterministic snapshot of a
project's static architecture (`callgraph-agent-knowledge-pack/v1`). Build an Obsidian
vault from it as a reliable, regeneratable knowledge source.

## Read order
1. `manifest.json` — counts, integrity, `snapshot_id`. Confirms the pack is complete.
2. `architecture.graph.json` — the canonical functions + calls (the whole graph).
3. `modules.json` — `module_edges` = the architecture dependency view.
4. `parse_quality.json` — REQUIRED before you assert anything. Honour `agent_rules`.
5. `functions.jsonl` / `calls.jsonl` / `files.jsonl` / `variables.jsonl` — detail, on demand.
6. `entry_points.json`, `indexes.json` — flows and lookups.

## Rules of evidence
- Every claim must trace to a field here. Do not invent calls or dependencies.
- A call with `confidence_category: "heuristic"` means *appears to call* — never certain.
- `unresolved` is not absence; it may be an external/library/dynamic/macro call.
- Use the per-record `agent_evidence` / `agent_interpretation_rules` to phrase claims safely.

## Vault layout (one note per entity; stable filenames)
- `Architecture.md` (MOC): project summary from `manifest`/`parse_quality`, a Mermaid graph
  from `modules.json.module_edges`, a linked module index, and callout blocks for
  `architecture.json.violations` and any cycles.
- `modules/<name>.md`, `files/<path-with-_>.md`, and `functions/<id>.md` for hotspots
  (`indexes.json.hotspots`) + entry points (`entry_points.json`).
- Use `[[wikilinks]]` for every dependency so Obsidian's graph view mirrors the call graph.

## Idempotent / updatable
- Note filenames derive from the stable ids in this pack; never rename across runs.
- Put generated content between `<!-- BEGIN callgraph:auto -->` and
  `<!-- END callgraph:auto -->`. Never touch text outside those markers (human notes).
- On regeneration, compare `snapshot_id`; rewrite only the managed region of entities whose
  records changed. Mark entities missing from the new pack with a `> [!warning] Removed`
  callout instead of deleting the note.
"""
