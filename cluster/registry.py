"""
cluster/registry.py — NodeRegistry

Phase 0: stub. Full implementation in Phase 3.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class NodeConfig:
    id: str
    url: str
    personality: str


class NodeRegistry:
    """Loads cluster.yaml and provides peer lookup."""

    def __init__(self, config_path: Path, self_id: str) -> None:
        with open(config_path) as f:
            raw = yaml.safe_load(f)
        self.self_id = self_id
        self.config = raw
        self._nodes: list[NodeConfig] = [
            NodeConfig(
                id=n["id"],
                url=n["url"].rstrip("/"),
                personality=n.get("personality", "default"),
            )
            for n in raw.get("nodes", [])
        ]

    def all_nodes(self) -> list[NodeConfig]:
        return list(self._nodes)

    def peers(self, exclude_self: bool = True) -> list[NodeConfig]:
        if exclude_self:
            return [n for n in self._nodes if n.id != self.self_id]
        return list(self._nodes)

    def get(self, node_id: str) -> NodeConfig | None:
        return next((n for n in self._nodes if n.id == node_id), None)
