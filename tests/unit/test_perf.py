"""Tests for the perf instrumentation module."""

from __future__ import annotations

import unittest

from copilot_commander.perf import record, summarize, timed


class TimedContextManagerTests(unittest.TestCase):
    def test_records_sample(self) -> None:
        with timed("test.span"):
            pass
        stats = summarize(reset=True)
        names = [s.name for s in stats]
        assert "test.span" in names

    def test_multiple_samples(self) -> None:
        for _ in range(5):
            with timed("test.multi"):
                pass
        stats = summarize(reset=True)
        span = next(s for s in stats if s.name == "test.multi")
        assert span.count == 5
        assert span.avg_ms > 0.0


class RecordTests(unittest.TestCase):
    def test_manual_record(self) -> None:
        record("test.manual", 42.5)
        record("test.manual", 100.0)
        stats = summarize(reset=True)
        span = next(s for s in stats if s.name == "test.manual")
        assert span.count == 2
        assert abs(span.total_ms - 142.5) < 0.01
        assert abs(span.max_ms - 100.0) < 0.01


class SummarizeTests(unittest.TestCase):
    def test_empty_summary(self) -> None:
        summarize(reset=True)
        stats = summarize()
        assert stats == []

    def test_reset_clears_data(self) -> None:
        record("test.reset", 10.0)
        summarize(reset=True)
        stats = summarize()
        names = [s.name for s in stats]
        assert "test.reset" not in names

    def test_sorted_by_total_desc(self) -> None:
        record("test.small", 1.0)
        record("test.big", 100.0)
        stats = summarize(reset=True)
        assert stats[0].name == "test.big"
