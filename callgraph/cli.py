"""
Command-line interface. Orchestrates the full pipeline.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

from . import __version__
from .config import load_config, Config
from .discovery import discover_files, summary
from .graph.builder import build_call_graph
from .models import Language
from .parsers import get_parser
from .renderers import render_graph
from .sln_reader import discover_from_sln

console = Console()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="callgraph_tool",
        description="Offline function call graph analyzer for C, C++, and Python projects.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python callgraph_tool.py --project ./my_project --output graph.html
  python callgraph_tool.py --project ./my_project --config config.yaml --output graph.html
  python callgraph_tool.py --project ./my_project --entry main --depth 3 --output graph.html
  python callgraph_tool.py --project ./my_project --output graph --formats html svg
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
        required=True,
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
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    console.print(Panel.fit(
        f"[bold cyan]CallGraph Tool[/bold cyan] [dim]v{__version__}[/dim]",
        border_style="cyan",
    ))

    # Load config
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[bold red]Config error:[/bold red] {exc}")
        return 1

    cfg = _apply_cli_overrides(cfg, args)

    # Discover files — directory scan or Visual Studio solution
    project_path = Path(args.project)
    is_sln = project_path.suffix.lower() == ".sln"

    if is_sln:
        if not project_path.exists():
            console.print(f"[bold red]Error:[/bold red] Solution file not found: {args.project}")
            return 1

        console.print(f"\n[bold]Reading solution:[/bold] {args.project}")
        try:
            sln_result = discover_from_sln(project_path, cfg)
        except FileNotFoundError as exc:
            console.print(f"[bold red]Solution error:[/bold red] {exc}")
            return 1

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

        files = sln_result.files
        console.print(summary(files))

    else:
        console.print(f"\n[bold]Scanning project:[/bold] {args.project}")
        try:
            files = discover_files(args.project, cfg)
        except (FileNotFoundError, NotADirectoryError) as exc:
            console.print(f"[bold red]Discovery error:[/bold red] {exc}")
            return 1
        console.print(summary(files))

    total_files = sum(len(v) for v in files.values())
    if total_files == 0:
        console.print("[yellow]No source files found. Check --project path and config filters.[/yellow]")
        return 1

    # Parse all files
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

    # Build call graph
    console.print("\n[bold]Building call graph...[/bold]")
    graph = build_call_graph(all_functions, all_calls, parse_errors, cfg)

    stats = graph.stats()
    _print_stats(stats)

    if stats["functions"] == 0:
        console.print("[yellow]No functions found after filtering. Try relaxing your config filters.[/yellow]")
        return 1

    # Warn if node cap hit or graph is large
    if stats["functions"] >= cfg.output.max_nodes:
        console.print(
            f"[yellow]Warning: graph has {stats['functions']} nodes (limit: {cfg.output.max_nodes}). "
            f"Consider using --entry and --depth to focus the graph.[/yellow]"
        )
    elif stats["functions"] > 500:
        console.print(
            f"[yellow]Note: graph has {stats['functions']} nodes. "
            f"Large graphs may render slowly in the browser.[/yellow]"
        )

    # Render outputs
    output_path = Path(args.output)
    console.print(f"\n[bold]Rendering to:[/bold] {output_path}")

    try:
        output_files = render_graph(graph, output_path, cfg)
    except Exception as exc:
        console.print(f"[bold red]Render error:[/bold red] {exc}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1

    for out_file in output_files:
        console.print(f"  [green]OK[/green]  {out_file}")

    console.print("\n[bold green]Done.[/bold green]")
    return 0


def _print_stats(stats: dict) -> None:
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column(style="bold cyan")
    table.add_row("Functions", str(stats["functions"]))
    table.add_row("Call edges", str(stats["calls"]))
    table.add_row("Resolved", f"{stats['resolved_calls']} / {stats['calls']}")
    table.add_row("External", str(stats["external_functions"]))
    table.add_row("Files parsed", str(stats["files_parsed"]))
    if stats["parse_errors"]:
        table.add_row("Parse errors", f"[red]{stats['parse_errors']}[/red]")
    console.print(table)
