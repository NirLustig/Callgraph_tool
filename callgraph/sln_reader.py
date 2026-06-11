"""
Visual Studio Solution (.sln) and project (.vcxproj) reader.

Extracts the source/header file lists referenced by a VS solution so that
the rest of the callgraph pipeline can analyze them exactly like a normal
C/C++ source folder.

Supported input:
  - Visual Studio 2010–2022 .sln (text format)
  - MSBuild .vcxproj (XML, any schema version)

Limitations:
  - Only C/C++ vcxproj projects are analysed; C#/F#/VB projects are silently
    skipped (they have different project type GUIDs).
  - Build-configuration conditions on <ClCompile>/<ClInclude> items are
    ignored — all listed files are included regardless of Debug/Release.
  - Wildcard globs in Include attributes (e.g. "src\\*.cpp") are expanded
    relative to the project directory at read time.
"""
from __future__ import annotations

import fnmatch
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .models import Language


# ------------------------------------------------------------------ #
# Constants                                                           #
# ------------------------------------------------------------------ #

# Known C++ project type GUIDs (upper-case, no braces).
# Any project whose type GUID is in this set (or not listed below as
# "definitely not C++") is treated as a candidate vcxproj project.
_CPP_GUIDS: set[str] = {
    "8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942",   # Visual C++ (Win32 / x64)
    "8BC9CEB8-8B4A-11D0-8D11-00A0C91BC943",   # Visual C++ variant (rare)
}

# GUIDs to unconditionally skip (solution folders and known non-C++ types)
_SKIP_GUIDS: set[str] = {
    "2150E333-8FDC-42A3-9474-1A3956D46DE8",   # Solution Folder
    "FAE04EC0-301F-11D3-BF4B-00C04F79EFBC",   # C#
    "F184B08F-C81C-45F6-A57F-5ABD9991F28F",   # VB.NET
    "F2A71F9B-5D33-465A-A702-920D77279786",   # F#
    "E6FDF86B-F3D1-11D4-8576-0002A516ECE8",   # J#
}

# MSBuild XML namespace
_MSBUILD_NS = "http://schemas.microsoft.com/developer/msbuild/2003"
_NS_RE = re.compile(r"\{[^}]+\}")

# vcxproj item types that carry source/header files
_SOURCE_ITEM_TYPES = {"clcompile", "clinclude", "none"}

# File extensions we care about
_SOURCE_EXTS: set[str] = {".c", ".cpp", ".cxx", ".cc"}
_HEADER_EXTS: set[str] = {".h", ".hpp", ".hxx", ".hh"}
_ALL_EXTS: set[str] = _SOURCE_EXTS | _HEADER_EXTS

# Pattern to extract a Project() line from a .sln
_PROJ_RE = re.compile(
    r'Project\("\{([A-Fa-f0-9\-]+)\}"\)\s*=\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"\{([A-Fa-f0-9\-]+)\}"',
    re.IGNORECASE,
)


# ------------------------------------------------------------------ #
# Data structures                                                     #
# ------------------------------------------------------------------ #

@dataclass
class VcxprojInfo:
    """Metadata about one project extracted from the .sln."""
    name: str
    path: Path          # absolute, resolved path to the .vcxproj
    type_guid: str      # upper-case, no braces
    project_guid: str   # upper-case, no braces


@dataclass
class SlnSummary:
    """Result returned by discover_from_sln()."""
    files: dict[Language, list[Path]] = field(
        default_factory=lambda: {lang: [] for lang in Language}
    )
    project_count: int = 0
    file_count: int = 0
    warnings: list[str] = field(default_factory=list)
    # Per-project absolute file paths, used by BuildInfo / architecture inference.
    project_files: dict[str, list[Path]] = field(default_factory=dict)
    # Solution-level configurations and platforms (deduplicated, in original order).
    configurations: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    # Solution-level merged include paths and defines (best-effort).
    include_paths: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    # Per-project metadata for the GUI / build_info layer.
    project_metadata: list[dict] = field(default_factory=list)


# ------------------------------------------------------------------ #
# .sln parser                                                         #
# ------------------------------------------------------------------ #

