"""
Lightweight .sln / .vcxproj inspector used by the GUI's "Inspect Solution" modal.

CLI dispatch (`callgraph_tool.py --project FOO.sln --inspect-sln`) writes the JSON
produced here to stdout; the GUI parses it and renders the tree-picker.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Config, FilterConfig, SelectionConfig
from .models import Language
from .sln_reader import discover_from_sln


def inspect_sln(sln_path: Path, cfg: Config | None = None) -> dict[str, Any]:
    """Return a JSON-friendly dict describing a .sln solution.

    Structure:
        {
          "sln_path": "...",
          "configurations": ["Debug","Release"],
          "platforms": ["x64","Win32"],
          "active_configuration": "Debug",
          "active_platform": "x64",
          "warnings": ["..."],
          "projects": [{
            "name": "MyApp",
            "path": "...",
            "include_paths": ["..."],
            "defines": ["..."],
            "files": ["abs path", ...],
            "folders": [{"name": "src", "files": ["src/a.cpp", ...]}, ...]
          }, ...]
        }
    """
    cfg = cfg or _bare_config()

    summary = discover_from_sln(sln_path, cfg)

    active_cfg = cfg.build.configuration or _pick_default(summary.configurations, ["Debug", "Release"])
    active_plat = cfg.build.platform or _pick_default(summary.platforms, ["x64", "Win32"])

    projects_out: list[dict[str, Any]] = []
    for meta in summary.project_metadata:
        files = [str(Path(f)) for f in meta.get("files", [])]
        folders = _group_by_top_folder(files, sln_path.parent)
        projects_out.append({
            "name": meta["name"],
            "path": meta["path"],
            "include_paths": meta.get("include_paths", []),
            "defines": meta.get("defines", []),
            "files": files,
            "folders": folders,
        })

    return {
        "sln_path": str(sln_path),
        "configurations": list(summary.configurations),
        "platforms": list(summary.platforms),
        "active_configuration": active_cfg,
        "active_platform": active_plat,
        "warnings": list(summary.warnings),
        "projects": projects_out,
    }


def inspect_sln_to_json(sln_path: Path, cfg: Config | None = None) -> str:
    """JSON-encode `inspect_sln(...)`."""
    return json.dumps(inspect_sln(sln_path, cfg), indent=2, ensure_ascii=False)


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _bare_config() -> Config:
    """A Config with minimal filtering so inspection sees as much as possible."""
    c = Config()
    # Keep default exclude_dirs but clear inclusion gates that may narrow visibility.
    c.filter = FilterConfig(exclude_dirs=c.filter.exclude_dirs, exclude_functions=[])
    c.selection = SelectionConfig()
    return c


def _pick_default(values: list[str], preferred: list[str]) -> str | None:
    if not values:
        return None
    lower = {v.lower(): v for v in values}
    for p in preferred:
        if p.lower() in lower:
            return lower[p.lower()]
    return values[0]


def _group_by_top_folder(files: list[str], sln_dir: Path) -> list[dict[str, Any]]:
    """Group files by their top folder relative to the .sln directory."""
    buckets: dict[str, list[str]] = {}
    for f in files:
        p = Path(f)
        try:
            rel = p.resolve().relative_to(sln_dir.resolve())
            parts = rel.parts
        except (ValueError, OSError):
            parts = p.parts
        folder = parts[0] if len(parts) > 1 else "<root>"
        buckets.setdefault(folder, []).append(str(p))
    return [{"name": name, "files": files_in} for name, files_in in sorted(buckets.items())]
