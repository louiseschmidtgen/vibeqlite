"""
node/memory.py — MemoryDoc

Phase 0: stub with class definition only.
Full implementation in Phase 1 (in-memory) and Phase 2 (file-backed).
"""
from __future__ import annotations

from pathlib import Path


class MemoryDoc:
    """Holds a node's memory document as a markdown string."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._text: str = ""

    def get_text(self) -> str:
        return self._text

    def token_estimate(self, model: str = "gpt-3.5-turbo") -> int:
        try:
            import tiktoken
            enc = tiktoken.encoding_for_model(model)
            return len(enc.encode(self._text))
        except Exception:
            return len(self._text) // 4
