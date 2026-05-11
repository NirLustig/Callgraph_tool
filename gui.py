"""
gui.py — Tkinter GUI for CallGraph Tool.
Run: python gui.py

Exposes all CLI parameters through a graphical interface.
Delegates execution to callgraph_tool.py via subprocess so the full
pipeline runs unchanged and the CLI continues to work as before.
"""
from __future__ import annotations

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


def _strip_ansi(text: str) -> str:
    return _ANSI.sub('', text)


# ── Tooltip ───────────────────────────────────────────────────────────────────

class _Tooltip:
    """Hover popup attached to a widget."""

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
    """Blue circled-i badge that shows a tooltip on hover."""
    badge = tk.Label(
        parent, text=" i ", font=("Segoe UI", 8, "bold"),
        fg="white", bg="#0078d4", cursor="question_arrow",
        relief=tk.FLAT, padx=3, pady=1,
    )
    _Tooltip(badge, tip)
    return badge


# ── Tooltip text for every field ─────────────────────────────────────────────

_TIP = {
    "project": (
        "Required.\n\n"
        "Path to the source code folder you want to analyze,\n"
        "or a Visual Studio .sln solution file.\n\n"
        "• Folder… — browse for a directory\n"
        "• .sln…   — browse for a Visual Studio solution file"
    ),
    "output": (
        "Required.\n\n"
        "Where to save the generated graph.\n\n"
        "Two modes:\n"
        "• With extension  → one file only\n"
        "  e.g.  output/graph.html\n"
        "• Without extension → all selected formats\n"
        "  e.g.  output/graph  →  graph.html, graph.dot …"
    ),
    "formats": (
        "Required — select at least one format.\n\n"
        "• HTML  Interactive graph in your browser (no internet).\n"
        "• DOT   Graphviz source text file.\n"
        "• SVG   Vector image — requires Graphviz on PATH.\n"
        "• PNG   Raster image — requires Graphviz on PATH."
    ),
    "config": (
        "Optional.\n\n"
        "Path to a YAML or JSON config file that controls display\n"
        "options, file/function filters, layout style, and more.\n\n"
        "See config.example.yaml in the project folder for all\n"
        "available settings. Leave blank to use built-in defaults."
    ),
    "entry": (
        "Optional.\n\n"
        "One or more function names that serve as starting points\n"
        "for the graph. Only functions reachable from these entry\n"
        "points will appear.\n\n"
        "Enter multiple names separated by commas:\n"
        "  main, init, process_data\n\n"
        "Leave blank to include every function in the project."
    ),
    "depth": (
        "Optional.\n\n"
        "Maximum number of call hops to trace from each entry\n"
        "point. For example, depth 2 shows direct callees and\n"
        "their callees.\n\n"
        "Only meaningful when Entry points are set.\n"
        "Leave blank for unlimited depth."
    ),
    "external": (
        "Optional — off by default.\n\n"
        "When checked, calls to functions outside your project\n"
        "(standard library, third-party packages) appear as grey\n"
        "stub nodes in the graph.\n\n"
        "Useful for seeing how your code interacts with external\n"
        "libraries, but can make the graph larger."
    ),
    "verbose": (
        "Optional — off by default.\n\n"
        "When checked, the log shows detailed parsing information:\n"
        "every file scanned, all parse warnings, and call-resolution\n"
        "details.\n\n"
        "Helpful for debugging missing functions or unexpected\n"
        "graph structure."
    ),
}


# ── Main application ──────────────────────────────────────────────────────────