def read_sln(sln_path: Path) -> tuple[list[VcxprojInfo], list[str]]:
    """
    Parse a Visual Studio solution file.

    Returns (projects, warnings) where *projects* is a list of
    VcxprojInfo objects for every C/C++ vcxproj referenced by the
    solution, and *warnings* contains human-readable messages for
    skipped or missing entries.
    """
    warnings: list[str] = []
    projects: list[VcxprojInfo] = []
    sln_dir = sln_path.parent

    try:
        content = sln_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise FileNotFoundError(f"Cannot read solution file: {exc}") from exc

    for line in content.splitlines():
        m = _PROJ_RE.search(line)
        if not m:
            continue

        type_guid, name, rel_path, proj_guid = m.groups()
        type_guid_upper = type_guid.upper()
        proj_guid_upper = proj_guid.upper()

        # Skip known non-C++ project types
        if type_guid_upper in _SKIP_GUIDS:
            continue

        # Normalise Windows path separators
        rel_path_norm = rel_path.replace("\\", "/")

        # Only handle .vcxproj; warn about anything else
        if not rel_path_norm.lower().endswith(".vcxproj"):
            if type_guid_upper not in _SKIP_GUIDS:
                warnings.append(
                    f"Skipped '{name}': not a .vcxproj "
                    f"(path: {rel_path}, type GUID: {{{type_guid_upper}}})"
                )
            continue

        proj_path = (sln_dir / rel_path_norm).resolve()

        if not proj_path.exists():
            warnings.append(
                f"Missing project file for '{name}': {proj_path}"
            )
            continue

        projects.append(VcxprojInfo(
            name=name,
            path=proj_path,
            type_guid=type_guid_upper,
            project_guid=proj_guid_upper,
        ))

    return projects, warnings


# ------------------------------------------------------------------ #
# .vcxproj parser                                                     #
# ------------------------------------------------------------------ #

def read_vcxproj(vcxproj_path: Path) -> tuple[list[Path], list[str]]:
    """
    Parse a .vcxproj file and return the absolute paths of every
    source and header file it references.

    Returns (file_paths, warnings).  Missing files produce warnings
    rather than errors so one bad reference doesn't abort everything.
    """
    warnings: list[str] = []
    files: list[Path] = []
    proj_dir = vcxproj_path.parent

    try:
        tree = ET.parse(str(vcxproj_path))
    except ET.ParseError as exc:
        return [], [f"XML parse error in {vcxproj_path.name}: {exc}"]
    except OSError as exc:
        return [], [f"Cannot read {vcxproj_path.name}: {exc}"]

    root = tree.getroot()

    for elem in root.iter():
        # Strip MSBuild namespace so we can match tag names simply
        tag = _NS_RE.sub("", elem.tag).lower()
        if tag not in _SOURCE_ITEM_TYPES:
            continue

        include = elem.get("Include") or elem.get("include")
        if not include:
            continue

        # Skip MSBuild metadata references like %(Filename).obj
        if include.startswith("%"):
            continue

        # Normalise separators
        include_norm = include.replace("\\", "/")

        # Expand wildcards relative to the project directory
        if "*" in include_norm or "?" in include_norm:
            matched = list(proj_dir.glob(include_norm))
            if not matched:
                warnings.append(
                    f"Glob '{include}' in {vcxproj_path.name} matched no files"
                )
            for f in matched:
                if f.suffix.lower() in _ALL_EXTS and f.is_file():
                    files.append(f.resolve())
            continue

        # Plain path
        candidate = (proj_dir / include_norm).resolve()

        if candidate.suffix.lower() not in _ALL_EXTS:
            continue   # e.g. .rc, .idl, .asm — not relevant

        if not candidate.is_file():
            warnings.append(
                f"Missing source file in {vcxproj_path.name}: {candidate}"
            )
            continue

        files.append(candidate)

    return files, warnings


# ------------------------------------------------------------------ #
# High-level entry point used by the CLI                              #
# ------------------------------------------------------------------ #

def discover_from_sln(sln_path: Path, cfg: Config) -> SlnSummary:
    """
    Parse a .sln file and all its C/C++ projects.

    Applies the same exclude_dirs / exclude_files filters as the
    directory-based discovery so config files work transparently.

    Returns a SlnSummary with files grouped by Language.
    """
    from .discovery import EXTENSION_MAP, infer_header_language  # avoid circular import

    summary = SlnSummary()

    projects, sln_warnings = read_sln(sln_path)
    summary.warnings.extend(sln_warnings)
    summary.project_count = len(projects)

    # Solution-level configurations + platforms (best-effort regex on the .sln text).
    summary.configurations, summary.platforms = _read_sln_configs_platforms(sln_path)

    seen: set[Path] = set()
    config_filter = (cfg.build.configuration or "").strip().lower() if cfg else ""
    platform_filter = (cfg.build.platform or "").strip().lower() if cfg else ""

    project_filter = []
    if cfg and cfg.selection and cfg.selection.projects:
        project_filter = [p.lower() for p in cfg.selection.projects if p]

    for proj in projects:
        if project_filter and not any(p in proj.name.lower() for p in project_filter):
            continue

        source_files, proj_warnings = read_vcxproj(proj.path)
        summary.warnings.extend(
            f"[{proj.name}] {w}" for w in proj_warnings
        )

        proj_files_kept: list[Path] = []
        for f in source_files:
            if f in seen:
                continue
            seen.add(f)

            # Apply exclude_dirs filter — check every path component
            if _is_excluded(f, cfg):
                continue

            ext = f.suffix.lower()
            if ext not in EXTENSION_MAP:
                continue

            lang = EXTENSION_MAP[ext]
            if ext == ".h":
                lang = infer_header_language(f)

            summary.files[lang].append(f)
            proj_files_kept.append(f)

        summary.project_files[proj.name] = proj_files_kept

        # Per-project include paths / defines from PropertyGroup.
        try:
            includes, defines = _read_vcxproj_metadata(
                proj.path, config_filter, platform_filter
            )
        except Exception as exc:
            includes, defines = [], []
            summary.warnings.append(
                f"[{proj.name}] metadata read failed: {exc}"
            )
        summary.include_paths.extend(includes)
        summary.defines.extend(defines)
        summary.project_metadata.append({
            "name": proj.name,
            "path": str(proj.path),
            "include_paths": includes,
            "defines": defines,
            "files": [str(f) for f in proj_files_kept],
        })

    # Deduplicate while preserving order.
    summary.include_paths = list(dict.fromkeys(summary.include_paths))
    summary.defines = list(dict.fromkeys(summary.defines))
    summary.file_count = sum(len(v) for v in summary.files.values())
    return summary


