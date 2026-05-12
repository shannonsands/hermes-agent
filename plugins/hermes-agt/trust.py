"""Per-session trust scoring.

Each Hermes session starts at trust = 1.0. Denied tool calls decrement
trust by ``deny_penalty`` (default 0.15), bottoming out at 0.0. When trust
falls below ``threshold``, the interceptor forces human review on
otherwise-allowed actions.

Trust is in-memory per process. A future iteration can persist to
``~/.hermes/state/agt-trust.sqlite`` so trust survives session restarts.
"""
from __future__ import annotations

import threading
from typing import Dict


class TrustTracker:
    def __init__(self, *, threshold: float = 0.5, deny_penalty: float = 0.15) -> None:
        self._scores: Dict[str, float] = {}
        self._lock = threading.Lock()
        self.threshold = threshold
        self.deny_penalty = deny_penalty

    def get(self, session_id: str) -> float:
        with self._lock:
            return self._scores.setdefault(session_id or "default", 1.0)

    def adjust(self, session_id: str, delta: float) -> float:
        key = session_id or "default"
        with self._lock:
            current = self._scores.get(key, 1.0)
            new = max(0.0, min(1.0, current + delta))
            self._scores[key] = new
            return new

    def penalize(self, session_id: str) -> float:
        return self.adjust(session_id, -self.deny_penalty)

    def below_threshold(self, session_id: str) -> bool:
        return self.get(session_id) < self.threshold

    def reset(self, session_id: str = "") -> None:
        with self._lock:
            if session_id:
                self._scores.pop(session_id or "default", None)
            else:
                self._scores.clear()
