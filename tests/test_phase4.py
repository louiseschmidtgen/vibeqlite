"""Phase 4: async write gossip tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from cluster.clock import VectorClock
from node.llm_engine import LLMClient
from node.memory import MemoryDoc
from node.server import app


# ── build_gossip_task unit test ─────────────────────────────────────────────

def test_build_gossip_task_contains_sql():
    msg = {
        "type": "write",
        "from": "pluton",
        "statement": "INSERT INTO users (name) VALUES ('Bob')",
        "table_name": "users",
        "summary": "Inserted Bob.",
    }
    task = LLMClient.build_gossip_task(msg)
    assert "pluton" in task
    assert "INSERT INTO users" in task
    assert "Inserted Bob." in task
    assert "users" in task


def test_build_gossip_task_missing_fields():
    # Must not raise with minimal message
    task = LLMClient.build_gossip_task({"type": "write"})
    assert "GOSSIP_MERGE" in task


# ── server fixture ───────────────────────────────────────────────────────────────────

@pytest.fixture()
async def client():
    app.state.node_id = "saturn"
    app.state.cfg = {"context_budget_tokens": 8192}
    app.state.memory = MemoryDoc(node_id="saturn")
    app.state.memory.update_schema_section("## Schema\n- users: id INTEGER, name TEXT")
    app.state.llm = LLMClient("saturn", "default", "llama3.2", "http://localhost:11434")
    app.state.vc = VectorClock("saturn", initial={"saturn": 0})
    app.state.compaction_count = 0
    app.state.gossip = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── /gossip write handler ───────────────────────────────────────────────

async def test_gossip_write_merges_table(client: AsyncClient):
    llm_result = {
        "task_type": "gossip_merge",
        "confidence": "high",
        "explanation": "Merged Bob from pluton.",
        "memory_doc_updated": True,
        "updated_table_section": (
            "## Table: users\n"
            "| id | name |\n"
            "|----|------|\n"
            "| 1  | Bob  |"
        ),
    }
    app.state.llm.call = AsyncMock(return_value=llm_result)

    resp = await client.post("/gossip", json={
        "type": "write",
        "from": "pluton",
        "statement": "INSERT INTO users (name) VALUES ('Bob')",
        "table_name": "users",
        "summary": "Inserted Bob.",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["merged"] is True
    assert "Bob" in app.state.memory.get_text()


async def test_gossip_write_llm_failure_returns_not_merged(client: AsyncClient):
    from node.llm_engine import LLMParseError
    app.state.llm.call = AsyncMock(side_effect=LLMParseError("bad json"))

    resp = await client.post("/gossip", json={
        "type": "write",
        "from": "pluton",
        "statement": "INSERT INTO users (name) VALUES ('Carol')",
        "table_name": "users",
        "summary": "Inserted Carol.",
    })
    assert resp.status_code == 200
    assert resp.json()["merged"] is False


# ── /query write path fires gossip ────────────────────────────────────────

async def test_query_insert_fires_write_gossip(client: AsyncClient):
    mock_broadcast = AsyncMock()
    app.state.gossip = AsyncMock()
    app.state.gossip.broadcast_write = mock_broadcast

    llm_result = {
        "task_type": "write",
        "confidence": "high",
        "explanation": "Inserted Alice.",
        "affected_rows": 1,
        "memory_doc_updated": True,
        "updated_table_section": (
            "## Table: users\n"
            "| id | name |\n"
            "|----|------|\n"
            "| 1  | Alice|"
        ),
    }
    app.state.llm.call = AsyncMock(return_value=llm_result)

    resp = await client.post("/query", json={
        "sql": "INSERT INTO users (name) VALUES ('Alice')",
        "consistency": "yolo",
    })
    assert resp.status_code == 200
    assert resp.json()["affected_rows"] == 1
    mock_broadcast.assert_called_once()
    gossip_msg = mock_broadcast.call_args[0][0]
    assert gossip_msg["type"] == "write"
    assert "INSERT INTO users" in gossip_msg["statement"]
    assert gossip_msg["summary"] == "Inserted Alice."
