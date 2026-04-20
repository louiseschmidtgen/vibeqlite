"""
node/llm_engine.py — LLMClient

Phase 0: classify_sql.
Phase 1: async LLM call via Ollama /api/generate.
Phase 4: build_gossip_task static helper.
Phase 7: build_explain_task, build_arbitrate_task, build_correction_task.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

import httpx

_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"


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
        self._system_template: str = _SYSTEM_PROMPT_PATH.read_text()

    def _build_prompt(self, memory_doc: str, task: str) -> str:
        return (
            self._system_template
            .replace("{NODE_ID}", self.node_id)
            .replace("{PERSONALITY}", self.personality)
            .replace("{MEMORY_DOC}", memory_doc)
            .replace("{TASK}", task)
        )

    async def call(self, task: str, memory_doc: str) -> dict:
        """POST to Ollama /api/generate and return parsed JSON response."""
        prompt = self._build_prompt(memory_doc, task)
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
            )
            resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
        # Strip markdown code fences if model adds them
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.MULTILINE)
            raw = raw.rstrip("`").strip()
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMParseError(f"non-JSON response: {raw[:300]}") from exc
        if "task_type" not in result:
            raise LLMParseError(f"missing task_type: {result}")
        return result

    @staticmethod
    def build_gossip_task(message: dict) -> str:
        """Format incoming write gossip message into a TASK string for the prompt."""
        table = message.get("table_name", "unknown")
        sql = message.get("statement", "")
        summary = message.get("summary", "")
        from_node = message.get("from", "peer")
        return (
            f"GOSSIP_MERGE: peer node '{from_node}' executed the following on table '{table}'.\n"
            f"SQL: {sql}\n"
            f"Summary: {summary}\n"
            "Apply the change to your memory document."
        )

    @staticmethod
    def build_explain_task(sql: str) -> str:
        """Build an EXPLAIN VIBE task for the Argument Protocol (Phase 7)."""
        return (
            f"EXPLAIN VIBE: A consistency check detected that nodes disagree on the "
            f"answer to this query. Explain, based on your memory document, exactly "
            f"what your answer is and why.\n"
            f"SQL: {sql}\n"
            "Respond with JSON: task_type='explain', explanation (string describing "
            "your reasoning), rows (list of answer rows), memory_doc_updated=false."
        )

    @staticmethod
    def build_arbitrate_task(sql: str, explanation_a: str, explanation_b: str) -> str:
        """Build an ARBITRATE task to pick a winner between two node explanations."""
        return (
            f"ARBITRATE: Two nodes in a distributed database disagree on the answer "
            f"to a query. Pick the winner based on the explanations provided.\n"
            f"SQL: {sql}\n"
            f"--- Node A explanation ---\n{explanation_a}\n"
            f"--- Node B explanation ---\n{explanation_b}\n"
            "Respond with JSON: task_type='arbitrate', winner ('A' or 'B'), "
            "explanation (your reasoning), rows (the correct answer rows list), "
            "memory_doc_updated=false."
        )

    @staticmethod
    def build_correction_task(sql: str, winning_rows: list) -> str:
        """Build a CORRECTION task for a node that held the wrong answer."""
        rows_json = json.dumps(winning_rows, indent=2)
        return (
            f"CORRECTION: Cluster consensus determined the correct answer to a query "
            f"differs from what you believe. Update your memory document.\n"
            f"SQL: {sql}\n"
            f"Correct rows:\n{rows_json}\n"
            "Respond with JSON: task_type='correction', memory_doc_updated=true, "
            "updated_table_section (the corrected ## Table: section as a markdown string)."
        )

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
            if isinstance(stmt, exp.Select):
                return "read"
            if isinstance(stmt, (exp.Insert, exp.Update, exp.Delete)):
                return "write"
            # sqlglot.expressions.ddl.DDL is the base for Create/Drop/Alter
            ddl_base = getattr(exp, "DDL", None)
            if ddl_base and isinstance(stmt, ddl_base):
                return "ddl"
            # Fallback: check by class name for older sqlglot versions
            if stmt.__class__.__name__ in ("Create", "Drop", "AlterTable"):
                return "ddl"
            return "unknown"
        except Exception:
            return "unknown"
