"""
cluster/gossip.py — GossipClient

Phase 0: stub. Full implementation in Phase 3 (DDL) and Phase 4 (write).
"""
from __future__ import annotations

from cluster.registry import NodeConfig, NodeRegistry


class GossipError(Exception):
    """Raised when a required gossip operation (e.g. DDL) fails."""


class GossipClient:
    """Sends typed gossip messages to cluster peers."""

    def __init__(self, registry: NodeRegistry, self_id: str) -> None:
        self.registry = registry
        self.self_id = self_id
