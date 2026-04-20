"""Phase 3: DDL gossip tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from cluster.clock import VectorClock
from cluster.gossip import GossipClient
from cluster.registry import NodeConfig, NodeRegistry
from node.llm_engine import LLMClient
from node.memory import MemoryDoc
from node.server import app


# ── GossipClient unit tests ───────────────────────────────────────────────────────────────

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


async def test_broadcast_ddl_calls_all_peers(registry):
    gc = GossipClient(registry, "saturn")
    with patch.object(gc, "_post_gossip", new=AsyncMock(return_value=True)) as mock_post:
        result = await gc.broadcast_ddl({"type": "ddl_change", "schema_snapshot": "## Schema\n- t: id INT"})
    assert mock_post.call_count == 1  # 1 peer (pluton)
    assert result == {"pluton": True}


async def test_broadcast_ddl_peer_failure(registry):
    gc = GossipClient(registry, "saturn")
    with patch.object(gc, "_post_gossip", new=AsyncMock(return_value=False)):
        result = await gc.broadcast_ddl({"type": "ddl_change"})
    assert result == {"pluton": False}


def test_peers_excludes_self(registry):
    peers = registry.peers()
    assert all(p.id != "saturn" for p in peers)
    assert len(peers) == 1


# ── /gossip endpoint tests ─────────────────────────────────────────────────────────────

@pytest.fixture()
async def client():
    """Wire app.state directly."""
    app.state.node_id = "saturn"
    app.state.cfg = {"context_budget_tokens": 8192}
    app.state.memory = MemoryDoc(node_id="saturn")
    app.state.llm = LLMClient("saturn", "default", "llama3.2", "http://localhost:11434")
    app.state.vc = VectorClock("saturn", initial={"saturn": 0})
    app.state.compaction_count = 0
    app.state.conflict_count = 0
    # gossip client with no peers (no config needed for endpoint tests)
    app.state.gossip = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_gossip_ddl_updates_schema(client: AsyncClient):
    resp = await client.post("/gossip", json={
        "type": "ddl_change",
        "from": "pluton",
        "schema_snapshot": "## Schema\n- users: id INTEGER, name TEXT",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert "users: id INTEGER" in app.state.memory.get_text()


async def test_gossip_unknown_type_ok(client: AsyncClient):
    resp = await client.post("/gossip", json={"type": "unknown_future_type"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ── /query DDL path broadcasts gossip ────────────────────────────────────────────

async def test_query_ddl_broadcasts_to_peers(client: AsyncClient):
    mock_gossip = AsyncMock(return_value={"pluton": True})
    app.state.gossip = AsyncMock()
    app.state.gossip.broadcast_ddl = mock_gossip

    llm_result = {
        "task_type": "ddl",
        "confidence": "high",
        "explanation": "Created users table.",
        "memory_doc_updated": True,
        "updated_schema_section": "## Schema\n- users: id INTEGER, name TEXT",
    }
    app.state.llm.call = AsyncMock(return_value=llm_result)

    resp = await client.post("/query", json={
        "sql": "CREATE TABLE users (id INTEGER, name TEXT)",
        "consistency": "yolo",
    })
    assert resp.status_code == 200
    assert resp.json()["peer_acks"] == {"pluton": True}
    mock_gossip.assert_called_once()
    gossip_msg = mock_gossip.call_args[0][0]
    assert gossip_msg["type"] == "ddl_change"
    assert "## Schema" in gossip_msg["schema_snapshot"]
