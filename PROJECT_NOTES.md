# CallGraph Tool — Project Notes

Last updated: 2026-05-24

> **Source of truth**: always treat the source code as authoritative over this document.
> This file is the single living doc; do not split guidance into multiple files.

---

## Purpose

CallGraph Tool is an offline static call-graph, variable-flow, and architecture analyser for
C, C++, Python, and MATLAB. It scans source code, extracts function definitions and call
relationships, resolves calls where possible, optionally aggregates them to higher abstraction
levels (file / folder / module / library / namespace), validates architecture rules, builds
an include graph, and renders a single self-contained HTML file with multiple interactive
viewing modes.

Supported inputs:
- Project folders
- Visual Studio `.sln` files (referenced `.vcxproj` projects)
- `compile_commands.json` (auto-detected or via `--compile-commands`)

Supported languages: Python (`ast`), C (Tree-sitter), C++ (Tree-sitter), MATLAB (regex)

Supported output formats: `html`, `dot`, `svg`, `png`, `agent` (LLM-readable JSON), `pack` (agent knowledge-pack directory)

---

## Repository Layout

```
callgraph_tool.py       — thin CLI entry point → callgraph.cli.main()
gui.py                  — Tkinter GUI (invokes CLI via subprocess; .sln inspect modal)
config.example.yaml     — full annotated config reference
requirements.txt        — runtime dependencies
CMD_COMMANDS.txt        — user-facing command reference
tests/                  — pytest tests + example source projects
output/                 — generated graph outputs (gitignored)
callgraph/
  cli.py                — argument parsing + pipeline orchestration
  config.py             — dataclass config, YAML/JSON loading
  discovery.py          — recursive file discovery, language grouping, selection filter
  sln_reader.py         — Visual Studio .sln / .vcxproj reader (now exposes configs/platforms)
  sln_inspect.py        — .sln/.vcxproj inspector for GUI (JSON over stdout)
  build_info.py         — compile_commands.json parser + BuildInfo cross-referencing
  aggregator.py         — Function → File/Folder/Module/Library/Namespace aggregation
  include_graph.py      — #include extraction + local/system classification + cycles
  architecture.py       — Module mapping + rule engine + violations
  models.py             — FunctionDef, CallRelationship, CallGraph, VariableDef, Parameter,
                          RenderLevel, BuildInfo, CompileUnit, ModuleDef, ArchitectureRule,
                          ArchitectureViolation, IncludeEdge, IncludeGraph
  parsers/
    base.py             — BaseParser (parallel `parse_files` via ThreadPoolExecutor)
    c_parser.py         — Tree-sitter C; shared helpers imported by cpp_parser
    cpp_parser.py       — Tree-sitter C++; imports all helpers from c_parser
    python_parser.py    — ast-based Python parser
    matlab_parser.py    — regex-based MATLAB parser
  graph/
    builder.py          — dedup, resolve, filter, depth-limit → CallGraph
                          (records resolution_reason + confidence_category on every edge)
    filters.py          — function/file filter helpers
  renderers/
    html_renderer.py    — ~5000-line self-contained HTML generator
                          (configurable view slots, include-graph mode, build-info &
                           architecture sidebar panels, confidence-styled edges)
    dot_renderer.py     — DOT/SVG/PNG output via Graphviz
    agent_renderer.py   — `agent` format: single <out>.agent.json (LLM-readable,
                          portable, both-direction adjacency; --agent-shards → <out>.agent/ dir)
    agent_pack.py       — `pack` format: <out>.knowledge/ dir (manifest + *.jsonl +
                          indexes + obsidian_agent_instructions.md) for offline agents
```

---

## Main Pipeline

```
cli.parse_args()
  → config.load_config()                     # YAML / JSON / defaults
  → build_info.load_compile_commands()       # auto-detect or --compile-commands
  → discovery.discover_files()               # folder scan
    OR sln_reader.discover_from_sln()        # .sln input (configurations + platforms aware)
  → apply selection filter (projects/folders/files/languages/modules)
  → build_info.cross_reference(files, cc)    # populate BuildInfo
  → parsers.get_parser().parse_files()       # parallel ThreadPoolExecutor
        emits: list[FunctionDef], list[CallRelationship]
  → graph.builder.build_call_graph()
        dedup → resolve (records resolution_reason + confidence_category)
              → filter → depth-limit → node-cap
  → architecture.build_modules() + validate() → ModuleDef + violations
  → if include_graph.enabled: include_graph.build()
  → aggregator.aggregate(level) for each render slot (function|script|folder|module|library|namespace)
  → renderers.render_graph()
      → html_renderer  (embeds slot-1 + slot-2 + var-flow + include + build_info + violations)
      OR dot_renderer  (uses --render-level for the abstraction level)
```

---

## Core Data Models (`callgraph/models.py`)

