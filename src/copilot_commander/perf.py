"""Lightweight performance instrumentation for hot paths.

Collects timing samples per named span and logs a summary on demand.
All state is module-level; safe to call from any thread.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

_log = logging.getLogger(__name__)

_MAX_SAMPLES: Final[int] = 200

_lock = threading.Lock()
_spans: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=_MAX_SAMPLES))


@dataclass(frozen=True, slots=True)
class SpanSummary:
    name: str
    count: int
    total_ms: float
    avg_ms: float
    max_ms: float
    min_ms: float
    p95_ms: float


@contextmanager
def timed(name: str) -> Iterator[None]:
    """Context manager that records wall-clock duration for *name*."""
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        with _lock:
            _spans[name].append(elapsed_ms)
        if elapsed_ms > 100:
            _log.warning("PERF SLOW %s: %.1fms", name, elapsed_ms)
        elif elapsed_ms > 30:
            _log.info("PERF %s: %.1fms", name, elapsed_ms)


def record(name: str, elapsed_ms: float) -> None:
    """Manually record a timing sample."""
    with _lock:
        _spans[name].append(elapsed_ms)


def summarize(*, reset: bool = False) -> list[SpanSummary]:
    """Return summary stats for all recorded spans, sorted by total_ms desc."""
    with _lock:
        snapshot = {name: list(samples) for name, samples in _spans.items()}
        if reset:
            _spans.clear()
    results: list[SpanSummary] = []
    for name, samples in snapshot.items():
        if not samples:
            continue
        samples_sorted = sorted(samples)
        count = len(samples_sorted)
        total = sum(samples_sorted)
        p95_idx = min(int(count * 0.95), count - 1)
        results.append(
            SpanSummary(
                name=name,
                count=count,
                total_ms=total,
                avg_ms=total / count,
                max_ms=samples_sorted[-1],
                min_ms=samples_sorted[0],
                p95_ms=samples_sorted[p95_idx],
            )
        )
    results.sort(key=lambda s: s.total_ms, reverse=True)
    return results


def log_summary(*, reset: bool = True) -> None:
    """Log a human-readable summary of all spans."""
    stats = summarize(reset=reset)
    if not stats:
        return
    lines = ["─── PERF SUMMARY ───"]
    for s in stats:
        lines.append(
            f"  {s.name:<40} n={s.count:<4} "
            f"tot={s.total_ms:>8.1f}ms  avg={s.avg_ms:>7.1f}ms  "
            f"p95={s.p95_ms:>7.1f}ms  max={s.max_ms:>7.1f}ms"
        )
    _log.warning("\n".join(lines))


__all__ = ["SpanSummary", "log_summary", "record", "summarize", "timed"]