class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("CallGraph Tool")
        self.root.minsize(720, 660)
        self._output_files: list[str] = []
        self._build()

    def _build(self) -> None:
        root = self.root

        # ── Required: Project, Output, Formats ───────────────────────────────
        req = ttk.LabelFrame(root, text=" Project & Output ", padding=10)
        req.pack(fill=tk.X, padx=12, pady=(12, 4))
        req.columnconfigure(1, weight=1)

        # Column guide: 0=label  1=entry(stretch)  2=btn1  3=btn2  4=[i]

        # Project *
        ttk.Label(req, text="Project *").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.project_var = tk.StringVar()
        ttk.Entry(req, textvariable=self.project_var).grid(
            row=0, column=1, sticky=tk.EW, padx=(6, 4))
        ttk.Button(req, text="Folder…", width=8,
                   command=self._browse_project_dir).grid(row=0, column=2, padx=(0, 2))
        ttk.Button(req, text=".sln…", width=7,
                   command=self._browse_project_sln).grid(row=0, column=3, padx=(0, 4))
        _info(req, _TIP["project"]).grid(row=0, column=4)

        # Output *
        ttk.Label(req, text="Output *").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.output_var = tk.StringVar()
        ttk.Entry(req, textvariable=self.output_var).grid(
            row=1, column=1, sticky=tk.EW, padx=(6, 4))
        ttk.Button(req, text="Browse…", width=8,
                   command=self._browse_output).grid(
            row=1, column=2, columnspan=2, sticky=tk.W, padx=(0, 4))
        _info(req, _TIP["output"]).grid(row=1, column=4)

        ttk.Label(req,
                  text="Tip: omit the file extension (e.g. output/graph) to generate all selected formats at once.",
                  foreground="gray", font=("", 8)).grid(
            row=2, column=1, columnspan=3, sticky=tk.W, padx=(6, 0), pady=(0, 4))

        # Formats *
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

        ttk.Label(req, text="SVG and PNG require the Graphviz binary installed on your system PATH.",
                  foreground="gray", font=("", 8)).grid(
            row=4, column=1, columnspan=3, sticky=tk.W, padx=(6, 0), pady=(0, 2))

        # ── Advanced Options (all optional) ──────────────────────────────────
        adv = ttk.LabelFrame(root, text=" Advanced Options ", padding=10)
        adv.pack(fill=tk.X, padx=12, pady=4)
        adv.columnconfigure(1, weight=1)

        # Column guide: 0=label  1=entry(stretch)  2=btn or hint  3=[i]

        ttk.Label(adv, text="None of the settings below are mandatory — leave them blank to use defaults.",
                  foreground="gray", font=("", 9, "italic")).grid(
            row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 8))

        # Config file
        ttk.Label(adv, text="Config file").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.config_var = tk.StringVar()
        ttk.Entry(adv, textvariable=self.config_var).grid(
            row=1, column=1, sticky=tk.EW, padx=(6, 4))
        ttk.Button(adv, text="Browse…", width=8,
                   command=self._browse_config).grid(row=1, column=2, padx=(0, 4))
        _info(adv, _TIP["config"]).grid(row=1, column=3)

        # Entry points
        ttk.Label(adv, text="Entry points").grid(row=2, column=0, sticky=tk.W, pady=3)
        self.entry_var = tk.StringVar()
        ttk.Entry(adv, textvariable=self.entry_var).grid(
            row=2, column=1, sticky=tk.EW, padx=(6, 4))
        ttk.Label(adv, text="comma-separated", foreground="gray").grid(
            row=2, column=2, sticky=tk.W, padx=(0, 4))
        _info(adv, _TIP["entry"]).grid(row=2, column=3)

        # Max depth
        ttk.Label(adv, text="Max depth").grid(row=3, column=0, sticky=tk.W, pady=3)
        self.depth_var = tk.StringVar()
        ttk.Entry(adv, textvariable=self.depth_var, width=8).grid(
            row=3, column=1, sticky=tk.W, padx=(6, 4))
        ttk.Label(adv, text="blank = unlimited", foreground="gray").grid(
            row=3, column=2, sticky=tk.W, padx=(0, 4))
        _info(adv, _TIP["depth"]).grid(row=3, column=3)

        # Flag checkboxes
        flags = ttk.Frame(adv)
        flags.grid(row=4, column=0, columnspan=4, sticky=tk.W, pady=(10, 2))

        self.external_var = tk.BooleanVar()
        ttk.Checkbutton(flags, text="Show external functions",
                        variable=self.external_var).pack(side=tk.LEFT)
        _info(flags, _TIP["external"]).pack(side=tk.LEFT, padx=(4, 20))

        self.verbose_var = tk.BooleanVar()
        ttk.Checkbutton(flags, text="Verbose output",
                        variable=self.verbose_var).pack(side=tk.LEFT)
        _info(flags, _TIP["verbose"]).pack(side=tk.LEFT, padx=(4, 0))

        # ── Analysis log ──────────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(root, text=" Analysis Log ", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        self.log = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, height=12,
            font=("Consolas", 9), state=tk.DISABLED,
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white",
        )
        self.log.pack(fill=tk.BOTH, expand=True)

        # ── Action bar ────────────────────────────────────────────────────────
        bar = ttk.Frame(root)
        bar.pack(fill=tk.X, padx=12, pady=(4, 12))

        self.run_btn = ttk.Button(bar, text="Run Analysis", width=18,
                                  command=self._on_run)
        self.run_btn.pack(side=tk.LEFT, padx=(0, 8))

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

    def _browse_project_sln(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Visual Studio Solution File",
            filetypes=[("Solution files", "*.sln"), ("All files", "*.*")],
        )
        if path:
            self.project_var.set(path)

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

    # ── Run logic ─────────────────────────────────────────────────────────────

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

        cmd = [_PYTHON, str(_TOOL),
               "--project", project,
               "--output", output,
               "--formats"] + formats

        config = self.config_var.get().strip()
        if config:
            cmd += ["--config", config]

        entries = [e.strip() for e in self.entry_var.get().split(",") if e.strip()]
        if entries:
            cmd += ["--entry"] + entries

        if depth:
            cmd += ["--depth", depth]
        if self.external_var.get():
            cmd += ["--show-external"]
        if self.verbose_var.get():
            cmd += ["--verbose"]

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

    def _finish(self, success: bool) -> None:
        self._set_running(False)
        if success:
            self._log("\n✓ Analysis complete.\n")
            self.status_var.set("Done.")
            if self._output_files:
                self.open_btn.config(state=tk.NORMAL)
        else:
            self._log("\n✗ Analysis failed — see log above.\n")
            self.status_var.set("Failed.")

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

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _log(self, text: str) -> None:
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, text)
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self.log.config(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.config(state=tk.DISABLED)


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