| Model | Key fields |
|---|---|
| `Language` | enum: Python, C, Cpp, MATLAB; display labels and node colors |
| `RenderLevel` | enum: function, script, folder, module, library, namespace, include |
| `FunctionDef` | `node_id` (stable key = file + qualified name + line), `variables`, `parameters` |
| `CallRelationship` | caller → callee edge; `resolution_confidence`, `confidence_category`, `resolution_reason`, `underlying_count`, `sample_call_sites` |
| `CallGraph` | `functions: dict[str, FunctionDef]`, `calls`, `total_files_parsed`, `parse_errors`, `build_info`, `include_graph`, `modules`, `violations`, `render_level` |
| `VariableDef` | (unchanged — see source) |
| `Parameter` | (unchanged — see source) |
| `CompileUnit` | per-source `compile_commands.json` record: command, includes, defines, flags |
| `BuildInfo` | aggregate build metadata: source ("folder"/"sln"/"compile_commands"/"compile_commands+sln"), units, configuration, platform, projects, mismatches |
| `ModuleDef` | name, set of source files, `inferred_from` ("folder"/"project"/"namespace"/"config"), project |
| `ArchitectureRule` | kind (forbidden/allowed_only/required/layer), from_module, to_module, reason |
| `ArchitectureViolation` | rule + offending module pair + sample edges |
| `IncludeEdge` | from_file → to_file (is_system, resolved, raw_target, line) |
| `IncludeGraph` | per-file edges, unresolved list, cycles, most-included stats |

Call resolution confidence: `EXACT` → `HEURISTIC` → `UNRESOLVED` (string enum).
Confidence categories used in the UI: `exact` | `heuristic` | `unresolved` | `external` | `aggregated` | `violation`.

---

## Compile Commands & Build Info (`callgraph/build_info.py`)

Auto-detection order: `<project>/compile_commands.json`, `<project>/build/compile_commands.json`,
`<project>/out/compile_commands.json`. `--compile-commands PATH` overrides.

For each unit the parser extracts:
- absolute source file path (normalized)
- working directory
- argv (split using shlex if `command` is a string)
- include directories (`-I`, `/I`, `-isystem`)
- defines (`-D`, `/D` → `{NAME: value-or-None}`)
- extra flags (everything else, kept as-is)

When both `.sln` and `compile_commands.json` are present, **compile_commands wins** for per-file
includes/defines; files in the `.sln` that are absent from CC fall back to `.vcxproj`
`<AdditionalIncludeDirectories>` / `<PreprocessorDefinitions>` (best-effort).

BuildInfo is attached to the CallGraph and rendered in the HTML "Build Info" sidebar panel.
The metadata is *not yet* used to drive preprocessing — it is stored for display and future use.

---

## Aggregator (`callgraph/aggregator.py`)

Single entry point: `aggregate(graph, level, modules=None) -> CallGraph`.

- `function`  → returns input unchanged
- `script`    → one synthetic node per file (delegates to `builder.collapse_to_files`)
- `folder`    → groups by first N folders under project root (default 2, configurable)
- `library`   → groups by `.vcxproj` project name (from `BuildInfo.projects`)
- `module`    → groups by `ModuleDef` membership (config or auto-inferred)
- `namespace` → groups by `FunctionDef.parent` (namespace or class)

Aggregated edges set `confidence_category = "aggregated"`, store
`underlying_count`, and keep a few `sample_call_sites` for the detail panel.
Violations override the category: an aggregated edge that crosses a forbidden module pair
becomes `"violation"` so the renderer paints it red.

---

## Include Graph (`callgraph/include_graph.py`)

Regex extractor — no preprocessor. For every C/C++/header file we scan for
`^\s*#\s*include\s*(["<])([^">]+)[">]`.

Local includes are resolved in order:
1. same directory as the including file
2. project-wide basename index
3. include paths from `BuildInfo.units[file].includes`
4. global `<AdditionalIncludeDirectories>` collected from `.vcxproj`

Outputs: per-file edge list, unresolved list, cycles (iterative DFS), most-included files
(top N by in-degree). System includes (`<...>`) are tagged `is_system=True`; they are hidden
in the UI by default and toggled via the sidebar "Show system includes" checkbox.

---

## Architecture & Rules (`callgraph/architecture.py`)

`build_modules(files, mapping_cfg, build_info) -> dict[name, ModuleDef]`:
- User-supplied globs win (first match per file).
- Unmatched files fall back to:
  - `.vcxproj` project name (when a `.sln` is loaded), else
  - first folder under `src/` if it exists, else top-level folder.

`validate(graph, rules) -> list[ArchitectureViolation]`:
- Iterates resolved edges, looks up `(from_module, to_module)` and evaluates each rule.
- Rule kinds: `forbidden`, `allowed_only`, `required`, `layer`.
  - `required`  — module A must have at least one edge to module B.
  - `layer`     — declare an ordered list of layers; any upward edge is a violation.
- Wildcards `*` allowed in `from` / `to`.
- Violations are attached to the affected edges (`confidence_category = "violation"`) so the
  renderer can paint them red across **all** views (Function, Script, Folder, Module).

Reports:
- `--architecture-report PATH` writes a structured JSON file.
- The HTML always exposes an "Architecture" sidebar section + Violations modal.

---

## Render Slots — Slot ≠ Renderer

The HTML used to hardcode **Function Mode + Script Mode**. They are now two configurable slots
backed by a JS render-mode registry. The slot is just a placement in the sidebar; the **renderer**
is picked by the slot's `selectedMode`.

