"""
node/server.py — vibeqlite node HTTP server

Phase 1: POST /query, GET /status.
Phase 2: file-backed memory.
Phase 3: POST /gossip, DDL broadcast.
Start with: NODE_ID=saturn CONFIG_PATH=cluster.yaml uvicorn node.server:app
"""
from __future__ import annotations

import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from cluster.gossip import GossipClient
from cluster.registry import NodeRegistry
from node.llm_engine import LLMClient, LLMParseError
from node.memory import MemoryDoc


# ── startup ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    node_id = os.environ.get("NODE_ID", "").strip()
    config_path = Path(os.environ.get("CONFIG_PATH", "cluster.yaml"))

    if not node_id:
        print("ERROR: NODE_ID env var required", file=sys.stderr)
        sys.exit(1)
    if not config_path.exists():
        print(f"ERROR: config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    ids = [n["id"] for n in cfg.get("nodes", [])]
    if node_id not in ids:
        print(f"ERROR: node '{node_id}' not in {config_path}", file=sys.stderr)
        sys.exit(1)

    node_cfg = next(n for n in cfg["nodes"] if n["id"] == node_id)

    data_dir = Path(cfg.get("data_dir", "data"))
    mem_path = data_dir / f"{node_id}.md"

    app.state.node_id = node_id
    app.state.cfg = cfg
    app.state.memory = MemoryDoc(node_id=node_id, path=mem_path)
    app.state.llm = LLMClient(
        node_id=node_id,
        personality=node_cfg.get("personality", "default"),
        model=cfg.get("llm_model", "llama3.2"),
        base_url=cfg.get("llm_base_url", "http://localhost:11434"),
    )
    registry = NodeRegistry(config_path, node_id)
    app.state.registry = registry
    app.state.gossip = GossipClient(
        registry=registry,
        self_id=node_id,
        ddl_timeout_ms=cfg.get("ddl_gossip_timeout_ms", 5000),
    )
    app.state.vector_clock: dict[str, int] = {node_id: 0}
    app.state.compaction_count: int = 0
    yield


app = FastAPI(title="vibeqlite node", lifespan=lifespan)


# ── models ────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    sql: str
    consistency: str = "yolo"


# ── routes ────────────────────────────────────────────────────────────────────

@app.post("/query")
async def query(req: QueryRequest) -> dict[str, Any]:
    llm: LLMClient = app.state.llm
    mem: MemoryDoc = app.state.memory
    node_id: str = app.state.node_id

    sql_type = llm.classify_sql(req.sql)
    task = f"{sql_type.upper()}: {req.sql}"

    try:
        result = await llm.call(task=task, memory_doc=mem.get_text())
    except LLMParseError as exc:
        return _envelope(node_id, app.state.vector_clock, app.state.compaction_count,
                         vibe_error=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    peer_acks: dict[str, bool] = {}
    if result.get("memory_doc_updated"):
        if result.get("updated_table_section"):
            m = re.match(r"## Table:\s*(\S+)", result["updated_table_section"].strip())
            if m:
                mem.update_table_section(m.group(1), result["updated_table_section"])
        if result.get("updated_schema_section"):
            mem.update_schema_section(result["updated_schema_section"])
        app.state.vector_clock[node_id] = app.state.vector_clock.get(node_id, 0) + 1
        mem.save()
        if sql_type == "ddl" and result.get("updated_schema_section") and app.state.gossip:
            peer_acks = await app.state.gossip.broadcast_ddl({
                "type": "ddl_change",
                "from": node_id,
                "vector_clock": app.state.vector_clock,
                "schema_snapshot": result["updated_schema_section"],
            })

    rows = result.get("rows")
    envelope = _envelope(
        node_id,
        app.state.vector_clock,
        app.state.compaction_count,
        confidence=result.get("confidence"),
        rows=rows,
        affected_rows=result.get("affected_rows"),
        headers=list(rows[0].keys()) if rows else None,
    )
    if peer_acks:
        envelope["peer_acks"] = peer_acks
    return envelope


@app.get("/status")
async def status() -> dict[str, Any]:
    mem: MemoryDoc = app.state.memory
    cfg = app.state.cfg
    budget = cfg.get("context_budget_tokens", 8192)
    tokens = mem.token_estimate()
    return {
        "node_id": app.state.node_id,
        "vector_clock": app.state.vector_clock,
        "compaction_count": app.state.compaction_count,
        "token_estimate": tokens,
        "context_usage_pct": round(tokens / budget * 100, 1),
    }


@app.post("/gossip")
async def gossip(msg: dict) -> dict[str, Any]:
    mem: MemoryDoc = app.state.memory
    msg_type = msg.get("type", "")

    if msg_type == "ddl_change":
        schema = msg.get("schema_snapshot", "")
        if schema:
            mem.update_schema_section(schema)
            mem.save()
        return {"status": "ok", "type": msg_type}

    # Other types stubbed for future phases
    return {"status": "ok", "type": msg_type}


# ── helpers ───────────────────────────────────────────────────────────────────

def _envelope(
    node_id: str,
    vector_clock: dict,
    compaction_count: int,
    confidence: str | None = None,
    rows: list | None = None,
    affected_rows: int | None = None,
    headers: list | None = None,
    vibe_error: str | None = None,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "vector_clock": vector_clock,
        "compaction_count": compaction_count,
        "confidence": confidence,
        "rows": rows,
        "affected_rows": affected_rows,
        "headers": headers,
        "vibe_error": vibe_error,
    }
