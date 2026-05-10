"""Abstract base renderer."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..config import Config
from ..models import CallGraph


class BaseRenderer(ABC):
    def __init__(self, config: Config) -> None:
        self.config = config

    @abstractmethod
    def render(self, graph: CallGraph, output_path: Path) -> Path:
        """Render graph to file(s). Returns the primary output path."""
        ...
