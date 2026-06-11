"""
Renderer factory and dispatch.
"""
from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..models import CallGraph


def render_graph(
    graph: CallGraph,
    output_path: Path,
    config: Config,
    secondary_graph: CallGraph | None = None,
    slot1_graph: CallGraph | None = None,
    slot2_graph: CallGraph | None = None,
) -> list[Path]:
    """
    Render graph in all requested formats.

    Returns list of output file paths created.

    For the HTML renderer:
      - ``graph`` is always the function-level graph (drives the vis.js network).
      - ``slot1_graph`` / ``slot2_graph`` are the per-slot aggregated graphs that
        feed the slot-1 / slot-2 view. When None, the slot falls back to
        ``graph`` (function level).
      - ``secondary_graph`` is the legacy slot-2 alias kept for callers that
        haven't migrated.
    """
    output_files: list[Path] = []
    formats = config.output.formats

    for fmt in formats:
        if fmt == "html":
            from .html_renderer import HtmlRenderer
            renderer = HtmlRenderer(config)
            if slot1_graph is not None:
                renderer.slot1_graph = slot1_graph
            if slot2_graph is not None:
                renderer.slot2_graph = slot2_graph
            elif secondary_graph is not None:
                renderer.secondary_graph = secondary_graph
        elif fmt in ("svg", "png", "dot"):
            from .dot_renderer import DotRenderer
            renderer = DotRenderer(config, output_format=fmt)
        else:
            raise ValueError(f"Unknown output format: {fmt!r}")

        out = renderer.render(graph, output_path)
        output_files.append(out)

    return output_files
