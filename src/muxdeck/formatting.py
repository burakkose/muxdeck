"""Shared human-readable formatters.

This module exists so adapters, services, controllers, and widgets can
agree on a single duration formatting convention without each layer
inventing its own ``f"{seconds}s"`` string. Centralizing here also
lets us evolve the format (e.g. switching to ``min``/``hr`` words) in
one place if we ever want to.
"""

from __future__ import annotations


def format_duration_seconds(seconds: float) -> str:
    """Format a duration in seconds as a compact human-readable string.

    Granularity adapts to magnitude so a 2 617 s value renders as
    ``43m37s`` instead of a wall of digits, and a 14 724 s value
    renders as ``4h05m``. Negative inputs are clamped to ``0s``;
    fractional seconds are truncated to whole seconds so the output
    is stable across renders (no flickering trailing digit).
    """
    total = int(max(seconds, 0))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    if total < 86400:
        return f"{total // 3600}h{(total % 3600) // 60:02d}m"
    return f"{total // 86400}d{(total % 86400) // 3600:02d}h"


__all__ = ["format_duration_seconds"]
