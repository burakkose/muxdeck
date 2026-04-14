# ruff: noqa: I001,PT009,PT027

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from typing import Literal, cast, get_type_hints
import unittest

from copilot_commander.domain.events import Event, LogChunk
from copilot_commander.exceptions import DomainValidationError


class DomainEventTests(unittest.TestCase):
    def test_event_matches_psd_contract(self) -> None:
        self.assertEqual(
            tuple(field.name for field in fields(Event)),
            ("id", "occurred_at", "agent_id", "session_id", "kind", "severity", "payload_json"),
        )
        hints = get_type_hints(Event)
        self.assertEqual(hints["severity"], Literal["debug", "info", "warning", "error"])

        event = Event(
            id="event-123",
            occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
            agent_id="agent-123",
            session_id="session-123",
            kind="session.updated",
            severity="warning",
            payload_json='{"status":"idle"}',
        )

        self.assertEqual(event.payload_json, '{"status":"idle"}')
        self.assertEqual(event.occurred_at.tzinfo, UTC)

    def test_log_chunk_matches_psd_contract(self) -> None:
        self.assertEqual(
            tuple(field.name for field in fields(LogChunk)),
            ("id", "agent_id", "session_id", "source", "sequence_no", "captured_at", "content"),
        )
        hints = get_type_hints(LogChunk)
        self.assertEqual(hints["source"], Literal["tmux_capture", "stdout", "stderr", "system"])

        chunk = LogChunk(
            id="logchunk-123",
            agent_id="agent-123",
            session_id="session-123",
            source="stderr",
            sequence_no=2,
            captured_at=datetime(2025, 1, 1, tzinfo=UTC),
            content="first\nsecond",
        )

        self.assertEqual(chunk.source, "stderr")
        self.assertEqual(chunk.sequence_no, 2)
        self.assertEqual(chunk.captured_at.tzinfo, UTC)

    def test_event_and_log_chunk_validate_json_literals_and_timestamps(self) -> None:
        with self.assertRaises(DomainValidationError):
            Event(id="event-123", kind="bad", occurred_at=datetime(2025, 1, 1), payload_json="{}")
        with self.assertRaises(DomainValidationError):
            Event(
                id="event-123",
                kind="bad-json",
                occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
                payload_json="not-json",
            )
        with self.assertRaises(DomainValidationError):
            LogChunk(
                id="logchunk-123",
                agent_id="agent-123",
                source=cast(Literal["tmux_capture", "stdout", "stderr", "system"], "tmux"),
                captured_at=datetime(2025, 1, 1, tzinfo=UTC),
                content="oops",
            )


if __name__ == "__main__":
    unittest.main()
