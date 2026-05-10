"""
Abstract base class for all language parsers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

from ..config import Config
from ..models import CallRelationship, FunctionDef


class BaseParser(ABC):
    def __init__(self, config: Config) -> None:
        self.config = config

    @abstractmethod
    def parse_file(
        self, path: Path
    ) -> tuple[list[FunctionDef], list[CallRelationship]]:
        """
        Parse a single source file.
        Returns (functions_defined, call_relationships_found).
        Must not raise on parse errors — return partial results and log.
        """
        ...

    def parse_files(
        self,
        paths: list[Path],
        progress_task: Optional[tuple[Any, Any]] = None,
    ) -> tuple[list[FunctionDef], list[CallRelationship], list[str]]:
        """
        Parse multiple files, isolating errors per file.
        Returns (all_functions, all_calls, error_messages).
        """
        all_fns: list[FunctionDef] = []
        all_calls: list[CallRelationship] = []
        errors: list[str] = []

        for path in paths:
            try:
                fns, calls = self.parse_file(path)
                all_fns.extend(fns)
                all_calls.extend(calls)
            except Exception as exc:
                errors.append(f"{path}: {exc}")

            if progress_task:
                progress, task = progress_task
                progress.advance(task)

        return all_fns, all_calls, errors
