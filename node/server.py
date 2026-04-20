"""
node/server.py — vibeqlite node HTTP server

Phase 1: POST /query, GET /status.
Phase 2: file-backed memory.
Phase 3: POST /gossip, DDL broadcast.
Phase 4: async write gossip fanout.
Phase 5: VectorClock tracking + gossip deduplication.
Phase 6: VIBE_CHECK consistency mode + conflict counter.
Phase 7: Argument Protocol — EXPLAIN VIBE, /arbitrate, correction gossip.
Phase 8: Compaction — /compact, auto-compact after writes, COMPACT MEMORY ON.
Phase 9: Node Personalities — personality injected into prompt, exposed in /status.
Start with: NODE_ID=saturn CONFIG_PATH=cluster.yaml uvicorn node.server:app
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from math import floor
from pathlib import Path
from typing import Any

import httpx
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
    app.state._arbiter_idx: int = 0
    yield


app = FastAPI(title="vibeqlite node", lifespan=lifespan)


# ── models ────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    sql: str
    consistency: str = "yolo"


class ArbitrateRequest(BaseModel):
    sql: str
    explanation_a: str
    explanation_b: str


class CompactRequest(BaseModel):
    aggressive: bool = False


# ── routes ────────────────────────────────────────────────────────────────────

@app.post("/query")
async def query(
    req: QueryRequest,
    internal: bool = Query(default=False),
) -> dict[str, Any]:
    llm: LLMClient = app.state.llm
    mem: MemoryDoc = app.state.memory
    node_id: str = app.state.node_id

    # ── COMPACT MEMORY ON <node> command ───────────────────────────────
    compact_match = re.match(
        r"COMPACT\s+MEMORY\s+ON\s+(\S+)(\s+AGGRESSIVE)?\s*$",
        req.sql.strip(),
        re.IGNORECASE,
    )
    if compact_match:
        target_id = compact_match.group(1)
        aggressive = compact_match.group(2) is not None
        registry = getattr(app.state, "registry", None)
        if target_id == node_id:
            # Compact self
            new_count = await _run_compaction(llm, mem, node_id, aggressive)
            return _envelope(node_id, app.state.vc.to_dict(), new_count)
        node_cfg = registry.get(target_id) if registry else None
        if node_cfg is None:
            raise HTTPException(status_code=404, detail=f"node '{target_id}' not found")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{node_cfg.url}/compact",
                    json={"aggressive": aggressive},
                )
            return resp.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ── EXPLAIN VIBE: return LLM explanation for the Argument Protocol ────
    if req.sql.upper().startswith("EXPLAIN VIBE"):
        original_sql = req.sql[len("EXPLAIN VIBE"):].strip()
        task = LLMClient.build_explain_task(original_sql)
        try:
            result = await llm.call(task=task, memory_doc=mem.get_text())
        except LLMParseError as exc:
            return _envelope(node_id, app.state.vc.to_dict(), app.state.compaction_count,
                             vibe_error=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return _envelope(
            node_id,
            app.state.vc.to_dict(),
            app.state.compaction_count,
            confidence=result.get("confidence"),
            rows=result.get("rows"),
        )

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

    # ── Auto-compact after writes that updated memory ──────────────────────
    if sql_type in ("write", "ddl") and result.get("memory_doc_updated"):
        cfg = app.state.cfg
        budget = cfg.get("context_budget_tokens", 8192)
        threshold_pct = cfg.get("compaction_threshold_pct", 80.0)
        if mem.needs_compaction(budget, threshold_pct):
            asyncio.create_task(_auto_compact(llm, mem, node_id))

    return envelope


@app.get("/status")
async def status() -> dict[str, Any]:
    mem: MemoryDoc = app.state.memory
    cfg = app.state.cfg
    budget = cfg.get("context_budget_tokens", 8192)
    tokens = mem.token_estimate()
    return {
        "node_id": app.state.node_id,
        "personality": app.state.llm.personality,
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

    if msg_type == "correction":
        winning_rows = msg.get("winning_rows")
        sql = msg.get("sql", "")
        if winning_rows is not None:
            llm: LLMClient = app.state.llm
            task = LLMClient.build_correction_task(sql, winning_rows)
            try:
                result = await llm.call(task=task, memory_doc=mem.get_text())
            except Exception:
                return {"status": "ok", "type": msg_type, "corrected": False}
            if result.get("updated_table_section"):
                m = re.match(r"## Table:\s*(\S+)", result["updated_table_section"].strip())
                if m:
                    mem.update_table_section(m.group(1), result["updated_table_section"])
                    mem.save()
            return {"status": "ok", "type": msg_type, "corrected": True}
        return {"status": "ok", "type": msg_type, "corrected": False}

    if msg_type == "compaction":
        # Peers receive a notification only — no memory change
        return {"status": "ok", "type": msg_type, "noted": True}

    # Other types stubbed for future phases
    return {"status": "ok", "type": msg_type}


# ── helpers ───────────────────────────────────────────────────────────────────

@app.post("/compact")
async def compact(req: CompactRequest) -> dict[str, Any]:
    """Phase 8: Trigger compaction on this node."""
    llm: LLMClient = app.state.llm
    mem: MemoryDoc = app.state.memory
    node_id: str = app.state.node_id
    new_count = await _run_compaction(llm, mem, node_id, req.aggressive)
    return _envelope(node_id, app.state.vc.to_dict(), new_count)


async def _run_compaction(
    llm: LLMClient, mem: MemoryDoc, node_id: str, aggressive: bool = False
) -> int:
    """Run compaction: backup, LLM compact, update sections, gossip."""
    mem.backup_pre_compact()
    task = LLMClient.build_compaction_task(aggressive)
    try:
        result = await llm.call(task=task, memory_doc=mem.get_text())
    except Exception:
        return app.state.compaction_count
    for section in result.get("compacted_sections") or []:
        table_name = section.get("table_name", "")
        updated = section.get("updated_table_section", "")
        if table_name and updated:
            mem.update_table_section(table_name, updated)
    new_count = mem.increment_compaction_count()
    mem.save()
    app.state.compaction_count = new_count
    if app.state.gossip:
        await app.state.gossip.broadcast_compaction({
            "type": "compaction",
            "from": node_id,
            "vector_clock": app.state.vc.to_dict(),
            "compaction_count": new_count,
        })
    return new_count


async def _auto_compact(llm: LLMClient, mem: MemoryDoc, node_id: str) -> None:
    """Background auto-compact triggered after a write crosses the threshold."""
    await _run_compaction(llm, mem, node_id, aggressive=False)


@app.post("/arbitrate")
async def arbitrate(req: ArbitrateRequest) -> dict[str, Any]:
    """Phase 7: Arbiter endpoint — pick winner between two node explanations."""
    llm: LLMClient = app.state.llm
    mem: MemoryDoc = app.state.memory
    node_id: str = app.state.node_id
    task = LLMClient.build_arbitrate_task(req.sql, req.explanation_a, req.explanation_b)
    try:
        result = await llm.call(task=task, memory_doc=mem.get_text())
    except LLMParseError as exc:
        return {"node_id": node_id, "winner": "A", "explanation": str(exc), "rows": None}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "node_id": node_id,
        "winner": result.get("winner", "A"),
        "explanation": result.get("explanation", ""),
        "rows": result.get("rows"),
    }


async def argument_protocol(
    sql: str,
    majority_rows: list | None,
    minority_responses: list[dict],
    node_id: str,
    llm: LLMClient,
    mem: MemoryDoc,
) -> dict[str, Any]:
    """Phase 7: Argument Protocol — arbitrate conflicts, correct losing nodes."""
    registry = app.state.registry
    timeout = app.state.cfg.get("gossip_timeout_ms", 2000) / 1000.0
    minority_ids = {r["_from_node"] for r in minority_responses}

    # Step 1: Self explanation
    self_explanation = ""
    try:
        self_result = await llm.call(
            task=LLMClient.build_explain_task(sql),
            memory_doc=mem.get_text(),
        )
        self_explanation = self_result.get("explanation", "")
    except Exception:
        pass

    # Step 2: Minority explanation (first minority node only, internal call)
    minority_explanation = ""
    for minority_resp in minority_responses:
        minority_node_id = minority_resp["_from_node"]
        node_cfg = registry.get(minority_node_id)
        if node_cfg:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{node_cfg.url}/query",
                        params={"internal": "true"},
                        json={"sql": f"EXPLAIN VIBE {sql}", "consistency": "yolo"},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        rows = data.get("rows") or []
                        minority_explanation = (
                            rows[0].get("explanation", "") if rows else ""
                        )
            except Exception:
                pass
        break  # use only the first minority node

    # Step 3: Pick arbiter (round-robin, excluding minority nodes)
    eligible = [n for n in registry.peers() if n.id not in minority_ids]
    arbiter_url: str | None = None
    if eligible:
        idx = getattr(app.state, "_arbiter_idx", 0)
        arbiter_url = eligible[idx % len(eligible)].url
        app.state._arbiter_idx = (idx + 1) % len(eligible)

    # Step 4: Arbitrate (via arbiter node or self)
    winning_rows = majority_rows
    if arbiter_url:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{arbiter_url}/arbitrate",
                    json={
                        "sql": sql,
                        "explanation_a": self_explanation,
                        "explanation_b": minority_explanation,
                    },
                )
                if resp.status_code == 200:
                    arb_data = resp.json()
                    if arb_data.get("winner") == "B":
                        winning_rows = arb_data.get("rows", majority_rows)
        except Exception:
            pass
    else:
        # Self-arbitrate
        try:
            arb_result = await llm.call(
                task=LLMClient.build_arbitrate_task(sql, self_explanation, minority_explanation),
                memory_doc=mem.get_text(),
            )
            if arb_result.get("winner") == "B":
                winning_rows = arb_result.get("rows", majority_rows)
        except Exception:
            pass

    # Step 5: Send correction gossip to losing minority nodes
    gossip = app.state.gossip
    if gossip:
        for minority_resp in minority_responses:
            minority_node_id = minority_resp["_from_node"]
            node_cfg = registry.get(minority_node_id)
            if node_cfg:
                asyncio.create_task(gossip._post_gossip(node_cfg, {
                    "type": "correction",
                    "from": node_id,
                    "vector_clock": app.state.vc.to_dict(),
                    "sql": sql,
                    "winning_rows": winning_rows,
                }))

    # Step 6: Return result
    envelope = _envelope(
        node_id,
        app.state.vc.to_dict(),
        app.state.compaction_count,
        rows=winning_rows,
        headers=list(winning_rows[0].keys()) if winning_rows else None,
    )
    envelope["vibe_error"] = {
        "type": "CONFLICT",
        "resolution": "argument_protocol",
        "minority_nodes": list(minority_ids),
    }
    return envelope


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
        if getattr(app.state, "registry", None) is not None:
            return await argument_protocol(
                sql, winning_group[0].get("rows"), minority_responses, node_id, llm, mem
            )
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
