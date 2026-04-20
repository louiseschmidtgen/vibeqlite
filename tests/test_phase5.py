"""Phase 5: vector clocks + gossip deduplication tests."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from cluster.clock import VectorClock
from cluster.gossip import GossipClient
from node.llm_engine import LLMClient
from node.memory import MemoryDoc
from node.server import app


# ── VectorClock unit tests ───────────────────────────────────────────────────────────────

def test_tick_increments_own_counter():
    vc = VectorClock("a")
    clk = vc.tick()
    assert clk["a"] == 1
    clk2 = vc.tick()
    assert clk2["a"] == 2


def test_merge_takes_max():
    vc = VectorClock("a", initial={"a": 3, "b": 1})
    merged = vc.merge({"a": 1, "b": 5, "c": 2})
    assert merged == {"a": 3, "b": 5, "c": 2}


def test_dominates_true():
    vc = VectorClock("a", initial={"a": 3, "b": 2})
    assert vc.dominates({"a": 2, "b": 1}) is True
    assert vc.dominates({"a": 3, "b": 2}) is True


def test_dominates_false():
    vc = VectorClock("a", initial={"a": 3, "b": 1})
    assert vc.dominates({"a": 3, "b": 2}) is False


# ── MemoryDoc clock persistence ─────────────────────────────────────────────────────

def test_memory_doc_parses_clock_from_header():
    mem = MemoryDoc("saturn")
    assert mem._vector_clock == {}


def test_memory_doc_update_clock_merges():
    mem = MemoryDoc("saturn")
    mem.update_clock({"saturn": 2, "pluton": 1})
    assert mem._vector_clock == {"saturn": 2, "pluton": 1}
    assert '"saturn": 2' in mem.get_text()


def test_memory_doc_update_clock_takes_max():
    mem = MemoryDoc("saturn")
    mem.update_clock({"saturn": 5})
    mem.update_clock({"saturn": 3})  # lower — should not decrease
    assert mem._vector_clock["saturn"] == 5


def test_memory_doc_clock_round_trips_via_text(tmp_path):
    """Clock written to file and reloaded yields same dict."""
    p = tmp_path / "saturn.md"
    mem = MemoryDoc("saturn", path=p)
    mem.update_clock({"saturn": 7, "neptune": 2})
    mem.save()

    mem2 = MemoryDoc("saturn", path=p)
    assert mem2._vector_clock == {"saturn": 7, "neptune": 2}


# ── GossipClient deduplication ───────────────────────────────────────────────

def test_gossip_first_message_not_duplicate():
    from cluster.registry import NodeRegistry
    from unittest.mock import MagicMock
    reg = MagicMock(spec=NodeRegistry)
    reg.peers.return_value = []
    gc = GossipClient(registry=reg, self_id="saturn")
    msg = {"type": "write", "from": "pluton", "vector_clock": {"pluton": 1}}
    assert gc.check_and_mark_seen(msg) is False


def test_gossip_second_identical_message_is_duplicate():
    from cluster.registry import NodeRegistry
    from unittest.mock import MagicMock
    reg = MagicMock(spec=NodeRegistry)
    reg.peers.return_value = []
    gc = GossipClient(registry=reg, self_id="saturn")
    msg = {"type": "write", "from": "pluton", "vector_clock": {"pluton": 1}}
    gc.check_and_mark_seen(msg)
    assert gc.check_and_mark_seen(msg) is True


def test_gossip_different_clock_not_duplicate():
    from cluster.registry import NodeRegistry
    from unittest.mock import MagicMock
    reg = MagicMock(spec=NodeRegistry)
    reg.peers.return_value = []
    gc = GossipClient(registry=reg, self_id="saturn")
    msg1 = {"type": "write", "from": "pluton", "vector_clock": {"pluton": 1}}
    msg2 = {"type": "write", "from": "pluton", "vector_clock": {"pluton": 2}}
    gc.check_and_mark_seen(msg1)
    assert gc.check_and_mark_seen(msg2) is False


# ── Server: /gossip dedup via endpoint ───────────────────────────────────────────

@pytest.fixture()
async def client():
    app.state.node_id = "saturn"
    app.state.cfg = {"context_budget_tokens": 8192}
    app.state.memory = MemoryDoc(node_id="saturn")
    app.state.memory.update_schema_section("## Schema\n- users: id INTEGER, name TEXT")
    app.state.llm = LLMClient("saturn", "default", "llama3.2", "http://localhost:11434")
    app.state.vc = VectorClock("saturn", initial={"saturn": 0})
    app.state.compaction_count = 0
    from cluster.registry import NodeRegistry
    from unittest.mock import MagicMock
    reg = MagicMock(spec=NodeRegistry)
    reg.peers.return_value = []
    from cluster.gossip import GossipClient
    app.state.gossip = GossipClient(registry=reg, self_id="saturn")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_gossip_dedup_returns_duplicate_on_second_delivery(client: AsyncClient):
    """The same write gossip delivered twice: second is a no-op."""
    llm_result = {
        "task_type": "gossip_merge",
        "confidence": "high",
        "explanation": "Merged.",
        "memory_doc_updated": True,
        "updated_table_section": (
            "## Table: users\n| id | name |\n|----|------|\n| 1  | Bob  |"
        ),
    }
    app.state.llm.call = AsyncMock(return_value=llm_result)

    msg = {
        "type": "write",
        "from": "pluton",
        "vector_clock": {"pluton": 1},
        "statement": "INSERT INTO users (name) VALUES ('Bob')",
        "table_name": "users",
        "summary": "Inserted Bob.",
    }

    resp1 = await client.post("/gossip", json=msg)
    assert resp1.status_code == 200
    assert resp1.json().get("duplicate") is not True

    # Second delivery — must be flagged duplicate, LLM not called again
    call_count_before = app.state.llm.call.call_count
    resp2 = await client.post("/gossip", json=msg)
    assert resp2.status_code == 200
    assert resp2.json()["duplicate"] is True
    assert app.state.llm.call.call_count == call_count_before  # no extra LLM call


async def test_gossip_merges_incoming_clock(client: AsyncClient):
    """Incoming gossip clock is merged into node's VectorClock."""
    llm_result = {
        "task_type": "gossip_merge",
        "confidence": "high",
        "explanation": "Merged.",
        "memory_doc_updated": False,
    }
    app.state.llm.call = AsyncMock(return_value=llm_result)

    msg = {
        "type": "write",
        "from": "neptune",
        "vector_clock": {"neptune": 5, "saturn": 0},
        "statement": "INSERT INTO users VALUES (2, 'Carol')",
        "table_name": "users",
        "summary": "",
    }
    await client.post("/gossip", json=msg)
    assert app.state.vc.to_dict().get("neptune") == 5


async def test_query_clock_ticks_on_write(client: AsyncClient):
    """POST /query with a write ticks the vector clock."""
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "write",
        "confidence": "high",
        "explanation": "Inserted Alice.",
        "affected_rows": 1,
        "memory_doc_updated": True,
        "updated_table_section": (
            "## Table: users\n| id | name |\n|----|------|\n| 1  | Alice|"
        ),
    })
    resp = await client.post("/query", json={"sql": "INSERT INTO users (name) VALUES ('Alice')"})
    assert resp.status_code == 200
    assert resp.json()["vector_clock"]["saturn"] == 1
