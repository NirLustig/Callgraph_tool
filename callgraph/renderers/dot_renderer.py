"""
Graphviz DOT/SVG/PNG renderer.
Groups functions into subgraph clusters by source file.
Falls back gracefully to .dot if the Graphviz binary is not installed.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Optional

from ..config import Config
from ..models import CallGraph, FunctionDef, Language, ResolutionConfidence
from .base import BaseRenderer
from .html_renderer import _build_node_label  # reuse label logic

try:
    import graphviz
    _GV_AVAILABLE = True
except ImportError:
    _GV_AVAILABLE = False


# Common installation directories for Graphviz on Windows
_GRAPHVIZ_WIN_DIRS = [
    r"C:\Program Files\Graphviz\bin",
    r"C:\Program Files (x86)\Graphviz\bin",
    r"C:\Graphviz\bin",
    r"C:\tools\graphviz\bin",
    r"C:\ProgramData\chocolatey\lib\graphviz.portable\tools\bin",
    r"C:\ProgramData\chocolatey\lib\Graphviz\tools\bin",
]


def _ensure_graphviz_on_path() -> bool:
    """
    Check if `dot` is accessible. If not, search common Windows install
    locations and temporarily prepend the found directory to PATH.
    Returns True if dot is (now) accessible, False otherwise.
    """
    if shutil.which("dot"):
        return True

    # Also check GRAPHVIZ_DOT env var (explicit override)
    explicit = os.environ.get("GRAPHVIZ_DOT")
    if explicit and Path(explicit).is_file():
        bin_dir = str(Path(explicit).parent)
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        return True

    # Search well-known Windows locations
    for candidate_dir in _GRAPHVIZ_WIN_DIRS:
        dot_path = Path(candidate_dir) / "dot.exe"
        if dot_path.is_file():
            os.environ["PATH"] = candidate_dir + os.pathsep + os.environ.get("PATH", "")
            return True

    # Last resort: scan Program Files for a dot.exe up to 2 levels deep
    for root in (r"C:\Program Files", r"C:\Program Files (x86)"):
        root_p = Path(root)
        if not root_p.exists():
            continue
        for dot_path in root_p.glob("*/bin/dot.exe"):
            bin_dir = str(dot_path.parent)
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            return True
        for dot_path in root_p.glob("Graphviz*/bin/dot.exe"):
            bin_dir = str(dot_path.parent)
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            return True

    return False


LANG_DOT_COLORS: dict[Language, dict[str, str]] = {
    Language.PYTHON: {"fillcolor": "#4A90D9", "fontcolor": "white"},
    Language.C:      {"fillcolor": "#E8832A", "fontcolor": "white"},
    Language.CPP:    {"fillcolor": "#27AE60", "fontcolor": "white"},
    Language.MATLAB: {"fillcolor": "#8E44AD", "fontcolor": "white"},
}
EXTERNAL_DOT = {"fillcolor": "#95A5A6", "fontcolor": "white"}
VAR_DOT       = {"fillcolor": "#F0E68C", "fontcolor": "#333333", "shape": "diamond"}


def _safe_id(text: str) -> str:
    """Convert a node_id to a valid DOT identifier."""
    return re.sub(r"[^A-Za-z0-9_]", "_", text)


def _dot_label(fn: FunctionDef, cfg: Config) -> str:
    label = _build_node_label(fn, cfg)
    return label.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class DotRenderer(BaseRenderer):
    def __init__(self, config: Config, output_format: str = "dot") -> None:
        super().__init__(config)
        self.output_format = output_format  # "dot" | "svg" | "png"

    def render(self, graph: CallGraph, output_path: Path) -> Path:
        dot_source = self._build_dot(graph)

        suffix = f".{self.output_format}"
        out = output_path.with_suffix(suffix)
        out.parent.mkdir(parents=True, exist_ok=True)

        if self.output_format == "dot":
            out.write_text(dot_source, encoding="utf-8")
            return out

        # Need Graphviz binary for svg/png
        if not _GV_AVAILABLE:
            dot_fallback = output_path.with_suffix(".dot")
            dot_fallback.write_text(dot_source, encoding="utf-8")
            from rich.console import Console
            Console().print(
                f"[yellow]graphviz Python package not installed — wrote .dot file instead: {dot_fallback}[/yellow]"
            )
            return dot_fallback

        # Locate the Graphviz binary (searches common Windows paths if not on PATH)
        dot_available = _ensure_graphviz_on_path()

        if not dot_available:
            dot_fallback = output_path.with_suffix(".dot")
            dot_fallback.write_text(dot_source, encoding="utf-8")
            from rich.console import Console
            Console().print(
                "[yellow]Graphviz binary (dot.exe) not found. "
                "Install it: winget install graphviz  (or choco install graphviz, or apt/brew). "
                "After installing, restart your terminal so PATH is updated. "
                f"Wrote .dot file: {dot_fallback}[/yellow]"
            )
            return dot_fallback

        try:
            src = graphviz.Source(dot_source)
            rendered_path = src.render(
                filename=str(output_path.with_suffix("")),
                format=self.output_format,
                cleanup=True,
            )
            return Path(rendered_path)
        except Exception as exc:
            dot_fallback = output_path.with_suffix(".dot")
            dot_fallback.write_text(dot_source, encoding="utf-8")
            from rich.console import Console
            Console().print(
                f"[yellow]Graphviz render failed ({exc}). "
                f"Wrote .dot file: {dot_fallback}[/yellow]"
            )
            return dot_fallback

    def _build_dot(self, graph: CallGraph) -> str:
        """Build the complete DOT source string."""
        lines = [
            "digraph callgraph {",
            '  graph [rankdir=LR fontname="Helvetica" bgcolor="#1a1d23" splines=ortho];',
            '  node  [style=filled shape=box fontname="Courier" fontsize=9 margin="0.2,0.1"];',
            '  edge  [color="#6c8ebf" arrowsize=0.7 fontname="Helvetica" fontsize=8];',
            "",
        ]

        # Group nodes by source file
        files: dict[str, list[str]] = {}  # file_path → [node_ids]
        for node_id, fn in graph.functions.items():
            fp = fn.file_path if fn.file_path != "<external>" else "<external>"
            files.setdefault(fp, []).append(node_id)

        cluster_idx = 0
        for file_path, node_ids in sorted(files.items()):
            file_label = Path(file_path).name if file_path != "<external>" else "external"
            lines.append(f'  subgraph cluster_{cluster_idx} {{')
            lines.append(f'    label="{file_label}";')
            lines.append('    style=filled; fillcolor="#23272e"; color="#3d4451";')
            lines.append('    fontcolor="#8090a0"; fontname="Helvetica"; fontsize=10;')

            for node_id in node_ids:
                fn = graph.functions[node_id]
                safe = _safe_id(node_id)
                label = _dot_label(fn, self.config)

                if fn.is_external:
                    attrs = EXTERNAL_DOT
                else:
                    attrs = LANG_DOT_COLORS.get(fn.language, LANG_DOT_COLORS[Language.PYTHON])

                attr_str = " ".join(f'{k}="{v}"' for k, v in attrs.items())
                lines.append(f'    {safe} [label="{label}" {attr_str}];')

                # Variable annotation nodes
                if self.config.variables.track:
                    for var_name, var_value in fn.tracked_vars.items():
                        ann_id = _safe_id(f"__var__{node_id}__{var_name}")
                        ann_label = f"{var_name} = {var_value[:30]}"
                        lines.append(
                            f'    {ann_id} [label="{ann_label}" '
                            f'shape=diamond fillcolor="#F0E68C" fontcolor="#333"];'
                        )
                        lines.append(f'    {safe} -> {ann_id} [style=dashed arrowhead=none color="#C8B400"];')

            lines.append("  }")
            lines.append("")
            cluster_idx += 1

        # Edges
        for call in graph.calls:
            if not call.callee_id or call.callee_id not in graph.functions:
                continue
            caller_safe = _safe_id(call.caller_id)
            callee_safe = _safe_id(call.callee_id)
            is_heuristic = (call.resolution_confidence == ResolutionConfidence.HEURISTIC)
            style = 'style=dashed color="#aaaaaa"' if is_heuristic else 'color="#6c8ebf"'
            edge_label = f'label=":{ call.call_line}"' if self.config.display.show_line_numbers else ""
            lines.append(f"  {caller_safe} -> {callee_safe} [{style} {edge_label}];")

        lines.append("}")
        return "\n".join(lines)
