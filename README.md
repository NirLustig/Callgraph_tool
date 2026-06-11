# 📊 CallGraph Analyzer

> **Offline, self-contained static call-graph, variable-flow, and architecture analyser**  
> for **C, C++, Python, and MATLAB** — with a single-file interactive HTML output.

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)]()

---

## ✨ What It Does

CallGraph Analyzer scans your source code and produces a **fully self-contained interactive HTML graph** — no server, no internet, no extra installs needed to view it. Just open the file in any browser.

| Capability | Details |
|---|---|
| **Call graph** | Extracts all function definitions and resolves call relationships with confidence scoring (exact / heuristic / unresolved) |
| **Variable Flow** | Tracks specific variable names as they flow through function calls — with per-path branch colouring |
| **Include Graph** | Maps `#include` dependency chains for C/C++ projects |
| **Architecture rules** | Define forbidden dependencies, allowed-only edges, layering rules in YAML — violations shown as red edges |
| **Script Nodes** | File-level view with collapsible cards, callee badges colour-coded by location (same file / other file / external) |
| **Function Nodes** | Function-level canvas with drag, search, isolate, highlight, and depth-limited traversal |
| **Module View** | Aggregated module-level graph auto-inferred from folder structure or YAML config |
| **Visual Studio .sln** | Reads `.sln` + `.vcxproj` to discover and analyse all C/C++ source files automatically |
| **Macro expansion** | Conservative `#define` pre-pass so calls hidden behind macros resolve correctly |
| **Function-pointer resolution** | Detects `fp = handler;` / `fp = target;` assignments and resolves pointer calls to real callees |

---

## 🖥️ Screenshots

> Open `output/demo_chaincheck.html` in your browser after installation to see the tool in action.

---

## 📋 Requirements

