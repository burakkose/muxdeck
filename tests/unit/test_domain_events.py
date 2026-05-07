# ruff: noqa: I001,PT009,PT027

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from typing import Literal, cast, get_type_hints
import unittest

from muxdeck.domain.events import Event, LogChunk
from muxdeck.exceptions import DomainValidationError


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


class DomainEventBranchTests(unittest.TestCase):
    """Cover the typed-id branches of ``_normalize_id`` and severity validation."""

    def test_event_accepts_agent_id_event_id_session_id_wrappers(self) -> None:
        from muxdeck.domain.value_objects import AgentId, EventId, SessionId

        evt = Event(
            id=cast(str, EventId(value="event-x")),
            agent_id=cast(str, AgentId(value="agent-x")),
            session_id=cast(str, SessionId(value="session-x")),
            kind="ok",
            occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
            payload_json="{}",
        )
        self.assertEqual(evt.id, "event-x")
        self.assertEqual(evt.agent_id, "agent-x")
        self.assertEqual(evt.session_id, "session-x")

    def test_log_chunk_accepts_log_chunk_id_and_session_id_wrappers(self) -> None:
        from muxdeck.domain.value_objects import LogChunkId, SessionId

        chunk = LogChunk(
            id=cast(str, LogChunkId(value="logchunk-x")),
            agent_id="agent-x",
            session_id=cast(str, SessionId(value="session-x")),
            captured_at=datetime(2025, 1, 1, tzinfo=UTC),
            content="hi",
        )
        self.assertEqual(chunk.id, "logchunk-x")
        self.assertEqual(chunk.session_id, "session-x")

    def test_event_invalid_severity_raises(self) -> None:
        with self.assertRaises(DomainValidationError):
            Event(
                id="event-x",
                kind="ok",
                severity=cast(Literal["debug"], "loud"),
                occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
                payload_json="{}",
            )

    def test_log_chunk_negative_sequence_no_raises(self) -> None:
        with self.assertRaises(DomainValidationError):
            LogChunk(
                id="logchunk-x",
                agent_id="agent-x",
                source="stdout",
                sequence_no=-1,
                captured_at=datetime(2025, 1, 1, tzinfo=UTC),
                content="hi",
            )

    def test_log_chunk_empty_content_raises(self) -> None:
        with self.assertRaises(DomainValidationError):
            LogChunk(
                id="logchunk-x",
                agent_id="agent-x",
                source="stdout",
                captured_at=datetime(2025, 1, 1, tzinfo=UTC),
                content="   ",
            )

    def test_log_chunk_default_factories_generate_ids(self) -> None:
        chunk = LogChunk(
            captured_at=datetime(2025, 1, 1, tzinfo=UTC),
            content="hi",
        )
        self.assertTrue(chunk.id.startswith("logchunk-"))
        self.assertTrue(chunk.agent_id.startswith("agent-"))


if __name__ == "__main__":
    unittest.main()
