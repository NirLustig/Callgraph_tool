# CallGraph Tool

Offline multi-language function call graph analyzer for Python, C, C++, MATLAB, and Visual Studio C/C++ solutions.

CallGraph Tool scans a source-code project, extracts functions and call relationships, resolves internal calls where possible, and generates an interactive self-contained HTML call graph. It is designed to help developers understand unfamiliar codebases, trace execution flow, inspect function dependencies, and navigate large projects visually without uploading code to external services.

## Repository Description

**CallGraph Tool** is an offline static-analysis utility that builds interactive function call graphs from local source-code projects. It supports Python, C, C++, MATLAB, and Visual Studio `.sln` files, with configurable filtering, entry-point analysis, depth limiting, external-call visibility, variable tracking, and multiple export formats including HTML, DOT, SVG, and PNG. The generated HTML report includes search, isolation, expansion, function inspection, script-level grouping, and persistent browser-side layout adjustments.

## Features

- **Offline static analysis** — no cloud service or internet connection required for analysis or viewing the generated HTML.
- **Multi-language support**:
  - Python (`.py`)
  - C (`.c`, `.h`)
  - C++ (`.cpp`, `.cc`, `.cxx`, `.hpp`, `.hxx`, `.hh`)
  - MATLAB (`.m`)
- **Visual Studio solution support** — pass a `.sln` file directly and the tool discovers referenced C/C++ `.vcxproj` files.
- **Interactive HTML output** with:
  - Function-node graph mode
  - Script/file-node mode
  - Search and highlight
  - Call-tree isolation
  - Expand by caller/callee direction
  - Hover popups
  - Double-click function inspection
  - Draggable nodes with saved browser layout
- **Configurable filtering**:
  - Include/exclude directories
  - Include/exclude files
  - Include/exclude functions
  - Entry-point filtering
  - Maximum call depth
  - External/library-call visibility
- **Multiple output formats**:
  - `html`
  - `dot`
  - `svg`
  - `png`
- **Optional tracked-variable annotations** for selected variable names.
- **Pytest-based test suite** for parsers, configuration, and graph-building logic.

## Project Structure

```text
callgraph_tool_v4_filenode/
├── callgraph/
│   ├── cli.py                  # Command-line interface and pipeline orchestration
│   ├── config.py               # YAML/JSON configuration loading
│   ├── discovery.py            # Project file discovery
│   ├── sln_reader.py           # Visual Studio .sln / .vcxproj reader
│   ├── models.py               # Core data models
│   ├── graph/
│   │   ├── builder.py          # Call graph construction and call resolution
│   │   └── filters.py          # Graph filtering utilities
│   ├── parsers/
│   │   ├── python_parser.py    # Python parser using ast
│   │   ├── c_parser.py         # C parser using Tree-sitter
│   │   ├── cpp_parser.py       # C++ parser using Tree-sitter
│   │   └── matlab_parser.py    # MATLAB parser
│   └── renderers/
│       ├── html_renderer.py    # Interactive HTML renderer
│       └── dot_renderer.py     # DOT/SVG/PNG renderer
├── tests/                      # Unit tests and example projects
├── config.example.yaml         # Example configuration file
├── requirements.txt            # Python dependencies
├── callgraph_tool.py           # Main executable entry point
└── CMD_COMMANDS.txt            # Command reference
```

## Requirements

- Python 3.9+
- pip
- Optional: Graphviz system binary for SVG/PNG export

Install Graphviz on Windows with:

```powershell
winget install graphviz
```

HTML and DOT output do not require the Graphviz binary.

## Installation

Clone the repository:

```bash
git clone https://github.com/<your-username>/<your-repo-name>.git
cd <your-repo-name>
```

Create and activate a virtual environment:

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation scripts, use:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.\.venv\Scripts\Activate.ps1
```

### Windows CMD

```cmd
python -m venv .venv
.venv\Scripts\activate.bat
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## Basic Usage

Analyze a project folder and create an interactive HTML call graph:

```bash
python callgraph_tool.py --project ./my_project --output graph.html
```

Analyze a Visual Studio solution:

```bash
python callgraph_tool.py --project "C:\work\MyApp.sln" --output graph.html
```

Use a configuration file:

```bash
python callgraph_tool.py --project ./my_project --config config.yaml --output graph.html
```

Generate multiple output formats:

```bash
python callgraph_tool.py --project ./my_project --output graph --formats html svg dot
```

Focus on a specific function and limit call depth:

```bash
python callgraph_tool.py --project ./my_project --entry main --depth 3 --output focused_graph.html
```

Show external or unresolved library calls:

```bash
python callgraph_tool.py --project ./my_project --output graph.html --show-external
```

Run with verbose parse information:

```bash
python callgraph_tool.py --project ./my_project --output graph.html --verbose
```

## HTML Viewer Modes

The generated HTML report is self-contained and can be opened directly in a browser.

### Function Nodes Mode

In this mode, every node represents a function or method.

Useful actions:

- Hover over a node to see quick function information.
- Click a node to select it and open the detail panel.
- Double-click a node to open the full function inspection modal.
- Drag nodes to rearrange the graph.
- Use search to find functions by name.
- Use isolate/expand controls to inspect callers, callees, or both.
- Use depth controls to limit traversal distance.

