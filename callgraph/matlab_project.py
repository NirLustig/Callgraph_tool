"""MATLAB project / search-path metadata discovery (idea G10).

The C/C++ path integrates with ``compile_commands.json`` and ``.vcxproj`` to learn
defines, include directories and per-translation-unit build flags. MATLAB has no
compiler database, but it *does* express project structure on the file system and
in a handful of conventional files. This module harvests that information without
touching the parser:

* ``+package`` directories       -> dotted package namespaces (``+a/+b`` -> ``a.b``)
* ``@ClassName`` directories     -> class folders
* ``*.prj`` files                -> project name (best-effort XML scrape)
* ``pathdef.m`` / ``addpath(...)`` -> search-path directories

The result is a :class:`~callgraph.models.MatlabProjectInfo` attached to the
``CallGraph`` for display. Nothing here changes call resolution.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

from .models import MatlabProjectInfo

# Directories we never descend into when scanning the tree for package/class folders.
_SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".vscode", ".idea"}

_ADDPATH_RE = re.compile(
    r"\b(?:addpath|genpath)\s*\(\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
# .prj name scrape — handles both the legacy XML attribute form and the
# <param.appname> element used by newer MATLAB project files.
_PRJ_NAME_ATTR_RE = re.compile(r"<\s*\w+[^>]*\bname\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_PRJ_APPNAME_RE = re.compile(r"<param\.appname>\s*([^<]+?)\s*</param\.appname>", re.IGNORECASE)


def _package_name(directory: Path, root: Path) -> str:
    """Dotted package name for a ``+pkg`` directory (walking nested ``+`` parents)."""
    parts: list[str] = []
    cur = directory
    while cur != root and cur.name.startswith("+"):
        parts.append(cur.name[1:])
        cur = cur.parent
    parts.reverse()
    return ".".join(p for p in parts if p)


def _scan_tree(root: Path) -> tuple[set[str], set[str]]:
    """Return (packages, class_folder_names) discovered under ``root``."""
    packages: set[str] = set()
    classes: set[str] = set()
    for dirpath, dirnames, _filenames in _walk(root):
        for d in list(dirnames):
            if d in _SKIP_DIRS:
                dirnames.remove(d)
                continue
            full = dirpath / d
            if d.startswith("+"):
                pkg = _package_name(full, root)
                if pkg:
                    packages.add(pkg)
            elif d.startswith("@") and len(d) > 1:
                classes.add(d[1:])
    return packages, classes


def _walk(root: Path):
    """Lightweight os.walk-style generator yielding (Path dir, [child dir names], [])."""
    try:
        entries = list(root.iterdir())
    except (OSError, PermissionError):
        return
    child_dirs = [e.name for e in entries if e.is_dir()]
    yield root, child_dirs, []
    for name in list(child_dirs):
        if name in _SKIP_DIRS:
            continue
        sub = root / name
        yield from _walk(sub)


def _prj_name(prj: Path) -> Optional[str]:
    try:
        text = prj.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return None
    m = _PRJ_APPNAME_RE.search(text)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = _PRJ_NAME_ATTR_RE.search(text)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return None


def _scan_addpath(text: str) -> set[str]:
    return {m.group(1).strip() for m in _ADDPATH_RE.finditer(text) if m.group(1).strip()}


def discover_matlab_project(
    project_root: str | Path,
    m_files: Optional[Iterable[Path]] = None,
) -> MatlabProjectInfo:
    """Discover MATLAB project metadata under ``project_root``.

    ``m_files`` (when given) restricts the ``addpath`` scan to the already
    discovered MATLAB sources; otherwise every ``.m`` file under the root is read.
    """
    root = Path(project_root).resolve()
    info = MatlabProjectInfo()
    if not root.exists():
        info.warnings.append(f"project root not found: {root}")
        return info

    packages, classes = _scan_tree(root)
    info.packages = sorted(packages)
    info.class_folders = sorted(classes)

    # .prj project files (best-effort name scrape).
    prj_files: list[str] = []
    for dirpath, dirnames, _ in _walk(root):
        for d in list(dirnames):
            if d in _SKIP_DIRS:
                dirnames.remove(d)
        for child in _iter_files(dirpath, ".prj"):
            prj_files.append(str(child))
            if info.project_name is None:
                nm = _prj_name(child)
                if nm:
                    info.project_name = nm
    info.project_files = sorted(prj_files)

    # Search-path directories: pathdef.m + addpath()/genpath() across sources.
    addpaths: set[str] = set()
    sources: list[Path]
    if m_files is not None:
        sources = list(m_files)
    else:
        sources = [p for dp, dn, _ in _walk(root) for p in _iter_files(dp, ".m")]
    saw_pathdef = False
    for src in sources:
        try:
            text = src.read_text(encoding="utf-8-sig", errors="replace")
        except (OSError, UnicodeError):
            continue
        if src.name.lower() == "pathdef.m":
            saw_pathdef = True
            # pathdef.m lists quoted absolute/relative path strings in a cell array.
            for q in re.findall(r"'([^']+)'", text):
                if "/" in q or "\\" in q:
                    addpaths.add(q.strip())
        addpaths |= _scan_addpath(text)
    info.addpath_dirs = sorted(addpaths)

    # Provenance label.
    if info.project_files and (saw_pathdef or info.addpath_dirs):
        info.source = "mixed"
    elif info.project_files:
        info.source = "prj"
    elif saw_pathdef or info.addpath_dirs:
        info.source = "pathdef"
    else:
        info.source = "filesystem"
    return info


def _iter_files(directory: Path, suffix: str):
    try:
        for entry in directory.iterdir():
            if entry.is_file() and entry.suffix.lower() == suffix:
                yield entry
    except (OSError, PermissionError):
        return
