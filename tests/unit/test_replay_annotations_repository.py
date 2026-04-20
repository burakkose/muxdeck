# ruff: noqa: I001,PT009,PT027,B017

from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from muxdeck.adapters import DEFAULT_DATABASE_FILE_NAME, SQLiteStore
from muxdeck.adapters.sqlite_replay_annotations import (
    SqliteReplayAnnotationsRepository,
)
from muxdeck.config import AppConfig, PathsConfig
from muxdeck.domain.replay_annotations import ReplayAnnotation


class SqliteReplayAnnotationsRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = (
            Path(__file__).resolve().parent / "_runtime_replay_annotations" / self._testMethodName
        )
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._cleanup)
        config = AppConfig(
            paths=PathsConfig(
                state_dir=self.runtime_dir / "state",
                workspace_root=self.runtime_dir / "worktrees",
                database_path=self.runtime_dir / "state" / DEFAULT_DATABASE_FILE_NAME,
                fallback_database_path=(
                    self.runtime_dir / "legacy-state" / DEFAULT_DATABASE_FILE_NAME
                ),
            ),
            config_file=self.runtime_dir / "config.toml",
        )
        self.store = SQLiteStore.from_config(config)
        self.addCleanup(self.store.close)
        self.repo = SqliteReplayAnnotationsRepository(self.store)

    def _cleanup(self) -> None:
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)

    def test_add_and_list_round_trip(self) -> None:
        bookmark = ReplayAnnotation(session_id="sess-1", ordinal=2, kind="bookmark")
        note = ReplayAnnotation(
            session_id="sess-1",
            ordinal=5,
            kind="note",
            body="check this out",
        )
        self.repo.add(bookmark)
        self.repo.add(note)

        listed = self.repo.list_for_session("sess-1")

        self.assertEqual(tuple(item.id for item in listed), (bookmark.id, note.id))
        self.assertEqual(listed[1].body, "check this out")
        self.assertEqual(self.repo.list_for_session("other"), ())

    def test_toggle_bookmark_round_trips(self) -> None:
        added = self.repo.toggle_bookmark("sess-1", 7)
        self.assertTrue(added)
        listed = self.repo.list_for_session("sess-1")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].kind, "bookmark")

        removed = self.repo.toggle_bookmark("sess-1", 7)
        self.assertFalse(removed)
        self.assertEqual(self.repo.list_for_session("sess-1"), ())

    def test_toggle_bookmark_is_unique_per_session_ordinal(self) -> None:
        self.repo.toggle_bookmark("sess-1", 1)
        # A second insert via add() with the same (session, ordinal, kind)
        # must be rejected by the partial unique index.
        with self.assertRaises(Exception):
            self.repo.add(ReplayAnnotation(session_id="sess-1", ordinal=1, kind="bookmark"))

    def test_delete_and_update_note_body(self) -> None:
        note = ReplayAnnotation(
            session_id="sess-1",
            ordinal=3,
            kind="note",
            body="initial",
        )
        self.repo.add(note)
        updated = self.repo.update_note_body(note.id, "revised")
        self.assertTrue(updated)
        self.assertEqual(self.repo.list_for_session("sess-1")[0].body, "revised")

        deleted = self.repo.delete(note.id)
        self.assertTrue(deleted)
        self.assertEqual(self.repo.list_for_session("sess-1"), ())
        self.assertFalse(self.repo.delete(note.id))


if __name__ == "__main__":
    unittest.main()
