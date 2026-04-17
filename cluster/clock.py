"""
cluster/clock.py — VectorClock

Phase 0: stub. Full implementation in Phase 5.
"""
from __future__ import annotations


class VectorClock:
    """Lightweight vector clock — dict of {node_id: int}."""

    def __init__(self, node_id: str, initial: dict[str, int] | None = None) -> None:
        self.node_id = node_id
        self._clock: dict[str, int] = dict(initial or {})

    def tick(self) -> dict[str, int]:
        """Increment own counter and return a copy."""
        self._clock[self.node_id] = self._clock.get(self.node_id, 0) + 1
        return dict(self._clock)

    def merge(self, other: dict[str, int]) -> dict[str, int]:
        """Merge by taking the max of each component."""
        for k, v in other.items():
            self._clock[k] = max(self._clock.get(k, 0), v)
        return dict(self._clock)

    def dominates(self, other: dict[str, int]) -> bool:
        """True if self >= other on every component present in other."""
        return all(self._clock.get(k, 0) >= v for k, v in other.items())

    def to_dict(self) -> dict[str, int]:
        return dict(self._clock)
