"""
Abstract base class for all language parsers.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        Parse multiple files in parallel via a ThreadPoolExecutor.
        Returns (all_functions, all_calls, error_messages).

        Worker count: cfg.output.parallel if set, else min(32, cpu_count + 4).
        Per-file errors are isolated — one failure does not cascade.
        Tree-sitter releases the GIL during parsing, so threads give near-linear speedup
        on multi-core machines until I/O saturates. Language parsers that hold
        thread-unsafe state (CParser, CppParser) use threading.local() internally.
        """
        all_fns: list[FunctionDef] = []
        all_calls: list[CallRelationship] = []
        errors: list[str] = []

        if not paths:
            return all_fns, all_calls, errors

        configured = getattr(self.config.output, "parallel", None)
        if configured is not None and configured >= 1:
            workers = configured
        else:
            workers = min(32, (os.cpu_count() or 1) + 4)
        workers = min(workers, len(paths))

        # Serial fast-path: cheaper than ThreadPoolExecutor overhead for tiny batches.
        if workers <= 1:
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

        with ThreadPoolExecutor(max_workers=workers) as ex:
            future_to_path = {ex.submit(self.parse_file, p): p for p in paths}
            for fut in as_completed(future_to_path):
                path = future_to_path[fut]
                try:
                    fns, calls = fut.result()
                    all_fns.extend(fns)
                    all_calls.extend(calls)
                except Exception as exc:
                    errors.append(f"{path}: {exc}")
                if progress_task:
                    progress, task = progress_task
                    progress.advance(task)

        return all_fns, all_calls, errors
