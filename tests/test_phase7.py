"""Phase 7: Argument Protocol tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from cluster.clock import VectorClock
from cluster.gossip import GossipClient
from cluster.registry import NodeRegistry
from node.llm_engine import LLMClient
from node.memory import MemoryDoc
from node.server import app


# ── task builder unit tests ──────────────────────────────────────────────

def test_build_explain_task_contains_sql():
    task = LLMClient.build_explain_task("SELECT * FROM users")
    assert "SELECT * FROM users" in task
    assert "EXPLAIN VIBE" in task


def test_build_arbitrate_task_contains_both_explanations():
    task = LLMClient.build_arbitrate_task(
        "SELECT * FROM users",
        "I see Alice in my memory.",
        "I see Bob in my memory.",
    )
    assert "SELECT * FROM users" in task
    assert "I see Alice in my memory." in task
    assert "I see Bob in my memory." in task
    assert "ARBITRATE" in task


def test_build_correction_task_contains_rows():
    rows = [{"id": 1, "name": "Alice"}]
    task = LLMClient.build_correction_task("SELECT * FROM users", rows)
    assert "CORRECTION" in task
    assert "Alice" in task
    assert "SELECT * FROM users" in task


# ── server fixture ────────────────────────────────────────────────────────────

@pytest.fixture()
async def client():
    app.state.node_id = "saturn"
    app.state.cfg = {"context_budget_tokens": 8192, "gossip_timeout_ms": 500}
    app.state.memory = MemoryDoc(node_id="saturn")
    app.state.memory.update_schema_section("## Schema\n- users: id INTEGER, name TEXT")
    app.state.memory.update_table_section(
        "users",
        "## Table: users\n| id | name |\n|----|------|\n| 1  | Alice|",
    )
    app.state.llm = LLMClient("saturn", "default", "llama3.2", "http://localhost:11434")
    app.state.vc = VectorClock("saturn", initial={"saturn": 1})
    app.state.compaction_count = 0
    app.state.conflict_count = 0
    app.state._arbiter_idx = 0
    app.state.gossip = None
    # No registry → argument_protocol skipped (Phase 6 compat)
    if hasattr(app.state, "registry"):
        del app.state.registry
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture()
async def client_with_registry(tmp_path):
    """Fixture with a 3-node registry to enable argument_protocol."""
    cfg = {
        "nodes": [
            {"id": "saturn", "url": "http://localhost:8001", "personality": "default"},
            {"id": "pluton", "url": "http://localhost:8002", "personality": "confident"},
            {"id": "neptune", "url": "http://localhost:8003", "personality": "skeptical"},
        ]
    }
    p = tmp_path / "cluster.yaml"
    p.write_text(yaml.dump(cfg))
    registry = NodeRegistry(p, "saturn")

    app.state.node_id = "saturn"
    app.state.cfg = {"context_budget_tokens": 8192, "gossip_timeout_ms": 500}
    app.state.memory = MemoryDoc(node_id="saturn")
    app.state.memory.update_schema_section("## Schema\n- users: id INTEGER, name TEXT")
    app.state.memory.update_table_section(
        "users",
        "## Table: users\n| id | name |\n|----|------|\n| 1  | Alice|",
    )
    app.state.llm = LLMClient("saturn", "default", "llama3.2", "http://localhost:11434")
    app.state.vc = VectorClock("saturn", initial={"saturn": 1})
    app.state.compaction_count = 0
    app.state.conflict_count = 0
    app.state._arbiter_idx = 0
    app.state.registry = registry
    mock_gossip = MagicMock(spec=GossipClient)
    mock_gossip._post_gossip = AsyncMock(return_value=True)
    app.state.gossip = mock_gossip
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── EXPLAIN VIBE via /query ─────────────────────────────────────────────

async def test_explain_vibe_query_returns_explanation(client: AsyncClient):
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "explain",
        "confidence": "high",
        "rows": [{"id": 1, "name": "Alice"}],
        "explanation": "Alice is in my memory doc.",
        "memory_doc_updated": False,
    })
    resp = await client.post(
        "/query?internal=true",
        json={"sql": "EXPLAIN VIBE SELECT * FROM users", "consistency": "yolo"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["rows"] == [{"id": 1, "name": "Alice"}]
    assert data["vibe_error"] is None


# ── /arbitrate endpoint ──────────────────────────────────────────────

async def test_arbitrate_endpoint_returns_winner(client: AsyncClient):
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "arbitrate",
        "winner": "A",
        "explanation": "Alice explanation is more consistent with schema.",
        "rows": [{"id": 1, "name": "Alice"}],
        "memory_doc_updated": False,
    })
    resp = await client.post("/arbitrate", json={
        "sql": "SELECT * FROM users",
        "explanation_a": "I see Alice.",
        "explanation_b": "I see Bob.",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["winner"] == "A"
    assert data["rows"] == [{"id": 1, "name": "Alice"}]
    assert "explanation" in data


async def test_arbitrate_defaults_to_a_on_parse_error(client: AsyncClient):
    from node.llm_engine import LLMParseError
    app.state.llm.call = AsyncMock(side_effect=LLMParseError("bad json"))
    resp = await client.post("/arbitrate", json={
        "sql": "SELECT * FROM users",
        "explanation_a": "A explanation",
        "explanation_b": "B explanation",
    })
    assert resp.status_code == 200
    assert resp.json()["winner"] == "A"


# ── /gossip correction handler ─────────────────────────────────────────────

async def test_gossip_correction_updates_memory(client: AsyncClient):
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "correction",
        "memory_doc_updated": True,
        "updated_table_section": (
            "## Table: users\n"
            "| id | name |\n"
            "|----|------|\n"
            "| 1  | Alice|"
        ),
    })
    resp = await client.post("/gossip", json={
        "type": "correction",
        "from": "arbiter",
        "vector_clock": {"arbiter": 5},
        "sql": "SELECT * FROM users",
        "winning_rows": [{"id": 1, "name": "Alice"}],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["corrected"] is True
    assert "Alice" in app.state.memory.get_text()


async def test_gossip_correction_no_rows_returns_not_corrected(client: AsyncClient):
    resp = await client.post("/gossip", json={
        "type": "correction",
        "from": "arbiter",
        "sql": "SELECT * FROM users",
        # no winning_rows
    })
    assert resp.status_code == 200
    assert resp.json()["corrected"] is False


# ── argument_protocol integration ─────────────────────────────────────────

async def test_vibe_check_conflict_triggers_argument_protocol(client_with_registry: AsyncClient):
    """When vibe_check detects a conflict and registry is set, argument_protocol fires."""
    alice = [{"id": 1, "name": "Alice"}]
    bob = [{"id": 1, "name": "Bob"}]

    # gossip.broadcast_read: pluton agrees (alice), neptune disagrees (bob)
    app.state.gossip.broadcast_read = AsyncMock(return_value=[
        {"_from_node": "pluton", "rows": alice, "vector_clock": {}},
        {"_from_node": "neptune", "rows": bob, "vector_clock": {}},
    ])

    # LLM calls in order:
    # 1. vibe_check self-read (returns alice)
    # 2. argument_protocol: EXPLAIN VIBE self
    app.state.llm.call = AsyncMock(side_effect=[
        {
            "task_type": "read",
            "confidence": "high",
            "rows": alice,
            "explanation": "Alice found in table.",
            "memory_doc_updated": False,
        },
        {
            "task_type": "explain",
            "confidence": "high",
            "rows": alice,
            "explanation": "I have Alice in my memory.",
            "memory_doc_updated": False,
        },
    ])

    # Mock httpx for: (a) neptune EXPLAIN VIBE call, (b) pluton /arbitrate call
    arb_response = MagicMock()
    arb_response.status_code = 200
    arb_response.json.return_value = {
        "node_id": "pluton",
        "winner": "A",
        "explanation": "Alice is correct.",
        "rows": alice,
    }
    explain_response = MagicMock()
    explain_response.status_code = 200
    explain_response.json.return_value = {
        "node_id": "neptune",
        "rows": [{"id": 1, "name": "Bob"}],
        "explanation": "Bob is in my memory.",
        "vibe_error": None,
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        # First call: neptune EXPLAIN VIBE → second call: pluton /arbitrate
        mock_http.post = AsyncMock(side_effect=[explain_response, arb_response])
        mock_cls.return_value = mock_http

        resp = await client_with_registry.post("/query", json={
            "sql": "SELECT * FROM users",
            "consistency": "vibe_check",
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["rows"] == alice
    assert data["vibe_error"]["type"] == "CONFLICT"
    assert data["vibe_error"]["resolution"] == "argument_protocol"
    assert "neptune" in data["vibe_error"]["minority_nodes"]
    assert app.state.conflict_count == 1
    # Correction gossip was scheduled for neptune
    app.state.gossip._post_gossip.assert_called()


async def test_vibe_check_without_registry_returns_plain_conflict(client: AsyncClient):
    """Without a registry, vibe_check returns the Phase 6 CONFLICT (no argument_protocol)."""
    alice = [{"id": 1, "name": "Alice"}]
    bob = [{"id": 1, "name": "Bob"}]

    app.state.llm.call = AsyncMock(return_value={
        "task_type": "read",
        "confidence": "high",
        "rows": alice,
        "explanation": "Alice.",
        "memory_doc_updated": False,
    })

    mock_gossip = MagicMock(spec=GossipClient)
    mock_gossip.broadcast_read = AsyncMock(return_value=[
        {"_from_node": "pluton", "rows": alice, "vector_clock": {}},
        {"_from_node": "neptune", "rows": bob, "vector_clock": {}},
    ])
    app.state.gossip = mock_gossip

    resp = await client.post("/query", json={
        "sql": "SELECT * FROM users",
        "consistency": "vibe_check",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["vibe_error"]["type"] == "CONFLICT"
    # No resolution key means argument_protocol was not triggered
    assert data["vibe_error"].get("resolution") is None
