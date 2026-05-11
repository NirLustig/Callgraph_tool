"""
Renderer factory and dispatch.
"""
from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..models import CallGraph


def render_graph(graph: CallGraph, output_path: Path, config: Config) -> list[Path]:
    """
    Render graph in all requested formats.
    Returns list of output file paths created.
    """
    output_files: list[Path] = []
    formats = config.output.formats

    for fmt in formats:
        if fmt == "html":
            from .html_renderer import HtmlRenderer
            renderer = HtmlRenderer(config)
        elif fmt in ("svg", "png", "dot"):
            from .dot_renderer import DotRenderer
            renderer = DotRenderer(config, output_format=fmt)
        else:
            raise ValueError(f"Unknown output format: {fmt!r}")

        out = renderer.render(graph, output_path)
        output_files.append(out)

    return output_files
