"""
node/server.py — vibeqlite node HTTP server

Phase 1: POST /query, GET /status.
Phase 2: file-backed memory.
Phase 3: POST /gossip, DDL broadcast.
Phase 4: async write gossip fanout.
Phase 5: VectorClock tracking + gossip deduplication.
Phase 6: VIBE_CHECK consistency mode + conflict counter.
Start with: NODE_ID=saturn CONFIG_PATH=cluster.yaml uvicorn node.server:app
"""
from __future__ import annotations

import json
import os
import re
import sys
from contextlib import asynccontextmanager
from math import floor
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from cluster.clock import VectorClock
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
    mem = app.state.memory
    app.state.vc = VectorClock(node_id, initial=mem._vector_clock)
    app.state.compaction_count: int = 0
    app.state.conflict_count: int = 0
    yield


app = FastAPI(title="vibeqlite node", lifespan=lifespan)


# ── models ────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    sql: str
    consistency: str = "yolo"


# ── routes ────────────────────────────────────────────────────────────────────

@app.post("/query")
async def query(
    req: QueryRequest,
    internal: bool = Query(default=False),
) -> dict[str, Any]:
    llm: LLMClient = app.state.llm
    mem: MemoryDoc = app.state.memory
    node_id: str = app.state.node_id

    # ── VIBE_CHECK fan-out (only on non-internal calls) ───────────────────
    if req.consistency == "vibe_check" and not internal:
        return await _vibe_check(req.sql, node_id, llm, mem)

    sql_type = llm.classify_sql(req.sql)
    task = f"{sql_type.upper()}: {req.sql}"

    try:
        result = await llm.call(task=task, memory_doc=mem.get_text())
    except LLMParseError as exc:
        return _envelope(node_id, app.state.vc.to_dict(), app.state.compaction_count,
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
        clock = app.state.vc.tick()
        mem.update_clock(clock)
        mem.save()
        if sql_type == "ddl" and result.get("updated_schema_section") and app.state.gossip and not internal:
            peer_acks = await app.state.gossip.broadcast_ddl({
                "type": "ddl_change",
                "from": node_id,
                "vector_clock": clock,
                "schema_snapshot": result["updated_schema_section"],
            })
        elif sql_type == "write" and app.state.gossip and not internal:
            table_name = ""
            if result.get("updated_table_section"):
                m2 = re.match(r"## Table:\s*(\S+)", result["updated_table_section"].strip())
                if m2:
                    table_name = m2.group(1)
            await app.state.gossip.broadcast_write({
                "type": "write",
                "from": node_id,
                "vector_clock": clock,
                "statement": req.sql,
                "table_name": table_name,
                "summary": result.get("explanation", ""),
            })

    rows = result.get("rows")
    envelope = _envelope(
        node_id,
        app.state.vc.to_dict(),
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
        "vector_clock": app.state.vc.to_dict(),
        "compaction_count": app.state.compaction_count,
        "conflict_count": app.state.conflict_count,
        "token_estimate": tokens,
        "context_usage_pct": round(tokens / budget * 100, 1),
    }


@app.post("/gossip")
async def gossip(msg: dict) -> dict[str, Any]:
    mem: MemoryDoc = app.state.memory
    msg_type = msg.get("type", "")

    # Deduplicate replayed messages
    if app.state.gossip and app.state.gossip.check_and_mark_seen(msg):
        return {"status": "ok", "type": msg_type, "duplicate": True}

    # Merge incoming clock
    if msg.get("vector_clock"):
        app.state.vc.merge(msg["vector_clock"])

    if msg_type == "ddl_change":
        schema = msg.get("schema_snapshot", "")
        if schema:
            mem.update_schema_section(schema)
            mem.save()
        return {"status": "ok", "type": msg_type}

    if msg_type == "write":
        llm: LLMClient = app.state.llm
        task = LLMClient.build_gossip_task(msg)
        try:
            result = await llm.call(task=task, memory_doc=mem.get_text())
        except Exception:
            return {"status": "ok", "type": msg_type, "merged": False}
        if result.get("updated_table_section"):
            m = re.match(r"## Table:\s*(\S+)", result["updated_table_section"].strip())
            if m:
                mem.update_table_section(m.group(1), result["updated_table_section"])
                mem.save()
        return {"status": "ok", "type": msg_type, "merged": True}

    # Other types stubbed for future phases
    return {"status": "ok", "type": msg_type}


# ── helpers ───────────────────────────────────────────────────────────────────

async def _vibe_check(
    sql: str,
    node_id: str,
    llm: LLMClient,
    mem: MemoryDoc,
) -> dict[str, Any]:
    """Fan out to all peers + self, pick majority, track conflicts."""
    # Self answer
    sql_type = llm.classify_sql(sql)
    task = f"{sql_type.upper()}: {sql}"
    self_rows: list | None = None
    try:
        self_result = await llm.call(task=task, memory_doc=mem.get_text())
        self_rows = self_result.get("rows")
    except Exception:
        pass

    self_response: dict[str, Any] = {
        "_from_node": node_id,
        "rows": self_rows,
        "vector_clock": app.state.vc.to_dict(),
    }

    # Peer answers
    peer_responses: list[dict] = []
    if app.state.gossip:
        cfg = app.state.cfg
        peer_responses = await app.state.gossip.broadcast_read(
            sql,
            timeout_ms=cfg.get("gossip_timeout_ms", 2000),
        )

    all_responses = [self_response] + peer_responses
    n = len(all_responses)
    majority_threshold = floor(n / 2) + 1

    # Group by normalised rows content
    groups: dict[str, list[dict]] = {}
    for resp in all_responses:
        key = json.dumps(resp.get("rows"), sort_keys=True, default=str)
        groups.setdefault(key, []).append(resp)

    # Find winning group
    winning_key = max(groups, key=lambda k: len(groups[k]))
    winning_group = groups[winning_key]
    minority_groups = [g for k, g in groups.items() if k != winning_key]
    minority_responses = [r for g in minority_groups for r in g]

    if len(winning_group) < majority_threshold:
        # No majority — pure split
        return _envelope(
            node_id,
            app.state.vc.to_dict(),
            app.state.compaction_count,
            vibe_error={"type": "NO_MAJORITY", "all_responses": all_responses},
        )

    envelope = _envelope(
        node_id,
        app.state.vc.to_dict(),
        app.state.compaction_count,
        rows=winning_group[0].get("rows"),
        headers=list(winning_group[0]["rows"][0].keys()) if winning_group[0].get("rows") else None,
    )

    if minority_responses:
        app.state.conflict_count += 1
        envelope["vibe_error"] = {
            "type": "CONFLICT",
            "minority_responses": minority_responses,
        }

    return envelope

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