### Script Nodes Mode

In this mode, every block represents a source file or script, and functions are shown as rows inside the file block.

This is useful when you want to understand how functions are organized by file rather than seeing every function as a separate graph node.

Callee badges indicate call type:

- Green: call to a function in the same file
- Orange: call to a function in another project file
- Gray: external or unresolved function call

## Configuration

Copy the example configuration file:

```bash
cp config.example.yaml config.yaml
```

On Windows CMD:

```cmd
copy config.example.yaml config.yaml
```

Example configuration:

```yaml
display:
  show_parameters: true
  show_return_types: true
  show_filenames: true
  show_line_numbers: false
  show_classes: true

filter:
  exclude_dirs:
    - ".git"
    - "__pycache__"
    - "node_modules"
    - "vendor"
    - ".venv"
    - "build"
    - "dist"

  include_dirs: []
  exclude_files: []
  include_files: []

  exclude_functions:
    - "__init__"
    - "__repr__"
    - "__str__"
    - "__del__"

  entry_points: []
  max_depth: null
  show_external: false

variables:
  track: false
  names: []

output:
  formats:
    - html
  layout: "force"
  max_nodes: 3000
```

## Tracked Variables

The tool can optionally annotate selected variable names inside functions.

Example:

```yaml
variables:
  track: true
  names:
    - result
    - mode
    - buffer_size
```

Then run:

```bash
python callgraph_tool.py --project ./my_project --config config.yaml --output graph.html
```

This is a best-effort static analysis feature and is intended for quick inspection, not full runtime data-flow analysis.

## Visual Studio `.sln` Support

You can pass a `.sln` file directly:

```bash
python callgraph_tool.py --project "C:\work\MyApp\MyApp.sln" --output graph.html
```

The tool will:

1. Parse the solution file.
2. Detect referenced C/C++ `.vcxproj` projects.
3. Extract source and header files from `ClCompile` and `ClInclude` entries.
4. Deduplicate shared files.
5. Analyze the discovered C/C++ source files.
6. Render cross-file and cross-project call relationships when they can be resolved.

Notes:

- C#, F#, and VB.NET projects are skipped.
- Solution folders are skipped.
- Missing project or source files are reported as warnings.
- Build-configuration conditions are ignored; all listed files are analyzed.

## Output Formats

### HTML

Interactive graph viewer. Recommended for most use cases.

```bash
python callgraph_tool.py --project ./my_project --output graph.html
```

### DOT

Graphviz DOT text output.

```bash
python callgraph_tool.py --project ./my_project --output graph --formats dot
```

### SVG / PNG

Requires Graphviz installed on the system.

```bash
python callgraph_tool.py --project ./my_project --output graph --formats svg png
```

## Running Tests

Install test dependencies:

```bash
pip install pytest
```

Run all tests:

```bash
pytest tests/ -v
```

Note: in the uploaded version, two configuration tests expect `max_nodes: 300`, while the current code default is `3000`. Update either the tests or the default value before treating the suite as fully passing.

Run a specific test file:

```bash
pytest tests/test_python_parser.py -v
```

## Common Troubleshooting

### `No source files found`

Check that `--project` points to the correct folder and that the project contains supported file types.

### `tree-sitter-c is not installed`

Install the parser dependencies:

```bash
pip install tree-sitter tree-sitter-c tree-sitter-cpp
```

### `No module named yaml`

Install PyYAML:

```bash
pip install PyYAML
```

### SVG/PNG output fails with `dot not found`

Install Graphviz and restart the terminal:

```powershell
winget install graphviz
```

You can still generate HTML and DOT output without Graphviz.

### HTML opens but the graph is too large or slow

Use entry-point and depth filtering:

```bash
python callgraph_tool.py --project ./my_project --entry main --depth 3 --output focused_graph.html
```

You can also reduce the scanned files using `include_dirs`, `include_files`, or `exclude_dirs` in `config.yaml`.

## Recommended `.gitignore`

Before publishing to GitHub, consider excluding generated and local files:

```gitignore
.venv/
venv/
__pycache__/
*.pyc
.pytest_cache/
output/
*.html
*.svg
*.png
*.dot
```

If you want to include example HTML outputs in the repository, remove the relevant output patterns from `.gitignore`.

## Limitations

- This is a static-analysis tool, so dynamic calls, reflection, macros, function pointers, runtime dispatch, and generated code may not be fully resolved.
- MATLAB parsing is regex-based and may not cover every advanced MATLAB syntax pattern.
- Variable tracking is best-effort and does not replace a complete language-server or runtime data-flow analysis.
- Very large graphs can become hard to read; entry-point and depth filtering are recommended.

## Roadmap Ideas

Potential future improvements:

- More advanced variable-flow analysis.
- Better C/C++ macro and function-pointer handling.
- Export/import of graph layouts.
- Additional language support.
- Better project-level grouping and module-level summaries.
- Searchable source-code snippets inside the inspection panel.

## License

Add your preferred license here, for example MIT, Apache-2.0, or GPL-3.0.

## Author

Developed as a local developer tool for visualizing and understanding function relationships in multi-language codebases.
