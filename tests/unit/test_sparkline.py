# ruff: noqa: E402,I001,PT009

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from muxdeck.controllers.dashboard_controller import _build_sparkline


class BuildSparklineTests(unittest.TestCase):
    def test_empty_list_returns_spaces(self) -> None:
        result = _build_sparkline([], now=datetime(2025, 1, 1, 12, 0, tzinfo=UTC))
        self.assertEqual(result, "        ")
        self.assertEqual(len(result), 8)

    def test_length_equals_bars_default(self) -> None:
        now = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        timestamps = [now - timedelta(minutes=1)]
        result = _build_sparkline(timestamps, now=now)
        self.assertEqual(len(result), 8)

    def test_length_equals_custom_bars(self) -> None:
        now = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        timestamps = [now - timedelta(minutes=1)]
        result = _build_sparkline(timestamps, now=now, bars=5)
        self.assertEqual(len(result), 5)

    def test_all_events_in_one_bucket(self) -> None:
        now = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        timestamps = [now - timedelta(seconds=10)] * 5
        result = _build_sparkline(timestamps, now=now, bars=4)
        self.assertEqual(len(result), 4)
        # The last bucket should have the tallest bar, rest should be spaces
        self.assertNotEqual(result.strip(), "")
        self.assertEqual(result[-1], "▇")

    def test_events_spread_across_buckets(self) -> None:
        now = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        window = 10  # minutes
        bars = 4
        bucket_width = timedelta(minutes=window) / bars
        timestamps = [
            now - timedelta(minutes=window) + bucket_width * i + timedelta(seconds=1)
            for i in range(bars)
        ]
        result = _build_sparkline(timestamps, now=now, window_minutes=window, bars=bars)
        self.assertEqual(len(result), bars)
        # All buckets have 1 event each, so all should be the max char
        for ch in result:
            self.assertEqual(ch, "▇")

    def test_old_events_outside_window_ignored(self) -> None:
        now = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        old = now - timedelta(minutes=20)
        result = _build_sparkline([old], now=now, window_minutes=10)
        self.assertEqual(result, "        ")

    def test_varying_counts_produce_graduated_bars(self) -> None:
        now = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        window = 10
        bars = 4
        bucket_width = timedelta(minutes=window) / bars
        # Bucket 0: 1 event, Bucket 1: 2, Bucket 2: 4, Bucket 3: 8
        timestamps: list[datetime] = []
        for bucket_idx, count in enumerate([1, 2, 4, 8]):
            for _ in range(count):
                ts = now - timedelta(minutes=window) + bucket_width * bucket_idx
                timestamps.append(ts + timedelta(seconds=1))
        result = _build_sparkline(timestamps, now=now, window_minutes=window, bars=bars)
        self.assertEqual(len(result), bars)
        # Last bucket (8 events) should be tallest
        self.assertEqual(result[-1], "▇")
        # First bucket (1 event) should be smallest non-space
        self.assertEqual(result[0], "▁")

    def test_single_event_gives_one_bar(self) -> None:
        now = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        timestamps = [now - timedelta(minutes=3)]
        result = _build_sparkline(timestamps, now=now, bars=8)
        self.assertEqual(len(result), 8)
        non_space = [ch for ch in result if ch != " "]
        self.assertEqual(len(non_space), 1)
        self.assertEqual(non_space[0], "▇")


if __name__ == "__main__":
    unittest.main()