| Requirement | Version |
|---|---|
| Python | 3.9 or later |
| pip | any recent version |
| Graphviz binary *(optional)* | for SVG / PNG output — [graphviz.org](https://graphviz.org/download/) |

---

## 🚀 Installation

### 1 — Clone or download the project

```bash
git clone https://github.com/your-username/callgraph-tool.git
cd callgraph-tool
```

### 2 — Create a virtual environment *(recommended)*

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# macOS / Linux
python -m venv .venv
source .venv/bin/activate
```

### 3 — Install dependencies

```bash
pip install -r requirements.txt
```

### 4 — (Optional) Install Graphviz for SVG / PNG output

```bash
# Windows (winget)
winget install graphviz

# macOS
brew install graphviz

# Ubuntu / Debian
sudo apt install graphviz
```

> The `.dot` text file is **always** written regardless of whether Graphviz is installed.  
> The interactive HTML output **never** requires Graphviz.

---

## 🖱️ GUI Usage

The GUI exposes all options through a visual form — no command-line knowledge required.

```bash
python gui.py
```

**GUI walkthrough:**

1. **Project path** — click *Browse* to select a folder or a `.sln` file.
2. **Output path** — choose where to save the HTML (defaults to `output/graph.html`).
3. **Config file** — optionally pick a `config.yaml` for advanced filtering and rules.
4. **Render slots** — choose what each of the two HTML view buttons shows (Function / Script / Module / Folder / etc.).
5. **Entry point + Depth** — optionally focus the graph from a specific function.
6. **Include Graph** — tick to add a `#include` dependency view to the HTML.
7. Click **Run** — a live log shows parse progress; the HTML opens automatically on completion.

> **Visual Studio solution**: when you browse to a `.sln` file a project-selection dialog appears automatically. Tick the projects and folders you want to analyse then click Confirm.

---

## 💻 Terminal Usage

### Basic — analyse a folder

```bash
python callgraph_tool.py --project ./my_project --output graph.html
```

### Analyse a Visual Studio solution

```bash
python callgraph_tool.py --project "C:\work\MyApp.sln" --output graph.html
```

### With a config file

```bash
python callgraph_tool.py --project ./my_project --config config.yaml --output graph.html
```

### Focus on an entry point, depth 4

```bash
python callgraph_tool.py --project ./my_project --entry main --depth 4 --output graph.html
```

### Multiple output formats (HTML + SVG)

```bash
python callgraph_tool.py --project ./my_project --output graph --formats html svg
```

### Enable the Include Graph view

```bash
python callgraph_tool.py --project ./src --include-graph --output graph.html
```

### Custom render slots (e.g. Function vs Module)

```bash
python callgraph_tool.py --project ./src --view-slot-1 function --view-slot-2 module --output graph.html
```

### Architecture violations report

```bash
python callgraph_tool.py --project ./src --config arch.yaml \
    --architecture-report violations.json --output graph.html
```

### Show all options

```bash
python callgraph_tool.py --help
```

---

## 📁 Supported Languages & File Types

| Language | Extensions | Parser |
|---|---|---|
| Python | `.py` | Built-in `ast` module |
| C | `.c`, `.h` | Tree-sitter |
| C++ | `.cpp`, `.cc`, `.cxx`, `.hpp`, `.hxx`, `.hh` | Tree-sitter |
| MATLAB | `.m` | Regex-based |

---

## 🗂️ Output Formats

| Format | Description |
|---|---|
| `html` | ✅ **Recommended** — fully self-contained interactive graph (no internet needed) |
| `dot` | Graphviz DOT source text — always written |
| `svg` | Scalable vector graphic *(requires Graphviz binary)* |
| `png` | Raster image *(requires Graphviz binary)* |

---

## ⚙️ Configuration File

Copy the example and customise:

```bash
copy config.example.yaml my_config.yaml   # Windows
cp config.example.yaml my_config.yaml     # macOS / Linux
```

### Key settings

```yaml
filter:
  exclude_dirs: [".git", "__pycache__", "tests", "vendor"]
  exclude_functions: ["__init__", "__repr__"]
  entry_points: ["main"]   # focus graph from here
  max_depth: 5             # null = unlimited
  show_external: false     # include stdlib / library calls

variables:
  track: true
  names: ["buf_size", "mode", "result"]   # variables to trace in Variable Flow

output:
  formats: ["html", "svg"]
  layout: "hierarchical"   # or "force"
  max_nodes: 300

parser:
  expand_macros: true      # expand #define macros before parsing (C/C++)

architecture:
  modules:
    Control:  ["src/control/**"]
    Drivers:  ["src/drivers/**"]
    UI:       ["src/ui/**"]
  rules:
    - kind: forbidden
      from: UI
      to:   Drivers
      reason: "UI must go through Application layer"
    - kind: layer
      layers: ["UI", "Application", "Control", "Drivers", "HAL"]
      reason: "Strict downward layering"
```

See `config.example.yaml` for the full annotated reference.

---

## 🌐 Navigating the HTML Output

Open the generated `.html` file in any modern browser (Chrome, Firefox, Edge).  
The sidebar on the left switches between view modes. The **dark/light theme toggle** is next to the title.

### Global controls (work in every mode)

| Control | How |
|---|---|
| **Pan** | Click and hold the **mouse wheel** (middle button) + drag |
| **Zoom** | Scroll wheel |
| **Dark / Light theme** | Toggle button next to *CallGraph Analyzer* title |
| **Search** | Type a function or file name in the search box + Enter |
| **Fit view** | Click *Fit* to zoom-to-fit all visible content |

### Confidence edge colours (Function / Module / Include modes)

| Colour | Meaning |
|---|---|
| Solid blue | `exact` — resolved to a unique definition |
| Dashed orange | `heuristic` — best-guess resolution |
| Gray dashed | `unresolved` / `external` |
| Bold red | `violation` — architecture rule breach |

---

### 🔵 Mode 1 — Function Nodes

**What it shows:** One node per function. Edges are call relationships. This is the default starting mode and the most detailed view.

**How to use:**

| Action | How |
|---|---|
| Select a function | Single-click a node → opens the **detail panel** on the right (signature, file, calls, confidence) |
| Full inspection | Double-click a node → opens a **modal** with full signature, all callers, all callees, variable annotations, and resolution reason |
| Drag a node | Click-and-hold then drag — positions are saved in browser `localStorage` automatically |
| Multi-select nodes | Click-and-drag on empty canvas to draw a **selection rectangle** |
| Search | Type in the search box + Enter → matching nodes are highlighted yellow |
| Highlight | After searching, click *Highlight* to zoom to the first match |
| Isolate | Select a node → click *Isolate* → hides everything **except** that node and its call tree |
| Expand | Select a node → click *Expand* → makes reachable nodes visible without hiding others |
| Clear isolation | Click *Show All* to restore all hidden nodes |
| Set depth | The **Depth** selector controls how many call hops Isolate/Expand follows |
| Set direction | *Callers*, *Callees*, or *Both* — controls which direction Isolate/Expand traverses |
| Fit view | *Fit* button → zoom-to-fit all visible nodes |
| Reset layout | *Reset* → restore the original computed positions |
| Clear saved positions | *Clear saved* → erase `localStorage` positions and re-layout |

**Tips:**
- Use *Isolate* + a shallow **Depth** (1–2) to focus on a single function's immediate neighbourhood.
- Use *Direction = Callers* to trace **who calls** a specific function (useful for debugging or impact analysis).
- Use *Direction = Callees* to trace **what a function depends on** (useful for understanding a module's footprint).
- Hover over an edge to see the confidence tooltip.
- Violation edges (red) are always visible regardless of the Confidence filter.

---

### 📄 Mode 2 — Script Nodes

**What it shows:** One **card** per source file. Each card lists the functions defined in that file as clickable rows. Call relationships are shown as **callee badges** inside each row — colour-coded by destination:

| Badge colour | Meaning |
|---|---|
| 🟢 Green | Callee is in the **same file** |
| 🟠 Orange | Callee is in a **different project file** |
| ⚫ Gray | Callee is an **external / library** function |

**How to use:**

| Action | How |
|---|---|
| Select a function | Click a function row → highlights the row and opens the detail panel |
| Inspect a function | Double-click a function row → opens the full inspection modal |
| Jump to a callee | Click a callee badge → scrolls to and selects that function's row (even across cards) |
| Highlight a file card | Click the **file header** (the card title bar) → highlights the whole card |
| Collapse a card | Click the **▼ / ▶ triangle** on the card header → collapses/expands it. Collapsed state is saved in `localStorage` |
| Search | Type a function or filename → matching rows are highlighted |
| Fit view | Scrolls back to the top |
| Annotation toggle | The *Annot* button in the sidebar toggles variable annotation badges on rows |

**Tips:**
- Collapse cards you don't need to reduce visual clutter — the state persists across page reloads.
- This mode is ideal for **code review** — you can see at a glance what each file calls and whether those calls stay local or cross file boundaries.
- The callee badge count next to a function name shows how many distinct callees it has.

---

### 🧩 Mode 3 — Module View

**What it shows:** One node per **logical module** (either defined in `config.yaml → architecture.modules` or auto-inferred from top-level folder structure). Edges represent aggregated call relationships between modules — the edge label shows the number of underlying individual calls.

**How to use:**

| Action | How |
|---|---|
| Select a module node | Single-click → opens detail panel showing which files/functions belong to this module |
| Inspect | Double-click → modal with all cross-module calls in and out |
| Hover over an edge | Tooltip shows the underlying call count and confidence breakdown |
| Violation edges | Red edges = architecture rule breaches. Click *Violations* in the sidebar for a full list |
| Search | Type a module name to highlight it |
| Isolate | Same as Function mode — hides unrelated modules |

**Tips:**
- This is the best mode for **architecture review meetings** — it gives a high-level dependency map of your system.
- If modules look wrong, define them explicitly in `config.yaml → architecture.modules` with glob patterns.
- Pair with the **Architecture Rule Engine** to enforce and visualise layering constraints.

---

### 🔬 Mode 4 — Variable Flow

**What it shows:** Highlights how a tracked variable flows through function calls. Each outgoing path from the variable is coloured with a **distinct colour**. When a path splits again (the variable is passed to multiple functions at a deeper level), a **blended second-layer colour** is added.

**Setup:**

```yaml
# config.yaml
variables:
  track: true
  names:
    - "buf_size"       # track this variable name
    - "ChainCheck"
```

**How to use:**

| Action | How |
|---|---|
| View variable paths | Switch to *Variable Flow* in the sidebar — coloured edges show propagation paths |
| First-level branches | Each direct recipient of the variable gets a unique colour |
| Second-level branches | A sub-path inherits a **blended** variant of its parent colour |
| Select a node | Single-click → detail panel shows where the variable came from and where it goes next |
| Inspect a node | Double-click → modal shows the full variable annotation for that function |
| Filter by variable | Use the search box to highlight a specific variable name if multiple are tracked |

**Tips:**
- This mode is most powerful for tracking **data buffers, configuration values, or callback function pointers** that are passed down through many layers.
- A variable that fans out to many functions at once will produce many coloured branches — useful for spotting unexpected data sharing.
- If a variable disappears from the graph it means it's not passed further (it's consumed locally or the tracking ended at that depth).

---

### 🗺️ Mode 5 — Include Graph

**What it shows:** A graph of `#include` relationships between C/C++ source and header files. Local includes and system includes (`<stdio.h>`) are distinguished. Cycles in the include chain are highlighted.

**Enable it:**

```bash
python callgraph_tool.py --project ./src --include-graph --output graph.html
```

Or in `config.yaml`:

```yaml
include_graph:
  enabled: true
  follow_system: false   # set true to also render <system> headers
```

**How to use:**

| Action | How |
|---|---|
| Select a file node | Single-click → shows which files it includes and which files include it |
| Toggle system headers | The **System includes** checkbox in the sidebar shows/hides `<stdio.h>`-style headers |
| Identify cycles | Cycle edges are highlighted — circular includes can cause compilation issues |
| Inspect | Double-click → full include list for that file |
| Isolate | Select a header → Isolate to see exactly which files depend on it |

**Tips:**
- Use this mode to find **headers that are included everywhere** (candidates for pre-compiled headers or refactoring).
- Cycles in the include graph are a common source of compilation order problems — this mode makes them immediately visible.
- System headers are hidden by default; enable them only when you need to see stdlib dependencies.

---

## 🏗️ Architecture Rule Engine

Define dependency constraints in YAML:

```yaml
architecture:
  rules:
    - kind: forbidden      # A must not call B
    - kind: allowed_only   # A may only call listed modules
    - kind: required       # A must have at least one edge to B
    - kind: layer          # declare ordered layers; upward calls are violations
```

Violations appear as **red edges** in all view modes and are listed in a sidebar modal. Export a machine-readable JSON report with `--architecture-report`.

---

## 🧪 Running Tests

```bash
pip install pytest
pytest tests/ -v
```

Run a specific test file:

```bash
pytest tests/test_c_parser.py -v
pytest tests/test_macro_expand.py -v
pytest tests/test_fp_resolution.py -v
```

Current test count: **214 tests**.

---

## 🛠️ Troubleshooting

### "No source files found"
- Verify `--project` points to the correct directory.
- Confirm the project contains `.py`, `.c`, `.cpp`, or `.m` files.
- If `include_dirs` is set in config, ensure the glob pattern matches your structure.

### `ModuleNotFoundError: tree_sitter_c`
```bash
pip install tree-sitter tree-sitter-c tree-sitter-cpp
```

### `ModuleNotFoundError: yaml`
```bash
pip install PyYAML
```

### `ModuleNotFoundError: pyvis`
```bash
pip install pyvis
```

### SVG / PNG fails — "dot not found"
- Install Graphviz and restart your terminal so `PATH` is updated.
- The `.dot` text file is always written — you can run `dot` manually later.

### HTML opens but graph is blank
- The graph may be very large. Add `--entry <function>` and `--depth 4` to reduce scope.
- Open browser DevTools (F12 → Console) to check for JavaScript errors.
- Try a different browser.

### Buttons show "Not ready" briefly
- Normal on first load while the graph engine initialises. Wait 1–2 seconds and try again.

### Nodes overlap badly
- For large projects use `--entry` + `--depth` to show a focused subgraph.
- Drag nodes to reposition them — positions are saved in browser `localStorage`.

### Macro-wrapped calls not resolving (C/C++)
- Ensure `parser.expand_macros: true` in your config (default is `true`).
- Token-paste (`##`) and variadic macros are intentionally not expanded.

### Function-pointer calls show as unresolved
- The tool resolves same-file `fp = target;` assignments automatically.
- Cross-file pointer registration (e.g. `init()` in another TU) is not yet tracked — see idea RES-5.

---

## 📂 Project Structure

```
callgraph_tool.py       CLI entry point
gui.py                  Tkinter GUI
config.example.yaml     Full annotated configuration reference
requirements.txt        Python dependencies
tests/                  pytest test suite + example projects
output/                 Generated graphs (gitignored)
callgraph/
  cli.py                Argument parsing + pipeline orchestration
  config.py             Config dataclasses + YAML loading
  discovery.py          File discovery + language grouping
  models.py             FunctionDef, CallRelationship, CallGraph, …
  aggregator.py         Function → File/Folder/Module/Library/Namespace
  architecture.py       Module mapping + rule engine + violations
  include_graph.py      #include extraction + cycle detection
  build_info.py         compile_commands.json parser
  sln_reader.py         Visual Studio .sln / .vcxproj reader
  parsers/
    c_parser.py         C parser (Tree-sitter) + macro/FP helpers
    cpp_parser.py       C++ parser (Tree-sitter)
    python_parser.py    Python parser (ast)
    matlab_parser.py    MATLAB parser (regex)
    macro_expand.py     Conservative #define expander
  graph/
    builder.py          Call resolution + graph construction
    resolver.py         _ResolutionIndex + confidence scoring
  renderers/
    html_renderer.py    Self-contained HTML generator (all UI lives here)
```

---

## 📄 License

This project is provided for internal / personal use. No redistribution license is currently specified.

---

*Built with [Tree-sitter](https://tree-sitter.github.io/tree-sitter/), [vis-network](https://visjs.github.io/vis-network/), [NetworkX](https://networkx.org/), and [PyYAML](https://pyyaml.org/).*
