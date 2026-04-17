"""
node/server.py — vibeqlite node HTTP server

Phase 0: empty FastAPI app. Routes added in Phase 1+.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from fastapi import FastAPI

app = FastAPI(title="vibeqlite node")

# ---------------------------------------------------------------------------
# Config is loaded at startup via lifespan or direct call from __main__.
# In Phase 0 it is stored as a module-level dict; Phase 1 replaces this
# with a proper Config dataclass and app.state.
# ---------------------------------------------------------------------------
_config: dict = {}
_node_id: str = ""


def load_config(config_path: Path, node_id: str) -> None:
    global _config, _node_id
    with open(config_path) as f:
        _config = yaml.safe_load(f)
    node_ids = [n["id"] for n in _config.get("nodes", [])]
    if node_id not in node_ids:
        print(f"ERROR: node-id '{node_id}' not found in {config_path}", file=sys.stderr)
        sys.exit(1)
    _node_id = node_id


# Routes added in Phase 1.
# Phase 0 acceptance: uvicorn starts without import errors, GET /status → 404.


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="vibeqlite node")
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--config", default="cluster.yaml")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    load_config(Path(args.config), args.node_id)
    uvicorn.run("node.server:app", host="0.0.0.0", port=args.port, reload=False)
