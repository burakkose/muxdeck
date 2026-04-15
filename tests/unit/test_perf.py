"""Tests for the perf instrumentation module."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from copilot_commander.perf import log_summary, record, summarize, timed


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


class LoggingTests(unittest.TestCase):
    def tearDown(self) -> None:
        summarize(reset=True)

    def test_timed_slow_span_skips_logging_by_default(self) -> None:
        with (
            patch.dict(os.environ, {"COMMANDER_LOG": "0"}, clear=False),
            patch("copilot_commander.perf._log.warning") as warning_log,
            patch("copilot_commander.perf._log.info") as info_log,
            patch("copilot_commander.perf.time.perf_counter", side_effect=(10.0, 10.2)),
            timed("test.slow"),
        ):
            pass

        warning_log.assert_not_called()
        info_log.assert_not_called()
        stats = summarize(reset=True)
        span = next(s for s in stats if s.name == "test.slow")
        assert span.count == 1

    def test_timed_slow_span_logs_when_command_logging_enabled(self) -> None:
        with (
            patch.dict(os.environ, {"COMMANDER_LOG": "1"}, clear=False),
            patch("copilot_commander.perf._log.warning") as warning_log,
            patch("copilot_commander.perf.time.perf_counter", side_effect=(20.0, 20.2)),
            timed("test.slow.enabled"),
        ):
            pass

        warning_log.assert_called_once()
        args = warning_log.call_args.args
        assert args[:2] == ("PERF SLOW %s: %.1fms", "test.slow.enabled")
        assert round(args[2], 1) == 200.0
        summarize(reset=True)

    def test_log_summary_resets_without_emitting_when_logging_disabled(self) -> None:
        record("test.summary", 25.0)

        with (
            patch.dict(os.environ, {"COMMANDER_LOG": "0"}, clear=False),
            patch("copilot_commander.perf._log.warning") as warning_log,
        ):
            log_summary(reset=True)

        warning_log.assert_not_called()
        assert summarize() == []
