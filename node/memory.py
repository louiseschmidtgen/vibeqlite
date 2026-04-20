"""
node/memory.py — MemoryDoc

Phase 1: in-memory document with section update helpers.
Phase 2: atomic file-backed persistence.
Phase 5: vector clock stored in header.
"""
from __future__ import annotations

import json
import os
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
        self._vector_clock: dict[str, int] = self._parse_clock()

    def _parse_clock(self) -> dict[str, int]:
        """Extract vector clock dict from the memory doc header."""
        m = re.search(r"# Vector Clock: (\{[^}]*\})", self._text)
        if m:
            try:
                return json.loads(m.group(1))
            except (json.JSONDecodeError, ValueError):
                pass
        return {}

    def update_clock(self, clock: dict[str, int]) -> None:
        """Merge incoming clock into header (take max per component)."""
        merged: dict[str, int] = dict(self._vector_clock)
        for k, v in clock.items():
            merged[k] = max(merged.get(k, 0), v)
        self._vector_clock = merged
        new_header = f"# Vector Clock: {json.dumps(self._vector_clock, sort_keys=True)}"
        self._text = re.sub(r"# Vector Clock: \{[^}]*\}", new_header, self._text)

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

    def save(self) -> None:
        """Atomically persist to self.path (write .tmp then rename)."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(self._text, encoding="utf-8")
        os.replace(tmp, self.path)

    def token_estimate(self, model: str = "gpt-3.5-turbo") -> int:
        try:
            import tiktoken
            enc = tiktoken.encoding_for_model(model)
            return len(enc.encode(self._text))
        except Exception:
            return len(self._text) // 4
