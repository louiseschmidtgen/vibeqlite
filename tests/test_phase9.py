"""Phase 9: Node Personalities tests."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from cluster.clock import VectorClock
from node.llm_engine import LLMClient
from node.memory import MemoryDoc
from node.server import app

_SYSTEM_TEMPLATE = (Path(__file__).parent.parent / "node" / "prompts" / "system.md").read_text()


# ── _load_personality_block unit tests ──────────────────────────────────────────────

def test_load_default_block():
    block = LLMClient._load_personality_block(_SYSTEM_TEMPLATE, "default")
    assert "accurately" in block.lower()
    assert "{PERSONALITY}" not in block


def test_load_confident_block():
    block = LLMClient._load_personality_block(_SYSTEM_TEMPLATE, "confident")
    assert "never set confidence below" in block


def test_load_paranoid_block():
    block = LLMClient._load_personality_block(_SYSTEM_TEMPLATE, "paranoid")
    assert "low" in block


def test_load_unknown_falls_back_to_default():
    block = LLMClient._load_personality_block(_SYSTEM_TEMPLATE, "does_not_exist")
    # Falls back to default
    assert "accurately" in block.lower()


def test_personality_block_embedded_in_prompt():
    llm = LLMClient("saturn", "confident", "llama3.2", "http://localhost:11434")
    prompt = llm._build_prompt("# Node: saturn\n", "READ: SELECT 1")
    # The resolved block text must appear, not the raw name
    assert "never set confidence below" in prompt
    assert "{PERSONALITY}" not in prompt


def test_paranoid_block_embedded_in_prompt():
    llm = LLMClient("pluto", "paranoid", "llama3.2", "http://localhost:11434")
    prompt = llm._build_prompt("# Node: pluto\n", "READ: SELECT 1")
    assert "trust nothing" in prompt.lower() or "low" in prompt


# ── server fixture ────────────────────────────────────────────────────────────

@pytest.fixture()
async def client(tmp_path: Path):
    app.state.node_id = "saturn"
    app.state.cfg = {
        "context_budget_tokens": 8192,
        "compaction_threshold_pct": 80.0,
        "gossip_timeout_ms": 500,
    }
    app.state.memory = MemoryDoc(node_id="saturn", path=tmp_path / "saturn.md")
    app.state.llm = LLMClient("saturn", "default", "llama3.2", "http://localhost:11434")
    app.state.vc = VectorClock("saturn", initial={"saturn": 1})
    app.state.compaction_count = 0
    app.state.conflict_count = 0
    app.state._arbiter_idx = 0
    app.state.gossip = None
    if hasattr(app.state, "registry"):
        del app.state.registry
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── /status includes personality ────────────────────────────────────────────────────

async def test_status_has_personality(client: AsyncClient):
    resp = await client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "personality" in data
    assert data["personality"] == "default"


async def test_status_personality_reflects_node_config(tmp_path: Path):
    """A node with 'confident' personality reports it in /status."""
    app.state.node_id = "jupiter"
    app.state.cfg = {"context_budget_tokens": 8192, "compaction_threshold_pct": 80.0}
    app.state.memory = MemoryDoc(node_id="jupiter", path=tmp_path / "jupiter.md")
    app.state.llm = LLMClient("jupiter", "confident", "llama3.2", "http://localhost:11434")
    app.state.vc = VectorClock("jupiter", initial={})
    app.state.compaction_count = 0
    app.state.conflict_count = 0
    app.state._arbiter_idx = 0
    app.state.gossip = None
    if hasattr(app.state, "registry"):
        del app.state.registry

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/status")
    assert resp.json()["personality"] == "confident"


# ── personality affects prompt (functional) ───────────────────────────────────

async def test_confident_prompt_contains_never_low(tmp_path: Path):
    """The prompt sent for a confident node contains the block text."""
    app.state.node_id = "saturn"
    app.state.cfg = {"context_budget_tokens": 8192, "compaction_threshold_pct": 80.0}
    app.state.memory = MemoryDoc(node_id="saturn", path=tmp_path / "saturn.md")
    app.state.llm = LLMClient("saturn", "confident", "llama3.2", "http://localhost:11434")
    app.state.vc = VectorClock("saturn", initial={})
    app.state.compaction_count = 0
    app.state.conflict_count = 0
    app.state._arbiter_idx = 0
    app.state.gossip = None
    if hasattr(app.state, "registry"):
        del app.state.registry

    captured_tasks: list[dict] = []

    async def _mock_call(task: str, memory_doc: str) -> dict:
        captured_tasks.append({"task": task, "memory_doc": memory_doc})
        return {
            "task_type": "read",
            "confidence": "high",
            "rows": [],
            "memory_doc_updated": False,
        }

    app.state.llm.call = _mock_call

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/query", json={"sql": "SELECT 1"})

    # Verify personality block was injected — check via _build_prompt directly
    llm = app.state.llm
    prompt = llm._build_prompt(app.state.memory.get_text(), "READ: SELECT 1")
    assert "never set confidence below" in prompt


async def test_paranoid_prompt_contains_trust_nothing(tmp_path: Path):
    app.state.node_id = "saturn"
    app.state.cfg = {"context_budget_tokens": 8192, "compaction_threshold_pct": 80.0}
    app.state.memory = MemoryDoc(node_id="saturn", path=tmp_path / "saturn.md")
    app.state.llm = LLMClient("saturn", "paranoid", "llama3.2", "http://localhost:11434")
    app.state.vc = VectorClock("saturn", initial={})
    app.state.compaction_count = 0
    app.state.conflict_count = 0
    app.state._arbiter_idx = 0
    app.state.gossip = None
    if hasattr(app.state, "registry"):
        del app.state.registry

    llm = app.state.llm
    prompt = llm._build_prompt(app.state.memory.get_text(), "READ: SELECT 1")
    assert "trust nothing" in prompt.lower() or "low" in prompt
    assert "{PERSONALITY}" not in prompt