# ------------------------------------------------------------------ #
# Solution configurations + platforms                                 #
# ------------------------------------------------------------------ #

_SLN_GLOBAL_CFG_LINE = re.compile(
    r'^\s*([^|=]+?)\|([^=]+?)\s*=\s*\1\|\2\s*$', re.MULTILINE
)


def _read_sln_configs_platforms(sln_path: Path) -> tuple[list[str], list[str]]:
    """Return (configurations, platforms) found in the .sln SolutionConfigurationPlatforms section."""
    try:
        content = sln_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return [], []
    in_section = False
    configs: list[str] = []
    platforms: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("GlobalSection(SolutionConfigurationPlatforms)"):
            in_section = True
            continue
        if in_section and stripped.startswith("EndGlobalSection"):
            break
        if in_section:
            m = _SLN_GLOBAL_CFG_LINE.match(line)
            if m:
                cfg_name = m.group(1).strip()
                plat_name = m.group(2).strip()
                if cfg_name and cfg_name not in configs:
                    configs.append(cfg_name)
                if plat_name and plat_name not in platforms:
                    platforms.append(plat_name)
    return configs, platforms


# ------------------------------------------------------------------ #
# .vcxproj include paths / defines (PropertyGroup, best-effort)       #
# ------------------------------------------------------------------ #

def _read_vcxproj_metadata(
    vcxproj_path: Path,
    config_filter: str,
    platform_filter: str,
) -> tuple[list[str], list[str]]:
    """Return (include_paths, defines) merged across matching ItemDefinitionGroups."""
    try:
        tree = ET.parse(str(vcxproj_path))
    except (ET.ParseError, OSError):
        return [], []
    root = tree.getroot()

    includes: list[str] = []
    defines: list[str] = []

    for idg in root.iter():
        tag = _NS_RE.sub("", idg.tag).lower()
        if tag != "itemdefinitiongroup":
            continue
        condition = idg.get("Condition", "") or ""
        if config_filter and config_filter not in condition.lower() and condition:
            continue
        if platform_filter and platform_filter not in condition.lower() and condition:
            continue
        for child in idg.iter():
            ctag = _NS_RE.sub("", child.tag).lower()
            text = (child.text or "").strip()
            if not text:
                continue
            if ctag == "additionalincludedirectories":
                for tok in text.split(";"):
                    tok = tok.strip()
                    if tok and not tok.startswith("%"):
                        includes.append(tok.replace("\\", "/"))
            elif ctag == "preprocessordefinitions":
                for tok in text.split(";"):
                    tok = tok.strip()
                    if tok and not tok.startswith("%"):
                        defines.append(tok)
    return includes, defines


# ------------------------------------------------------------------ #
# Internal helpers                                                    #
# ------------------------------------------------------------------ #

def _is_excluded(file_path: Path, cfg: Config) -> bool:
    """Return True if file_path should be skipped per config filters."""
    # exclude_dirs: any path component matches
    for part in file_path.parts:
        if any(fnmatch.fnmatch(part, pat) for pat in cfg.filter.exclude_dirs):
            return True

    # exclude_files: filename matches
    name = file_path.name
    if any(fnmatch.fnmatch(name, pat) for pat in cfg.filter.exclude_files):
        return True

    # include_files: if specified, file must match at least one pattern
    if cfg.filter.include_files:
        if not any(fnmatch.fnmatch(name, pat) for pat in cfg.filter.include_files):
            return True

    return False
