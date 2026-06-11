"""
Command-line interface. Orchestrates the full pipeline.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

from . import __version__
from . import architecture as arch_mod
from . import build_info as build_info_mod
from . import include_graph as include_graph_mod
from .aggregator import aggregate
from .config import load_config, Config
from .discovery import apply_selection, discover_files, summary
from .graph.builder import build_call_graph, collapse_to_files
from .models import Language, RenderLevel
from .parsers import get_parser
from .renderers import render_graph
from .sln_inspect import inspect_sln_to_json
from .sln_reader import discover_from_sln

console = Console()


_RENDER_LEVELS = ("function", "script", "folder", "module", "library", "namespace")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="callgraph_tool",
        description="Offline function call graph analyzer for C, C++, Python, and MATLAB projects.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python callgraph_tool.py --project ./my_project --output graph.html
  python callgraph_tool.py --project ./my_project --config config.yaml --output graph.html
  python callgraph_tool.py --project ./my_project --entry main --depth 3 --output graph.html
  python callgraph_tool.py --project ./my_project --output graph --formats html svg
  python callgraph_tool.py --project ./src --view-slot-1 module --view-slot-2 folder --output arch
  python callgraph_tool.py --project ./src --include-graph --output graph
  python callgraph_tool.py --project ./My.sln --include-projects MyApp MyLib --output graph
        """,
    )
    parser.add_argument(
        "--project", "-p",
        required=True,
        metavar="DIR_OR_SLN",
        help="Path to the project directory to analyze, or a Visual Studio .sln file",
    )
    parser.add_argument(
        "--output", "-o",
        required=False,
        metavar="PATH",
        help="Output path (without extension for multi-format, with extension for single)",
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        metavar="FILE",
        help="Path to YAML or JSON config file",
    )
    parser.add_argument(
        "--formats", "-f",
        nargs="+",
        choices=["html", "svg", "png", "dot"],
        metavar="FORMAT",
        help="Output format(s): html, svg, png, dot (overrides config)",
    )
    parser.add_argument(
        "--entry", "-e",
        nargs="+",
        metavar="FUNC",
        help="Entry point function name(s) for depth-limited graphs (overrides config)",
    )
    parser.add_argument(
        "--depth", "-d",
        type=int,
        default=None,
        metavar="N",
        help="Maximum call depth from entry points (overrides config)",
    )
    parser.add_argument(
        "--show-external",
        action="store_true",
        default=None,
        help="Include calls to external/library functions (overrides config)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=None,
        metavar="N",
        help="Parser worker threads (overrides config; default: auto-detect, set to 1 to force serial)",
    )
    parser.add_argument(
        "--max-nodes",
        type=int,
        default=None,
        metavar="N",
        help="Maximum nodes in the final graph (overrides config; default 3000)",
    )
    parser.add_argument(
        "--summary-by-file",
        action="store_true",
        default=None,
        help="Render one node per source file instead of one per function (use for very large .sln projects)",
    )
    parser.add_argument(
        "--compile-commands",
        default=None,
        metavar="PATH",
        help="Path to compile_commands.json (overrides auto-detect)",
    )
    parser.add_argument(
        "--view-slot-1",
        choices=_RENDER_LEVELS,
        default=None,
        help="Render level for the first HTML view button (default: function)",
    )
    parser.add_argument(
        "--view-slot-2",
        choices=_RENDER_LEVELS,
        default=None,
        help="Render level for the second HTML view button (default: script)",
    )
    parser.add_argument(
        "--render-level",
        choices=_RENDER_LEVELS,
        default=None,
        help="Single render level (used for DOT/SVG/PNG output)",
    )
    parser.add_argument(
        "--include-graph",
        action="store_true",
        default=None,
        help="Build and render the Include Graph mode (C/C++ #include relationships)",
    )
    parser.add_argument(
        "--include-system-headers",
        action="store_true",
        default=None,
        help="Show system includes (#include <...>) as nodes in the Include Graph",
    )
    parser.add_argument(
        "--include-projects",
        nargs="+",
        default=None,
        metavar="NAME",
        help="Restrict .sln analysis to these project names (substring match)",
    )
    parser.add_argument(
        "--include-modules",
        nargs="+",
        default=None,
        metavar="NAME",
        help="Restrict analysis to these module names (see architecture.modules)",
    )
    parser.add_argument(
        "--include-files",
        nargs="+",
        default=None,
        metavar="GLOB",
        help="Restrict analysis to files matching these glob patterns (relative to project root)",
    )
    parser.add_argument(
        "--build-configuration",
        default=None,
        help="Active .sln build configuration (e.g. Debug, Release) for include-paths/defines",
    )
    parser.add_argument(
        "--build-platform",
        default=None,
        help="Active .sln build platform (e.g. x64, Win32) for include-paths/defines",
    )
    parser.add_argument(
        "--architecture-report",
        default=None,
        metavar="PATH",
        help="Write the architecture violations report to this JSON path",
    )
    parser.add_argument(
        "--inspect-sln",
        action="store_true",
        default=False,
        help="Print solution structure as JSON and exit (used by the GUI)",
    )
    parser.add_argument(
        "--version", "-v",
        action="version",
        version=f"callgraph_tool {__version__}",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed parse information",
    )
    return parser.parse_args(argv)


