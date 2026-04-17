"""
node/llm_engine.py — LLMClient

Phase 0: stub with class definition and SQL classifier only.
Full implementation in Phase 1.
"""
from __future__ import annotations

from typing import Literal


class LLMParseError(Exception):
    """Raised when the LLM returns something that cannot be parsed as JSON."""


class LLMClient:
    """Wraps an Ollama-compatible LLM backend."""

    def __init__(
        self,
        node_id: str,
        personality: str,
        model: str,
        base_url: str,
    ) -> None:
        self.node_id = node_id
        self.personality = personality
        self.model = model
        self.base_url = base_url.rstrip("/")

    def classify_sql(
        self, sql: str
    ) -> Literal["read", "write", "ddl", "unknown"]:
        """Classify SQL intent using sqlglot. No LLM call."""
        import sqlglot
        from sqlglot import expressions as exp

        try:
            statements = sqlglot.parse(sql)
            if not statements:
                return "unknown"
            stmt = statements[0]
            if isinstance(stmt, (exp.Select,)):
                return "read"
            if isinstance(stmt, (exp.Insert, exp.Update, exp.Delete)):
                return "write"
            if isinstance(
                stmt,
                (exp.Create, exp.Drop, exp.AlterTable),
            ):
                return "ddl"
            return "unknown"
        except Exception:
            return "unknown"
