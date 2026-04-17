"""
cluster/gossip.py — GossipClient

Phase 3: DDL broadcast (synchronous, awaits all peers).
Phase 4: write broadcast (fire-and-forget).
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from cluster.registry import NodeConfig, NodeRegistry

log = logging.getLogger(__name__)


class GossipError(Exception):
    """Raised when a required gossip operation (e.g. DDL) fails."""


class GossipClient:
    """Sends typed gossip messages to cluster peers."""

    def __init__(self, registry: NodeRegistry, self_id: str, ddl_timeout_ms: int = 5000) -> None:
        self.registry = registry
        self.self_id = self_id
        self._ddl_timeout = ddl_timeout_ms / 1000.0

    async def _post_gossip(self, node: NodeConfig, message: dict) -> bool:
        """POST gossip to one peer. Returns True on 200."""
        try:
            async with httpx.AsyncClient(timeout=self._ddl_timeout) as client:
                resp = await client.post(f"{node.url}/gossip", json=message)
                return resp.status_code == 200
        except Exception as exc:
            log.warning("gossip to %s failed: %s", node.id, exc)
            return False

    async def broadcast_ddl(self, message: dict) -> dict[str, bool]:
        """Synchronous DDL broadcast — awaits all peers, returns per-node success."""
        peers = self.registry.peers()
        results = await asyncio.gather(
            *[self._post_gossip(peer, message) for peer in peers],
            return_exceptions=False,
        )
        return {peer.id: ok for peer, ok in zip(peers, results)}

    async def broadcast_write(self, message: dict) -> None:
        """Fire-and-forget write gossip (Phase 4)."""
        peers = self.registry.peers()
        for peer in peers:
            asyncio.create_task(self._post_gossip(peer, message))
