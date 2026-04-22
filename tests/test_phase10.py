"""Phase 10: Fun SQL Extensions tests."""
from __future__ import annotations

import json
from io import StringIO
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


# ── MemoryDoc.reload() ────────────────────────────────────────────────────────

def test_reload_picks_up_disk_changes(tmp_path: Path):
    mem = MemoryDoc("saturn", path=tmp_path / "saturn.md")
    mem.update_schema_section("## Schema\n- users: id INTEGER, name TEXT")
    mem.save()

    # Externally write a different value to disk
    (tmp_path / "saturn.md").write_text(
        "# Node: saturn\n# Vector Clock: {}\n# Compaction Count: 0\n\n"
        "## Schema\n- products: id INTEGER, price REAL\n"
    )

    mem.reload()
    assert "products" in mem.get_text()
    assert "users" not in mem.get_text()


def test_reload_no_path_is_noop():
    mem = MemoryDoc("saturn")  # no path
    original = mem.get_text()
    mem.reload()  # must not raise
    assert mem.get_text() == original


def test_reload_missing_file_is_noop(tmp_path: Path):
    mem = MemoryDoc("saturn", path=tmp_path / "missing.md")
    original = mem.get_text()
    mem.reload()
    assert mem.get_text() == original


# ── LLMClient.build_confidence_task ──────────────────────────────────────────

def test_build_confidence_task_contains_table_and_id():
    task = LLMClient.build_confidence_task("users", "42")
    assert "users" in task
    assert "id=42" in task
    assert "confidence_check" in task


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
    app.state.memory.save()
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


# ── SELECT * FROM vibe_nodes ──────────────────────────────────────────────────

