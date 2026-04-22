"""
cluster/gossip.py — GossipClient

Phase 3: DDL broadcast (synchronous, awaits all peers).
Phase 4: write broadcast (fire-and-forget).
Phase 5: deduplication via seen-set; attach clock to outgoing messages.
Phase 6: broadcast_read() for VIBE_CHECK fan-out.
Phase 8: broadcast_compaction() fire-and-forget notification.
Phase 11: broadcast_election(), announce_winner().
"""
from __future__ import annotations

import asyncio
import json
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
        self._seen: set[str] = set()

    def _msg_key(self, message: dict) -> str:
        """Stable key for dedup: '{from}:{sorted-clock-json}'."""
        from_node = message.get("from", "")
        clock = message.get("vector_clock", {})
        clock_str = json.dumps(clock, sort_keys=True)
        return f"{from_node}:{clock_str}"

    def check_and_mark_seen(self, message: dict) -> bool:
        """Return True if this message is a duplicate. Mark as seen if not."""
        key = self._msg_key(message)
        if key in self._seen:
            return True
        self._seen.add(key)
        return False

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

    async def broadcast_read(self, sql: str, timeout_ms: int = 2000) -> list[dict]:
        """Fan out a read query to all peers and collect responses (Phase 6).

        Peers are called with ?internal=true so they skip gossip fanout.
        Non-responding peers are excluded (Timeout-Based Exclusion).
        Returns a list of response dicts (one per responding peer).
        """
        timeout = timeout_ms / 1000.0
        peers = self.registry.peers()

        async def _query_peer(node: NodeConfig) -> dict | None:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{node.url}/query",
                        params={"internal": "true"},
                        json={"sql": sql, "consistency": "yolo"},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        data["_from_node"] = node.id
                        return data
            except Exception as exc:
                log.warning("vibe_check read from %s failed: %s", node.id, exc)
            return None

        results = await asyncio.gather(*[_query_peer(p) for p in peers])
        return [r for r in results if r is not None]

    async def broadcast_compaction(self, message: dict) -> None:
        """Fire-and-forget compaction notification to all peers (Phase 8)."""
        peers = self.registry.peers()
        for peer in peers:
            asyncio.create_task(self._post_gossip(peer, message))

    async def broadcast_election(self, timeout_ms: int = 2000) -> list[dict]:
        """POST /election to all peers and collect their campaign cases (Phase 11)."""
        timeout = timeout_ms / 1000.0
        peers = self.registry.peers()

        async def _call_peer(node: NodeConfig) -> dict | None:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(f"{node.url}/election")
                    if resp.status_code == 200:
                        data = resp.json()
                        data["_from_node"] = node.id
                        return data
            except Exception as exc:
                log.warning("election call to %s failed: %s", node.id, exc)
            return None

        results = await asyncio.gather(*[_call_peer(p) for p in peers])
        return [r for r in results if r is not None]

    async def announce_winner(self, message: dict) -> None:
        """Fire-and-forget election_result announcement to all peers (Phase 11)."""
        peers = self.registry.peers()
        for peer in peers:
            asyncio.create_task(self._post_gossip(peer, message))