def _apply_cli_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    if args.formats:
        cfg.output.formats = args.formats
    if args.entry:
        cfg.filter.entry_points = args.entry
    if args.depth is not None:
        cfg.filter.max_depth = args.depth
    if args.show_external:
        cfg.filter.show_external = True
    if args.parallel is not None:
        cfg.output.parallel = args.parallel
    if args.max_nodes is not None:
        cfg.output.max_nodes = args.max_nodes
    if args.summary_by_file:
        cfg.output.summary_by_file = True
    if args.compile_commands is not None:
        cfg.build.compile_commands = args.compile_commands
    if args.view_slot_1 is not None:
        cfg.render.view_slot_1 = args.view_slot_1
    if args.view_slot_2 is not None:
        cfg.render.view_slot_2 = args.view_slot_2
    if args.render_level is not None:
        cfg.render.render_level = args.render_level
    if args.include_graph:
        cfg.include_graph.enabled = True
    if getattr(args, 'include_system_headers', None):
        cfg.include_graph.follow_system = True
    if args.include_projects:
        cfg.selection.projects = list(args.include_projects)
    if args.include_modules:
        cfg.selection.modules = list(args.include_modules)
    if args.include_files:
        cfg.selection.files = list(args.include_files)
    if args.build_configuration is not None:
        cfg.build.configuration = args.build_configuration
    if args.build_platform is not None:
        cfg.build.platform = args.build_platform
    if args.architecture_report is not None:
        cfg.architecture.report = args.architecture_report
    return cfg


def _stage_begin(n: int, total: int, label: str) -> float:
    """
    Print a `[stage N/M] LABEL...` marker (parsed by gui.py for the progress bar)
    and return a perf_counter timestamp for the caller to use when ending the stage.
    """
    console.print(f"[bold cyan]\\[stage {n}/{total}][/bold cyan] {label}...")
    return time.perf_counter()