async def test_select_vibe_nodes_self_only(client: AsyncClient):
    resp = await client.post("/query", json={"sql": "SELECT * FROM vibe_nodes"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["rows"] is not None
    assert len(data["rows"]) == 1
    row = data["rows"][0]
    assert row["node_id"] == "saturn"
    assert row["personality"] == "default"
    assert "token_estimate" in row
    assert "vector_clock" in row


async def test_select_vibe_nodes_case_insensitive(client: AsyncClient):
    resp = await client.post("/query", json={"sql": "select * from vibe_nodes"})
    assert resp.status_code == 200
    assert resp.json()["rows"][0]["node_id"] == "saturn"


async def test_select_vibe_nodes_with_peers(client: AsyncClient, tmp_path: Path):
    cfg = {
        "nodes": [
            {"id": "saturn", "url": "http://localhost:8001", "personality": "default"},
            {"id": "pluton", "url": "http://localhost:8002", "personality": "confident"},
        ]
    }
    p = tmp_path / "cluster.yaml"
    p.write_text(yaml.dump(cfg))
    app.state.registry = NodeRegistry(p, "saturn")

    peer_status = {
        "node_id": "pluton",
        "personality": "confident",
        "token_estimate": 100,
        "context_usage_pct": 1.2,
        "conflict_count": 0,
        "compaction_count": 0,
        "vector_clock": {},
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = peer_status

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_http

        resp = await client.post("/query", json={"sql": "SELECT * FROM vibe_nodes"})

    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) == 2
    node_ids = {r["node_id"] for r in rows}
    assert node_ids == {"saturn", "pluton"}
    del app.state.registry


# ── SHOW CONFLICTS ────────────────────────────────────────────────────────────

async def test_show_conflicts_zero(client: AsyncClient):
    resp = await client.post("/query", json={"sql": "SHOW CONFLICTS"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["rows"] == [{"conflict_count": 0}]


async def test_show_conflicts_nonzero(client: AsyncClient):
    app.state.conflict_count = 3
    resp = await client.post("/query", json={"sql": "SHOW CONFLICTS"})
    assert resp.json()["rows"][0]["conflict_count"] == 3
    app.state.conflict_count = 0


# ── SHOW SCHEMA ───────────────────────────────────────────────────────────────

async def test_show_schema_returns_schema(client: AsyncClient):
    resp = await client.post("/query", json={"sql": "SHOW SCHEMA"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["rows"] is not None
    schema_text = data["rows"][0]["schema"]
    assert "users" in schema_text


async def test_show_schema_no_schema_doc(client: AsyncClient):
    # Doc with no ## Schema section
    app.state.memory._text = "# Node: saturn\n# Vector Clock: {}\n"
    resp = await client.post("/query", json={"sql": "SHOW SCHEMA"})
    assert resp.status_code == 200
    assert "(no schema)" in resp.json()["rows"][0]["schema"]


# ── SHOW EVICTIONS ────────────────────────────────────────────────────────────

async def test_show_evictions_stub(client: AsyncClient):
    resp = await client.post("/query", json={"sql": "SHOW EVICTIONS"})
    assert resp.status_code == 200
    data = resp.json()
    assert "evictions" in data["rows"][0]


# ── REFRESH MEMORY ON ────────────────────────────────────────────────────────

async def test_refresh_memory_on_self(client: AsyncClient, tmp_path: Path):
    # Write updated content to disk externally
    (tmp_path / "saturn.md").write_text(
        "# Node: saturn\n# Vector Clock: {}\n# Compaction Count: 0\n\n"
        "## Schema\n- refreshed_table: id INTEGER\n"
    )
    resp = await client.post("/query", json={"sql": "REFRESH MEMORY ON saturn"})
    assert resp.status_code == 200
    assert "refreshed" in resp.json()["rows"][0]["status"]
    assert "refreshed_table" in app.state.memory.get_text()


async def test_refresh_memory_on_unknown_node_404(client: AsyncClient):
    resp = await client.post("/query", json={"sql": "REFRESH MEMORY ON neptune"})
    assert resp.status_code == 404


async def test_refresh_memory_on_peer(client: AsyncClient, tmp_path: Path):
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
    mock_resp.json.return_value = {"node_id": "pluton", "status": "refreshed", "token_estimate": 50}

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_http

        resp = await client.post("/query", json={"sql": "REFRESH MEMORY ON pluton"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "refreshed"
    call_url: str = mock_http.post.call_args[0][0]
    assert "8002" in call_url and "/refresh" in call_url
    del app.state.registry


# ── SELECT CONFIDENCE(*) ──────────────────────────────────────────────────────

async def test_select_confidence(client: AsyncClient):
    app.state.llm.call = AsyncMock(return_value={
        "task_type": "confidence_check",
        "confidence": "high",
        "rows": [{"id": 1, "name": "Alice"}],
        "explanation": "Row is present verbatim.",
        "memory_doc_updated": False,
    })
    resp = await client.post(
        "/query", json={"sql": "SELECT CONFIDENCE(*) FROM users WHERE id = 1"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["confidence"] == "high"
    assert data["rows"][0]["name"] == "Alice"
    # Verify correct task was built
    call_task: str = app.state.llm.call.call_args[1]["task"]
    assert "users" in call_task and "id=1" in call_task


async def test_select_confidence_llm_error_returns_vibe_error(client: AsyncClient):
    from node.llm_engine import LLMParseError
    app.state.llm.call = AsyncMock(side_effect=LLMParseError("bad"))
    resp = await client.post(
        "/query", json={"sql": "SELECT CONFIDENCE(*) FROM users WHERE id = 1"}
    )
    assert resp.status_code == 200
    assert resp.json()["vibe_error"] is not None


# ── /refresh endpoint ─────────────────────────────────────────────────────────

async def test_refresh_endpoint_reloads_disk(client: AsyncClient, tmp_path: Path):
    (tmp_path / "saturn.md").write_text(
        "# Node: saturn\n# Vector Clock: {}\n# Compaction Count: 0\n\n"
        "## Schema\n- disk_table: id INTEGER\n"
    )
    resp = await client.post("/refresh")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "refreshed"
    assert data["node_id"] == "saturn"
    assert "disk_table" in app.state.memory.get_text()


# ── CLI helpers ───────────────────────────────────────────────────────────────

def test_print_table_empty(capsys):
    from cli.vibeqlite import _print_table
    _print_table([])
    assert "(empty)" in capsys.readouterr().out


def test_print_table_with_rows(capsys):
    from cli.vibeqlite import _print_table
    _print_table([{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}])
    out = capsys.readouterr().out
    assert "Alice" in out
    assert "Bob" in out
    assert "id" in out
    assert "name" in out


def test_print_result_low_confidence_warning(capsys):
    from cli.vibeqlite import _print_result
    _print_result({
        "node_id": "saturn",
        "vector_clock": {"saturn": 1},
        "compaction_count": 0,
        "confidence": "low",
        "rows": [{"id": 1}],
        "headers": ["id"],
        "affected_rows": None,
        "vibe_error": None,
    })
    out = capsys.readouterr().out
    assert "X-Vibe-Confidence: low" in out


def test_print_result_vibe_error(capsys):
    from cli.vibeqlite import _print_result
    _print_result({
        "node_id": "saturn",
        "vector_clock": {},
        "compaction_count": 0,
        "confidence": None,
        "rows": None,
        "headers": None,
        "affected_rows": None,
        "vibe_error": {"type": "NO_MAJORITY", "detail": "split vote"},
    })
    out = capsys.readouterr().out
    assert "VIBE_ERROR" in out
    assert "NO_MAJORITY" in out


def test_print_result_affected_rows(capsys):
    from cli.vibeqlite import _print_result
    _print_result({
        "node_id": "saturn",
        "vector_clock": {},
        "compaction_count": 0,
        "confidence": "high",
        "rows": None,
        "headers": None,
        "affected_rows": 3,
        "vibe_error": None,
    })
    out = capsys.readouterr().out
    assert "3 rows affected" in out
