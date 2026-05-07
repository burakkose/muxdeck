# ruff: noqa: I001, PT009, PT027

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from muxdeck.domain.subagents import (
    ReadAgentInteraction,
    SubAgentSnapshot,
    SubAgentTree,
)


_BASE = datetime(2025, 1, 1, 12, tzinfo=UTC)


def _snapshot(
    *,
    tool_call_id: str = "call-1",
    completed_at: datetime | None = None,
) -> SubAgentSnapshot:
    return SubAgentSnapshot(
        tool_call_id=tool_call_id,
        agent_name="general-purpose",
        display_name="general-purpose",
        description="describe",
        started_at=_BASE,
        completed_at=completed_at,
    )


class SubAgentSnapshotTests(unittest.TestCase):
    def test_is_running_true_when_completed_at_is_none(self) -> None:
        snap = _snapshot()
        self.assertTrue(snap.is_running)
        self.assertIsNone(snap.duration_seconds)

    def test_duration_seconds_computed_when_completed(self) -> None:
        completed = _BASE + timedelta(seconds=12, milliseconds=500)
        snap = _snapshot(completed_at=completed)
        self.assertFalse(snap.is_running)
        assert snap.duration_seconds is not None
        self.assertAlmostEqual(snap.duration_seconds, 12.5, places=3)

    def test_optional_metric_fields_default_to_none_or_empty(self) -> None:
        snap = _snapshot()
        self.assertEqual(snap.read_interactions, ())
        self.assertIsNone(snap.total_tokens)
        self.assertIsNone(snap.duration_ms)
        self.assertIsNone(snap.total_tool_calls)
        self.assertIsNone(snap.model)
        self.assertIsNone(snap.error_message)

    def test_read_agent_interaction_dataclass_accepts_optional_result(self) -> None:
        interaction_no_result = ReadAgentInteraction(
            timestamp=_BASE,
            arguments_summary='{"agent_id":"x"}',
            result_content=None,
        )
        interaction_with_result = ReadAgentInteraction(
            timestamp=_BASE,
            arguments_summary="{}",
            result_content="output",
        )
        self.assertIsNone(interaction_no_result.result_content)
        self.assertEqual(interaction_with_result.result_content, "output")


class SubAgentTreeTests(unittest.TestCase):
    def test_running_count_and_total_count_and_is_empty(self) -> None:
        running = (_snapshot(tool_call_id="r1"), _snapshot(tool_call_id="r2"))
        recent = (_snapshot(tool_call_id="d1", completed_at=_BASE + timedelta(seconds=5)),)
        tree = SubAgentTree(
            session_id="session-x",
            running=running,
            recent=recent,
            scanned_at=_BASE,
        )
        self.assertEqual(tree.running_count, 2)
        self.assertEqual(tree.total_count, 3)
        self.assertFalse(tree.is_empty())

    def test_empty_tree_is_empty_returns_true(self) -> None:
        tree = SubAgentTree(
            session_id="session-x",
            running=(),
            recent=(),
            scanned_at=_BASE,
        )
        self.assertTrue(tree.is_empty())
        self.assertEqual(tree.total_count, 0)
        self.assertEqual(tree.running_count, 0)


if __name__ == "__main__":
    unittest.main()