- `--view-slot-1 LEVEL` and `--view-slot-2 LEVEL` (or YAML `render.view_slot_1` / `view_slot_2`)
- `LEVEL ∈ {function, script, folder, module, library, namespace}`
- Defaults: slot 1 = `function`, slot 2 = `script` (today's exact behavior).
- HTML mode buttons re-label themselves to the level name (e.g. "Folder Nodes" / "Module Nodes").
- `Var Flow` and `Include Graph` are separate fixed buttons (always available; Include Graph is
  hidden unless `--include-graph` / `include_graph.enabled`).

For DOT/SVG/PNG the single `--render-level` flag picks the aggregation level (one image per run).

### JS render-mode registry

```js
LEVEL_TO_MODE = {
  function:  'fn',      // vis.js network
  script:    'script',  // file-card view
  folder:    'module',  // shared hierarchy-card renderer (Folder → File → Function)
  module:    'module',  // NEW dedicated card-hierarchy renderer
  library:   'module',  // shared (label = "Library")
  namespace: 'module',  // shared (label = "Namespace")
};
RENDER_MODES = { fn, script, module, varflow, inc };
SLOTS = { slot1: {level, payload}, slot2: {level, payload} };
function activateSlot(slotId) { setViewMode(LEVEL_TO_MODE[SLOTS[slotId].level]); }
```

The CLI always passes the **function-level graph as the primary** to the renderer so vis.js has
the full call graph regardless of either slot's level. Per-slot aggregated graphs flow through
`slot1_graph` / `slot2_graph` kwargs into the JS as `CGX.slots.slot1` / `CGX.slots.slot2`.

### Module View (new)

`callgraph/renderers/html_renderer.py` `_CGX_EXTRAS_JS` — class names start with `cgx-mv-`.

- Cards collapsed by default; header shows `N file(s) · N fn(s) · in:N · out:N`.
- Click header → expand to file rows. Click a file row → expand to function rows.
- Click a function row → opens the existing function-detail modal (`cgOpenModalById`).
- Sidebar controls (active only in Module View): **Expand All**, **Expand One Level**,
  **Collapse One Level**, **Collapse All**, `Hide intra-module edges` toggle,
  `Top-N per card` numeric input. Top-N limits visible children only; it does
  not change the current expansion depth.
- Drag the header to reposition; positions persist to `localStorage[cgx_mv_pos_<graphid>]`.
- Wheel zoom and background pan use the same Script-Mode-style canvas transform model.
- Script-style smooth SVG arrows between card/row edges; recomputed on every expand/collapse/drag.
  The shared Straight control maps to direct line edges in this card renderer, matching Script View.
- Arrows use function-level calls as the source of truth and anchor to the lowest visible level
  (function row → file row → card as parents collapse).
- Existing View controls route into this renderer for fit, highlight/center, layout save/reset,
  edge filters, and the shared Straight toggle.
- Edge style derived from `confidence_category` (same colours as the vis.js network).
- Hierarchy data comes from each aggregated node's `tracked_vars.__hierarchy__` — JSON blob
  produced by `aggregator._build_aggregated_graph`: `[{file, fns: [[fn_id, label], ...]}, ...]`.

### Edge filter — "Edge Type" (renamed from "Confidence")

- Six checkboxes (Exact / Heuristic / Unresolved / External / Aggregated / Violation), each
  with the matching colour swatch.
- State persisted to `localStorage[cg_edge_filter_<graphid>]`.
- Applied **per mode** via `RENDER_MODES[currentMode].edgeFilter(state)`:
  - `fn` → updates vis.js edges (`hidden` flag).
  - `script` → toggles `display:none` on SVG arrows tagged with `data-edge-id`.
  - `module` → re-renders the Module View arrow SVG with filtered edges.
  - `inc` → no-op (Include Graph has its own toggles).

### Include Graph exit

- Header inside `#cgx-inc-view` now has a visible `× Close` button → re-activates slot 1.
- Sidebar slot/varflow buttons remain clickable and switch directly too.

### Keyboard shortcuts (project-wide)

- `1` / `2` → activate Slot 1 / Slot 2.
- `V` → Var Flow.   `I` → Include Graph (when enabled).
- `N` → capture current view to Nodebook.   `B` → open the Nodebook gallery.
- `/` → focus sidebar search.   `Esc` → close any open modal.
- Skipped while an `<input>` / `<textarea>` is focused.
- A `?` hint badge bottom-right lists them on hover.

---

## SLN Inspect & GUI Picker (`callgraph/sln_inspect.py`, `gui.py`)

`sln_inspect` reads a `.sln`, walks every `.vcxproj`, and emits a JSON tree:

```json
{
  "configurations": ["Debug", "Release"],
  "platforms": ["x64", "Win32"],
  "active_configuration": "Debug",
  "active_platform": "x64",
  "projects": [{
    "name": "MyApp",
    "path": "...",
    "include_paths": ["..."],
    "defines": ["..."],
    "folders": [{"path": "src", "files": ["main.cpp", "util.cpp"]}, ...]
  }, ...]
}
```

The GUI auto-opens the inspect modal when a `.sln` is loaded. The modal shows:
- Dropdowns for Configuration / Platform (default `Debug|x64` if present)
- A `ttk.Treeview` (projects expanded, folders collapsed) with checkboxes at every node
- OK builds the selection sidecar `.callgraph.selection.json` next to the `.sln` and passes
  `--include-projects` / file glob filters into the CLI invocation
- Cancel = analyze whole solution (today's behavior)

Configuration / Platform choice affects only which `<...|Debug|x64...>` PropertyGroup is read
for include paths and defines — the file list is the union across configs.

---

## Confidence UI

Edges are styled by `confidence_category`:

| Category | Style |
|---|---|
| exact | solid blue |
| heuristic | dashed orange |
| unresolved / external | grey dashed |
| aggregated | thicker blue (tooltip shows underlying count) |
| violation | bold red (wins over all others) |

Hover tooltip and the detail panel both show the `resolution_reason` string
(e.g. "exact qualified match", "same-file fallback", "aggregated 12 underlying calls").

Sidebar checkboxes (under "Confidence") hide heuristic / unresolved / external edges on demand.
All categories are visible by default — no regression vs today.

---

## Variable Flow Mode

See `_build_var_flow_data` and the existing rules around `.Connect()` / lugasi /
memset / memcpy. Var Flow has its own dedicated button independent of the render slots.

### Branch highlight (VF-2)

Clicking a Variable Flow block lights up its **downstream flow**, colouring each path
distinctly (`_vfApplyBranchHighlight` in `html_renderer.py`):

- Each immediate outgoing branch from the clicked node gets a distinct base **hue**
  (evenly spaced around the colour wheel).
- When a branch **splits again** downstream, child hues are *derived* from the parent
  hue (fanned out per sibling, shaded darker by depth) so a colour's lineage stays
  readable instead of blending into mud.
- Nodes reached by **more than one branch** are flagged as merge points (`vf-merge`,
  dashed border).
- Nodes not downstream of the click are dimmed (`vf-dim`); edges are coloured by the
  branch of the node they flow into (per-colour arrow markers) and dimmed otherwise.
- A legend overlay (`#cg-vf-legend`) lists the branches with swatches + a Clear button.
- Clicking the origin again, "Clear", selecting another variable, or rebuilding the
  graph clears the highlight. State: `_vfBranchActive` / `_vfBranchNodeColor` /
  `_vfBranchMerge` / `_vfBranchOriginId`.

### Cross-mode flow trace (shared engine)

The VF-2/VF-6 colouring algorithm above is now a **mode-agnostic** helper
`window.cgFlowTraceColors(origin, edges, direction)` (top of `callgraph-sidebar-js`),
returning `{color, hue, merge, neighbors, nBranch, branchColor}`. `_vfApplyBranchHighlight`
calls it (behaviour-identical), and **every** other mode wires a thin apply/clear adapter onto
its own DOM/canvas using the same output:

- Function (vis.js): `_fnApplyTrace` / `_fnClearTrace`. This vis build has **no per-node
  `opacity`** option, so dimming uses supported channels only — muted `color`/`font` for
  non-traced nodes, branch-hued `color.border` + `shadow` for traced nodes, and the **vis edges
  along each path are recoloured per branch** (`width:3`) while the rest are dimmed via edge
  `color.opacity`. Edge colour sets both `color.color` **and** `color.highlight` (plus
  `inherit:false`) — required because vis renders edges connected to the *selected* origin node
  using their `color.highlight`, so without it those edges stayed default grey. Both node and
  edge styling are snapshotted/restored exactly. Click keeps
  opening the summary (`selectNode → openDetail`); re-clicking the origin clears the trace.
  The `click` handler tests `p.nodes` **before** `p.edges` so a node click traces only and does
  **not** raise the "X calls Y" edge-details popup (vis puts the node's edges in `p.edges` too).
- Script: `_svApplyTrace` / `_svClearTrace` (`.cg-fn-row` borders + `.cg-sv-edge` strokes).
- Module (folder/library/namespace): `_mvApplyTrace` / `_mvClearTrace` (`.cgx-mv-card`, `_mvAggEdges`).
- Include: `_ivApplyTrace` / `_ivClearTrace` (`.cg-iv-node` tiles **and** the SVG `path.iv-arrow`
  edges). Arrows carry `data-from`/`data-to`; traced arrows are recoloured to the branch hue
  (`stroke-width:3` + a per-colour arrowhead `<marker>` minted on demand via
  `_ivEnsureArrowMarker`), the rest dimmed to `opacity:0.08`. Clear restores defaults by simply
  re-running `_ivDrawArrows()`. Uses `_ivEdges` for the trace topology.

Each mode has a `☑ trace` enable checkbox (built by `cgBuildTraceControl`) and a
`⇟ Downstream / ⇞ Upstream` button; both persist independently per mode in `localStorage`
(`cg-trace-enabled-<mode>` / `cg-trace-dir-<mode>`, default ON + downstream). Click handlers run
the trace only when the mode's flag is set, so unchecking lets you drag nodes freely. `Esc` and
the sidebar Clear-focus/Show-all buttons call `_cgClearAllTraces`. See the vault note
*Features/Flow Trace Highlight*.

### Interprocedural flow & direction (VFI-2 / VFI-3 / VFI-7 / VFI-9)

`_vfBuildFlowChain(normKey, seedScopeId, direction)` (JS) and the browser-free canonical
`callgraph/analysis/var_flow_interproc.py:build_interprocedural_flow(graph, root_var, …,
seed_scope_id, direction)` perform the same BFS and are kept in sync (the Python pass is
unit-tested so CI pins the JS behaviour):

- **VFI-2 forward** (`direction="forward"`, default): when a tracked variable is passed as
  a call argument, flow continues into the callee's matching positional **parameter**,
  even when renamed at every hop. Args are matched on both the base name and the full
  member path (`cfg.speed`).
- **VFI-3 cross-variable assignment edges**: after the BFS, same-function `dst = src;` /
  `dst = fn(src)` links are emitted as dashed-orange `assign` edges (parser sets
  `VariableDef.assign_src`; renderer pre-indexes `ASSIGN_DST_INDEX`).
- **VFI-7 backward** (`direction="backward"`): the inverse of VFI-2 — from a selected sink
  **parameter**, walk each caller's positional argument expression back to the originating
  variable. Edge orientation stays source→sink; only discovery direction flips. UI: the
  **⮜ Backward / ⮞ Forward** toolbar toggle (`#cg-vf-backward-btn`, `_vfBackwardFlow`,
  localStorage `cg-vf-backward-flow`).
- **VFI-9 member-level identity**: `_build_var_flow_data` keys a plain member read
  (`source_kind == "member_access"` with a `parent_name`) by its full `parent.member` path
  — *not* the bare leaf field — and stamps `scope_id = "m:<full.path>"`. This keeps
  `cfg.speed`, `cfg.rpm` and `engine.speed` as distinct end-to-end identities, aligns reads
  with the full-path custom-input/connect destinations, and (because the BFS already matches
  args on the full member path) needs **no BFS change** — flow is purely data-driven from the
  bucket key. The dropdown's substring filter still matches the leaf, so typing `speed`
  surfaces `cfg.speed`.

---

## Parser Notes

### c_parser.py / cpp_parser.py

Tree-sitter-based parsers. The C parser owns the shared helpers
(`_extract_variables_from_declaration`, `_extract_assignment_variable`, `_classify_value_node`,
`_extract_preproc_constant`, `_detect_dead_variables`, `_extract_connect_call`,
`_extract_connect_free_func_call`, `_extract_custom_input_call`, `_extract_memory_op`).
The C++ parser imports all helpers from `c_parser` and mirrors them in `_walk`.

Per-file `CompileUnit` metadata is attached via thread-local storage so parsers can read it
without changing their public `parse_file` signature. The current parsers only **carry** the
metadata (so it shows up in the HTML Build Info panel and edge detail); they do **not** yet
use defines / includes to drive preprocessing.

Critical fix in `cpp_parser._extract_qualified_name`: must handle `field_identifier` nodes so
inline class methods (and the LUGASI / `.Connect` calls inside them) are not silently dropped.

### Custom input families — `lugasi` / `lugasian`

Module-level constant in `c_parser.py`:

```python
CUSTOM_INPUT_FUNC_NAMES = [
    "lugasi", "lugasian",        # C++ method form
    "lugasi2", "lugasian2",      # C-style free function form
    "LUGASI", "LUGASIAN",        # uppercase macro/legacy forms
    "LUGASI2", "LUGASIAN2",
]
CUSTOM_INPUT_ARG_DEST     = 0
CUSTOM_INPUT_ARG_SOURCE   = 1
CUSTOM_INPUT_ARG_CLASSIFY = 2
```

Call pattern: `lugasi(&X, "Y", wow_na)` — `Y → X`, classifier = `wow_na`.
`source_kind = "custom_input"`, `sort_priority = 0` (highest; renders before `.Connect`).

### `.Connect()` — method-call form

```python
CONNECT_METHOD_NAMES: list[str] = ["connect", "Connect"]
```

`source_kind = "input_file_connect"`, `sort_priority = 1`. The receiver is the dest variable
and the first arg supplies `connect_path` + `connect_input_name`.

### `connect2()` — free-function form

```python
CONNECT_FREE_FUNC_NAMES = ["connect2", "CONNECT2"]
CONNECT_FREE_ARG_DEST = 0
CONNECT_FREE_ARG_PATH = 1
```

Badge renders as `connect2`.

### Ordering rule (same variable, same function)

`sort_priority`: `0` custom_input → `1` input_file_connect → `2` everything else.
Renders LUGASI before `.Connect` and wires an intra-function chain edge between them.

---

## HTML Renderer (`callgraph/renderers/html_renderer.py`)

Single self-contained file (~5K lines).

| Function | Purpose |
|---|---|
| `_build_var_flow_data(graph)` | aggregates all `VariableDef` occurrences into `VAR_FLOW_DATA` JSON (each occurrence carries a `scope_id` for VFI-1 "split by scope" when its scope is broader than the enclosing function; VFI-9 keys member reads by their full `parent.member` path with `scope_id="m:<path>"`) |
| `_compute_layout(graph)` | **Smart Top-Down layered layout** (Sugiyama-style) — weakly-connected components laid out independently; cycles collapsed via SCC condensation; real architectural roots selected (not every no-caller node, cap 30/component); layers ranked by longest-path FROM roots with non-root sources sunk (ALAP) to where they plug into the tree (avoids a huge fake-root top layer); crossings reduced with barycenter/median sweeps (auto-scaled for huge graphs); x blends connected-neighbour pos (0.70) + same-file affinity (0.25, SOFT) + source order (0.05) with min-gap; components importance-ranked and shelf-packed in 2D. Deterministic. Default Function-mode engine; vis hierarchical is opt-in via "Layered". Optional diagnostics via `CG_LAYOUT_DEBUG=1`. |
| `_build_slot_payload(graph, level)` | NEW — emits NODE_DATA + EDGE_DATA for one render slot |
| `_build_include_payload(graph)` | NEW — emits include-graph data when `include_graph.enabled` |
| `_build_build_info_block(graph)` | NEW — text rendered into the Build Info sidebar panel |
| `_build_architecture_block(graph)` | NEW — modules + violations rendered into Architecture panel |
| `HtmlRenderer.render(graph, output_path)` | assembles final HTML |
| `_deflate_b64(s)` / `_DECOMP_JS` | PERF-5 — raw-DEFLATE+base64 a payload; `_DECOMP_JS` is the self-contained in-browser inflate bootstrap (`#cg-decomp-js`, exposes `__cgJ`) |

**PERF-5 payload compression:** `_inject_sidebar` builds an `_emit(obj)` closure that
serialises each big payload (nodes/edges/positions/var-flow/extras) and, per
`output.compress_payload` (`auto`/`true`/`false`, default `auto` = compress ≥ 32 KB =
`_COMPRESS_THRESHOLD`), emits either a plain JSON literal or `__cgJ("<base64>")`. The
~6 KB inflate bootstrap is embedded only when something was compressed. Output stays
100% self-contained. Demo: 1.55 MB → 1.23 MB; large graphs shrink ~5–10×.

**PERF-8 viewport virtualisation (custom-DOM modes):** Script View, Variable Flow,
Include graph and Module view each build one absolutely-positioned DOM element per
node inside a CSS-transform-panned canvas. For big solution files that DOM is the
real cost of *opening/panning* (not the JSON — PERF-5 handles bytes). PERF-8 pins
each node card with `content-visibility:auto` + a real-measured `contain-intrinsic-size`
via the shared `window._cgVirtualize(root, selector)` helper (declared in `_SIDEBAR_JS`,
also reachable from the `_CGX_EXTRAS_JS` IIFE for include/module). The browser then
skips layout/paint of off-screen cards while keeping every node in the DOM, so edges,
marquee, fit, drag, collapse and annotations are unaffected. Gated by
`output.virtualize_dom` (`auto`/`true`/`false`, default `auto` = on at ≥ 400 nodes =
`_VIRT_DOM_THRESHOLD`); below that, output is behaviour-identical (Rule 9). Only Script
View reads inner-row offsets, so those are cached at build time (`window._svRowGeom`)
and used as an off-screen fallback in `_svGetRowAnchor`/`_svScrollTo`, with
collapse/expand refreshing the cache + the card's intrinsic size (and module-card
toggles call `window._cgVirtRefresh`). Function Nodes mode already virtualises natively
via the vis.js canvas. Rule-13 artifact: `output/perf8_viewport_virtualization.html`.

Injected HTML structure (additive — existing IDs untouched):

```
<body>
  #cg-sidebar
    .cg-section View Mode
    .cg-section Search & Focus
    .cg-section Confidence  (NEW: heuristic / unresolved / external checkboxes)
    .cg-section Build Info  (NEW: collapsed by default)
    .cg-section Architecture (NEW: violation count + open modal button)
    .cg-section Stats / Dead Var / Legend
  #cg-script-view
  #cg-varflow-view
  #cg-inc-view              (NEW: include-graph canvas)
  #cg-vf-modal
  #cg-dead-modal
  #cg-arch-modal            (NEW: violations table)
  #cg-detail
  #cg-modal
</body>
```

JavaScript state — `currentMode` now ranges over `'fn' | 'script' | 'varflow' | 'inc'` plus
two slot bindings (`_slot1Level`, `_slot2Level`); the existing edge-index / VarFlow / DOM
helpers remain unchanged.

**Nodebook (saveable, restorable view favorites):** an additive `'nodebook'` pseudo-mode.
Constants `_NODEBOOK_CSS` / `_NODEBOOK_HTML` / `_NODEBOOK_JS` inject a `#cg-nodebook`
gallery panel, a `➕ Capture` button + `📓 Nodebook` tab (in the mode-button row), and the
engine. Each mode registers a capture/restore adapter on `window.CG_NB_ADAPTERS[mode]`
(`{label, title(st), capture(), restore(st)}`): `fn`/`script`/`varflow` adapters live in
`_SIDEBAR_JS`; `inc`/`module` adapters wrap new `window.cgxIncGetState/SetState` +
`cgxModuleGetState/SetState` helpers in `_CGX_EXTRAS_JS`. Capturing snapshots the *live
state* of the last real mode (`window.CG_NB_LAST_MODE`) — mode + filter/search + node
layout + pan/zoom + selected var — auto-titles it, and rasterises a thumbnail **fully
offline**: native `canvas.toDataURL()` for Function Nodes, and an SVG `<foreignObject>`
DOM snapshot (all page `<style>` inlined) for the other modes — *no html2canvas / no CDN*
(deviation from the original plan to stay self-contained per Rule 8). Pages persist to
`localStorage` (`<graphId>:cg_nodebook_v1`, exposed via `window.cgGraphId`) with a
quota guard that drops thumbnails on `QuotaExceededError`, plus Export/Import of a portable
`.nodebook.json`. Every `➕ Capture` always pushes a *new* page (unique id) — captures never
override each other; only the per-card *Update* button rewrites a specific page. Folder and
Module are both internal mode `module` but live in different slots, so each page records its
`builtFor` (`<slotId>:<level>`) + `level` and restore routes through
`window.cgxModuleActivateBuilt(builtFor)` to re-activate the original slot (keeping Folder vs
Module captures independent). The Capture + `📓 Nodebook` controls sit on their own second row
(`.cg-nb-btn-row`) inside the View Mode section. Gallery cards support inline rename,
drag-reorder, open (→ `setViewMode`/`cgxModuleActivateBuilt` + `adapter.restore`), update
(re-capture), and delete. Empty/inert by default (Rule 9). Tests: `tests/test_nodebook.py`
(+ functional `output/nodebook_per_mode_capture_test.py`). Rule-13 artifact:
`output/nodebook_feature.html`.

---

## Dead Variable Analysis

**Redesigned (VF-3)** — a shared read/write + lexical-scope **liveness engine** lives in
`callgraph/parsers/_liveness.py` and is wired into all parsers:

- **C/C++** (`detect_dead_variables_c`, called via `c_parser._detect_dead_variables`, reused
  by `cpp_parser`): tree-sitter occurrence-role classifier (DECL/READ/WRITE/ADDR via parent-node
  type — `assignment_expression`, `update_expression`, `pointer_expression` `&`/`*`,
  `field_expression`, `subscript_expression`) + a case-sensitive lexical-scope binder
  (`compound_statement`/`for`/`if`/`while` scopes). Fixes the old `Foo`/`foo()` case-collision
  and `obj.count`/`count` field-collision bugs. Pointer alloc/deref reasoning gives `dead_alloc`.
- **Python** (`detect_dead_variables_python`): exact `ast` `Load`/`Store`/`Del`; closure
  free-var capture and `locals()`/`eval()`/`exec()` keep names alive; `global`/`nonlocal` excluded.
- **MATLAB** (`detect_dead_variables_matlab`): regex per-function line-scan, **always low
  confidence**; `~` discards, no-`;` display = use, `global`/`persistent`/output vars escape,
  `eval`/`feval`/`assignin` keep the function alive.

Shared `classify_verdict()` maps evidence to categories **unused / dead_store / unused_param /
dead_alloc / unused_value** with `high`/`medium`/`low` confidence. Suppression for `(void)x`,
`volatile`, `_`-prefix / `pad`/`dummy`/`reserved` names. New `VariableDef`/`Parameter` fields:
`dead_category`, `dead_confidence`, `read_lines`, `write_lines`, `is_suppressed`,
`suppress_reason` (legacy `is_dead`/`dead_reason` still derived). Renderer emits these in
`_build_var_flow_data`; per-category block badges (`_vfDeadBadge`) + grouped, confidence-rated
dead report modal (`cgOpenDeadReport`). Named artifacts: `output/deadvar_categories.html`,
`deadvar_pointer_cases.html`, `deadvar_suppression.html`, `deadvar_python.html`,
`deadvar_matlab.html`. Tests in `tests/test_dead_vars.py` (`TestLivenessEngine{C,Python,Matlab}`).

---

## Scaling Notes

| Knob | Use |
|---|---|
| `--max-nodes N` | raise node cap (default 3000). |
| `--parallel N` | override parser worker count (auto = `min(32, cpu+4)`). |
| `--summary-by-file` | quick alias for `--view-slot-1 script --view-slot-2 script`. |
| `--view-slot-1 module --view-slot-2 folder` | architectural views for very large solutions. |
| `--include-projects A B` | only parse those `.vcxproj` projects from the `.sln`. |
| `--entry FUNC --depth N` | original slicing flag; still the most surgical option. |

Function Nodes view shows a banner above 8000 nodes (vis.js limit) and auto-switches to
Script Nodes view.

---

## Known Limitations

- Static analysis only — reflection is not resolved. **Function pointers** are resolved for
  same-file `fp = target` assignments (idea C-2). **C++ virtual / override dispatch** is resolved
  by fanning each virtual call out to every override in the class hierarchy (idea CPP-1, default on);
  cross-file pointer registration and non-C++ dynamic dispatch remain unresolved.
- Simple `#define` macros (object-like + function-like) **are** expanded before C/C++ parsing
  so macro-wrapped calls resolve to the real callee (`parser.expand_macros`, default on; idea C-1).
  Token-paste/stringize (`#`/`##`), variadic, and self-referential macros are still left as-is.
- C/C++ macro-generated function *definitions* (e.g. `DECLARE_HANDLER(foo, int)`) are not detected.
- `.Connect()` detection requires a string literal first argument (no variable path).
- `memset` / `memcpy`: only simple identifier targets (not `buf[i]` or `ptr->field`).
- Python method calls record only the attribute name (heuristic resolution).
- MATLAB `foo(x)` is ambiguous (call vs array index).
- Dead-variable detection is **intra-procedural** (reads/writes aggregated per name within a
  function) and **MATLAB is best-effort/low-confidence**; no cross-file / file-scope dead-symbol
  analysis yet (idea VF-13). Intermediate dead stores (`x=1; x=2; use(x)`) are not flagged.
- `compile_commands.json` metadata is currently **informational** — defines/include paths are
  shown in the UI and used by the include-graph resolver but do not yet drive the parser.
- `.vcxproj` PropertyGroup parsing is best-effort across schema versions.
- Include-graph cycle detection is iterative DFS; results are deterministic but may surface
  multiple overlapping cycles when the SCC contains more than one back-edge.
- Architecture rule engine: wildcards `*` are matched at full-module-name granularity (no
  glob syntax inside the wildcard).
- HTML output is large (single file). Make small, careful edits in `html_renderer.py`.

---

## Running the Tool

### GUI

```powershell
.\.venv\Scripts\python.exe gui.py
```

Tkinter window exposing all CLI parameters, plus the auto-opening .sln Inspect modal.

### CLI

```powershell
# Basic
.\.venv\Scripts\python.exe callgraph_tool.py --project tests\example_project --output output\graph --formats html

# compile_commands.json
.\.venv\Scripts\python.exe callgraph_tool.py --project src --compile-commands build\compile_commands.json --output output\graph --formats html

# Configurable render slots
.\.venv\Scripts\python.exe callgraph_tool.py --project src --view-slot-1 module --view-slot-2 folder --output output\arch --formats html

# Include graph mode
.\.venv\Scripts\python.exe callgraph_tool.py --project src --include-graph --output output\inc --formats html

# Architecture rules + report
.\.venv\Scripts\python.exe callgraph_tool.py --project src --config arch.yaml --architecture-report output\violations.json --output output\arch --formats html

# Visual Studio solution + scope
.\.venv\Scripts\python.exe callgraph_tool.py --project C:\path\to\Project.sln --include-projects MyApp MyLib --output output\sln --formats html

# .sln inspect (machine-readable; used by the GUI)
.\.venv\Scripts\python.exe callgraph_tool.py --project C:\path\to\Project.sln --inspect-sln
```

### Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -v
```

Key test files:
- `tests/test_build_info.py` — compile_commands parsing
- `tests/test_aggregator.py` — render-level aggregation
- `tests/test_include_graph.py` — include extraction + cycle detection
- `tests/test_architecture.py` — module mapping + rule engine
- `tests/test_sln_inspect.py` — .sln/.vcxproj JSON inspection
- `tests/test_dead_vars.py` — dead vars, connect pattern, memset/memcpy, VFD integration
- `tests/test_c_parser.py` / `test_cpp_parser.py` — Tree-sitter parsing
- `tests/test_builder.py` — graph assembly, resolution_reason, filters, node cap
- `tests/test_python_parser.py`, `test_matlab_parser.py`, `test_config.py` — language + config

---

## Development Rules (Safe Future Changes)

1. **Never break backward compatibility.** All new flags / config fields have defaults.
   The default render is `slot1=function, slot2=script` — exactly today's UI.
2. **Surgical edits only in `html_renderer.py`.** Use `Edit`, not `Write`. New sections are
   appended; existing IDs (`#cg-script-view`, `#cg-varflow-view`, etc.) are never renamed.
3. **Model extensions are additive.** Existing dataclasses get new optional fields with
   defaults — never reorder or remove fields. `FunctionDef.node_id` is load-bearing.
4. **`collapse_to_files` stays** as a thin wrapper over `aggregator.aggregate(level="script")`
   so any external caller keeps working.
5. **Aggregator emits synthetic nodes** with `qualified_name = f"<{level}>::{key}"` to avoid
   colliding with real `FunctionDef.node_id`s.
6. **Rule engine is generic** — no hardcoded module names. All examples in
   `config.example.yaml` are illustrative comments.
7. **GUI inspect is opt-in.** Cancel = analyze everything (today's behavior).
   Selection lives in `.callgraph.selection.json` next to the `.sln`.

---

## First Places To Look

| Symptom | File |
|---|---|
| CLI flag / new argument | `callgraph/cli.py` |
| New config field | `callgraph/config.py`, `config.example.yaml`, `tests/test_config.py` |
| compile_commands not loaded | `callgraph/build_info.py` |
| Include graph wrong | `callgraph/include_graph.py` (extraction) + `html_renderer._build_include_payload` |
| Wrong module assignment | `callgraph/architecture.build_modules` |
| Rule not firing | `callgraph/architecture.validate` |
| Wrong aggregation | `callgraph/aggregator.aggregate` |
| .sln inspect JSON wrong | `callgraph/sln_inspect.py` |
| File discovery issue | `callgraph/discovery.py`, `callgraph/sln_reader.py` |
| Render slot button label wrong | `html_renderer.py` `_SIDEBAR_HTML` template + `_VIEW_SLOT_LABELS` |
| Edge styling wrong | `html_renderer.py` JS `_edgeStyle(cat)` helper |
| Build Info panel empty | `html_renderer._build_build_info_block` + `graph.build_info` |
| Architecture panel / red edges | `html_renderer._build_architecture_block` + `graph.violations` |
| Language extraction bug | `callgraph/parsers/<lang>_parser.py` |
| Slow parsing on huge projects | `callgraph/parsers/base.py` ThreadPoolExecutor |
| LUGASI / `.Connect` not detected in C++ class methods | `callgraph/parsers/cpp_parser.py` → `_extract_qualified_name` |
| `.Connect` not detected | `c_parser.py` → `CONNECT_METHOD_NAMES` |
| lugasi / lugasian not detected | `c_parser.py` → `CUSTOM_INPUT_FUNC_NAMES` |
| `connect2` not detected | `c_parser.py` → `_extract_connect_free_func_call`, `CONNECT_FREE_FUNC_NAMES` |
| Variable Flow rendering bug | `html_renderer.py` → `_build_var_flow_data`, `_vfBuildFlowChain`, `_vfBuildGraph` |
| Resolution / filter bug | `callgraph/graph/builder.py`, `callgraph/graph/filters.py` |
| DOT / SVG / PNG output | `callgraph/renderers/dot_renderer.py` |
| Agent export (`agent` JSON / `pack` dir) | `callgraph/renderers/agent_renderer.py`, `callgraph/renderers/agent_pack.py` |
