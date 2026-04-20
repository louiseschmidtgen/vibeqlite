"""Phase 6: VIBE_CHECK consistency mode tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from cluster.clock import VectorClock
from cluster.gossip import GossipClient
from cluster.registry import NodeConfig, NodeRegistry
from node.llm_engine import LLMClient
from node.memory import MemoryDoc
from node.server import app


# ── broadcast_read unit tests ─────────────────────────────────────────────────

@pytest.fixture()
def registry(tmp_path):
    import yaml
    cfg = {
        "nodes": [
            {"id": "saturn", "url": "http://localhost:8001", "personality": "default"},
            {"id": "pluton", "url": "http://localhost:8002", "personality": "confident"},
        ]
    }
    p = tmp_path / "cluster.yaml"
    p.write_text(yaml.dump(cfg))
    return NodeRegistry(p, "saturn")


async def test_broadcast_read_returns_peer_responses(registry):
    gc = GossipClient(registry, "saturn")

    import httpx
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"rows": [{"id": 1, "name": "Alice"}], "node_id": "pluton"}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        results = await gc.broadcast_read("SELECT * FROM users", timeout_ms=1000)

    assert len(results) == 1
    assert results[0]["_from_node"] == "pluton"
    assert results[0]["rows"] == [{"id": 1, "name": "Alice"}]


async def test_broadcast_read_excludes_failed_peer(registry):
    gc = GossipClient(registry, "saturn")

    import httpx
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client_cls.return_value = mock_client

        results = await gc.broadcast_read("SELECT * FROM users", timeout_ms=100)

    assert results == []


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
    app.state.gossip = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── status exposes conflict_count ─────────────────────────────────────────────

async def test_status_includes_conflict_count(client: AsyncClient):
    resp = await client.get("/status")
    assert resp.status_code == 200
    assert resp.json()["conflict_count"] == 0


# ── vibe_check: unanimous (no peers) ─────────────────────────────────────────

async def test_vibe_check_no_peers_returns_self_answer(client: AsyncClient):
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "read",
        "confidence": "high",
        "rows": [{"id": 1, "name": "Alice"}],
        "explanation": "Found Alice.",
        "memory_doc_updated": False,
    })

    resp = await client.post("/query", json={
        "sql": "SELECT * FROM users",
        "consistency": "vibe_check",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["rows"] == [{"id": 1, "name": "Alice"}]
    assert data["vibe_error"] is None
    assert app.state.conflict_count == 0


# ── vibe_check: majority wins ─────────────────────────────────────────────────

async def test_vibe_check_majority_wins(client: AsyncClient):
    # Self returns Alice; 2 peers return Alice; 1 peer returns Bob → Alice wins
    alice_rows = [{"id": 1, "name": "Alice"}]
    bob_rows = [{"id": 1, "name": "Bob"}]

    app.state.llm.call = AsyncMock(return_value={
        "task_type": "read",
        "confidence": "high",
        "rows": alice_rows,
        "explanation": "Alice.",
        "memory_doc_updated": False,
    })

    mock_gossip = MagicMock(spec=GossipClient)
    mock_gossip.broadcast_read = AsyncMock(return_value=[
        {"_from_node": "pluton", "rows": alice_rows, "vector_clock": {}},
        {"_from_node": "neptune", "rows": bob_rows, "vector_clock": {}},
    ])
    app.state.gossip = mock_gossip

    resp = await client.post("/query", json={
        "sql": "SELECT * FROM users",
        "consistency": "vibe_check",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["rows"] == alice_rows
    assert data["vibe_error"]["type"] == "CONFLICT"
    assert app.state.conflict_count == 1


# ── vibe_check: no majority → NO_MAJORITY ─────────────────────────────────────

async def test_vibe_check_no_majority(client: AsyncClient):
    # Self + 1 peer = 2 nodes; each disagrees → no majority of 2 needed (floor(2/2)+1 = 2)
    alice_rows = [{"id": 1, "name": "Alice"}]
    bob_rows = [{"id": 1, "name": "Bob"}]

    app.state.llm.call = AsyncMock(return_value={
        "task_type": "read",
        "confidence": "medium",
        "rows": alice_rows,
        "explanation": "Alice or Bob?",
        "memory_doc_updated": False,
    })

    mock_gossip = MagicMock(spec=GossipClient)
    mock_gossip.broadcast_read = AsyncMock(return_value=[
        {"_from_node": "pluton", "rows": bob_rows, "vector_clock": {}},
    ])
    app.state.gossip = mock_gossip

    resp = await client.post("/query", json={
        "sql": "SELECT * FROM users",
        "consistency": "vibe_check",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["vibe_error"]["type"] == "NO_MAJORITY"


# ── internal=true skips vibe_check fan-out ────────────────────────────────────

async def test_internal_query_skips_vibe_check(client: AsyncClient):
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "read",
        "confidence": "high",
        "rows": [{"id": 1, "name": "Alice"}],
        "explanation": "Alice.",
        "memory_doc_updated": False,
    })

    # Even though consistency=vibe_check, internal=true should bypass fan-out
    mock_gossip = MagicMock(spec=GossipClient)
    mock_gossip.broadcast_read = AsyncMock(return_value=[])
    app.state.gossip = mock_gossip

    resp = await client.post(
        "/query?internal=true",
        json={"sql": "SELECT * FROM users", "consistency": "vibe_check"},
    )
    assert resp.status_code == 200
    mock_gossip.broadcast_read.assert_not_called()
