# ruff: noqa: I001, PT009, PT027, B017

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from typing import Literal, cast

from muxdeck.domain.replay_annotations import ReplayAnnotation


class ReplayAnnotationTests(unittest.TestCase):
    def test_kind_must_be_bookmark_or_note(self) -> None:
        with self.assertRaises(ValueError):
            ReplayAnnotation(
                session_id="session-1",
                ordinal=0,
                kind=cast(Literal["bookmark", "note"], "comment"),
            )

    def test_bookmark_with_blank_body_normalizes_to_empty_string(self) -> None:
        ann = ReplayAnnotation(
            session_id="session-1",
            ordinal=2,
            kind="bookmark",
        )
        self.assertEqual(ann.body, "")
        self.assertEqual(ann.kind, "bookmark")
        self.assertEqual(ann.session_id, "session-1")
        self.assertEqual(ann.ordinal, 2)

    def test_id_default_factory_uses_replay_annotation_prefix(self) -> None:
        ann = ReplayAnnotation(
            session_id="session-1",
            ordinal=0,
            kind="note",
            body="hello",
        )
        self.assertTrue(ann.id.startswith("replay-annotation-"))

    def test_created_at_must_be_aware(self) -> None:
        with self.assertRaises(Exception):
            ReplayAnnotation(
                session_id="session-1",
                ordinal=0,
                kind="note",
                body="hi",
                created_at=datetime(2025, 1, 1),
            )

    def test_created_at_round_trip_preserves_timezone(self) -> None:
        ts = datetime(2025, 6, 1, 9, 30, tzinfo=UTC)
        ann = ReplayAnnotation(
            session_id="session-1",
            ordinal=0,
            kind="note",
            body="hi",
            created_at=ts,
        )
        self.assertEqual(ann.created_at, ts)


if __name__ == "__main__":
    unittest.main()
