"""Lightweight metrics counters (names align with design §16). No secrets in labels."""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_counters: dict[str, float] = defaultdict(float)
_gauges: dict[str, float] = {}


def inc(name: str, value: float = 1.0, **labels: Any) -> None:
    key = _label_key(name, labels)
    with _lock:
        _counters[key] += value
    logger.debug("metric_inc %s=%s labels=%s", name, value, _safe_labels(labels))


def set_gauge(name: str, value: float, **labels: Any) -> None:
    key = _label_key(name, labels)
    with _lock:
        _gauges[key] = value


def get_counter(name: str, **labels: Any) -> float:
    with _lock:
        if labels:
            return float(_counters.get(_label_key(name, labels), 0.0))
        # Sum all series for this metric name (with or without labels)
        total = 0.0
        prefix = name + "{"
        for key, val in _counters.items():
            if key == name or key.startswith(prefix):
                total += float(val)
        return total


def snapshot() -> dict[str, float]:
    with _lock:
        out = dict(_counters)
        out.update({f"gauge:{k}": v for k, v in _gauges.items()})
        return out


def reset_for_tests() -> None:
    with _lock:
        _counters.clear()
        _gauges.clear()


def _safe_labels(labels: dict[str, Any]) -> dict[str, str]:
    banned = {"api_key", "authorization", "prompt", "messages", "token", "password"}
    return {
        str(k): str(v)[:64]
        for k, v in labels.items()
        if str(k).lower() not in banned and v is not None
    }


def _label_key(name: str, labels: dict[str, Any]) -> str:
    safe = _safe_labels(labels)
    if not safe:
        return name
    parts = ",".join(f"{k}={safe[k]}" for k in sorted(safe))
    return f"{name}{{{parts}}}"
