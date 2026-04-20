"""Phase 8: Compaction tests."""
from __future__ import annotations

from pathlib import Path
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


# ── MemoryDoc unit tests ──────────────────────────────────────────────────────

def test_needs_compaction_false_when_small():
    mem = MemoryDoc("saturn")
    # Default blank doc is tiny — should not need compaction at 80%
    assert mem.needs_compaction(budget_tokens=8192, threshold_pct=80.0) is False


def test_needs_compaction_true_when_large():
    mem = MemoryDoc("saturn")
    # Manually bloat the doc past the threshold
    big_table = "## Table: users\n" + "\n".join(
        f"| {i} | {'Alice' * 40} |" for i in range(200)
    )
    mem.update_table_section("users", big_table)
    # Very low budget → should trigger
    assert mem.needs_compaction(budget_tokens=100, threshold_pct=1.0) is True


def test_backup_pre_compact(tmp_path):
    mem = MemoryDoc("saturn", path=tmp_path / "saturn.md")
    mem.save()
    mem.backup_pre_compact()
    backup = tmp_path / "saturn.pre-compact.md"
    assert backup.exists()
    assert backup.read_text() == mem.get_text()


def test_backup_pre_compact_no_path():
    mem = MemoryDoc("saturn")  # no path
    mem.backup_pre_compact()  # must not raise


def test_increment_compaction_count_from_zero():
    mem = MemoryDoc("saturn")
    count = mem.increment_compaction_count()
    assert count == 1
    assert "# Compaction Count: 1" in mem.get_text()


def test_increment_compaction_count_repeated():
    mem = MemoryDoc("saturn")
    mem.increment_compaction_count()
    count = mem.increment_compaction_count()
    assert count == 2
    assert mem.get_text().count("# Compaction Count:") == 1
    assert "# Compaction Count: 2" in mem.get_text()


def test_build_compaction_task_normal():
    task = LLMClient.build_compaction_task(aggressive=False)
    assert "COMPACT MEMORY" in task
    assert "AGGRESSIVE" not in task


def test_build_compaction_task_aggressive():
    task = LLMClient.build_compaction_task(aggressive=True)
    assert "AGGRESSIVE" in task


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
    app.state.memory.update_schema_section("## Schema\n- users: id INTEGER, name TEXT")
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


# ── POST /compact endpoint ─────────────────────────────────────────────────────

async def test_compact_endpoint_runs_compaction(client: AsyncClient):
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "compaction",
        "memory_doc_updated": True,
        "compacted_sections": [
            {
                "table_name": "users",
                "updated_table_section": (
                    "## Table: users\n"
                    "| id | name |\n"
                    "|----|------|\n"
                    "| 1  | Alice| (summarised)"
                ),
            }
        ],
    })
    resp = await client.post("/compact", json={"aggressive": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["compaction_count"] == 1
    assert app.state.compaction_count == 1
    assert "summarised" in app.state.memory.get_text()


async def test_compact_endpoint_aggressive(client: AsyncClient):
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "compaction",
        "memory_doc_updated": True,
        "compacted_sections": [],
    })
    resp = await client.post("/compact", json={"aggressive": True})
    assert resp.status_code == 200
    # Check aggressive flag was passed to LLM
    call_task: str = app.state.llm.call.call_args[1]["task"]
    assert "AGGRESSIVE" in call_task


async def test_compact_llm_failure_leaves_count_unchanged(client: AsyncClient):
    from node.llm_engine import LLMParseError
    app.state.llm.call = AsyncMock(side_effect=LLMParseError("bad json"))
    resp = await client.post("/compact", json={})
    assert resp.status_code == 200
    assert app.state.compaction_count == 0


async def test_compact_creates_backup(client: AsyncClient, tmp_path: Path):
    app.state.memory.save()  # ensure file exists
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "compaction",
        "memory_doc_updated": True,
        "compacted_sections": [],
    })
    await client.post("/compact", json={})
    assert (tmp_path / "saturn.pre-compact.md").exists()


# ── compaction gossip broadcast ────────────────────────────────────────────────

