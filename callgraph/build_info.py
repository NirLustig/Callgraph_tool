"""
compile_commands.json ingestion and BuildInfo cross-referencing.

Public surface:
    - find_compile_commands(project_root) -> Optional[Path]
    - load_compile_commands(path) -> tuple[dict[str, CompileUnit], list[str]]
    - cross_reference(files, units, project_root, sln_metadata, config) -> BuildInfo

The first two helpers are pure parsers — no side effects, no project knowledge.
cross_reference() assembles the final BuildInfo attached to the CallGraph.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import Config
from .models import BuildInfo, CompileUnit


_AUTO_DETECT_RELATIVE = (
    Path("compile_commands.json"),
    Path("build") / "compile_commands.json",
    Path("out") / "compile_commands.json",
)


def find_compile_commands(project_root: Path) -> Optional[Path]:
    """Return the first existing compile_commands.json under common locations, or None."""
    project_root = Path(project_root)
    if project_root.suffix.lower() == ".sln":
        project_root = project_root.parent
    for rel in _AUTO_DETECT_RELATIVE:
        candidate = (project_root / rel).resolve()
        if candidate.is_file():
            return candidate
    return None


def _parse_argv(entry: dict[str, Any]) -> list[str]:
    """Return the argv list for a compile_commands entry (handles 'command' or 'arguments')."""
    if "arguments" in entry and isinstance(entry["arguments"], list):
        return [str(a) for a in entry["arguments"]]
    command = entry.get("command", "")
    if not isinstance(command, str) or not command:
        return []
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        # Fallback: whitespace split if shlex chokes on a stray quote on Windows.
        return command.split()


def _extract_metadata(argv: list[str]) -> tuple[list[str], dict[str, Optional[str]], list[str]]:
    """
    From an argv, return (includes, defines, extra_flags).

    Recognises -I/<I (MSVC), -isystem, -D/<D (MSVC). Everything else flows into extra_flags.
    """
    includes: list[str] = []
    defines: dict[str, Optional[str]] = {}
    extras: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        # combined -Ipath / -Dname=val
        if tok.startswith(("-I", "/I")):
            payload = tok[2:]
            if payload:
                includes.append(payload)
            elif i + 1 < len(argv):
                includes.append(argv[i + 1])
                i += 1
        elif tok == "-isystem" and i + 1 < len(argv):
            includes.append(argv[i + 1])
            i += 1
        elif tok.startswith("-isystem="):
            includes.append(tok[len("-isystem="):])
        elif tok.startswith(("-D", "/D")):
            payload = tok[2:]
            if not payload and i + 1 < len(argv):
                payload = argv[i + 1]
                i += 1
            if payload:
                if "=" in payload:
                    name, value = payload.split("=", 1)
                    defines[name] = value
                else:
                    defines[payload] = None
        elif tok in ("-c", "/c"):
            # Skip; "compile only" flag is structural, not metadata.
            pass
        else:
            extras.append(tok)
        i += 1
    return includes, defines, extras


def _normalise(path: str | Path, base: Optional[Path] = None) -> str:
    """Return an absolute, normalised string path."""
    p = Path(path)
    if not p.is_absolute() and base is not None:
        p = base / p
    try:
        return str(p.resolve())
    except OSError:
        # Path may not exist on disk yet — keep the abs form.
        return str(p)


def load_compile_commands(path: Path) -> tuple[dict[str, CompileUnit], list[str]]:
    """
    Parse a compile_commands.json file.

    Returns (units_by_abs_path, warnings). The dict key is the resolved absolute
    path of the source file (case-normalised on Windows by ``Path.resolve``).
    """
    warnings: list[str] = []
    units: dict[str, CompileUnit] = {}

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"Cannot read compile_commands.json: {exc}"]

    if not isinstance(data, list):
        return {}, ["compile_commands.json: expected a top-level JSON array"]

    for idx, entry in enumerate(data):
        if not isinstance(entry, dict):
            warnings.append(f"compile_commands[{idx}]: not a JSON object — skipped")
            continue
        src = entry.get("file")
        directory = entry.get("directory", "")
        if not src:
            warnings.append(f"compile_commands[{idx}]: missing 'file' — skipped")
            continue
        base = Path(directory) if directory else None
        abs_src = _normalise(src, base)
        argv = _parse_argv(entry)
        includes, defines, extras = _extract_metadata(argv)
        # Resolve include paths against the working directory
        includes_abs = []
        for inc in includes:
            includes_abs.append(_normalise(inc, base))
        units[abs_src] = CompileUnit(
            source_file=abs_src,
            directory=str(base) if base else "",
            command=entry.get("command", "") if isinstance(entry.get("command"), str) else "",
            arguments=argv,
            includes=includes_abs,
            defines=defines,
            extra_flags=extras,
        )

    return units, warnings


def cross_reference(
    discovered_files: Iterable[Path],
    units: dict[str, CompileUnit],
    project_root: Path,
    cc_path: Optional[Path],
    sln_metadata: Optional[dict[str, Any]] = None,
    config: Optional[Config] = None,
) -> BuildInfo:
    """
    Build a BuildInfo by cross-referencing discovered files with the
    compile_commands.json units and any .sln metadata.

    sln_metadata (if provided) is the dict produced by sln_inspect.inspect_sln
    and supplies project names, configurations, platforms, and per-project
    include paths / defines.
    """
    discovered_abs = {_normalise(p): Path(p) for p in discovered_files}
    cc_keys = set(units.keys())

    # Files in discovery but missing from compile_commands.
    files_not_in_cc = sorted(p for p in discovered_abs.keys() if p not in cc_keys)
    # Files in compile_commands but not found on disk (or not discovered).
    cc_not_found = sorted(p for p in cc_keys if p not in discovered_abs)

    if sln_metadata is not None:
        source = "compile_commands+sln" if units else "sln"
    else:
        source = "compile_commands" if units else "folder"

    info = BuildInfo(
        source=source,
        compile_commands_path=str(cc_path) if cc_path else None,
        units=dict(units),
        configuration=(config.build.configuration if config else None) or None,
        platform=(config.build.platform if config else None) or None,
        files_not_in_compile_commands=files_not_in_cc,
        cc_files_not_found=cc_not_found,
    )

    if sln_metadata is not None:
        info.configuration = info.configuration or sln_metadata.get("active_configuration")
        info.platform = info.platform or sln_metadata.get("active_platform")
        info.projects = [p.get("name", "") for p in sln_metadata.get("projects", [])]
        for proj in sln_metadata.get("projects", []):
            files = [_normalise(f) for f in proj.get("files", [])]
            info.project_files[proj.get("name", "")] = files
            for inc in proj.get("include_paths", []) or []:
                # Resolve against the .vcxproj directory if it exists.
                base = Path(proj.get("path", "")).parent if proj.get("path") else None
                info.global_includes.append(_normalise(inc, base))
            for d in proj.get("defines", []) or []:
                if "=" in d:
                    name, val = d.split("=", 1)
                    info.global_defines.setdefault(name, val)
                else:
                    info.global_defines.setdefault(d, None)

    return info
