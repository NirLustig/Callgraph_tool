"""Utility functions used by the sample Python project."""
from typing import Optional


def tokenize(text: str) -> list[str]:
    """Split text into tokens."""
    tokens = text.split()
    return _clean_tokens(tokens)


def _clean_tokens(tokens: list[str]) -> list[str]:
    return [t.strip().lower() for t in tokens if t.strip()]


def parse(text: str) -> dict:
    """Parse a text string and return a structured result."""
    tokens = tokenize(text)
    result = _build_result(tokens)
    return result


def _build_result(tokens: list[str]) -> dict:
    return {"count": len(tokens), "tokens": tokens}


class Renderer:
    """Simple text renderer."""

    def __init__(self, width: int = 80) -> None:
        self.width = width
        self._buffer: list[str] = []

    def render(self, data: dict) -> str:
        """Render the parsed data to a string."""
        self._buffer = []
        self._add_header()
        lines = self._format_tokens(data.get("tokens", []))
        self._buffer.extend(lines)
        return self._flush()

    def _add_header(self) -> None:
        self._buffer.append("=" * self.width)

    def _format_tokens(self, tokens: list[str]) -> list[str]:
        return [f"  [{i}] {t}" for i, t in enumerate(tokens)]

    def _flush(self) -> str:
        output = "\n".join(self._buffer)
        self._buffer = []
        return output
