"""Phase 1: single-node query loop tests."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from node.llm_engine import LLMClient
from node.memory import MemoryDoc
from node.server import app


# ── MemoryDoc unit tests ─────────────────────────────────────────────

def test_blank_doc_contains_schema():
    mem = MemoryDoc("saturn")
    assert "## Schema" in mem.get_text()
    assert "# Node: saturn" in mem.get_text()


def test_update_schema_section():
    mem = MemoryDoc("saturn")
    mem.update_schema_section("## Schema\n- users: id INTEGER, name TEXT")
    assert "- users: id INTEGER, name TEXT" in mem.get_text()
    assert "(no tables yet)" not in mem.get_text()


def test_update_schema_section_idempotent():
    mem = MemoryDoc("saturn")
    mem.update_schema_section("## Schema\n- users: id INTEGER")
    mem.update_schema_section("## Schema\n- users: id INTEGER, name TEXT")
    assert mem.get_text().count("## Schema") == 1
    assert "name TEXT" in mem.get_text()


def test_update_table_section_append():
    mem = MemoryDoc("saturn")
    section = "## Table: users\n| id | name |\n|----|------|\n| 1  | Alice |"
    mem.update_table_section("users", section)
    assert "## Table: users" in mem.get_text()
    assert "Alice" in mem.get_text()


def test_update_table_section_replace():
    mem = MemoryDoc("saturn")
    mem.update_table_section("users", "## Table: users\n| id |\n|----|\n| 1  |")
    mem.update_table_section("users", "## Table: users\n| id |\n|----|\n| 1  |\n| 2  |")
    assert mem.get_text().count("## Table: users") == 1
    assert "| 2  |" in mem.get_text()


def test_token_estimate_nonzero():
    mem = MemoryDoc("saturn")
    assert mem.token_estimate() > 0


# ── server integration tests (mocked LLM) ──────────────────────────────────

@pytest.fixture()
async def client(tmp_path: Path):
    """Set up app.state directly — ASGITransport does not trigger lifespan."""
    cfg = {
        "context_budget_tokens": 8192,
        "compaction_threshold_pct": 80,
        "gossip_timeout_ms": 2000,
        "ddl_gossip_timeout_ms": 5000,
        "llm_model": "llama3.2",
        "llm_base_url": "http://localhost:11434",
        "data_dir": "data",
        "nodes": [
            {"id": "saturn", "url": "http://localhost:8001", "personality": "default"}
        ],
    }
    app.state.node_id = "saturn"
    app.state.cfg = cfg
    app.state.memory = MemoryDoc(node_id="saturn")
    app.state.llm = LLMClient(
        node_id="saturn",
        personality="default",
        model="llama3.2",
        base_url="http://localhost:11434",
    )
    app.state.vector_clock = {"saturn": 0}
    app.state.compaction_count = 0

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_status(client: AsyncClient):
    resp = await client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["node_id"] == "saturn"
    assert data["vector_clock"] == {"saturn": 0}
    assert data["token_estimate"] > 0


async def test_query_ddl(client: AsyncClient):
    llm_result = {
        "task_type": "ddl",
        "confidence": "high",
        "explanation": "Created users table.",
        "memory_doc_updated": True,
        "updated_schema_section": "## Schema\n- users: id INTEGER, name TEXT, age INTEGER",
    }
    app.state.llm.call = AsyncMock(return_value=llm_result)

    resp = await client.post(
        "/query",
        json={"sql": "CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)", "consistency": "yolo"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["vibe_error"] is None
    assert data["node_id"] == "saturn"
    # Schema update bumped vector clock
    assert data["vector_clock"]["saturn"] == 1
    # Memory doc now contains schema
    assert "users: id INTEGER" in app.state.memory.get_text()


async def test_query_insert(client: AsyncClient):
    # Pre-load schema via mock DDL
    app.state.memory.update_schema_section(
        "## Schema\n- users: id INTEGER, name TEXT, age INTEGER"
    )
    llm_result = {
        "task_type": "write",
        "confidence": "high",
        "explanation": "Inserted Alice.",
        "affected_rows": 1,
        "memory_doc_updated": True,
        "updated_table_section": (
            "## Table: users\n"
            "| id | name  | age |\n"
            "|----|-------|-----|\n"
            "| 1  | Alice | 30  |"
        ),
    }
    app.state.llm.call = AsyncMock(return_value=llm_result)

    resp = await client.post(
        "/query",
        json={"sql": "INSERT INTO users (name, age) VALUES ('Alice', 30)", "consistency": "yolo"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["affected_rows"] == 1
    assert "Alice" in app.state.memory.get_text()


async def test_query_select(client: AsyncClient):
    app.state.memory.update_table_section(
        "users",
        "## Table: users\n| id | name  | age |\n|----|-------|-----|\n| 1  | Alice | 30  |",
    )
    llm_result = {
        "task_type": "read",
        "confidence": "high",
        "rows": [{"id": 1, "name": "Alice", "age": 30}],
        "explanation": "Found Alice.",
        "memory_doc_updated": False,
    }
    app.state.llm.call = AsyncMock(return_value=llm_result)

    resp = await client.post(
        "/query",
        json={"sql": "SELECT * FROM users", "consistency": "yolo"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["rows"] == [{"id": 1, "name": "Alice", "age": 30}]
    assert data["headers"] == ["id", "name", "age"]


async def test_query_llm_parse_error_returns_vibe_error(client: AsyncClient):
    from node.llm_engine import LLMParseError
    app.state.llm.call = AsyncMock(side_effect=LLMParseError("bad json"))

    resp = await client.post("/query", json={"sql": "SELECT 1"})
    assert resp.status_code == 200
    assert resp.json()["vibe_error"] is not None
