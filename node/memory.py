"""
node/memory.py — MemoryDoc

Phase 1: in-memory document with section update helpers.
Phase 2 adds file-backed persistence.
"""
from __future__ import annotations

import re
from pathlib import Path


def _blank_doc(node_id: str) -> str:
    return (
        f"# Node: {node_id}\n"
        "# Vector Clock: {}\n"
        "# Compaction Count: 0\n\n"
        "## Schema\n"
        "(no tables yet)\n"
    )


class MemoryDoc:
    """Holds a node's memory document as a markdown string."""

    def __init__(self, node_id: str, path: Path | None = None) -> None:
        self.node_id = node_id
        self.path = path
        if path and path.exists():
            self._text = path.read_text()
        else:
            self._text = _blank_doc(node_id)

    def get_text(self) -> str:
        return self._text

    def update_table_section(self, table_name: str, new_section: str) -> None:
        """Replace ## Table: <name> block; append if absent."""
        pattern = re.compile(
            rf"## Table: {re.escape(table_name)}[\s\S]*?(?=\n##|\Z)"
        )
        new_section = new_section.rstrip("\n")
        if pattern.search(self._text):
            self._text = pattern.sub(new_section, self._text)
        else:
            self._text = self._text.rstrip("\n") + "\n\n" + new_section + "\n"

    def update_schema_section(self, new_section: str) -> None:
        """Replace ## Schema block."""
        pattern = re.compile(r"## Schema[\s\S]*?(?=\n##|\Z)")
        new_section = new_section.rstrip("\n")
        if pattern.search(self._text):
            self._text = pattern.sub(new_section, self._text)
        else:
            self._text = self._text.rstrip("\n") + "\n\n" + new_section + "\n"

    def token_estimate(self, model: str = "gpt-3.5-turbo") -> int:
        try:
            import tiktoken
            enc = tiktoken.encoding_for_model(model)
            return len(enc.encode(self._text))
        except Exception:
            return len(self._text) // 4
