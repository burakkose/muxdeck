# ruff: noqa: E402,I001,PT009

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
from typing import Literal
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from muxdeck.domain.events import Event, LogChunk
from muxdeck.services.replay_insights import (
    IDLE_GAP_THRESHOLD,
    compute_replay_insights,
)
from muxdeck.services.replay_service import ReplayEntry

EventSeverity = Literal["debug", "info", "warning", "error"]


def _event_entry(
    *,
    ordinal: int,
    ts: datetime,
    kind: str = "k",
    severity: EventSeverity = "info",
) -> ReplayEntry:
    return ReplayEntry(
        kind="event",
        timestamp=ts,
        ordinal=ordinal,
        session_id="s",
        agent_id="a",
        event=Event(occurred_at=ts, kind=kind, payload_json="{}", severity=severity),
    )


def _log_entry(*, ordinal: int, ts: datetime, content: str) -> ReplayEntry:
    return ReplayEntry(
        kind="log",
        timestamp=ts,
        ordinal=ordinal,
        session_id="s",
        agent_id="a",
        log_chunk=LogChunk(
            session_id="s",
            agent_id="a",
            sequence_no=ordinal,
            captured_at=ts,
            source="stdout",
            content=content,
        ),
    )


class ComputeReplayInsightsTests(unittest.TestCase):
    def test_empty_returns_zeroed_view(self) -> None:
        view = compute_replay_insights(())
        self.assertEqual(view.total_duration, timedelta())
        self.assertEqual(view.idle_gaps, ())
        self.assertEqual(view.error_count, 0)
        self.assertEqual(view.files_touched, 0)

    def test_total_duration_spans_first_to_last(self) -> None:
        start = datetime(2025, 1, 1, 12, tzinfo=UTC)
        entries = (
            _event_entry(ordinal=0, ts=start),
            _event_entry(ordinal=1, ts=start + timedelta(seconds=30)),
            _event_entry(ordinal=2, ts=start + timedelta(seconds=120)),
        )
        view = compute_replay_insights(entries)
        self.assertEqual(view.total_duration, timedelta(seconds=120))

    def test_idle_gap_threshold_strictly_greater_than_60s(self) -> None:
        start = datetime(2025, 1, 1, 12, tzinfo=UTC)
        # 60s gap exactly → NOT idle
        entries = (
            _event_entry(ordinal=0, ts=start),
            _event_entry(ordinal=1, ts=start + IDLE_GAP_THRESHOLD),
        )
        self.assertEqual(compute_replay_insights(entries).idle_gaps, ())
        # 61s gap → idle
        entries = (
            _event_entry(ordinal=0, ts=start),
            _event_entry(ordinal=1, ts=start + IDLE_GAP_THRESHOLD + timedelta(seconds=1)),
        )
        gaps = compute_replay_insights(entries).idle_gaps
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].duration, timedelta(seconds=61))

    def test_longest_streak_excludes_idle_gaps(self) -> None:
        start = datetime(2025, 1, 1, 12, tzinfo=UTC)
        # streak A spans 30s, then a 120s idle gap, then streak B
        # spans 90s built from two non-idle 45s sub-gaps.
        entries = (
            _event_entry(ordinal=0, ts=start),
            _event_entry(ordinal=1, ts=start + timedelta(seconds=30)),
            _event_entry(ordinal=2, ts=start + timedelta(seconds=30 + 120)),
            _event_entry(ordinal=3, ts=start + timedelta(seconds=30 + 120 + 45)),
            _event_entry(ordinal=4, ts=start + timedelta(seconds=30 + 120 + 90)),
        )
        view = compute_replay_insights(entries)
        self.assertEqual(view.longest_activity_streak, timedelta(seconds=90))

    def test_error_count_and_clusters_wired(self) -> None:
        start = datetime(2025, 1, 1, 12, tzinfo=UTC)
        # Same canonical error in two log chunks (paths/numbers differ).
        entries = (
            _log_entry(
                ordinal=0,
                ts=start,
                content="fatal: connection refused on /tmp/sock-1 port 5432\n",
            ),
            _log_entry(
                ordinal=1,
                ts=start + timedelta(seconds=10),
                content="fatal: connection refused on /tmp/sock-2 port 5433\n",
            ),
        )
        view = compute_replay_insights(entries)
        self.assertGreaterEqual(view.error_count, 2)
        self.assertGreaterEqual(len(view.top_error_clusters), 1)
        # The top cluster should bundle both into one canonical entry.
        top = view.top_error_clusters[0]
        self.assertGreaterEqual(top.count, 2)
        self.assertNotIn("5432", top.canonical)
        self.assertNotIn("/tmp/sock-1", top.canonical)

    def test_files_touched_gracefully_zero_without_parser_field(self) -> None:
        start = datetime(2025, 1, 1, 12, tzinfo=UTC)
        view = compute_replay_insights(
            (_log_entry(ordinal=0, ts=start, content="just a log line"),)
        )
        self.assertEqual(view.files_touched, 0)


if __name__ == "__main__":
    unittest.main()
