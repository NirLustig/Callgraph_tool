"""
File discovery: recursively scan a project directory and classify source files by language.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Iterator

from .config import Config, FilterConfig
from .models import Language

EXTENSION_MAP: dict[str, Language] = {
    ".py":   Language.PYTHON,
    ".c":    Language.C,
    ".h":    Language.C,        # default; may be upgraded to CPP by context
    ".cpp":  Language.CPP,
    ".hpp":  Language.CPP,
    ".hxx":  Language.CPP,
    ".hh":   Language.CPP,
    ".cc":   Language.CPP,
    ".cxx":  Language.CPP,
    ".m":    Language.MATLAB,
}

CPP_EXTENSIONS = {".cpp", ".hpp", ".hxx", ".hh", ".cc", ".cxx"}


def _matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _should_exclude_dir(dir_path: Path, project_root: Path, cfg: FilterConfig) -> bool:
    relative = dir_path.relative_to(project_root)
    parts = relative.parts
    for part in parts:
        if _matches_any(part, cfg.exclude_dirs):
            return True
    if cfg.include_dirs:
        return not any(_matches_any(str(relative), pat) for pat in cfg.include_dirs)
    return False


def _should_exclude_file(file_path: Path, project_root: Path, cfg: FilterConfig) -> bool:
    name = file_path.name
    if _matches_any(name, cfg.exclude_files):
        return True
    if cfg.include_files:
        return not _matches_any(name, cfg.include_files)
    return False


def infer_header_language(header: Path) -> Language:
    """Upgrade a .h file to CPP if sibling C++ files exist in the same directory."""
    parent = header.parent
    for sibling in parent.iterdir():
        if sibling.suffix.lower() in CPP_EXTENSIONS:
            return Language.CPP
    return Language.C


# Keep private alias for backwards compatibility with any direct callers
_infer_header_language = infer_header_language


def discover_files(project_root: str, config: Config) -> dict[Language, list[Path]]:
    """
    Recursively walk project_root, apply filters, and return source files grouped by language.
    """
    root = Path(project_root).resolve()
    if not root.exists():
        raise FileNotFoundError(f"Project directory not found: {project_root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {project_root}")

    result: dict[Language, list[Path]] = {lang: [] for lang in Language}

    for path in _walk_files(root, root, config.filter):
        ext = path.suffix.lower()
        if ext not in EXTENSION_MAP:
            continue

        lang = EXTENSION_MAP[ext]
        if ext == ".h":
            lang = infer_header_language(path)

        result[lang].append(path)

    return apply_selection(result, config, root)


# ---------------------------------------------------------------- #
# Selection filter                                                  #
# ---------------------------------------------------------------- #

_LANG_KEYS = {
    "python": Language.PYTHON,
    "py":     Language.PYTHON,
    "c":      Language.C,
    "cpp":    Language.CPP,
    "c++":    Language.CPP,
    "matlab": Language.MATLAB,
    "m":      Language.MATLAB,
}


def apply_selection(
    files: dict[Language, list[Path]],
    config: Config,
    project_root: Path | None = None,
) -> dict[Language, list[Path]]:
    """Filter the discovered file dict by the selection config (folders/files/languages).

    Project-level selection is applied earlier (during discover_from_sln). This helper
    handles folder / file glob filters and language gates.
    """
    sel = getattr(config, "selection", None)
    if sel is None:
        return files

    keep_langs: set[Language] | None = None
    if sel.languages:
        keep_langs = {_LANG_KEYS[k.lower()] for k in sel.languages if k.lower() in _LANG_KEYS}

    folder_globs = list(sel.folders or [])
    file_globs = list(sel.files or [])

    if not (keep_langs or folder_globs or file_globs):
        return files

    out: dict[Language, list[Path]] = {lang: [] for lang in Language}
    for lang, paths in files.items():
        if keep_langs is not None and lang not in keep_langs:
            continue
        for p in paths:
            rel = ""
            if project_root is not None:
                try:
                    rel = str(p.resolve().relative_to(project_root).as_posix())
                except (ValueError, OSError):
                    rel = p.as_posix()
            else:
                rel = p.as_posix()
            if folder_globs and not any(_glob_match(rel, pat) for pat in folder_globs):
                continue
            if file_globs and not any(_glob_match(rel, pat) for pat in file_globs):
                # Fallback: try matching just the filename (handles files outside project root)
                if not any(_glob_match(p.name, pat) for pat in file_globs):
                    continue
            out[lang].append(p)
    return out


def _glob_match(rel_path: str, pattern: str) -> bool:
    """Glob with ``**`` support (mirrors architecture._glob_match)."""
    import re as _re
    pat = pattern.replace("\\", "/")
    rel = rel_path.replace("\\", "/")
    if "**" not in pat:
        return fnmatch.fnmatch(rel, pat)
    out: list[str] = []
    i = 0
    while i < len(pat):
        ch = pat[i]
        if ch == "*" and i + 1 < len(pat) and pat[i + 1] == "*":
            out.append(".*")
            i += 2
            if i < len(pat) and pat[i] == "/":
                i += 1
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(_re.escape(ch))
            i += 1
    regex = "^" + "".join(out) + "$"
    return _re.match(regex, rel) is not None


def _walk_files(
    current: Path, root: Path, cfg: FilterConfig
) -> Iterator[Path]:
    try:
        entries = sorted(current.iterdir())
    except PermissionError:
        return

    for entry in entries:
        if entry.is_dir():
            if not _should_exclude_dir(entry, root, cfg):
                yield from _walk_files(entry, root, cfg)
        elif entry.is_file():
            if not _should_exclude_file(entry, root, cfg):
                yield entry


def summary(files: dict[Language, list[Path]]) -> str:
    lines = []
    total = 0
    for lang, paths in files.items():
        if paths:
            lines.append(f"  {lang.display_name()}: {len(paths)} file(s)")
            total += len(paths)
    lines.append(f"  Total: {total} file(s)")
    return "\n".join(lines)
