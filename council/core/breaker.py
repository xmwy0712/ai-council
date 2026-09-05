"""Per-node circuit breaker.

Consecutive failures open the breaker for a node; a successful call or an
explicit reset (health probe) closes it again. The breaker is deliberately
in-memory: the durable, replayable view of "this node is degraded" lives in the
event log (``NodeStateChanged``), so ``council resume`` restores degraded state
without trusting a fresh process's streak counters.
"""

from __future__ import annotations

__all__ = ["CircuitBreaker"]


class CircuitBreaker:
    def __init__(self, threshold: int) -> None:
        if threshold < 1:
            raise ValueError("circuit threshold 必须 >= 1")
        self._threshold = threshold
        self._streaks: dict[str, int] = {}

    def failure(self, node_id: str) -> bool:
        """Record a failure. Returns True when this failure *tripped* the breaker."""
        streak = self._streaks.get(node_id, 0) + 1
        self._streaks[node_id] = streak
        return streak == self._threshold

    def success(self, node_id: str) -> None:
        self._streaks.pop(node_id, None)

    def is_open(self, node_id: str) -> bool:
        return self._streaks.get(node_id, 0) >= self._threshold

    def reset(self, node_id: str) -> None:
        self._streaks.pop(node_id, None)