def _stage_end(t0: float) -> None:
    console.print(f"  [dim]done in {time.perf_counter() - t0:.2f}s[/dim]")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # ---- Special non-pipeline mode: --inspect-sln --------------------------
    if args.inspect_sln:
        sln_path = Path(args.project)
        if not sln_path.exists() or sln_path.suffix.lower() != ".sln":
            console.print(f"[bold red]Error:[/bold red] --inspect-sln requires a .sln path, got {args.project}")
            return 1
        cfg = load_config(args.config)
        cfg = _apply_cli_overrides(cfg, args)
        try:
            print(inspect_sln_to_json(sln_path, cfg))
        except Exception as exc:
            console.print(f"[bold red]Inspect error:[/bold red] {exc}")
            return 1
        return 0

    console.print(Panel.fit(
        f"[bold cyan]CallGraph Tool[/bold cyan] [dim]v{__version__}[/dim]",
        border_style="cyan",
    ))

    if not args.output:
        console.print("[bold red]Error:[/bold red] --output is required (omit only with --inspect-sln)")
        return 1

    # Stage planning: 1 config, 2 discover, 3 parse, 4 build, 5 [aggregate/extras], 6 render
    _will_summarize = bool(args.summary_by_file)
    _total_stages = 6 if _will_summarize else 5

    # ---- stage 1: config ---------------------------------------------------
    t0 = _stage_begin(1, _total_stages, "Loading config")
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[bold red]Config error:[/bold red] {exc}")
        return 1
    _stage_end(t0)

    cfg = _apply_cli_overrides(cfg, args)
    _will_summarize = bool(cfg.output.summary_by_file)
    _total_stages = 6 if _will_summarize else 5

    # ---- stage 2: discover -------------------------------------------------
    project_path = Path(args.project).resolve()
    is_sln = project_path.suffix.lower() == ".sln"
    project_root = project_path.parent if is_sln else project_path

    t0 = _stage_begin(2, _total_stages,
                      "Reading solution" if is_sln else "Discovering source files")
    sln_metadata = None
    if is_sln:
        if not project_path.exists():
            console.print(f"[bold red]Error:[/bold red] Solution file not found: {args.project}")
            return 1
        try:
            sln_result = discover_from_sln(project_path, cfg)
        except FileNotFoundError as exc:
            console.print(f"[bold red]Solution error:[/bold red] {exc}")
            return 1
        sln_metadata = {
            "active_configuration": cfg.build.configuration or (sln_result.configurations[0] if sln_result.configurations else None),
            "active_platform": cfg.build.platform or (sln_result.platforms[0] if sln_result.platforms else None),
            "projects": sln_result.project_metadata,
        }
        console.print(
            f"  [cyan]Detected Visual Studio solution.[/cyan] "
            f"Found [green]{sln_result.project_count}[/green] project(s) and "
            f"[green]{sln_result.file_count}[/green] source/header file(s)."
        )
        shown = sln_result.warnings if args.verbose else sln_result.warnings[:3]
        for warn in shown:
            console.print(f"  [yellow]Warning:[/yellow] {warn}")
        remainder = len(sln_result.warnings) - len(shown)
        if remainder > 0:
            console.print(f"  [dim]({remainder} more warning(s) — use --verbose to see all)[/dim]")
        files = apply_selection(sln_result.files, cfg, project_path.parent)
        # For include graph: use ALL project files (pre file-selection) so the graph
        # is never truncated by the call-graph's --include-files filter.
        _all_files_for_ig = sln_result.files
    else:
        try:
            files = discover_files(args.project, cfg)
        except (FileNotFoundError, NotADirectoryError) as exc:
            console.print(f"[bold red]Discovery error:[/bold red] {exc}")
            return 1
        # For include graph: re-discover without SelectionConfig file/folder globs
        # (only exclude_dirs / exclude_files filters from cfg.filter apply).
        try:
            from .discovery import _walk_files, EXTENSION_MAP, infer_header_language
            _root = Path(args.project).resolve()
            _all_files_for_ig = {lang: [] for lang in Language}
            for _p in _walk_files(_root, _root, cfg.filter):
                _ext = _p.suffix.lower()
                if _ext not in EXTENSION_MAP:
                    continue
                _lang = EXTENSION_MAP[_ext]
                if _ext == ".h":
                    _lang = infer_header_language(_p)
                _all_files_for_ig[_lang].append(_p)
        except Exception:
            _all_files_for_ig = files  # fallback
    console.print(summary(files))
    _stage_end(t0)

    total_files = sum(len(v) for v in files.values())
    if total_files == 0:
        console.print("[yellow]No source files found. Check --project path and config filters.[/yellow]")
        return 1

    # ---- compile_commands.json + BuildInfo ---------------------------------
    cc_units, cc_warnings = {}, []
    cc_path = None
    if cfg.build.compile_commands:
        cc_path = Path(cfg.build.compile_commands)
        if not cc_path.exists():
            console.print(f"[yellow]Warning:[/yellow] compile_commands path not found: {cc_path}")
            cc_path = None
    elif cfg.build.auto_detect:
        cc_path = build_info_mod.find_compile_commands(project_root)
    if cc_path is not None:
        cc_units, cc_warnings = build_info_mod.load_compile_commands(cc_path)
        for w in cc_warnings if args.verbose else cc_warnings[:3]:
            console.print(f"  [yellow]CC warning:[/yellow] {w}")
        console.print(
            f"  [cyan]compile_commands.json[/cyan] loaded: "
            f"[green]{len(cc_units)}[/green] unit(s) from [dim]{cc_path}[/dim]"
        )

    flat_files = [p for paths in files.values() for p in paths]
    build_info = build_info_mod.cross_reference(
        flat_files, cc_units, project_root, cc_path, sln_metadata, cfg
    )

    # ---- stage 3: parse ----------------------------------------------------
    t0 = _stage_begin(3, _total_stages, f"Parsing {total_files} source file(s)")
    all_functions = []
    all_calls = []
    parse_errors = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Parsing source files...", total=total_files)
        for lang, paths in files.items():
            if not paths:
                continue
            parser = get_parser(lang, cfg)
            fns, calls, errors = parser.parse_files(paths, progress_task=(progress, task))
            all_functions.extend(fns)
            all_calls.extend(calls)
            parse_errors.extend(errors)

    console.print(f"  Parsed [green]{len(all_functions)}[/green] functions, "
                  f"[green]{len(all_calls)}[/green] raw calls, "
                  f"[{'red' if parse_errors else 'green'}]{len(parse_errors)}[/{'red' if parse_errors else 'green'}] error(s)")
    if parse_errors and args.verbose:
        for err in parse_errors:
            console.print(f"  [dim red]Parse error:[/dim red] {err}")
    _stage_end(t0)

    # ---- stage 4: build call graph -----------------------------------------
    t0 = _stage_begin(4, _total_stages,
                      "Building call graph (dedup + resolve + filter + cap)")
    graph = build_call_graph(all_functions, all_calls, parse_errors, cfg)
    graph.build_info = build_info
    graph.project_root = str(project_root.resolve())
    _stage_end(t0)

    # ---- module inference + architecture validation ------------------------
    modules = arch_mod.build_modules(
        [fn.file_path for fn in graph.functions.values()
         if not fn.is_external and fn.file_path != "<external>"],
        cfg.architecture.modules,
        build_info=build_info,
        project_root=project_root,
    )
    graph.modules = modules

    # Apply module-level selection (post-build, before render).
    if cfg.selection.modules:
        keep_modules = {m for m in cfg.selection.modules}
        # Keep only functions whose file belongs to a selected module
        keep_files: set[str] = set()
        for name, mod in modules.items():
            if name in keep_modules:
                keep_files |= mod.files
        if keep_files:
            graph.functions = {
                nid: fn for nid, fn in graph.functions.items()
                if fn.file_path in keep_files or fn.is_external
            }
            keep_ids = set(graph.functions.keys())
            graph.calls = [c for c in graph.calls if c.caller_id in keep_ids]

    if cfg.architecture.rules:
        graph.violations = arch_mod.validate(graph, cfg.architecture.rules, modules)

    if cfg.architecture.report:
        try:
            _write_violations_report(cfg.architecture.report, graph)
            console.print(f"  [cyan]Architecture report:[/cyan] {cfg.architecture.report}")
        except OSError as exc:
            console.print(f"[yellow]Warning:[/yellow] Cannot write architecture report: {exc}")

    # ---- include graph -----------------------------------------------------
    if cfg.include_graph.enabled:
        # Use all project files (not the call-graph selection) so the include
        # graph always shows the full header dependency picture.
        c_files = []
        for lang, paths in _all_files_for_ig.items():
            if lang in (Language.C, Language.CPP):
                c_files.extend(paths)
        if c_files:
            try:
                graph.include_graph = include_graph_mod.build_include_graph(
                    c_files,
                    project_root=project_root,
                    build_info=build_info,
                    follow_system=cfg.include_graph.follow_system,
                )
                console.print(
                    f"  [cyan]Include graph:[/cyan] "
                    f"{len(graph.include_graph.files)} file(s), "
                    f"{len(graph.include_graph.unresolved)} unresolved, "
                    f"{len(graph.include_graph.cycles)} cycle(s)."
                )
            except Exception as exc:
                console.print(f"[yellow]Warning:[/yellow] include graph build failed: {exc}")

    stats = graph.stats()
    _print_stats(stats, total_files)

    if stats["functions"] == 0:
        console.print("[yellow]No functions found after filtering. Try relaxing your config filters.[/yellow]")
        return 1

    if stats["functions"] >= cfg.output.max_nodes:
        console.print(
            f"[yellow]Warning:[/yellow] {stats['functions']} functions hit the cap "
            f"({cfg.output.max_nodes}).\n"
            f"  Recommended: re-run with"
            f" [cyan]--max-nodes 20000 --summary-by-file --parallel 8[/cyan]"
            f" for a full-project view,\n"
            f"  or [cyan]--entry <func_name> --depth 5[/cyan] for a focused slice."
        )
    if stats["functions"] >= 8000 and not cfg.output.summary_by_file:
        console.print(
            f"[bold yellow]Heads up:[/bold yellow] Function Nodes view is banner-blocked above 8000 nodes "
            f"because vis.js becomes unusable at this size. Script Nodes view will be served on open. "
            f"For a much smaller HTML, re-run with [cyan]--summary-by-file[/cyan]."
        )
    elif stats["functions"] > 500:
        console.print(
            f"[yellow]Note: graph has {stats['functions']} nodes. "
            f"Large graphs may render slowly in the browser.[/yellow]"
        )

    # ---- stage 5: optional file-summary collapse ---------------------------
    if cfg.output.summary_by_file:
        t0 = _stage_begin(5, _total_stages, "Collapsing function graph to file summary")
        before = stats["functions"]
        graph = collapse_to_files(graph)
        after = len(graph.functions)
        console.print(
            f"  [cyan]Summary mode:[/cyan] collapsed {before} function nodes -> "
            f"{after} file nodes ({len(graph.calls)} cross-file edges)."
        )
        _stage_end(t0)

    # ---- stage N: render ---------------------------------------------------
    output_path = Path(args.output)
    render_stage = 6 if _will_summarize else 5
    t0 = _stage_begin(render_stage, _total_stages,
                      f"Rendering to {output_path} ({', '.join(cfg.output.formats)})")

    # Compute slot-1 / slot-2 graphs for the HTML renderer.
    # Special-case `function` and `script`: both views consume the raw
    # function-level data — `script` view does its own per-file grouping in JS,
    # and `function` view IS the underlying graph. For every other level
    # (folder / module / library / namespace), pass the aggregated graph.
    slot1_level = RenderLevel(cfg.render.view_slot_1)
    slot2_level = RenderLevel(cfg.render.view_slot_2)

    def _graph_for_slot(level: RenderLevel) -> "CallGraph":
        if level == RenderLevel.FUNCTION or level == RenderLevel.SCRIPT:
            return graph
        return aggregate(
            graph, level,
            project_root=str(project_root.resolve()),
            modules=modules,
            folder_depth=cfg.render.folder_depth,
        )

    slot1_graph = _graph_for_slot(slot1_level)
    slot2_graph = _graph_for_slot(slot2_level) if slot2_level != slot1_level else None

    # Non-HTML formats use the single --render-level shorthand if set.
    non_html = [f for f in cfg.output.formats if f != "html"]
    if non_html and cfg.render.render_level and cfg.render.render_level != "function":
        try:
            level = RenderLevel(cfg.render.render_level)
            graph_for_other = aggregate(
                graph,
                level,
                project_root=str(project_root.resolve()),
                modules=modules,
                folder_depth=cfg.render.folder_depth,
            )
        except Exception:
            graph_for_other = graph
    else:
        graph_for_other = graph

    try:
        if "html" in cfg.output.formats:
            # Primary graph fed to vis.js is ALWAYS the function-level graph so
            # the network is available no matter which level either slot picks.
            # Slot-specific aggregated graphs are passed alongside.
            output_files = render_graph(
                graph,
                output_path,
                cfg,
                slot1_graph=slot1_graph,
                slot2_graph=slot2_graph if slot2_graph is not None else slot1_graph,
            )
        else:
            output_files = render_graph(graph_for_other, output_path, cfg)
    except Exception as exc:
        console.print(f"[bold red]Render error:[/bold red] {exc}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1
    for out_file in output_files:
        console.print(f"  [green]OK[/green]  {out_file}")
    _stage_end(t0)

    console.print("\n[bold green]Done.[/bold green]")
    return 0


def _write_violations_report(path: str, graph) -> None:
    payload = {
        "modules": {
            name: {
                "inferred_from": m.inferred_from,
                "project": m.project,
                "file_count": len(m.files),
            }
            for name, m in graph.modules.items()
        },
        "violations": [
            {
                "kind": v.rule_kind,
                "from": v.from_module,
                "to": v.to_module,
                "reason": v.reason,
                "sample_edges": v.sample_edges,
            }
            for v in graph.violations
        ],
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def _print_stats(stats: dict, files_discovered: int | None = None) -> None:
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column(style="bold cyan")
    table.add_row("Functions in graph", str(stats["functions"]))
    table.add_row("Call edges (total)", str(stats["calls"]))
    table.add_row(
        "Resolved (linked)",
        f"{stats['resolved_calls']} / {stats['calls']}",
    )
    unresolved = stats["calls"] - stats["resolved_calls"]
    table.add_row("Unresolved (external)", str(unresolved))
    table.add_row("External stubs", str(stats["external_functions"]))
    if files_discovered is not None:
        table.add_row("Files discovered", str(files_discovered))
    table.add_row("Files in graph", str(stats["files_parsed"]))
    if stats["parse_errors"]:
        table.add_row("Parse errors", f"[red]{stats['parse_errors']}[/red]")
    console.print(table)
    console.print(
        "  [dim]Resolved = call sites linked to a function defined inside the project. "
        "Unresolved = standard library, virtual dispatch, macros, or unparseable callees. "
        "'Files in graph' counts only files whose functions survived the node cap.[/dim]"
    )
