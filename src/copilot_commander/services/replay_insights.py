"""Replay insights — pure summary stats over a replay's entries.

Lives in the services layer because it composes domain helpers
(``error_clustering``) with parser output, but takes plain
``ReplayEntry`` instances and returns only typed value objects. No
SQLite, no subprocess, no Textual.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise

from copilot_commander.domain.error_clustering import ErrorCluster, cluster_errors
from copilot_commander.parsers.copilot_output_parser import parse_copilot_output
from copilot_commander.services.replay_service import ReplayEntry

__all__ = [
    "IDLE_GAP_THRESHOLD",
    "IdleGap",
    "ReplayInsightsView",
    "compute_replay_insights",
]


# Gaps strictly **greater than** 60 seconds are reported. A boundary
# of exactly 60s is *not* idle — useful for smoothing over the typical
# Copilot heartbeat cadence.
IDLE_GAP_THRESHOLD: timedelta = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class IdleGap:
    start: datetime
    end: datetime
    duration: timedelta


@dataclass(frozen=True, slots=True)
class ReplayInsightsView:
    total_duration: timedelta
    idle_gaps: tuple[IdleGap, ...]
    longest_activity_streak: timedelta
    error_count: int
    top_error_clusters: tuple[ErrorCluster, ...]
    files_touched: int


def compute_replay_insights(
    entries: Iterable[ReplayEntry],
    *,
    top_error_clusters: int = 3,
) -> ReplayInsightsView:
    """Summarize a replay's entries.

    The function is intentionally tolerant of partially-shaped entries
    (for example, a future ``file_mutations`` parser field) so callers
    do not need to feature-flag the call.
    """

    materialized = tuple(entries)
    if not materialized:
        return ReplayInsightsView(
            total_duration=timedelta(),
            idle_gaps=(),
            longest_activity_streak=timedelta(),
            error_count=0,
            top_error_clusters=(),
            files_touched=0,
        )

    timestamps = tuple(entry.timestamp for entry in materialized)
    total_duration = timestamps[-1] - timestamps[0]

    idle_gaps: list[IdleGap] = []
    longest_streak = timedelta()
    streak_start = timestamps[0]
    for previous, current in pairwise(timestamps):
        gap = current - previous
        if gap > IDLE_GAP_THRESHOLD:
            idle_gaps.append(IdleGap(start=previous, end=current, duration=gap))
            streak_duration = previous - streak_start
            if streak_duration > longest_streak:
                longest_streak = streak_duration
            streak_start = current
    final_streak = timestamps[-1] - streak_start
    if final_streak > longest_streak:
        longest_streak = final_streak

    error_messages: list[str] = []
    file_paths: set[str] = set()
    for entry in materialized:
        if entry.event is not None and entry.event.severity == "error":
            error_messages.append(entry.event.kind)
        if entry.log_chunk is None:
            continue
        parsed = parse_copilot_output(entry.log_chunk.content)
        error_messages.extend(error.message for error in parsed.errors)
        # Future-compatible: the parser may grow ``file_mutations``
        # carrying ``path`` attributes. Until then this loop is a
        # no-op and ``files_touched`` stays at 0.
        file_mutations = getattr(parsed, "file_mutations", ())
        for mutation in file_mutations:
            path = getattr(mutation, "path", None)
            if isinstance(path, str) and path:
                file_paths.add(path)

    clusters = cluster_errors(error_messages, top_n=top_error_clusters)

    return ReplayInsightsView(
        total_duration=total_duration,
        idle_gaps=tuple(idle_gaps),
        longest_activity_streak=longest_streak,
        error_count=len(error_messages),
        top_error_clusters=clusters,
        files_touched=len(file_paths),
    )
