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

from muxdeck.controllers.dashboard_controller import (
    _check_stale_output,
    _output_hashes,
)
from muxdeck.domain.enums import AgentStatus


class CheckStaleOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        _output_hashes.clear()

    def tearDown(self) -> None:
        _output_hashes.clear()

    def test_first_call_is_not_stuck(self) -> None:
        result = _check_stale_output(
            "agent-1",
            "some output",
            now=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
            agent_status=AgentStatus.RUNNING,
        )
        self.assertFalse(result)

    def test_same_output_below_threshold_not_stuck(self) -> None:
        t0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        _check_stale_output("agent-1", "output", now=t0, agent_status=AgentStatus.RUNNING)
        result = _check_stale_output(
            "agent-1",
            "output",
            now=t0 + timedelta(seconds=60),
            agent_status=AgentStatus.RUNNING,
        )
        self.assertFalse(result)

    def test_same_output_above_threshold_is_stuck(self) -> None:
        t0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        _check_stale_output("agent-1", "output", now=t0, agent_status=AgentStatus.RUNNING)
        result = _check_stale_output(
            "agent-1",
            "output",
            now=t0 + timedelta(seconds=130),
            agent_status=AgentStatus.RUNNING,
        )
        self.assertTrue(result)

    def test_first_call_uses_persisted_observed_at_to_flag_stale(self) -> None:
        t0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        result = _check_stale_output(
            "agent-1",
            "output",
            now=t0 + timedelta(seconds=130),
            agent_status=AgentStatus.RUNNING,
            observed_at=t0,
        )
        self.assertTrue(result)

    def test_hash_change_resets_timer(self) -> None:
        t0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        _check_stale_output("agent-1", "output-A", now=t0, agent_status=AgentStatus.RUNNING)
        # Wait 100 seconds, then change output
        _check_stale_output(
            "agent-1",
            "output-B",
            now=t0 + timedelta(seconds=100),
            agent_status=AgentStatus.RUNNING,
        )
        # 100 more seconds with B — total 200s since start but only 100s since change
        result = _check_stale_output(
            "agent-1",
            "output-B",
            now=t0 + timedelta(seconds=200),
            agent_status=AgentStatus.RUNNING,
        )
        self.assertFalse(result)

    def test_completed_agent_not_flagged(self) -> None:
        t0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        _check_stale_output("agent-1", "output", now=t0, agent_status=AgentStatus.RUNNING)
        result = _check_stale_output(
            "agent-1",
            "output",
            now=t0 + timedelta(seconds=300),
            agent_status=AgentStatus.COMPLETED,
        )
        self.assertFalse(result)
        self.assertNotIn("agent-1", _output_hashes)

    def test_dead_agent_not_flagged(self) -> None:
        t0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        _check_stale_output("agent-1", "output", now=t0, agent_status=AgentStatus.RUNNING)
        result = _check_stale_output(
            "agent-1",
            "output",
            now=t0 + timedelta(seconds=300),
            agent_status=AgentStatus.DEAD,
        )
        self.assertFalse(result)
        self.assertNotIn("agent-1", _output_hashes)

    def test_error_agent_not_flagged(self) -> None:
        t0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        _check_stale_output("agent-1", "output", now=t0, agent_status=AgentStatus.RUNNING)
        result = _check_stale_output(
            "agent-1",
            "output",
            now=t0 + timedelta(seconds=300),
            agent_status=AgentStatus.ERROR,
        )
        self.assertFalse(result)

    def test_idle_agent_can_be_flagged(self) -> None:
        t0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        _check_stale_output("agent-1", "output", now=t0, agent_status=AgentStatus.IDLE)
        result = _check_stale_output(
            "agent-1",
            "output",
            now=t0 + timedelta(seconds=130),
            agent_status=AgentStatus.IDLE,
        )
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