async def test_compact_broadcasts_to_peers(client: AsyncClient):
    mock_gossip = MagicMock(spec=GossipClient)
    mock_gossip.broadcast_compaction = AsyncMock()
    app.state.gossip = mock_gossip

    app.state.llm.call = AsyncMock(return_value={
        "task_type": "compaction",
        "memory_doc_updated": True,
        "compacted_sections": [],
    })
    await client.post("/compact", json={})
    mock_gossip.broadcast_compaction.assert_called_once()
    msg = mock_gossip.broadcast_compaction.call_args[0][0]
    assert msg["type"] == "compaction"
    assert msg["from"] == "saturn"
    assert msg["compaction_count"] == 1


# ── /gossip compaction type ────────────────────────────────────────────────────

async def test_gossip_compaction_is_noted(client: AsyncClient):
    resp = await client.post("/gossip", json={
        "type": "compaction",
        "from": "pluton",
        "compaction_count": 3,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["noted"] is True
    # Peer's own compaction_count is NOT changed
    assert app.state.compaction_count == 0


# ── COMPACT MEMORY ON <node> command ──────────────────────────────────────────

async def test_compact_memory_on_self(client: AsyncClient):
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "compaction",
        "memory_doc_updated": True,
        "compacted_sections": [],
    })
    resp = await client.post("/query", json={"sql": "COMPACT MEMORY ON saturn"})
    assert resp.status_code == 200
    assert resp.json()["compaction_count"] == 1


async def test_compact_memory_on_self_aggressive(client: AsyncClient):
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "compaction",
        "memory_doc_updated": True,
        "compacted_sections": [],
    })
    resp = await client.post("/query", json={"sql": "COMPACT MEMORY ON saturn AGGRESSIVE"})
    assert resp.status_code == 200
    call_task: str = app.state.llm.call.call_args[1]["task"]
    assert "AGGRESSIVE" in call_task


async def test_compact_memory_on_unknown_node_404(client: AsyncClient, tmp_path: Path):
    """Without a registry, targeting an unknown node returns 404."""
    resp = await client.post("/query", json={"sql": "COMPACT MEMORY ON neptune"})
    assert resp.status_code == 404


async def test_compact_memory_on_remote_node(client: AsyncClient, tmp_path: Path):
    """With a registry, COMPACT MEMORY ON peer forwards to peer's /compact."""
    cfg = {
        "nodes": [
            {"id": "saturn", "url": "http://localhost:8001", "personality": "default"},
            {"id": "pluton", "url": "http://localhost:8002", "personality": "confident"},
        ]
    }
    p = tmp_path / "cluster.yaml"
    p.write_text(yaml.dump(cfg))
    app.state.registry = NodeRegistry(p, "saturn")

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"node_id": "pluton", "compaction_count": 1}
    mock_resp.status_code = 200

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_http

        resp = await client.post("/query", json={"sql": "COMPACT MEMORY ON pluton"})

    assert resp.status_code == 200
    assert resp.json()["compaction_count"] == 1
    # Verify it hit pluton's /compact endpoint
    mock_http.post.assert_called_once()
    call_url: str = mock_http.post.call_args[0][0]
    assert "8002" in call_url
    assert "/compact" in call_url

    # clean up registry
    del app.state.registry


# ── auto-compact after write ───────────────────────────────────────────────────

async def test_auto_compact_fires_when_threshold_crossed(client: AsyncClient, tmp_path: Path):
    """If a write tips memory over threshold, auto-compact is scheduled."""
    # Lower budget so current doc size exceeds threshold
    app.state.cfg["context_budget_tokens"] = 1  # absurdly small → always compact

    compact_mock = AsyncMock(return_value={
        "task_type": "compaction",
        "memory_doc_updated": True,
        "compacted_sections": [],
    })

    # LLM call order: write first, then compact
    app.state.llm.call = AsyncMock(side_effect=[
        {
            "task_type": "write",
            "confidence": "high",
            "explanation": "Inserted Alice.",
            "affected_rows": 1,
            "memory_doc_updated": True,
            "updated_table_section": (
                "## Table: users\n| id | name |\n|----|------|\n| 1  | Alice|"
            ),
        },
        {
            "task_type": "compaction",
            "memory_doc_updated": True,
            "compacted_sections": [],
        },
    ])

    resp = await client.post("/query", json={
        "sql": "INSERT INTO users (name) VALUES ('Alice')",
        "consistency": "yolo",
    })
    assert resp.status_code == 200
    # Let the auto-compact task run
    import asyncio
    await asyncio.sleep(0)
    assert app.state.compaction_count == 1
