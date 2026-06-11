"""
gui.py — Tkinter GUI for CallGraph Tool.
Run: python gui.py

Exposes all CLI parameters through a graphical interface.

Two big additions over earlier versions:
- A "Rendering" section with two render-slot dropdowns + Include Graph checkbox +
  compile_commands path browser.
- An auto-opening "Inspect Solution" modal that fires when the user selects a .sln file:
  the GUI runs `callgraph_tool.py --inspect-sln`, parses the JSON, and presents a
  tree-view of projects -> folders -> files with checkboxes. The selection is
  persisted in `.callgraph.selection.json` next to the .sln file and turned into
  --include-projects / --include-files flags when the Run button is clicked.
- An auto-opening "Inspect Folders" modal that fires when the user selects a folder:
  the GUI shows a subfolder tree with checkboxes. The selection is persisted in
  `.callgraph.folder_selection.json` in that folder and turned into --include-files
  globs when the Run button is clicked.

Delegates execution to callgraph_tool.py via subprocess so the full pipeline runs
unchanged and the CLI continues to work as before.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk


_TOOL = Path(__file__).parent / "callgraph_tool.py"
_PYTHON = sys.executable
_ANSI = re.compile(r'\x1b\[[0-9;]*[mGKHF]')
_STAGE_RE = re.compile(r'^\[stage (\d+)/(\d+)\]\s+(.+?)\.\.\.\s*$')

_RENDER_LEVELS = (
    ("function",  "Function (one node per function)"),
    ("script",    "Script / File (one node per file)"),
    ("folder",    "Folder (configurable depth)"),
    ("module",    "Module (config or auto-inferred)"),
    ("library",   "Library (.vcxproj / project)"),
    ("namespace", "Namespace / Class"),
)


def _strip_ansi(text: str) -> str:
    return _ANSI.sub('', text)


# ── Tooltip ───────────────────────────────────────────────────────────────────

class _Tooltip:
    def __init__(self, widget: tk.Widget, text: str) -> None:
        self._w = widget
        self._text = text
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None) -> None:
        if self._tip:
            return
        x = self._w.winfo_rootx() + self._w.winfo_width() + 6
        y = self._w.winfo_rooty() - 2
        self._tip = tk.Toplevel(self._w)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self._tip, text=self._text, justify=tk.LEFT,
            background="#fffce8", relief=tk.SOLID, borderwidth=1,
            font=("Segoe UI", 9), wraplength=290, padx=8, pady=6,
        ).pack()

    def _hide(self, _event=None) -> None:
        if self._tip:
            self._tip.destroy()
            self._tip = None


def _info(parent: tk.Widget, tip: str) -> tk.Label:
    badge = tk.Label(
        parent, text=" i ", font=("Segoe UI", 8, "bold"),
        fg="white", bg="#0078d4", cursor="question_arrow",
        relief=tk.FLAT, padx=3, pady=1,
    )
    _Tooltip(badge, tip)
    return badge


_TIP = {
    "project": (
        "Required.\n\n"
        "Path to the source code folder you want to analyze,\n"
        "or a Visual Studio .sln solution file.\n\n"
        "When a .sln is loaded, the 'Inspect Solution' modal\n"
        "auto-opens so you can pick which projects/folders/files\n"
        "to include.\n\n"
        "When a folder is loaded, the 'Inspect Folders' modal\n"
        "auto-opens so you can pick which subfolders to include."
    ),
    "output": (
        "Required.\n\n"
        "Where to save the generated graph.\n\n"
        "With extension  → one file only (output/graph.html)\n"
        "Without extension → all selected formats (output/graph)."
    ),
    "formats": (
        "Required — select at least one format.\n\n"
        "HTML  Interactive graph in your browser.\n"
        "DOT   Graphviz source text file.\n"
        "SVG   Vector image — requires Graphviz on PATH.\n"
        "PNG   Raster image — requires Graphviz on PATH."
    ),
    "config": (
        "Optional. YAML or JSON config (see config.example.yaml).\n"
        "Used for architecture rules, module mapping, custom\n"
        "filters, etc."
    ),
    "entry": (
        "Optional. Comma-separated function names. Only functions\n"
        "reachable from these entry points appear in the graph."
    ),
    "depth": "Optional. Hop limit from entry points.",
    "external": "Show external (stdlib / third-party) calls as grey stub nodes.",
    "verbose": "Detailed parse log.",
    "max_nodes": "Cap on graph nodes (default 3000).",
    "parallel": "Parser worker threads (default: auto-detect).",
    "summary": "Collapse to one node per file. Useful for huge .sln projects.",
    "cc": (
        "Optional. Path to compile_commands.json.\n"
        "Auto-detected in project root / build/ / out/ when blank.\n"
        "Provides per-file include paths and preprocessor defines that\n"
        "improve C/C++ include-graph resolution and feed the Build Info\n"
        "panel in the HTML output."
    ),
    "slot_1": (
        "What the FIRST HTML view button shows.\n"
        "Default: Function (one node per function — today's behaviour).\n\n"
        "Other levels aggregate the call graph to file / folder / module /\n"
        "library (= .vcxproj project) / namespace boundaries."
    ),
    "slot_2": (
        "What the SECOND HTML view button shows.\n"
        "Default: Script (one card per file — today's behaviour).\n\n"
        "Pick a coarser level for a high-level architecture view; pick\n"
        "Function for a dense low-level view in slot 2."
    ),
    "include_graph": (
        "Build the Include Graph mode in the HTML output.\n"
        "Parses #include directives in C/C++ files and renders a separate\n"
        "view with local/system include classification + cycle detection."
    ),
    "include_system_headers": (
        "Also show system includes (#include <math.h>, <string>, etc.) as nodes\n"
        "in the Include Graph. By default only project headers are shown.\n"
        "You can also toggle this inside the HTML output without re-running."
    ),
}


_SLN_SELECTION_SIDECAR = ".callgraph.selection.json"
_FOLDER_SELECTION_SIDECAR = ".callgraph.folder_selection.json"

_FOLDER_PICKER_EXCLUDE_DIRS = {
    ".git", ".svn", "__pycache__", "node_modules", "vendor",
    ".venv", "venv", "env", "build", "dist", ".eggs",
}


# ── .sln inspection helpers ───────────────────────────────────────────────────

def _run_inspect_sln(sln_path: str) -> dict | None:
    """Invoke the CLI inspector and return the parsed JSON, or None on failure."""
    cmd = [_PYTHON, str(_TOOL), "--project", sln_path, "--inspect-sln"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _load_sln_sidecar(sln_path: str) -> dict:
    p = Path(sln_path).with_name(_SLN_SELECTION_SIDECAR)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_sln_sidecar(sln_path: str, payload: dict) -> None:
    p = Path(sln_path).with_name(_SLN_SELECTION_SIDECAR)
    try:
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def _load_folder_sidecar(project_dir: str) -> dict:
    p = Path(project_dir) / _FOLDER_SELECTION_SIDECAR
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_folder_sidecar(project_dir: str, payload: dict) -> None:
    p = Path(project_dir) / _FOLDER_SELECTION_SIDECAR
    try:
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def _list_subfolders(project_dir: str) -> list[str]:
    root = Path(project_dir).resolve()
    if not root.exists() or not root.is_dir():
        return []
    out: list[str] = []
    for dirpath, dirnames, _ in os.walk(root, topdown=True):
        dirnames[:] = [
            d for d in dirnames
            if d not in _FOLDER_PICKER_EXCLUDE_DIRS and not d.startswith(".")
        ]
        rel = Path(dirpath).resolve().relative_to(root).as_posix()
        if rel != ".":
            out.append(rel)
    out.sort(key=lambda s: (s.count("/"), s.lower()))
    return out


# ── Inspect Solution modal ────────────────────────────────────────────────────

class InspectSolutionDialog:
    """Modal Toplevel showing projects/folders/files with checkboxes + config picker.

    Usage:
        dlg = InspectSolutionDialog(parent, sln_path, data)
        result = dlg.show()  # returns dict or None on cancel
    """

    CHECKED = "☑"     # ☑
    UNCHECKED = "☐"   # ☐

    def __init__(self, parent: tk.Misc, sln_path: str, data: dict) -> None:
        self.parent = parent
        self.sln_path = sln_path
        self.data = data
        self.result: dict | None = None
        self._checked: dict[str, bool] = {}
        self._top: tk.Toplevel | None = None
        self._tree: ttk.Treeview | None = None
        self._cfg_var: tk.StringVar | None = None
        self._plat_var: tk.StringVar | None = None

    def show(self) -> dict | None:
        self._top = tk.Toplevel(self.parent)
        self._top.title("Inspect Solution — select projects, folders, and files")
        self._top.minsize(720, 520)
        self._top.transient(self.parent)
        self._top.grab_set()

        sidecar = _load_sln_sidecar(self.sln_path)
        prev_checked = set(sidecar.get("checked_ids", []))
        prev_cfg = sidecar.get("configuration") or self.data.get("active_configuration")
        prev_plat = sidecar.get("platform") or self.data.get("active_platform")

        # ── Top: Configuration / Platform dropdowns ───────────────────────
        top_bar = ttk.Frame(self._top, padding=(10, 8))
        top_bar.pack(fill=tk.X)
        ttk.Label(top_bar, text="Configuration:").pack(side=tk.LEFT)
        self._cfg_var = tk.StringVar(value=prev_cfg or "")
        cfg_combo = ttk.Combobox(
            top_bar, textvariable=self._cfg_var,
            values=self.data.get("configurations", []),
            width=14, state="readonly",
        )
        cfg_combo.pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(top_bar, text="Platform:").pack(side=tk.LEFT)
        self._plat_var = tk.StringVar(value=prev_plat or "")
        plat_combo = ttk.Combobox(
            top_bar, textvariable=self._plat_var,
            values=self.data.get("platforms", []),
            width=14, state="readonly",
        )
        plat_combo.pack(side=tk.LEFT, padx=4)

        ttk.Label(top_bar, foreground="gray",
                  text="Affects only include paths / preprocessor defines from .vcxproj").pack(
            side=tk.LEFT, padx=(12, 0))

        # ── Tree ──────────────────────────────────────────────────────────
        tree_frame = ttk.Frame(self._top, padding=(10, 4))
        tree_frame.pack(fill=tk.BOTH, expand=True)

        self._tree = ttk.Treeview(tree_frame, show="tree", selectmode="none", height=20)
        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=ysb.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ysb.pack(side=tk.RIGHT, fill=tk.Y)

        self._populate_tree(prev_checked)
        self._tree.bind("<Button-1>", self._on_click)

        # ── Bottom buttons ────────────────────────────────────────────────
        btn_bar = ttk.Frame(self._top, padding=(10, 8))
        btn_bar.pack(fill=tk.X)
        ttk.Label(btn_bar, foreground="gray",
                  text="Tip: Cancel = analyze the whole solution (today's behavior).").pack(
            side=tk.LEFT)
        ttk.Button(btn_bar, text="Cancel", command=self._cancel).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_bar, text="OK", command=self._ok).pack(side=tk.RIGHT)

        self._top.protocol("WM_DELETE_WINDOW", self._cancel)
        self.parent.wait_window(self._top)
        return self.result

    def _populate_tree(self, prev_checked: set[str]) -> None:
        t = self._tree
        assert t is not None
        projects = self.data.get("projects", [])
        for proj in projects:
            pname = proj.get("name", "<unnamed>")
            pid = "P:" + pname
            file_count = len(proj.get("files", []))
            label = f"{self.CHECKED} {pname}   ({file_count} files)"
            t.insert("", "end", iid=pid, text=label, open=True)
            self._checked[pid] = (not prev_checked) or pid in prev_checked
            self._refresh_label(pid)
            for folder in proj.get("folders", []):
                fname = folder.get("name", "<root>")
                fid = pid + ":F:" + fname
                ffiles = folder.get("files", [])
                flabel = f"{self.CHECKED} {fname}  ({len(ffiles)} files)"
                t.insert(pid, "end", iid=fid, text=flabel, open=False)
                self._checked[fid] = (not prev_checked) or fid in prev_checked
                self._refresh_label(fid)
                for f in ffiles:
                    fpath = f
                    fid2 = fid + ":F:" + fpath
                    t.insert(fid, "end", iid=fid2,
                             text=f"{self.CHECKED} {Path(fpath).name}",
                             open=False)
                    self._checked[fid2] = (not prev_checked) or fid2 in prev_checked
                    self._refresh_label(fid2)

    def _refresh_label(self, iid: str) -> None:
        t = self._tree
        assert t is not None
        cur = t.item(iid, "text")
        # Drop existing leading box character + space
        if cur and cur[0] in (self.CHECKED, self.UNCHECKED):
            cur = cur[2:]
        box = self.CHECKED if self._checked.get(iid, False) else self.UNCHECKED
        t.item(iid, text=f"{box} {cur}")

    def _on_click(self, event) -> None:
        t = self._tree
        assert t is not None
        region = t.identify("region", event.x, event.y)
        if region != "tree":
            return
        iid = t.identify_row(event.y)
        if not iid:
            return
        # Toggle this node and propagate to descendants.
        new_state = not self._checked.get(iid, True)
        self._toggle_subtree(iid, new_state)
        # Up-propagate: if any sibling has a different state, parent stays as-is;
        # if all siblings share state, parent inherits.
        parent = t.parent(iid)
        while parent:
            children = t.get_children(parent)
            states = {self._checked.get(c, True) for c in children}
            if len(states) == 1:
                self._checked[parent] = next(iter(states))
                self._refresh_label(parent)
            else:
                # Mixed — keep current parent state; nothing to do.
                break
            parent = t.parent(parent)

    def _toggle_subtree(self, iid: str, new_state: bool) -> None:
        t = self._tree
        assert t is not None
        self._checked[iid] = new_state
        self._refresh_label(iid)
        for c in t.get_children(iid):
            self._toggle_subtree(c, new_state)

    def _ok(self) -> None:
        t = self._tree
        assert t is not None
        # Selected projects = projects whose checkbox is True
        selected_projects: list[str] = []
        excluded_files: list[str] = []   # for projects fully selected, deselected files become exclusions
        included_files: list[str] = []   # for projects partially selected, only checked files count
        projects = self.data.get("projects", [])
        for proj in projects:
            pid = "P:" + proj.get("name", "<unnamed>")
            if not self._checked.get(pid, True):
                continue
            selected_projects.append(proj.get("name", ""))
            # Walk every file under this project; if unchecked, exclude.
            for folder in proj.get("folders", []):
                fid = pid + ":F:" + folder.get("name", "<root>")
                for f in folder.get("files", []):
                    file_iid = fid + ":F:" + f
                    if self._checked.get(file_iid, True):
                        included_files.append(f)
                    else:
                        excluded_files.append(f)

        self.result = {
            "configuration": self._cfg_var.get() if self._cfg_var else "",
            "platform": self._plat_var.get() if self._plat_var else "",
            "projects": selected_projects,
            "included_files": included_files,
            "excluded_files": excluded_files,
            "checked_ids": [iid for iid, ok in self._checked.items() if ok],
        }
        # Persist sidecar
        _save_sln_sidecar(self.sln_path, self.result)
        if self._top:
            self._top.destroy()

    def _cancel(self) -> None:
        self.result = None
        if self._top:
            self._top.destroy()


class InspectFolderDialog:
    """Modal Toplevel for selecting subfolders when a project directory is loaded."""

    CHECKED = "☑"
    UNCHECKED = "☐"

    def __init__(self, parent: tk.Misc, project_dir: str, subfolders: list[str]) -> None:
        self.parent = parent
        self.project_dir = str(Path(project_dir).resolve())
        self.subfolders = subfolders
        self.result: dict | None = None
        self._checked: dict[str, bool] = {}
        self._top: tk.Toplevel | None = None
        self._tree: ttk.Treeview | None = None

    def show(self) -> dict | None:
        self._top = tk.Toplevel(self.parent)
        self._top.title("Inspect Folder — select subfolders")
        self._top.minsize(680, 500)
        self._top.transient(self.parent)
        self._top.grab_set()

        sidecar = _load_folder_sidecar(self.project_dir)
        prev_checked = set(sidecar.get("checked_ids", []))

        top_bar = ttk.Frame(self._top, padding=(10, 8))
        top_bar.pack(fill=tk.X)
        ttk.Label(
            top_bar,
            text="Choose which subfolders should be rendered. Unchecked folders are excluded.",
            foreground="gray",
        ).pack(side=tk.LEFT)
        ttk.Button(top_bar, text="Select All", command=self._select_all).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(top_bar, text="Clear All", command=self._clear_all).pack(side=tk.RIGHT)

        tree_frame = ttk.Frame(self._top, padding=(10, 4))
        tree_frame.pack(fill=tk.BOTH, expand=True)
        self._tree = ttk.Treeview(tree_frame, show="tree", selectmode="none", height=20)
        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=ysb.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ysb.pack(side=tk.RIGHT, fill=tk.Y)

        self._populate_tree(prev_checked)
        self._tree.bind("<Button-1>", self._on_click)

        btn_bar = ttk.Frame(self._top, padding=(10, 8))
        btn_bar.pack(fill=tk.X)
        ttk.Label(
            btn_bar,
            foreground="gray",
            text="Tip: Cancel = analyze the whole folder (today's behavior).",
        ).pack(side=tk.LEFT)
        ttk.Button(btn_bar, text="Cancel", command=self._cancel).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_bar, text="OK", command=self._ok).pack(side=tk.RIGHT)

        self._top.protocol("WM_DELETE_WINDOW", self._cancel)
        self.parent.wait_window(self._top)
        return self.result

    def _populate_tree(self, prev_checked: set[str]) -> None:
        t = self._tree
        assert t is not None

        for rel in self.subfolders:
            parent = ""
            parts = rel.split("/")
            acc: list[str] = []
            for part in parts:
                acc.append(part)
                cur_rel = "/".join(acc)
                iid = "D:" + cur_rel
                if t.exists(iid):
                    parent = iid
                    continue
                t.insert(parent, "end", iid=iid, text=f"{self.CHECKED} {part}", open=(len(acc) <= 2))
                self._checked[iid] = (not prev_checked) or iid in prev_checked
                self._refresh_label(iid)
                parent = iid

    def _refresh_label(self, iid: str) -> None:
        t = self._tree
        assert t is not None
        cur = t.item(iid, "text")
        if cur and cur[0] in (self.CHECKED, self.UNCHECKED):
            cur = cur[2:]
        box = self.CHECKED if self._checked.get(iid, False) else self.UNCHECKED
        t.item(iid, text=f"{box} {cur}")

    def _on_click(self, event) -> None:
        t = self._tree
        assert t is not None
        if t.identify("region", event.x, event.y) != "tree":
            return
        iid = t.identify_row(event.y)
        if not iid:
            return
        new_state = not self._checked.get(iid, True)
        self._toggle_subtree(iid, new_state)
        parent = t.parent(iid)
        while parent:
            children = t.get_children(parent)
            states = {self._checked.get(c, False) for c in children}
            self._checked[parent] = next(iter(states)) if len(states) == 1 else False
            self._refresh_label(parent)
            parent = t.parent(parent)

    def _toggle_subtree(self, iid: str, new_state: bool) -> None:
        t = self._tree
        assert t is not None
        self._checked[iid] = new_state
        self._refresh_label(iid)
        for c in t.get_children(iid):
            self._toggle_subtree(c, new_state)

    def _select_all(self) -> None:
        for iid in list(self._checked.keys()):
            self._checked[iid] = True
            self._refresh_label(iid)

    def _clear_all(self) -> None:
        for iid in list(self._checked.keys()):
            self._checked[iid] = False
            self._refresh_label(iid)

    def _ok(self) -> None:
        # Keep top-most checked nodes only; each becomes <folder>/** include glob.
        checked_rel = sorted(
            [iid[2:] for iid, ok in self._checked.items() if ok and iid.startswith("D:")],
            key=lambda s: (s.count("/"), s.lower()),
        )
        selected: list[str] = []
        for rel in checked_rel:
            if not any(rel == parent or rel.startswith(parent + "/") for parent in selected):
                selected.append(rel)

        self.result = {
            "project_dir": self.project_dir,
            "selected_folders": selected,
            "include_globs": [f"{rel}/**" for rel in selected],
            "checked_ids": [iid for iid, ok in self._checked.items() if ok],
        }
        _save_folder_sidecar(self.project_dir, self.result)
        if self._top:
            self._top.destroy()

    def _cancel(self) -> None:
        self.result = None
        if self._top:
            self._top.destroy()


# ── Main application ──────────────────────────────────────────────────────────

class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("CallGraph Tool")
        self.root.minsize(820, 760)
        self._output_files: list[str] = []
        self._sln_selection: dict | None = None
        self._folder_selection: dict | None = None
        self._build()

    def _build(self) -> None:
        root = self.root

        # ── Required ──────────────────────────────────────────────────────
        req = ttk.LabelFrame(root, text=" Project & Output ", padding=10)
        req.pack(fill=tk.X, padx=12, pady=(12, 4))
        req.columnconfigure(1, weight=1)

        ttk.Label(req, text="Project *").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.project_var = tk.StringVar()
        ttk.Entry(req, textvariable=self.project_var).grid(
            row=0, column=1, sticky=tk.EW, padx=(6, 4))
        ttk.Button(req, text="Folder…", width=8,
                   command=self._browse_project_dir).grid(row=0, column=2, padx=(0, 2))
        ttk.Button(req, text=".sln…", width=7,
                   command=self._browse_project_sln).grid(row=0, column=3, padx=(0, 4))
        _info(req, _TIP["project"]).grid(row=0, column=4)

        ttk.Label(req, text="Output *").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.output_var = tk.StringVar()
        ttk.Entry(req, textvariable=self.output_var).grid(
            row=1, column=1, sticky=tk.EW, padx=(6, 4))
        ttk.Button(req, text="Browse…", width=8,
                   command=self._browse_output).grid(
            row=1, column=2, columnspan=2, sticky=tk.W, padx=(0, 4))
        _info(req, _TIP["output"]).grid(row=1, column=4)

        ttk.Label(req,
                  text="Tip: omit the file extension to generate all selected formats at once.",
                  foreground="gray", font=("", 8)).grid(
            row=2, column=1, columnspan=3, sticky=tk.W, padx=(6, 0), pady=(0, 4))

        ttk.Label(req, text="Formats *").grid(row=3, column=0, sticky=tk.W, pady=3)
        fmt_row = ttk.Frame(req)
        fmt_row.grid(row=3, column=1, columnspan=3, sticky=tk.W, padx=(6, 4))
        self.format_vars: dict[str, tk.BooleanVar] = {}
        for fmt in ("html", "dot", "svg", "png"):
            var = tk.BooleanVar(value=(fmt == "html"))
            self.format_vars[fmt] = var
            ttk.Checkbutton(fmt_row, text=fmt.upper(), variable=var).pack(
                side=tk.LEFT, padx=(0, 14))
        _info(req, _TIP["formats"]).grid(row=3, column=4)

        # ── Rendering (NEW) ───────────────────────────────────────────────
        rend = ttk.LabelFrame(root, text=" Rendering ", padding=10)
        rend.pack(fill=tk.X, padx=12, pady=4)
        rend.columnconfigure(1, weight=1)
        rend.columnconfigure(3, weight=1)

        ttk.Label(rend, text="Render Slot 1").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.slot1_var = tk.StringVar(value="function")
        ttk.Combobox(rend, textvariable=self.slot1_var, state="readonly",
                     values=[label for _, label in _RENDER_LEVELS],
                     width=32).grid(row=0, column=1, sticky=tk.EW, padx=(6, 4))
        _info(rend, _TIP["slot_1"]).grid(row=0, column=4)

        ttk.Label(rend, text="Render Slot 2").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.slot2_var = tk.StringVar(value="script")
        ttk.Combobox(rend, textvariable=self.slot2_var, state="readonly",
                     values=[label for _, label in _RENDER_LEVELS],
                     width=32).grid(row=1, column=1, sticky=tk.EW, padx=(6, 4))
        _info(rend, _TIP["slot_2"]).grid(row=1, column=4)

        # Pre-select the recommended defaults.
        self.slot1_var.set(_RENDER_LEVELS[0][1])
        self.slot2_var.set(_RENDER_LEVELS[1][1])

        self.include_graph_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(rend, text="Build Include Graph mode (C/C++ #include relationships)",
                        variable=self.include_graph_var).grid(
            row=2, column=0, columnspan=4, sticky=tk.W, pady=(6, 0))
        _info(rend, _TIP["include_graph"]).grid(row=2, column=4)

        self.include_system_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(rend, text="    └─ Include system headers (#include <...>) in the graph",
                        variable=self.include_system_var).grid(
            row=3, column=0, columnspan=4, sticky=tk.W, pady=(2, 0))
        _info(rend, _TIP["include_system_headers"]).grid(row=3, column=4)

        # ── Advanced ──────────────────────────────────────────────────────
        adv = ttk.LabelFrame(root, text=" Advanced Options ", padding=10)
        adv.pack(fill=tk.X, padx=12, pady=4)
        adv.columnconfigure(1, weight=1)

        ttk.Label(adv, text="None of the settings below are mandatory — leave them blank to use defaults.",
                  foreground="gray", font=("", 9, "italic")).grid(
            row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 8))

        ttk.Label(adv, text="Config file").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.config_var = tk.StringVar()
        ttk.Entry(adv, textvariable=self.config_var).grid(
            row=1, column=1, sticky=tk.EW, padx=(6, 4))
        ttk.Button(adv, text="Browse…", width=8,
                   command=self._browse_config).grid(row=1, column=2, padx=(0, 4))
        _info(adv, _TIP["config"]).grid(row=1, column=3)

        ttk.Label(adv, text="compile_commands.json").grid(row=2, column=0, sticky=tk.W, pady=3)
        self.cc_var = tk.StringVar()
        ttk.Entry(adv, textvariable=self.cc_var).grid(
            row=2, column=1, sticky=tk.EW, padx=(6, 4))
        ttk.Button(adv, text="Browse…", width=8,
                   command=self._browse_cc).grid(row=2, column=2, padx=(0, 4))
        _info(adv, _TIP["cc"]).grid(row=2, column=3)

        ttk.Label(adv, text="Entry points").grid(row=3, column=0, sticky=tk.W, pady=3)
        self.entry_var = tk.StringVar()
        ttk.Entry(adv, textvariable=self.entry_var).grid(
            row=3, column=1, sticky=tk.EW, padx=(6, 4))
        ttk.Label(adv, text="comma-separated", foreground="gray").grid(
            row=3, column=2, sticky=tk.W, padx=(0, 4))
        _info(adv, _TIP["entry"]).grid(row=3, column=3)

        ttk.Label(adv, text="Max depth").grid(row=4, column=0, sticky=tk.W, pady=3)
        self.depth_var = tk.StringVar()
        ttk.Entry(adv, textvariable=self.depth_var, width=8).grid(
            row=4, column=1, sticky=tk.W, padx=(6, 4))
        ttk.Label(adv, text="blank = unlimited", foreground="gray").grid(
            row=4, column=2, sticky=tk.W, padx=(0, 4))
        _info(adv, _TIP["depth"]).grid(row=4, column=3)

        ttk.Label(adv, text="Max nodes").grid(row=5, column=0, sticky=tk.W, pady=3)
        self.max_nodes_var = tk.StringVar()
        ttk.Entry(adv, textvariable=self.max_nodes_var, width=8).grid(
            row=5, column=1, sticky=tk.W, padx=(6, 4))
        ttk.Label(adv, text="blank = default (3000)", foreground="gray").grid(
            row=5, column=2, sticky=tk.W, padx=(0, 4))
        _info(adv, _TIP["max_nodes"]).grid(row=5, column=3)

        ttk.Label(adv, text="Parallel workers").grid(row=6, column=0, sticky=tk.W, pady=3)
        self.parallel_var = tk.StringVar()
        ttk.Entry(adv, textvariable=self.parallel_var, width=8).grid(
            row=6, column=1, sticky=tk.W, padx=(6, 4))
        ttk.Label(adv, text="blank = auto-detect", foreground="gray").grid(
            row=6, column=2, sticky=tk.W, padx=(0, 4))
        _info(adv, _TIP["parallel"]).grid(row=6, column=3)

        flags = ttk.Frame(adv)
        flags.grid(row=7, column=0, columnspan=4, sticky=tk.W, pady=(10, 2))

        self.external_var = tk.BooleanVar()
        ttk.Checkbutton(flags, text="Show external functions",
                        variable=self.external_var).pack(side=tk.LEFT)
        _info(flags, _TIP["external"]).pack(side=tk.LEFT, padx=(4, 20))

        self.summary_var = tk.BooleanVar()
        ttk.Checkbutton(flags, text="Summary by file",
                        variable=self.summary_var).pack(side=tk.LEFT)
        _info(flags, _TIP["summary"]).pack(side=tk.LEFT, padx=(4, 20))

        self.verbose_var = tk.BooleanVar()
        ttk.Checkbutton(flags, text="Verbose output",
                        variable=self.verbose_var).pack(side=tk.LEFT)
        _info(flags, _TIP["verbose"]).pack(side=tk.LEFT, padx=(4, 0))

        # ── Log ───────────────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(root, text=" Analysis Log ", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        prog_row = ttk.Frame(log_frame)
        prog_row.pack(fill=tk.X, pady=(0, 4))
        self.stage_label = ttk.Label(
            prog_row, text="Ready", foreground="#4a90d9",
            font=("Segoe UI", 9, "bold"),
        )
        self.stage_label.pack(side=tk.LEFT, padx=(2, 8))
        self.progress = ttk.Progressbar(
            prog_row, mode="determinate", maximum=5, value=0,
        )
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))

        self.log = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, height=12,
            font=("Consolas", 9), state=tk.DISABLED,
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white",
        )
        self.log.pack(fill=tk.BOTH, expand=True)

        bar = ttk.Frame(root)
        bar.pack(fill=tk.X, padx=12, pady=(4, 12))

        self.run_btn = ttk.Button(bar, text="Run Analysis", width=18,
                                  command=self._on_run)
        self.run_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.inspect_btn = ttk.Button(bar, text="Inspect Solution…", width=18,
                                      command=self._on_inspect, state=tk.DISABLED)
        self.inspect_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.open_btn = ttk.Button(bar, text="Open Output", width=14,
                                   command=self._on_open, state=tk.DISABLED)
        self.open_btn.pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(bar, textvariable=self.status_var,
                  foreground="gray").pack(side=tk.RIGHT)

    # ── Browse helpers ────────────────────────────────────────────────────────

    def _browse_project_dir(self) -> None:
        path = filedialog.askdirectory(title="Select Project Directory")
        if path:
            self.project_var.set(path)
            self._sln_selection = None
            self._folder_selection = None
            self.inspect_btn.config(state=tk.NORMAL, text="Inspect Folders…")
            # Auto-open folder inspect modal.
            self.root.after(100, self._on_inspect)

    def _browse_project_sln(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Visual Studio Solution File",
            filetypes=[("Solution files", "*.sln"), ("All files", "*.*")],
        )
        if path:
            self.project_var.set(path)
            self._sln_selection = None
            self._folder_selection = None
            self.inspect_btn.config(state=tk.NORMAL, text="Inspect Solution…")
            # Auto-open inspect modal.
            self.root.after(100, self._on_inspect)

    def _browse_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Select Output File or Base Name",
            defaultextension="",
            filetypes=[
                ("HTML file", "*.html"),
                ("DOT file", "*.dot"),
                ("SVG file", "*.svg"),
                ("PNG file", "*.png"),
                ("No extension — multi-format", "*.*"),
            ],
        )
        if path:
            self.output_var.set(path)

    def _browse_config(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Config File",
            filetypes=[
                ("YAML files", "*.yaml *.yml"),
                ("JSON files", "*.json"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.config_var.set(path)

    def _browse_cc(self) -> None:
        path = filedialog.askopenfilename(
            title="Select compile_commands.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.cc_var.set(path)

    # ── Inspect ───────────────────────────────────────────────────────────────

    def _on_inspect(self) -> None:
        project = self.project_var.get().strip()
        if not project:
            messagebox.showerror("Missing Input", "Please pick a project folder or .sln file first.")
            return
        if project.lower().endswith(".sln"):
            self._inspect_solution(project)
            return
        p = Path(project)
        if p.exists() and p.is_dir():
            self._inspect_folder(str(p))
            return
        messagebox.showerror("Invalid Path", "Please pick an existing project folder or .sln file.")

    def _inspect_solution(self, sln: str) -> None:
        self.status_var.set("Inspecting solution...")
        data = _run_inspect_sln(sln)
        self.status_var.set("Ready.")
        if data is None:
            messagebox.showerror(
                "Inspect Failed",
                "Could not inspect the solution. Run `callgraph_tool.py --inspect-sln` "
                "manually to see the underlying error."
            )
            return
        dlg = InspectSolutionDialog(self.root, sln, data)
        result = dlg.show()
        if result is None:
            self._log("[Inspect] Cancelled — full solution will be analyzed.\n")
            self._sln_selection = None
            self._folder_selection = None
        else:
            self._sln_selection = result
            self._folder_selection = None
            self._log(
                f"[Inspect] Selected {len(result['projects'])} project(s), "
                f"{len(result['included_files'])} included file(s), "
                f"{len(result['excluded_files'])} excluded file(s). "
                f"Configuration={result.get('configuration') or '(default)'}, "
                f"Platform={result.get('platform') or '(default)'}.\n"
            )

    def _inspect_folder(self, project_dir: str) -> None:
        subfolders = _list_subfolders(project_dir)
        if not subfolders:
            self._folder_selection = None
            messagebox.showinfo(
                "No Subfolders Found",
                "This folder has no subfolders to filter. The full folder will be analyzed.",
            )
            return

        dlg = InspectFolderDialog(self.root, project_dir, subfolders)
        result = dlg.show()
        if result is None:
            self._log("[Inspect] Cancelled — full folder will be analyzed.\n")
            self._folder_selection = None
            self._sln_selection = None
            return

        self._folder_selection = result
        self._sln_selection = None
        self._log(
            f"[Inspect] Selected {len(result.get('selected_folders', []))} folder(s) "
            f"({len(result.get('include_globs', []))} include glob(s)).\n"
        )

    # ── Run logic ─────────────────────────────────────────────────────────────

    def _slot_value(self, label: str) -> str:
        for key, lbl in _RENDER_LEVELS:
            if lbl == label:
                return key
        return "function"

    def _on_run(self) -> None:
        project = self.project_var.get().strip()
        output = self.output_var.get().strip()

        if not project:
            messagebox.showerror("Missing Input",
                                 "Select a project folder or .sln file.")
            return
        if not output:
            messagebox.showerror("Missing Input", "Specify an output path.")
            return

        formats = [fmt for fmt, var in self.format_vars.items() if var.get()]
        if not formats:
            messagebox.showerror("Missing Input",
                                 "Select at least one output format.")
            return

        depth = self.depth_var.get().strip()
        if depth and not depth.isdigit():
            messagebox.showerror("Invalid Input",
                                 "Max depth must be a positive integer or blank.")
            return

        max_nodes = self.max_nodes_var.get().strip()
        if max_nodes and (not max_nodes.isdigit() or int(max_nodes) < 1):
            messagebox.showerror("Invalid Input",
                                 "Max nodes must be a positive integer or blank.")
            return

        parallel = self.parallel_var.get().strip()
        if parallel and not parallel.isdigit():
            messagebox.showerror("Invalid Input",
                                 "Parallel workers must be a positive integer or blank.")
            return

        cmd = [_PYTHON, str(_TOOL),
               "--project", project,
               "--output", output,
               "--formats"] + formats

        config = self.config_var.get().strip()
        if config:
            cmd += ["--config", config]

        cc = self.cc_var.get().strip()
        if cc:
            cmd += ["--compile-commands", cc]

        entries = [e.strip() for e in self.entry_var.get().split(",") if e.strip()]
        if entries:
            cmd += ["--entry"] + entries

        if depth:
            cmd += ["--depth", depth]
        if max_nodes:
            cmd += ["--max-nodes", max_nodes]
        if parallel:
            cmd += ["--parallel", parallel]
        if self.external_var.get():
            cmd += ["--show-external"]
        if self.summary_var.get():
            cmd += ["--summary-by-file"]
        if self.verbose_var.get():
            cmd += ["--verbose"]
        if self.include_graph_var.get():
            cmd += ["--include-graph"]
        if self.include_system_var.get():
            cmd += ["--include-system-headers"]

        # Render slots
        slot1 = self._slot_value(self.slot1_var.get())
        slot2 = self._slot_value(self.slot2_var.get())
        cmd += ["--view-slot-1", slot1, "--view-slot-2", slot2]

        # Selection filters from inspect dialogs
        if project.lower().endswith(".sln"):
            sel = self._sln_selection
            if sel:
                if sel.get("projects"):
                    cmd += ["--include-projects"] + sel["projects"]
                if sel.get("configuration"):
                    cmd += ["--build-configuration", sel["configuration"]]
                if sel.get("platform"):
                    cmd += ["--build-platform", sel["platform"]]
                if sel.get("included_files") and len(sel.get("included_files")) < 2000:
                    # Convert absolute paths to globs relative to the .sln directory.
                    sln_dir = Path(project).parent
                    globs = []
                    for f in sel["included_files"]:
                        try:
                            rel = Path(f).resolve().relative_to(sln_dir).as_posix()
                            globs.append(rel)
                        except (ValueError, OSError):
                            globs.append("**/" + Path(f).name)
                    if globs:
                        cmd += ["--include-files"] + globs
        else:
            fsel = self._folder_selection
            if fsel:
                expected_root = str(Path(project).resolve())
                if fsel.get("project_dir") != expected_root:
                    self._folder_selection = None
                    fsel = None
                if fsel is not None:
                    globs = list(fsel.get("include_globs") or [])
                    if not globs:
                        messagebox.showerror(
                            "No Folders Selected",
                            "No subfolders are selected. Use 'Inspect Folders…' and "
                            "select at least one subfolder, or cancel inspect to analyze all.",
                        )
                        return
                    cmd += ["--include-files"] + globs

        self._output_files = []
        self.open_btn.config(state=tk.DISABLED)
        self._set_running(True)
        self._clear_log()
        self._log(f"$ {' '.join(cmd)}\n\n")

        threading.Thread(target=self._worker, args=(cmd,), daemon=True).start()

    def _worker(self, cmd: list[str]) -> None:
        output_files: list[str] = []
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for raw in proc.stdout:
                line = _strip_ansi(raw)
                self.root.after(0, self._log, line)
                stage = _STAGE_RE.match(line.rstrip("\n"))
                if stage:
                    cur = int(stage.group(1))
                    total = int(stage.group(2))
                    label = stage.group(3)
                    self.root.after(0, self._update_stage, cur, total, label)
                m = re.match(r'^\s+OK\s+(.+\S)\s*$', line)
                if m:
                    output_files.append(m.group(1).strip())
            proc.wait()
            success = proc.returncode == 0
        except Exception as exc:
            self.root.after(0, self._log, f"\nFailed to start process: {exc}\n")
            success = False

        if success:
            self._output_files = output_files
        self.root.after(0, self._finish, success)

    def _update_stage(self, cur: int, total: int, label: str) -> None:
        if self.progress["maximum"] != total:
            self.progress["maximum"] = total
        self.progress["value"] = cur
        self.stage_label.config(
            text=f"Stage {cur}/{total}: {label}",
            foreground="#4a90d9",
        )

    def _finish(self, success: bool) -> None:
        self._set_running(False)
        if success:
            self._log("\n[OK] Analysis complete.\n")
            self.status_var.set("Done.")
            try:
                self.progress["value"] = self.progress["maximum"]
            except Exception:
                pass
            self.stage_label.config(text="Done", foreground="#27ae60")
            if self._output_files:
                self.open_btn.config(state=tk.NORMAL)
        else:
            self._log("\n[FAIL] Analysis failed — see log above.\n")
            self.status_var.set("Failed.")
            self.stage_label.config(text="Failed", foreground="#e74c3c")

    def _set_running(self, running: bool) -> None:
        self.run_btn.config(
            state=tk.DISABLED if running else tk.NORMAL,
            text="Running…" if running else "Run Analysis",
        )
        if running:
            self.status_var.set("Running…")

    def _on_open(self) -> None:
        if not self._output_files:
            return
        target = next(
            (f for f in self._output_files if f.lower().endswith(".html")),
            self._output_files[0],
        )
        if not Path(target).exists():
            messagebox.showerror("File Not Found",
                                 f"Output file not found:\n{target}")
            return
        if sys.platform == "win32":
            os.startfile(target)
        elif sys.platform == "darwin":
            subprocess.run(["open", target], check=False)
        else:
            subprocess.run(["xdg-open", target], check=False)

    def _log(self, text: str) -> None:
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, text)
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self.log.config(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.config(state=tk.DISABLED)
        self.progress["value"] = 0
        self.stage_label.config(text="Starting…", foreground="#4a90d9")


def main() -> None:
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
