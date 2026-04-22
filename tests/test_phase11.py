"""Phase 11: CALL ELECTION tests."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from cluster.clock import VectorClock
from cluster.registry import NodeRegistry
from node.llm_engine import LLMClient
from node.memory import MemoryDoc
from node.server import app


# ── build_election_task ──────────────────────────────────────────────────────

def test_build_election_task_content():
    task = LLMClient.build_election_task()
    assert "election" in task.lower()
    assert "candidate" in task.lower()
    assert "case" in task.lower()


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
    app.state.memory.save()
    app.state.llm = LLMClient("saturn", "default", "llama3.2", "http://localhost:11434")
    app.state.vc = VectorClock("saturn", initial={"saturn": 1})
    app.state.compaction_count = 0
    app.state.conflict_count = 0
    app.state._arbiter_idx = 0
    app.state.gossip = None
    app.state.is_leader = False
    if hasattr(app.state, "registry"):
        del app.state.registry
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── POST /election ───────────────────────────────────────────────────────────

async def test_election_endpoint_returns_case(client: AsyncClient):
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "election",
        "candidate": "saturn",
        "case": "I have the most consistent data in the cluster.",
    })
    resp = await client.post("/election")
    assert resp.status_code == 200
    data = resp.json()
    assert data["node_id"] == "saturn"
    assert data["task_type"] == "election"
    assert "consistent" in data["case"]
    assert data["candidate"] == "saturn"


async def test_election_endpoint_llm_parse_error_returns_fallback(client: AsyncClient):
    from node.llm_engine import LLMParseError
    app.state.llm.call = AsyncMock(side_effect=LLMParseError("bad json"))
    resp = await client.post("/election")
    assert resp.status_code == 200
    data = resp.json()
    assert data["node_id"] == "saturn"
    assert data["task_type"] == "election"
    assert "candidate" in data


# ── CALL ELECTION single node ──────────────────────────────────────────────

async def test_call_election_self_only_self_wins(client: AsyncClient):
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "election",
        "candidate": "saturn",
        "case": "I am the sole node.",
    })
    resp = await client.post("/query", json={"sql": "CALL ELECTION"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["winner"] == "saturn"
    assert app.state.is_leader is True
    rows = data["rows"]
    assert len(rows) == 1
    assert rows[0]["winner"] is True
    assert rows[0]["node_id"] == "saturn"


async def test_call_election_case_insensitive(client: AsyncClient):
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "election", "candidate": "saturn", "case": "yes",
    })
    resp = await client.post("/query", json={"sql": "call election"})
    assert resp.status_code == 200
    assert resp.json()["winner"] == "saturn"


async def test_call_election_llm_error_still_returns(client: AsyncClient):
    from node.llm_engine import LLMParseError
    app.state.llm.call = AsyncMock(side_effect=LLMParseError("bad"))
    resp = await client.post("/query", json={"sql": "CALL ELECTION"})
    assert resp.status_code == 200
    # Saturn wins by default (only candidate, even with empty case)
    assert resp.json()["winner"] == "saturn"


# ── CALL ELECTION with peers ────────────────────────────────────────────────

async def test_call_election_peer_wins_with_longer_case(client: AsyncClient, tmp_path: Path):
    # Self case is short, peer case is much longer
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "election", "candidate": "saturn", "case": "short",
    })

    mock_gossip = MagicMock()
    mock_gossip.broadcast_election = AsyncMock(return_value=[{
        "_from_node": "pluton",
        "node_id": "pluton",
        "task_type": "election",
        "candidate": "pluton",
        "case": "I have processed far more writes and my memory document is complete and verified.",
    }])
    mock_gossip.announce_winner = AsyncMock()
    app.state.gossip = mock_gossip

    resp = await client.post("/query", json={"sql": "CALL ELECTION"})
    app.state.gossip = None

    assert resp.status_code == 200
    data = resp.json()
    assert data["winner"] == "pluton"
    assert app.state.is_leader is False
    assert len(data["rows"]) == 2


async def test_call_election_self_wins_with_longer_case(client: AsyncClient):
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "election",
        "candidate": "saturn",
        "case": "I have the most comprehensive and consistent dataset in the entire cluster.",
    })

    mock_gossip = MagicMock()
    mock_gossip.broadcast_election = AsyncMock(return_value=[{
        "_from_node": "pluton",
        "node_id": "pluton",
        "task_type": "election",
        "candidate": "pluton",
        "case": "short",
    }])
    mock_gossip.announce_winner = AsyncMock()
    app.state.gossip = mock_gossip

    resp = await client.post("/query", json={"sql": "CALL ELECTION"})
    app.state.gossip = None

    assert resp.status_code == 200
    data = resp.json()
    assert data["winner"] == "saturn"
    assert app.state.is_leader is True


async def test_call_election_tie_break_alphabetical(client: AsyncClient):
    # Same length case — "apollo" < "saturn" alphabetically, so apollo wins
    same_case = "x" * 50
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "election", "candidate": "saturn", "case": same_case,
    })

    mock_gossip = MagicMock()
    mock_gossip.broadcast_election = AsyncMock(return_value=[{
        "_from_node": "apollo",
        "node_id": "apollo",
        "task_type": "election",
        "candidate": "apollo",
        "case": same_case,
    }])
    mock_gossip.announce_winner = AsyncMock()
    app.state.gossip = mock_gossip

    resp = await client.post("/query", json={"sql": "CALL ELECTION"})
    app.state.gossip = None

    assert resp.status_code == 200
    assert resp.json()["winner"] == "apollo"


# ── election_result gossip ──────────────────────────────────────────────────────

async def test_gossip_election_result_sets_is_leader(client: AsyncClient):
    app.state.is_leader = False
    resp = await client.post("/gossip", json={
        "type": "election_result",
        "from": "pluton",
        "winner": "saturn",
        "vector_clock": {},
    })
    assert resp.status_code == 200
    assert resp.json()["winner"] == "saturn"
    assert app.state.is_leader is True


async def test_gossip_election_result_not_leader(client: AsyncClient):
    app.state.is_leader = True
    resp = await client.post("/gossip", json={
        "type": "election_result",
        "from": "pluton",
        "winner": "pluton",
        "vector_clock": {},
    })
    assert resp.status_code == 200
    assert app.state.is_leader is False


# ── /status includes is_leader ───────────────────────────────────────────────

async def test_status_includes_is_leader_false(client: AsyncClient):
    app.state.is_leader = False
    resp = await client.get("/status")
    assert resp.status_code == 200
    assert resp.json()["is_leader"] is False


async def test_status_includes_is_leader_true(client: AsyncClient):
    app.state.is_leader = True
    resp = await client.get("/status")
    assert resp.status_code == 200
    assert resp.json()["is_leader"] is True
